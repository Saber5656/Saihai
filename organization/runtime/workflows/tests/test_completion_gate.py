#!/usr/bin/env python3
"""Tests for workflow completion verification and Vault evidence rendering."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
FACADE = ROOT / "scripts" / "configure_organization.py"
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"
SERVER_SCRIPT = SCRIPT_DIR / "frontdoor_server.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def external_review_classification() -> dict:
    return {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["completion-gate-test"],
        "task_kind": "external_review",
        "permission_required": "readonly",
        "external_provider_required": True,
        "publication_required": False,
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only",
        "expected_artifacts": ["typed_report"],
    }


def run_frontdoor(
    state_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SAIHAI_ORCH_STATE_ROOT"] = str(state_root)
    return subprocess.run(
        [
            sys.executable,
            str(FACADE),
            "workflow-frontdoor",
            "--state-root",
            str(state_root),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def load_payload(completed: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(completed.stdout)


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def file_sha256(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_terminal_run(
    state_root: Path,
    *,
    run_id: str = "run-completion",
    request_id: str = "req-completion",
    evidence_mutator=None,
    validation_check: bool = True,
) -> dict:
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
    prepared = load_payload(run_frontdoor(state_root, "prepare-claude-adapter", "--run-id", run_id))
    adapter = prepared["adapter_request"]
    evidence_path = Path(adapter["evidence_path"])
    transcript_path = Path(adapter["transcript_path"])
    report_path = Path(adapter["report_path"])
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps({"signal_only": True}) + "\n", encoding="utf-8")
    fixed_fields = adapter["evidence_contract"]["fixed_fields"]
    evidence = {
        **fixed_fields,
        "provider": "claude_headless",
        "effective_model": "claude-sonnet-test",
        "provider_request_id": f"provider-{request_id}",
        "provider_session_id": f"session-{run_id}",
        "duration_ms": 12,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "stdout_sha256": file_sha256(transcript_path),
    }
    if evidence_mutator is not None:
        evidence_mutator(evidence)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
    report = {
        "report_version": "1",
        "report_id": f"report-{run_id}",
        "request_id": request_id,
        "run_id": run_id,
        "workflow_id": "single_step_external_review",
        "step_id": "review",
        "result": "pass",
        "summary": "Review completed.",
        "provider_evidence": {
            "provider": "claude_headless",
            "effective_model": "claude-sonnet-test",
            "request_id": request_id,
            "provider_session_id": f"session-{run_id}",
            "transcript_path": str(transcript_path),
            "evidence_path": str(evidence_path),
        },
        "findings": [],
        "authority": {
            "canonical_result": "typed_report_file",
            "stdout_is_signal_only": True,
            "raw_transcript_shared": False,
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    validation = load_payload(
        run_frontdoor(
            state_root,
            "validate-report",
            "--run-id",
            run_id,
            check=validation_check,
        )
    )
    return {
        "adapter": adapter,
        "report_path": report_path,
        "evidence_path": evidence_path,
        "transcript_path": transcript_path,
        "validation": validation,
    }


def verify(state_root: Path, run_id: str = "run-completion", *, check: bool = True) -> dict:
    return load_payload(run_frontdoor(state_root, "verify-completion", "--run-id", run_id, check=check))


def reason_classes(payload: dict) -> set[str]:
    return {item["reason_class"] for item in payload.get("reasons", [])}


def test_complete_run_verifies_and_annotates() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        payload = verify(state_root)
        assert_equal(payload["decision"], "complete", "decision")
        block = payload["evidence"]
        assert_equal(block["verification_decision"], "complete", "vault decision")
        assert_equal(block["report_sha256"], file_sha256(artifacts["report_path"]), "report digest")
        assert_equal(block["evidence_sha256"], file_sha256(artifacts["evidence_path"]), "evidence digest")
        assert "completion_verification" in payload["workflow_run"], "run annotation missing"
        assert_equal(payload["workflow_run"]["run_state"], "complete", "run state unchanged")


def test_non_terminal_blocks_without_annotation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = load_payload(
            run_frontdoor(
                state_root,
                "propose",
                "--task-id",
                "TSK-nonterminal",
                "--request-id",
                "req-nonterminal",
                "--prompt",
                "Run bounded external review",
                "--classification",
                json.dumps(external_review_classification()),
                "--ref",
                "organization/runtime/workflows/README.md",
            )
        )
        load_payload(run_frontdoor(state_root, "approve", "--request-id", "req-nonterminal", "--human-action-id", proposed["approval"]["human_action_id"]))
        load_payload(run_frontdoor(state_root, "create-run", "--request-id", "req-nonterminal", "--run-id", "run-nonterminal"))
        blocked = verify(state_root, "run-nonterminal", check=False)
        assert "run_not_terminal_complete" in reason_classes(blocked)
        run = json.loads((state_root / "runs/run-nonterminal.json").read_text(encoding="utf-8"))
        assert "completion_verification" not in run


def test_missing_report_and_evidence_are_all_reported() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        artifacts["report_path"].unlink()
        artifacts["evidence_path"].unlink()
        blocked = verify(state_root, check=False)
        classes = reason_classes(blocked)
        assert "missing_typed_report" in classes
        assert "missing_provider_evidence" in classes
        run = json.loads((state_root / "runs/run-completion.json").read_text(encoding="utf-8"))
        assert "completion_verification" not in run


def test_evidence_path_escape_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        report = json.loads(artifacts["report_path"].read_text(encoding="utf-8"))
        report["provider_evidence"]["evidence_path"] = "/tmp/escaped-provider-evidence.json"
        artifacts["report_path"].write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        blocked = verify(state_root, check=False)
        assert "evidence_path_escape" in reason_classes(blocked)


def test_tampered_report_digest_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        report = json.loads(artifacts["report_path"].read_text(encoding="utf-8"))
        report["result"] = "findings"
        report["findings"] = [
            {
                "finding_id": "finding-after-gate",
                "severity": "low",
                "status": "open",
                "summary": "Tampered after report gate.",
                "evidence_refs": ["post-gate-edit"],
            }
        ]
        artifacts["report_path"].write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        blocked = verify(state_root, check=False)
        assert "transition_artifact_mismatch" in reason_classes(blocked)
        run = json.loads((state_root / "runs/run-completion.json").read_text(encoding="utf-8"))
        assert "completion_verification" not in run


def test_tampered_evidence_digest_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        evidence = json.loads(artifacts["evidence_path"].read_text(encoding="utf-8"))
        evidence["usage"] = {"input_tokens": 999, "output_tokens": 1}
        artifacts["evidence_path"].write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
        blocked = verify(state_root, check=False)
        assert "transition_artifact_mismatch" in reason_classes(blocked)


def test_incomplete_provider_evidence_metadata_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        truncated = {
            "evidence_version": "1",
            "run_id": "run-completion",
            "step_id": "review",
        }
        artifacts["evidence_path"].write_text(json.dumps(truncated, ensure_ascii=False) + "\n", encoding="utf-8")
        blocked = verify(state_root, check=False)
        assert "invalid_provider_evidence" in reason_classes(blocked)


def test_deprecated_provider_evidence_version_alias_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        evidence = json.loads(artifacts["evidence_path"].read_text(encoding="utf-8"))
        evidence["provider_evidence_version"] = evidence.pop("evidence_version")
        artifacts["evidence_path"].write_text(
            json.dumps(evidence, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        blocked = verify(state_root, check=False)
        assert "invalid_provider_evidence" in reason_classes(blocked)


def test_schema_violations_block_before_transition_and_at_final_gate() -> None:
    def add_alias(evidence: dict) -> None:
        evidence["provider_evidence_version"] = "1"

    def add_unknown(evidence: dict) -> None:
        evidence["unexpected_field"] = "unexpected"

    def add_raw_transcript(evidence: dict) -> None:
        evidence["raw_transcript"] = "sensitive-provider-output"

    def add_nested_stdout(evidence: dict) -> None:
        evidence["usage"]["stdout"] = "sensitive-provider-output"

    def add_nested_pane_output(evidence: dict) -> None:
        evidence["surface_metadata"] = {"pane_output": "sensitive-provider-output"}

    def hide_raw_content_in_usage_notes(evidence: dict) -> None:
        evidence["usage"]["notes"] = "sensitive-provider-output"

    def hide_raw_content_in_surface_metadata(evidence: dict) -> None:
        evidence["surface_metadata"] = {"debug_notes": "sensitive-provider-output"}

    def set_non_finite_duration(value: float):
        def mutate(evidence: dict) -> None:
            evidence["duration_ms"] = value

        return mutate

    variants = (
        ("canonical-plus-alias", add_alias, "provider_evidence_version"),
        ("unknown-field", add_unknown, "unexpected_field"),
        ("raw-transcript", add_raw_transcript, "forbidden_raw_provider_field:$.raw_transcript"),
        ("nested-stdout", add_nested_stdout, "forbidden_raw_provider_field:$.usage.stdout"),
        (
            "nested-pane-output",
            add_nested_pane_output,
            "forbidden_raw_provider_field:$.surface_metadata.pane_output",
        ),
        (
            "hidden-usage-notes",
            hide_raw_content_in_usage_notes,
            "schema:$.usage.notes:additional_property",
        ),
        (
            "hidden-surface-metadata",
            hide_raw_content_in_surface_metadata,
            "schema:$.surface_metadata.debug_notes:additional_property",
        ),
        ("duration-nan", set_non_finite_duration(float("nan")), "schema:$.duration_ms:type"),
        ("duration-infinity", set_non_finite_duration(float("inf")), "schema:$.duration_ms:type"),
        ("duration-negative-infinity", set_non_finite_duration(float("-inf")), "schema:$.duration_ms:type"),
    )
    for name, mutate, expected_error in variants:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            run_id = f"run-{name}"
            artifacts = prepare_terminal_run(
                state_root,
                run_id=run_id,
                request_id=f"req-{name}",
                evidence_mutator=mutate,
                validation_check=False,
            )
            validation = artifacts["validation"]
            assert_equal(validation["decision"], "blocked", f"{name} report decision")
            assert_equal(validation["outcome"], "report_invalid", f"{name} report outcome")
            assert any(expected_error in item for item in validation["errors"]), (
                f"{name} missing report-gate error: {validation['errors']}"
            )
            blocked = verify(state_root, run_id, check=False)
            assert_equal(blocked["decision"], "blocked", f"{name} final decision")
            assert "invalid_provider_evidence" in reason_classes(blocked)


def test_escaped_transcript_path_is_not_hashed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        with tempfile.TemporaryDirectory() as outside_tmp:
            escaped = Path(outside_tmp) / "outside-transcript.json"
            escaped.write_text(json.dumps({"outside": True}) + "\n", encoding="utf-8")
            report = json.loads(artifacts["report_path"].read_text(encoding="utf-8"))
            report["provider_evidence"]["transcript_path"] = str(escaped)
            artifacts["report_path"].write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
            blocked = verify(state_root, check=False)
            classes = reason_classes(blocked)
            assert "evidence_path_escape" in classes
            assert "digest_mismatch" not in classes


def test_digest_mismatch_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifacts = prepare_terminal_run(state_root)
        artifacts["transcript_path"].write_text(json.dumps({"tampered": True}) + "\n", encoding="utf-8")
        blocked = verify(state_root, check=False)
        assert "digest_mismatch" in reason_classes(blocked)


def test_markdown_render_has_no_verbose_output() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_terminal_run(state_root)
        completed = run_frontdoor(
            state_root,
            "verify-completion",
            "--run-id",
            "run-completion",
            "--format",
            "markdown",
        )
        markdown = completed.stdout
        assert "## Workflow Run Evidence" in markdown
        assert "| Report |" in markdown
        assert "Review completed" not in markdown
        assert "Run bounded external review" not in markdown
        assert len(markdown) < 2000


def load_server_module():
    spec = importlib.util.spec_from_file_location("frontdoor_server", SERVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def http_json_response(method: str, url: str, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_verify_completion_route() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        prepare_terminal_run(state_root)
        server_module = load_server_module()
        try:
            server = server_module.FrontdoorServer(
                ("127.0.0.1", 0),
                server_module.Handler,
                state_root=state_root,
            )
        except PermissionError:
            return
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            token = server_module.frontdoor.channel_token(state_root, "harness")
            status, payload = http_json_response(
                "GET",
                f"http://127.0.0.1:{server.server_port}/orchestrator/runs/run-completion/verify-completion",
                {"X-Orchestrator-Channel": "harness", "X-Orchestrator-Token": token},
            )
            bad_status, bad_payload = http_json_response(
                "GET",
                f"http://127.0.0.1:{server.server_port}/orchestrator/runs/bad!/verify-completion",
                {"X-Orchestrator-Channel": "harness", "X-Orchestrator-Token": token},
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        assert_equal(status, 200, "http status")
        assert_equal(payload["decision"], "complete", "http decision")
        assert_equal(bad_status, 400, "invalid id http status")
        assert_equal(bad_payload["decision"], "blocked", "invalid id decision")


def run_all() -> None:
    tests = [
        test_complete_run_verifies_and_annotates,
        test_non_terminal_blocks_without_annotation,
        test_missing_report_and_evidence_are_all_reported,
        test_evidence_path_escape_blocks,
        test_tampered_report_digest_blocks,
        test_tampered_evidence_digest_blocks,
        test_incomplete_provider_evidence_metadata_blocks,
        test_deprecated_provider_evidence_version_alias_blocks,
        test_schema_violations_block_before_transition_and_at_final_gate,
        test_escaped_transcript_path_is_not_hashed,
        test_digest_mismatch_blocks,
        test_markdown_render_has_no_verbose_output,
        test_http_verify_completion_route,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    run_all()
