#!/usr/bin/env python3
"""Typed report gate for workflow-run validation and transition artifacts."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import provider_evidence_contract
import review_lifecycle
import run_lifecycle
import run_lock
import run_store
import safe_paths
import work_order_builder
import task_state_bridge
import workflow_selector
import work_order_builder

BRIDGE_PRINCIPAL_TYPE = "main_agent_bridge"
EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
}
FORBIDDEN_REPORT_CONTENT_KEYS = provider_evidence_contract.FORBIDDEN_RAW_CONTENT_KEYS
ADAPTER_METADATA_FIELDS = ("transport", "bridge_pattern", "surface_metadata")
ADAPTER_DESCRIPTOR_BINDING_FIELDS = ADAPTER_METADATA_FIELDS + (
    "default_model",
    "effective_model_policy",
)
EFFECTIVE_MODEL_POLICIES = {
    "required_exact_match",
    "record_without_equality",
}
MODEL_ASSURANCE_FOR_POLICY = {
    "required_exact_match": "exact_match_enforced",
    "record_without_equality": "provider_reported_only",
}
PROVIDER_MODEL_ASSURANCE_MISMATCH = "provider_model_assurance_mismatch"


class ReportGateError(RuntimeError):
    """A stable report-gate error surfaced through the frontdoor wrapper."""


def resolve_step_transition(
    template: dict[str, Any],
    step_id: str,
    event: str,
    *,
    accepted_steps: list[str],
    step_budget: int,
) -> dict[str, Any]:
    """Resolve a gate-derived event without granting authority to report fields."""
    steps = template.get("steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(s, dict) for s in steps):
        raise ReportGateError("unsupported_step_contract")
    ids = [s.get("id") for s in steps]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ReportGateError("unsupported_step_contract")
    if step_id not in ids:
        raise ReportGateError("step_report_mismatch")
    if step_id in accepted_steps:
        raise ReportGateError("duplicate_step_report")
    if accepted_steps != ids[:ids.index(step_id)]:
        raise ReportGateError("out_of_order_report")
    maximum = template.get("max_steps")
    if (
        type(step_budget) is not int or type(maximum) is not int
        or min(step_budget, maximum) < len(ids)
        or len(accepted_steps) >= min(step_budget, maximum)
    ):
        raise ReportGateError("step_budget_exceeded")
    step = steps[ids.index(step_id)]
    transitions = step.get("transitions")
    if not isinstance(transitions, list) or any(not isinstance(t, dict) for t in transitions):
        raise ReportGateError("undeclared_transition_event")
    matches = [t for t in transitions if t.get("on") == event]
    if not matches:
        raise ReportGateError("undeclared_transition_event")
    if len(matches) != 1:
        raise ReportGateError("duplicate_transition_event")
    target = matches[0].get("to")
    if target == "complete":
        if step_id != ids[-1]:
            raise ReportGateError("intermediate_terminal_transition")
    elif target in ("waiting_human", "blocked"):
        pass
    elif target not in ids:
        raise ReportGateError("unknown_transition_target")
    elif ids.index(target) <= ids.index(step_id) or target in accepted_steps:
        raise ReportGateError("cyclic_step_transition")
    elif ids.index(target) != ids.index(step_id) + 1:
        raise ReportGateError("out_of_order_transition")
    return dict(matches[0])


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def state_paths(state_root: Path) -> dict[str, Path]:
    root = state_root.expanduser().resolve(strict=False)
    return {
        "runs": root / "runs",
        "work_orders": root / "work-orders",
        "adapter_requests": root / "adapter-requests",
        "provider_evidence": root / "provider-evidence",
        "reports": root / "reports",
        "transitions": root / "transitions",
        "audit": root / "audit",
    }


def confined_state_artifact(
    state_root: Path,
    raw_path: str | Path,
    *,
    namespace: str,
    label: str,
) -> Path:
    try:
        return safe_paths.confined_state_path(
            state_root,
            raw_path,
            namespaces={namespace},
        )
    except safe_paths.SafePathError as exc:
        raise ReportGateError(f"{label} must stay under {namespace} state directory") from exc


def path_is_within(path: Path, parent: Path) -> bool:
    """Compatibility predicate for a path below one state namespace."""

    try:
        safe_paths.confined_state_path(
            parent.parent,
            path,
            namespaces={parent.name},
        )
    except safe_paths.SafePathError:
        return False
    return True


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = run_store.read_json(path)
    except run_store.RunStoreError as exc:
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
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-provider-transcript.json"
    )


def legacy_provider_transcript_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / run_store.validate_artifact_id(run_id, "run_id")
        / f"{run_store.validate_artifact_id(step_id, 'step_id')}-claude-transcript.json"
    )


def authoritative_adapter_request(
    state_root: Path,
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    accepted_request_path: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        run_id = run_store.validate_artifact_id(str(run.get("run_id") or ""), "run_id")
        step_id = run_store.validate_artifact_id(str(work_order.get("step_id") or ""), "step_id")
    except run_store.RunStoreError:
        return None, ["adapter_request_authority run or step identity is not artifact-safe"]
    request_dir = (
        state_paths(state_root)["adapter_requests"]
        / run_id
    )
    request_prefix = f"{step_id}-"

    transition_candidates: list[Path] = []
    has_run_provider_transition = False
    transitions = run.get("transitions")
    if isinstance(transitions, list):
        for transition in reversed(transitions):
            if not isinstance(transition, dict) or transition.get("transition") != "run_provider":
                continue
            refs = transition.get("artifact_refs")
            # A chain retains prior steps' attempts. Never use their request as
            # the authority for this step or mask the current step's request.
            if run.get("workflow_id") == "readonly_review_chain" and isinstance(refs, list):
                step_refs = [ref for ref in refs if isinstance(ref, str)
                             and Path(ref).parent.name == run_id
                             and Path(ref).name.startswith(request_prefix)]
                other_step_refs = [ref for ref in refs if isinstance(ref, str)
                                   and Path(ref).parent.name == run_id
                                   and any(Path(ref).name.startswith(s + "-") for s in CHAIN_CONTRACTS if s != step_id)]
                if other_step_refs and not step_refs:
                    continue
            has_run_provider_transition = True
            if isinstance(refs, list):
                for raw_ref in refs:
                    if not isinstance(raw_ref, str) or not raw_ref:
                        continue
                    try:
                        candidate = confined_state_artifact(
                            state_root,
                            raw_ref,
                            namespace="adapter-requests",
                            label="adapter request",
                        )
                    except ReportGateError:
                        continue
                    if (
                        candidate.parent == request_dir
                        and candidate.name.startswith(request_prefix)
                        and candidate.suffix == ".json"
                    ):
                        transition_candidates.append(candidate)
            break

    candidates = list(dict.fromkeys(transition_candidates))
    if accepted_request_path is not None and run.get("workflow_id") == CHAIN_ID:
        # Only a verified acceptance record can supply this internal binding.
        candidates = [confined_state_artifact(state_root, accepted_request_path,
                      namespace="adapter-requests", label="accepted adapter request")]
        has_run_provider_transition = True
    if run.get("workflow_id") == CHAIN_ID and not has_run_provider_transition:
        return None, ["adapter_request_authority current step has no recorded provider request"]
    if has_run_provider_transition and len(candidates) != 1:
        return None, [
            "adapter_request_authority run_provider transition requires exactly one "
            f"current request: found {len(candidates)}"
        ]
    if not has_run_provider_transition:
        try:
            candidates = run_store.list_private_artifacts(
                request_dir,
                prefix=request_prefix,
                suffix=".json",
            )
        except run_store.RunStoreError as exc:
            return None, [f"adapter_request_authority unsafe request artifacts: {exc.reason_class}"]
    if len(candidates) != 1:
        return None, [
            "adapter_request_authority requires exactly one current request: "
            f"found {len(candidates)}"
        ]

    request_path = candidates[0]
    try:
        request = read_json(request_path)
    except (ReportGateError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"adapter_request_authority unreadable: {exc}"]

    errors: list[str] = []
    if request.get("adapter_request_version") != "1":
        errors.append("adapter_request_authority.adapter_request_version must be '1'")
    expected_identity = {
        "request_id": str(run.get("request_id") or ""),
        "run_id": run_id,
        "workflow_id": str(run.get("workflow_id") or ""),
        "step_id": step_id,
    }
    for field, expected in expected_identity.items():
        if str(request.get(field) or "") != expected:
            errors.append(f"adapter_request_authority.{field} mismatch: expected {expected!r}")

    expected_paths = {
        "work_order_path": ("work-orders", work_order_path(state_root, run_id, step_id)),
        "report_path": ("reports", report_path(state_root, run_id, step_id)),
        "evidence_path": ("provider-evidence", provider_evidence_path(state_root, run_id, step_id)),
        "transcript_path": ("provider-evidence", provider_transcript_path(state_root, run_id, step_id)),
    }
    for field, (namespace, expected) in expected_paths.items():
        raw_path = request.get(field)
        if not isinstance(raw_path, str) or not raw_path:
            errors.append(f"adapter_request_authority.{field} must be non-empty string")
            continue
        try:
            candidate = confined_state_artifact(
                state_root,
                raw_path,
                namespace=namespace,
                label=f"adapter_request_authority.{field}",
            )
        except ReportGateError:
            errors.append(f"adapter_request_authority.{field} mismatch")
        else:
            if candidate != expected:
                errors.append(f"adapter_request_authority.{field} mismatch")

    adapter = request.get("adapter")
    if not isinstance(adapter, dict):
        return None, errors + ["adapter_request_authority.adapter must be object"]
    adapter_id = str(adapter.get("provider_adapter_id") or "")
    adapter_target = str(adapter.get("provider_target") or "")
    try:
        registry = workflow_selector.load_registry()
    except (OSError, json.JSONDecodeError) as exc:
        return None, errors + [f"adapter_request_authority registry unreadable: {exc}"]
    registry_adapters = {
        str(item.get("provider_adapter_id") or ""): item
        for item in registry.get("provider_adapters", [])
        if isinstance(item, dict) and item.get("provider_adapter_id")
    }
    registered = registry_adapters.get(adapter_id)
    if registered is None:
        errors.append(f"adapter_request_authority adapter is not registered: {adapter_id!r}")
    else:
        if str(registered.get("provider_target") or "") != adapter_target:
            errors.append("adapter_request_authority.provider_target does not match registry")
        for field in ADAPTER_DESCRIPTOR_BINDING_FIELDS:
            if field in registered and adapter.get(field) != registered[field]:
                errors.append(f"adapter_request_authority.{field} does not match registry")

    if adapter_id:
        try:
            validated_adapter_id = run_store.validate_artifact_id(adapter_id, "adapter_id")
        except run_store.RunStoreError:
            errors.append("adapter_request_authority.provider_adapter_id is not artifact-safe")
        else:
            expected_request_path = request_dir / f"{request_prefix}{validated_adapter_id}.json"
            if request_path != expected_request_path:
                errors.append("adapter_request_authority path does not match provider_adapter_id")
    else:
        errors.append("adapter_request_authority.provider_adapter_id must be non-empty string")
    if not adapter_target:
        errors.append("adapter_request_authority.provider_target must be non-empty string")
    if errors:
        return None, list(dict.fromkeys(errors))
    authoritative_metadata: dict[str, Any] = {
        "provider_adapter_id": adapter_id,
        "provider_target": adapter_target,
    }
    if registered is not None:
        for field in ADAPTER_DESCRIPTOR_BINDING_FIELDS:
            if field in registered:
                authoritative_metadata[field] = registered[field]
    if run.get("workflow_id") == "readonly_review_chain":
        authoritative_metadata["request"] = request
        authoritative_metadata["request_path"] = str(request_path)
    return authoritative_metadata, []


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(run_store.read_bytes(path)).hexdigest()


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
    run_store.append_json_line(path, event)
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
                if key in FORBIDDEN_REPORT_CONTENT_KEYS:
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
                try:
                    confined_state_artifact(
                        state_root,
                        raw,
                        namespace="provider-evidence",
                        label=f"provider_evidence.{field}",
                    )
                except ReportGateError:
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
        if PROVIDER_MODEL_ASSURANCE_MISMATCH in errors:
            return PROVIDER_MODEL_ASSURANCE_MISMATCH, errors
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
    if run.get('workflow_id') == 'standard_code_change':
        return validate_standard_review_report(report, run=run, work_order=work_order, state_root=state_root)
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
    allowed = required | {"recommendations", "resolution"}
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
    if report.get("workflow_id") not in {"single_step_external_review", "readonly_review_chain"}:
        errors.append("workflow_id must be single_step_external_review or readonly_review_chain")
    if report.get("step_id") != "review":
        errors.append("step_id must be review")
    if report.get("result") not in {"pass", "findings", "blocked", "invalid"}:
        errors.append("result unsupported")
    if not isinstance(report.get("summary"), str) or not report.get("summary"):
        errors.append("summary must be non-empty string")

    errors.extend(validate_provider_evidence(report.get("provider_evidence"), run, work_order, state_root))
    errors.extend(
        validate_normalized_provider_evidence_file(
            report,
            run=run,
            work_order=work_order,
            state_root=state_root,
        )
    )
    errors.extend(validate_findings(report.get("findings"), report.get("result")))
    errors.extend(validate_authority(report.get("authority")))
    flow = run.get('review_lifecycle', {}).get('resolution_flow')
    if 'resolution' in report and not flow:
        errors.append('resolution_without_flow')
    if not errors and report.get('result') in {'pass', 'findings'}:
        # Validate against a copy before the report gate persists any state.
        candidate = copy.deepcopy(run)
        try:
            review_lifecycle.start_from_gated_findings(candidate, report, work_order=work_order,
                principal=work_order['work_order_authority']['issuer_principal'])
            if not candidate.get('review_lifecycle', {}).get('resolution_flow'):
                return errors
            review_lifecycle.consume_gated_report(candidate, report, work_order=work_order,
                report_ref=str(report_path(state_root, str(run['run_id']), str(work_order['step_id']))),
                digest='sha256:' + stable_digest(report))
            errors.extend(review_lifecycle.validate_record(candidate['review_lifecycle'], run=candidate))
        except (review_lifecycle.ReviewLifecycleError, KeyError, TypeError, ValueError) as exc:
            errors.append('resolution_report_invalid:' + str(exc))
    return errors


def validate_effective_model_binding(
    value: dict[str, Any],
    *,
    work_order: dict[str, Any],
    adapter_identity: dict[str, Any] | None,
    label: str,
) -> list[str]:
    if adapter_identity is None:
        return [f"{label}.effective_model policy authority unavailable"]
    errors: list[str] = []
    expected_adapter_id = adapter_identity.get("provider_adapter_id")
    expected_policy = adapter_identity.get("effective_model_policy")
    expected_assurance = MODEL_ASSURANCE_FOR_POLICY.get(str(expected_policy or ""))
    assurance_mismatch = False
    if expected_adapter_id != work_order.get("provider_adapter_id"):
        errors.append(f"{label}.provider_adapter_id mismatch with work order")
    if value.get("provider_adapter_id") != expected_adapter_id:
        errors.append(f"{label}.provider_adapter_id mismatch with adapter registry")
    if adapter_identity.get("default_model") != work_order.get("intended_model"):
        errors.append(f"{label}.intended_model mismatch with adapter registry")
    if value.get("intended_model") != adapter_identity.get("default_model"):
        errors.append(f"{label}.intended_model mismatch with adapter descriptor")
    if expected_policy not in EFFECTIVE_MODEL_POLICIES:
        errors.append(f"{label}.effective_model policy unsupported")
        assurance_mismatch = True
    elif value.get("effective_model_policy") != expected_policy:
        errors.append(f"{label}.effective_model_policy mismatch with adapter registry")
        assurance_mismatch = True
    if value.get("model_assurance") != expected_assurance:
        errors.append(f"{label}.model_assurance mismatch with declared policy")
        assurance_mismatch = True
    if expected_policy == "required_exact_match" and value.get("effective_model") != value.get(
        "intended_model"
    ):
        errors.append(f"{label}.effective_model must match intended_model")
        assurance_mismatch = True
    if assurance_mismatch:
        return [PROVIDER_MODEL_ASSURANCE_MISMATCH, *errors]
    return errors


def validate_provider_evidence(
    value: Any,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
    *,
    accepted_request_path: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["provider_evidence must be object"]
    required = {
        "provider",
        "provider_adapter_id",
        "intended_model",
        "effective_model",
        "effective_model_policy",
        "model_assurance",
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
    if str(value.get("intended_model") or "") != str(work_order.get("intended_model") or ""):
        errors.append("provider_evidence.intended_model mismatch")
    adapter_identity, adapter_errors = authoritative_adapter_request(
        state_root,
        run=run,
        work_order=work_order,
        accepted_request_path=accepted_request_path,
    )
    errors.extend(adapter_errors)
    errors.extend(
        validate_effective_model_binding(
            value,
            work_order=work_order,
            adapter_identity=adapter_identity,
            label="provider_evidence",
        )
    )
    for field in (
        "provider",
        "provider_adapter_id",
        "intended_model",
        "effective_model",
        "effective_model_policy",
        "model_assurance",
        "provider_session_id",
        "transcript_path",
        "evidence_path",
    ):
        if not isinstance(value.get(field), str) or not value.get(field):
            errors.append(f"provider_evidence.{field} must be non-empty string")
    try:
        run_id = run_store.validate_artifact_id(str(run.get("run_id") or ""), "run_id")
        step_id = run_store.validate_artifact_id(str(work_order.get("step_id") or ""), "step_id")
    except run_store.RunStoreError:
        return errors + ["provider_evidence run or step identity is not artifact-safe"]
    expected_paths = {
        "transcript_path": [
            provider_transcript_path(state_root, run_id, step_id),
            legacy_provider_transcript_path(state_root, run_id, step_id),
        ],
        "evidence_path": [provider_evidence_path(state_root, run_id, step_id)],
    }
    for field in ("transcript_path", "evidence_path"):
        raw_path = value.get(field)
        if not isinstance(raw_path, str) or not raw_path:
            continue
        try:
            path = confined_state_artifact(
                state_root,
                raw_path,
                namespace="provider-evidence",
                label=f"provider_evidence.{field}",
            )
        except ReportGateError:
            errors.append(f"provider_evidence.{field} must stay under provider evidence state directory")
            continue
        if path not in expected_paths[field]:
            errors.append(f"provider_evidence.{field} must match current run evidence path")
            continue
        try:
            exists = run_store.private_artifact_exists(path)
        except run_store.RunStoreError:
            errors.append(f"provider_evidence.{field} is not a safe private artifact")
        else:
            if not exists:
                errors.append(f"provider_evidence.{field} does not exist: {path}")
    return errors


def validate_normalized_provider_evidence(
    value: Any,
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
    evidence_path: Path,
    report_provider_evidence: dict[str, Any],
    accepted_request_path: str | None = None,
) -> list[str]:
    errors = provider_evidence_contract.validate_provider_evidence_schema(value)
    if not isinstance(value, dict):
        return errors

    expected_identity = {
        "request_id": str(run.get("request_id") or ""),
        "run_id": str(run.get("run_id") or ""),
        "workflow_id": str(run.get("workflow_id") or ""),
        "step_id": str(work_order.get("step_id") or ""),
    }
    for field, expected in expected_identity.items():
        if str(value.get(field) or "") != expected:
            errors.append(f"normalized_evidence.{field} mismatch: expected {expected!r}")

    for field in (
        "provider",
        "provider_adapter_id",
        "intended_model",
        "effective_model",
        "effective_model_policy",
        "model_assurance",
        "provider_session_id",
    ):
        expected = str(report_provider_evidence.get(field) or "")
        if str(value.get(field) or "") != expected:
            errors.append(f"normalized_evidence.{field} mismatch: expected {expected!r}")
    if str(value.get("intended_model") or "") != str(work_order.get("intended_model") or ""):
        errors.append("normalized_evidence.intended_model mismatch with work order")

    adapter_identity, adapter_errors = authoritative_adapter_request(
        state_root,
        run=run,
        work_order=work_order,
        accepted_request_path=accepted_request_path,
    )
    errors.extend(adapter_errors)
    if adapter_identity is not None:
        for field in ("provider_adapter_id", "provider_target", *ADAPTER_METADATA_FIELDS):
            expected = adapter_identity.get(field)
            if value.get(field) != expected:
                errors.append(f"normalized_evidence.{field} mismatch: expected {expected!r}")
    errors.extend(
        validate_effective_model_binding(
            value,
            work_order=work_order,
            adapter_identity=adapter_identity,
            label="normalized_evidence",
        )
    )

    expected_evidence_path = provider_evidence_path(
        state_root,
        expected_identity["run_id"],
        expected_identity["step_id"],
    )
    if evidence_path != expected_evidence_path:
        errors.append("normalized_evidence path must match current run evidence path")
    artifact_evidence_path = value.get("evidence_path")
    if isinstance(artifact_evidence_path, str) and artifact_evidence_path:
        try:
            normalized_artifact_evidence_path = confined_state_artifact(
                state_root,
                artifact_evidence_path,
                namespace="provider-evidence",
                label="normalized_evidence.evidence_path",
            )
        except ReportGateError:
            normalized_artifact_evidence_path = None
        if normalized_artifact_evidence_path != evidence_path:
            errors.append("normalized_evidence.evidence_path must reference its own artifact")

    expected_transcript_paths = {
        provider_transcript_path(
            state_root,
            expected_identity["run_id"],
            expected_identity["step_id"],
        ),
        legacy_provider_transcript_path(
            state_root,
            expected_identity["run_id"],
            expected_identity["step_id"],
        ),
    }
    artifact_transcript_path = value.get("transcript_path")
    report_transcript_path = report_provider_evidence.get("transcript_path")
    if isinstance(artifact_transcript_path, str) and artifact_transcript_path:
        try:
            transcript_path = confined_state_artifact(
                state_root,
                artifact_transcript_path,
                namespace="provider-evidence",
                label="normalized_evidence.transcript_path",
            )
        except ReportGateError:
            transcript_path = None
        if transcript_path not in expected_transcript_paths:
            errors.append("normalized_evidence.transcript_path must match current run transcript path")
        elif transcript_path is not None:
            try:
                exists = run_store.private_artifact_exists(transcript_path)
            except run_store.RunStoreError:
                errors.append("normalized_evidence.transcript_path is not a safe private artifact")
            else:
                if not exists:
                    errors.append(f"normalized_evidence.transcript_path does not exist: {transcript_path}")
        if (
            isinstance(report_transcript_path, str)
            and report_transcript_path
        ):
            try:
                normalized_report_transcript_path = confined_state_artifact(
                    state_root,
                    report_transcript_path,
                    namespace="provider-evidence",
                    label="report provider_evidence.transcript_path",
                )
            except ReportGateError:
                normalized_report_transcript_path = None
            if transcript_path != normalized_report_transcript_path:
                errors.append("normalized_evidence.transcript_path must match report provider_evidence")

    if value.get("outcome") != "ok":
        errors.append("normalized_evidence.outcome must be 'ok' for a valid report")
    return list(dict.fromkeys(errors))


def validate_normalized_provider_evidence_file(
    report: dict[str, Any],
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    report_provider_evidence = report.get("provider_evidence")
    if not isinstance(report_provider_evidence, dict):
        return []
    raw_path = report_provider_evidence.get("evidence_path")
    if not isinstance(raw_path, str) or not raw_path:
        return []
    try:
        path = confined_state_artifact(
            state_root,
            raw_path,
            namespace="provider-evidence",
            label="normalized_evidence.evidence_path",
        )
    except ReportGateError:
        return []
    try:
        run_id = run_store.validate_artifact_id(str(run.get("run_id") or ""), "run_id")
        step_id = run_store.validate_artifact_id(str(work_order.get("step_id") or ""), "step_id")
    except run_store.RunStoreError:
        return ["normalized_evidence run or step identity is not artifact-safe"]
    expected_path = provider_evidence_path(state_root, run_id, step_id)
    if path != expected_path:
        return ["normalized_evidence path must match current run evidence path"]
    try:
        if not run_store.private_artifact_exists(path):
            return []
    except run_store.RunStoreError:
        return ["normalized_evidence artifact is not a safe private artifact"]
    try:
        value = read_json(path)
    except ReportGateError as exc:
        return [f"normalized_evidence unreadable: {exc}"]
    return validate_normalized_provider_evidence(
        value,
        run=run,
        work_order=work_order,
        state_root=state_root,
        evidence_path=path,
        report_provider_evidence=report_provider_evidence,
    )


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
    safe_suffix = run_store.validate_artifact_id(suffix, "artifact_suffix")
    existing = run_store.list_private_artifacts(path, suffix=f"-{safe_suffix}.json")
    return path / f"{len(existing) + 1:04d}-{safe_suffix}.json"


def write_transition_artifact(
    *,
    state_root: Path,
    run_id: str,
    payload: dict[str, Any],
) -> Path:
    safe_run_id = run_store.validate_artifact_id(run_id, "run_id")
    path = next_numbered_artifact(
        state_paths(state_root)["transitions"] / safe_run_id,
        "report-gate",
    )
    run_store.atomic_write_json(path, payload)
    return path


def write_rejection_artifact(
    *,
    state_root: Path,
    run_id: str,
    step_id: str,
    payload: dict[str, Any],
) -> Path:
    safe_run_id = run_store.validate_artifact_id(run_id, "run_id")
    safe_step_id = run_store.validate_artifact_id(step_id, "step_id")
    directory = state_paths(state_root)["reports"] / safe_run_id
    existing = run_store.list_private_artifacts(
        directory,
        prefix=f"{safe_step_id}-rejection-",
        suffix=".json",
    )
    path = directory / f"{safe_step_id}-rejection-{len(existing) + 1}.json"
    run_store.atomic_write_json(path, payload)
    return path


CHAIN_ID = "readonly_review_chain"
CHAIN_CONTRACTS = {
    "research": ("research_report", "research-report.schema.json", "research_complete", "report_invalid"),
    "review": ("external_review_report", "external-review-report.schema.json", "review_complete", "review_blocked"),
    "final_evidence": ("final_evidence", "readonly-final-evidence-report.schema.json", "final_evidence_valid", "final_evidence_invalid"),
}
CHAIN_QUALITY_GATES = {
    "research": [
        {"gate": "source_refs_gate", "requires": "source_refs"},
        {"gate": "uncertainty_gate", "requires": "uncertainty"},
        {"gate": "no_diff_gate", "requires": "no_diff_completion"},
    ],
    "review": [
        {"gate": "schema_gate", "requires": "external_review_report"},
        {"gate": "provider_evidence_gate", "requires": ["provider_session_id", "request_id", "intended_model",
                                                        "effective_model", "transcript_path", "evidence_path"]},
        {"gate": "context_scope_gate", "forbids": ["secrets", "unbounded_repo_dump", "unbounded_vault_dump", "raw_transcript_broadcast"]},
    ],
    "final_evidence": [{"gate": "final_evidence_gate", "requires": "evidence_refs"}],
}
CHAIN_ROLES = {"research": ("contents-researcher", "observer"), "review": ("tech-reviewer", "reviewer"),
               "final_evidence": ("gate-task-evaluator", "reviewer")}


def _chain_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(run_store.read_bytes(path)).hexdigest()


def _chain_source_contract(template: dict[str, Any]) -> str:
    schemas = {name: hashlib.sha256((workflow_selector.SCHEMA_ROOT / name).read_bytes()).hexdigest()
               for _, name, _, _ in CHAIN_CONTRACTS.values()}
    return stable_digest({"template": template, "schemas": schemas})


def _chain_contract(template: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(template, dict) or template.get("workflow_id") != CHAIN_ID:
        raise ReportGateError("unsupported_step_contract")
    steps = template.get("steps")
    if not isinstance(steps, list) or [s.get("id") for s in steps if isinstance(s, dict)] != list(CHAIN_CONTRACTS):
        raise ReportGateError("unsupported_step_contract")
    if template.get("safety_class") != "readonly" or template.get("initial_step") != "research":
        raise ReportGateError("unsupported_step_contract")
    supported_gates = {"entry.activation_approved", "entry.work_order_valid", "exit.typed_report_valid",
                       "exit.research_evidence_complete", "exit.final_evidence_complete"}
    gates = template.get("gates", {})
    if (set(template.get("mandatory_gates", [])) != supported_gates
            or set(gates.get("entry", []) + gates.get("exit", [])) != supported_gates):
        raise ReportGateError("unsupported_gate_contract")
    selection = run.get("activation", {}).get("workflow_selection", {})
    if selection.get("workflow_id") != CHAIN_ID or run.get("activation", {}).get("activation_status") != "approved":
        raise ReportGateError("step_report_mismatch")
    for step in steps:
        sid = step["id"]
        expected_route = {
            "adapter_kind": "harness_gate" if sid == "final_evidence" else "bounded_provider",
            "runner_authority": "validate_evidence_only" if sid == "final_evidence" else "write_report_only",
            "transition_authority": "harness_engine",
        }
        output, schema_name, _, _ = CHAIN_CONTRACTS[sid]
        declaration = template.get("output_contracts", {}).get(output, {})
        if (step.get("output_contract") != output
                or declaration.get("schema_path") != "organization/runtime/workflows/schemas/" + schema_name
                or declaration.get("required") is not True or declaration.get("canonical") is not True
                or step.get("permission_mode") != "readonly"
                or (step.get("role"), step.get("assignment_role")) != CHAIN_ROLES[sid]
                or step.get("provider_route") != expected_route
                or set(step.get("allowed_ops", {})) != {"edit", "commit", "push", "network"}
                or any(v is not False for v in step["allowed_ops"].values())):
            raise ReportGateError("unsupported_step_contract")
        if step.get("quality_gates") != CHAIN_QUALITY_GATES[sid]:
            raise ReportGateError("unsupported_gate_contract")
    return steps


def _chain_order_binding(state_root: Path, run: dict[str, Any], template: dict[str, Any],
                         step: dict[str, Any], iteration: int) -> tuple[dict[str, Any], dict[str, str]]:
    # Use the same signed order and frozen snapshot as the runner, including for
    # previously accepted steps. Current-step-only runner verification cannot
    # verify an earlier step after the chain advances.
    import scoped_worker_executor

    sid = step["id"]
    order_path = work_order_path(state_root, run["run_id"], sid)
    order = read_json(order_path)
    errors = work_order_builder.validate_work_order(order, template=template, step=step, state_root=state_root, run=run)
    expected = {"to_role": step["role"], "assignment_role": step["assignment_role"],
                "expected_output": step["output_contract"], "permission_mode": "readonly"}
    if errors or any(order.get(k) != v for k, v in expected.items()):
        raise ReportGateError("work_order_contract_mismatch")
    scope = order.get("activation_scope", {})
    if (scope != run.get("activation", {}).get("activation_scope")
            or set(scope.get("allowed_ops", {})) != {"edit", "commit", "push", "network"}
            or any(v is not False for v in scope.get("allowed_ops", {}).values())
            or order.get("external_provider_allowed") is not (sid != "final_evidence")):
        raise ReportGateError("work_order_contract_mismatch")
    try:
        scoped_worker_executor.verify_work_order_signature(state_root, order)
    except scoped_worker_executor.ScopedWorkerError as exc:
        raise ReportGateError("work_order_signature_invalid") from exc
    snapshot_path = work_order_builder.snapshot_path(state_root, run["run_id"], sid, iteration)
    snapshot = read_json(snapshot_path)
    digest = work_order_builder.sha256_digest(order)
    if (snapshot.get("snapshot_version") != "1" or type(snapshot.get("iteration")) is not int
            or snapshot.get("iteration") != iteration or snapshot.get("run_id") != run["run_id"]
            or snapshot.get("step_id") != sid or snapshot.get("work_order") != order
            or snapshot.get("work_order_digest") != digest
            or any(snapshot.get(k) != order.get(k) for k in ("activation_scope", "context_refs", "policy_digest"))):
        raise ReportGateError("work_order_snapshot_mismatch")
    return order, {"work_order_path": str(order_path), "work_order_sha256": _chain_digest(order_path),
                   "snapshot_path": str(snapshot_path), "snapshot_sha256": _chain_digest(snapshot_path)}


def _verify_chain_promotion(request: dict[str, Any], journal: dict[str, Any],
                            attempt_transcript: dict[str, Any], transcript: dict[str, Any],
                            transcript_digest: str, evidence: dict[str, Any]) -> None:
    """Reconstruct existing recovery/finalize output from the authoritative journal."""
    import provider_runner

    details = journal.get("details")
    if not isinstance(details, dict):
        raise ReportGateError("provider_result_promotion_mismatch")
    # Recovery copies the attempt JSON exactly. Normal finalize regenerates only
    # the envelope timestamp; signal finalize uses the completed journal details.
    matches = canonical_json(transcript) == canonical_json(attempt_transcript)
    timestamp = transcript.get("written_at")
    if not matches and isinstance(timestamp, str) and timestamp:
        if attempt_transcript.get("provider_transcript_version") == "1":
            expected = {**attempt_transcript, "written_at": timestamp}
            matches = canonical_json(transcript) == canonical_json(expected)
        elif attempt_transcript.get("transcript_signal_version") == "1":
            expected = {"transcript_signal_version": "1", "written_at": timestamp,
                        "payload": {"outcome": journal["outcome"], "details": details},
                        "raw_content_policy": "signal_only_not_shared"}
            matches = canonical_json(transcript) == canonical_json(expected)
    if not matches:
        raise ReportGateError("provider_result_promotion_mismatch")
    promoted_details = {**details, "transcript_sha256": transcript_digest}
    if transcript.get("provider_transcript_version") == "1":
        promoted_details["stdout_sha256"] = transcript.get("stdout_sha256")
        promoted_details["stderr_sha256"] = transcript.get("stderr_sha256")
    expected_evidence = provider_runner.normalized_evidence(request=request, adapter=request["adapter"],
        report=journal["report"], outcome=journal["outcome"], details=promoted_details)
    # JSON comparison retains boolean/number distinctions that dict equality loses.
    if canonical_json(evidence) != canonical_json(expected_evidence):
        raise ReportGateError("provider_result_promotion_mismatch")


def _chain_provider_binding(state_root: Path, run: dict[str, Any], order: dict[str, Any],
                            bindings: dict[str, Any], *, accepted_record: dict[str, Any] | None = None) -> dict[str, Any]:
    import provider_runner

    metadata, errors = authoritative_adapter_request(state_root, run=run, work_order=order,
        accepted_request_path=accepted_record["request_path"] if accepted_record is not None else None)
    if errors or metadata is None:
        raise ReportGateError("adapter_request_authority: " + "; ".join(errors))
    request = metadata["request"]
    request_digest = "sha256:" + stable_digest({k: v for k, v in request.items() if k != "adapter_request_digest"})
    context = request.get("approved_context")
    if not isinstance(context, list) or len(context) != len(order["context_refs"]):
        raise ReportGateError("context_snapshot_mismatch")
    for ref, item in zip(order["context_refs"], context):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ReportGateError("context_snapshot_mismatch")
        content = item["content"].encode("utf-8")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if (item.get("path") != ref.get("value") or item.get("sha256") != ref.get("digest")
                or digest != ref.get("digest") or len(content) != ref.get("size_bytes")
                or item.get("size_bytes") != len(content)):
            raise ReportGateError("context_snapshot_mismatch")
    context_bytes = canonical_json(context)
    if (request.get("context_snapshot_digest") != "sha256:" + stable_digest(context)
            or request.get("context_snapshot") != {"content": context_bytes.decode("utf-8"),
                "byte_length": len(context_bytes), "sha256": hashlib.sha256(context_bytes).hexdigest()}):
        raise ReportGateError("context_snapshot_mismatch")
    attempt = request.get("attempt_id")
    if (not isinstance(attempt, str) or not attempt
            or request.get("adapter_request_digest") != request_digest
            or request.get("work_order_digest") != work_order_builder.sha256_digest(order)
            or request.get("work_order_snapshot_path") != bindings["snapshot_path"]
            or request.get("authority", {}).get("work_order_signature") != order["work_order_authority"]["signature"]
            or request.get("context_refs") != order["context_refs"]):
        raise ReportGateError("step_attempt_mismatch")
    execution = run.get("provider_execution")
    if accepted_record is None:
        if not isinstance(execution, dict):
            raise ReportGateError("provider_execution_missing")
        if execution.get("phase") != "result_ready":
            raise ReportGateError("provider_result_not_ready")
        if (execution.get("step_id") != order["step_id"] or execution.get("attempt_id") != attempt
                or execution.get("adapter_request_digest") != request_digest
                or execution.get("work_order_digest") != request.get("work_order_digest")
                or execution.get("adapter_id") != order.get("provider_adapter_id")
                or execution.get("context_snapshot_digest") != request.get("context_snapshot_digest")
                or execution.get("provider_binding") != provider_runner.work_order_model_policy_binding(order)
                or execution.get("lease", {}).get("lease_id") != request.get("lease_id")):
            raise ReportGateError("step_attempt_mismatch")
        claims = [t for t in run.get("transitions", []) if t.get("transition") == "run_provider"
                  and metadata["request_path"] in t.get("artifact_refs", [])]
        if not claims:
            raise ReportGateError("provider_claim_missing")
        claim = claims[-1]
        _verify_chain_acceptance(state_root, claim, transition_name="run_provider")
        run_lifecycle.assert_execution_principal(claim.get("principal", {}))
        if (claim.get("run_id") != run["run_id"] or claim.get("from_state") != "step_queued"
                or claim.get("to_state") != "waiting_provider" or claim.get("reason_class") != "provider_claimed"
                or len(claim.get("artifact_refs", [])) != 3
                or set(claim.get("artifact_refs", [])) != {bindings["work_order_path"], bindings["snapshot_path"], metadata["request_path"]}):
            raise ReportGateError("provider_claim_invalid")
    evidence_path = provider_evidence_path(state_root, run["run_id"], order["step_id"])
    evidence = read_json(evidence_path)
    embedded_fields = ("provider", "provider_adapter_id", "intended_model", "effective_model", "effective_model_policy",
                       "model_assurance", "request_id", "provider_session_id", "transcript_path", "evidence_path")
    embedded = {k: evidence.get(k) for k in embedded_fields}
    pinned_path = accepted_record["request_path"] if accepted_record is not None else None
    errors = validate_provider_evidence(embedded, run, order, state_root, accepted_request_path=pinned_path)
    errors += validate_normalized_provider_evidence(evidence, run=run, work_order=order, state_root=state_root,
                                                   evidence_path=evidence_path, report_provider_evidence=embedded,
                                                   accepted_request_path=pinned_path)
    transcript_path = provider_transcript_path(state_root, run["run_id"], order["step_id"])
    transcript_digest = _chain_digest(transcript_path)
    if (errors or evidence.get("attempt_id") != attempt or evidence.get("transcript_sha256") != transcript_digest):
        raise ReportGateError("provider_evidence_mismatch: " + "; ".join(errors))
    result_path, attempt_transcript = provider_runner.provider_attempt_paths(state_root, run["run_id"], attempt)
    journal = read_json(result_path)
    journal_expected = {"attempt_id": attempt, "lease_id": request.get("lease_id"),
        "work_order_digest": request["work_order_digest"], "adapter_request_digest": request_digest,
        "context_snapshot_digest": request["context_snapshot_digest"], "adapter_id": order["provider_adapter_id"]}
    if (journal.get("outcome") != "ok" or journal.get("abandoned") is True
            or any(journal.get(k) != v for k, v in journal_expected.items())
            or canonical_json(journal.get("report")) != canonical_json(read_json(report_path(state_root, run["run_id"], order["step_id"])))
            or journal.get("transcript_path") != str(attempt_transcript)
            or journal.get("transcript_sha256") != _chain_digest(attempt_transcript)):
        raise ReportGateError("provider_result_journal_mismatch")
    _verify_chain_promotion(request, journal, read_json(attempt_transcript), read_json(transcript_path),
                            transcript_digest, evidence)
    if accepted_record is None and execution.get("last_outcome", {}).get("attempt_result_path") != str(result_path):
        raise ReportGateError("provider_result_not_promoted")
    return {"request_path": metadata["request_path"], "request_sha256": _chain_digest(Path(metadata["request_path"])),
            "adapter_request_digest": request_digest, "attempt_id": attempt,
            "attempt_result_path": str(result_path), "attempt_result_sha256": _chain_digest(result_path),
            "attempt_transcript_path": str(attempt_transcript), "attempt_transcript_sha256": _chain_digest(attempt_transcript),
            "evidence_path": str(evidence_path), "evidence_sha256": _chain_digest(evidence_path),
            "transcript_path": str(transcript_path), "transcript_sha256": transcript_digest}


def _verify_chain_acceptance(state_root: Path, acceptance: dict[str, Any], *,
                             transition_name: str = "report_gate_acceptance") -> None:
    signature = acceptance.get("signature")
    principal = acceptance.get("principal")
    if not isinstance(signature, dict) or not isinstance(principal, dict):
        raise ReportGateError("prior_acceptance_invalid")
    key = run_store.read_bytes(run_lifecycle.signing_key_path(state_root, principal)).strip()
    material = {"principal": run_lifecycle.redacted_principal(principal), "transition": transition_name,
                "subject": {k: v for k, v in acceptance.items() if k != "signature"}}
    expected = "sha256:" + hmac.new(key, canonical_json(material), hashlib.sha256).hexdigest()
    if signature.get("algorithm") != "sha256-local-principal-key" or not hmac.compare_digest(str(signature.get("signature")), expected):
        raise ReportGateError("prior_acceptance_invalid")


def _verify_chain_transition(state_root: Path, record: dict[str, Any], acceptance: dict[str, Any]) -> None:
    _verify_chain_acceptance(state_root, record, transition_name="validate_report")
    target = acceptance.get("to_step")
    expected_state = target if target in {"complete", "waiting_human"} else "step_queued"
    if (record.get("transition") != "validate_report" or record.get("run_id") != acceptance.get("run_id")
            or record.get("from_state") != "validating" or record.get("to_state") != expected_state
            or record.get("reason_class") != acceptance.get("on") or record.get("report_binding") != acceptance):
        raise ReportGateError("prior_acceptance_invalid")


def _chain_prior_acceptances(state_root: Path, run: dict[str, Any], template: dict[str, Any],
                             steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = [h for h in run.get("step_history", []) if h.get("status") == "accepted"]
    accepted = []
    for index, history in enumerate(records):
        value = history.get("acceptance")
        if not isinstance(value, dict):
            raise ReportGateError("prior_acceptance_invalid")
        _verify_chain_acceptance(state_root, value)
        if (index >= len(steps) or value.get("step_id") != steps[index]["id"]
                or history.get("step_id") != value["step_id"] or value.get("run_id") != run["run_id"]
                or value.get("request_id") != run["request_id"] or value.get("source_contract") != _chain_source_contract(template)):
            raise ReportGateError("prior_acceptance_invalid")
        sid = value["step_id"]
        path = report_path(state_root, run["run_id"], sid)
        if value.get("report_path") != str(path) or value.get("report_sha256") != _chain_digest(path):
            raise ReportGateError("prior_artifact_drift")
        order, bindings = _chain_order_binding(state_root, run, template, steps[index], value.get("iteration"))
        if sid != "final_evidence":
            bindings.update(_chain_provider_binding(state_root, run, order, bindings, accepted_record=value))
        if any(value.get(k) != v for k, v in bindings.items()):
            raise ReportGateError("prior_artifact_drift")
        transitions = [t for t in run.get("transitions", []) if t.get("report_binding") == value]
        if len(transitions) != 1 or transitions[0].get("reason_class") != value.get("on"):
            raise ReportGateError("prior_acceptance_invalid")
        _verify_chain_transition(state_root, transitions[0], value)
        accepted.append(value)
    return accepted


def _chain_report_errors(state_root: Path, run: dict[str, Any], order: dict[str, Any],
                         report: dict[str, Any], sid: str) -> list[str]:
    schema_name = CHAIN_CONTRACTS[sid][1]
    schema = json.loads((workflow_selector.SCHEMA_ROOT / schema_name).read_text())
    errors = work_order_builder._validate_schema_fragment(report, schema, "$")
    errors += _scope_violation_errors(report, run=run, state_root=state_root)
    if sid in {"research", "final_evidence"} and report.get("no_diff_completion") is not True:
        errors.append("no_diff_completion must be boolean true")
    if sid == "research":
        allowed_refs = {ref["value"] for ref in order["context_refs"]}
        sources = report.get("source_refs")
        if not isinstance(sources, list) or any(not isinstance(ref, str) or ref not in allowed_refs for ref in sources):
            errors.append("source_refs outside bounded work order")
        findings = report.get("findings")
        if isinstance(findings, list):
            for finding in findings:
                if isinstance(finding, dict) and isinstance(finding.get("evidence_refs"), list):
                    if any(not isinstance(ref, str) or ref not in allowed_refs for ref in finding["evidence_refs"]):
                        errors.append("finding evidence outside bounded work order")
    if sid == "review":
        errors += validate_external_review_report(report, run=run, work_order=order, state_root=state_root)
        authority = report.get("authority") if isinstance(report.get("authority"), dict) else {}
        if authority.get("stdout_is_signal_only") is not True or authority.get("raw_transcript_shared") is not False:
            errors.append("review authority must contain strict booleans")
    return errors


def require_existing_signing_key(state_root: Path, principal: dict[str, Any]) -> None:
    """Read-only guard: never create or repair a signing credential."""
    try:
        key = run_store.read_bytes(run_lifecycle.signing_key_path(state_root, principal))
        if not key.strip():
            raise ReportGateError("signing_key_unavailable")
    except (run_store.RunStoreError, OSError) as exc:
        raise ReportGateError("signing_key_unavailable") from exc


def _chain_claim_is_live(run: dict[str, Any], order: dict[str, Any],
                         verified_prior: list[dict[str, Any]]) -> bool:
    execution = run.get("provider_execution", {})
    # #108 retains the completed review lease. Only a verified accepted attempt
    # can establish that it belongs to the previous step, not this final order.
    if (order.get("step_id") == "final_evidence" and verified_prior
            and execution.get("phase") == "completed"
            and execution.get("step_id") == verified_prior[-1].get("step_id") == "review"
            and execution.get("attempt_id") == verified_prior[-1].get("attempt_id")
            and execution.get("adapter_request_digest") == verified_prior[-1].get("adapter_request_digest")):
        run = {k: v for k, v in run.items() if k != "provider_execution"}
    return run_lifecycle.provider_claim_is_live(run, order)


def verify_readonly_final_inputs(state_root: Path, *, run: dict[str, Any],
                                 template: dict[str, Any]) -> dict[str, Any]:
    """Reverify the signed artifact chain under the caller's global lock.

    Artifact checks are shared with the report gate and completion validation;
    producer-only queued-state checks belong to the executor.
    """
    steps = _chain_contract(template, run)
    if run.get("workflow_id") != CHAIN_ID or run.get("current_step") != "final_evidence":
        raise ReportGateError("unsupported_step_contract")
    prior = _chain_prior_acceptances(state_root, run, template, steps)
    if [p["step_id"] for p in prior] != ["research", "review"]:
        raise ReportGateError("prior_acceptance_invalid")
    for step, accepted in zip(steps, prior):
        sid = step["id"]
        order, _ = _chain_order_binding(state_root, run, template, step, accepted["iteration"])
        report = read_json(Path(accepted["report_path"]))
        expected = "findings" if sid == "research" else "pass"
        if report.get("result") != expected or accepted.get("result") != expected:
            raise ReportGateError("prior_result_not_passed")
        errors = _chain_report_errors(state_root, run, order, report, sid)
        if errors:
            raise ReportGateError("prior_report_invalid: " + "; ".join(errors))
    if type(run.get("iteration")) is not int or run["iteration"] < 1:
        raise ReportGateError("step_attempt_mismatch")
    order, bindings = _chain_order_binding(state_root, run, template, steps[-1], run["iteration"])
    if _chain_claim_is_live(run, order, prior):
        raise ReportGateError("provider_in_flight")
    return {"prior_acceptances": prior, "work_order_binding": bindings,
            "report_path": str(report_path(state_root, run["run_id"], "final_evidence"))}


def _chain_rejection(state_root: Path, run: dict[str, Any], actor: dict[str, Any], reason: str) -> dict[str, Any]:
    artifact = write_rejection_artifact(state_root=state_root, run_id=run["run_id"], step_id=run["current_step"],
        payload={"rejection_version": "1", "run_id": run["run_id"], "step_id": run["current_step"],
                 "outcome": reason, "errors": [reason], "occurred_at": now_iso(),
                 "principal": run_lifecycle.redacted_principal(actor)})
    return {"schema_version": 1, "decision": "blocked", "validated": False, "reason": reason.split(":", 1)[0],
            "outcome": reason.split(":", 1)[0], "errors": [reason], "workflow_run": run,
            "run_path": str(run_store.run_path(state_root, run["run_id"])),
            "transition_artifact_path": None, "rejection_artifact_path": str(artifact)}


def _gate_chain_report(state_root: Path, run: dict[str, Any], actor: dict[str, Any], report_path_arg: str) -> dict[str, Any]:
    """Called only under the existing global gate lock; persist one accepted tree."""
    try:
        template = workflow_selector.load_template(CHAIN_ID)
        steps = _chain_contract(template, run)
        sid = run["current_step"]
        if sid not in CHAIN_CONTRACTS:
            raise ReportGateError("step_report_mismatch")
        path = report_path(state_root, run["run_id"], sid)
        submitted = confined_state_artifact(state_root, report_path_arg or path, namespace="reports", label="report path")
        prior = _chain_prior_acceptances(state_root, run, template, steps)
        accepted_ids = [r["step_id"] for r in prior]
        if str(submitted) in [a["report_path"] for a in prior] or sid in accepted_ids:
            raise ReportGateError("duplicate_step_report")
        if submitted != path:
            if submitted in [report_path(state_root, run["run_id"], s["id"]) for s in steps]:
                raise ReportGateError("out_of_order_report")
            raise ReportGateError("step_report_mismatch")
        if accepted_ids != [s["id"] for s in steps[:list(CHAIN_CONTRACTS).index(sid)]]:
            raise ReportGateError("prior_acceptance_invalid")
        if run["run_state"] not in {"step_queued", "waiting_provider", "validating"}:
            raise ReportGateError("out_of_order_report")
        if type(run.get("iteration")) is not int or run["iteration"] < 1:
            raise ReportGateError("step_attempt_mismatch")
        step = steps[list(CHAIN_CONTRACTS).index(sid)]
        order, bindings = _chain_order_binding(state_root, run, template, step, run["iteration"])
        execution = run.get("provider_execution")
        if sid == "final_evidence":
            claim_live = _chain_claim_is_live(run, order, prior)
        else:
            claim_live = (not isinstance(execution, dict) or execution.get("step_id") == sid) and run_lifecycle.provider_claim_is_live(run, order)
        if claim_live:
            raise ReportGateError("provider_in_flight")
        report = read_json(path)
        if report.get("step_id") != sid or report.get("workflow_id") != CHAIN_ID:
            if report.get("step_id") in accepted_ids:
                raise ReportGateError("duplicate_step_report")
            raise ReportGateError("step_report_mismatch")
        _, schema_name, success_event, failure_event = CHAIN_CONTRACTS[sid]
        errors = _chain_report_errors(state_root, run, order, report, sid)
        if sid != "final_evidence":
            bindings.update(_chain_provider_binding(state_root, run, order, bindings))
        else:
            require_existing_signing_key(state_root, actor)
            verify_readonly_final_inputs(state_root, run=run, template=template)
        if sid == "final_evidence":
            if len(prior) != 2:
                errors.append("prior_acceptance_invalid")
            else:
                for name, value in zip(("research_report_ref", "review_report_ref"), prior):
                    if report.get(name) != value["report_path"]:
                        errors.append("prior_report_ref_mismatch")
                refs = report.get("evidence_refs")
                if not isinstance(refs, list) or set(refs) != {p["report_path"] for p in prior}:
                    errors.append("final_evidence_refs_mismatch")
                if prior[0].get("result") != "findings" or prior[1].get("result") != "pass":
                    errors.append("prior_result_not_passed")
            if report.get("review_status") != "pass" or report.get("validation_status") != "passed":
                errors.append("final_status_not_passed")
        valid = not errors and report.get("result") == {"research": "findings", "review": "pass", "final_evidence": "complete"}[sid]
        event = success_event if valid else failure_event
        budget = run.get("activation", {}).get("activation_scope", {}).get("step_budget")
        if type(run.get("max_steps")) is not int or run["max_steps"] != template.get("max_steps"):
            raise ReportGateError("step_budget_exceeded")
        target = resolve_step_transition(template, sid, event, accepted_steps=accepted_ids, step_budget=budget)
        if valid and target["to"] == "waiting_human":
            raise ReportGateError("unsupported_success_waiting_transition")
        # Invalid reports may block/wait according to the template, never advance.
        if not valid and target["to"] not in {"blocked", "waiting_human"}:
            raise ReportGateError("invalid_report_transition")
        original_state = run["run_state"]
        acceptance = {"run_id": run["run_id"], "request_id": run["request_id"], "step_id": sid,
                      "iteration": run["iteration"], "source_contract": _chain_source_contract(template),
                      "on": event, "from_step": sid, "to_step": target["to"], "result": report.get("result"),
                      "report_path": str(path), "report_sha256": _chain_digest(path), **bindings,
                      "principal": run_lifecycle.redacted_principal(actor)}
        acceptance["signature"] = run_lifecycle.sign_transition(state_root=state_root, principal=actor,
            transition="report_gate_acceptance", subject=acceptance)
        # Prepare legal lifecycle transitions in memory, then store all state,
        # history and signed report bindings together with the original state CAS.
        if run["run_state"] == "step_queued":
            run_lifecycle.transition_run(state_root, run["run_id"], to_state="waiting_provider", reason_class="report_received",
                transition="validate_report", principal=actor, run=run, persist=False)
        if run["run_state"] == "waiting_provider":
            run_lifecycle.transition_run(state_root, run["run_id"], to_state="validating", reason_class="report_received",
                transition="validate_report", principal=actor, run=run, persist=False)
        next_state = target["to"] if target["to"] in {"complete", "waiting_human"} else "failed" if target["to"] == "blocked" else "step_queued"
        if valid:
            run["step_history"].append({"step_id": sid, "status": "accepted", "acceptance": acceptance})
        if next_state == "step_queued":
            run["current_step"] = target["to"]
        record = run_lifecycle.transition_run(state_root, run["run_id"], to_state=next_state,
            reason_class=event, transition="validate_report", principal=actor, run=run,
            expected_current_state=original_state, report_binding=acceptance, artifact_refs=[str(path)],
            terminal_status="blocked" if next_state == "failed" else None, persist=False)
        artifact = write_transition_artifact(state_root=state_root, run_id=run["run_id"],
            payload={"transition_artifact_version": "1", "gate": "report_gate", **record})
        # An orphan artifact after a failed run CAS is not an acceptance. The
        # canonical run is the sole commit point for history and step state.
        run_store.store_run(state_root, run, expected_current_state=original_state)
        return {"schema_version": 1, "decision": "ok" if valid else "blocked", "validated": True,
                "outcome": event, "reason": event, "errors": errors, "report_status": next_state,
                "workflow_run": run, "run_path": str(run_store.run_path(state_root, run["run_id"])),
                "transition_artifact_path": str(artifact), "rejection_artifact_path": None,
                "next_action": "drain" if next_state == "step_queued" else None}
    except (ReportGateError, run_store.RunStoreError, work_order_builder.WorkOrderError,
            run_lifecycle.LifecycleError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        return _chain_rejection(state_root, run_store.load_run(state_root, run["run_id"]), actor,
                                str(exc) if isinstance(exc, ReportGateError) else "step_contract_invalid: " + type(exc).__name__)


def gate_report(
    state_root: Path,
    run_id: str,
    *,
    report_path_arg: str = "",
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = run_store.validate_artifact_id(run_id, "run_id")
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
            if run.get("workflow_id") == CHAIN_ID:
                result = _gate_chain_report(state_root, run, actor, report_path_arg)
            elif run.get("workflow_id") not in {"single_step_external_review", "standard_code_change"}:
                result = _chain_rejection(state_root, run, actor, "unsupported_step_contract")
            else:
                result = None
            if result is not None:
                link_status = record_run_link_status(state_root, result["workflow_run"])
                append_audit_event(state_root=state_root, event_type="validate_report", principal=actor,
                    subject={"run_id": run_id, "request_id": run["request_id"]},
                    outcome=result["decision"], details={"reason": result["reason"], "run_link": link_status,
                        "transition_artifact_path": result["transition_artifact_path"],
                        "rejection_artifact_path": result["rejection_artifact_path"]})
                return result
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
            if run.get('workflow_id') == 'standard_code_change' and run.get('current_step') == 'final_evidence':
                return finalize_standard_review(state_root, run, principal=actor)
            step_id = run_store.validate_artifact_id(str(run["current_step"]), "step_id")
            work_order_file = work_order_path(state_root, run_id, step_id)
            work_order = read_json(work_order_file)
            expected_report_path = report_path(state_root, run_id, step_id)
            canonical_report_path = confined_state_artifact(
                state_root,
                str(work_order["report_path"]),
                namespace="reports",
                label="canonical report path",
            )
            if canonical_report_path != expected_report_path:
                raise ReportGateError("work order report path must match canonical report artifact")
            path = (
                confined_state_artifact(
                    state_root,
                    report_path_arg,
                    namespace="reports",
                    label="report path",
                )
                if report_path_arg
                else canonical_report_path
            )
            if path != canonical_report_path:
                raise ReportGateError("report path must match canonical work order report path")

            if run_state == "waiting_provider" and run_lifecycle.provider_claim_is_live(run, work_order):
                append_audit_event(
                    state_root=state_root,
                    event_type="validate_report",
                    principal=actor,
                    subject=subject,
                    outcome="blocked",
                    details={"reason": "provider_in_flight", "run_state": run_state},
                )
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "validated": False,
                    "report_status": "waiting_provider",
                    "reason": "provider_in_flight",
                    "errors": [],
                    "outcome": "provider_in_flight",
                    "transition_artifact_path": None,
                    "rejection_artifact_path": None,
                    "run_path": str(run_file),
                    "workflow_run": run,
                }

            report_read_outcome: str | None = None
            report_read_error: str | None = None
            report_digest: str | None = None
            try:
                raw_report = run_store.read_bytes(path)
            except run_store.RunStoreError:
                report = {}
                report_read_outcome = "report_not_written"
                report_read_error = "report file is unavailable"
            else:
                report_digest = "sha256:" + hashlib.sha256(raw_report).hexdigest()
                try:
                    report = json.loads(raw_report.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    report = {}
                    report_read_outcome = "report_invalid"
                    report_read_error = f"report unreadable: {type(exc).__name__}"
                else:
                    if not isinstance(report, dict):
                        report = {}
                        report_read_outcome = "report_invalid"
                        report_read_error = "report must be object"

            if run_state == "step_queued" and report_read_outcome != "report_not_written":
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
            if run_state == "waiting_provider" and report_read_outcome != "report_not_written":
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

            if report_read_outcome is not None:
                outcome, errors = report_read_outcome, [str(report_read_error)]
            else:
                outcome, errors = classify_report_outcome(report, run=run, work_order=work_order, state_root=state_root)
            if outcome == 'report_valid' and run.get('workflow_id') == 'standard_code_change':
                return consume_standard_report(state_root, run, report, work_order=work_order, principal=actor)
            if outcome == 'report_valid':
                review_lifecycle.start_from_gated_findings(run, report, work_order=work_order, principal=actor)
            if outcome == "report_valid" and run.get('review_lifecycle', {}).get('resolution_flow'):
                review_lifecycle._live(run, review_lifecycle._owner(actor), run['review_lifecycle'])
                # Preserve the accepted source before the canonical report file is
                # reused by the bounded verification. No terminal or human-wait transition.
                report_digest = 'sha256:' + stable_digest(report)
                sealed_path = path.parent / ('review-record-' + report_digest.removeprefix('sha256:') + '.json')
                if sealed_path.exists():
                    if read_json(sealed_path) != report:
                        raise ReportGateError('sealed_review_conflict')
                else:
                    run_store.atomic_write_json(sealed_path, report)
                review_lifecycle.consume_gated_report(run, report, work_order=work_order,
                    report_ref=str(sealed_path), digest=report_digest)
                run_store.store_run(state_root, run, expected_current_state=run['run_state'])
                action = review_lifecycle.next_review_action(run)
                append_audit_event(state_root=state_root, event_type='validate_report', principal=actor,
                    subject=subject, outcome='ok', details={'report_digest': report_digest,
                    'report_ref': str(sealed_path), 'next_action': action, 'signature': signature})
                return {'schema_version': 1, 'decision': 'blocked' if action == 'stopped' else 'ok',
                        'validated': True, 'outcome': 'report_valid', 'report_status': 'review_flow',
                        'next_action': action, 'workflow_run': run, 'report': report,
                        'report_ref': str(sealed_path), 'run_path': str(run_file)}
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
            elif outcome == PROVIDER_MODEL_ASSURANCE_MISMATCH:
                to_state = "failed"
                report_status = "blocked"
                terminal_status = "blocked"
                terminal_reason = PROVIDER_MODEL_ASSURANCE_MISMATCH
                reason_class = PROVIDER_MODEL_ASSURANCE_MISMATCH
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
            elif outcome == "report_not_written":
                to_state = "waiting_human"
                report_status = "waiting_human"
                terminal_status = None
                terminal_reason = None
                reason_class = "report_not_written"
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
            evidence_path = (
                report.get("provider_evidence", {}).get("evidence_path")
                if isinstance(report.get("provider_evidence"), dict)
                else None
            )
            evidence_sha256 = None
            if isinstance(evidence_path, str) and evidence_path:
                try:
                    digest_path = confined_state_artifact(
                        state_root,
                        evidence_path,
                        namespace="provider-evidence",
                        label="provider_evidence.evidence_path",
                    )
                    expected_digest_path = provider_evidence_path(
                        state_root,
                        run_id,
                        step_id,
                    )
                    if digest_path == expected_digest_path:
                        evidence_sha256 = file_sha256(digest_path)
                except (ReportGateError, run_store.RunStoreError):
                    evidence_sha256 = None
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
                "evidence_sha256": evidence_sha256,
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
            if outcome in {
                "report_invalid",
                "scope_violation",
                PROVIDER_MODEL_ASSURANCE_MISMATCH,
            }:
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



def standard_review_view(report: dict[str, Any]) -> dict[str, Any]:
    """Normalize structured findings, never normalize a provider failure to success."""
    return dict(report_id=report['report_id'], provider_evidence=report['provider_evidence'],
        result=('blocked' if report['result'] == 'blocked' else 'pass' if 'resolution' in report
                or report['review']['status'] == 'approved' and not report['review'].get('findings') else 'findings'),
        findings=report['review'].get('findings', []),
        **({'resolution': report['resolution']} if 'resolution' in report else {}))


def validate_standard_review_report(report: dict[str, Any], *, run: dict[str, Any],
                                    work_order: dict[str, Any], state_root: Path) -> list[str]:
    import scoped_worker_executor
    schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/code-change-report.schema.json').read_text())
    errors = work_order_builder._validate_schema_fragment(report, schema, '$')
    for key in ('report_id', 'request_id', 'run_id', 'step_id', 'provider_evidence', 'authority'):
        if key not in report:
            errors.append('missing_required_field:' + key)
    for key in ('request_id', 'run_id', 'workflow_id'):
        if report.get(key) != run.get(key):
            errors.append(key + '_mismatch')
    if report.get('step_id') != run['current_step'] or run['current_step'] not in {'review', 'qa'}:
        errors.append('standard_report_step_mismatch')
    try:
        frozen, _, _ = scoped_worker_executor.verify_frozen_work_order(state_root,
            run_id=run['run_id'], step_id=run['current_step'], expected_run_states={'validating', 'waiting_provider'},
            expected_iteration=run['iteration'])
        if frozen != work_order:
            errors.append('standard_frozen_order_mismatch')
        refs = scoped_worker_executor.completed_review_context_refs(state_root, run)
        expected = [dict(type='repo_file', value=row['path'], size_bytes=row['size_bytes'], digest=row['digest']) for row in refs]
        if work_order['context_refs'] != expected:
            errors.append('standard_context_stale')
    except (scoped_worker_executor.ScopedWorkerError, KeyError, TypeError) as exc:
        errors.append('standard_authority_invalid:' + str(exc))
    errors.extend(validate_provider_evidence(report.get('provider_evidence'), run, work_order, state_root))
    errors.extend(validate_normalized_provider_evidence_file(report, run=run, work_order=work_order, state_root=state_root))
    errors.extend(validate_authority(report.get('authority')))
    if errors or report.get('result') == 'blocked':
        return errors
    candidate = copy.deepcopy(run)
    try:
        if run['current_step'] == 'review':
            if report['review']['status'] not in {'approved', 'changes_requested'}:
                raise review_lifecycle.ReviewLifecycleError('review_blocked')
            view = standard_review_view(report)
            errors.extend(validate_findings(view['findings'], view['result']))
            review_lifecycle.start_from_gated_findings(candidate, view, work_order=work_order,
                principal=work_order['work_order_authority']['issuer_principal'])
            review_lifecycle.consume_gated_report(candidate, view, work_order=work_order,
                report_ref='reports/validated-standard.json', digest='sha256:' + stable_digest(report))
            errors.extend(review_lifecycle.validate_record(candidate['review_lifecycle'], run=candidate))
        elif report['validation']['status'] != 'passed' or review_lifecycle.next_review_action(run) != 'merge_preflight':
            errors.append('required_validation_not_passed')
        if run['current_step'] == 'qa' and (report['review']['status'] != 'approved' or report['review'].get('findings')):
            errors.append('qa_blocking_findings')
        if run['current_step'] == 'qa' and 'resolution' in report:
            errors.append('qa_cannot_replace_review')
    except (review_lifecycle.ReviewLifecycleError, KeyError, TypeError) as exc:
        errors.append('standard_review_invalid:' + str(exc))
    return errors


def consume_standard_report(state_root: Path, run: dict[str, Any], report: dict[str, Any], *,
                            work_order: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
    digest = 'sha256:' + stable_digest(report)
    archive = report_path(state_root, run['run_id'], run['current_step']).parent / ('review-record-' + digest[7:] + '.json')
    if archive.exists() and read_json(archive) != report:
        raise ReportGateError('sealed_review_conflict')
    if not archive.exists():
        run_store.atomic_write_json(archive, report)
    if run['current_step'] == 'review':
        view = standard_review_view(report)
        receipt_ref, receipt_digest = seal_provider_receipt(state_root, run, work_order, digest)
        view['provider_evidence'] = dict(view['provider_evidence'], host_receipt_ref=receipt_ref, host_receipt_digest=receipt_digest)
        review_lifecycle.start_from_gated_findings(run, view, work_order=work_order, principal=principal)
        review_lifecycle._live(run, review_lifecycle._owner(principal), run['review_lifecycle'])
        review_lifecycle.consume_gated_report(run, view, work_order=work_order, report_ref=str(archive), digest=digest)
        action = review_lifecycle.next_review_action(run)
        if action == 'repair_original_findings':
            review_lifecycle.reserve_followup_repair(run)
            step = 'implement'
        elif action == 'merge_preflight':
            step = 'qa'
        else:
            run_store.store_run(state_root, run, expected_current_state=run['run_state'])
            return {'schema_version':1, 'decision':'blocked', 'reason':action, 'workflow_run':run}
    else:
        seal_provider_receipt(state_root, run, work_order, digest)
        run['review_lifecycle']['resolution_flow']['qa'] = dict(report_ref=str(archive), digest=digest,
            snapshot=copy.deepcopy(run['review_lifecycle']['snapshot']))
        step = 'final_evidence'
    run['step_history'].append(dict(step_id=run['current_step'], status='complete', report_path=str(archive), report_digest=digest))
    transition = run_lifecycle.queue_standard_step(state_root, run, step=step, principal=principal, artifact_refs=[str(archive)])
    blocked = run['run_state'] == 'waiting_human'
    return {'schema_version':1, 'decision':'blocked' if blocked else 'ok', 'validated':True,
            'outcome':'report_valid', 'next_action':'blocked' if blocked else 'drain',
            'review_action':review_lifecycle.next_review_action(run), 'transition':transition, 'workflow_run':run}


def finalize_standard_review(state_root: Path, run: dict[str, Any], *, principal: dict[str, Any]) -> dict[str, Any]:
    import scoped_worker_executor
    scoped_worker_executor.verify_frozen_work_order(state_root, run_id=run['run_id'], step_id='final_evidence',
        expected_run_states={'step_queued'}, expected_iteration=run['iteration'])
    scoped_worker_executor.load_completed_review_context(state_root, run)
    state = run.get('review_lifecycle', {})
    review_lifecycle._live(run, review_lifecycle._owner(principal), state)
    qa = state.get('resolution_flow', {}).get('qa')
    if not qa or qa['snapshot'] != state['snapshot'] or review_lifecycle.next_review_action(run) != 'merge_preflight':
        raise ReportGateError('final_validation_evidence_missing')
    path = confined_state_artifact(state_root, qa['report_ref'], namespace='reports', label='qa report')
    report = read_json(path)
    if 'sha256:' + stable_digest(report) != qa['digest'] or report['validation']['status'] != 'passed':
        raise ReportGateError('final_validation_evidence_invalid')
    # The final step is a harness gate, not another model review.
    run_lifecycle.transition_run(state_root, run['run_id'], to_state='validating',
        reason_class='final_evidence_ready', transition='finalize_standard_review', principal=principal, run=run, persist=False)
    transition = run_lifecycle.transition_run(state_root, run['run_id'], to_state='complete',
        reason_class='standard_review_flow_complete', transition='finalize_standard_review', principal=principal,
        artifact_refs=[str(path)], run=run, persist=False)
    run_store.store_run(state_root, run, expected_current_state='step_queued')
    return {'schema_version':1, 'decision':'ok', 'validated':True, 'outcome':'report_valid',
            'next_action':'complete', 'transition':transition, 'workflow_run':run}



def seal_provider_receipt(state_root: Path, run: dict[str, Any], work_order: dict[str, Any],
                          report_digest: str) -> tuple[str, str]:
    """Retain provider provenance before step files are reused by verification."""
    run_id, step = run['run_id'], work_order['step_id']
    evidence = read_json(provider_evidence_path(state_root, run_id, step))
    request_file = state_paths(state_root)['adapter_requests'] / run_id / (step + '-' + work_order['provider_adapter_id'] + '.json')
    request = read_json(confined_state_artifact(state_root, request_file, namespace='adapter-requests', label='provider request'))
    transcript_source = confined_state_artifact(state_root, evidence['transcript_path'], namespace='provider-evidence', label='transcript')
    transcript = read_json(transcript_source)
    saved_transcript = provider_evidence_path(state_root, run_id, step).parent / (step + '-' + report_digest[7:] + '-transcript.json')
    if saved_transcript.exists() and read_json(saved_transcript) != transcript:
        raise ReportGateError('sealed_transcript_conflict')
    if not saved_transcript.exists():
        run_store.atomic_write_json(saved_transcript, transcript)
    receipt = dict(report_digest=report_digest, work_order=work_order, adapter_request=request,
                   normalized_evidence=evidence, transcript_ref=str(saved_transcript),
                   transcript_digest='sha256:' + stable_digest(transcript))
    path = report_path(state_root, run_id, step).parent / ('review-record-' + report_digest[7:] + '-receipt.json')
    if path.exists() and read_json(path) != receipt:
        raise ReportGateError('sealed_provider_receipt_conflict')
    if not path.exists():
        run_store.atomic_write_json(path, receipt)
    return str(path), 'sha256:' + stable_digest(receipt)
