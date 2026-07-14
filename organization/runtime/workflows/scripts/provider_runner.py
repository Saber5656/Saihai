#!/usr/bin/env python3
"""Headless provider adapter runner with normalized evidence artifacts."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import report_gate
import run_lifecycle
import run_lock
import run_store
import workflow_selector

BRIDGE_PRINCIPAL_TYPE = "main_agent_bridge"
EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
}

DEFAULT_ADAPTER_ID = "claude_headless_p0"
PROVIDER_TARGETS = {
    "claude_headless",
    "codex_cli_openai",
    "hermes_agent",
    "cursor_cli",
    "grok_build_cli",
}


class ProviderRunnerError(RuntimeError):
    """Stable provider runner failure."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
        "adapter_requests": state_root / "adapter-requests",
        "audit": state_root / "audit",
    }


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


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderRunnerError(f"unreadable json: {path}") from exc
    if not isinstance(payload, dict):
        raise ProviderRunnerError(f"expected object json: {path}")
    return payload


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
        "event_id": "evt-" + stable_digest(
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


def precheck_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    subject: dict[str, Any],
) -> None:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type in EXECUTION_PRINCIPAL_TYPES:
        return
    reason = (
        "bridge principal cannot perform execution transition"
        if principal_type == BRIDGE_PRINCIPAL_TYPE
        else "unsupported execution principal"
    )
    append_audit_event(
        state_root=state_root,
        event_type="run_provider",
        principal=principal,
        subject=subject,
        outcome="blocked",
        details={"reason": reason, "principal_type": principal_type},
    )
    raise ProviderRunnerError(f"{reason}: {principal_type}")


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["work_orders"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}.json"
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
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-provider-transcript.json"
    )


