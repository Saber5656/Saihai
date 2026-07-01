#!/usr/bin/env python3
"""Unit/static tests for Orchestrator P0 workflow contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "organization/runtime/workflows/scripts/workflow_selector.py"
FACADE = ROOT / "scripts/configure_organization.py"
TEMPLATE = ROOT / "organization/runtime/workflows/templates/single_step_external_review.yaml"
WORK_ORDER_SCHEMA = ROOT / "organization/runtime/workflows/schemas/work-order.schema.json"
WORKFLOW_RUN_SCHEMA = ROOT / "organization/runtime/workflows/schemas/workflow-run.schema.json"
ACTIVATION_SCHEMA = ROOT / "organization/runtime/workflows/schemas/activation-envelope.schema.json"


def load_selector_module():
    spec = importlib.util.spec_from_file_location("workflow_selector", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


selector = load_selector_module()


def external_review_classification(**overrides):
    candidate = {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["test-fixture"],
        "task_kind": "external_review",
        "permission_required": "readonly",
        "external_provider_required": True,
        "publication_required": False,
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only",
        "expected_artifacts": ["typed_report"],
    }
    candidate.update(overrides)
    return candidate


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


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
    assert_equal(contracts["decision"], "ok", "contracts decision")
    assert_equal(contracts["workflow_contracts"]["template_count"], 1, "template count")


def test_single_step_external_review_template() -> None:
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert_equal(template["workflow_id"], "single_step_external_review", "workflow_id")
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
    selection = selector.select_workflow(external_review_classification())
    assert_equal(selection["decision"], "selected", "selection decision")
    assert_equal(
        selection["workflow_selection"]["workflow_id"],
        "single_step_external_review",
        "selected workflow",
    )
    assert_equal(selection["workflow_selection"]["initial_step"], "review", "initial step")
    assert_equal(selection["policy"]["bounded_provider_transport"], "allowed", "provider policy")

    security_selection = selector.select_workflow(
        external_review_classification(security_sensitive=True)
    )
    assert_equal(
        security_selection["workflow_selection"]["optional_expansions"],
        ["security_focus"],
        "security expansion",
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


def test_activation_schema_approved_envelope_constraints() -> None:
    schema = json.loads(ACTIVATION_SCHEMA.read_text(encoding="utf-8"))
    approved_condition = json.dumps(schema["allOf"], sort_keys=True)
    for fragment in (
        '"status": {"const": "selected"}',
        '"workflow_id"',
        '"initial_step"',
        '"next_action": {"const": "create_workflow_run"}',
        '"minItems": 1',
    ):
        assert fragment in approved_condition, f"missing approved activation constraint {fragment}"
    assert "classification_provenance" in schema["required"]
    assert '"activation_source"' in approved_condition
    assert '"enum": ["orchestrator-start", "human_ui", "manual_cli"]' in approved_condition


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


def test_orchestrator_start_can_approve_bounded_external_review() -> None:
    approved = selector.activation_envelope(
        external_review_classification(),
        activation_source="orchestrator-start",
        task_id="TSK-test",
        request_id="req-test",
        refs=["organization/runtime/workflows/README.md"],
    )
    assert_equal(approved["activation_status"], "approved", "explicit activation")
    assert_equal(approved["next_action"], "create_workflow_run", "explicit next action")
    assert_equal(approved["approved_by"], "human_explicit_skill_invocation", "approval source")
    assert_equal(
        approved["context_scope"]["raw_transcript_sharing"],
        "forbidden",
        "raw transcript sharing",
    )
    assert_equal(
        approved["activation_scope"]["allowed_ops"],
        {"edit": False, "commit": False, "push": False, "network": False},
        "approved allowed ops",
    )
    assert_equal(approved["activation_scope"]["step_budget"], 1, "approved step budget")


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


def test_selector_blocks_destructive_and_publication_gate() -> None:
    destructive = selector.select_workflow(
        external_review_classification(destructive_operation=True)
    )
    assert_equal(destructive["decision"], "blocked", "destructive decision")
    assert_equal(
        destructive["workflow_selection"]["reason"],
        "destructive_operation_requires_separate_approval",
        "destructive reason",
    )

    publication = selector.select_workflow(
        external_review_classification(publication_required=True)
    )
    assert_equal(publication["decision"], "waiting_human", "publication decision")
    assert_equal(
        publication["policy"]["publication"],
        "separate_gate_required",
        "publication policy",
    )


def test_p0_waits_for_planned_code_change_template() -> None:
    code_change = selector.select_workflow(
        {
            "classification_version": "1",
            "classification_source": "deterministic_fixture",
            "classification_confidence": 1.0,
            "classification_evidence": ["test-fixture"],
            "task_kind": "code_change",
            "permission_required": "edit",
            "external_provider_required": False,
            "publication_required": False,
            "security_sensitive": False,
            "destructive_operation": False,
            "context_scope": "diff_summary",
            "expected_artifacts": ["code_diff", "validation_result", "vault_update"],
        }
    )
    assert_equal(code_change["decision"], "waiting_human", "code change p0 decision")
    assert_equal(
        code_change["workflow_selection"]["candidates"],
        ["standard_code_change"],
        "planned code change workflow",
    )

    security_sensitive_code = selector.select_workflow(
        {
            "classification_version": "1",
            "classification_source": "deterministic_fixture",
            "classification_confidence": 1.0,
            "classification_evidence": ["test-fixture"],
            "task_kind": "code_change",
            "permission_required": "edit",
            "external_provider_required": False,
            "publication_required": False,
            "security_sensitive": True,
            "destructive_operation": False,
            "context_scope": "diff_summary",
            "expected_artifacts": ["code_diff", "validation_result", "vault_update"],
        }
    )
    assert_equal(
        security_sensitive_code["workflow_selection"]["candidates"],
        ["security_sensitive_change"],
        "security-sensitive code change workflow",
    )

    security_sensitive_policy = selector.select_workflow(
        {
            "classification_version": "1",
            "classification_source": "deterministic_fixture",
            "classification_confidence": 1.0,
            "classification_evidence": ["test-fixture"],
            "task_kind": "policy_change",
            "permission_required": "readonly",
            "external_provider_required": False,
            "publication_required": False,
            "security_sensitive": True,
            "destructive_operation": False,
            "context_scope": "refs_only",
            "expected_artifacts": ["typed_report"],
        }
    )
    assert_equal(
        security_sensitive_policy["workflow_selection"]["candidates"],
        ["security_sensitive_change"],
        "security-sensitive policy workflow",
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


def test_external_review_report_schema_rejects_embedded_raw_fields() -> None:
    report_schema = json.loads(
        (
            ROOT
            / "organization/runtime/workflows/schemas/external-review-report.schema.json"
        ).read_text(encoding="utf-8")
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


def test_workflow_run_schema_encodes_p0_scheduler() -> None:
    workflow_run_schema = json.loads(WORKFLOW_RUN_SCHEMA.read_text(encoding="utf-8"))
    scheduling = workflow_run_schema["properties"]["scheduling"]["properties"]
    assert_equal(scheduling["scheduler_mode"]["const"], "invocation-drain", "scheduler mode")
    assert_equal(scheduling["lock_policy"]["const"], "global_advisory_lock", "lock policy")
    assert_equal(scheduling["state_persistence"]["const"], "durable_state", "scheduler state persistence")
    assert_equal(scheduling["concurrency"]["const"], 1, "concurrency")
    activation = json.dumps(
        workflow_run_schema["properties"]["activation"],
        sort_keys=True,
    )
    for fragment in (
        '"activation_status": {"const": "approved"}',
        '"status": {"const": "selected"}',
        '"next_action": {"const": "create_workflow_run"}',
    ):
        assert fragment in activation, f"missing run activation constraint {fragment}"
    assert "classification_provenance" in workflow_run_schema["properties"]["activation"]["required"]
    assert '"activation_source"' in activation
    assert '"enum": ["orchestrator-start", "human_ui", "manual_cli"]' in activation


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
        test_single_step_external_review_template,
        test_selector_external_review,
        test_activation_prompt_is_only_proposed,
        test_activation_schema_approved_envelope_constraints,
        test_activation_requires_bounded_refs_for_approval,
        test_orchestrator_start_can_approve_bounded_external_review,
        test_human_ui_and_manual_cli_approval_attribution,
        test_selector_blocks_destructive_and_publication_gate,
        test_p0_waits_for_planned_code_change_template,
        test_malformed_expected_artifacts_blocks_without_crashing,
        test_classifier_provenance_is_required_and_bounded,
        test_permission_monotonicity_fuzz_for_p0_selection,
        test_work_order_schema_constrains_single_step_external_review,
        test_external_review_report_schema_rejects_embedded_raw_fields,
        test_workflow_run_schema_encodes_p0_scheduler,
        test_configure_organization_facade,
        test_blocked_activation_cli_exits_nonzero,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
