#!/usr/bin/env python3
"""Smoke tests for the offline orchestrator E2E harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path

from e2e_harness import HarnessAssertion, HarnessFeatureUnavailable, OrchestratorHarness

ROOT = Path(__file__).resolve().parents[4]
VALIDATE_ALL = ROOT / "scripts" / "validate_all.py"
VALIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
EXPECTED_VALIDATE_WORKFLOW = """name: validate

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  validation_shards:
    name: validate / shard-${{ matrix.shard_index }}
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        shard_index: [0, 1, 2, 3, 4, 5, 6, 7]
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: "3.11"
      - name: Run validation suite
        env:
          SAIHAI_ROOT: ${{ github.workspace }}
          AGENTS_VAULT_ROOT: ${{ runner.temp }}/agents-vault
          USER_VAULT_ROOT: ${{ runner.temp }}/user-vault
          SKILLS_REPO_ROOT: ${{ github.workspace }}
          SKILLS_ROOT: ${{ github.workspace }}/organization/roles
          DOTFILES_ROOT: ${{ github.workspace }}
          DEV_ROOT: ${{ runner.temp }}
          DEV_WORKTREES_ROOT: ${{ runner.temp }}/worktrees
          TASK_WORKTREE_ROOT: ${{ runner.temp }}/worktrees
        run: |
          mkdir -p "$AGENTS_VAULT_ROOT" "$USER_VAULT_ROOT" "$DEV_WORKTREES_ROOT"
          python3 scripts/validate_all.py __CONT__
            --shard-index "${{ matrix.shard_index }}" __CONT__
            --shard-count 8

  validate:
    name: validate
    runs-on: ubuntu-latest
    timeout-minutes: 5
    needs: validation_shards
    if: ${{ always() }}
    steps:
      - name: Confirm every validation shard passed
        env:
          SHARD_RESULT: ${{ needs.validation_shards.result }}
        run: test "$SHARD_RESULT" = "success"
""".replace("__CONT__", chr(92))


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


def load_json_lines(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def load_validate_all_module():
    spec = importlib.util.spec_from_file_location("validate_all", VALIDATE_ALL)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_happy_path_pre_runner() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        with OrchestratorHarness(Path(raw_tmp)) as harness:
            runner_available = "provider_runner" in harness.optional_modules
            result = harness.happy_path()
            assert_equal(result["terminal"], {"status": "complete", "reason": "report_valid"}, "terminal state")
            tree = harness.artifact_tree()
            required = {
                "requests/req-e2e.json",
                "runs/run-e2e.json",
                "work-orders/run-e2e/review.json",
            }
            if not runner_available:
                required.update(
                    {
                        "reports/run-e2e/review-external-review-report.json",
                        "provider-evidence/run-e2e/review-provider-evidence.json",
                    }
                )
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
    assert "organization/runtime/infra-team-bootstrap/tests/test_itb_bootstrap_builder.py" in listed_suites
    assert "organization/roles/infra-team-bootstrap/tests/test_itb_bootstrap_builder.py" in listed_suites

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
    assert_equal(
        len([line for line in completed.stdout.splitlines() if line.strip()]),
        1,
        "stdout JSON lines",
    )
    assert_equal(summary["result"], "pass", "validate_all filtered result")
    assert_equal(len(summary["suites"]), 1, "filtered suite count")
    assert summary["suites"][0]["duration_seconds"] >= 0
    progress = load_json_lines(completed.stderr)
    events = [item["event"] for item in progress]
    assert_equal(events[0], "validation_start", "first progress event")
    assert_equal(events[-1], "validation_complete", "last progress event")
    assert_equal(events.count("suite_start"), 1, "suite start event count")
    assert_equal(events.count("suite_complete"), 1, "suite complete event count")
    assert_equal(events.count("contract_start"), 2, "contract start event count")
    assert_equal(events.count("contract_complete"), 2, "contract complete event count")
    assert_equal(events.count("compile_start"), 1, "compile start event count")
    assert_equal(events.count("compile_complete"), 1, "compile complete event count")
    completed_suite = next(item for item in progress if item["event"] == "suite_complete")
    assert_equal(completed_suite["result"], "pass", "suite progress result")
    assert_equal(completed_suite["target"], summary["suites"][0]["path"], "suite progress target")


def test_validate_all_shards_are_complete_and_disjoint() -> None:
    validate_all = load_validate_all_module()
    suites = validate_all.discover_suites()
    shard_count = 8
    shards = [
        validate_all.select_shard(suites, index=index, count=shard_count)
        for index in range(shard_count)
    ]
    flattened = [path for shard in shards for path in shard]
    assert_equal(len(flattened), len(suites), "sharded suite count")
    assert_equal(len(set(flattened)), len(suites), "sharded suite uniqueness")
    assert_equal(set(flattened), set(suites), "sharded suite coverage")

    filtered = [path for path in suites if "runtime/workflows/tests" in validate_all.rel(path)]
    filtered_shards = [
        validate_all.select_shard(filtered, index=index, count=shard_count)
        for index in range(shard_count)
    ]
    assert_equal(
        {path for shard in filtered_shards for path in shard},
        set(filtered),
        "filter-before-shard coverage",
    )
    for position, path in enumerate(filtered):
        assert path in filtered_shards[position % shard_count], (position, path)

    for index, count in ((shard_count, shard_count), (-1, shard_count), (0, 0), (0, -1)):
        try:
            validate_all.select_shard(suites, index=index, count=count)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid shard {index}/{count} should fail")


def test_validate_all_shard_cli_rejects_invalid_pairs() -> None:
    invalid_args = [
        ["--shard-index", "0"],
        ["--shard-count", "8"],
        ["--shard-index", "-1", "--shard-count", "8"],
        ["--shard-index", "8", "--shard-count", "8"],
        ["--shard-index", "0", "--shard-count", "0"],
        ["--shard-index", "not-an-integer", "--shard-count", "8"],
    ]
    for arguments in invalid_args:
        completed = subprocess.run(
            [sys.executable, str(VALIDATE_ALL), *arguments, "--list"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_equal(completed.returncode, 2, f"invalid CLI {arguments}")


def test_validate_all_progress_flushes_before_process_exit() -> None:
    script = f"""
