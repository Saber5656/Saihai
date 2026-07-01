#!/usr/bin/env python3
"""Host-owned frontdoor and P0 harness for deterministic workflow control."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import workflow_selector

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_STATE_ROOT = Path.home() / ".codex" / "state" / "itb" / "frontdoor-orchestrator"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def load_json_path(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_json_arg(raw: str) -> Any:
    return workflow_selector.load_json_arg(raw)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = load_json_path(path)
    except OSError as exc:
        raise FrontdoorError(f"missing file: {path}") from exc
    if not isinstance(data, dict):
        raise FrontdoorError(f"expected object json: {path}")
    return data


class FrontdoorError(RuntimeError):
    pass


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "requests": state_root / "requests",
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "adapter_requests": state_root / "adapter-requests",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
    }


def request_path(state_root: Path, request_id: str) -> Path:
    return state_paths(state_root)["requests"] / f"{request_id}.json"


def run_path(state_root: Path, run_id: str) -> Path:
    return state_paths(state_root)["runs"] / f"{run_id}.json"


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return state_paths(state_root)["work_orders"] / run_id / f"{step_id}.json"


def report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return state_paths(state_root)["reports"] / run_id / f"{step_id}-external-review-report.json"


def provider_evidence_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return state_paths(state_root)["provider_evidence"] / run_id / f"{step_id}-provider-evidence.json"


def adapter_request_path(state_root: Path, run_id: str, step_id: str, adapter_id: str) -> Path:
    return state_paths(state_root)["adapter_requests"] / run_id / f"{step_id}-{adapter_id}.json"


def stable_run_id(request_id: str, workflow_id: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{workflow_id}".encode("utf-8")).hexdigest()[:12]
    return f"run-{digest}"


def policy_digest(envelope: dict[str, Any]) -> str:
    policy_material = {
        "policy": envelope.get("policy"),
        "activation_scope": envelope.get("activation_scope"),
        "context_scope": envelope.get("context_scope"),
        "workflow_selection": envelope.get("workflow_selection"),
    }
    encoded = json.dumps(policy_material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sanitize_activation_for_run(envelope: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "activation_version",
        "activation_source",
        "activation_status",
        "approved_by",
        "approved_at",
        "workflow_selection",
        "context_scope",
        "activation_scope",
        "next_action",
    }
    return {key: envelope[key] for key in allowed if key in envelope}


def load_registry() -> dict[str, Any]:
    return workflow_selector.load_registry()


def load_template(workflow_id: str) -> dict[str, Any]:
    registry = load_registry()
    for entry in registry.get("templates", []):
        if entry.get("workflow_id") != workflow_id:
            continue
        path = REPO_ROOT / entry["path"]
        return read_json(path)
    raise FrontdoorError(f"active workflow template not found: {workflow_id}")


def proposed_request(
    *,
    state_root: Path,
    task_id: str,
    request_id: str,
    user_prompt: str,
    refs: list[str],
    classification: dict[str, Any] | None,
    allowed_paths: list[str],
    expires_at: str,
    frontdoor: str,
    chat_session_id: str,
) -> dict[str, Any]:
    if classification is None:
        payload = {
            "schema_version": 1,
            "decision": "waiting_human",
            "request_status": "waiting_human",
            "reason": "typed_classification_required",
            "task_id": task_id,
            "request_id": request_id,
            "next_action": "ask_human",
        }
        record = {
            "request_version": "1",
            "task_id": task_id,
            "request_id": request_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "user_prompt": user_prompt,
            "context_refs": refs,
            "allowed_paths": allowed_paths,
            "expires_at": expires_at,
            "classification": None,
            "requester": requester(frontdoor, chat_session_id),
            "status": "waiting_human",
            "proposal": payload,
        }
        write_json(request_path(state_root, request_id), record)
        return payload

    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source="frontdoor_prompt",
        task_id=task_id,
        request_id=request_id,
        refs=refs,
        allowed_paths=allowed_paths,
        expires_at=expires_at,
    )
    record = {
        "request_version": "1",
        "task_id": task_id,
        "request_id": request_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "user_prompt": user_prompt,
        "context_refs": refs,
        "allowed_paths": allowed_paths,
        "expires_at": expires_at,
        "classification": classification,
        "requester": requester(frontdoor, chat_session_id),
        "status": envelope["activation_status"],
        "proposal": envelope,
    }
    write_json(request_path(state_root, request_id), record)
    return {
        "schema_version": 1,
        "decision": "ok",
        "request_status": envelope["activation_status"],
        "request_path": str(request_path(state_root, request_id)),
        "activation": envelope,
    }


def requester(frontdoor: str, chat_session_id: str = "") -> dict[str, str]:
    payload = {"frontdoor": frontdoor}
    if chat_session_id:
        payload["chat_session_id"] = chat_session_id
    return payload


def approve_request(
    *,
    state_root: Path,
    request_id: str,
    human_action_id: str,
) -> dict[str, Any]:
    path = request_path(state_root, request_id)
    record = read_json(path)
    classification = record.get("classification")
    if not isinstance(classification, dict):
        raise FrontdoorError("typed classification is required before approval")
    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source="human_ui",
        task_id=record["task_id"],
        request_id=record["request_id"],
        refs=list(record.get("context_refs") or []),
        allowed_paths=list(record.get("allowed_paths") or []),
        expires_at=str(record.get("expires_at") or "run_terminal"),
    )
    record["updated_at"] = now_iso()
    record["status"] = envelope["activation_status"]
    record["human_action_id"] = human_action_id
    record["approved_activation"] = envelope
    write_json(path, record)
    return {
        "schema_version": 1,
        "decision": "ok" if envelope["activation_status"] == "approved" else "blocked",
        "request_status": envelope["activation_status"],
        "request_path": str(path),
        "activation": envelope,
    }


def create_run(
    *,
    state_root: Path,
    request_id: str,
    run_id: str,
    resume_policy: str,
) -> dict[str, Any]:
    record = read_json(request_path(state_root, request_id))
    envelope = record.get("approved_activation")
    if not isinstance(envelope, dict) or envelope.get("activation_status") != "approved":
        raise FrontdoorError("approved activation envelope required")
    selection = envelope.get("workflow_selection") or {}
    workflow_id = selection.get("workflow_id")
    initial_step = selection.get("initial_step")
    if not workflow_id or not initial_step:
        raise FrontdoorError("approved activation must contain selected workflow and initial step")

    template = load_template(str(workflow_id))
    effective_run_id = run_id or stable_run_id(request_id, str(workflow_id))
    path = run_path(state_root, effective_run_id)
    if path.exists():
        return {
            "schema_version": 1,
            "decision": "ok",
            "created": False,
            "run_path": str(path),
            "workflow_run": read_json(path),
        }

    run = {
        "run_version": "1",
        "run_id": effective_run_id,
        "task_id": record["task_id"],
        "request_id": request_id,
        "workflow_id": workflow_id,
        "goal_state": "approved",
        "run_state": "created",
        "current_step": initial_step,
        "iteration": 1,
        "max_steps": int(template.get("max_steps") or 1),
        "step_history": [],
        "activation": sanitize_activation_for_run(envelope),
        "terminal": {"status": None, "reason": None},
        "requester": record.get("requester") or requester("manual"),
        "scheduling": {
            "scheduler_mode": "invocation-drain",
            "concurrency_group": "global",
            "state_persistence": "durable_state",
            "lock_policy": "global_advisory_lock",
            "concurrency": 1,
            "resume_policy": resume_policy,
        },
        "context_sharing": {
            "shared_run_state": "typed_durable_state",
            "step_local_snapshot": "immutable_step_attempt_snapshot",
            "provider_transcript": "confined_evidence_path_only",
        },
    }
    write_json(path, run)
    return {
        "schema_version": 1,
        "decision": "ok",
        "created": True,
        "run_path": str(path),
        "workflow_run": run,
    }


def drain_run(*, state_root: Path, run_id: str) -> dict[str, Any]:
    path = run_path(state_root, run_id)
    run = read_json(path)
    if run.get("run_state") not in {"created", "step_queued"}:
        return {
            "schema_version": 1,
            "decision": "ok",
            "drained": False,
            "reason": "run_state_not_queueable",
            "workflow_run": run,
        }

    template = load_template(str(run["workflow_id"]))
    step = next((item for item in template.get("steps", []) if item.get("id") == run["current_step"]), None)
    if not isinstance(step, dict):
        raise FrontdoorError(f"step not found in template: {run['current_step']}")

    order_path = work_order_path(state_root, run_id, str(step["id"]))
    if order_path.exists():
        work_order = read_json(order_path)
        drained = False
    else:
        request_record = read_json(request_path(state_root, str(run["request_id"])))
        work_order = build_work_order(
            state_root=state_root,
            run=run,
            request_record=request_record,
            template=template,
            step=step,
        )
        write_json(order_path, work_order)
        drained = True

    if run["run_state"] == "created":
        run["goal_state"] = "active"
        run["run_state"] = "step_queued"
        run["step_history"].append(
            {
                "step_id": step["id"],
                "status": "queued",
                "queued_at": now_iso(),
                "work_order_path": str(order_path),
            }
        )
        write_json(path, run)

    return {
        "schema_version": 1,
        "decision": "ok",
        "drained": drained,
        "run_path": str(path),
        "work_order_path": str(order_path),
        "workflow_run": run,
        "work_order": work_order,
    }


def claude_headless_capability() -> dict[str, Any]:
    return {
        "adapter_contract_version": "1",
        "provider_adapter_id": "claude_headless_p0",
        "transport": "headless_cli",
        "sync_mode": "sync",
        "context_freshness": "fresh_process",
        "concurrency_unit": "process",
        "permission_enforcement": "harness",
        "supports_structured_output": True,
        "requires_marker": False,
        "reset_strategy": "new_session",
        "report_authority": "typed_report_and_evidence_file",
    }


def prepare_claude_adapter(*, state_root: Path, run_id: str) -> dict[str, Any]:
    run = read_json(run_path(state_root, run_id))
    step_id = str(run["current_step"])
    order_path = work_order_path(state_root, run_id, step_id)
    work_order = read_json(order_path)
    errors = validate_work_order_for_adapter(work_order)
    if errors:
        return {
            "schema_version": 1,
            "decision": "blocked",
            "reason": "work_order_not_adapter_safe",
            "errors": errors,
        }

    capability = claude_headless_capability()
    evidence_path = provider_evidence_path(state_root, run_id, step_id)
    transcript_path = state_paths(state_root)["provider_evidence"] / run_id / f"{step_id}-claude-transcript.json"
    prompt = bounded_claude_prompt(work_order, evidence_path=evidence_path, transcript_path=transcript_path)
    adapter_request = {
        "adapter_request_version": "1",
        "adapter": capability,
        "run_id": run_id,
        "request_id": run["request_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "work_order_path": str(order_path),
        "report_path": work_order["report_path"],
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "prompt": prompt,
        "authority": {
            "provider_may_write": ["typed_report_file", "normalized_provider_evidence_file"],
            "provider_must_not": ["select_workflow", "approve_activation", "mutate_run_state", "edit_repo", "commit", "push"],
        },
    }
    path = adapter_request_path(state_root, run_id, step_id, capability["provider_adapter_id"])
    write_json(path, adapter_request)
    return {
        "schema_version": 1,
        "decision": "ok",
        "adapter_request_path": str(path),
        "adapter_request": adapter_request,
    }


def validate_work_order_for_adapter(work_order: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if work_order.get("workflow_id") != "single_step_external_review":
        errors.append("only single_step_external_review is supported")
    if work_order.get("step_id") != "review":
        errors.append("only review step is supported")
    if work_order.get("permission_mode") != "readonly":
        errors.append("permission_mode must be readonly")
    allowed_ops = ((work_order.get("activation_scope") or {}).get("allowed_ops") or {})
    for op in ("edit", "commit", "push", "network"):
        if allowed_ops.get(op) is not False:
            errors.append(f"activation_scope.allowed_ops.{op} must be false")
    context_scope = work_order.get("context_scope") or {}
    if context_scope.get("raw_transcript_sharing") != "forbidden":
        errors.append("raw transcript sharing must be forbidden")
    if not work_order.get("context_refs"):
        errors.append("context_refs must be non-empty")
    return errors


def bounded_claude_prompt(work_order: dict[str, Any], *, evidence_path: Path, transcript_path: Path) -> str:
    refs = "\n".join(f"- {item.get('value', item)}" for item in work_order.get("context_refs", []))
    return "\n".join(
        [
            "You are the bounded reviewer for a deterministic P0 orchestrator work order.",
            "Do not select workflows, approve activation, mutate run state, edit files, commit, push, or publish.",
            "Use only the bounded context refs listed below. Do not request or infer raw transcript sharing.",
            "",
            f"task_id: {work_order['task_id']}",
            f"request_id: {work_order['request_id']}",
            f"run_id: {work_order['run_id']}",
            f"workflow_id: {work_order['workflow_id']}",
            f"step_id: {work_order['step_id']}",
            "",
            "Context refs:",
            refs,
            "",
            "Instruction:",
            work_order["instruction"],
            "",
            "Return only an External Review Report JSON object matching",
            "organization/runtime/workflows/schemas/external-review-report.schema.json.",
            f"Set provider_evidence.evidence_path to: {evidence_path}",
            f"Set provider_evidence.transcript_path to: {transcript_path}",
            f"The canonical report path is: {work_order['report_path']}",
        ]
    )


def validate_report(*, state_root: Path, run_id: str, report_path_arg: str = "") -> dict[str, Any]:
    run_file = run_path(state_root, run_id)
    run = read_json(run_file)
    step_id = str(run["current_step"])
    work_order = read_json(work_order_path(state_root, run_id, step_id))
    path = Path(report_path_arg).expanduser() if report_path_arg else Path(work_order["report_path"]).expanduser()
    report = read_json(path)
    errors = validate_external_review_report(report, run=run, work_order=work_order)
    if errors:
        run["run_state"] = "failed"
        run["goal_state"] = "blocked"
        run["terminal"] = {"status": "blocked", "reason": "invalid_report"}
        run["step_history"].append(
            {
                "step_id": step_id,
                "status": "blocked",
                "checked_at": now_iso(),
                "report_path": str(path),
                "errors": errors,
            }
        )
        write_json(run_file, run)
        return {
            "schema_version": 1,
            "decision": "blocked",
            "reason": "invalid_report",
            "errors": errors,
            "workflow_run": run,
        }

    result = report["result"]
    if result in {"pass", "findings"}:
        run["run_state"] = "complete"
        run["goal_state"] = "complete"
        terminal_status = "complete"
        terminal_reason = "report_valid"
    else:
        run["run_state"] = "failed"
        run["goal_state"] = "blocked"
        terminal_status = "blocked"
        terminal_reason = f"provider_report_{result}"

    run["terminal"] = {"status": terminal_status, "reason": terminal_reason}
    run["step_history"].append(
        {
            "step_id": step_id,
            "status": terminal_status,
            "checked_at": now_iso(),
            "report_path": str(path),
            "result": result,
        }
    )
    write_json(run_file, run)
    return {
        "schema_version": 1,
        "decision": "ok",
        "report_status": terminal_status,
        "run_path": str(run_file),
        "workflow_run": run,
        "report": report,
    }


def validate_external_review_report(
    report: dict[str, Any],
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
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

    errors.extend(validate_provider_evidence(report.get("provider_evidence"), run))
    errors.extend(validate_findings(report.get("findings"), report.get("result")))
    errors.extend(validate_authority(report.get("authority")))
    return errors


def validate_provider_evidence(value: Any, run: dict[str, Any]) -> list[str]:
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
    for field in ("transcript_path", "evidence_path"):
        path = Path(str(value.get(field, ""))).expanduser()
        if value.get(field) and not path.exists():
            errors.append(f"provider_evidence.{field} does not exist: {path}")
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
        if not isinstance(finding.get("evidence_refs"), list):
            errors.append(f"findings[{index}].evidence_refs must be array")
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


def build_work_order(
    *,
    state_root: Path,
    run: dict[str, Any],
    request_record: dict[str, Any],
    template: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    refs = run["activation"]["context_scope"]["refs"]
    step_id = str(step["id"])
    return {
        "work_order_version": "1",
        "task_id": run["task_id"],
        "request_id": run["request_id"],
        "run_id": run["run_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "from_role": "frontdoor",
        "to_role": str(step["role"]),
        "assignment_role": str(step["assignment_role"]),
        "instruction": (
            "Perform the bounded readonly external review described by the user request. "
            f"User request: {request_record.get('user_prompt', '')}"
        ).strip(),
        "expected_output": str(step["output_contract"]),
        "context_refs": [{"type": "ref", "value": ref} for ref in refs],
        "context_scope": {
            "mode": str(request_record.get("classification", {}).get("context_scope") or "refs_only"),
            "raw_transcript_sharing": "forbidden",
        },
        "permission_mode": str(step["permission_mode"]),
        "external_provider_allowed": True,
        "report_path": str(report_path(state_root, str(run["run_id"]), step_id)),
        "policy_digest": policy_digest(request_record["approved_activation"]),
        "requester": run.get("requester") or requester("manual"),
        "activation_scope": run["activation"]["activation_scope"],
    }


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Host-owned P0 frontdoor orchestrator")
    parser.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)

    propose = sub.add_parser("propose")
    propose.add_argument("--task-id", required=True)
    propose.add_argument("--request-id", required=True)
    propose.add_argument("--prompt", default="")
    propose.add_argument("--classification", default="")
    propose.add_argument("--ref", action="append", default=[])
    propose.add_argument("--allowed-path", action="append", default=[])
    propose.add_argument("--expires-at", default="run_terminal")
    propose.add_argument("--frontdoor", choices=["codex", "claude", "manual"], default="codex")
    propose.add_argument("--chat-session-id", default="")

    approve = sub.add_parser("approve")
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--human-action-id", required=True)

    create = sub.add_parser("create-run")
    create.add_argument("--request-id", required=True)
    create.add_argument("--run-id", default="")
    create.add_argument("--resume-policy", choices=["manual", "daemon_future"], default="manual")

    drain = sub.add_parser("drain")
    drain.add_argument("--run-id", required=True)

    sub.add_parser("adapter-capability")

    adapter = sub.add_parser("prepare-claude-adapter")
    adapter.add_argument("--run-id", required=True)

    report = sub.add_parser("validate-report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--report-path", default="")
    return parser


def main() -> None:
    args = parser().parse_args()
    state_root = Path(args.state_root).expanduser()
    try:
        if args.command == "propose":
            classification = load_json_arg(args.classification) if args.classification else None
            payload = proposed_request(
                state_root=state_root,
                task_id=args.task_id,
                request_id=args.request_id,
                user_prompt=args.prompt,
                refs=args.ref,
                classification=classification,
                allowed_paths=args.allowed_path,
                expires_at=args.expires_at,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
            )
        elif args.command == "approve":
            payload = approve_request(
                state_root=state_root,
                request_id=args.request_id,
                human_action_id=args.human_action_id,
            )
        elif args.command == "create-run":
            payload = create_run(
                state_root=state_root,
                request_id=args.request_id,
                run_id=args.run_id,
                resume_policy=args.resume_policy,
            )
        elif args.command == "drain":
            payload = drain_run(state_root=state_root, run_id=args.run_id)
        elif args.command == "adapter-capability":
            payload = {
                "schema_version": 1,
                "decision": "ok",
                "adapter": claude_headless_capability(),
            }
        elif args.command == "prepare-claude-adapter":
            payload = prepare_claude_adapter(state_root=state_root, run_id=args.run_id)
        elif args.command == "validate-report":
            payload = validate_report(
                state_root=state_root,
                run_id=args.run_id,
                report_path_arg=args.report_path,
            )
        else:
            raise FrontdoorError(f"unsupported command: {args.command}")
    except FrontdoorError as exc:
        payload = {"schema_version": 1, "decision": "blocked", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("decision") == "blocked" or payload.get("request_status") == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
