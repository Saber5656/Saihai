#!/usr/bin/env python3
"""Offline end-to-end proof for a complete typed workflow run."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from e2e_harness import OrchestratorHarness

ROOT = Path(__file__).resolve().parents[4]
SAIHAI_CLI = ROOT / "scripts" / "saihai.py"
FRONTDOOR_CLI = ROOT / "organization" / "runtime" / "workflows" / "scripts" / "frontdoor_orchestrator.py"

SAIHAI_TEST_WRAPPER = """
import importlib.util
import sys
from pathlib import Path
cli_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("saihai_e2e_cli", cli_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
frontdoor = module.frontdoor_module()
frontdoor.DIRECTORY_CATALOG["SAIHAI_ORCH_STATE_ROOT"] = sys.argv[2]
module.frontdoor_module = lambda: frontdoor
raise SystemExit(module.main(sys.argv[3:]))
"""

FRONTDOOR_TEST_WRAPPER = """
import importlib.util
import sys
from pathlib import Path
cli_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("frontdoor_e2e_cli", cli_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.DIRECTORY_CATALOG["SAIHAI_ORCH_STATE_ROOT"] = sys.argv[2]
sys.argv = [str(cli_path), "--state-root", sys.argv[2], *sys.argv[3:]]
module.main()
"""

REQUIRED_EVIDENCE_FIELDS = {
    "evidence_version",
    "provider_adapter_id",
    "provider_target",
    "provider",
    "effective_model",
    "request_id",
    "run_id",
    "workflow_id",
    "step_id",
    "provider_request_id",
    "provider_session_id",
    "transcript_path",
    "evidence_path",
    "duration_ms",
    "usage",
    "outcome",
    "raw_transcript_policy",
}


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path
    return payload


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_cli_payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict), payload
    return payload


def run_saihai_cli(state_root: Path, *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", SAIHAI_TEST_WRAPPER, str(SAIHAI_CLI), str(state_root), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return load_cli_payload(completed)


def run_frontdoor_cli(state_root: Path, *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", FRONTDOOR_TEST_WRAPPER, str(FRONTDOOR_CLI), str(state_root), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return load_cli_payload(completed)


def assert_within_state_root(state_root: Path, value: str) -> None:
    Path(value).resolve().relative_to(state_root.resolve())


def assert_artifact_inventory(state_root: Path, run_id: str, request_id: str) -> None:
    required = {
        f"requests/{request_id}.json",
        f"runs/{run_id}.json",
        f"work-orders/{run_id}/review.json",
        f"work-orders/{run_id}/review-snapshot-1.json",
        f"adapter-requests/{run_id}/review-claude_headless_p0.json",
        f"reports/{run_id}/review-external-review-report.json",
        f"provider-evidence/{run_id}/review-provider-evidence.json",
        f"provider-evidence/{run_id}/review-provider-transcript.json",
        f"transitions/{run_id}/0001-report-gate.json",
        f"envelopes/{request_id}/0001-proposed.json",
        f"envelopes/{request_id}/0002-approved.json",
        "audit/events.jsonl",
    }
    actual = {
        path.relative_to(state_root).as_posix()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    assert required <= actual, sorted(required - actual)
    assert not any(".tmp" in Path(path).name for path in actual), sorted(actual)


def assert_identity_and_evidence(state_root: Path, run_id: str, request_id: str) -> None:
    request = read_json(state_root / "requests" / f"{request_id}.json")
    run = read_json(state_root / "runs" / f"{run_id}.json")
    work_order = read_json(state_root / "work-orders" / run_id / "review.json")
    report = read_json(state_root / "reports" / run_id / "review-external-review-report.json")
    evidence_path = state_root / "provider-evidence" / run_id / "review-provider-evidence.json"
    transcript_path = state_root / "provider-evidence" / run_id / "review-provider-transcript.json"
    evidence = read_json(evidence_path)

    assert request["task_id"] == run["task_id"] == work_order["task_id"]
    assert request_id == run["request_id"] == work_order["request_id"] == report["request_id"]
    assert run_id == work_order["run_id"] == report["run_id"] == evidence["run_id"]
    assert run["workflow_id"] == work_order["workflow_id"] == report["workflow_id"]
    assert work_order["step_id"] == report["step_id"] == evidence["step_id"] == "review"
    assert REQUIRED_EVIDENCE_FIELDS <= set(evidence), sorted(REQUIRED_EVIDENCE_FIELDS - set(evidence))
    assert evidence["outcome"] == "ok"
    assert evidence["raw_transcript_policy"] == "signal_only_not_shared"
    assert Path(evidence["evidence_path"]).resolve() == evidence_path.resolve()
    assert Path(evidence["transcript_path"]).resolve() == transcript_path.resolve()

    for value in (
        work_order["report_path"],
        report["provider_evidence"]["evidence_path"],
        report["provider_evidence"]["transcript_path"],
        evidence["evidence_path"],
        evidence["transcript_path"],
    ):
        assert_within_state_root(state_root, value)

    transcript = transcript_path.read_bytes()
    if evidence.get("stdout_sha256"):
        expected = "sha256:" + hashlib.sha256(transcript).hexdigest()
        assert evidence["stdout_sha256"] == expected
    serialized_run = json.dumps(run, ensure_ascii=False)
    assert transcript.decode("utf-8") not in serialized_run
    assert report["summary"] not in serialized_run
    assert "raw_content_policy" not in serialized_run


def assert_work_order_contract(work_order: dict[str, Any]) -> None:
    assert work_order["permission_mode"] == "readonly"
    assert work_order["assignment_role"] == "reviewer"
    assert work_order["external_provider_allowed"] is True
    assert work_order["context_scope"]["raw_transcript_sharing"] == "forbidden"
    assert work_order["activation_scope"]["step_budget"] == 1
    assert set(work_order["activation_scope"]["allowed_ops"].values()) == {False}


def assert_audit_chain(state_root: Path, run_id: str, request_id: str) -> None:
    events = read_json_lines(state_root / "audit" / "events.jsonl")
    event_types = {event["event_type"] for event in events}
    assert {
        "request_proposed",
        "approve_request",
        "create_run",
        "drain_run",
        "run_provider",
        "validate_report",
    } <= event_types
    relevant = [
        event
        for event in events
        if event.get("subject", {}).get("run_id") == run_id
        or event.get("subject", {}).get("request_id") == request_id
    ]
    assert relevant
    assert all(event.get("outcome") != "blocked" for event in relevant), relevant


def execute_harness_flow(*, mode: str, request_id: str, run_id: str) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory()
    state_root = Path(temporary.name)
    harness = OrchestratorHarness(state_root)
    proposed = harness.propose(task_id=f"TSK-{request_id}", request_id=request_id)
    assert proposed["request_status"] == "proposed"
    assert proposed["activation"]["activation_status"] == "proposed"
    assert proposed["activation"]["next_action"] == "keep_draft"

    approved = harness.approve(request_id)
    assert approved["request_status"] == "approved"
    assert approved["activation"]["activation_status"] == "approved"
    assert approved["activation"]["approved_by"] == "human_ui_action"
    assert approved["activation"]["next_action"] == "create_workflow_run"

    created = harness.create_run(request_id, run_id)
    replayed = harness.create_run(request_id, run_id)
    assert created["created"] is True
    assert replayed["created"] is False
    assert len(list((state_root / "runs").glob("*.json"))) == 1

    drained = harness.drain(run_id)
    assert drained["workflow_run"]["run_state"] == "step_queued"
    assert_work_order_contract(drained["work_order"])
    provider = harness.run_step(run_id, adapter="fake_pass", fake_provider_mode=mode)
    assert provider["decision"] == "ok"
    assert provider["report_gate"]["outcome"] == "report_valid"

    completed = harness.verify_completion(run_id)
    assert completed["decision"] == "complete"
    assert completed["reasons"] == []
    assert completed["skipped"] == []
    run = harness.run_record(run_id)
    assert run["run_state"] == "complete"
    assert run["terminal"] == {"status": "complete", "reason": "report_valid"}
    assert run["completion_verification"]["decision"] == "complete"

    harness.cleanup()
    return state_root, temporary


def test_pass_report_full_loop() -> None:
    state_root, temporary = execute_harness_flow(mode="success", request_id="req-e2e-pass", run_id="run-e2e-pass")
    try:
        assert_artifact_inventory(state_root, "run-e2e-pass", "req-e2e-pass")
        assert_identity_and_evidence(state_root, "run-e2e-pass", "req-e2e-pass")
        assert_audit_chain(state_root, "run-e2e-pass", "req-e2e-pass")
        report = read_json(state_root / "reports/run-e2e-pass/review-external-review-report.json")
        assert report["result"] == "pass"
        assert report["findings"] == []
    finally:
        temporary.cleanup()


def test_findings_report_full_loop() -> None:
    state_root, temporary = execute_harness_flow(
        mode="findings",
        request_id="req-e2e-findings",
        run_id="run-e2e-findings",
    )
    try:
        assert_identity_and_evidence(state_root, "run-e2e-findings", "req-e2e-findings")
        report = read_json(state_root / "reports/run-e2e-findings/review-external-review-report.json")
        assert report["result"] == "findings"
        assert len(report["findings"]) == 1
        finding = report["findings"][0]
        assert {"finding_id", "severity", "status", "summary", "evidence_refs"} <= set(finding)
    finally:
        temporary.cleanup()


def test_cli_parity_happy_path() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        request_id = "req-e2e-cli"
        run_id = "run-e2e-cli"
        classification = OrchestratorHarness(state_root).classification()
        proposed = run_saihai_cli(
            state_root,
            "frontdoor",
            "--state-root",
            str(state_root),
            "propose",
            "--task-id",
            "TSK-e2e-cli",
            "--request-id",
            request_id,
            "--prompt",
            "Run bounded offline external review.",
            "--classification",
            json.dumps(classification),
            "--ref",
            "organization/runtime/workflows/README.md",
        )
        assert proposed["request_status"] == "proposed"
        status = run_saihai_cli(
            state_root,
            "frontdoor",
            "--state-root",
            str(state_root),
            "status",
            "--request-id",
            request_id,
        )
        nonce = status["request"]["approval"]["human_action_id"]
        approved = run_saihai_cli(
            state_root,
            "frontdoor",
            "--state-root",
            str(state_root),
            "approve",
            "--request-id",
            request_id,
            "--nonce",
            nonce,
        )
        assert approved["request_status"] == "approved"
        created = run_saihai_cli(
            state_root,
            "workflow",
            "--state-root",
            str(state_root),
            "create-run",
            "--request-id",
            request_id,
            "--run-id",
            run_id,
        )
        assert created["workflow_run"]["run_state"] == "created"
        drained = run_saihai_cli(
            state_root,
            "workflow",
            "--state-root",
            str(state_root),
            "drain",
            "--run-id",
            run_id,
        )
        assert drained["workflow_run"]["run_state"] == "step_queued"
        provider = run_saihai_cli(
            state_root,
            "workflow",
            "--state-root",
            str(state_root),
            "run-provider",
            "--run-id",
            run_id,
            "--fake-provider-mode",
            "success",
        )
        assert provider["report_gate"]["outcome"] == "report_valid"
        completed = run_frontdoor_cli(state_root, "verify-completion", "--run-id", run_id)
        assert completed["decision"] == "complete"
        task_view = run_frontdoor_cli(state_root, "task-view", "--task-id", "TSK-e2e-cli")
        assert len(task_view["runs"]) == 1
        assert task_view["runs"][0]["run_state"] == "complete"
        assert_identity_and_evidence(state_root, run_id, request_id)


def main() -> None:
    tests = [
        test_pass_report_full_loop,
        test_findings_report_full_loop,
        test_cli_parity_happy_path,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests), "skipped": []}, ensure_ascii=False))


if __name__ == "__main__":
    main()
