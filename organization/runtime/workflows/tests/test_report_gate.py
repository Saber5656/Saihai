#!/usr/bin/env python3
"""Tests for report-gate outcome classification and artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from test_frontdoor_orchestrator import (
    assert_equal,
    external_review_report,
    load_payload,
    prepare_review_handoff,
    run_frontdoor,
)

import report_gate
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
    path.chmod(0o600)
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


def test_normalized_evidence_adapter_metadata_matches_registry() -> None:
    def change_transport(evidence: dict) -> None:
        evidence["transport"] = "tmux_interactive"

    def change_bridge_pattern(evidence: dict) -> None:
        evidence["bridge_pattern"] = "oneshot"

    def change_surface_metadata(evidence: dict) -> None:
        evidence["surface_metadata"]["async_callback_supported"] = True

    def remove_transport(evidence: dict) -> None:
        del evidence["transport"]

    variants = (
        ("transport", change_transport, "normalized_evidence.transport mismatch"),
        ("bridge-pattern", change_bridge_pattern, "normalized_evidence.bridge_pattern mismatch"),
        ("surface-metadata", change_surface_metadata, "normalized_evidence.surface_metadata mismatch"),
        ("missing-transport", remove_transport, "normalized_evidence.transport mismatch"),
    )
    for name, mutate, expected_error in variants:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-adapter-metadata-{name}"
            request_id = f"req-adapter-metadata-{name}"
            adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
            evidence_path = Path(adapter["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            mutate(evidence)
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
            write_report(adapter, request_id=request_id, run_id=run_id)

            blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
            payload = load_payload(blocked)
            assert_equal(blocked.returncode, 2, f"{name} exit")
            assert_equal(payload["outcome"], "report_invalid", f"{name} outcome")
            assert any(expected_error in item for item in payload["errors"]), payload["errors"]


def test_adapter_request_metadata_must_match_registry() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-adapter-request-metadata"
        request_id = "req-adapter-request-metadata"
        adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
        request_path = (
            Path(adapter["work_order_path"]).parents[2]
            / "adapter-requests"
            / run_id
            / "review-claude_headless_p0.json"
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["adapter"]["surface_metadata"]["async_callback_supported"] = True
        request_path.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
        evidence_path = Path(adapter["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["surface_metadata"]["async_callback_supported"] = True
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
        write_report(adapter, request_id=request_id, run_id=run_id)

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "request metadata exit")
        assert_equal(payload["outcome"], "report_invalid", "request metadata outcome")
        assert (
            "adapter_request_authority.surface_metadata does not match registry"
            in payload["errors"]
        ), payload["errors"]


def test_adapter_request_authority_fails_closed_when_missing_or_ambiguous() -> None:
    for name, expected_count in (("missing", 0), ("ambiguous", 2)):
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-authority-{name}"
            request_id = f"req-authority-{name}"
            adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
            request_dir = Path(adapter["work_order_path"]).parents[2] / "adapter-requests" / run_id
            request_path = request_dir / "review-claude_headless_p0.json"
            if name == "missing":
                request_path.unlink()
            else:
                decoy_path = request_dir / "review-decoy.json"
                decoy_path.write_text("{}\n", encoding="utf-8")
                decoy_path.chmod(0o600)
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
                Path(adapter["work_order_path"]).parents[2]
                / "adapter-requests"
                / run_id
                / "review-claude_headless_p0.json"
            )
            artifact_refs: list[str] = []
            if name == "ambiguous":
                decoy_path = request_path.with_name("review-decoy.json")
                decoy_path.write_text("{}\n", encoding="utf-8")
                decoy_path.chmod(0o600)
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
    for name, raw_key in (
        ("raw-transcript", "raw_transcript"),
        ("raw-provider-output", "raw_provider_output"),
        ("stderr", "stderr"),
    ):
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-nested-leak-{name}"
            request_id = f"req-nested-leak-{name}"
            adapter = prepare_review_handoff(
                state_root,
                request_id=request_id,
                run_id=run_id,
            )
            write_report(
                adapter,
                request_id=request_id,
                run_id=run_id,
                recommendations=[{"summary": "do not ship", raw_key: "raw provider text"}],
            )

            blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
            payload = load_payload(blocked)
            assert_equal(blocked.returncode, 2, f"{name} leak exit")
            assert_equal(payload["outcome"], "scope_violation", f"{name} leak outcome")
            expected_error = f"raw_transcript_embedded:recommendations[0].{raw_key}"
            assert expected_error in payload["errors"], payload["errors"]
            assert "report" not in payload


def test_undecodable_normalized_evidence_is_invalid_report() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-undecodable-evidence"
        request_id = "req-undecodable-evidence"
        adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
        Path(adapter["evidence_path"]).write_bytes(b"\xff")
        write_report(adapter, request_id=request_id, run_id=run_id)

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "undecodable evidence exit")
        assert_equal(payload["outcome"], "report_invalid", "undecodable evidence outcome")
        assert_equal(payload["workflow_run"]["run_state"], "failed", "undecodable evidence state")
        assert any(
            item.startswith("normalized_evidence unreadable:")
            for item in payload["errors"]
        ), payload["errors"]
        assert rejection_artifacts(state_root, run_id), "undecodable rejection artifact exists"


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


def test_adapter_request_symlink_is_rejected_without_reading_target() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-adapter-request-symlink"
        request_id = "req-adapter-request-symlink"
        adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
        request_path = (
            Path(adapter["work_order_path"]).parents[2]
            / "adapter-requests"
            / run_id
            / "review-claude_headless_p0.json"
        )
        victim = state_root / "outside-adapter-request.json"
        victim.write_bytes(request_path.read_bytes())
        victim.chmod(0o600)
        before = victim.read_bytes()
        request_path.unlink()
        request_path.symlink_to(victim)
        write_report(adapter, request_id=request_id, run_id=run_id)

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "adapter request symlink exit")
        assert_equal(payload["outcome"], "report_invalid", "adapter request symlink outcome")
        assert any("unsafe request artifacts" in item for item in payload["errors"]), payload["errors"]
        assert_equal(victim.read_bytes(), before, "adapter request symlink target unchanged")


def test_provider_evidence_hardlink_is_rejected_without_hashing_target() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-provider-evidence-hardlink"
        request_id = "req-provider-evidence-hardlink"
        adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
        evidence_path = Path(adapter["evidence_path"])
        victim = state_root / "outside-provider-evidence.json"
        victim.write_bytes(evidence_path.read_bytes())
        victim.chmod(0o600)
        before = victim.read_bytes()
        evidence_path.unlink()
        os.link(victim, evidence_path)
        write_report(adapter, request_id=request_id, run_id=run_id)

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "provider evidence hardlink exit")
        assert_equal(payload["outcome"], "report_invalid", "provider evidence hardlink outcome")
        assert any("not a safe private artifact" in item for item in payload["errors"]), payload["errors"]
        transition = json.loads(Path(payload["transition_artifact_path"]).read_text(encoding="utf-8"))
        assert_equal(transition["evidence_sha256"], None, "unsafe evidence is not hashed")
        assert_equal(victim.read_bytes(), before, "provider evidence hardlink target unchanged")


def test_report_symlink_is_rejected_before_target_read() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-report-symlink"
        request_id = "req-report-symlink"
        adapter = prepare_review_handoff(state_root, request_id=request_id, run_id=run_id)
        report = external_review_report(adapter, request_id=request_id, run_id=run_id)
        victim = state_root / "outside-report.json"
        victim.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        victim.chmod(0o600)
        before = victim.read_bytes()
        report_path = Path(adapter["report_path"])
        report_path.symlink_to(victim)

        try:
            report_gate.gate_report(state_root, run_id)
        except report_gate.ReportGateError as exc:
            assert "canonical report path must stay under reports state directory" in str(exc)
        else:
            raise AssertionError("report symlink should be rejected before reading its target")
        assert_equal(victim.read_bytes(), before, "report symlink target unchanged")


def test_legacy_claude_transcript_path_is_accepted_for_inflight_requests() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-legacy-transcript", run_id="run-legacy-transcript")
        legacy_transcript = Path(adapter["transcript_path"]).with_name("review-claude-transcript.json")
        legacy_transcript.write_text(json.dumps({"signal_only": True}) + "\n", encoding="utf-8")
        legacy_transcript.chmod(0o600)
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
        report_path = Path(adapter["report_path"])
        report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        report_path.chmod(0o600)

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
        report_path = Path(adapter["report_path"])
        report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        report_path.chmod(0o600)

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


def test_malformed_report_json_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-malformed-json", run_id="run-malformed-json")
        report_path = Path(adapter["report_path"])
        report_path.write_text("{not-json\n", encoding="utf-8")
        report_path.chmod(0o600)

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-malformed-json", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "malformed json exit")
        assert_equal(payload["outcome"], "report_invalid", "malformed json outcome")
        assert_equal(payload["reason"], "invalid_report", "malformed json reason")
        assert_equal(payload["workflow_run"]["run_state"], "failed", "malformed json state")
        assert_equal(payload["errors"], ["report unreadable: JSONDecodeError"], "malformed json error")
        assert_equal(bool(rejection_artifacts(state_root, "run-malformed-json")), True, "malformed json rejection artifact")


def test_unreadable_report_shapes_fail_closed() -> None:
    cases = (
        ("utf8", b"\xff\xfe", "report unreadable: UnicodeDecodeError"),
        ("array", b"[]\n", "report must be object"),
    )
    for suffix, content, expected_error in cases:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-report-{suffix}"
            adapter = prepare_review_handoff(state_root, request_id=f"req-report-{suffix}", run_id=run_id)
            report_path = Path(adapter["report_path"])
            report_path.write_bytes(content)
            report_path.chmod(0o600)

            blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
            payload = load_payload(blocked)
            assert_equal(blocked.returncode, 2, f"{suffix} report exit")
            assert_equal(payload["outcome"], "report_invalid", f"{suffix} report outcome")
            assert_equal(payload["workflow_run"]["run_state"], "failed", f"{suffix} report state")
            assert_equal(payload["errors"], [expected_error], f"{suffix} report error")


def test_missing_report_waits_for_human() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-report-missing", run_id="run-report-missing")
        report_path = Path(adapter["report_path"])
        if report_path.exists():
            report_path.unlink()

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-report-missing", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "missing report exit")
        assert_equal(payload["outcome"], "report_not_written", "missing report outcome")
        assert_equal(payload["reason"], "report_not_written", "missing report reason")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "missing report state")


def test_directory_report_path_fails_closed_without_hashing() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-report-directory", run_id="run-report-directory")
        report_path = Path(adapter["report_path"])
        report_path.mkdir()

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-report-directory", check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "directory report exit")
        assert_equal(payload["outcome"], "report_not_written", "directory report outcome")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "directory report state")
        artifact = json.loads(Path(payload["transition_artifact_path"]).read_text(encoding="utf-8"))
        assert_equal(artifact["report_sha256"], None, "directory report digest")


def test_permission_denied_report_fails_closed_without_hashing() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        adapter = prepare_review_handoff(state_root, request_id="req-report-denied", run_id="run-report-denied")
        report_path = Path(adapter["report_path"])
        report_path.write_text("{}\n", encoding="utf-8")
        report_path.chmod(0o000)
        try:
            blocked = run_frontdoor(state_root, "validate-report", "--run-id", "run-report-denied", check=False)
        finally:
            report_path.chmod(0o600)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "permission-denied report exit")
        assert_equal(payload["outcome"], "report_not_written", "permission-denied report outcome")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_human", "permission-denied report state")
        artifact = json.loads(Path(payload["transition_artifact_path"]).read_text(encoding="utf-8"))
        assert_equal(artifact["report_sha256"], None, "permission-denied report digest")


def test_live_provider_claim_blocks_report_validation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-report-live-claim"
        prepare_review_handoff(state_root, request_id="req-report-live-claim", run_id=run_id)
        run = run_store.load_run(state_root, run_id)
        run_lifecycle.transition_run(
            state_root,
            run_id,
            to_state="waiting_provider",
            reason_class="provider_invoked",
            transition="test_live_claim",
            principal={"principal_type": "harness_runner", "principal_id": "first-runner", "authn_method": "local_test"},
            run=run,
        )
        order_path = run_lifecycle.work_order_path(state_root, run_id, "review")
        work_order = json.loads(order_path.read_text(encoding="utf-8"))
        work_order["work_order_authority"]["runner_claim"] = {
            "claim_state": "claimed",
            "lease_id": "lease-live-report",
            "lease_expires_at": "2999-01-01T00:00:00+0000",
        }
        run_store.atomic_write_json(order_path, work_order)
        run_path = run_store.run_path(state_root, run_id)
        before = run_path.read_bytes()

        blocked = run_frontdoor(state_root, "validate-report", "--run-id", run_id, check=False)
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "live claim validation exit")
        assert_equal(payload["reason"], "provider_in_flight", "live claim validation reason")
        assert_equal(payload["workflow_run"]["run_state"], "waiting_provider", "live claim validation state")
        assert_equal(run_path.read_bytes(), before, "live claim canonical run unchanged")
        assert_equal(payload["transition_artifact_path"], None, "live claim transition artifact")


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
        test_normalized_evidence_adapter_metadata_matches_registry,
        test_adapter_request_metadata_must_match_registry,
        test_adapter_request_authority_fails_closed_when_missing_or_ambiguous,
        test_run_provider_transition_authority_does_not_fallback_to_manual_requests,
        test_schema_violation_blocks_with_errors,
        test_transcript_leak_is_scope_violation,
        test_nested_transcript_leak_is_scope_violation,
        test_undecodable_normalized_evidence_is_invalid_report,
        test_evidence_path_escape_is_scope_violation,
        test_adapter_request_symlink_is_rejected_without_reading_target,
        test_provider_evidence_hardlink_is_rejected_without_hashing_target,
        test_report_symlink_is_rejected_before_target_read,
        test_legacy_claude_transcript_path_is_accepted_for_inflight_requests,
        test_identity_mismatch_is_scope_violation,
        test_missing_identity_is_invalid_report,
        test_provider_blocked_waits_human,
        test_result_invalid_fails,
        test_malformed_report_json_fails_closed,
        test_unreadable_report_shapes_fail_closed,
        test_missing_report_waits_for_human,
        test_directory_report_path_fails_closed_without_hashing,
        test_permission_denied_report_fails_closed_without_hashing,
        test_live_provider_claim_blocks_report_validation,
        test_terminal_replay_does_not_write_new_transition_artifact,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