import importlib.util
import time
spec = importlib.util.spec_from_file_location('validate_all_progress_probe', {str(VALIDATE_ALL)!r})
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
module.emit_progress('probe', target='stream')
time.sleep(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stderr is not None
    first = json.loads(process.stderr.readline())
    assert_equal(
        first,
        {"schema_version": 1, "event": "probe", "target": "stream"},
        "flushed progress",
    )
    assert process.poll() is None, "progress must arrive before process exit"
    stdout, stderr = process.communicate(timeout=5)
    assert_equal(process.returncode, 0, "progress probe exit")
    assert_equal(stdout, "", "progress stdout isolation")
    assert_equal(stderr, "", "progress stderr remainder")


def test_validate_workflow_requires_every_shard() -> None:
    workflow = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
    assert_equal(workflow, EXPECTED_VALIDATE_WORKFLOW, "exact validate workflow contract")
    for result, expected in (
        ("success", 0),
        ("failure", 1),
        ("cancelled", 1),
        ("skipped", 1),
        ("", 1),
    ):
        completed = subprocess.run(
            ["bash", "-c", 'test "$SHARD_RESULT" = "success"'],
            env={**os.environ, "SHARD_RESULT": result},
            check=False,
        )
        assert_equal(completed.returncode, expected, f"aggregate result {result!r}")


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
    progress = load_json_lines(completed.stderr)
    assert_equal(progress[0]["event"], "validation_start", "failure first progress event")
    assert_equal(progress[-1]["event"], "validation_complete", "failure last progress event")
    assert_equal(progress[-1]["result"], "fail", "failure terminal progress result")


def test_validate_all_rejects_empty_zero_exit_suite() -> None:
    validate_all = load_validate_all_module()
    original_run = validate_all.subprocess.run

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    validate_all.subprocess.run = lambda *args, **kwargs: Completed()
    try:
        result = validate_all.run_suite(ROOT / "tests" / "test_empty_fixture.py")
    finally:
        validate_all.subprocess.run = original_run
    assert_equal(result["result"], "fail", "empty zero-exit suite result")
    assert_equal(
        result["detail"],
        "exit_zero_no_result_json",
        "empty zero-exit suite detail",
    )


def test_validate_all_tail_decodes_timeout_bytes() -> None:
    validate_all = load_validate_all_module()
    assert_equal(validate_all.tail(b"prefix\nbytes-stderr"), "prefix\nbytes-stderr", "bytes tail")
    assert_equal(validate_all.tail(None), "", "none tail")
    assert_equal(
        validate_all.parse_unittest_cases("", "Ran 19 tests in 2.0s\n\nOK"),
        19,
        "unittest case count",
    )


def test_validate_all_contract_timeout_is_reported() -> None:
    validate_all = load_validate_all_module()
    result = validate_all.run_contract(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        timeout=0.01,
    )
    assert_equal(result["result"], "fail", "contract timeout result")
    assert_equal(result["detail"], "timeout", "contract timeout detail")
    json.dumps(result)


def main() -> None:
    tests = [
        test_happy_path_pre_runner,
        test_happy_path_uses_runner_when_available,
        test_harness_assertion_carries_response,
        test_validate_all_list_and_run,
        test_validate_all_shards_are_complete_and_disjoint,
        test_validate_all_shard_cli_rejects_invalid_pairs,
        test_validate_all_progress_flushes_before_process_exit,
        test_validate_workflow_requires_every_shard,
        test_validate_all_fails_on_broken_suite,
        test_validate_all_rejects_empty_zero_exit_suite,
        test_validate_all_tail_decodes_timeout_bytes,
        test_validate_all_contract_timeout_is_reported,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
