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
        "research_report",
        "code_change_report",
        "publication_result",
        "policy_change_report",
        "security_review_report",
        "final_evidence",
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
PUBLICATION_GATE_ID = "exit.publication_result_recorded"
POLICY_APPROVAL_GATE_ID = "exit.policy_approval_recorded"
SAFETY_RANK = {
    "readonly": 0,
    "standard": 1,
    "policy": 2,
    "security": 3,
}
PERMISSION_RANK = {
    "readonly": 0,
    "edit": 1,
    "full": 2,
}
ARTIFACT_BY_OUTPUT_CONTRACT = {
    "external_review_report": "typed_report",
    "research_report": "research_report",
    "code_change_report": "code_change_report",
    "publication_result": "publication_result",
    "policy_change_report": "policy_change_report",
    "security_review_report": "security_review_report",
}


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


def gate_profiles(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        profile["gate_id"]: profile
        for profile in registry.get("gate_profiles", [])
        if isinstance(profile, dict) and "gate_id" in profile
    }


def load_template(workflow_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any] | None:
    registry = registry or load_registry()
    entry = active_templates(registry).get(workflow_id)
    if not entry:
        return None
    return load_json_path(REPO_ROOT / entry["path"])


def publication_is_required(classification: dict[str, Any]) -> bool:
    return bool(
        classification.get("publication_required")
        or classification.get("task_kind") == "publication"
    )


def required_safety_class(classification: dict[str, Any]) -> str:
    if classification.get("task_kind") == "external_review":
        return "readonly"
    if classification.get("security_sensitive"):
        return "security"
    if classification.get("task_kind") == "policy_change":
        return "policy"
    if (
        classification.get("task_kind") == "research"
        and classification.get("permission_required") == "readonly"
    ):
        return "readonly"
    return "standard"


def candidate_workflow_id(classification: dict[str, Any]) -> str | None:
    task_kind = classification["task_kind"]
    permission = classification["permission_required"]

    if task_kind == "external_review":
        if permission != "readonly":
            return None
        return "single_step_external_review"
    if classification["security_sensitive"]:
        return "security_sensitive_change"
    if task_kind == "policy_change":
        return "policy_or_permission_change"
    if publication_is_required(classification):
        return "publication_required"
    if task_kind == "code_change":
        return "standard_code_change"
    if task_kind == "research":
        return "research_only"
    return None


def required_gates_for_candidate(
    template: dict[str, Any],
    classification: dict[str, Any],
    publication_gate_required: bool,
) -> list[str]:
    required_gates = list(template.get("mandatory_gates") or [])
    exit_gates = set((template.get("gates") or {}).get("exit") or [])

    if publication_gate_required and PUBLICATION_GATE_ID not in required_gates:
        required_gates.append(PUBLICATION_GATE_ID)

    if classification.get("task_kind") == "policy_change":
        if POLICY_APPROVAL_GATE_ID not in exit_gates:
            return []
        if POLICY_APPROVAL_GATE_ID not in required_gates:
            required_gates.append(POLICY_APPROVAL_GATE_ID)

    return required_gates


def required_permission_for_candidate(
    template: dict[str, Any],
    publication_gate_required: bool,
) -> str:
    if publication_gate_required:
        return "full"
    non_publication_modes = {
        step.get("permission_mode")
        for step in template.get("steps", [])
        if step.get("output_contract") != "publication_result"
    }
    if "full" in non_publication_modes or "edit" in non_publication_modes:
        return "edit"
    return "readonly"


def required_artifacts_for_candidate(
    template: dict[str, Any],
    classification: dict[str, Any],
    publication_gate_required: bool,
    required_gates: list[str],
) -> set[str]:
    artifacts: set[str] = set()
    for contract_name, contract in (template.get("output_contracts") or {}).items():
        if contract.get("required"):
            artifact = ARTIFACT_BY_OUTPUT_CONTRACT.get(contract_name)
            if artifact:
                artifacts.add(artifact)

    if publication_gate_required:
        artifacts.add("publication_result")
    if classification.get("task_kind") == "policy_change":
        artifacts.add("policy_change_report")
    if "exit.final_evidence_complete" in required_gates:
        artifacts.add("final_evidence")

    return artifacts


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


