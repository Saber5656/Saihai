#!/usr/bin/env python3
"""Smoke tests for the offline orchestrator E2E harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from e2e_harness import HarnessAssertion, HarnessFeatureUnavailable, OrchestratorHarness

ROOT = Path(__file__).resolve().parents[4]
VALIDATE_ALL = ROOT / "scripts" / "validate_all.py"


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def load_last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"no JSON object found in stdout: {stdout!r}")


def test_happy_path_pre_runner() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        with OrchestratorHarness(Path(raw_tmp)) as harness:
            result = harness.happy_path()
            assert_equal(result["terminal"], {"status": "complete", "reason": "report_valid"}, "terminal state")
            tree = harness.artifact_tree()
            required = {
                "requests/req-e2e.json",
                "runs/run-e2e.json",
                "work-orders/run-e2e/review.json",
                "reports/run-e2e/review-external-review-report.json",
                "provider-evidence/run-e2e/review-provider-evidence.json",
            }
            missing = sorted(required - set(tree))
            assert_equal(missing, [], "artifact tree")
            assert not any(path.endswith(".tmp") or ".tmp" in Path(path).name for path in tree)


def test_happy_path_uses_runner_when_available() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        with OrchestratorHarness(Path(raw_tmp)) as harness:
            if "provider_runner" not in harness.optional_modules:
                try:
                    harness.run_step("run-missing")
                except HarnessFeatureUnavailable:
                    pass
                else:
                    raise AssertionError("missing provider_runner should raise HarnessFeatureUnavailable")
            result = harness.happy_path()
            responses = result["responses"]
            if "provider_runner" in harness.optional_modules:
                assert "run_step" in responses
            else:
                assert "place_report" in responses
                assert "validate_report" in responses


def test_harness_assertion_carries_response() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        with OrchestratorHarness(Path(raw_tmp)) as harness:
            harness.propose(request_id="req-bad-approval")
            try:
                harness.approve("req-bad-approval", human_action_id="wrong-challenge")
            except HarnessAssertion as exc:
                message = str(exc)
                assert "blocked" in message
                assert "approval challenge mismatch" in message
            else:
                raise AssertionError("wrong approval challenge should fail")


def test_validate_all_list_and_run() -> None:
    listed = subprocess.run(
        [sys.executable, str(VALIDATE_ALL), "--list"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    listed_suites = [line for line in listed.stdout.splitlines() if line.strip()]
    assert len(listed_suites) >= 3, listed.stdout
    assert "organization/runtime/workflows/tests/test_e2e_harness.py" in listed_suites

    if os.environ.get("SAIHAI_VALIDATE_ALL_CHILD"):
        return

    completed = subprocess.run(
        [sys.executable, str(VALIDATE_ALL), "--only", "test_e2e_harness"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    summary = load_last_json(completed.stdout)
    assert_equal(summary["result"], "pass", "validate_all filtered result")
    assert_equal(len(summary["suites"]), 1, "filtered suite count")
    assert summary["suites"][0]["duration_seconds"] >= 0


def test_validate_all_fails_on_broken_suite() -> None:
    completed = subprocess.run(
        [sys.executable, str(VALIDATE_ALL), "--only", "no_such_suite"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_equal(completed.returncode, 1, "empty filter exit")
    summary = load_last_json(completed.stdout)
    assert_equal(summary["result"], "fail", "empty filter result")
    assert_equal(summary["detail"], "no_suites_matched", "empty filter detail")


def main() -> None:
    tests = [
        test_happy_path_pre_runner,
        test_happy_path_uses_runner_when_available,
        test_harness_assertion_carries_response,
        test_validate_all_list_and_run,
        test_validate_all_fails_on_broken_suite,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
