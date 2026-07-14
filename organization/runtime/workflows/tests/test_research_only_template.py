#!/usr/bin/env python3
"""Focused acceptance tests for issue #28 research-only contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_ROOT = ROOT / "organization/runtime/workflows"
sys.path.insert(0, str(WORKFLOW_ROOT / "scripts"))

import work_order_builder  # noqa: E402
import workflow_selector  # noqa: E402


SCHEMA = WORKFLOW_ROOT / "schemas/research-report.schema.json"


def schema_errors(report: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return work_order_builder._validate_schema_fragment(report, schema, "$")


def valid_report() -> dict:
    return {
        "report_version": "1",
        "workflow_id": "research_only",
        "result": "findings",
        "source_refs": ["docs/research-source.md"],
        "findings": [
            {
                "summary": "The contract is internally consistent.",
                "evidence_refs": ["docs/research-source.md#result"],
            }
        ],
        "uncertainty": ["Live-provider behavior is out of scope."],
        "no_diff_completion": True,
    }


def test_valid_research_completion_matches_report_contract() -> None:
    assert schema_errors(valid_report()) == []


def test_research_mutation_and_diff_attempts_are_rejected() -> None:
    diff_report = valid_report()
    diff_report["no_diff_completion"] = False
    diff_report["code_diff"] = "forbidden mutation"
    errors = schema_errors(diff_report)
    assert "schema:$.no_diff_completion:const" in errors
    assert "schema:$.code_diff:additional_property" in errors

    classification = {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["issue-28-focused-test"],
        "task_kind": "research",
        "permission_required": "readonly",
        "external_provider_required": False,
        "publication_required": False,
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only",
        "expected_artifacts": ["research_report"],
    }
    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source="orchestrator-start",
        task_id="TSK-issue-28",
        request_id="req-issue-28",
        refs=["docs/research-source.md"],
    )
    assert envelope["activation_status"] == "approved"
    assert envelope["activation_scope"]["allowed_ops"] == {
        "edit": False,
        "commit": False,
        "push": False,
        "network": False,
    }


def main() -> None:
    tests = [
        test_valid_research_completion_matches_report_contract,
        test_research_mutation_and_diff_attempts_are_rejected,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    main()