def adapter_request_path(state_root: Path, run_id: str, step_id: str, adapter_id: str) -> Path:
    return (
        state_paths(state_root)["adapter_requests"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-{run_store.validate_artifact_id(adapter_id, 'adapter_id')}.json"
    )


def load_provider_adapters(registry: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = registry or workflow_selector.load_registry()
    adapters = registry.get("provider_adapters")
    if not isinstance(adapters, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in adapters:
        if not isinstance(item, dict):
            continue
        adapter_id = str(item.get("provider_adapter_id") or "")
        if adapter_id:
            result[adapter_id] = item
    return result


def validate_adapter_descriptor(adapter: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "adapter_contract_version",
        "provider_adapter_id",
        "provider_target",
        "transport",
        "sync_mode",
        "context_freshness",
        "concurrency_unit",
        "permission_enforcement",
        "supports_structured_output",
        "requires_marker",
        "reset_strategy",
        "report_authority",
    }
    missing = sorted(required - set(adapter))
    if missing:
        errors.append("adapter missing:" + ",".join(missing))
    if adapter.get("adapter_contract_version") != "1":
        errors.append("adapter_contract_version must be '1'")
    if adapter.get("provider_target") not in PROVIDER_TARGETS:
        errors.append("provider_target unsupported")
    if adapter.get("transport") != "headless_cli":
        errors.append("only headless_cli transport is runnable in this runner")
    if adapter.get("permission_enforcement") != "harness":
        errors.append("permission_enforcement must be harness")
    if adapter.get("report_authority") != "typed_report_and_evidence_file":
        errors.append("report_authority must be typed_report_and_evidence_file")
    if adapter.get("supports_structured_output") is not True:
        errors.append("supports_structured_output must be true")
    return errors


def validate_work_order_for_runner(
    work_order: dict[str, Any],
    *,
    state_root: Path | None = None,
    run: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if work_order.get("workflow_id") != "single_step_external_review":
        errors.append("only single_step_external_review is supported")
    if work_order.get("step_id") != "review":
        errors.append("only review step is supported")
    if work_order.get("permission_mode") != "readonly":
        errors.append("permission_mode must be readonly")
    if work_order.get("external_provider_allowed") is not True:
        errors.append("external_provider_allowed must be true")
    allowed_ops = ((work_order.get("activation_scope") or {}).get("allowed_ops") or {})
    for op in ("edit", "commit", "push", "network"):
        if allowed_ops.get(op) is not False:
            errors.append(f"activation_scope.allowed_ops.{op} must be false")
    if (work_order.get("context_scope") or {}).get("raw_transcript_sharing") != "forbidden":
        errors.append("raw transcript sharing must be forbidden")
    authority = work_order.get("work_order_authority")
    if not isinstance(authority, dict):
        errors.append("work_order_authority must be object")
    elif (authority.get("issuer_principal") or {}).get("principal_type") == BRIDGE_PRINCIPAL_TYPE:
        errors.append("bridge principal cannot issue work orders")
    if state_root is not None and run is not None:
        raw_report_path = work_order.get("report_path")
        if not isinstance(raw_report_path, str) or not raw_report_path:
            errors.append("report_path must be non-empty string")
        else:
            report_path = Path(raw_report_path).expanduser()
            canonical_report_path = report_gate.report_path(
                state_root,
                str(run.get("run_id") or ""),
                str(work_order.get("step_id") or ""),
            )
            if not report_gate.path_is_within(report_path, state_paths(state_root)["reports"]):
                errors.append("report_path must stay under reports")
            if report_path.resolve() != canonical_report_path.resolve():
                errors.append("report_path must match current run report path")
    return errors


def runner_claim_is_live(work_order: dict[str, Any]) -> bool:
    authority = work_order.get("work_order_authority")
    claim = authority.get("runner_claim") if isinstance(authority, dict) else None
    return isinstance(claim, dict) and claim.get("claim_state") == "claimed" and not run_lifecycle._runner_claim_expired(
        work_order
    )


def adapter_request(
    *,
    state_root: Path,
    run: dict[str, Any],
    work_order: dict[str, Any],
    adapter: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(run["run_id"])
    step_id = str(work_order["step_id"])
    evidence_path = provider_evidence_path(state_root, run_id, step_id)
    transcript_path = provider_transcript_path(state_root, run_id, step_id)
    return {
        "adapter_request_version": "1",
        "adapter": adapter,
        "run_id": run_id,
        "request_id": run["request_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "work_order_path": str(work_order_path(state_root, run_id, step_id)),
        "report_path": work_order["report_path"],
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "instruction": work_order["instruction"],
        "context_refs": work_order.get("context_refs", []),
        "authority": {
            "provider_may_write": ["typed_report_file", "normalized_provider_evidence_file"],
            "provider_must_not": [
                "select_workflow",
                "approve_activation",
                "mutate_run_state",
                "edit_repo",
                "commit",
                "push",
                "publish",
                "copy_raw_transcript_to_run_state",
            ],
            "issued_by_principal": run_lifecycle.redacted_principal(principal),
            "work_order_signature": (work_order.get("work_order_authority") or {}).get("signature"),
        },
    }


def fake_provider_report(
    *,
    request: dict[str, Any],
    adapter: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    result = "pass"
    findings: list[dict[str, Any]] = []
    if mode == "findings":
        result = "findings"
        findings = [
            {
                "finding_id": "F-1",
                "severity": "low",
                "status": "open",
                "summary": "Fake provider finding.",
                "evidence_refs": ["organization/runtime/workflows/README.md"],
            }
        ]
    elif mode == "blocked":
        result = "blocked"
    provider = str(adapter.get("provider_target") or adapter.get("provider_adapter_id"))
    return {
        "report_version": "1",
        "report_id": f"report-{request['run_id']}",
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "workflow_id": request["workflow_id"],
        "step_id": request["step_id"],
        "result": result,
        "summary": "Fake provider completed the bounded work order.",
        "provider_evidence": {
            "provider": provider,
            "effective_model": str(adapter.get("default_model") or "fake-model"),
            "request_id": request["request_id"],
            "provider_session_id": f"fake-session-{request['run_id']}",
            "transcript_path": request["transcript_path"],
            "evidence_path": request["evidence_path"],
        },
        "findings": findings,
        "authority": {
            "canonical_result": "typed_report_file",
            "stdout_is_signal_only": True,
            "raw_transcript_shared": False,
        },
    }


def execute_provider(
    *,
    request: dict[str, Any],
    adapter: dict[str, Any],
    timeout_seconds: int,
    fake_provider_mode: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    started = time.monotonic()
    if fake_provider_mode:
        if fake_provider_mode == "timeout":
            time.sleep(0.001)
            return "provider_timeout", None, {"timed_out": True, "duration_ms": int((time.monotonic() - started) * 1000)}
        if fake_provider_mode == "nonzero":
            return "provider_nonzero_exit", None, {"exit_code": 42, "duration_ms": int((time.monotonic() - started) * 1000)}
        if fake_provider_mode == "malformed":
            return "provider_malformed_output", None, {"stdout_sha256": "sha256:" + hashlib.sha256(b"not json").hexdigest()}
        if fake_provider_mode == "malformed_binary":
            return parse_provider_stdout(b"\xff")
        if fake_provider_mode == "unavailable":
            return "provider_unavailable", None, {"reason": "fake_provider_unavailable"}
        report = fake_provider_report(request=request, adapter=adapter, mode=fake_provider_mode)
        return "ok", report, {"duration_ms": int((time.monotonic() - started) * 1000)}

    command = adapter.get("command_argv")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
        return "provider_unavailable", None, {"reason": "command_argv_not_configured"}
    return "provider_unavailable", None, {"reason": "live_command_adapter_requires_sandbox"}


def parse_provider_stdout(stdout: bytes | str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    raw = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
    stdout_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "provider_malformed_output", None, {"stdout_sha256": stdout_sha256}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "provider_malformed_output", None, {"stdout_sha256": stdout_sha256}
    if not isinstance(payload, dict):
        return "provider_malformed_output", None, {"reason": "stdout_json_not_object"}
    return "ok", payload, {"stdout_sha256": stdout_sha256}


def normalized_evidence(
    *,
    request: dict[str, Any],
    adapter: dict[str, Any],
    report: dict[str, Any] | None,
    outcome: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    report_evidence = report.get("provider_evidence") if isinstance(report, dict) else {}
    if not isinstance(report_evidence, dict):
        report_evidence = {}
    evidence = {
        "evidence_version": "1",
        "provider_adapter_id": adapter["provider_adapter_id"],
        "provider_target": adapter["provider_target"],
        "provider": report_evidence.get("provider") or adapter["provider_target"],
        "effective_model": report_evidence.get("effective_model") or adapter.get("default_model") or "unknown",
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "workflow_id": request["workflow_id"],
        "step_id": request["step_id"],
        "provider_request_id": f"provider-{stable_digest(request)[:16]}",
        "provider_session_id": report_evidence.get("provider_session_id") or f"session-{request['run_id']}",
        "transcript_path": request["transcript_path"],
        "evidence_path": request["evidence_path"],
        "duration_ms": int(details.get("duration_ms") or 0),
        "usage": details.get("usage") if isinstance(details.get("usage"), dict) else {},
        "outcome": outcome,
        "reason_class": details.get("reason") or outcome,
        "transport": adapter["transport"],
        "bridge_pattern": adapter.get("bridge_pattern"),
        "surface_metadata": adapter.get("surface_metadata") if isinstance(adapter.get("surface_metadata"), dict) else {},
        "stdout_sha256": details.get("stdout_sha256"),
        "exit_code": details.get("exit_code"),
        "timed_out": bool(details.get("timed_out", False)),
        "raw_transcript_policy": "signal_only_not_shared",
    }
    return {key: value for key, value in evidence.items() if value is not None}


def write_signal_transcript(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_store.atomic_write_json(
        path,
        {
            "transcript_signal_version": "1",
            "written_at": now_iso(),
            "payload": payload,
            "raw_content_policy": "signal_only_not_shared",
        },
    )


def transition_failure(
    *,
    state_root: Path,
    run_id: str,
    run: dict[str, Any],
    reason_class: str,
    principal: dict[str, Any],
    artifact_refs: list[str],
) -> dict[str, Any]:
    current = str(run.get("run_state") or "")
    recoverable_reasons = {
        "provider_unavailable",
        "provider_timeout",
        "provider_nonzero_exit",
        "report_not_written",
    }
    if current == "step_queued":
        target = "waiting_human" if reason_class in recoverable_reasons else "failed"
    elif current == "waiting_provider":
        target = "waiting_human" if reason_class in recoverable_reasons else "failed"
    else:
        target = "failed"
    terminal_status = "blocked" if target == "failed" else None
    transition = run_lifecycle.transition_run(
        state_root,
        run_id,
        to_state=target,
        reason_class=reason_class,
        transition="run_provider",
        principal=principal,
        artifact_refs=artifact_refs,
        terminal_status=terminal_status,
        terminal_reason=reason_class if terminal_status else None,
        run=run,
    )
    return transition


def run_provider(
    *,
    state_root: Path,
    run_id: str,
    adapter_id: str = DEFAULT_ADAPTER_ID,
    timeout_seconds: int = 60,
    fake_provider_mode: str = "",
    principal: dict[str, Any],
) -> dict[str, Any]:
    run_store.validate_artifact_id(run_id, "run_id")
    subject = {"run_id": run_id, "adapter_id": adapter_id}
    precheck_execution_principal(state_root=state_root, principal=principal, subject=subject)
    adapters = load_provider_adapters()
    adapter = adapters.get(adapter_id)
    if adapter is None:
        raise ProviderRunnerError(f"provider adapter not found: {adapter_id}")
    adapter_errors = validate_adapter_descriptor(adapter)
    if adapter_errors:
        return {"schema_version": 1, "decision": "blocked", "reason": "adapter_descriptor_invalid", "errors": adapter_errors}

    with run_lock.hold_global_lock(
        state_root,
        operation="run_provider",
        run_id=run_id,
        principal=principal,
    ):
        run = run_store.load_run(state_root, run_id)
        run_state = str(run.get("run_state") or "")
        if run_state not in {"step_queued", "waiting_provider"}:
            append_audit_event(
                state_root=state_root,
                event_type="run_provider",
                principal=principal,
                subject=subject,
                outcome="blocked",
                details={"reason": "run_not_runnable", "run_state": run_state},
            )
            return {
                "schema_version": 1,
                "decision": "blocked",
                "reason": "run_not_runnable",
                "run_state": run_state,
                "workflow_run": run,
            }
        step_id = str(run.get("current_step") or "")
        order_path = work_order_path(state_root, run_id, step_id)
        work_order = read_json(order_path)
        work_order_errors = validate_work_order_for_runner(work_order, state_root=state_root, run=run)
        if work_order_errors:
            return {
                "schema_version": 1,
                "decision": "blocked",
                "reason": "work_order_not_provider_safe",
                "errors": work_order_errors,
            }
        if runner_claim_is_live(work_order):
            append_audit_event(
                state_root=state_root,
                event_type="run_provider",
                principal=principal,
                subject=subject,
                outcome="blocked",
                details={"reason": "provider_in_flight", "run_state": run_state},
            )
            return {
                "schema_version": 1,
                "decision": "blocked",
                "reason": "provider_in_flight",
                "run_state": run_state,
                "workflow_run": run,
            }
        if run_state == "waiting_provider":
            run_lifecycle.transition_run(
                state_root,
                run_id,
                to_state="step_queued",
                reason_class="provider_retry",
                transition="retry_provider",
                principal=principal,
                artifact_refs=[str(order_path)],
                run=run,
            )
        report_path = report_gate.report_path(state_root, run_id, step_id)
        request = adapter_request(
            state_root=state_root,
            run=run,
            work_order=work_order,
            adapter=adapter,
            principal=principal,
        )
        request_path = adapter_request_path(state_root, run_id, step_id, adapter["provider_adapter_id"])
        run_store.atomic_write_json(request_path, request)
        evidence_path = Path(request["evidence_path"])
        transcript_path = Path(request["transcript_path"])

        run_lifecycle.transition_run(
            state_root,
            run_id,
            to_state="waiting_provider",
            reason_class="provider_invoked",
            transition="run_provider",
            principal=principal,
            artifact_refs=[str(order_path), str(request_path)],
            run=run,
        )

        outcome, report, details = execute_provider(
            request=request,
            adapter=adapter,
            timeout_seconds=timeout_seconds,
            fake_provider_mode=fake_provider_mode,
        )
        if outcome == "ok" and report is None:
            outcome = "report_not_written"
            details = {**details, "reason": "report_not_written"}
        write_signal_transcript(transcript_path, {"outcome": outcome, "details": details})
        evidence = normalized_evidence(
            request=request,
            adapter=adapter,
            report=report,
            outcome=outcome,
            details=details,
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        run_store.atomic_write_json(evidence_path, evidence)

        if outcome != "ok" or report is None:
            transition = transition_failure(
                state_root=state_root,
                run_id=run_id,
                run=run,
                reason_class=outcome,
                principal=principal,
                artifact_refs=[str(request_path), str(evidence_path), str(transcript_path)],
            )
            append_audit_event(
                state_root=state_root,
                event_type="run_provider",
                principal=principal,
                subject=subject,
                outcome="blocked",
                details={"reason": outcome, "evidence_path": str(evidence_path)},
            )
            return {
                "schema_version": 1,
                "decision": "blocked",
                "reason": outcome,
                "adapter_request_path": str(request_path),
                "evidence_path": str(evidence_path),
                "transcript_path": str(transcript_path),
                "transition": transition,
                "workflow_run": run,
            }

        report_path.parent.mkdir(parents=True, exist_ok=True)
        run_store.atomic_write_json(report_path, report)

    gate_payload = report_gate.gate_report(
        state_root,
        run_id,
        report_path_arg=str(report_path),
        principal=principal,
    )
    append_audit_event(
        state_root=state_root,
        event_type="run_provider",
        principal=principal,
        subject=subject,
        outcome="ok" if gate_payload.get("decision") == "ok" else "blocked",
        details={
            "adapter_request_path": str(request_path),
            "report_path": str(report_path),
            "report_sha256": file_sha256(report_path),
            "evidence_path": str(evidence_path),
            "evidence_sha256": file_sha256(evidence_path),
            "gate_outcome": gate_payload.get("outcome"),
        },
    )
    return {
        "schema_version": 1,
        "decision": gate_payload.get("decision", "ok"),
        "reason": gate_payload.get("reason", "report_valid"),
        "adapter_request_path": str(request_path),
        "report_path": str(report_path),
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "provider_evidence": evidence,
        "report_gate": gate_payload,
        "workflow_run": gate_payload.get("workflow_run"),
    }


def run_provider_step(
    *,
    state_root: Path,
    run_id: str,
    adapter_id: str = "",
    adapter: str = "",
    timeout_seconds: int = 60,
    fake_provider_mode: str = "success",
    principal: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Compatibility hook for the offline E2E harness."""

    requested_adapter = adapter_id or adapter or DEFAULT_ADAPTER_ID
    if requested_adapter in {"fake_pass", "fake_provider", "fake"}:
        requested_adapter = DEFAULT_ADAPTER_ID
    actor = principal or {
        "principal_type": "harness_runner",
        "principal_id": "e2e-harness",
        "authn_method": "local_test",
    }
    return run_provider(
        state_root=state_root,
        run_id=run_id,
        adapter_id=requested_adapter,
        timeout_seconds=timeout_seconds,
        fake_provider_mode=fake_provider_mode or "success",
        principal=actor,
    )


def run_step(**kwargs: Any) -> dict[str, Any]:
    return run_provider_step(**kwargs)
