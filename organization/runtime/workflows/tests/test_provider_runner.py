#!/usr/bin/env python3
"""Tests for headless provider runner dispatch and evidence boundaries."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import provider_runner

from test_frontdoor_orchestrator import (
    assert_equal,
    external_review_classification,
    load_payload,
    run_frontdoor,
)


def prepare_run(state_root: Path, *, request_id: str, run_id: str) -> None:
    proposed = load_payload(
        run_frontdoor(
            state_root,
            "propose",
            "--task-id",
            f"TSK-{request_id}",
            "--request-id",
            request_id,
            "--prompt",
            "Run bounded external review",
            "--classification",
            json.dumps(external_review_classification()),
            "--ref",
            "organization/runtime/workflows/README.md",
        )
    )
    load_payload(
        run_frontdoor(
            state_root,
            "approve",
            "--request-id",
            request_id,
            "--human-action-id",
            proposed["approval"]["human_action_id"],
        )
    )
    load_payload(run_frontdoor(state_root, "create-run", "--request-id", request_id, "--run-id", run_id))
    load_payload(run_frontdoor(state_root, "drain", "--run-id", run_id))


def run_provider(
    state_root: Path,
    *,
    run_id: str,
    adapter_id: str = "claude_headless_p0",
    mode: str = "success",
    check: bool = True,
):
    return run_frontdoor(
        state_root,
        "run-provider",
        "--run-id",
        run_id,
        "--adapter-id",
        adapter_id,
        "--fake-provider-mode",
        mode,
        check=check,
    )


def test_fake_provider_success_completes_with_normalized_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-ok", run_id="run-provider-ok")

        payload = load_payload(run_provider(state_root, run_id="run-provider-ok"))

        assert_equal(payload["decision"], "ok", "runner decision")
        assert_equal(payload["report_gate"]["outcome"], "report_valid", "gate outcome")
        assert_equal(payload["workflow_run"]["run_state"], "complete", "run state")
        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        assert_equal(evidence["provider_adapter_id"], "claude_headless_p0", "adapter id")
        assert_equal(evidence["provider_target"], "claude_headless", "provider target")
        assert_equal(evidence["outcome"], "ok", "evidence outcome")
        assert "provider_request_id" in evidence
        assert "duration_ms" in evidence
        serialized_run = json.dumps(payload["workflow_run"], ensure_ascii=False)
        assert "raw transcript" not in serialized_run.lower()
        assert "Fake provider completed" not in serialized_run


def test_runner_dispatches_through_adapter_metadata() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-cursor", run_id="run-provider-cursor")

        payload = load_payload(
            run_provider(
                state_root,
                run_id="run-provider-cursor",
                adapter_id="cursor_cli_p0",
            )
        )

        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        report = json.loads(Path(payload["report_path"]).read_text(encoding="utf-8"))
        assert_equal(evidence["provider_adapter_id"], "cursor_cli_p0", "adapter id")
        assert_equal(evidence["provider_target"], "cursor_cli", "provider target")
        assert_equal(report["provider_evidence"]["provider"], "cursor_cli", "report provider")


def test_hermes_evidence_records_bridge_pattern_without_async_claim() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-hermes", run_id="run-provider-hermes")

        payload = load_payload(
            run_provider(
                state_root,
                run_id="run-provider-hermes",
                adapter_id="hermes_agent_oneshot_p0",
            )
        )

        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        assert_equal(evidence["provider_target"], "hermes_agent", "provider target")
        assert_equal(evidence["bridge_pattern"], "oneshot", "bridge pattern")
        assert_equal(evidence["surface_metadata"]["async_callback_supported"], False, "async claim")


def test_provider_unavailable_waits_for_human() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-missing", run_id="run-provider-missing")

        blocked = run_provider(
            state_root,
            run_id="run-provider-missing",
            mode="unavailable",
            check=False,
        )
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "unavailable exit")
        assert_equal(payload["reason"], "provider_unavailable", "reason")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "run state")


def test_malformed_provider_output_fails_without_raw_stdout_in_run() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-bad", run_id="run-provider-bad")

        blocked = run_provider(
            state_root,
            run_id="run-provider-bad",
            mode="malformed",
            check=False,
        )
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "malformed exit")
        assert_equal(payload["reason"], "provider_malformed_output", "reason")
        assert_equal(payload["workflow_run"]["run_state"], "failed", "run state")
        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        assert "stdout_sha256" in evidence
        assert "not json" not in json.dumps(payload["workflow_run"], ensure_ascii=False)


def test_run_provider_rejects_non_runnable_run_state() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-complete", run_id="run-provider-complete")
        load_payload(run_provider(state_root, run_id="run-provider-complete"))

        blocked = run_provider(state_root, run_id="run-provider-complete", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "non-runnable exit")
        assert_equal(payload["reason"], "run_not_runnable", "non-runnable reason")
        assert_equal(payload["run_state"], "complete", "non-runnable state")


def test_runner_rejects_noncanonical_report_path_before_provider_output() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-report-path", run_id="run-provider-report-path")
        work_order_path = state_root / "work-orders" / "run-provider-report-path" / "review.json"
        work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
        escaped_report_path = state_root / "outside-report.json"
        work_order["report_path"] = str(escaped_report_path)
        work_order_path.write_text(json.dumps(work_order, ensure_ascii=False) + "\n", encoding="utf-8")

        blocked = run_provider(state_root, run_id="run-provider-report-path", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "report path exit")
        assert_equal(payload["reason"], "work_order_not_provider_safe", "report path reason")
        assert "report_path must stay under reports" in payload["errors"]
        assert not escaped_report_path.exists()
        assert not (state_root / "adapter-requests" / "run-provider-report-path").exists()


def test_live_command_adapter_is_rejected_until_sandbox_support() -> None:
    outcome, report, details = provider_runner.execute_provider(
        request={"request_id": "req-live", "run_id": "run-live", "workflow_id": "single_step_external_review", "step_id": "review"},
        adapter={"command_argv": ["python3", "-c", "print('{}')"]},
        timeout_seconds=1,
        fake_provider_mode="",
    )
    assert_equal(outcome, "provider_unavailable", "live command outcome")
    assert report is None
    assert_equal(details["reason"], "live_command_adapter_requires_sandbox", "live command reason")


def test_undecodable_provider_stdout_is_malformed_output() -> None:
    outcome, report, details = provider_runner.parse_provider_stdout(b"\xff")
    assert_equal(outcome, "provider_malformed_output", "binary malformed outcome")
    assert report is None
    assert "stdout_sha256" in details


if __name__ == "__main__":
    tests = (
        test_fake_provider_success_completes_with_normalized_evidence,
        test_runner_dispatches_through_adapter_metadata,
        test_hermes_evidence_records_bridge_pattern_without_async_claim,
        test_provider_unavailable_waits_for_human,
        test_malformed_provider_output_fails_without_raw_stdout_in_run,
        test_run_provider_rejects_non_runnable_run_state,
        test_runner_rejects_noncanonical_report_path_before_provider_output,
        test_live_command_adapter_is_rejected_until_sandbox_support,
        test_undecodable_provider_stdout_is_malformed_output,
    )
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))
