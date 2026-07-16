#!/usr/bin/env python3
"""Tests for headless provider runner dispatch and evidence boundaries."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import provider_runner
import run_lifecycle
import run_store
import work_order_builder

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
        assert_equal(evidence["evidence_version"], "1", "evidence version")
        assert "provider_evidence_version" not in evidence
        assert_equal(evidence["provider_adapter_id"], "claude_headless_p0", "adapter id")
        assert_equal(evidence["provider_target"], "claude_headless", "provider target")
        assert_equal(evidence["request_id"], "req-provider-ok", "evidence request id")
        assert_equal(evidence["run_id"], "run-provider-ok", "evidence run id")
        assert_equal(evidence["workflow_id"], "single_step_external_review", "evidence workflow id")
        assert_equal(evidence["step_id"], "review", "evidence step id")
        assert_equal(
            Path(evidence["evidence_path"]).resolve(),
            Path(payload["evidence_path"]).resolve(),
            "evidence self path",
        )
        assert_equal(
            Path(evidence["transcript_path"]).resolve(),
            Path(payload["transcript_path"]).resolve(),
            "evidence transcript path",
        )
        assert isinstance(evidence["duration_ms"], (int, float))
        assert not isinstance(evidence["duration_ms"], bool)
        assert isinstance(evidence["usage"], dict)
        assert_equal(evidence["outcome"], "ok", "evidence outcome")
        assert_equal(
            evidence["raw_transcript_policy"],
            "signal_only_not_shared",
            "raw transcript policy",
        )
        assert "provider_request_id" in evidence
        assert "duration_ms" in evidence
        serialized_run = json.dumps(payload["workflow_run"], ensure_ascii=False)
        assert "raw transcript" not in serialized_run.lower()
        assert "Fake provider completed" not in serialized_run
        completion = load_payload(
            run_frontdoor(
                state_root,
                "verify-completion",
                "--run-id",
                "run-provider-ok",
            )
        )
        assert_equal(completion["decision"], "complete", "completion decision")
        assert_equal(
            completion["evidence"]["verification_decision"],
            "complete",
            "completion evidence decision",
        )


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


def test_runner_transition_request_wins_over_manual_adapter_candidate() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-current-request"
        prepare_run(state_root, request_id="req-provider-current-request", run_id=run_id)
        manual = load_payload(
            run_frontdoor(state_root, "prepare-claude-adapter", "--run-id", run_id)
        )
        manual_path = Path(manual["adapter_request_path"])

        payload = load_payload(
            run_provider(
                state_root,
                run_id=run_id,
                adapter_id="cursor_cli_p0",
            )
        )

        runner_path = Path(payload["adapter_request_path"])
        assert manual_path.exists(), "manual request should remain as a bounded fallback artifact"
        assert runner_path.exists(), "runner request should exist"
        assert manual_path != runner_path, "test requires two adapter request candidates"
        assert_equal(payload["report_gate"]["outcome"], "report_valid", "current request gate")
        assert_equal(
            payload["provider_evidence"]["provider_adapter_id"],
            "cursor_cli_p0",
            "transition-selected adapter",
        )


def test_waiting_provider_retry_records_fresh_request_authority() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-retry-authority"
        prepare_run(state_root, request_id="req-provider-retry-authority", run_id=run_id)
        run = run_store.load_run(state_root, run_id)
        step_id = str(run["current_step"])
        order_path = provider_runner.work_order_path(state_root, run_id, step_id)
        work_order = provider_runner.read_json(order_path)
        principal = {
            "principal_type": "harness_runner",
            "principal_id": "provider-retry-test",
            "authn_method": "local_test",
        }
        first_adapter = provider_runner.load_provider_adapters()["claude_headless_p0"]
        first_request = provider_runner.adapter_request(
            state_root=state_root,
            run=run,
            work_order=work_order,
            adapter=first_adapter,
            principal=principal,
        )
        first_request_path = provider_runner.adapter_request_path(
            state_root,
            run_id,
            step_id,
            "claude_headless_p0",
        )
        run_store.atomic_write_json(first_request_path, first_request)
        run_lifecycle.transition_run(
            state_root,
            run_id,
            to_state="waiting_provider",
            reason_class="provider_invoked",
            transition="run_provider",
            principal=principal,
            artifact_refs=[str(order_path), str(first_request_path)],
            run=run,
        )

        payload = load_payload(
            run_provider(
                state_root,
                run_id=run_id,
                adapter_id="cursor_cli_p0",
            )
        )

        current_request_path = Path(payload["adapter_request_path"])
        provider_transitions = [
            item
            for item in payload["workflow_run"]["transitions"]
            if item.get("transition") == "run_provider"
        ]
        assert_equal(len(provider_transitions), 2, "provider transition count")
        assert str(current_request_path) in provider_transitions[-1]["artifact_refs"]
        assert_equal(payload["report_gate"]["outcome"], "report_valid", "retry gate outcome")
        assert_equal(
            payload["provider_evidence"]["provider_adapter_id"],
            "cursor_cli_p0",
            "retry adapter authority",
        )


def test_completion_rejects_tampered_runner_evidence_identity_path_and_type() -> None:
    def change_request_id(evidence: dict, _state_root: Path) -> None:
        evidence["request_id"] = "req-other"

    def change_self_path(evidence: dict, state_root: Path) -> None:
        evidence["evidence_path"] = str(state_root / "provider-evidence/wrong.json")

    def change_transcript_path(evidence: dict, state_root: Path) -> None:
        evidence["transcript_path"] = str(state_root / "provider-evidence/wrong-transcript.json")

    def change_duration_type(evidence: dict, _state_root: Path) -> None:
        evidence["duration_ms"] = "12"

    variants = (
        ("request-id", change_request_id, "normalized_evidence.request_id mismatch"),
        ("self-path", change_self_path, "must reference its own artifact"),
        ("transcript-path", change_transcript_path, "must match current run transcript path"),
        ("duration-type", change_duration_type, "schema:$.duration_ms:type"),
    )
    for name, mutate, expected_error in variants:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-provider-tampered-{name}"
            prepare_run(
                state_root,
                request_id=f"req-provider-tampered-{name}",
                run_id=run_id,
            )
            runner_payload = load_payload(run_provider(state_root, run_id=run_id))
            evidence_path = Path(runner_payload["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            mutate(evidence, state_root)
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            blocked_process = run_frontdoor(
                state_root,
                "verify-completion",
                "--run-id",
                run_id,
                check=False,
            )
            blocked = load_payload(blocked_process)
            assert_equal(blocked_process.returncode, 2, f"{name} completion exit")
            assert_equal(blocked["decision"], "blocked", f"{name} completion decision")
            invalid_details = [
                item["detail"]
                for item in blocked["reasons"]
                if item["reason_class"] == "invalid_provider_evidence"
            ]
            assert any(expected_error in detail for detail in invalid_details), (
                f"{name} missing invalid evidence detail: {invalid_details}"
            )


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


def test_provider_nonzero_exit_waits_for_human() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-nonzero", run_id="run-provider-nonzero")

        blocked = run_provider(
            state_root,
            run_id="run-provider-nonzero",
            mode="nonzero",
            check=False,
        )
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "nonzero exit")
        assert_equal(payload["reason"], "provider_nonzero_exit", "reason")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "run state")


def test_missing_provider_report_waits_for_human() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-no-report", run_id="run-provider-no-report")
        original_execute_provider = provider_runner.execute_provider
        provider_runner.execute_provider = lambda **_kwargs: ("ok", None, {"duration_ms": 1})
        try:
            payload = provider_runner.run_provider_step(
                state_root=state_root,
                run_id="run-provider-no-report",
                adapter="fake_pass",
            )
        finally:
            provider_runner.execute_provider = original_execute_provider

        assert_equal(payload["decision"], "blocked", "missing report decision")
        assert_equal(payload["reason"], "report_not_written", "missing report reason")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "missing report state")
        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        assert_equal(evidence["outcome"], "report_not_written", "missing report evidence")


def test_live_runner_claim_blocks_second_provider() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-claimed"
        prepare_run(state_root, request_id="req-provider-claimed", run_id=run_id)
        run = run_store.load_run(state_root, run_id)
        run_lifecycle.transition_run(
            state_root,
            run_id,
            to_state="waiting_provider",
            reason_class="provider_invoked",
            transition="test_provider_claim",
            principal={"principal_type": "harness_runner", "principal_id": "first-runner", "authn_method": "local_test"},
            run=run,
        )
        order_path = run_lifecycle.work_order_path(state_root, run_id, "review")
        work_order = json.loads(order_path.read_text(encoding="utf-8"))
        work_order["work_order_authority"]["runner_claim"] = {
            "claim_state": "claimed",
            "lease_id": "lease-first-runner",
            "lease_expires_at": "2999-01-01T00:00:00+0000",
        }
        run_store.atomic_write_json(order_path, work_order)
        before = run_store.run_path(state_root, run_id).read_bytes()

        blocked = run_provider(state_root, run_id=run_id, check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "claimed provider exit")
        assert_equal(payload["reason"], "provider_in_flight", "claimed provider reason")
        assert_equal(payload["run_state"], "waiting_provider", "claimed provider state")
        assert_equal(run_store.run_path(state_root, run_id).read_bytes(), before, "claimed run unchanged")


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
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "run state")
        retry = payload["workflow_run"]["provider_execution"]["retry"]
        assert_equal(retry["auto_retries_used"], 5, "bounded retries")
        assert_equal(retry["consecutive_failures"], 6, "same failure count")
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
        assert_equal(payload["reason"], "work_order_snapshot_mismatch", "report path reason")
        assert not escaped_report_path.exists()
        assert not (state_root / "adapter-requests" / "run-provider-report-path").exists()


def test_runner_reports_unreadable_work_order_as_blocked_json() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-bad-order", run_id="run-provider-bad-order")
        work_order_path = state_root / "work-orders" / "run-provider-bad-order" / "review.json"
        work_order_path.write_text("{not-json\n", encoding="utf-8")

        blocked = run_provider(state_root, run_id="run-provider-bad-order", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "unreadable work order exit")
        assert_equal(payload["decision"], "blocked", "unreadable work order decision")
        assert_equal(payload["reason"], "work_order_unavailable", "unreadable work order reason")


def test_arbitrary_live_command_adapter_is_rejected() -> None:
    outcome, report, details = provider_runner.execute_provider(
        request={"request_id": "req-live", "run_id": "run-live", "workflow_id": "single_step_external_review", "step_id": "review"},
        adapter={"command_argv": ["python3", "-c", "print('{}')"]},
        timeout_seconds=1,
        fake_provider_mode="",
    )
    assert_equal(outcome, "provider_unavailable", "live command outcome")
    assert report is None
    assert_equal(details["reason"], "live_adapter_not_supported", "live command reason")


def test_live_guard_requires_flag_and_environment() -> None:
    adapter = provider_runner.load_provider_adapters()["claude_headless_p0"]
    request = {
        "request_id": "req-live-guard",
        "run_id": "run-live-guard",
        "workflow_id": "single_step_external_review",
        "step_id": "review",
    }
    original = provider_runner.LIVE_ADAPTERS["claude_headless_p0"]
    original_env = os.environ.pop(provider_runner.LIVE_ENV_FLAG, None)
    calls = []
    provider_runner.LIVE_ADAPTERS["claude_headless_p0"] = lambda *_args, **_kwargs: calls.append(True)
    try:
        outcome, _report, details = provider_runner.execute_provider(
            request=request,
            adapter=adapter,
            timeout_seconds=1,
            fake_provider_mode="",
            live=False,
        )
        assert_equal(outcome, "provider_unavailable", "flag guard outcome")
        assert_equal(details["reason"], "live_execution_not_enabled", "flag guard reason")
        outcome, _report, details = provider_runner.execute_provider(
            request=request,
            adapter=adapter,
            timeout_seconds=1,
            fake_provider_mode="",
            live=True,
        )
        assert_equal(outcome, "provider_unavailable", "environment guard outcome")
        assert_equal(details["reason"], "live_env_guard_missing", "environment guard reason")
        assert not calls
    finally:
        provider_runner.LIVE_ADAPTERS["claude_headless_p0"] = original
        if original_env is not None:
            os.environ[provider_runner.LIVE_ENV_FLAG] = original_env


def test_patched_live_adapter_completes_without_raw_output_leakage() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-live-patched"
        request_id = "req-provider-live-patched"
        prepare_run(state_root, request_id=request_id, run_id=run_id)
        marker = b"SECRET-LIVE-PROVIDER-OUTPUT"
        report = {
            "report_version": "1",
            "report_id": f"report-{run_id}",
            "request_id": request_id,
            "run_id": run_id,
            "workflow_id": "single_step_external_review",
            "step_id": "review",
            "result": "pass",
            "summary": "Patched live adapter passed.",
            "provider_evidence": {},
            "findings": [],
            "authority": {
                "canonical_result": "typed_report_file",
                "stdout_is_signal_only": True,
                "raw_transcript_shared": False,
            },
        }
        invocation = {
            "status": "ok",
            "reason": "ok",
            "exit_code": 0,
            "stdout": marker,
            "stderr": b"",
            "duration_ms": 3,
            "report": report,
            "evidence_fields": {
                "provider": "anthropic",
                "effective_model": "claude-fixture",
                "provider_request_id": "provider-request-fixture",
                "provider_session_id": "provider-session-fixture",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
        original = provider_runner.LIVE_ADAPTERS["claude_headless_p0"]
        original_binding = provider_runner.provider_adapters.resolve_execution_binding
        original_env = os.environ.get(provider_runner.LIVE_ENV_FLAG)
        provider_runner.LIVE_ADAPTERS["claude_headless_p0"] = lambda *_args, **_kwargs: invocation
        provider_runner.provider_adapters.resolve_execution_binding = lambda _adapter_id: {
            "binding_version": "1",
            "binary": {"path": "/fixture/claude", "sha256": "sha256:" + "a" * 64},
            "confinement": "test",
        }
        os.environ[provider_runner.LIVE_ENV_FLAG] = "1"
        try:
            payload = provider_runner.run_provider(
                state_root=state_root,
                run_id=run_id,
                live=True,
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "live-adapter-test",
                    "authn_method": "local_test",
                },
            )
        finally:
            provider_runner.LIVE_ADAPTERS["claude_headless_p0"] = original
            provider_runner.provider_adapters.resolve_execution_binding = original_binding
            if original_env is None:
                os.environ.pop(provider_runner.LIVE_ENV_FLAG, None)
            else:
                os.environ[provider_runner.LIVE_ENV_FLAG] = original_env
        assert_equal(payload["decision"], "ok", "live patched decision")
        assert_equal(payload["workflow_run"]["run_state"], "complete", "live patched state")
        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        transcript = json.loads(Path(payload["transcript_path"]).read_text(encoding="utf-8"))
        typed_report = json.loads(Path(payload["report_path"]).read_text(encoding="utf-8"))
        assert base64.b64decode(transcript["stdout_base64"]) == marker
        assert_equal(evidence["provider_request_id"], "provider-request-fixture", "provider request id")
        assert_equal(evidence["usage"], {"input_tokens": 1, "output_tokens": 2}, "provider usage")
        assert_equal(evidence["stdout_sha256"], provider_runner.sha256_bytes(marker), "raw stdout digest")
        assert_equal(
            evidence["transcript_sha256"],
            provider_runner.file_sha256(Path(payload["transcript_path"])),
            "transcript digest",
        )
        shared = json.dumps({"run": payload["workflow_run"], "evidence": evidence, "report": typed_report})
        assert marker.decode() not in shared


def test_run_provider_step_honors_adapter_alias() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_run(state_root, request_id="req-provider-step-alias", run_id="run-provider-step-alias")

        payload = provider_runner.run_provider_step(
            state_root=state_root,
            run_id="run-provider-step-alias",
            adapter="cursor_cli_p0",
        )

        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        assert_equal(evidence["provider_adapter_id"], "cursor_cli_p0", "adapter alias id")
        assert_equal(evidence["provider_target"], "cursor_cli", "adapter alias target")


def test_undecodable_provider_stdout_is_malformed_output() -> None:
    outcome, report, details = provider_runner.parse_provider_stdout(b"\xff")
    assert_equal(outcome, "provider_malformed_output", "binary malformed outcome")
    assert report is None
    assert "stdout_sha256" in details


def test_provider_invocation_releases_global_lock_and_renews_lease() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-two-phase"
        prepare_run(state_root, request_id="req-provider-two-phase", run_id=run_id)
        original = provider_runner.execute_provider
        observations: list[bool] = []

        def observed_execute(**kwargs):
            observations.append(not provider_runner.run_lock.global_lock_path(state_root).exists())
            observations.append(kwargs["heartbeat"]() is True)
            return original(**kwargs)

        provider_runner.execute_provider = observed_execute
        try:
            payload = provider_runner.run_provider(
                state_root=state_root,
                run_id=run_id,
                fake_provider_mode="success",
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "two-phase-test",
                    "authn_method": "local_test",
                },
            )
        finally:
            provider_runner.execute_provider = original
        assert_equal(observations, [True, True], "two-phase lock and heartbeat")
        assert_equal(payload["decision"], "ok", "two-phase result")
        execution = payload["workflow_run"]["provider_execution"]
        assert_equal(execution["phase"], "completed", "durable result phase")
        assert Path(execution["last_outcome"]["attempt_result_path"]).is_file()


def test_call_immediate_snapshot_recheck_blocks_provider() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-dispatch-recheck"
        prepare_run(state_root, request_id="req-provider-dispatch-recheck", run_id=run_id)
        original_authorize = provider_runner.authorize_provider_dispatch
        original_execute = provider_runner.execute_provider
        calls: list[str] = []

        def tamper_then_authorize(**kwargs):
            run = provider_runner.run_store.load_run(state_root, run_id)
            snapshot = work_order_builder.snapshot_path(
                state_root, run_id, str(run["current_step"]), int(run["iteration"])
            )
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            payload["policy_digest"] = "sha256:" + "0" * 64
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            return original_authorize(**kwargs)

        def forbidden_execute(**_kwargs):
            calls.append("called")
            raise AssertionError("provider must not run after snapshot tamper")

        provider_runner.authorize_provider_dispatch = tamper_then_authorize
        provider_runner.execute_provider = forbidden_execute
        try:
            payload = provider_runner.run_provider(
                state_root=state_root,
                run_id=run_id,
                fake_provider_mode="success",
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "dispatch-recheck-test",
                    "authn_method": "local_test",
                },
            )
        finally:
            provider_runner.authorize_provider_dispatch = original_authorize
            provider_runner.execute_provider = original_execute
        assert not calls
        assert_equal(payload["reason"], "provider_unavailable", "tamper outcome")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "tamper human gate")


def test_timeout_contract_and_private_transcript_permissions() -> None:
    assert_equal(
        provider_runner.validate_provider_timeout(provider_runner.DEFAULT_PROVIDER_TIMEOUT_SECONDS),
        1800,
        "default timeout",
    )
    assert_equal(provider_runner.validate_provider_timeout(86400), 86400, "maximum timeout")
    for invalid in (0, 86401):
        try:
            provider_runner.validate_provider_timeout(invalid)
        except provider_runner.ProviderRunnerError:
            pass
        else:
            raise AssertionError(f"invalid timeout accepted: {invalid}")

    with tempfile.TemporaryDirectory() as raw_tmp:
        path = Path(raw_tmp) / "provider-evidence" / "run-private" / "review-provider-transcript.json"
        provider_runner.write_live_transcript(
            Path(raw_tmp),
            path,
            stdout=b"ok",
            stderr=b"",
            outcome="ok",
            exit_code=0,
        )
        assert_equal(path.stat().st_mode & 0o777, 0o600, "transcript mode")
        assert_equal(path.parent.stat().st_mode & 0o777, 0o700, "transcript directory mode")
        assert_equal(path.parent.parent.stat().st_mode & 0o777, 0o700, "evidence root mode")


def test_direct_runner_accounts_expired_attempt_before_new_claim() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-expired-direct"
        prepare_run(state_root, request_id="req-provider-expired-direct", run_id=run_id)
        run = run_store.load_run(state_root, run_id)
        digest = "sha256:" + "1" * 64
        run["run_state"] = "waiting_provider"
        run["provider_execution"] = {
            "execution_version": "1",
            "step_id": "review",
            "adapter_id": "claude_headless_p0",
            "work_order_digest": digest,
            "adapter_request_digest": "sha256:" + "2" * 64,
            "context_snapshot_digest": "sha256:" + "3" * 64,
            "phase": "invoking",
            "attempt_number": 1,
            "attempt_id": "provider-attempt-expired-direct",
            "timeout_seconds": 1800,
            "lease": {
                "lease_id": "provider-lease-expired-direct",
                "claimed_by": {"principal_type": "harness_runner"},
                "claimed_at": "2000-01-01T00:00:00+00:00",
                "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                "lease_expires_at": "2000-01-01T00:00:00+00:00",
            },
            "retry": {
                "last_failure_fingerprint": None,
                "consecutive_failures": 0,
                "auto_retries_used": 0,
                "max_auto_retries": 5,
            },
            "last_outcome": None,
        }
        run_store.store_run(state_root, run, expected_current_state="step_queued")
        interrupted = run_store.load_run(state_root, run_id)
        retry_allowed, abandoned_path = run_lifecycle.account_expired_provider_attempt(
            state_root, interrupted
        )
        assert retry_allowed and abandoned_path is not None
        assert json.loads(abandoned_path.read_text(encoding="utf-8"))["abandoned"] is True

        payload = provider_runner.run_provider(
            state_root=state_root,
            run_id=run_id,
            fake_provider_mode="success",
            principal={
                "principal_type": "harness_runner",
                "principal_id": "expired-direct-test",
                "authn_method": "local_test",
            },
        )
        assert_equal(payload["decision"], "ok", "direct retry completes")
        retry = payload["workflow_run"]["provider_execution"]["retry"]
        assert_equal(retry["auto_retries_used"], 1, "direct runner consumes expired retry")
        assert str(retry["last_failure_fingerprint"]).startswith("sha256:")


def test_completed_attempt_journal_recovers_without_provider_reinvocation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-journal-recovery"
        prepare_run(state_root, request_id="req-provider-journal-recovery", run_id=run_id)
        original_store = run_store.store_run

        def crash_before_result_ready_store(root, candidate, **kwargs):
            execution = candidate.get("provider_execution")
            if isinstance(execution, dict) and execution.get("phase") == "result_ready":
                raise RuntimeError("simulated process crash after attempt journal")
            return original_store(root, candidate, **kwargs)

        run_store.store_run = crash_before_result_ready_store
        try:
            try:
                provider_runner.run_provider(
                    state_root=state_root,
                    run_id=run_id,
                    adapter_id="cursor_cli_p0",
                    fake_provider_mode="success",
                    principal={
                        "principal_type": "harness_runner",
                        "principal_id": "journal-crash-test",
                        "authn_method": "local_test",
                    },
                )
            except RuntimeError as exc:
                assert_equal(str(exc), "simulated process crash after attempt journal", "crash point")
            else:
                raise AssertionError("simulated crash did not occur")
        finally:
            run_store.store_run = original_store

        interrupted = run_store.load_run(state_root, run_id)
        interrupted["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        original_store(state_root, interrupted, expected_current_state="waiting_provider")
        resumed = run_lifecycle.resume_run(
            state_root,
            run_id,
            principal={
                "principal_type": "harness_runner",
                "principal_id": "journal-resume-test",
                "authn_method": "local_test",
            },
        )
        assert_equal(resumed["decision"], "ok", "journal resume decision")
        assert_equal(resumed["reason"], "provider_attempt_result_pending", "journal resume reason")
        assert_equal(resumed["next_action"], "run_provider", "journal recovery action")
        assert_equal(
            run_store.load_run(state_root, run_id)["run_state"],
            "waiting_provider",
            "resume preserves recoverable attempt",
        )
        original_execute = provider_runner.execute_provider

        def forbidden_execute(**_kwargs):
            raise AssertionError("completed attempt journal must prevent provider reinvocation")

        provider_runner.execute_provider = forbidden_execute
        try:
            recovered = provider_runner.run_provider(
                state_root=state_root,
                run_id=run_id,
                fake_provider_mode="success",
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "journal-recovery-test",
                    "authn_method": "local_test",
                },
            )
        finally:
            provider_runner.execute_provider = original_execute
        assert_equal(recovered["decision"], "ok", "journal recovery decision")
        assert_equal(recovered["workflow_run"]["run_state"], "complete", "journal recovery state")
        assert_equal(
            recovered["provider_evidence"]["provider_adapter_id"],
            "cursor_cli_p0",
            "journal uses recorded adapter",
        )
        assert recovered["workflow_run"]["provider_execution"]["last_outcome"].get(
            "recovered_from_journal"
        )


def test_failed_attempt_journal_preserves_typed_outcome_without_reinvocation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-failure-journal"
        prepare_run(state_root, request_id="req-provider-failure-journal", run_id=run_id)
        original_execute = provider_runner.execute_provider
        original_write = provider_runner.private_atomic_write_json

        def auth_failure(**_kwargs):
            return (
                "provider_unavailable",
                None,
                {"reason": "auth_or_quota", "duration_ms": 1, "_live": False},
            )

        def crash_after_result_write(root, path, payload):
            original_write(root, path, payload)
            if isinstance(payload, dict) and payload.get("attempt_result_version") == "1":
                raise RuntimeError("simulated crash after failed attempt journal")

        provider_runner.execute_provider = auth_failure
        provider_runner.private_atomic_write_json = crash_after_result_write
        try:
            try:
                provider_runner.run_provider(
                    state_root=state_root,
                    run_id=run_id,
                    fake_provider_mode="success",
                    principal={
                        "principal_type": "harness_runner",
                        "principal_id": "failure-journal-crash-test",
                        "authn_method": "local_test",
                    },
                )
            except RuntimeError as exc:
                assert_equal(
                    str(exc),
                    "simulated crash after failed attempt journal",
                    "failed journal crash point",
                )
            else:
                raise AssertionError("failed journal crash did not occur")
        finally:
            provider_runner.execute_provider = original_execute
            provider_runner.private_atomic_write_json = original_write

        interrupted = run_store.load_run(state_root, run_id)
        interrupted["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        run_store.store_run(state_root, interrupted, expected_current_state="waiting_provider")
        resumed = run_lifecycle.resume_run(
            state_root,
            run_id,
            principal={
                "principal_type": "harness_runner",
                "principal_id": "failure-journal-resume-test",
                "authn_method": "local_test",
            },
        )
        assert_equal(resumed["reason"], "provider_attempt_result_pending", "failed journal pending")

        def forbidden_execute(**_kwargs):
            raise AssertionError("failed attempt journal must prevent provider reinvocation")

        provider_runner.execute_provider = forbidden_execute
        try:
            recovered = provider_runner.run_provider(
                state_root=state_root,
                run_id=run_id,
                fake_provider_mode="success",
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "failure-journal-recovery-test",
                    "authn_method": "local_test",
                },
            )
        finally:
            provider_runner.execute_provider = original_execute
        assert_equal(recovered["decision"], "blocked", "failed journal recovery decision")
        assert_equal(recovered["reason"], "provider_unavailable", "failed journal outcome")
        assert_equal(recovered["workflow_run"]["run_state"], "waiting_human", "auth human gate")
        assert_equal(
            recovered["workflow_run"]["provider_execution"]["last_outcome"]["reason_class"],
            "auth_or_quota",
            "typed failure preserved",
        )


def test_serialized_context_limit_blocks_before_claim_without_retry() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-context-serialized-limit"
        prepare_run(state_root, request_id="req-provider-context-serialized-limit", run_id=run_id)
        original_limit = provider_runner.provider_adapters.MAX_CONTEXT_BYTES
        original_execute = provider_runner.execute_provider

        def forbidden_execute(**_kwargs):
            raise AssertionError("oversized serialized context must not invoke provider")

        provider_runner.provider_adapters.MAX_CONTEXT_BYTES = 1
        provider_runner.execute_provider = forbidden_execute
        try:
            blocked = provider_runner.run_provider(
                state_root=state_root,
                run_id=run_id,
                fake_provider_mode="success",
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "serialized-context-limit-test",
                    "authn_method": "local_test",
                },
            )
        finally:
            provider_runner.provider_adapters.MAX_CONTEXT_BYTES = original_limit
            provider_runner.execute_provider = original_execute
        assert_equal(blocked["decision"], "blocked", "serialized context decision")
        assert_equal(blocked["reason"], "context_snapshot_serialized_limit", "serialized context reason")
        persisted = run_store.load_run(state_root, run_id)
        assert_equal(persisted["run_state"], "step_queued", "serialized context state")
        assert "provider_execution" not in persisted


def test_request_artifact_paths_are_recomputed_and_confined() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp) / "state"
        state_root.mkdir()
        run_id = "run-artifact-paths"
        step_id = "review"
        request = {
            "report_path": str(provider_runner.provider_report_path(state_root, run_id, step_id)),
            "evidence_path": str(provider_runner.provider_evidence_path(state_root, run_id, step_id)),
            "transcript_path": str(provider_runner.provider_transcript_path(state_root, run_id, step_id)),
        }
        provider_runner.verified_request_artifact_paths(
            state_root=state_root,
            run_id=run_id,
            step_id=step_id,
            request=request,
        )
        request["evidence_path"] = str(state_root.parent / "escaped.json")
        try:
            provider_runner.verified_request_artifact_paths(
                state_root=state_root,
                run_id=run_id,
                step_id=step_id,
                request=request,
            )
        except provider_runner.ProviderRunnerError as exc:
            assert_equal(str(exc), "provider_evidence_path_mismatch", "artifact path reason")
        else:
            raise AssertionError("request-controlled evidence path must be rejected")
        outside = Path(raw_tmp) / "outside"
        outside.mkdir()
        (state_root / "reports").symlink_to(outside, target_is_directory=True)
        try:
            provider_runner.verified_request_artifact_paths(
                state_root=state_root,
                run_id=run_id,
                step_id=step_id,
                request=request,
            )
        except provider_runner.ProviderRunnerError as exc:
            assert_equal(str(exc), "state_artifact_symlink", "report symlink reason")
        else:
            raise AssertionError("symlinked report root must be rejected")


if __name__ == "__main__":
    tests = (
        test_fake_provider_success_completes_with_normalized_evidence,
        test_runner_dispatches_through_adapter_metadata,
        test_runner_transition_request_wins_over_manual_adapter_candidate,
        test_waiting_provider_retry_records_fresh_request_authority,
        test_completion_rejects_tampered_runner_evidence_identity_path_and_type,
        test_hermes_evidence_records_bridge_pattern_without_async_claim,
        test_provider_unavailable_waits_for_human,
        test_provider_nonzero_exit_waits_for_human,
        test_missing_provider_report_waits_for_human,
        test_live_runner_claim_blocks_second_provider,
        test_malformed_provider_output_fails_without_raw_stdout_in_run,
        test_run_provider_rejects_non_runnable_run_state,
        test_runner_rejects_noncanonical_report_path_before_provider_output,
        test_runner_reports_unreadable_work_order_as_blocked_json,
        test_arbitrary_live_command_adapter_is_rejected,
        test_live_guard_requires_flag_and_environment,
        test_patched_live_adapter_completes_without_raw_output_leakage,
        test_run_provider_step_honors_adapter_alias,
        test_undecodable_provider_stdout_is_malformed_output,
        test_provider_invocation_releases_global_lock_and_renews_lease,
        test_call_immediate_snapshot_recheck_blocks_provider,
        test_timeout_contract_and_private_transcript_permissions,
        test_direct_runner_accounts_expired_attempt_before_new_claim,
        test_completed_attempt_journal_recovers_without_provider_reinvocation,
        test_failed_attempt_journal_preserves_typed_outcome_without_reinvocation,
        test_serialized_context_limit_blocks_before_claim_without_retry,
        test_request_artifact_paths_are_recomputed_and_confined,
    )
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))
