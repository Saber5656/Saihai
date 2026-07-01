#!/usr/bin/env python3
"""Tests for the host-owned P0 frontdoor orchestrator."""

from __future__ import annotations

import json
import importlib.util
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


def external_review_classification(**overrides):
    candidate = {
        "classification_version": "1",
        "task_kind": "external_review",
        "permission_required": "readonly",
        "external_provider_required": True,
        "publication_required": False,
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only",
        "expected_artifacts": ["typed_report"],
    }
    candidate.update(overrides)
    return candidate


def run_frontdoor(state_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
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
        check=check,
    )


def load_payload(completed: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(completed.stdout)


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def load_server_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("frontdoor_server", SERVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def http_text(method: str, url: str) -> str:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def test_frontdoor_propose_approve_create_run_and_drain() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        classification = json.dumps(external_review_classification())

        proposed = load_payload(
            run_frontdoor(
                state_root,
                "propose",
                "--task-id",
                "TSK-frontdoor",
                "--request-id",
                "req-frontdoor",
                "--prompt",
                "Claudeにreadonlyレビューを依頼する",
                "--classification",
                classification,
                "--ref",
                "organization/runtime/workflows/README.md",
                "--allowed-path",
                "organization/runtime/workflows",
                "--frontdoor",
                "codex",
                "--chat-session-id",
                "thread-test",
            )
        )
        assert_equal(proposed["request_status"], "proposed", "proposal status")
        assert_equal(proposed["activation"]["next_action"], "keep_draft", "proposal next action")
        assert_equal(
            proposed["activation"]["activation_scope"]["allowed_ops"],
            {"edit": False, "commit": False, "push": False, "network": False},
            "proposal allowed ops",
        )

        approved = load_payload(
            run_frontdoor(
                state_root,
                "approve",
                "--request-id",
                "req-frontdoor",
                "--human-action-id",
                "ui-click-1",
            )
        )
        assert_equal(approved["request_status"], "approved", "approval status")
        assert_equal(approved["activation"]["activation_source"], "human_ui", "approval source")
        assert_equal(approved["activation"]["approved_by"], "human_ui_action", "approval attribution")
        assert_equal(approved["activation"]["next_action"], "create_workflow_run", "approval next action")

        created = load_payload(
            run_frontdoor(
                state_root,
                "create-run",
                "--request-id",
                "req-frontdoor",
                "--run-id",
                "run-frontdoor",
            )
        )
        run = created["workflow_run"]
        assert_equal(created["created"], True, "run created")
        assert_equal(run["workflow_id"], "single_step_external_review", "run workflow")
        assert_equal(run["run_state"], "created", "initial run state")
        assert_equal(run["activation"]["activation_status"], "approved", "run activation")
        assert "policy" not in run["activation"], "run activation must be schema-shaped"

        drained = load_payload(
            run_frontdoor(
                state_root,
                "drain",
                "--run-id",
                "run-frontdoor",
            )
        )
        work_order = drained["work_order"]
        assert_equal(drained["workflow_run"]["run_state"], "step_queued", "drained run state")
        assert_equal(work_order["workflow_id"], "single_step_external_review", "work order workflow")
        assert_equal(work_order["step_id"], "review", "work order step")
        assert_equal(work_order["permission_mode"], "readonly", "work order permission")
        assert_equal(work_order["external_provider_allowed"], True, "provider allowed")
        assert_equal(
            work_order["activation_scope"]["allowed_ops"],
            {"edit": False, "commit": False, "push": False, "network": False},
            "work order allowed ops",
        )
        assert_equal(
            work_order["context_scope"]["raw_transcript_sharing"],
            "forbidden",
            "work order transcript sharing",
        )

        second_drain = load_payload(
            run_frontdoor(
                state_root,
                "drain",
                "--run-id",
                "run-frontdoor",
            )
        )
        assert_equal(second_drain["drained"], False, "drain idempotence")

        capability = load_payload(run_frontdoor(state_root, "adapter-capability"))
        adapter = capability["adapter"]
        assert_equal(adapter["provider_adapter_id"], "claude_headless_p0", "adapter id")
        assert_equal(adapter["transport"], "headless_cli", "adapter transport")
        assert_equal(adapter["permission_enforcement"], "harness", "adapter permission authority")

        prepared = load_payload(
            run_frontdoor(
                state_root,
                "prepare-claude-adapter",
                "--run-id",
                "run-frontdoor",
            )
        )
        adapter_request = prepared["adapter_request"]
        assert "raw transcript" in adapter_request["prompt"]
        assert "Do not select workflows" in adapter_request["prompt"]
        assert_equal(
            adapter_request["authority"]["provider_may_write"],
            ["typed_report_file", "normalized_provider_evidence_file"],
            "adapter write authority",
        )

        evidence_path = Path(adapter_request["evidence_path"])
        transcript_path = Path(adapter_request["transcript_path"])
        report_path = Path(adapter_request["report_path"])
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps({"normalized": True}) + "\n", encoding="utf-8")
        transcript_path.write_text(json.dumps({"signal_only": True}) + "\n", encoding="utf-8")
        report = {
            "report_version": "1",
            "report_id": "report-frontdoor",
            "request_id": "req-frontdoor",
            "run_id": "run-frontdoor",
            "workflow_id": "single_step_external_review",
            "step_id": "review",
            "result": "pass",
            "summary": "No findings.",
            "provider_evidence": {
                "provider": "claude",
                "effective_model": "claude-sonnet-test",
                "request_id": "req-frontdoor",
                "provider_session_id": "claude-session-test",
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

        validated = load_payload(
            run_frontdoor(
                state_root,
                "validate-report",
                "--run-id",
                "run-frontdoor",
            )
        )
        assert_equal(validated["report_status"], "complete", "report validation status")
        assert_equal(validated["workflow_run"]["run_state"], "complete", "validated run state")
        assert_equal(validated["workflow_run"]["goal_state"], "complete", "validated goal state")


def test_frontdoor_blocks_unapproved_and_unbounded_requests() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        no_classification = load_payload(
            run_frontdoor(
                state_root,
                "propose",
                "--task-id",
                "TSK-missing",
                "--request-id",
                "req-missing",
                "--prompt",
                "レビューして",
            )
        )
        assert_equal(
            no_classification["reason"],
            "typed_classification_required",
            "missing classification reason",
        )

        classification = json.dumps(external_review_classification())
        run_frontdoor(
            state_root,
            "propose",
            "--task-id",
            "TSK-norefs",
            "--request-id",
            "req-norefs",
            "--classification",
            classification,
        )
        blocked = run_frontdoor(
            state_root,
            "approve",
            "--request-id",
            "req-norefs",
            "--human-action-id",
            "ui-click-2",
            check=False,
        )
        payload = load_payload(blocked)
        assert_equal(blocked.returncode, 2, "blocked approval exit")
        assert_equal(payload["request_status"], "blocked", "blocked approval status")
        assert_equal(
            payload["activation"]["approval_required_reason"],
            "bounded_context_refs_required",
            "bounded refs reason",
        )

        create_without_approval = run_frontdoor(
            state_root,
            "create-run",
            "--request-id",
            "req-norefs",
            "--run-id",
            "run-norefs",
            check=False,
        )
        assert_equal(create_without_approval.returncode, 2, "unapproved run exit")
        assert_equal(
            load_payload(create_without_approval)["reason"],
            "approved activation envelope required",
            "unapproved run reason",
        )

        unsafe_adapter = run_frontdoor(
            state_root,
            "prepare-claude-adapter",
            "--run-id",
            "missing-run",
            check=False,
        )
        assert_equal(unsafe_adapter.returncode, 2, "missing run adapter exit")


def test_http_frontdoor_api_flow() -> None:
    server_module = load_server_module()
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        server = server_module.FrontdoorServer(
            ("127.0.0.1", 0),
            server_module.Handler,
            state_root=state_root,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            index = http_text("GET", f"{base}/")
            assert "data-frontdoor-ui=\"p0\"" in index, "frontdoor UI marker missing"
            assert "POST\", \"/frontdoor/propose\"" in index, "frontdoor UI propose action missing"

            health = http_json("GET", f"{base}/healthz")
            assert_equal(health["decision"], "ok", "health decision")

            proposed = http_json(
                "POST",
                f"{base}/frontdoor/propose",
                {
                    "task_id": "TSK-http",
                    "request_id": "req-http",
                    "prompt": "Run HTTP readonly review",
                    "refs": ["organization/runtime/workflows/README.md"],
                    "classification": external_review_classification(),
                },
            )
            assert_equal(proposed["request_status"], "proposed", "http proposed")

            approved = http_json(
                "POST",
                f"{base}/frontdoor/approve",
                {"request_id": "req-http", "human_action_id": "ui-http"},
            )
            assert_equal(approved["request_status"], "approved", "http approved")

            created = http_json(
                "POST",
                f"{base}/orchestrator/runs",
                {"request_id": "req-http", "run_id": "run-http"},
            )
            assert_equal(created["workflow_run"]["run_state"], "created", "http run created")

            drained = http_json("POST", f"{base}/orchestrator/runs/run-http/drain", {})
            assert_equal(drained["workflow_run"]["run_state"], "step_queued", "http drain")

            prepared = http_json("POST", f"{base}/provider/claude/prepare", {"run_id": "run-http"})
            assert_equal(prepared["decision"], "ok", "http adapter prepared")

            request_read = http_json("GET", f"{base}/frontdoor/requests/req-http")
            assert_equal(request_read["request"]["status"], "approved", "http request read")

            run_read = http_json("GET", f"{base}/orchestrator/runs/run-http")
            assert_equal(run_read["workflow_run"]["run_state"], "step_queued", "http run read")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def main() -> None:
    tests = [
        test_frontdoor_propose_approve_create_run_and_drain,
        test_frontdoor_blocks_unapproved_and_unbounded_requests,
        test_http_frontdoor_api_flow,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
