#!/usr/bin/env python3
"""Tests for orchestrator task/session derived views."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"
FRONTDOOR_SCRIPT = SCRIPT_DIR / "frontdoor_orchestrator.py"
SERVER_SCRIPT = ROOT / "server.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import frontdoor_orchestrator as frontdoor  # noqa: E402
import task_state_bridge  # noqa: E402


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_record(
    *,
    run_id: str,
    task_id: str = "TSK-bridge",
    request_id: str | None = None,
    run_state: str = "created",
    goal_state: str = "approved",
    chat_session_id: str = "thread-bridge",
) -> dict:
    return {
        "run_version": "1",
        "run_id": run_id,
        "task_id": task_id,
        "request_id": request_id or f"req-{run_id}",
        "workflow_id": "single_step_external_review",
        "goal_state": goal_state,
        "run_state": run_state,
        "current_step": "review",
        "iteration": 1,
        "max_steps": 1,
        "step_history": [
            {
                "step_id": "review",
                "status": "queued",
                "queued_at": "2026-07-08T10:00:00+0900",
            }
        ],
        "activation": {"prompt": "must not leak"},
        "terminal": {"status": None, "reason": None},
        "requester": {"frontdoor": "codex", "chat_session_id": chat_session_id},
        "scheduling": {},
        "context_sharing": {},
        "instruction": "must not leak",
        "context_refs": ["must-not-leak.md"],
        "transitions": [{"occurred_at": "2026-07-08T10:01:00+0900"}],
    }


def tree_digest(root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            out.append((relative + "/", "dir"))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out.append((relative, digest))
    return out


def external_review_classification() -> dict:
    return {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["test-fixture"],
        "task_kind": "external_review",
        "permission_required": "readonly",
        "external_provider_required": True,
        "publication_required": False,
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only",
        "expected_artifacts": ["typed_report"],
    }


def provider_report(adapter_request: dict, *, request_id: str, run_id: str) -> dict:
    return {
        "report_version": "1",
        "report_id": f"report-{run_id}",
        "request_id": request_id,
        "run_id": run_id,
        "workflow_id": "single_step_external_review",
        "step_id": "review",
        "result": "pass",
        "summary": "No findings.",
        "provider_evidence": {
            "provider": "claude",
            "effective_model": "claude-test",
            "request_id": request_id,
            "provider_session_id": f"session-{run_id}",
            "transcript_path": adapter_request["transcript_path"],
            "evidence_path": adapter_request["evidence_path"],
        },
        "findings": [],
        "authority": {
            "canonical_result": "typed_report_file",
            "stdout_is_signal_only": True,
            "raw_transcript_shared": False,
        },
    }


def test_run_task_view_is_thin() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = run_record(run_id="run-thin")
        report = state_root / "reports" / "run-thin" / "review-external-review-report.json"
        evidence = state_root / "provider-evidence" / "run-thin" / "review-provider-evidence.json"
        write_json(report, {"result": "pass"})
        write_json(evidence, {"normalized": True})

        view = task_state_bridge.run_task_view(run, state_root=state_root, run_path=None)
        assert_equal(
            set(view),
            {
                "view_version",
                "run_id",
                "task_id",
                "request_id",
                "workflow_id",
                "run_state",
                "goal_state",
                "terminal",
                "current_step",
                "iteration",
                "chat_session_id",
                "frontdoor",
                "report_path",
                "evidence_path",
                "last_transition_at",
                "updated_at",
            },
            "thin view keys",
        )
        forbidden = {"instruction", "prompt", "context_refs", "activation", "transitions", "step_history"}
        assert not (forbidden & set(view)), "thin view must not expose verbose provider/request fields"
        assert_equal(view["report_path"], str(report), "thin report link")
        assert_equal(view["evidence_path"], str(evidence), "thin evidence link")
        assert_equal(view["last_transition_at"], "2026-07-08T10:01:00+0900", "last transition")


def test_runs_for_task_filters_and_sorts() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        write_json(state_root / "runs" / "run-b.json", run_record(run_id="run-b", task_id="TSK-sort"))
        write_json(state_root / "runs" / "run-a.json", run_record(run_id="run-a", task_id="TSK-sort"))
        write_json(state_root / "runs" / "run-c.json", run_record(run_id="run-c", task_id="TSK-other"))
        (state_root / "runs" / "run-corrupt.json").write_text('{"run_id": tru', encoding="utf-8")

        rows = task_state_bridge.runs_for_task(state_root, "TSK-sort")
        assert_equal(
            [row["run_id"] for row in rows if not row.get("view_error")],
            ["run-a", "run-b"],
            "filtered sorted runs",
        )
        corrupt = [row for row in rows if row.get("view_error")]
        assert_equal(corrupt, [{"run_id": "run-corrupt", "view_error": "corrupt_json"}], "corrupt row")


def test_record_run_link_writes_session_index() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        orch_root = root / "orch"
        itb_root = root / "itb"
        session_dir = itb_root / task_state_bridge.safe_session_id("thread-index")
        write_json(session_dir / "active-execution-context.json", {"session_id": "thread-index"})
        run = run_record(run_id="run-index", chat_session_id="thread-index")
        write_json(orch_root / "runs" / "run-index.json", run)

        index_path = task_state_bridge.record_run_link(orch_root, run, [itb_root])
        assert_equal(index_path, session_dir / "orchestrator-runs.json", "session index path")
        index = load_json(index_path)
        assert_equal(index["orchestrator_runs_version"], "1", "index version")
        assert_equal([item["run_id"] for item in index["runs"]], ["run-index"], "initial linked run")

        task_state_bridge.record_run_link(orch_root, run, [itb_root])
        repeated = load_json(index_path)
        assert_equal([item["run_id"] for item in repeated["runs"]], ["run-index"], "replacement no duplicates")

        second = run_record(run_id="run-index-2", chat_session_id="thread-index")
        write_json(orch_root / "runs" / "run-index-2.json", second)
        task_state_bridge.record_run_link(orch_root, second, [itb_root])
        updated = load_json(index_path)
        assert_equal([item["run_id"] for item in updated["runs"]], ["run-index", "run-index-2"], "rebuilt run list")


def test_record_run_link_missing_session_is_silent() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        orch_root = root / "orch"
        itb_root = root / "missing-itb"
        run = run_record(run_id="run-missing", chat_session_id="thread-missing")
        write_json(orch_root / "runs" / "run-missing.json", run)

        assert_equal(task_state_bridge.record_run_link(orch_root, run, [itb_root]), None, "missing session")
        assert not itb_root.exists(), "record_run_link must not create ITB roots or session dirs"


def test_queue_evidence_view_status_mapping() -> None:
    expected = {
        "step_queued": ("pending", "pending"),
        "waiting_provider": ("processing", "pending"),
        "validating": ("processing", "pending"),
        "waiting_human": ("pending", "pending"),
        "complete": ("done", "done"),
        "failed": ("failed", "failed"),
        "aborted": ("failed", "failed"),
    }
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        for run_state, statuses in expected.items():
            run = run_record(run_id=f"run-{run_state}", run_state=run_state)
            row = task_state_bridge.queue_evidence_view(state_root, run)[0]
            assert row["message_id"].startswith("wo-"), "synthetic work-order id prefix"
            assert_equal(row["inbox_path"], "", "orchestrator does not use role inbox")
            assert_equal((row["message_status"], row["report_status"]), statuses, f"{run_state} statuses")
            assert_equal(row["notes"], "orchestrator workflow run (derived view)", "derived notes")


def test_task_view_cli_shape() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        write_json(state_root / "runs" / "run-cli.json", run_record(run_id="run-cli", task_id="TSK-cli"))
        completed = subprocess.run(
            [
                sys.executable,
                str(FRONTDOOR_SCRIPT),
                "--state-root",
                str(state_root),
                "task-view",
                "--task-id",
                "TSK-cli",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        assert_equal(payload["decision"], "ok", "task-view decision")
        assert_equal([item["run_id"] for item in payload["runs"]], ["run-cli"], "task-view runs")
        assert_equal(payload["queue_evidence"][0]["message_id"], "wo-run-cli-review", "queue evidence")

        unknown = subprocess.run(
            [
                sys.executable,
                str(FRONTDOOR_SCRIPT),
                "--state-root",
                str(state_root),
                "task-view",
                "--task-id",
                "TSK-unknown",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        empty = json.loads(unknown.stdout)
        assert_equal(empty["runs"], [], "unknown task runs")
        assert_equal(empty["queue_evidence"], [], "unknown task queue evidence")


def test_orchestrator_root_not_a_session() -> None:
    spec = importlib.util.spec_from_file_location("viewer_server_for_bridge_test", SERVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as raw_tmp:
        itb_root = Path(raw_tmp)
        (itb_root / "frontdoor-orchestrator" / "runs").mkdir(parents=True)
        write_json(itb_root / "thread-viewer" / "active-execution-context.json", {"session_id": "thread-viewer"})
        module.STATE_ROOTS = [("codex", itb_root)]
        sessions = module.session_dirs()
        assert_equal([path.name for _runtime, path in sessions], ["thread-viewer"], "viewer sessions")


def test_no_writes_into_queue_dirs() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        state_root = root / "orch"
        itb_root = root / "itb"
        session_dir = itb_root / "thread-full"
        write_json(session_dir / "active-execution-context.json", {"session_id": "thread-full"})
        write_json(session_dir / "active-task.json", {"task_id": "TSK-full"})
        (session_dir / "queue" / "inbox").mkdir(parents=True)
        (session_dir / "queue" / "inbox" / "role.yaml").write_text("messages: []\n", encoding="utf-8")
        (session_dir / "queue" / "tasks").mkdir()
        (session_dir / "queue" / "reports").mkdir()
        before = tree_digest(session_dir / "queue")

        old_env = os.environ.get(task_state_bridge.ITB_STATE_ROOTS_ENV)
        os.environ[task_state_bridge.ITB_STATE_ROOTS_ENV] = str(itb_root)
        try:
            proposed = frontdoor.proposed_request(
                state_root=state_root,
                task_id="TSK-full",
                request_id="req-full",
                user_prompt="Run bounded external review",
                refs=["organization/runtime/workflows/README.md"],
                classification=external_review_classification(),
                allowed_paths=[],
                expires_at="run_terminal",
                frontdoor="codex",
                chat_session_id="thread-full",
            )
            frontdoor.approve_request(
                state_root=state_root,
                request_id="req-full",
                human_action_id=proposed["approval"]["human_action_id"],
            )
            frontdoor.create_run(
                state_root=state_root,
                request_id="req-full",
                run_id="run-full",
                resume_policy="manual",
            )
            frontdoor.drain_run(state_root=state_root, run_id="run-full")
            prepared = frontdoor.prepare_claude_adapter(state_root=state_root, run_id="run-full")
            adapter_request = prepared["adapter_request"]
            evidence_path = Path(adapter_request["evidence_path"])
            transcript_path = Path(adapter_request["transcript_path"])
            report_path = Path(adapter_request["report_path"])
            write_json(evidence_path, {"normalized": True})
            write_json(transcript_path, {"signal_only": True})
            write_json(report_path, provider_report(adapter_request, request_id="req-full", run_id="run-full"))
            validated = frontdoor.validate_report(state_root=state_root, run_id="run-full")
        finally:
            if old_env is None:
                os.environ.pop(task_state_bridge.ITB_STATE_ROOTS_ENV, None)
            else:
                os.environ[task_state_bridge.ITB_STATE_ROOTS_ENV] = old_env

        after = tree_digest(session_dir / "queue")
        assert_equal(validated["workflow_run"]["run_state"], "complete", "full run terminal state")
        assert_equal(after, before, "queue tree must remain byte-identical")
        index = load_json(session_dir / "orchestrator-runs.json")
        assert_equal(index["runs"][0]["run_state"], "complete", "session index terminal state")


def main() -> None:
    tests = [
        test_run_task_view_is_thin,
        test_runs_for_task_filters_and_sorts,
        test_record_run_link_writes_session_index,
        test_record_run_link_missing_session_is_silent,
        test_queue_evidence_view_status_mapping,
        test_task_view_cli_shape,
        test_orchestrator_root_not_a_session,
        test_no_writes_into_queue_dirs,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
