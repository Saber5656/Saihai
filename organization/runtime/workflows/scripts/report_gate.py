#!/usr/bin/env python3
"""Typed report gate for workflow-run validation and transition artifacts."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import run_lifecycle
import run_lock
import run_store
import task_state_bridge

BRIDGE_PRINCIPAL_TYPE = "main_agent_bridge"
EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
}
RAW_TRANSCRIPT_KEYS = {"raw_transcript", "transcript_content", "stdout", "pane_output"}


class ReportGateError(RuntimeError):
    """A stable report-gate error surfaced through the frontdoor wrapper."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
        "transitions": state_root / "transitions",
        "audit": state_root / "audit",
    }


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ReportGateError(f"missing file: {path}") from exc
    if not isinstance(data, dict):
        raise ReportGateError(f"expected object json: {path}")
    return data


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["work_orders"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}.json"
    )


def report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["reports"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-external-review-report.json"
    )


def provider_evidence_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-provider-evidence.json"
    )


def provider_transcript_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-claude-transcript.json"
    )


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def append_audit_event(
    *,
    state_root: Path,
    event_type: str,
    principal: dict[str, Any],
    subject: dict[str, Any],
    outcome: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "audit_event_version": "1",
        "event_id": "evt-"
        + stable_digest(
            {
                "event_type": event_type,
                "principal": run_lifecycle.redacted_principal(principal),
                "subject": subject,
                "created_at": time.time_ns(),
            }
        )[:20],
        "created_at": now_iso(),
        "event_type": event_type,
        "principal": run_lifecycle.redacted_principal(principal),
        "subject": subject,
        "outcome": outcome,
        "details": details or {},
    }
    path = state_paths(state_root)["audit"] / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def record_run_link_status(state_root: Path, run: dict[str, Any]) -> str:
    try:
        path = task_state_bridge.record_run_link(state_root, run)
    except Exception as exc:  # defensive isolation: view refresh must not fail transitions
        return f"error:{type(exc).__name__}:{exc}"
    if path is None:
        return "skipped:no_session"
    return f"linked:{path}"


def execution_principal_blocked_reason(principal: dict[str, Any]) -> str:
    return (
        "bridge principal cannot perform execution transition"
        if principal.get("principal_type") == BRIDGE_PRINCIPAL_TYPE
        else "unsupported execution principal"
    )


def precheck_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> None:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type in EXECUTION_PRINCIPAL_TYPES:
        return
    blocked_reason = execution_principal_blocked_reason(principal)
    append_audit_event(
        state_root=state_root,
        event_type=transition,
        principal=principal,
        subject=subject,
        outcome="blocked",
        details={"reason": blocked_reason, "principal_type": principal_type},
    )
    raise ReportGateError(f"{blocked_reason}: {principal_type}")


