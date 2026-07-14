#!/usr/bin/env python3
"""Focused acceptance tests for issue #27 standard code-change transitions."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = ROOT / "organization/runtime/workflows/templates/standard_code_change.yaml"


def load_template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def transitions_by_step(template: dict) -> dict[str, list[dict]]:
    return {step["id"]: step["transitions"] for step in template["steps"]}


def test_happy_path_reaches_final_evidence_and_complete() -> None:
    template = load_template()
    transitions = transitions_by_step(template)

    assert template["initial_step"] == "implement"
    assert {step["id"] for step in template["steps"]} == {
        "implement",
        "review",
        "qa",
        "final_evidence",
    }
    assert {"on": "implementation_complete", "to": "review"} in transitions["implement"]
    assert {"on": "review_complete", "to": "qa"} in transitions["review"]
    assert {"on": "qa_complete", "to": "final_evidence"} in transitions["qa"]
    assert {"on": "final_evidence_valid", "to": "complete"} in transitions["final_evidence"]


def test_blocked_and_waiting_paths_are_terminal() -> None:
    template = load_template()
    transitions = transitions_by_step(template)

    assert {
        "on": "scope_violation",
        "to": "waiting_human",
        "reason_class": "scope_violation",
    } in transitions["implement"]
    assert {
        "on": "final_evidence_invalid",
        "to": "blocked",
        "reason_class": "missing_final_evidence",
    } in transitions["final_evidence"]
    assert set(template["terminal_states"]) == {
        "complete",
        "blocked",
        "waiting_human",
        "aborted",
    }


def main() -> None:
    tests = [
        test_happy_path_reaches_final_evidence_and_complete,
        test_blocked_and_waiting_paths_are_terminal,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    main()
