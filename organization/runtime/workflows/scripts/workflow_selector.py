#!/usr/bin/env python3
"""Deterministic workflow selection and activation envelope helpers.

P0 deliberately does not run providers, schedule workers, or create queue
messages. It validates typed classification input and emits machine-readable
selection/activation artifacts that later runner phases can consume.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY_PATH = WORKFLOW_ROOT / "registry.yaml"
SCHEMA_ROOT = WORKFLOW_ROOT / "schemas"

CLASSIFICATION_REQUIRED_FIELDS = [
    "classification_version",
    "task_kind",
    "permission_required",
    "external_provider_required",
    "publication_required",
    "security_sensitive",
    "destructive_operation",
    "context_scope",
    "expected_artifacts",
]

CLASSIFICATION_ENUMS = {
    "task_kind": {"external_review", "code_change", "research", "publication", "policy_change"},
    "permission_required": {"readonly", "edit", "full"},
    "context_scope": {"refs_only", "selected_snapshot", "diff_summary"},
    "expected_artifacts": {
        "typed_report",
        "code_diff",
        "validation_result",
        "vault_update",
        "workflow_run",
        "work_order",
    },
}

BOOLEAN_CLASSIFICATION_FIELDS = {
    "external_provider_required",
    "publication_required",
    "security_sensitive",
    "destructive_operation",
}

EXPLICIT_APPROVAL_SOURCES = {"orchestrator-start", "human_ui", "manual_cli"}
PROMPT_ONLY_SOURCES = {"frontdoor_prompt"}


def load_json_path(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_json_arg(raw: str) -> Any:
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(raw)
    candidate = Path(raw)
    if candidate.exists():
        return load_json_path(candidate)
    return json.loads(raw)


def relative_to_repo(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def validate_classification(candidate: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    missing = [field for field in CLASSIFICATION_REQUIRED_FIELDS if field not in candidate]
    if missing:
        errors.append("missing_required_fields:" + ",".join(missing))
        return False, errors

    if candidate.get("classification_version") != "1":
        errors.append("classification_version must be '1'")

    for field in BOOLEAN_CLASSIFICATION_FIELDS:
        if not isinstance(candidate.get(field), bool):
            errors.append(f"{field} must be boolean")

    for field in ("task_kind", "permission_required", "context_scope"):
        value = candidate.get(field)
        if value not in CLASSIFICATION_ENUMS[field]:
            errors.append(f"{field} unsupported: {value!r}")

    expected_artifacts = candidate.get("expected_artifacts")
    if not isinstance(expected_artifacts, list) or not expected_artifacts:
        errors.append("expected_artifacts must be a non-empty list")
    elif any(not isinstance(item, str) for item in expected_artifacts):
        errors.append("expected_artifacts entries must be strings")
    else:
        invalid = sorted(set(expected_artifacts) - CLASSIFICATION_ENUMS["expected_artifacts"])
        if invalid:
            errors.append("expected_artifacts unsupported:" + ",".join(invalid))

    return not errors, errors


def load_registry() -> dict[str, Any]:
    return load_json_path(REGISTRY_PATH)


def active_templates(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        template["workflow_id"]: template
        for template in registry.get("templates", [])
        if template.get("status") == "active"
    }


def planned_templates(registry: dict[str, Any]) -> set[str]:
    return {template["workflow_id"] for template in registry.get("planned_templates", [])}


def base_policy() -> dict[str, str]:
    return {
        "bounded_provider_transport": "not_approved",
        "destructive_operations": "not_approved",
        "publication": "not_required",
    }


def blocked_selection(
    reason: str,
    *,
    candidates: list[str] | None = None,
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    selection: dict[str, Any] = {
        "status": "blocked",
        "reason": reason,
        "candidates": candidates or [],
    }
    if missing_fields:
        selection["missing_fields"] = missing_fields
    return selection


def waiting_selection(reason: str, candidates: list[str]) -> dict[str, Any]:
    return {
        "status": "waiting_human",
        "reason": reason,
        "candidates": candidates,
    }


def selected_workflow(workflow_id: str, initial_step: str, optional_expansions: list[str]) -> dict[str, Any]:
    return {
        "status": "selected",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "optional_expansions": optional_expansions,
    }


def select_workflow(classification: dict[str, Any]) -> dict[str, Any]:
    valid, errors = validate_classification(classification)
    policy = base_policy()
    registry = load_registry()
    active = active_templates(registry)
    planned = planned_templates(registry)

    if not valid:
        missing = [
            field for field in CLASSIFICATION_REQUIRED_FIELDS if field not in classification
        ]
        return {
            "schema_version": 1,
            "decision": "blocked",
            "selector_version": "1",
            "workflow_selection": blocked_selection(
                "invalid_classification",
                missing_fields=missing,
            ),
            "classification_errors": errors,
            "policy": policy,
        }

    task_kind = classification["task_kind"]
    permission = classification["permission_required"]
    expected_artifacts = set(classification["expected_artifacts"])

    if classification["destructive_operation"]:
        policy["destructive_operations"] = "blocked"
        return {
            "schema_version": 1,
            "decision": "blocked",
            "selector_version": "1",
            "workflow_selection": blocked_selection("destructive_operation_requires_separate_approval"),
            "classification_errors": [],
            "policy": policy,
        }

    if classification["publication_required"]:
        policy["publication"] = "separate_gate_required"
        candidate = "publication_required"
        return {
            "schema_version": 1,
            "decision": "waiting_human",
            "selector_version": "1",
            "workflow_selection": waiting_selection(
                "publication_requires_separate_gate",
                [candidate],
            ),
            "classification_errors": [],
            "policy": policy,
        }

    if task_kind == "external_review" and permission == "readonly":
        if "single_step_external_review" not in active:
            return {
                "schema_version": 1,
                "decision": "blocked",
                "selector_version": "1",
                "workflow_selection": blocked_selection("active_template_missing"),
                "classification_errors": [],
                "policy": policy,
            }
        if not classification["external_provider_required"]:
            return {
                "schema_version": 1,
                "decision": "waiting_human",
                "selector_version": "1",
                "workflow_selection": waiting_selection(
                    "external_review_without_external_provider_is_unsupported_in_p0",
                    ["single_step_external_review"],
                ),
                "classification_errors": [],
                "policy": policy,
            }
        if "typed_report" not in expected_artifacts:
            return {
                "schema_version": 1,
                "decision": "blocked",
                "selector_version": "1",
                "workflow_selection": blocked_selection("typed_report_artifact_required"),
                "classification_errors": [],
                "policy": policy,
            }
        optional_expansions = ["security_focus"] if classification["security_sensitive"] else []
        policy["bounded_provider_transport"] = "allowed"
        return {
            "schema_version": 1,
            "decision": "selected",
            "selector_version": "1",
            "workflow_selection": selected_workflow(
                "single_step_external_review",
                active["single_step_external_review"]["initial_step"],
                optional_expansions,
            ),
            "classification_errors": [],
            "policy": policy,
        }

    if task_kind == "external_review" and permission != "readonly":
        return {
            "schema_version": 1,
            "decision": "blocked",
            "selector_version": "1",
            "workflow_selection": blocked_selection("external_review_must_be_readonly_in_p0"),
            "classification_errors": [],
            "policy": policy,
        }

    planned_map = {
        "code_change": "standard_code_change",
        "research": "research_only",
        "publication": "publication_required",
        "policy_change": "policy_or_permission_change",
    }
    if classification["security_sensitive"] and task_kind in {"code_change", "policy_change"}:
        candidate = "security_sensitive_change"
        return {
            "schema_version": 1,
            "decision": "waiting_human",
            "selector_version": "1",
            "workflow_selection": waiting_selection(
                "specialized_security_or_policy_template_required",
                [candidate],
            ),
            "classification_errors": [],
            "policy": policy,
        }

    candidate = planned_map.get(task_kind)
    if candidate in planned:
        reason = "planned_template_not_installed_in_p0"
        return {
            "schema_version": 1,
            "decision": "waiting_human",
            "selector_version": "1",
            "workflow_selection": waiting_selection(reason, [candidate]),
            "classification_errors": [],
            "policy": policy,
        }

    return {
        "schema_version": 1,
        "decision": "blocked",
        "selector_version": "1",
        "workflow_selection": blocked_selection("unsupported_classification"),
        "classification_errors": [],
        "policy": policy,
    }


def activation_envelope(
    classification: dict[str, Any],
    *,
    activation_source: str,
    task_id: str,
    request_id: str,
    refs: list[str],
    allowed_paths: list[str] | None = None,
    expires_at: str = "run_terminal",
) -> dict[str, Any]:
    selection_result = select_workflow(classification)
    selection = selection_result["workflow_selection"]
    policy = selection_result["policy"]
    bounded_scope = {
        "allowed_paths": allowed_paths or [],
        "allowed_ops": {
            "edit": False,
            "commit": False,
            "push": False,
            "network": False,
        },
        "step_budget": 1,
        "expires_at": expires_at,
    }
    envelope: dict[str, Any] = {
        "activation_version": "1",
        "activation_source": activation_source,
        "task_id": task_id,
        "request_id": request_id,
        "workflow_selection": {
            key: value
            for key, value in selection.items()
            if key in {"status", "workflow_id", "initial_step", "optional_expansions", "candidates"}
        },
        "policy": policy,
        "context_scope": {
            "mode": "bounded_refs",
            "refs": refs,
            "raw_transcript_sharing": "forbidden",
        },
        "activation_scope": bounded_scope,
        "next_action": "ask_human",
    }

    if selection["status"] == "blocked":
        envelope["activation_status"] = "blocked"
        envelope["approval_required_reason"] = selection.get("reason", "workflow_selection_blocked")
        return envelope

    if selection["status"] == "waiting_human":
        envelope["activation_status"] = "waiting_human"
        envelope["approval_required_reason"] = selection.get("reason", "human_decision_required")
        return envelope

    if activation_source in PROMPT_ONLY_SOURCES:
        envelope["activation_status"] = "proposed"
        envelope["goal_state_transition"] = {"from": "draft", "to": "proposed"}
        envelope["workflow_selection"]["status"] = "selected"
        envelope["next_action"] = "keep_draft"
        return envelope

    if activation_source not in EXPLICIT_APPROVAL_SOURCES:
        envelope["activation_status"] = "blocked"
        envelope["approval_required_reason"] = "unsupported_activation_source"
        return envelope

    if not refs:
        envelope["activation_status"] = "blocked"
        envelope["approval_required_reason"] = "bounded_context_refs_required"
        return envelope

    envelope["activation_status"] = "approved"
    envelope["approved_by"] = {
        "orchestrator-start": "human_explicit_skill_invocation",
        "human_ui": "human_ui_action",
        "manual_cli": "manual_operator",
    }[activation_source]
    envelope["approved_at"] = now_iso()
    envelope["goal_state_transition"] = {"from": "proposed", "to": "approved"}
    envelope["next_action"] = "create_workflow_run"
    return envelope


def validate_template(template: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    required = [
        "workflow_template_version",
        "workflow_id",
        "initial_step",
        "max_steps",
        "scheduler",
        "result_authority",
        "context_sharing",
        "provider_adapter",
        "output_contracts",
        "steps",
        "terminal_states",
    ]
    missing = [field for field in required if field not in template]
    if missing:
        errors.append(f"{relative_to_repo(path)} missing fields: {','.join(missing)}")
        return errors

    if template["workflow_template_version"] != "1":
        errors.append(f"{template['workflow_id']} workflow_template_version must be '1'")
    if template["initial_step"] not in {step.get("id") for step in template["steps"]}:
        errors.append(f"{template['workflow_id']} initial_step is not in steps")
    if template["max_steps"] < 1:
        errors.append(f"{template['workflow_id']} max_steps must be positive")

    scheduler = template["scheduler"]
    expected_scheduler = {
        "mode": "invocation-drain",
        "state_persistence": "durable_state",
        "lock_policy": "global_advisory_lock",
        "concurrency": 1,
    }
    for key, expected in expected_scheduler.items():
        if scheduler.get(key) != expected:
            errors.append(f"{template['workflow_id']} scheduler.{key} expected {expected!r}")

    authority = template["result_authority"]
    canonical = set(authority.get("canonical", []))
    signals = set(authority.get("signals_only", []))
    if "typed_report_file" not in canonical:
        errors.append(f"{template['workflow_id']} typed_report_file must be canonical")
    if "provider_transcript" not in signals:
        errors.append(f"{template['workflow_id']} provider_transcript must be signal only")

    context = template["context_sharing"]
    if context.get("provider_transcript") != "confined_evidence_path_only":
        errors.append(f"{template['workflow_id']} provider transcript must be confined")

    adapter = template["provider_adapter"]
    transports = set(adapter.get("allowed_transports", []))
    if "tmux_interactive" not in transports:
        errors.append(f"{template['workflow_id']} adapter must model future tmux_interactive transport")

    step_ids = {step["id"] for step in template["steps"]}
    for step in template["steps"]:
        if step.get("permission_mode") not in {"readonly", "edit", "full"}:
            errors.append(f"{template['workflow_id']} step {step.get('id')} has invalid permission")
        for transition in step.get("transitions", []):
            target = transition.get("to")
            if target not in step_ids and target not in set(template["terminal_states"]):
                errors.append(
                    f"{template['workflow_id']} step {step.get('id')} transition target {target!r} is invalid"
                )
    return errors


def validate_contracts() -> dict[str, Any]:
    errors: list[str] = []
    loaded_schemas: dict[str, Any] = {}
    for path in sorted(SCHEMA_ROOT.glob("*.json")):
        try:
            loaded_schemas[path.name] = load_json_path(path)
        except json.JSONDecodeError as exc:
            errors.append(f"{relative_to_repo(path)} invalid json: {exc}")

    registry = load_registry()
    registered_templates = registry.get("templates", [])
    if not registered_templates:
        errors.append("registry has no active templates")

    for entry in registered_templates:
        path = REPO_ROOT / entry["path"]
        try:
            template = load_json_path(path)
        except json.JSONDecodeError as exc:
            errors.append(f"{relative_to_repo(path)} invalid json: {exc}")
            continue
        errors.extend(validate_template(template, path))
        if template.get("workflow_id") != entry.get("workflow_id"):
            errors.append(f"{relative_to_repo(path)} workflow_id does not match registry")
        if template.get("initial_step") != entry.get("initial_step"):
            errors.append(f"{template.get('workflow_id')} initial_step does not match registry")
        if template.get("max_steps") != entry.get("max_steps"):
            errors.append(f"{template.get('workflow_id')} max_steps does not match registry")

    activation_schema = loaded_schemas.get("activation-envelope.schema.json", {})
    activation_scope = (activation_schema.get("properties") or {}).get("activation_scope")
    if "activation_scope" not in activation_schema.get("required", []):
        errors.append("activation envelope must require activation_scope")
    if not activation_scope:
        errors.append("activation envelope schema missing activation_scope")
    approved_condition = json.dumps(activation_schema.get("allOf", []), sort_keys=True)
    if '"minItems": 1' not in approved_condition:
        errors.append("approved activation must require bounded context refs")
    if '"activation_source"' not in approved_condition or '"enum": ["orchestrator-start", "human_ui", "manual_cli"]' not in approved_condition:
        errors.append("approved activation must reject frontdoor_prompt source")
    for required_fragment in (
        '"next_action": {"const": "create_workflow_run"}',
        '"status": {"const": "selected"}',
        '"workflow_id"',
        '"initial_step"',
    ):
        if required_fragment not in approved_condition:
            errors.append(f"approved activation missing selected-workflow constraint: {required_fragment}")
    for op in ("edit", "commit", "push", "network"):
        if f'"{op}": {{"const": false}}' not in approved_condition:
            errors.append(f"approved activation must keep {op}=false in P0")

    work_order_schema = loaded_schemas.get("work-order.schema.json", {})
    work_order_context = (work_order_schema.get("properties") or {}).get("context_scope", {})
    if "raw_transcript_sharing" not in work_order_context.get("required", []):
        errors.append("work order context_scope must require raw_transcript_sharing")
    if work_order_context.get("additionalProperties") is not False:
        errors.append("work order context_scope must reject undeclared raw sharing fields")
    work_order_condition = json.dumps(work_order_schema.get("allOf", []), sort_keys=True)
    for required_fragment in (
        '"workflow_id": {"const": "single_step_external_review"}',
        '"step_id": {"const": "review"}',
        '"permission_mode": {"const": "readonly"}',
        '"step_budget": {"const": 1}',
    ):
        if required_fragment not in work_order_condition:
            errors.append(f"work order missing P0 conditional constraint: {required_fragment}")
    for op in ("edit", "commit", "push", "network"):
        if f'"{op}": {{"const": false}}' not in work_order_condition:
            errors.append(f"work order must keep {op}=false for P0 single_step_external_review")

    report_schema = loaded_schemas.get("external-review-report.schema.json", {})
    provider_evidence = (
        (report_schema.get("properties") or {}).get("provider_evidence", {})
    )
    findings = (report_schema.get("properties") or {}).get("findings", {})
    finding_items = findings.get("items") or {}
    if provider_evidence.get("additionalProperties") is not False:
        errors.append("external review provider_evidence must not allow embedded raw fields")
    if finding_items.get("additionalProperties") is not False:
        errors.append("external review findings must not allow embedded raw fields")
    report_condition = json.dumps(report_schema.get("allOf", []), sort_keys=True)
    if '"result": {"const": "findings"}' not in report_condition or '"minItems": 1' not in report_condition:
        errors.append("external review findings result must require non-empty findings")

    workflow_run_schema = loaded_schemas.get("workflow-run.schema.json", {})
    run_activation = (workflow_run_schema.get("properties") or {}).get("activation", {})
    run_activation_condition = json.dumps(run_activation, sort_keys=True)
    for required_fragment in (
        '"activation_status": {"const": "approved"}',
        '"next_action": {"const": "create_workflow_run"}',
        '"status": {"const": "selected"}',
    ):
        if required_fragment not in run_activation_condition:
            errors.append(f"workflow run activation missing approved-envelope constraint: {required_fragment}")
    if '"activation_source"' not in run_activation_condition or '"enum": ["orchestrator-start", "human_ui", "manual_cli"]' not in run_activation_condition:
        errors.append("workflow run activation missing approved-envelope source constraint")
    workflow_run_scheduling = (workflow_run_schema.get("properties") or {}).get("scheduling", {})
    if workflow_run_scheduling.get("properties", {}).get("state_persistence", {}).get("const") != "durable_state":
        errors.append("workflow run scheduling must require durable_state persistence")

    return {
        "schema_version": 1,
        "decision": "ok" if not errors else "blocked",
        "workflow_contracts": {
            "registry_path": relative_to_repo(REGISTRY_PATH),
            "schema_count": len(list(SCHEMA_ROOT.glob("*.json"))),
            "template_count": len(registered_templates),
        },
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrator P0 deterministic workflow selector")
    sub = parser.add_subparsers(dest="command", required=True)

    select_parser = sub.add_parser("select", help="Select workflow from typed classification JSON")
    select_parser.add_argument("--classification", required=True, help="JSON string or path")

    activation_parser = sub.add_parser("activation-envelope", help="Create activation envelope")
    activation_parser.add_argument("--classification", required=True, help="JSON string or path")
    activation_parser.add_argument(
        "--activation-source",
        required=True,
        choices=sorted(EXPLICIT_APPROVAL_SOURCES | PROMPT_ONLY_SOURCES),
    )
    activation_parser.add_argument("--task-id", required=True)
    activation_parser.add_argument("--request-id", required=True)
    activation_parser.add_argument("--ref", action="append", default=[])
    activation_parser.add_argument("--allowed-path", action="append", default=[])
    activation_parser.add_argument("--expires-at", default="run_terminal")

    sub.add_parser("validate-contracts", help="Validate P0 workflow registry/templates/schemas")

    args = parser.parse_args()

    if args.command == "select":
        payload = select_workflow(load_json_arg(args.classification))
    elif args.command == "activation-envelope":
        payload = activation_envelope(
            load_json_arg(args.classification),
            activation_source=args.activation_source,
            task_id=args.task_id,
            request_id=args.request_id,
            refs=args.ref,
            allowed_paths=args.allowed_path,
            expires_at=args.expires_at,
        )
    else:
        payload = validate_contracts()

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("decision") == "blocked" or payload.get("activation_status") == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
