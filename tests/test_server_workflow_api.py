#!/usr/bin/env python3
"""Regression tests for dashboard workflow-run read APIs."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("saihai_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@contextmanager
def orch_root(root: Path):
    previous = os.environ.get("SAIHAI_ORCH_STATE_ROOT")
    os.environ["SAIHAI_ORCH_STATE_ROOT"] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SAIHAI_ORCH_STATE_ROOT", None)
        else:
            os.environ["SAIHAI_ORCH_STATE_ROOT"] = previous


@contextmanager
def orch_roots(server, roots: list[tuple[str, Path]]):
    previous_env = os.environ.pop("SAIHAI_ORCH_STATE_ROOT", None)
    previous_roots = server.ORCH_STATE_ROOTS
    server.ORCH_STATE_ROOTS = roots
    try:
        yield
    finally:
        server.ORCH_STATE_ROOTS = previous_roots
        if previous_env is not None:
            os.environ["SAIHAI_ORCH_STATE_ROOT"] = previous_env


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_run(
    root: Path,
    run_id: str,
    *,
    task_id: str = "TSK-1",
    session_id: str = "sess-1",
    run_state: str = "step_queued",
    transition_at: str = "2026-07-09T00:00:00+0900",
) -> dict:
    terminal = {"status": "complete", "reason": "report_valid"} if run_state == "complete" else {}
    run = {
        "run_id": run_id,
        "task_id": task_id,
        "request_id": f"req-{run_id}",
        "workflow_id": "single_step_external_review",
        "run_state": run_state,
        "goal_state": "complete" if run_state == "complete" else "active",
        "current_step": "review",
        "iteration": 1,
        "terminal": terminal,
        "activation": {"activation_status": "approved"},
        "requester": {"chat_session_id": session_id, "frontdoor": "codex"},
        "transitions": [
            {
                "seq": 1,
                "from_state": "created",
                "to_state": run_state,
                "occurred_at": transition_at,
            }
        ],
        "prompt": "must not be served",
    }
    write_json(root / "runs" / f"{run_id}.json", run)
    return run


def test_list_empty_root() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        with orch_root(root):
            payload, code = server.api_workflow_runs()
        assert code == 200
        assert payload["workflow_runs"] == []
        assert payload["roots"] == [str(root)]


def test_list_and_filters() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-a", task_id="TSK-1", session_id="sess-1", run_state="step_queued", transition_at="2026-07-09T00:00:00+0900")
        make_run(root, "run-b", task_id="TSK-1", session_id="sess-2", run_state="complete", transition_at="2026-07-09T01:00:00+0900")
        make_run(root, "run-c", task_id="TSK-2", session_id="sess-1", run_state="failed", transition_at="2026-07-09T00:30:00+0900")
        with orch_root(root):
            payload, code = server.api_workflow_runs()
            task_payload, task_code = server.api_workflow_runs(task_id="TSK-1")
            session_payload, session_code = server.api_workflow_runs(session_id="sess-1")
            state_payload, state_code = server.api_workflow_runs(state="complete")
        assert code == task_code == session_code == state_code == 200
        rows = payload["workflow_runs"]
        assert [row["run_id"] for row in rows] == ["run-b", "run-c", "run-a"]
        assert all(row["runtime"] == "env" and row["orch_root"] == str(root) for row in rows)
        assert all("stale_seconds" in row for row in rows)
        assert all(row["activation_status"] == "approved" for row in rows)
        assert {row["derived_status"] for row in rows} == {"active", "complete", "terminal"}
        assert {row["run_id"] for row in task_payload["workflow_runs"]} == {"run-a", "run-b"}
        assert {row["run_id"] for row in session_payload["workflow_runs"]} == {"run-a", "run-c"}
        assert [row["run_id"] for row in state_payload["workflow_runs"]] == ["run-b"]


def test_detail_shapes() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-detail", task_id="TSK-9", session_id="sess-detail", run_state="complete")
        write_json(root / "work-orders" / "run-detail" / "review.json", {"work_order_version": "1", "step_id": "review"})
        write_json(
            root / "reports" / "run-detail" / "review-external-review-report.json",
            {
                "result": "pass",
                "prompt": "redact me",
                "raw_transcript": "must not be served",
                "provider_transcript": "must not be served",
            },
        )
        transcript = root / "provider-evidence" / "run-detail" / "transcript.txt"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("transcript fixture content", encoding="utf-8")
        write_json(
            root / "provider-evidence" / "run-detail" / "review-provider-evidence.json",
            {
                "provider": "fake",
                "transcript_path": str(transcript),
                "evidence_path": "evidence.json",
                "tmux_pane_output": "must not be served",
            },
        )
        write_json(root / "transitions" / "run-detail" / "0001-report-gate.json", {"seq": 1, "outcome": "queued"})
        write_json(root / "transitions" / "run-detail" / "0002-report-gate.json", {"seq": 2, "outcome": "report_valid"})
        write_json(root / "reports" / "run-detail" / "review-rejection-1.json", {"outcome": "report_invalid"})
        with orch_root(root):
            payload, code = server.api_workflow_run("run-detail")
            scoped_payload, scoped_code = server.api_workflow_run("run-detail", session_id="sess-detail")
            wrong_scope, wrong_scope_code = server.api_workflow_run("run-detail", session_id="sess-other")
        assert code == 200
        assert scoped_code == 200
        assert wrong_scope_code == 404
        assert wrong_scope == {"error": "run_not_found"}
        detail = payload["workflow_run"]
        assert detail["run"]["run_id"] == "run-detail"
        assert scoped_payload["workflow_run"]["run"]["activation_status"] == "approved"
        assert "prompt" not in detail["run_record"]
        assert detail["work_order"]["work_order_version"] == "1"
        assert detail["report"] == {"result": "pass"}
        assert detail["evidence"]["transcript_path"] == str(transcript)
        assert "tmux_pane_output" not in detail["evidence"]
        assert [item["seq"] for item in detail["transitions"]] == [1, 2]
        assert detail["rejections"] == [{"outcome": "report_invalid"}]
        assert detail["lock"]["locked"] is False
        assert "transcript fixture content" not in json.dumps(detail, ensure_ascii=False)


def test_detail_missing_run() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        with orch_root(root):
            payload, code = server.api_workflow_run("run-missing")
        assert code == 404
        assert payload == {"error": "run_not_found"}


def test_corrupt_run_row() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-good")
        corrupt = root / "runs" / "run-bad.json"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{not-json", encoding="utf-8")
        with orch_root(root):
            payload, code = server.api_workflow_runs()
        assert code == 200
        rows = {row["run_id"]: row for row in payload["workflow_runs"]}
        assert rows["run-bad"]["view_error"] == "corrupt_json"
        assert rows["run-good"]["run_state"] == "step_queued"


def test_fallback_rows_have_mtime_when_bridge_fails() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-fallback")

        class BrokenBridge:
            @staticmethod
            def run_task_view(*_args, **_kwargs):
                raise RuntimeError("bridge unavailable")

        original = server._task_state_bridge
        server._task_state_bridge = lambda: BrokenBridge
        try:
            with orch_root(root):
                payload, code = server.api_workflow_runs()
        finally:
            server._task_state_bridge = original
        assert code == 200
        row = payload["workflow_runs"][0]
        assert row["view_version"] == "fallback"
        assert row["updated_at"], "fallback row should include mtime"


def test_staleness_honors_timezone_offsets() -> None:
    server = load_server()
    jst_epoch = server._iso_to_epoch("2026-07-09T00:00:00+0900")
    utc_epoch = server._iso_to_epoch("2026-07-08T15:00:00+0000")
    z_epoch = server._iso_to_epoch("2026-07-08T15:00:00Z")
    assert jst_epoch == utc_epoch == z_epoch


def test_duplicate_run_detail_can_select_root() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        claude_root = base / "claude"
        codex_root = base / "codex"
        make_run(claude_root, "run-dup", task_id="TSK-claude", session_id="sess-claude")
        make_run(codex_root, "run-dup", task_id="TSK-codex", session_id="sess-codex")
        with orch_roots(server, [("claude", claude_root), ("codex", codex_root)]):
            payload, code = server.api_workflow_runs()
            codex_payload, codex_code = server.api_workflow_run("run-dup", runtime="codex")
            root_payload, root_code = server.api_workflow_run("run-dup", orch_root=str(codex_root))
            invalid_runtime, invalid_runtime_code = server.api_workflow_run("run-dup", runtime="missing")
            invalid_root, invalid_root_code = server.api_workflow_run("run-dup", orch_root=str(base / "missing"))
        assert code == 200
        assert {(row["runtime"], row["task_id"]) for row in payload["workflow_runs"]} == {
            ("claude", "TSK-claude"),
            ("codex", "TSK-codex"),
        }
        assert codex_code == root_code == 200
        assert codex_payload["workflow_run"]["runtime"] == "codex"
        assert codex_payload["workflow_run"]["run"]["task_id"] == "TSK-codex"
        assert root_payload["workflow_run"]["orch_root"] == str(codex_root)
        assert invalid_runtime_code == 400
        assert invalid_runtime == {"error": "invalid_runtime"}
        assert invalid_root_code == 400
        assert invalid_root == {"error": "invalid_orch_root"}


def test_session_filters_use_session_id_rules() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-session", session_id="codex:thread.001")
        with orch_root(root):
            list_payload, list_code = server.api_workflow_runs(session_id="codex:thread.001")
            detail_payload, detail_code = server.api_workflow_run("run-session", session_id="codex:thread.001")
        assert list_code == detail_code == 200
        assert [row["run_id"] for row in list_payload["workflow_runs"]] == ["run-session"]
        assert detail_payload["workflow_run"]["run"]["chat_session_id"] == "codex:thread.001"


def test_invalid_params_rejected() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        with orch_root(root):
            bad_run, bad_run_code = server.api_workflow_run("../x")
            bad_state, bad_state_code = server.api_workflow_runs(state="bogus")
            bad_session, bad_session_code = server.api_workflow_runs(session_id="../x")
        assert bad_run_code == 400
        assert bad_run == {"error": "invalid_run_id"}
        assert bad_state_code == 400
        assert bad_state == {"error": "invalid_run_state"}
        assert bad_session_code == 400
        assert bad_session == {"error": "invalid_session_id"}


def test_env_override_root() -> None:
    server = load_server()
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-env")
        with orch_root(root):
            payload, code = server.api_workflow_runs()
        assert code == 200
        assert payload["roots"] == [str(root)]
        assert [row["run_id"] for row in payload["workflow_runs"]] == ["run-env"]


def main() -> None:
    tests = [
        test_list_empty_root,
        test_list_and_filters,
        test_detail_shapes,
        test_detail_missing_run,
        test_corrupt_run_row,
        test_fallback_rows_have_mtime_when_bridge_fails,
        test_staleness_honors_timezone_offsets,
        test_duplicate_run_detail_can_select_root,
        test_session_filters_use_session_id_rules,
        test_invalid_params_rejected,
        test_env_override_root,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
