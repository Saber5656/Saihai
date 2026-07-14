#!/usr/bin/env python3
"""Focused acceptance tests for issue #31 security evidence contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_ROOT = ROOT / "organization/runtime/workflows"
sys.path.insert(0, str(WORKFLOW_ROOT / "scripts"))

import work_order_builder  # noqa: E402


SCHEMA = WORKFLOW_ROOT / "schemas/security-review-report.schema.json"


def schema_errors(report: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return work_order_builder._validate_schema_fragment(report, schema, "$")


def valid_report() -> dict:
    return {
        "report_version": "1",
        "workflow_id": "security_sensitive_change",
        "threat_risk_evidence": [
            {
                "threat": "Unapproved permission escalation",
                "risk": "high",
                "mitigation": "Require typed human approval evidence",
            }
        ],
        "security_review": {"status": "passed_with_residual_risk"},
        "residual_risk": "A compromised human operator remains in the trust boundary.",
        "evidence_refs": ["evidence/security-review.json"],
    }


def test_valid_security_report_matches_evidence_contract() -> None:
    assert schema_errors(valid_report()) == []


def test_missing_security_evidence_is_rejected() -> None:
    for field in (
        "threat_risk_evidence",
        "security_review",
        "residual_risk",
        "evidence_refs",
    ):
        incomplete = valid_report()
        del incomplete[field]
        assert f"schema:$.{field}:required" in schema_errors(incomplete)

    missing_status = valid_report()
    del missing_status["security_review"]["status"]
    assert "schema:$.security_review.status:required" in schema_errors(missing_status)

    empty_threats = valid_report()
    empty_threats["threat_risk_evidence"] = []
    assert "schema:$.threat_risk_evidence:min_items" in schema_errors(empty_threats)


def main() -> None:
    tests = [
        test_valid_security_report_matches_evidence_contract,
        test_missing_security_evidence_is_rejected,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    main()
