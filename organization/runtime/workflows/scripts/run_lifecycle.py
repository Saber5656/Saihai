#!/usr/bin/env python3
"""Workflow-run lifecycle state machine and durable transition helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import run_lock
import run_store

RUN_STATES = run_store.RUN_STATES
TERMINAL_RUN_STATES = run_store.TERMINAL_RUN_STATES

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "created": {"step_queued", "waiting_human", "aborted"},
    "step_queued": {"waiting_provider", "waiting_human", "aborted"},
    "waiting_provider": {"step_queued", "validating", "waiting_human", "failed", "aborted"},
    "validating": {"complete", "failed", "waiting_human", "aborted"},
    "waiting_human": {"step_queued", "failed", "aborted"},
    "remediating": {"step_queued", "failed", "aborted"},
    "complete": set(),
    "failed": set(),
    "aborted": set(),
}

GOAL_STATE_FOR_RUN_STATE = {
    "created": "approved",
    "step_queued": "active",
    "waiting_provider": "active",
    "validating": "active",
    "remediating": "active",
    "waiting_human": "blocked",
    "complete": "complete",
    "failed": "blocked",
    "aborted": "aborted",
}

EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
    "scoped_worker_executor",
}


class LifecycleError(RuntimeError):
    """Typed lifecycle failure with a stable reason_class."""

    def __init__(self, reason_class: str, errors: list[str] | None = None) -> None:
        super().__init__(reason_class)
        self.reason_class = reason_class
        self.errors = errors or []


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_id(value: str) -> str:
    allowed = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    compact = "".join(allowed).strip(".-")
    return compact[:96] or "anonymous"


def redacted_principal(principal: dict[str, Any]) -> dict[str, str]:
    return {
        "principal_type": str(principal.get("principal_type") or "unknown"),
        "principal_id": str(principal.get("principal_id") or "unknown"),
        "authn_method": str(principal.get("authn_method") or "unknown"),
    }


def signing_key_path(state_root: Path, principal: dict[str, Any]) -> Path:
    principal_id = str(principal.get("principal_id") or "anonymous")
    digest = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:24]
    principal_type = safe_id(str(principal.get("principal_type") or "principal"))
    return state_root / "principal-keys" / f"{principal_type}-{digest}.key"


def _read_private_file_text(path: Path, *, label: str) -> str:
    if path.is_symlink():
        raise LifecycleError("signing_key_unavailable", [f"{label} must not be a symlink: {path}"])
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        path.chmod(0o600)
        mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise LifecycleError("signing_key_unavailable", [f"{label} must have 0600 permissions: {path}"])
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError("signing_key_unavailable", [f"{label} cannot be opened safely: {path}"]) from exc
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def principal_key(state_root: Path, principal: dict[str, Any]) -> bytes:
    path = signing_key_path(state_root, principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(32) + "\n")
    return _read_private_file_text(path, label="principal signing key").encode("utf-8")


def sign_transition(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> dict[str, str]:
    material = {
        "principal": redacted_principal(principal),
        "transition": transition,
        "subject": subject,
    }
    signature = hmac.new(
        principal_key(state_root, principal),
        canonical_json(material),
        hashlib.sha256,
    ).hexdigest()
    return {
        "algorithm": "sha256-local-principal-key",
        "signature": "sha256:" + signature,
        "signed_at": now_iso(),
    }


def _normalized_artifact_refs(artifact_refs: list[str] | None) -> list[str]:
    if artifact_refs is None:
        return []
    return [str(item) for item in artifact_refs]


def assert_execution_principal(principal: dict[str, Any]) -> None:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type not in EXECUTION_PRINCIPAL_TYPES:
        raise LifecycleError(
            "unsupported_execution_principal",
            [f"unsupported execution principal: {principal_type}"],
        )


def transition_run(
    state_root: Path,
    run_id: str,
    *,
    to_state: str,
    reason_class: str,
    transition: str,
    principal: dict[str, Any],
    artifact_refs: list[str] | None = None,
    terminal_status: str | None = None,
    terminal_reason: str | None = None,
    expected_current_state: str | None = None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance a run through the canonical lifecycle table and persist it."""

    assert_execution_principal(principal)
    run = run if run is not None else run_store.load_run(state_root, run_id)
    from_state = str(run.get("run_state") or "")
    effective_run_id = str(run.get("run_id") or run_id)

    if from_state in TERMINAL_RUN_STATES:
        raise LifecycleError("terminal_state_immutable", [f"run is terminal: {from_state}"])
    if from_state not in ALLOWED_TRANSITIONS:
        raise LifecycleError("illegal_transition", [f"unknown run state: {from_state}"])
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise LifecycleError("illegal_transition", [f"{from_state} -> {to_state} is not allowed"])
    if to_state not in GOAL_STATE_FOR_RUN_STATE:
        raise LifecycleError("illegal_transition", [f"unknown target run state: {to_state}"])

    transitions = run.setdefault("transitions", [])
    if not isinstance(transitions, list):
        raise LifecycleError("invalid_transition_history", ["transitions must be a list"])

    record = {
        "transition_version": "1",
        "seq": len(transitions) + 1,
        "transition": transition,
        "from_state": from_state,
        "to_state": to_state,
        "reason_class": reason_class,
        "occurred_at": now_iso(),
        "principal": redacted_principal(principal),
        "run_id": effective_run_id,
        "artifact_refs": _normalized_artifact_refs(artifact_refs),
    }
    record["signature"] = sign_transition(
        state_root=state_root,
        principal=principal,
        transition=transition,
        subject=record,
    )
    transitions.append(record)
    run["run_state"] = to_state
    run["goal_state"] = GOAL_STATE_FOR_RUN_STATE[to_state]
    if to_state in TERMINAL_RUN_STATES:
        run["terminal"] = {
            "status": terminal_status or to_state,
            "reason": terminal_reason or reason_class,
        }

    run_store.store_run(
        state_root,
        run,
        expected_current_state=expected_current_state or from_state,
    )
    return record


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_root
        / "work-orders"
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}.json"
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise LifecycleError("invalid_work_order", [f"expected object json: {path}"])
    return payload


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if len(raw) >= 5 and raw[-5] in {"+", "-"} and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _runner_claim_expired(work_order: dict[str, Any]) -> bool:
    authority = work_order.get("work_order_authority")
    if not isinstance(authority, dict):
        return True
    claim = authority.get("runner_claim")
    if not isinstance(claim, dict):
        return True
    if claim.get("claim_state") != "claimed":
        return True
    lease_expires_at = _parse_timestamp(claim.get("lease_expires_at"))
    if lease_expires_at is None:
        return True
    return lease_expires_at <= datetime.now(timezone.utc)