def selected_workflow(
    workflow_id: str,
    initial_step: str,
    optional_expansions: list[str],
    *,
    safety_class: str,
    required_safety: str,
    publication_gate_required: bool,
    required_gates: list[str],
) -> dict[str, Any]:
    return {
        "status": "selected",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "optional_expansions": optional_expansions,
        "safety_class": safety_class,
        "required_safety_class": required_safety,
        "publication_gate_required": publication_gate_required,
        "required_gates": required_gates,
    }


def validate_workflow_candidate(
    workflow_id: str,
    classification: dict[str, Any],
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = registry or load_registry()
    active = active_templates(registry)
    entry = active.get(workflow_id)
    if not entry:
        return blocked_selection("active_template_missing", candidates=[workflow_id])

    template = load_template(workflow_id, registry)
    if template is None:
        return blocked_selection("active_template_missing", candidates=[workflow_id])

    required_safety = required_safety_class(classification)
    template_safety = template.get("safety_class")
    if SAFETY_RANK.get(template_safety, -1) < SAFETY_RANK[required_safety]:
        return blocked_selection(
            "safety_class_downgrade",
            candidates=[workflow_id],
        )

    publication_required = publication_is_required(classification)
    publication_gate = template.get("publication_gate") or {}
    if publication_required and not publication_gate.get("supported"):
        return blocked_selection(
            "publication_gate_required",
            candidates=[workflow_id],
        )

    gate_lists = template.get("gates") or {}
    exit_gates = list(gate_lists.get("exit") or [])
    template_requires_publication = bool(publication_gate.get("required_by_default"))
    publication_gate_required = publication_required or template_requires_publication
    if publication_gate_required and PUBLICATION_GATE_ID not in exit_gates:
        return blocked_selection(
            "publication_gate_missing",
            candidates=[workflow_id],
        )

    required_gates = required_gates_for_candidate(
        template,
        classification,
        publication_gate_required,
    )
    if classification.get("task_kind") == "policy_change" and not required_gates:
        return blocked_selection(
            "policy_approval_gate_required",
            candidates=[workflow_id],
        )

    required_permission = required_permission_for_candidate(
        template,
        publication_gate_required,
    )
    requested_permission = classification.get("permission_required")
    if PERMISSION_RANK.get(requested_permission, -1) < PERMISSION_RANK[required_permission]:
        return blocked_selection(
            "permission_scope_insufficient",
            candidates=[workflow_id],
            missing_fields=[required_permission],
        )

    if workflow_id == "single_step_external_review":
        if classification.get("task_kind") != "external_review":
            return blocked_selection("external_review_template_requires_external_review")
        if classification.get("permission_required") != "readonly":
            return blocked_selection("external_review_must_be_readonly")
        if not classification.get("external_provider_required"):
            return waiting_selection(
                "external_review_without_external_provider_is_unsupported",
                [workflow_id],
            )
        if "typed_report" not in set(classification.get("expected_artifacts") or []):
            return blocked_selection("typed_report_artifact_required")

    required_artifacts = required_artifacts_for_candidate(
        template,
        classification,
        publication_gate_required,
        required_gates,
    )
    expected_artifacts = set(classification.get("expected_artifacts") or [])
    missing_artifacts = sorted(required_artifacts - expected_artifacts)
    if missing_artifacts:
        return blocked_selection(
            "required_artifacts_missing",
            candidates=[workflow_id],
            missing_fields=missing_artifacts,
        )

    optional_expansions = []
    if workflow_id == "single_step_external_review" and classification.get("security_sensitive"):
        optional_expansions.append("security_focus")

    return selected_workflow(
        workflow_id,
        entry["initial_step"],
        optional_expansions,
        safety_class=template_safety,
        required_safety=required_safety,
        publication_gate_required=publication_gate_required,
        required_gates=required_gates,
    )


def activation_scope_for_selection(
    selection: dict[str, Any],
    classification: dict[str, Any],
    *,
    allowed_paths: list[str] | None,
    expires_at: str,
) -> dict[str, Any]:
    allowed_ops = {
        "edit": False,
        "commit": False,
        "push": False,
        "network": False,
    }
    step_budget = 1

    if selection.get("status") == "selected":
        registry = load_registry()
        template = load_template(selection["workflow_id"], registry)
        if template:
            step_budget = int(template.get("max_steps", step_budget))
            permission_modes = {step.get("permission_mode") for step in template.get("steps", [])}
            requested_rank = PERMISSION_RANK.get(classification.get("permission_required"), -1)
            allowed_ops["edit"] = requested_rank >= PERMISSION_RANK["edit"] and bool(
                permission_modes & {"edit", "full"}
            )
            publication_allowed = (
                requested_rank >= PERMISSION_RANK["full"]
                and bool(selection.get("publication_gate_required"))
            )
            allowed_ops["commit"] = publication_allowed
            allowed_ops["push"] = publication_allowed
            allowed_ops["network"] = publication_allowed

    return {
        "allowed_paths": allowed_paths or [],
        "allowed_ops": allowed_ops,
        "step_budget": step_budget,
        "expires_at": expires_at,
    }


def select_workflow(classification: dict[str, Any]) -> dict[str, Any]:
    valid, errors = validate_classification(classification)
    policy = base_policy()
    registry = load_registry()

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

    candidate = candidate_workflow_id(classification)
    if candidate is None:
        return {
            "schema_version": 1,
            "decision": "blocked",
            "selector_version": "1",
            "workflow_selection": blocked_selection(
                "unsupported_classification",
            ),
            "classification_errors": [],
            "policy": policy,
        }

    selection = validate_workflow_candidate(candidate, classification, registry)
    if selection.get("publication_gate_required"):
        policy["publication"] = "separate_gate_required"
    if selection["status"] == "blocked" and selection.get("reason") == "publication_gate_required":
        policy["publication"] = "blocked"
    if classification["external_provider_required"]:
        policy["bounded_provider_transport"] = "allowed"

    if selection["status"] == "selected":
        return {
            "schema_version": 1,
            "decision": "selected",
            "selector_version": "1",
            "workflow_selection": selection,
            "classification_errors": [],
            "policy": policy,
        }

    if selection["status"] == "waiting_human":
        return {
            "schema_version": 1,
            "decision": "waiting_human",
            "selector_version": "1",
            "workflow_selection": selection,
            "classification_errors": [],
            "policy": policy,
        }

    return {
        "schema_version": 1,
        "decision": "blocked",
        "selector_version": "1",
        "workflow_selection": selection,
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
    bounded_scope = activation_scope_for_selection(
        selection,
        classification,
        allowed_paths=allowed_paths,
        expires_at=expires_at,
    )
    envelope: dict[str, Any] = {
        "activation_version": "1",
        "activation_source": activation_source,
        "task_id": task_id,
        "request_id": request_id,
        "workflow_selection": {
            key: value
            for key, value in selection.items()
            if key
            in {
                "status",
                "workflow_id",
                "initial_step",
                "optional_expansions",
                "candidates",
                "safety_class",
                "required_safety_class",
                "publication_gate_required",
                "required_gates",
            }
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
        envelope["activation_scope"]["allowed_ops"] = {
            "edit": False,
            "commit": False,
            "push": False,
            "network": False,
        }
        envelope["activation_scope"]["step_budget"] = 1
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


def validate_template(template: dict[str, Any], path: Path, registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = [
        "workflow_template_version",
        "workflow_id",
        "lifecycle_status",
        "initial_step",
        "max_steps",
        "safety_class",
        "publication_gate",
        "gates",
        "mandatory_gates",
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
    if template["lifecycle_status"] != "active":
        errors.append(f"{template['workflow_id']} lifecycle_status must be active")
    if template["initial_step"] not in {step.get("id") for step in template["steps"]}:
        errors.append(f"{template['workflow_id']} initial_step is not in steps")
    if template["max_steps"] < 1:
        errors.append(f"{template['workflow_id']} max_steps must be positive")
    if template["safety_class"] not in SAFETY_RANK:
        errors.append(f"{template['workflow_id']} safety_class is invalid")

    gate_index = gate_profiles(registry)
    gates = template["gates"]
    entry_gates = list(gates.get("entry") or [])
    exit_gates = list(gates.get("exit") or [])
    all_gates = entry_gates + exit_gates
    for gate_id in registry.get("mandatory_gate_profiles", []):
        if gate_id not in template["mandatory_gates"]:
            errors.append(f"{template['workflow_id']} missing mandatory registry gate {gate_id}")
        if gate_id not in all_gates:
            errors.append(f"{template['workflow_id']} mandatory registry gate {gate_id} is not in gates")
    for gate_id in template["mandatory_gates"]:
        if gate_id not in all_gates:
            errors.append(f"{template['workflow_id']} mandatory gate {gate_id} is not in gates")
    for gate_id in all_gates:
        profile = gate_index.get(gate_id)
        if not profile:
            errors.append(f"{template['workflow_id']} unknown gate profile {gate_id}")
            continue
        expected_phase = profile.get("phase")
        if gate_id in entry_gates and expected_phase != "entry":
            errors.append(f"{template['workflow_id']} gate {gate_id} must not be in entry gates")
        if gate_id in exit_gates and expected_phase != "exit":
            errors.append(f"{template['workflow_id']} gate {gate_id} must not be in exit gates")

    publication_gate = template["publication_gate"]
    if publication_gate.get("supported") and PUBLICATION_GATE_ID not in exit_gates:
        errors.append(f"{template['workflow_id']} supports publication but omits {PUBLICATION_GATE_ID}")
    if publication_gate.get("required_by_default") and PUBLICATION_GATE_ID not in template["mandatory_gates"]:
        errors.append(f"{template['workflow_id']} default publication gate must be mandatory")

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

    output_contracts = template["output_contracts"]
    for contract_name, contract in output_contracts.items():
        schema_path = contract.get("schema_path")
        if not schema_path:
            errors.append(f"{template['workflow_id']} output contract {contract_name} missing schema_path")
            continue
        if not (REPO_ROOT / schema_path).exists():
            errors.append(f"{template['workflow_id']} output contract {contract_name} schema missing")

    step_ids = {step["id"] for step in template["steps"]}
    for step in template["steps"]:
        if step.get("permission_mode") not in {"readonly", "edit", "full"}:
            errors.append(f"{template['workflow_id']} step {step.get('id')} has invalid permission")
        if step.get("output_contract") not in output_contracts:
            errors.append(
                f"{template['workflow_id']} step {step.get('id')} output_contract is not declared"
            )
        for transition in step.get("transitions", []):
            target = transition.get("to")
            if target not in step_ids and target not in set(template["terminal_states"]):
                errors.append(
                    f"{template['workflow_id']} step {step.get('id')} transition target {target!r} is invalid"
                )
    return errors


def validate_registry(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    safety_classes = registry.get("safety_classes", [])
    safety_rank_from_registry = {
        item.get("safety_class"): item.get("rank")
        for item in safety_classes
        if isinstance(item, dict)
    }
    for safety_class, rank in SAFETY_RANK.items():
        if safety_rank_from_registry.get(safety_class) != rank:
            errors.append(f"registry safety class {safety_class} must have rank {rank}")

    profile_index = gate_profiles(registry)
    for gate_id in registry.get("mandatory_gate_profiles", []):
        if gate_id not in profile_index:
            errors.append(f"registry mandatory gate profile missing: {gate_id}")

    for gate_id, profile in profile_index.items():
        phase = profile.get("phase")
        if phase not in {"entry", "exit"}:
            errors.append(f"registry gate {gate_id} has invalid phase {phase!r}")
        if not gate_id.startswith(f"{phase}."):
            errors.append(f"registry gate {gate_id} must use {phase}. prefix")

    return errors


def template_step_constraint_fragments(registry: dict[str, Any]) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    for entry in registry.get("templates", []):
        path = REPO_ROOT / entry["path"]
        try:
            template = load_json_path(path)
        except (OSError, json.JSONDecodeError):
            continue
        workflow_id = template.get("workflow_id")
        for step in template.get("steps", []):
            fragments.extend(
                [
                    (workflow_id, f'"workflow_id": {{"const": "{workflow_id}"}}'),
                    (workflow_id, f'"step_id": {{"const": "{step.get("id")}"}}'),
                    (workflow_id, f'"assignment_role": {{"const": "{step.get("assignment_role")}"}}'),
                    (workflow_id, f'"permission_mode": {{"const": "{step.get("permission_mode")}"}}'),
                    (workflow_id, f'"expected_output": {{"const": "{step.get("output_contract")}"}}'),
                ]
            )
    return fragments


def validate_contracts() -> dict[str, Any]:
    errors: list[str] = []
    loaded_schemas: dict[str, Any] = {}
    for path in sorted(SCHEMA_ROOT.glob("*.json")):
        try:
            loaded_schemas[path.name] = load_json_path(path)
        except json.JSONDecodeError as exc:
            errors.append(f"{relative_to_repo(path)} invalid json: {exc}")

    registry = load_registry()
    errors.extend(validate_registry(registry))
    registered_templates = registry.get("templates", [])
    if not registered_templates:
        errors.append("registry has no active templates")

    for entry in registered_templates:
        if entry.get("status") != "active":
            errors.append(f"registry template {entry.get('workflow_id')} must be active")
        path = REPO_ROOT / entry["path"]
        try:
            template = load_json_path(path)
        except json.JSONDecodeError as exc:
            errors.append(f"{relative_to_repo(path)} invalid json: {exc}")
            continue
        errors.extend(validate_template(template, path, registry))
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
        '"safety_class"',
        '"required_safety_class"',
        '"publication_gate_required"',
        '"required_gates"',
    ):
        if required_fragment not in approved_condition:
            errors.append(f"approved activation missing selected-workflow constraint: {required_fragment}")
    allowed_ops_schema = json.dumps(activation_scope.get("properties", {}).get("allowed_ops", {}), sort_keys=True)
    for op in ("edit", "commit", "push", "network"):
        if f'"{op}": {{"type": "boolean"}}' not in allowed_ops_schema:
            errors.append(f"approved activation must type {op} as boolean")
    step_budget_schema = (
        activation_scope.get("properties", {}).get("step_budget", {})
        if activation_scope
        else {}
    )
    if step_budget_schema.get("type") != "integer" or step_budget_schema.get("minimum") != 1:
        errors.append("approved activation must allow positive integer step_budget")

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
    readonly_conditions = [
        item
        for item in work_order_schema.get("allOf", [])
        if (
            item.get("if", {})
            .get("properties", {})
            .get("permission_mode", {})
            .get("const")
            == "readonly"
        )
    ]
    if not readonly_conditions:
        errors.append("work order must pin allowed_ops=false for readonly permission_mode")
    else:
        readonly_ops = (
            readonly_conditions[0]
            .get("then", {})
            .get("properties", {})
            .get("activation_scope", {})
            .get("properties", {})
            .get("allowed_ops", {})
            .get("properties", {})
        )
        for op in ("edit", "commit", "push", "network"):
            if readonly_ops.get(op, {}).get("const") is not False:
                errors.append(f"work order readonly permission_mode must keep {op}=false")
    if '"publisher"' not in json.dumps(work_order_schema, sort_keys=True):
        errors.append("work order must allow publisher assignment role")
    for workflow_id, fragment in template_step_constraint_fragments(registry):
        if fragment not in work_order_condition:
            errors.append(f"work order missing template step constraint for {workflow_id}: {fragment}")

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
        '"safety_class"',
        '"required_safety_class"',
        '"publication_gate_required"',
        '"required_gates"',
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
