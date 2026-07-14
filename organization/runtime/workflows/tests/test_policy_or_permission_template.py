#!/usr/bin/env python3
"""Focused acceptance tests for issue #30 policy approval contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_ROOT = ROOT / "organization/runtime/workflows"
sys.path.insert(0, str(WORKFLOW_ROOT / "scripts"))

import work_order_builder  # noqa: E402


SCHEMA = WORKFLOW_ROOT / "schemas/policy-change-report.schema.json"
TEMPLATE = WORKFLOW_ROOT / "templates/policy_or_permission_change.yaml"


def schema_errors(report: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return work_order_builder._validate_schema_fragment(report, schema, "$")


def valid_report() -> dict:
    return {
        "report_version": "1",
        "workflow_id": "policy_or_permission_change",
        "human_approval": {
            "approved_before_mutation": True,
            "source": "human",
        },
        "policy_diff": {"summary": "Tighten the bounded permission rule."},
        "result": "complete",
        "evidence_refs": ["evidence/policy-approval.json"],
    }


def test_approved_report_and_transition_precede_mutation() -> None:
    assert schema_errors(valid_report()) == []

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    steps = {step["id"]: step for step in template["steps"]}
    assert template["initial_step"] == "policy_review"
    assert steps["policy_review"]["permission_mode"] == "readonly"
    assert {"on": "approval_recorded", "to": "apply_policy_change"} in steps[
        "policy_review"
    ]["transitions"]
    assert steps["apply_policy_change"]["permission_mode"] == "edit"


def test_missing_or_false_approval_is_rejected_or_waits_for_human() -> None:
    missing = valid_report()
    del missing["human_approval"]
    assert "schema:$.human_approval:required" in schema_errors(missing)

    false_approval = valid_report()
    false_approval["human_approval"]["approved_before_mutation"] = False
    assert (
        "schema:$.human_approval.approved_before_mutation:const"
        in schema_errors(false_approval)
    )

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    policy_review = next(step for step in template["steps"] if step["id"] == "policy_review")
    assert {
        "on": "approval_missing",
        "to": "waiting_human",
        "reason_class": "human_approval_required",
    } in policy_review["transitions"]


def main() -> None:
    tests = [
        test_approved_report_and_transition_precede_mutation,
        test_missing_or_false_approval_is_rejected_or_waits_for_human,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    main()
