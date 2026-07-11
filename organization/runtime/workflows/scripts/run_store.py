#!/usr/bin/env python3
"""Durable, atomic, schema-validated store for workflow-run records."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RESERVED_ARTIFACT_SUFFIX_RE = re.compile(r"(?:\.error|\.corrupt-\d+)$")

RUN_STATES = {
    "created",
    "step_queued",
    "waiting_provider",
    "validating",
    "remediating",
    "waiting_human",
    "complete",
    "failed",
    "aborted",
}
GOAL_STATES = {"approved", "active", "blocked", "complete", "aborted"}
TERMINAL_RUN_STATES = {"complete", "failed", "aborted"}
APPROVED_ACTIVATION_SOURCES = {"orchestrator-start", "human_ui", "manual_cli"}

STORE_REASON_CLASSES = {
    "run_not_found",
    "corrupt_json",
    "schema_invalid",
    "state_conflict",
    "io_error",
}


class RunStoreError(RuntimeError):
    """Typed store failure. reason_class is one of STORE_REASON_CLASSES."""

    def __init__(self, reason_class: str, errors: list[str] | None = None) -> None:
        super().__init__(reason_class)
        self.reason_class = reason_class
        self.errors = errors or []


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def validate_artifact_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise RunStoreError(
            "schema_invalid",
            [f"{label} must match {SAFE_ID_RE.pattern} and cannot contain path separators"],
        )
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value.split("."):
        raise RunStoreError("schema_invalid", [f"{label} cannot contain path traversal segments"])
    if RESERVED_ARTIFACT_SUFFIX_RE.search(value):
        raise RunStoreError("schema_invalid", [f"{label} cannot use reserved run-store artifact suffixes"])
    return value


def atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (TypeError, ValueError) as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RunStoreError("schema_invalid", [f"payload must be JSON serializable: {exc}"]) from exc
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RunStoreError("io_error", [str(exc)]) from exc


def _is_int_at_least(value: Any, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def validate_run_record(run: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(run, dict):
        return ["run must be a json object"]

    required = [
        "run_version",
        "run_id",
        "task_id",
        "request_id",
        "workflow_id",
        "goal_state",
        "run_state",
        "current_step",
        "iteration",
        "max_steps",
        "step_history",
        "activation",
        "terminal",
        "requester",
        "scheduling",
        "context_sharing",
    ]
    missing = [field for field in required if field not in run]
    if missing:
        return [f"missing_required_field:{field}" for field in missing]

    if run.get("run_version") != "1":
        errors.append("run_version must be '1'")
    for field in ("run_id", "task_id", "request_id"):
        try:
            validate_artifact_id(run.get(field), field)
        except RunStoreError as exc:
            errors.extend(exc.errors)
    if not _non_empty_string(run.get("workflow_id")):
        errors.append("workflow_id must be a non-empty string")
    if not _non_empty_string(run.get("current_step")):
        errors.append("current_step must be a non-empty string")
    if run.get("run_state") not in RUN_STATES:
        errors.append("run_state must be a known workflow run state")
    if run.get("goal_state") not in GOAL_STATES:
        errors.append("goal_state must be a known workflow goal state")
    if not _is_int_at_least(run.get("iteration"), 1):
        errors.append("iteration must be an integer >= 1")
    if not _is_int_at_least(run.get("max_steps"), 1):
        errors.append("max_steps must be an integer >= 1")
    if not isinstance(run.get("step_history"), list):
        errors.append("step_history must be a list")
    if "transitions" in run and not isinstance(run.get("transitions"), list):
        errors.append("transitions must be a list")
    completion_verification = run.get("completion_verification")
    if completion_verification is not None:
        if not isinstance(completion_verification, dict):
            errors.append("completion_verification must be a json object")
        else:
            required_completion_fields = {
                "verified_at",
                "decision",
                "report_sha256",
                "evidence_sha256",
                "verifier",
            }
            missing_completion_fields = sorted(required_completion_fields - set(completion_verification))
            errors.extend(
                f"completion_verification.{field} is required"
                for field in missing_completion_fields
            )
            if completion_verification.get("decision") != "complete":
                errors.append("completion_verification.decision must be complete")
            for field in ("report_sha256", "evidence_sha256"):
                value = completion_verification.get(field)
                if not isinstance(value, str) or not value.startswith("sha256:"):
                    errors.append(f"completion_verification.{field} must start with sha256:")
            if not isinstance(completion_verification.get("verifier"), dict):
                errors.append("completion_verification.verifier must be a json object")
    if not isinstance(run.get("requester"), dict):
        errors.append("requester must be a json object")

    activation = run.get("activation")
    if not isinstance(activation, dict):
        errors.append("activation must be a json object")
    else:
        if activation.get("activation_status") != "approved":
            errors.append("activation.activation_status must be approved")
        if activation.get("activation_source") not in APPROVED_ACTIVATION_SOURCES:
            errors.append("activation.activation_source must be approved source")
        if activation.get("next_action") != "create_workflow_run":
            errors.append("activation.next_action must be create_workflow_run")
        workflow_selection = activation.get("workflow_selection")
        if not isinstance(workflow_selection, dict):
            errors.append("activation.workflow_selection must be a json object")
        else:
            if workflow_selection.get("status") != "selected":
                errors.append("activation.workflow_selection.status must be selected")
            if not _non_empty_string(workflow_selection.get("workflow_id")):
                errors.append("activation.workflow_selection.workflow_id must be non-empty")
            if not _non_empty_string(workflow_selection.get("initial_step")):
                errors.append("activation.workflow_selection.initial_step must be non-empty")
        context_scope = activation.get("context_scope")
        if not isinstance(context_scope, dict):
            errors.append("activation.context_scope must be a json object")
        else:
            refs = context_scope.get("refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(item, str) or not item for item in refs):
                errors.append("activation.context_scope.refs must be a non-empty list of strings")
            if context_scope.get("raw_transcript_sharing") != "forbidden":
                errors.append("activation.context_scope.raw_transcript_sharing must be forbidden")
        activation_scope = activation.get("activation_scope")
        if not isinstance(activation_scope, dict):
            errors.append("activation.activation_scope must be a json object")
        else:
            allowed_ops = activation_scope.get("allowed_ops")
            if not isinstance(allowed_ops, dict):
                errors.append("activation.activation_scope.allowed_ops must be a json object")
            else:
                for op in ("edit", "commit", "push", "network"):
                    if not isinstance(allowed_ops.get(op), bool):
                        errors.append(f"activation.activation_scope.allowed_ops.{op} must be boolean")
            if not _is_int_at_least(activation_scope.get("step_budget"), 1):
                errors.append("activation.activation_scope.step_budget must be an integer >= 1")

    scheduling = run.get("scheduling")
    if not isinstance(scheduling, dict):
        errors.append("scheduling must be a json object")
    else:
        fixed_values = {
            "scheduler_mode": "invocation-drain",
            "concurrency_group": "global",
            "state_persistence": "durable_state",
            "lock_policy": "global_advisory_lock",
            "concurrency": 1,
        }
        for field, expected in fixed_values.items():
            if scheduling.get(field) != expected:
                errors.append(f"scheduling.{field} must be {expected!r}")
        if scheduling.get("resume_policy") not in {"manual", "daemon_future"}:
            errors.append("scheduling.resume_policy must be manual or daemon_future")

    context_sharing = run.get("context_sharing")
    if not isinstance(context_sharing, dict):
        errors.append("context_sharing must be a json object")
    else:
        fixed_values = {
            "shared_run_state": "typed_durable_state",
            "step_local_snapshot": "immutable_step_attempt_snapshot",
            "provider_transcript": "confined_evidence_path_only",
        }
        for field, expected in fixed_values.items():
            if context_sharing.get(field) != expected:
                errors.append(f"context_sharing.{field} must be {expected!r}")

    terminal = run.get("terminal")
    if not isinstance(terminal, dict):
        errors.append("terminal must be a json object")
    else:
        for field in ("status", "reason"):
            if field not in terminal:
                errors.append(f"terminal.{field} is required")
        if run.get("run_state") in TERMINAL_RUN_STATES and not _non_empty_string(terminal.get("status")):
            errors.append("terminal_status_required_for_terminal_state")

    return errors


def run_path(state_root: Path, run_id: str) -> Path:
    return state_root / "runs" / f"{validate_artifact_id(run_id, 'run_id')}.json"


def error_artifact_path(state_root: Path, run_id: str) -> Path:
    return state_root / "runs" / f"{validate_artifact_id(run_id, 'run_id')}.error.json"


def write_error_artifact(
    state_root: Path,
    run_id: str,
    *,
    reason_class: str,
    errors: list[str],
    operation: str,
) -> Path:
    if reason_class not in STORE_REASON_CLASSES:
        raise ValueError(f"unsupported store reason_class: {reason_class}")
    payload = {
        "error_version": "1",
        "run_id": run_id,
        "operation": operation,
        "reason_class": reason_class,
        "errors": errors,
        "occurred_at": now_iso(),
    }
    path = error_artifact_path(state_root, run_id)
    atomic_write_json(path, payload)
    return path


def quarantine_corrupt_run(state_root: Path, run_id: str) -> Path:
    source = run_path(state_root, run_id)
    index = 1
    while True:
        target = state_root / "runs" / f"{validate_artifact_id(run_id, 'run_id')}.corrupt-{index}.json"
        if not target.exists():
            break
        index += 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    return target


def load_run(state_root: Path, run_id: str) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    path = run_path(state_root, run_id)
    if not path.exists():
        raise RunStoreError("run_not_found")
    try:
        with path.open(encoding="utf-8") as handle:
            run = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors = [str(exc)]
        quarantine_corrupt_run(state_root, run_id)
        write_error_artifact(state_root, run_id, reason_class="corrupt_json", errors=errors, operation="load")
        raise RunStoreError("corrupt_json", errors) from exc
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc

    errors = validate_run_record(run)
    if isinstance(run, dict) and run.get("run_id") != run_id:
        errors.append(f"run_id must match requested run_id {run_id!r}")
    if errors:
        write_error_artifact(state_root, run_id, reason_class="schema_invalid", errors=errors, operation="load")
        raise RunStoreError("schema_invalid", errors)
    return run


def _valid_error_artifact_run_id(run: Any) -> str | None:
    if not isinstance(run, dict):
        return None
    run_id = run.get("run_id")
    try:
        return validate_artifact_id(run_id, "run_id")
    except RunStoreError:
        return None


def store_run(
    state_root: Path,
    run: dict[str, Any],
    *,
    expected_current_state: str | None = None,
) -> Path:
    errors = validate_run_record(run)
    run_id = _valid_error_artifact_run_id(run)
    if errors:
        if run_id is not None:
            write_error_artifact(
                state_root,
                run_id,
                reason_class="schema_invalid",
                errors=errors,
                operation="store",
            )
        raise RunStoreError("schema_invalid", errors)
    assert run_id is not None

    path = run_path(state_root, run_id)
    if expected_current_state is not None:
        if not path.exists():
            conflict_errors = [f"expected existing run_state {expected_current_state!r}, found missing run"]
            write_error_artifact(
                state_root,
                run_id,
                reason_class="state_conflict",
                errors=conflict_errors,
                operation="store",
            )
            raise RunStoreError("state_conflict", conflict_errors)
        on_disk = load_run(state_root, run_id)
        if on_disk.get("run_state") != expected_current_state:
            conflict_errors = [
                f"expected run_state {expected_current_state!r}, found {on_disk.get('run_state')!r}"
            ]
            write_error_artifact(
                state_root,
                run_id,
                reason_class="state_conflict",
                errors=conflict_errors,
                operation="store",
            )
            raise RunStoreError("state_conflict", conflict_errors)

    atomic_write_json(path, run)
    return path