def _reset_runner_claim(work_order: dict[str, Any]) -> None:
    authority = work_order.setdefault("work_order_authority", {})
    if not isinstance(authority, dict):
        raise LifecycleError("invalid_work_order", ["work_order_authority must be an object"])
    authority["runner_claim"] = {
        "claim_state": "unclaimed",
        "lease_expires_at": None,
    }


def resume_run(
    state_root: Path,
    run_id: str,
    *,
    principal: dict[str, Any],
    requeue: bool = False,
) -> dict[str, Any]:
    assert_execution_principal(principal)
    with run_lock.hold_global_lock(
        state_root,
        operation="resume_run",
        run_id=run_id,
        principal=principal,
    ):
        run = run_store.load_run(state_root, run_id)
        run_state = str(run.get("run_state") or "")

        if run_state == "created":
            return {
                "schema_version": 1,
                "decision": "ok",
                "resumed": False,
                "next_action": "drain",
                "workflow_run": run,
            }
        if run_state == "step_queued":
            return {
                "schema_version": 1,
                "decision": "ok",
                "resumed": False,
                "next_action": "run_step",
                "workflow_run": run,
            }
        if run_state == "validating":
            return {
                "schema_version": 1,
                "decision": "ok",
                "resumed": False,
                "next_action": "validate_report",
                "workflow_run": run,
            }
        if run_state in TERMINAL_RUN_STATES:
            return {
                "schema_version": 1,
                "decision": "ok",
                "resumed": False,
                "reason": "terminal_run_already_set",
                "workflow_run": run,
            }
        if run_state == "waiting_human":
            if not requeue:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "resumed": False,
                    "reason": "waiting_human",
                    "workflow_run": run,
                }
            run_lock.assert_p0_concurrency(state_root, target_run_id=run_id)
            transition = transition_run(
                state_root,
                run_id,
                to_state="step_queued",
                reason_class="human_resumed",
                transition="resume_run",
                principal=principal,
                run=run,
            )
            return {
                "schema_version": 1,
                "decision": "ok",
                "resumed": True,
                "reason": "human_resumed",
                "next_action": "run_step",
                "transition": transition,
                "workflow_run": run,
            }
        if run_state == "waiting_provider":
            step_id = str(run.get("current_step") or "")
            order_path = work_order_path(state_root, run_id, step_id)
            work_order = _load_json(order_path) if order_path.exists() else {}
            if not _runner_claim_expired(work_order):
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "resumed": False,
                    "reason": "provider_in_flight",
                    "workflow_run": run,
                }
            run_lock.assert_p0_concurrency(state_root, target_run_id=run_id)
            if order_path.exists():
                _reset_runner_claim(work_order)
                run_store.atomic_write_json(order_path, work_order)
            transition = transition_run(
                state_root,
                run_id,
                to_state="step_queued",
                reason_class="provider_lease_expired",
                transition="resume_run",
                principal=principal,
                artifact_refs=[str(order_path)] if order_path.exists() else [],
                run=run,
            )
            return {
                "schema_version": 1,
                "decision": "ok",
                "resumed": True,
                "reason": "provider_lease_expired",
                "next_action": "run_step",
                "transition": transition,
                "workflow_run": run,
            }

    raise LifecycleError("illegal_transition", [f"resume is not defined for run state: {run_state}"])


def abort_run(
    state_root: Path,
    run_id: str,
    *,
    reason: str,
    principal: dict[str, Any],
) -> dict[str, Any]:
    assert_execution_principal(principal)
    with run_lock.hold_global_lock(
        state_root,
        operation="abort_run",
        run_id=run_id,
        principal=principal,
    ):
        run = run_store.load_run(state_root, run_id)
        if str(run.get("run_state") or "") in TERMINAL_RUN_STATES:
            return {
                "schema_version": 1,
                "decision": "ok",
                "aborted": False,
                "reason": "terminal_run_already_set",
                "workflow_run": run,
            }
        terminal_reason = reason or "operator_abort"
        transition = transition_run(
            state_root,
            run_id,
            to_state="aborted",
            reason_class="operator_abort",
            transition="abort_run",
            principal=principal,
            terminal_status="aborted",
            terminal_reason=terminal_reason,
            run=run,
        )
        return {
            "schema_version": 1,
            "decision": "ok",
            "aborted": True,
            "reason": terminal_reason,
            "transition": transition,
            "workflow_run": run,
        }
