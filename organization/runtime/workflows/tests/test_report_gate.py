#!/usr/bin/env python3
"""Tests for report-gate outcome classification and artifacts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from test_frontdoor_orchestrator import (
    assert_equal,
    external_review_report,
    load_payload,
    prepare_review_handoff,
    run_frontdoor,
)

import run_lifecycle
import run_store


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_report(adapter_request: dict, *, request_id: str, run_id: str, **overrides) -> dict:
    report = external_review_report(adapter_request, request_id=request_id, run_id=run_id)
    for key, value in overrides.items():
        if key == "provider_evidence" and isinstance(value, dict):
            report["provider_evidence"].update(value)
        elif key == "authority" and isinstance(value, dict):
            report["authority"].update(value)
        else:
            report[key] = value
    path = Path(adapter_request["report_path"])
    path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def transition_artifacts(state_root: Path, run_id: str) -> list[Path]:
    directory = state_root / "transitions" / run_id
    if not directory.exists():
        return []
    return sorted(directory.glob("*-report-gate.json"))


def rejection_artifacts(state_root: Path, run_id: str, step_id: str = "review") -> list[Path]:
    directory = state_root / "reports" / run_id
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{step_id}-rejection-*.json"))


def test_pass_report_completes() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-pass", run_id="run-pass")
        report = write_report(adapter, request_id="req-pass", run_id="run-pass")

        payload = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-pass"))
        assert_equal(payload["outcome"], "report_valid", "pass outcome")
        assert_equal(payload["report_status"], "complete", "pass status")
        assert_equal(payload["workflow_run"]["run_state"], "complete", "pass run state")
        artifact_path = Path(payload["transition_artifact_path"])
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert_equal(artifact["from_state"], "validating", "artifact from state")
        assert_equal(artifact["to_state"], "complete", "artifact to state")
        assert_equal(artifact["report_sha256"], file_sha256(Path(adapter["report_path"])), "report digest")
        assert_equal(payload["report"], report, "response report")


def test_findings_report_completes_and_requires_findings() -> None:
    finding = {
        "finding_id": "F-1",
        "severity": "low",
        "status": "open",
        "summary": "Example finding.",
        "evidence_refs": ["organization/runtime/workflows/README.md"],
    }
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-findings-ok", run_id="run-findings-ok")
        write_report(
            adapter,
            request_id="req-findings-ok",
            run_id="run-findings-ok",
            result="findings",
            findings=[finding],
        )
        payload = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-findings-ok"))
        assert_equal(payload["outcome"], "report_valid", "findings outcome")
        assert_equal(payload["workflow_run"]["run_state"], "complete", "findings run state")

    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-findings-empty", run_id="run-findings-empty")
        write_report(
            adapter,
            request_id="req-findings-empty",
            run_id="run-findings-empty",
            result="findings",
            findings=[],
        )
        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-findings-empty", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "empty findings exit")
        assert_equal(payload["outcome"], "report_invalid", "empty findings outcome")
        assert "findings result requires at least one finding" in payload["errors"]


def test_missing_provider_evidence_blocks_and_preserves_report() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-missing-evidence", run_id="run-missing-evidence")
        report = write_report(adapter, request_id="req-missing-evidence", run_id="run-missing-evidence")
        del report["provider_evidence"]["provider_session_id"]
        report_path = Path(adapter["report_path"])
        report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        before = report_path.read_bytes()

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-missing-evidence", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "missing provider evidence exit")
        assert_equal(payload["outcome"], "report_invalid", "missing provider evidence outcome")
        assert any("provider_evidence missing:provider_session_id" in item for item in payload["errors"])
        assert_equal(report_path.read_bytes(), before, "invalid report preserved")
        assert rejection_artifacts(state_root, "run-missing-evidence"), "rejection artifact exists"


def test_normalized_evidence_adapter_identity_matches_authoritative_request() -> None:
    variants = (
        ("adapter-id", "provider_adapter_id", "different_adapter", "provider_adapter_id mismatch"),
        ("provider-target", "provider_target", "cursor_cli", "provider_target mismatch"),
    )
    for name, field, replacement, expected_error in variants:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-{name}"
            request_id = f"req-{name}"
            adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
            evidence_path = Path(adapter["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence[field] = replacement
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
            write_report(adapter, request_id=request_id, run_id=run_id)

            blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
            payload = load_payload(blocked)
            assert_equal(blocked.returncode, 2, f"{name} exit")
            assert_equal(payload["outcome"], "report_invalid", f"{name} outcome")
            assert any(expected_error in item for item in payload["errors"]), payload["errors"]


def test_adapter_request_authority_fails_closed_when_missing_or_ambiguous() -> None:
    for name, expected_count in (("missing", 0), ("ambiguous", 2)):
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-authority-{name}"
            request_id = f"req-authority-{name}"
            adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
            request_dir = state_root / "adapter-requests" / run_id
            request_path = request_dir / "review-claude_headless_p0.json"
            if name == "missing":
                request_path.unlink()
            else:
                (request_dir / "review-decoy.json").write_text("{}\n", encoding="utf-8")
            write_report(adapter, request_id=request_id, run_id=run_id)

            blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
            payload = load_payload(blocked)
            assert_equal(blocked.returncode, 2, f"{name} authority exit")
            assert_equal(payload["outcome"], "report_invalid", f"{name} authority outcome")
            expected_error = (
                "adapter_request_authority requires exactly one current request: "
                f"found {expected_count}"
            )
            assert expected_error in payload["errors"], payload["errors"]


def test_run_provider_transition_authority_does_not_fallback_to_manual_requests() -> None:
    for name, expected_count in (("missing", 0), ("ambiguous", 2)):
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-transition-authority-{name}"
            request_id = f"req-transition-authority-{name}"
            adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
            request_path = (
                state_root
                / "adapter-requests"
                / run_id
                / "review-claude_headless_p0.json"
            )
            artifact_refs: list[str] = []
            if name == "ambiguous":
                decoy_path = request_path.with_name("review-decoy.json")
                decoy_path.write_text("{}\n", encoding="utf-8")
                artifact_refs = [str(request_path), str(decoy_path)]

            run = run_store.load_run(state_root, run_id)
            run_lifecycle.transition_run(
                state_root,
                run_id,
                to_state="waiting_provider",
                reason_class="provider_invoked",
                transition="run_provider",
                principal={
                    "principal_type": "harness_runner",
                    "principal_id": "report-gate-test",
                    "authn_method": "local_test",
                },
                artifact_refs=artifact_refs,
                run=run,
            )
            write_report(adapter, request_id=request_id, run_id=run_id)

            blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
            payload = load_payload(blocked)
            assert_equal(blocked.returncode, 2, f"{name} transition authority exit")
            assert_equal(payload["outcome"], "report_invalid", f"{name} transition authority outcome")
            expected_error = (
                "adapter_request_authority run_provider transition requires exactly one "
                f"current request: found {expected_count}"
            )
            assert expected_error in payload["errors"], payload["errors"]


def test_schema_violation_blocks_with_errors() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-schema", run_id="run-schema")
        write_report(adapter, request_id="req-schema", run_id="run-schema", foo="bar")

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-schema", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "schema violation exit")
        assert_equal(payload["outcome"], "report_invalid", "schema violation outcome")
        assert "unexpected_fields:foo" in payload["errors"]
        assert rejection_artifacts(state_root, "run-schema"), "schema rejection artifact exists"


def test_transcript_leak_is_scope_violation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-leak", run_id="run-leak")
        write_report(adapter, request_id="req-leak", run_id="run-leak", raw_transcript="raw transcript text")

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-leak", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "transcript leak exit")
        assert_equal(payload["outcome"], "scope_violation", "transcript leak outcome")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "transcript leak state")
        assert "raw_transcript_embedded:raw_transcript" in payload["errors"]
        assert "report" not in payload
        rejection = json.loads(Path(payload["rejection_artifact_path"]).read_text(encoding="utf-8"))
        assert_equal(rejection["outcome"], "scope_violation", "rejection outcome")


def test_nested_transcript_leak_is_scope_violation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-nested-leak", run_id="run-nested-leak")
        write_report(
            adapter,
            request_id="req-nested-leak",
            run_id="run-nested-leak",
            recommendations=[{"summary": "do not ship", "raw_transcript": "raw transcript text"}],
        )

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-nested-leak", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "nested transcript leak exit")
        assert_equal(payload["outcome"], "scope_violation", "nested transcript leak outcome")
        assert "raw_transcript_embedded:recommendations[0].raw_transcript" in payload["errors"]
        assert "report" not in payload


def test_evidence_path_escape_is_scope_violation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-escape", run_id="run-escape")
        write_report(
            adapter,
            request_id="req-escape",
            run_id="run-escape",
            provider_evidence={"evidence_path": "/tmp/outside-provider-evidence.json"},
        )

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-escape", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "evidence path escape exit")
        assert_equal(payload["outcome"], "scope_violation", "evidence path escape outcome")
        assert "evidence_path_escape" in payload["errors"]


def test_legacy_claude_transcript_path_is_accepted_for_inflight_requests() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-legacy-transcript", run_id="run-legacy-transcript")
        legacy_transcript = Path(adapter["transcript_path"]).with_name("review-claude-transcript.json")
        legacy_transcript.write_text(json.dumps({"signal_only": True}) + "\n", encoding="utf-8")
        write_report(
            adapter,
            request_id="req-legacy-transcript",
            run_id="run-legacy-transcript",
            provider_evidence={"transcript_path": str(legacy_transcript)},
        )
        evidence_path = Path(adapter["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["transcript_path"] = str(legacy_transcript)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")

        payload = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-legacy-transcript"))
        assert_equal(payload["outcome"], "report_valid", "legacy transcript outcome")
        assert_equal(payload["workflow_run"]["run_state"], "complete", "legacy transcript run state")


def test_identity_mismatch_is_scope_violation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-identity", run_id="run-identity")
        report = external_review_report(adapter, request_id="req-identity", run_id="run-identity")
        report["run_id"] = "run-other"
        Path(adapter["report_path"]).write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-identity", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "identity mismatch exit")
        assert_equal(payload["outcome"], "scope_violation", "identity mismatch outcome")
        assert "report_identity_mismatch" in payload["errors"]


def test_missing_identity_is_invalid_report() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-missing-identity", run_id="run-missing-identity")
        report = external_review_report(adapter, request_id="req-missing-identity", run_id="run-missing-identity")
        del report["run_id"]
        Path(adapter["report_path"]).write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-missing-identity", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "missing identity exit")
        assert_equal(payload["outcome"], "report_invalid", "missing identity outcome")
        assert "missing_required_fields:run_id" in payload["errors"]
        assert "report_identity_mismatch" not in payload["errors"]
        assert "report" not in payload


def test_provider_blocked_waits_human() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-provider-blocked", run_id="run-provider-blocked")
        write_report(adapter, request_id="req-provider-blocked", run_id="run-provider-blocked", result="blocked")

        payload = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-provider-blocked"))
        assert_equal(payload["decision"], "ok", "provider blocked decision")
        assert_equal(payload["outcome"], "provider_reported_blocked", "provider blocked outcome")
        assert_equal(payload["report_status"], "waiting_human", "provider blocked status")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "provider blocked run state")
        replayed = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-provider-blocked"))
        assert_equal(replayed["decision"], "ok", "provider blocked replay decision")
        assert_equal(replayed["validated"], False, "provider blocked replay validated flag")
        assert_equal(replayed["outcome"], "provider_reported_blocked", "provider blocked replay outcome")
        assert_equal(replayed["workflow_run"]["run_state"], "waiting_human", "provider blocked replay run state")


def test_result_invalid_fails() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-invalid", run_id="run-invalid")
        write_report(adapter, request_id="req-invalid", run_id="run-invalid", result="invalid")

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-invalid", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "invalid result exit")
        assert_equal(payload["outcome"], "report_invalid", "invalid result outcome")
        assert_equal(payload["reason"], "invalid_report", "invalid result reason")


def test_terminal_replay_does_not_write_new_transition_artifact() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-replay", run_id="run-replay")
        write_report(adapter, request_id="req-replay", run_id="run-replay")

        first = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-replay"))
        assert_equal(first["outcome"], "report_valid", "first outcome")
        before = transition_artifacts(state_root, "run-replay")
        replayed = load_payload(run_frontdoor(state_root, "validate-report", "--run-id", "run-replay"))
        after = transition_artifacts(state_root, "run-replay")
        assert_equal(replayed["validated"], False, "terminal replay validated flag")
        assert_equal(replayed["outcome"], "terminal_replay", "terminal replay outcome")
        assert_equal(after, before, "terminal replay transition artifacts")


def main() -> None:
    tests = [
        test_pass_report_completes,
        test_findings_report_completes_and_requires_findings,
        test_missing_provider_evidence_blocks_and_preserves_report,
        test_normalized_evidence_adapter_identity_matches_authoritative_request,
        test_adapter_request_authority_fails_closed_when_missing_or_ambiguous,
        test_run_provider_transition_authority_does_not_fallback_to_manual_requests,
        test_schema_violation_blocks_with_errors,
        test_transcript_leak_is_scope_violation,
        test_nested_transcript_leak_is_scope_violation,
        test_evidence_path_escape_is_scope_violation,
        test_legacy_claude_transcript_path_is_accepted_for_inflight_requests,
        test_identity_mismatch_is_scope_violation,
        test_missing_identity_is_invalid_report,
        test_provider_blocked_waits_human,
        test_result_invalid_fails,
        test_terminal_replay_does_not_write_new_transition_artifact,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
