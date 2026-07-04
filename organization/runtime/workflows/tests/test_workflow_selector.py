#!/usr/bin/env python3
"""Unit/static tests for Orchestrator workflow contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_ROOT = ROOT / "organization/runtime/workflows"
SCRIPT = WORKFLOW_ROOT / "scripts/workflow_selector.py"
FACADE = ROOT / "scripts/configure_organization.py"
REGISTRY = WORKFLOW_ROOT / "registry.yaml"
TEMPLATE_ROOT = WORKFLOW_ROOT / "templates"
WORK_ORDER_SCHEMA = WORKFLOW_ROOT / "schemas/work-order.schema.json"
WORKFLOW_RUN_SCHEMA = WORKFLOW_ROOT / "schemas/workflow-run.schema.json"
ACTIVATION_SCHEMA = WORKFLOW_ROOT / "schemas/activation-envelope.schema.json"
WORKFLOW_TEMPLATE_SCHEMA = WORKFLOW_ROOT / "schemas/workflow-template.schema.json"
PUBLICATION_RESULT_SCHEMA = WORKFLOW_ROOT / "schemas/publication-result.schema.json"
CODE_CHANGE_REPORT_SCHEMA = WORKFLOW_ROOT / "schemas/code-change-report.schema.json"
ORCHESTRATOR_PROJECTION_SCHEMA = WORKFLOW_ROOT / "schemas/orchestrator-projection.schema.json"

PUBLICATION_GATE_ID = "exit.publication_result_recorded"


def load_selector_module():
    spec = importlib.util.spec_from_file_location("workflow_selector", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


selector = load_selector_module()


def typed_classification(task_kind: str = "external_review", **overrides):
    default_permission = {
        "external_review": "readonly",
        "research": "readonly",
        "code_change": "edit",
        "publication": "full",
        "policy_change": "edit",
    }[task_kind]
    default_artifacts = {
        "external_review": ["typed_report"],
        "research": ["research_report"],
        "code_change": ["code_change_report", "final_evidence"],
        "publication": ["code_change_report", "publication_result", "final_evidence"],
        "policy_change": ["policy_change_report", "final_evidence"],
    }[task_kind]
    candidate = {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["test-fixture"],
        "task_kind": task_kind,
        "permission_required": default_permission,
        "external_provider_required": task_kind == "external_review",
        "publication_required": task_kind == "publication",
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only" if default_permission == "readonly" else "diff_summary",
        "expected_artifacts": default_artifacts,
    }
    candidate.update(overrides)
    return candidate


def external_review_classification(**overrides):
    task_kind = overrides.pop("task_kind", "external_review")
    return typed_classification(task_kind, **overrides)


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def assert_selected(classification: dict, workflow_id: str) -> dict:
    selection = selector.select_workflow(classification)
    assert_equal(selection["decision"], "selected", f"{workflow_id} decision")
    assert_equal(
        selection["workflow_selection"]["workflow_id"],
        workflow_id,
        f"{workflow_id} selected workflow",
    )
    return selection


def run_facade(*args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(FACADE), "workflow-selector", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def run_facade_raw(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FACADE), "workflow-selector", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_contract_validation() -> None:
    contracts = selector.validate_contracts()
    assert_equal(contracts["decision"], "ok", f"contracts errors: {contracts['errors']}")
    assert_equal(contracts["workflow_contracts"]["template_count"], 6, "template count")
    assert_equal(contracts["workflow_contracts"]["schema_count"], 15, "schema count")


def test_registry_gate_profiles_and_active_templates() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    template_ids = [entry["workflow_id"] for entry in registry["templates"]]
    assert_equal(
        template_ids,
        [
            "single_step_external_review",
            "research_only",
            "standard_code_change",
            "publication_required",
            "policy_or_permission_change",
            "security_sensitive_change",
        ],
        "active workflow ids",
    )
    gate_ids = {profile["gate_id"] for profile in registry["gate_profiles"]}
    for gate_id in registry["mandatory_gate_profiles"]:
        assert gate_id in gate_ids, f"missing registry gate {gate_id}"

    for entry in registry["templates"]:
        template = json.loads((ROOT / entry["path"]).read_text(encoding="utf-8"))
        assert_equal(template["lifecycle_status"], "active", f"{entry['workflow_id']} active")
        for gate_id in registry["mandatory_gate_profiles"]:
            assert gate_id in template["mandatory_gates"], f"{entry['workflow_id']} missing {gate_id}"


def test_single_step_external_review_template() -> None:
    template = json.loads(
        (TEMPLATE_ROOT / "single_step_external_review.yaml").read_text(encoding="utf-8")
    )
    assert_equal(template["workflow_id"], "single_step_external_review", "workflow_id")
    assert_equal(template["safety_class"], "readonly", "safety class")
    assert_equal(template["publication_gate"]["supported"], False, "publication support")
    assert_equal(template["initial_step"], "review", "initial_step")
    assert_equal(template["max_steps"], 1, "max_steps")
    assert_equal(len(template["steps"]), 1, "step count")
    assert "tmux_interactive" in template["provider_adapter"]["allowed_transports"]
    assert "typed_report_file" in template["result_authority"]["canonical"]
    assert "provider_transcript" in template["result_authority"]["signals_only"]
    assert_equal(
        template["context_sharing"]["provider_transcript"],
        "confined_evidence_path_only",
        "provider transcript scope",
    )


def test_selector_external_review() -> None:
    selection = assert_selected(external_review_classification(), "single_step_external_review")
    assert_equal(selection["workflow_selection"]["initial_step"], "review", "initial step")
    assert_equal(selection["policy"]["bounded_provider_transport"], "allowed", "provider policy")

    security_selection = assert_selected(
        external_review_classification(security_sensitive=True),
        "single_step_external_review",
    )
    assert_equal(
        security_selection["workflow_selection"]["optional_expansions"],
        ["security_focus"],
        "security expansion",
    )

    unsupported = selector.select_workflow(
        external_review_classification(external_provider_required=False)
    )
    assert_equal(unsupported["decision"], "waiting_human", "external provider required")


def test_selector_active_template_routes() -> None:
    assert_selected(typed_classification("research"), "research_only")
    assert_selected(typed_classification("code_change"), "standard_code_change")

    publication = assert_selected(
        typed_classification(
            "code_change",
            permission_required="full",
            publication_required=True,
            expected_artifacts=["code_change_report", "publication_result", "final_evidence"],
        ),
        "publication_required",
    )
    assert_equal(
        publication["workflow_selection"]["publication_gate_required"],
        True,
        "publication gate required",
    )
    assert_equal(
        publication["policy"]["publication"],
        "separate_gate_required",
        "publication policy",
    )

    assert_selected(typed_classification("publication"), "publication_required")
    assert_selected(typed_classification("policy_change"), "policy_or_permission_change")

    security = assert_selected(
        typed_classification(
            "code_change",
            security_sensitive=True,
            expected_artifacts=["security_review_report", "code_change_report", "final_evidence"],
        ),
        "security_sensitive_change",
    )
    assert_equal(security["workflow_selection"]["safety_class"], "security", "security class")

    security_publication = assert_selected(
        typed_classification(
            "code_change",
            permission_required="full",
            security_sensitive=True,
            publication_required=True,
            expected_artifacts=[
                "security_review_report",
                "code_change_report",
                "publication_result",
                "final_evidence",
            ],
        ),
        "security_sensitive_change",
    )
    assert PUBLICATION_GATE_ID in security_publication["workflow_selection"]["required_gates"]

    security_policy = assert_selected(
        typed_classification(
            "policy_change",
            security_sensitive=True,
            expected_artifacts=[
                "security_review_report",
                "code_change_report",
                "policy_change_report",
                "final_evidence",
            ],
        ),
        "security_sensitive_change",
    )
    assert "exit.policy_approval_recorded" in security_policy["workflow_selection"]["required_gates"]


def test_candidate_validation_blocks_downgrades() -> None:
    security_on_standard = selector.validate_workflow_candidate(
        "standard_code_change",
        typed_classification("code_change", security_sensitive=True),
    )
    assert_equal(security_on_standard["status"], "blocked", "security downgrade status")
    assert_equal(security_on_standard["reason"], "safety_class_downgrade", "security downgrade")

    policy_on_standard = selector.validate_workflow_candidate(
        "standard_code_change",
        typed_classification("policy_change"),
    )
    assert_equal(policy_on_standard["reason"], "safety_class_downgrade", "policy downgrade")

    publication_on_standard = selector.validate_workflow_candidate(
        "standard_code_change",
        typed_classification("code_change", permission_required="full", publication_required=True),
    )
    assert_equal(
        publication_on_standard["reason"],
        "publication_gate_required",
        "publication gate missing",
    )

    publication_on_policy = selector.validate_workflow_candidate(
        "policy_or_permission_change",
        typed_classification("policy_change", permission_required="full", publication_required=True),
    )
    assert_equal(
        publication_on_policy["reason"],
        "publication_gate_required",
        "policy publication unsupported",
    )


def test_selector_rejects_permission_and_artifact_downgrades() -> None:
    readonly_code = selector.select_workflow(
        typed_classification("code_change", permission_required="readonly")
    )
    assert_equal(readonly_code["decision"], "blocked", "readonly code change decision")
    assert_equal(
        readonly_code["workflow_selection"]["reason"],
        "permission_scope_insufficient",
        "readonly code change reason",
    )

    edit_publication = selector.select_workflow(
        typed_classification("code_change", permission_required="edit", publication_required=True)
    )
    assert_equal(edit_publication["decision"], "blocked", "edit publication decision")
    assert_equal(
        edit_publication["workflow_selection"]["reason"],
        "permission_scope_insufficient",
        "edit publication reason",
    )

    missing_artifacts = selector.select_workflow(
        typed_classification("research", expected_artifacts=["typed_report"])
    )
    assert_equal(missing_artifacts["decision"], "blocked", "missing artifacts decision")
    assert_equal(
        missing_artifacts["workflow_selection"]["reason"],
        "required_artifacts_missing",
        "missing artifacts reason",
    )
    assert_equal(
        missing_artifacts["workflow_selection"]["missing_fields"],
        ["research_report"],
        "missing artifact names",
    )


def test_activation_prompt_is_only_proposed() -> None:
    proposed = selector.activation_envelope(
        external_review_classification(),
        activation_source="frontdoor_prompt",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
    )
    assert_equal(proposed["activation_status"], "proposed", "ordinary prompt activation")
    assert_equal(proposed["next_action"], "keep_draft", "ordinary prompt next action")
    assert "approved_by" not in proposed
    assert_equal(
        proposed["activation_scope"]["allowed_ops"],
        {"edit": False, "commit": False, "push": False, "network": False},
        "ordinary prompt allowed ops",
    )


def test_activation_scope_follows_selected_template() -> None:
    approved_readonly = selector.activation_envelope(
        external_review_classification(),
        activation_source="orchestrator-start",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
    )
    assert_equal(
        approved_readonly["activation_scope"]["allowed_ops"],
        {"edit": False, "commit": False, "push": False, "network": False},
        "readonly approved allowed ops",
    )
    assert_equal(approved_readonly["activation_scope"]["step_budget"], 1, "readonly budget")

    approved_code = selector.activation_envelope(
        typed_classification("code_change"),
        activation_source="orchestrator-start",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
        allowed_paths=["organization/runtime/workflows"],
    )
    assert_equal(
        approved_code["activation_scope"]["allowed_ops"],
        {"edit": True, "commit": False, "push": False, "network": False},
        "code approved allowed ops",
    )
    assert_equal(approved_code["activation_scope"]["step_budget"], 4, "code budget")

    approved_publication = selector.activation_envelope(
        typed_classification("publication"),
        activation_source="orchestrator-start",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
        allowed_paths=["organization/runtime/workflows"],
    )
    assert_equal(
        approved_publication["activation_scope"]["allowed_ops"],
        {"edit": True, "commit": True, "push": True, "network": True},
        "publication approved allowed ops",
    )
    assert_equal(approved_publication["activation_scope"]["step_budget"], 5, "publication budget")


def test_activation_schema_approved_envelope_constraints() -> None:
    schema = json.loads(ACTIVATION_SCHEMA.read_text(encoding="utf-8"))
    approved_condition = json.dumps(schema["allOf"], sort_keys=True)
    for fragment in (
        '"status": {"const": "selected"}',
        '"workflow_id"',
        '"initial_step"',
        '"safety_class"',
        '"required_safety_class"',
        '"publication_gate_required"',
        '"required_gates"',
        '"next_action": {"const": "create_workflow_run"}',
        '"minItems": 1',
    ):
        assert fragment in approved_condition, f"missing approved activation constraint {fragment}"
    assert "classification_provenance" in schema["required"]
    assert '"activation_source"' in approved_condition
    assert '"enum": ["orchestrator-start", "human_ui", "manual_cli"]' in approved_condition

    allowed_ops = schema["properties"]["activation_scope"]["properties"]["allowed_ops"]
    for op in ("edit", "commit", "push", "network"):
        assert_equal(allowed_ops["properties"][op]["type"], "boolean", f"{op} type")


def test_activation_requires_bounded_refs_for_approval() -> None:
    missing_refs = selector.activation_envelope(
        external_review_classification(),
        activation_source="orchestrator-start",
        task_id="TSK-test",
        request_id="req-test",
        refs=[],
    )
    assert_equal(missing_refs["activation_status"], "blocked", "missing refs activation")
    assert_equal(
        missing_refs["approval_required_reason"],
        "bounded_context_refs_required",
        "missing refs reason",
    )


def test_human_ui_and_manual_cli_approval_attribution() -> None:
    human_ui = selector.activation_envelope(
        external_review_classification(),
        activation_source="human_ui",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
    )
    assert_equal(human_ui["activation_status"], "approved", "human ui activation")
    assert_equal(human_ui["approved_by"], "human_ui_action", "human ui approval source")

    manual_cli = selector.activation_envelope(
        external_review_classification(),
        activation_source="manual_cli",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
    )
    assert_equal(manual_cli["approved_by"], "manual_operator", "manual cli approval source")


def test_selector_blocks_destructive_operation() -> None:
    destructive = selector.select_workflow(
        external_review_classification(destructive_operation=True)
    )
    assert_equal(destructive["decision"], "blocked", "destructive decision")
    assert_equal(
        destructive["workflow_selection"]["reason"],
        "destructive_operation_requires_separate_approval",
        "destructive reason",
    )


def test_malformed_expected_artifacts_blocks_without_crashing() -> None:
    malformed = selector.select_workflow(
        external_review_classification(expected_artifacts=[1])
    )
    assert_equal(malformed["decision"], "blocked", "malformed artifacts decision")
    assert "expected_artifacts entries must be strings" in malformed["classification_errors"]


def test_classifier_provenance_is_required_and_bounded() -> None:
    missing_source = selector.select_workflow(
        external_review_classification(classification_source="frontdoor_llm_proposal")
    )
    assert_equal(missing_source["decision"], "blocked", "llm proposal source decision")
    assert any("classification_source unsupported" in item for item in missing_source["classification_errors"])

    low_confidence = selector.select_workflow(
        external_review_classification(classification_confidence=0.5)
    )
    assert_equal(low_confidence["decision"], "blocked", "low confidence decision")
    assert any("below threshold" in item for item in low_confidence["classification_errors"])

    no_evidence = selector.select_workflow(
        external_review_classification(classification_evidence=[])
    )
    assert_equal(no_evidence["decision"], "blocked", "missing evidence decision")
    assert "classification_evidence must be a non-empty list" in no_evidence["classification_errors"]


def test_permission_monotonicity_fuzz_for_p0_selection() -> None:
    selected_cases = []
    for task_kind in ("external_review", "code_change", "research", "publication", "policy_change"):
        for permission in ("readonly", "edit", "full"):
            for destructive in (False, True):
                for publication_required in (False, True):
                    candidate = external_review_classification(
                        task_kind=task_kind,
                        permission_required=permission,
                        destructive_operation=destructive,
                        publication_required=publication_required,
                        external_provider_required=task_kind == "external_review",
                        expected_artifacts=["typed_report"]
                        if task_kind in {"external_review", "research", "policy_change"}
                        else ["code_diff", "validation_result", "vault_update"],
                        context_scope="refs_only" if permission == "readonly" else "diff_summary",
                    )
                    decision = selector.select_workflow(candidate)
                    if decision["decision"] == "selected":
                        selected_cases.append((task_kind, permission, destructive, publication_required))

    assert_equal(
        selected_cases,
        [("external_review", "readonly", False, False)],
        "only readonly external review can be selected in P0",
    )


def test_work_order_schema_constrains_single_step_external_review() -> None:
    work_order_schema = json.loads(WORK_ORDER_SCHEMA.read_text(encoding="utf-8"))
    for field in ["run_id", "workflow_id", "step_id", "activation_scope", "work_order_authority"]:
        assert field in work_order_schema["required"], f"work order requires {field}"
    allowed_ops = work_order_schema["properties"]["activation_scope"]["properties"]["allowed_ops"]
    assignment_roles = work_order_schema["properties"]["assignment_role"]["enum"]
    assert "publisher" in assignment_roles
    assert_equal(
        allowed_ops["properties"]["push"]["type"],
        "boolean",
        "work order push gate type",
    )
    context_scope = work_order_schema["properties"]["context_scope"]
    assert "raw_transcript_sharing" in context_scope["required"]
    assert_equal(
        context_scope["properties"]["raw_transcript_sharing"]["const"],
        "forbidden",
        "work order raw transcript sharing",
    )
    assert_equal(
        context_scope["additionalProperties"],
        False,
        "work order context extra fields",
    )
    conditional = json.dumps(work_order_schema["allOf"], sort_keys=True)
    for fragment in (
        '"workflow_id": {"const": "single_step_external_review"}',
        '"step_id": {"const": "review"}',
        '"permission_mode": {"const": "readonly"}',
        '"step_budget": {"const": 1}',
    ):
        assert fragment in conditional, f"missing work order conditional {fragment}"
    for op in ("edit", "commit", "push", "network"):
        fragment = f'"{op}": {{"const": false}}'
        assert fragment in conditional, f"missing work order op constraint {fragment}"
    authority = work_order_schema["properties"]["work_order_authority"]
    assert "signature" in authority["required"]
    assert "runner_claim" in authority["required"]
    readonly_conditions = [
        item
        for item in work_order_schema["allOf"]
        if (
            item.get("if", {})
            .get("properties", {})
            .get("permission_mode", {})
            .get("const")
            == "readonly"
        )
    ]
    assert_equal(len(readonly_conditions), 1, "readonly work order op condition")
    readonly_ops = readonly_conditions[0]["then"]["properties"]["activation_scope"][
        "properties"
    ]["allowed_ops"]["properties"]
    for op in ("edit", "commit", "push", "network"):
        assert_equal(
            readonly_ops[op]["const"],
            False,
            f"readonly work order {op} constraint",
        )
    readonly_branches = [
        branch
        for branch in work_order_schema["allOf"][0]["then"]["anyOf"]
        if branch["properties"]["permission_mode"]["const"] == "readonly"
    ]
    assert readonly_branches, "work order has readonly branches"
    for fragment in (
        '"workflow_id": {"const": "publication_required"}',
        '"step_id": {"const": "publication_gate"}',
        '"assignment_role": {"const": "publisher"}',
        '"expected_output": {"const": "publication_result"}',
        '"workflow_id": {"const": "security_sensitive_change"}',
        '"step_id": {"const": "policy_review"}',
        '"expected_output": {"const": "policy_change_report"}',
    ):
        assert fragment in conditional, f"missing work order template constraint {fragment}"


def test_external_review_report_schema_rejects_embedded_raw_fields() -> None:
    report_schema = json.loads(
        (WORKFLOW_ROOT / "schemas/external-review-report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    provider_evidence = report_schema["properties"]["provider_evidence"]
    finding_items = report_schema["properties"]["findings"]["items"]
    assert_equal(
        provider_evidence["additionalProperties"],
        False,
        "provider evidence extra fields",
    )
    assert_equal(
        finding_items["additionalProperties"],
        False,
        "finding extra fields",
    )
    assert "transcript_path" in provider_evidence["required"]
    assert "evidence_refs" in finding_items["required"]
    conditional = json.dumps(report_schema["allOf"], sort_keys=True)
    assert '"result": {"const": "findings"}' in conditional
    assert '"minItems": 1' in conditional


def test_orchestrator_projection_schema_closes_redacted_objects() -> None:
    schema = json.loads(ORCHESTRATOR_PROJECTION_SCHEMA.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    assert_equal(
        schema["properties"]["safe_for_principal"]["$ref"],
        "#/$defs/principal",
        "safe principal closed ref",
    )
    assert_equal(defs["principal"]["additionalProperties"], False, "safe principal extra fields")
    approval = defs["redacted_approval"]
    assert_equal(approval["additionalProperties"], False, "approval extra fields")
    assert_equal(
        defs["approval_work"]["additionalProperties"],
        False,
        "approval work extra fields",
    )
    assert "anyOf" in schema["properties"]["approval"], "approval must allow only redacted object or null"


def test_publication_and_code_change_report_schemas_require_evidence() -> None:
    publication_schema = json.loads(PUBLICATION_RESULT_SCHEMA.read_text(encoding="utf-8"))
    publication_condition = json.dumps(publication_schema["allOf"], sort_keys=True)
    assert '"result": {"const": "completed"}' in publication_condition
    assert '"approved": {"const": true}' in publication_condition

    code_schema = json.loads(CODE_CHANGE_REPORT_SCHEMA.read_text(encoding="utf-8"))
    complete_condition = json.dumps(code_schema["allOf"], sort_keys=True)
    assert '"result": {"const": "complete"}' in complete_condition
    assert '"review": {"required": ["status", "evidence_refs"]}' in complete_condition
    assert '"validation": {"required": ["status", "evidence_refs"]}' in complete_condition


def test_workflow_template_schema_and_security_template_publication_path() -> None:
    template_schema = json.loads(WORKFLOW_TEMPLATE_SCHEMA.read_text(encoding="utf-8"))
    publication_gate_schema = template_schema["properties"]["publication_gate"]["properties"]
    assert "required_when" not in publication_gate_schema

    template = json.loads(
        (TEMPLATE_ROOT / "security_sensitive_change.yaml").read_text(encoding="utf-8")
    )
    assert_equal(template["max_steps"], 7, "security max steps")
    assert "required_when" not in template["publication_gate"]
    assert "exit.policy_approval_recorded" in template["gates"]["exit"]
    publication_steps = [
        step
        for step in template["steps"]
        if step["id"] == "publication_gate"
        and step["output_contract"] == "publication_result"
    ]
    assert_equal(len(publication_steps), 1, "security publication step")
    policy_steps = [
        step
        for step in template["steps"]
        if step["id"] == "policy_review"
        and step["output_contract"] == "policy_change_report"
    ]
    assert_equal(len(policy_steps), 1, "security policy step")


def test_workflow_run_schema_encodes_scheduler_and_activation_scope() -> None:
    workflow_run_schema = json.loads(WORKFLOW_RUN_SCHEMA.read_text(encoding="utf-8"))
    scheduling = workflow_run_schema["properties"]["scheduling"]["properties"]
    assert_equal(scheduling["scheduler_mode"]["const"], "invocation-drain", "scheduler mode")
    assert_equal(scheduling["lock_policy"]["const"], "global_advisory_lock", "lock policy")
    assert_equal(scheduling["state_persistence"]["const"], "durable_state", "scheduler state persistence")
    assert_equal(scheduling["concurrency"]["const"], 1, "concurrency")
    activation = workflow_run_schema["properties"]["activation"]
    activation_text = json.dumps(activation, sort_keys=True)
    for fragment in (
        '"activation_status": {"const": "approved"}',
        '"status": {"const": "selected"}',
        '"next_action": {"const": "create_workflow_run"}',
        '"safety_class"',
        '"required_safety_class"',
        '"publication_gate_required"',
        '"required_gates"',
    ):
        assert fragment in activation_text, f"missing run activation constraint {fragment}"
    assert "classification_provenance" in workflow_run_schema["properties"]["activation"]["required"]
    assert '"activation_source"' in activation_text
    assert '"enum": ["orchestrator-start", "human_ui", "manual_cli"]' in activation_text

    allowed_ops = activation["properties"]["activation_scope"]["properties"]["allowed_ops"]
    for op in ("edit", "commit", "push", "network"):
        assert_equal(allowed_ops["properties"][op]["type"], "boolean", f"{op} type")


def test_configure_organization_facade() -> None:
    facade_result = run_facade("validate-contracts")
    assert_equal(facade_result["decision"], "ok", "facade validation")


def test_blocked_activation_cli_exits_nonzero() -> None:
    completed = run_facade_raw(
        "activation-envelope",
        "--activation-source",
        "orchestrator-start",
        "--task-id",
        "TSK-test",
        "--request-id",
        "req-test",
        "--classification",
        json.dumps(external_review_classification()),
    )
    assert_equal(completed.returncode, 2, "blocked activation exit code")
    payload = json.loads(completed.stdout)
    assert_equal(payload["activation_status"], "blocked", "blocked activation payload")


def main() -> None:
    tests = [
        test_contract_validation,
        test_registry_gate_profiles_and_active_templates,
        test_single_step_external_review_template,
        test_selector_external_review,
        test_selector_active_template_routes,
        test_candidate_validation_blocks_downgrades,
        test_selector_rejects_permission_and_artifact_downgrades,
        test_activation_prompt_is_only_proposed,
        test_activation_scope_follows_selected_template,
        test_activation_schema_approved_envelope_constraints,
        test_activation_requires_bounded_refs_for_approval,
        test_human_ui_and_manual_cli_approval_attribution,
        test_selector_blocks_destructive_operation,
        test_malformed_expected_artifacts_blocks_without_crashing,
        test_classifier_provenance_is_required_and_bounded,
        test_permission_monotonicity_fuzz_for_p0_selection,
        test_work_order_schema_constrains_single_step_external_review,
        test_external_review_report_schema_rejects_embedded_raw_fields,
        test_orchestrator_projection_schema_closes_redacted_objects,
        test_publication_and_code_change_report_schemas_require_evidence,
        test_workflow_template_schema_and_security_template_publication_path,
        test_workflow_run_schema_encodes_scheduler_and_activation_scope,
        test_configure_organization_facade,
        test_blocked_activation_cli_exits_nonzero,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