def _raw_content_errors(report: Any) -> list[str]:
    errors: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_path = f"{path}.{key}" if path else str(key)
                if key in RAW_TRANSCRIPT_KEYS:
                    errors.append(f"raw_transcript_embedded:{key_path}")
                walk(item, key_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                item_path = f"{path}[{index}]" if path else f"[{index}]"
                walk(item, item_path)

    walk(report)
    return list(dict.fromkeys(errors))


def _scope_violation_errors(report: Any, *, run: dict[str, Any], state_root: Path) -> list[str]:
    errors = _raw_content_errors(report)
    if not isinstance(report, dict):
        return errors
    evidence = report.get("provider_evidence")
    if isinstance(evidence, dict):
        for field in ("evidence_path", "transcript_path"):
            raw = evidence.get(field)
            if isinstance(raw, str) and raw:
                path = Path(raw).expanduser()
                if not path_is_within(path, state_paths(state_root)["provider_evidence"]):
                    errors.append("evidence_path_escape")
                    break
    authority = report.get("authority")
    if isinstance(authority, dict) and authority.get("raw_transcript_shared") is True:
        errors.append("raw_transcript_shared_true")
    for field in ("run_id", "request_id"):
        value = report.get(field)
        if value is not None and str(value) and str(value) != str(run.get(field)):
            errors.append("report_identity_mismatch")
    return list(dict.fromkeys(errors))


def classify_report_outcome(
    report: Any,
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> tuple[str, list[str]]:
    scope_errors = _scope_violation_errors(report, run=run, state_root=state_root)
    if scope_errors:
        return "scope_violation", scope_errors
    if not isinstance(report, dict):
        return "report_invalid", ["report must be object"]
    errors = validate_external_review_report(report, run=run, work_order=work_order, state_root=state_root)
    if errors:
        return "report_invalid", errors
    if report.get("result") == "blocked":
        return "provider_reported_blocked", []
    if report.get("result") == "invalid":
        return "report_invalid", ["result invalid"]
    return "report_valid", []


def validate_external_review_report(
    report: dict[str, Any],
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    errors: list[str] = []
    required = {
        "report_version",
        "report_id",
        "request_id",
        "run_id",
        "workflow_id",
        "step_id",
        "result",
        "summary",
        "provider_evidence",
        "findings",
        "authority",
    }
    allowed = required | {"recommendations"}
    missing = sorted(required - set(report))
    if missing:
        errors.append("missing_required_fields:" + ",".join(missing))
    extra = sorted(set(report) - allowed)
    if extra:
        errors.append("unexpected_fields:" + ",".join(extra))
    if report.get("report_version") != "1":
        errors.append("report_version must be '1'")
    for field in ("request_id", "run_id", "workflow_id", "step_id"):
        expected = str(run.get(field) if field != "step_id" else work_order.get("step_id"))
        if str(report.get(field)) != expected:
            errors.append(f"{field} mismatch: expected {expected!r}")
    if report.get("workflow_id") != "single_step_external_review":
        errors.append("workflow_id must be single_step_external_review")
    if report.get("step_id") != "review":
        errors.append("step_id must be review")
    if report.get("result") not in {"pass", "findings", "blocked", "invalid"}:
        errors.append("result unsupported")
    if not isinstance(report.get("summary"), str) or not report.get("summary"):
        errors.append("summary must be non-empty string")

    errors.extend(validate_provider_evidence(report.get("provider_evidence"), run, work_order, state_root))
    errors.extend(validate_findings(report.get("findings"), report.get("result")))
    errors.extend(validate_authority(report.get("authority")))
    return errors


def validate_provider_evidence(
    value: Any,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["provider_evidence must be object"]
    required = {
        "provider",
        "effective_model",
        "request_id",
        "provider_session_id",
        "transcript_path",
        "evidence_path",
    }
    missing = sorted(required - set(value))
    if missing:
        errors.append("provider_evidence missing:" + ",".join(missing))
    extra = sorted(set(value) - required)
    if extra:
        errors.append("provider_evidence unexpected:" + ",".join(extra))
    if str(value.get("request_id")) != str(run.get("request_id")):
        errors.append("provider_evidence.request_id mismatch")
    for field in ("provider", "effective_model", "provider_session_id", "transcript_path", "evidence_path"):
        if not isinstance(value.get(field), str) or not value.get(field):
            errors.append(f"provider_evidence.{field} must be non-empty string")
    expected_paths = {
        "transcript_path": provider_transcript_path(
            state_root,
            str(run.get("run_id") or ""),
            str(work_order.get("step_id") or ""),
        ),
        "evidence_path": provider_evidence_path(
            state_root,
            str(run.get("run_id") or ""),
            str(work_order.get("step_id") or ""),
        ),
    }
    for field in ("transcript_path", "evidence_path"):
        path = Path(str(value.get(field, ""))).expanduser()
        if value.get(field) and not path.exists():
            errors.append(f"provider_evidence.{field} does not exist: {path}")
        if value.get(field) and not path_is_within(path, state_paths(state_root)["provider_evidence"]):
            errors.append(f"provider_evidence.{field} must stay under provider evidence state directory")
        if value.get(field) and path.resolve() != expected_paths[field].resolve():
            errors.append(f"provider_evidence.{field} must match current run evidence path")
    return errors


def validate_findings(value: Any, result: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        return ["findings must be array"]
    if result == "findings" and not value:
        errors.append("findings result requires at least one finding")
    required = {"finding_id", "severity", "status", "summary", "evidence_refs"}
    allowed = required
    for index, finding in enumerate(value):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] must be object")
            continue
        missing = sorted(required - set(finding))
        if missing:
            errors.append(f"findings[{index}] missing:" + ",".join(missing))
        extra = sorted(set(finding) - allowed)
        if extra:
            errors.append(f"findings[{index}] unexpected:" + ",".join(extra))
        if finding.get("severity") not in {"critical", "high", "medium", "low", "info"}:
            errors.append(f"findings[{index}].severity unsupported")
        if finding.get("status") not in {"open", "closed", "waived", "informational"}:
            errors.append(f"findings[{index}].status unsupported")
        if not isinstance(finding.get("finding_id"), str) or not finding.get("finding_id"):
            errors.append(f"findings[{index}].finding_id must be non-empty string")
        if not isinstance(finding.get("summary"), str) or not finding.get("summary"):
            errors.append(f"findings[{index}].summary must be non-empty string")
        if not isinstance(finding.get("evidence_refs"), list):
            errors.append(f"findings[{index}].evidence_refs must be array")
        elif any(not isinstance(item, str) or not item for item in finding["evidence_refs"]):
            errors.append(f"findings[{index}].evidence_refs entries must be non-empty strings")
    return errors


def validate_authority(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["authority must be object"]
    errors: list[str] = []
    expected = {
        "canonical_result": "typed_report_file",
        "stdout_is_signal_only": True,
        "raw_transcript_shared": False,
    }
    missing = sorted(set(expected) - set(value))
    if missing:
        errors.append("authority missing:" + ",".join(missing))
    extra = sorted(set(value) - set(expected))
    if extra:
        errors.append("authority unexpected:" + ",".join(extra))
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            errors.append(f"authority.{key} must be {expected_value!r}")
    return errors


def next_numbered_artifact(path: Path, suffix: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    existing = sorted(path.glob(f"*-{suffix}.json"))
    return path / f"{len(existing) + 1:04d}-{suffix}.json"


def write_transition_artifact(
    *,
    state_root: Path,
    run_id: str,
    payload: dict[str, Any],
) -> Path:
    path = next_numbered_artifact(state_paths(state_root)["transitions"] / run_id, "report-gate")
    run_store.atomic_write_json(path, payload)
    return path


def write_rejection_artifact(
    *,
    state_root: Path,
    run_id: str,
    step_id: str,
    payload: dict[str, Any],
) -> Path:
    directory = state_paths(state_root)["reports"] / run_id
    existing = sorted(directory.glob(f"{step_id}-rejection-*.json"))
    path = directory / f"{step_id}-rejection-{len(existing) + 1}.json"
    run_store.atomic_write_json(path, payload)
    return path


def gate_report(
    state_root: Path,
    run_id: str,
    *,
    report_path_arg: str = "",
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_store.validate_artifact_id(run_id, "run_id")
    actor = principal or {
        "principal_type": "harness_runner",
        "principal_id": "local-harness",
        "authn_method": "local_cli",
    }
    run_file = run_store.run_path(state_root, run_id)
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="validate_report",
        subject=subject,
    )
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="validate_report",
            run_id=run_id,
            principal=actor,
        ):
            run = run_store.load_run(state_root, run_id)
            run_state = str(run.get("run_state") or "")
            subject = {"run_id": run_id, "request_id": str(run.get("request_id") or "")}
            signature = run_lifecycle.sign_transition(
                state_root=state_root,
                principal=actor,
                transition="validate_report",
                subject=subject,
            )
            if run_state in run_lifecycle.TERMINAL_RUN_STATES:
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="validate_report",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={
                        "reason": "terminal_run_already_set",
                        "run_state": run.get("run_state"),
                        "run_link": link_status,
                    },
                )
                return {
                    "schema_version": 1,
                    "decision": "ok",
                    "validated": False,
                    "reason": "terminal_run_already_set",
                    "run_path": str(run_file),
                    "workflow_run": run,
                    "outcome": "terminal_replay",
                    "transition_artifact_path": None,
                    "rejection_artifact_path": None,
                }
            step_id = str(run["current_step"])
            work_order_file = work_order_path(state_root, run_id, step_id)
            work_order = read_json(work_order_file)
            canonical_report_path = Path(str(work_order["report_path"])).expanduser()
            path = Path(report_path_arg).expanduser() if report_path_arg else canonical_report_path
            if not path_is_within(path, state_paths(state_root)["reports"]):
                raise ReportGateError("report path must stay under orchestrator state reports directory")
            if path.resolve() != canonical_report_path.resolve():
                raise ReportGateError("report path must match canonical work order report path")

            report = read_json(path)

            if run_state == "step_queued":
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="waiting_provider",
                    reason_class="manual_provider_execution_assumed",
                    transition="validate_report",
                    principal=actor,
                    artifact_refs=[str(work_order_file), str(path)],
                    run=run,
                )
                run_state = "waiting_provider"
            if run_state == "waiting_provider":
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="validating",
                    reason_class="report_received",
                    transition="validate_report",
                    principal=actor,
                    artifact_refs=[str(path)],
                    run=run,
                )
                run_state = "validating"

            outcome, errors = classify_report_outcome(report, run=run, work_order=work_order, state_root=state_root)
            if outcome == "report_valid":
                to_state = "complete"
                report_status = "complete"
                terminal_status = "complete"
                terminal_reason = "report_valid"
                reason_class = "report_valid"
                decision = "ok"
                history_status = "complete"
                audit_outcome = "ok"
            elif outcome == "report_invalid":
                to_state = "failed"
                report_status = "blocked"
                terminal_status = "blocked"
                terminal_reason = "invalid_report"
                reason_class = "invalid_report"
                decision = "blocked"
                history_status = "blocked"
                audit_outcome = "blocked"
            elif outcome == "scope_violation":
                to_state = "waiting_human"
                report_status = "waiting_human"
                terminal_status = None
                terminal_reason = None
                reason_class = "scope_violation"
                decision = "blocked"
                history_status = "blocked"
                audit_outcome = "blocked"
            else:
                to_state = "waiting_human"
                report_status = "waiting_human"
                terminal_status = None
                terminal_reason = None
                reason_class = "provider_reported_blocked"
                decision = "ok"
                history_status = "waiting_human"
                audit_outcome = "ok"

            if run_state == "waiting_human" and to_state == "waiting_human":
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="validate_report",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={
                        "reason": "waiting_human_replay",
                        "report_status": report_status,
                        "outcome": outcome,
                        "errors": errors,
                        "run_link": link_status,
                    },
                )
                response = {
                    "schema_version": 1,
                    "decision": decision,
                    "validated": False,
                    "report_status": report_status,
                    "reason": reason_class,
                    "errors": errors,
                    "outcome": outcome,
                    "transition_artifact_path": None,
                    "rejection_artifact_path": None,
                    "run_path": str(run_file),
                    "workflow_run": run,
                }
                if decision == "ok":
                    response["report"] = report
                if decision == "ok" and outcome == "provider_reported_blocked":
                    response.pop("errors")
                return response

            history = {
                "step_id": step_id,
                "status": history_status,
                "checked_at": now_iso(),
                "report_path": str(path),
                "outcome": outcome,
                "result": report.get("result"),
                "principal": run_lifecycle.redacted_principal(actor),
                "signature": signature,
            }
            if errors:
                history["errors"] = errors
            run["step_history"].append(history)
            run.setdefault("transition_provenance", []).append(
                {
                    "transition": "validate_report",
                    "principal": run_lifecycle.redacted_principal(actor),
                    "signature": signature,
                    "result": report_status,
                    "outcome": outcome,
                }
            )
            transition = run_lifecycle.transition_run(
                state_root,
                run_id,
                to_state=to_state,
                reason_class=reason_class,
                transition="validate_report",
                principal=actor,
                artifact_refs=[str(path)],
                terminal_status=terminal_status,
                terminal_reason=terminal_reason,
                run=run,
            )
            report_digest = file_sha256(path)
            evidence_path = (
                report.get("provider_evidence", {}).get("evidence_path")
                if isinstance(report.get("provider_evidence"), dict)
                else None
            )
            transition_payload = {
                "transition_artifact_version": "1",
                "run_id": run_id,
                "step_id": step_id,
                "iteration": int(run.get("iteration") or 1),
                "gate": "report_gate",
                "outcome": outcome,
                "on": outcome,
                "from_state": transition["from_state"],
                "to_state": transition["to_state"],
                "reason_class": reason_class,
                "errors": errors,
                "report_path": str(path),
                "report_sha256": report_digest,
                "evidence_path": evidence_path,
                "occurred_at": now_iso(),
                "principal": run_lifecycle.redacted_principal(actor),
            }
            transition_payload["signature"] = run_lifecycle.sign_transition(
                state_root=state_root,
                principal=actor,
                transition="report_gate_artifact",
                subject=transition_payload,
            )
            transition_artifact_path = write_transition_artifact(
                state_root=state_root,
                run_id=run_id,
                payload=transition_payload,
            )
            rejection_artifact_path = None
            if outcome in {"report_invalid", "scope_violation"}:
                rejection_payload = {
                    "rejection_version": "1",
                    "run_id": run_id,
                    "step_id": step_id,
                    "outcome": outcome,
                    "errors": errors,
                    "report_path": str(path),
                    "report_sha256": report_digest,
                    "occurred_at": now_iso(),
                    "principal": run_lifecycle.redacted_principal(actor),
                }
                rejection_artifact_path = write_rejection_artifact(
                    state_root=state_root,
                    run_id=run_id,
                    step_id=step_id,
                    payload=rejection_payload,
                )
            run_file = run_store.run_path(state_root, run_id)
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="validate_report",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise

    link_status = record_run_link_status(state_root, run)
    append_audit_event(
        state_root=state_root,
        event_type="validate_report",
        principal=actor,
        subject=subject,
        outcome=audit_outcome,
        details={
            "report_status": report_status,
            "result": report.get("result"),
            "outcome": outcome,
            "errors": errors,
            "transition_artifact_path": str(transition_artifact_path),
            "rejection_artifact_path": str(rejection_artifact_path) if rejection_artifact_path else None,
            "run_link": link_status,
        },
    )
    response = {
        "schema_version": 1,
        "decision": decision,
        "validated": True,
        "report_status": report_status,
        "reason": reason_class,
        "errors": errors,
        "outcome": outcome,
        "transition_artifact_path": str(transition_artifact_path),
        "rejection_artifact_path": str(rejection_artifact_path) if rejection_artifact_path else None,
        "run_path": str(run_file),
        "workflow_run": run,
    }
    if decision == "ok":
        response["report"] = report
    if decision == "ok" and outcome == "report_valid":
        response.pop("errors")
    return response
