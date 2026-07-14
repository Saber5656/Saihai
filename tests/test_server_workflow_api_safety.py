#!/usr/bin/env python3
"""Adversarial HTTP tests for the read-only workflow-run viewer APIs."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
TRANSCRIPT_LEAK_SENTINEL = "TRANSCRIPT-LEAK-SENTINEL"
PROMPT_LEAK_SENTINEL = "PROMPT-LEAK-SENTINEL"
PROMPT_ALIAS_LEAK_SENTINEL = "PROMPT-ALIAS-LEAK-SENTINEL"
CAMEL_PROMPT_LEAK_SENTINEL = "CAMEL-PROMPT-LEAK-SENTINEL"
CAMEL_TRANSCRIPT_LEAK_SENTINEL = "CAMEL-TRANSCRIPT-LEAK-SENTINEL"
USER_TRANSCRIPT_LEAK_SENTINEL = "USER-TRANSCRIPT-LEAK-SENTINEL"
TRANSCRIPT_TEXT_LEAK_SENTINEL = "TRANSCRIPT-TEXT-LEAK-SENTINEL"
PROVIDER_TRANSCRIPT_TEXT_LEAK_SENTINEL = "PROVIDER-TRANSCRIPT-TEXT-LEAK-SENTINEL"
USER_PROMPT_TEXT_LEAK_SENTINEL = "USER-PROMPT-TEXT-LEAK-SENTINEL"


def load_server():
    spec = importlib.util.spec_from_file_location("saihai_server_safety", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_run(
    root: Path,
    run_id: str,
    *,
    run_state: str = "step_queued",
    task_id: str = "TSK-safety",
    session_id: str = "session-safety",
    step_id: str = "review",
) -> dict:
    terminal = {"status": "complete", "reason": "report_valid"} if run_state == "complete" else {}
    payload = {
        "run_id": run_id,
        "task_id": task_id,
        "request_id": f"req-{run_id}",
        "workflow_id": "single_step_external_review",
        "run_state": run_state,
        "goal_state": "complete" if run_state == "complete" else "active",
        "current_step": step_id,
        "iteration": 1,
        "terminal": terminal,
        "activation": {"activation_status": "approved"},
        "requester": {"chat_session_id": session_id, "frontdoor": "codex"},
        "transitions": [
            {
                "seq": 1,
                "from_state": "created",
                "to_state": run_state,
                "occurred_at": "2026-07-14T00:00:00+0000",
            }
        ],
    }
    write_json(root / "runs" / f"{run_id}.json", payload)
    return payload


@contextmanager
def http_server(state_root: Path, *, state_roots: list[tuple[str, Path]] | None = None):
    server = load_server()
    previous_env = os.environ.get("SAIHAI_ORCH_STATE_ROOT")
    previous_state_roots = server.STATE_ROOTS
    os.environ["SAIHAI_ORCH_STATE_ROOT"] = str(state_root)
    if state_roots is not None:
        server.STATE_ROOTS = state_roots
    instance = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, f"http://127.0.0.1:{instance.server_address[1]}"
    finally:
        instance.shutdown()
        instance.server_close()
        worker.join(timeout=5)
        server.STATE_ROOTS = previous_state_roots
        if previous_env is None:
            os.environ.pop("SAIHAI_ORCH_STATE_ROOT", None)
        else:
            os.environ["SAIHAI_ORCH_STATE_ROOT"] = previous_env


def request(base: str, path: str, *, method: str = "GET") -> tuple[int, object, str]:
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    try:
        payload: object = json.loads(raw)
    except ValueError:
        payload = raw
    return status, payload, raw


def test_g1_run_id_validation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        root = base / "orch"
        root.mkdir()
        leak = base / "leak.json"
        leak.write_text("DO-NOT-READ", encoding="utf-8")
        with http_server(root) as (_, url):
            status, payload, raw = request(url, "/api/workflow-run?run=..%2Fleak")
        assert status == 400
        assert payload == {"error": "invalid_run_id"}
        assert "DO-NOT-READ" not in raw
        assert leak.read_text(encoding="utf-8") == "DO-NOT-READ"


def test_g2_raw_path_inputs_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        with http_server(root) as (_, url):
            for value in ("..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "a%2Fb"):
                status, payload, _ = request(url, f"/api/workflow-run?run={value}")
                assert status == 400, value
                assert payload == {"error": "invalid_run_id"}, value


def test_g3_corrupt_run_is_metadata_not_http_500() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-good")
        corrupt = root / "runs" / "run-corrupt.json"
        corrupt.write_text("{truncated", encoding="utf-8")
        with http_server(root) as (_, url):
            list_status, list_payload, list_raw = request(url, "/api/workflow-runs")
            detail_status, detail_payload, detail_raw = request(url, "/api/workflow-run?run=run-corrupt")
        assert list_status == detail_status == 200
        rows = {row["run_id"]: row for row in list_payload["workflow_runs"]}
        assert rows["run-corrupt"]["view_error"] == "corrupt_json"
        assert detail_payload["workflow_run"]["run"]["view_error"] == "corrupt_json"
        assert "Traceback" not in list_raw + detail_raw


def test_schema_mismatch_is_metadata_not_http_500() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        write_json(root / "runs" / "run-schema.json", ["not", "an", "object"])
        with http_server(root) as (_, url):
            list_status, list_payload, list_raw = request(url, "/api/workflow-runs")
            detail_status, detail_payload, detail_raw = request(url, "/api/workflow-run?run=run-schema")
        assert list_status == detail_status == 200
        assert list_payload["workflow_runs"][0]["view_error"] == "invalid_run_json"
        assert detail_payload["workflow_run"]["run"]["view_error"] == "invalid_run_json"
        assert "Traceback" not in list_raw + detail_raw


def test_deep_json_is_stable_metadata_not_http_500() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        path = root / "runs" / "run-deep.json"
        path.parent.mkdir(parents=True)
        depth = 1_200
        path.write_text('{"nested":' * depth + '"leaf"' + '}' * depth, encoding="utf-8")
        with http_server(root) as (_, url):
            list_status, list_payload, list_raw = request(url, "/api/workflow-runs")
            detail_status, detail_payload, detail_raw = request(url, "/api/workflow-run?run=run-deep")
        assert list_status == detail_status == 200
        assert list_payload["workflow_runs"][0]["view_error"] == "too_deep"
        assert detail_payload["workflow_run"]["run"]["view_error"] == "too_deep"
        assert "Traceback" not in list_raw + detail_raw


def test_g4_oversize_run_does_not_hide_healthy_rows() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-good")
        oversized = root / "runs" / "run-big.json"
        oversized.write_bytes(b"{" + b"x" * 1_000_001 + b"}")
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-runs")
        assert status == 200
        rows = {row["run_id"]: row for row in payload["workflow_runs"]}
        assert rows["run-big"]["view_error"] == "oversize"
        assert rows["run-good"]["run_state"] == "step_queued"


def test_g5_symlink_escape_is_rejected_as_outside_root() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        root = base / "orch"
        runs = root / "runs"
        runs.mkdir(parents=True)
        outside = base / "outside.json"
        write_json(outside, {"run_id": "run-evil", "marker": "OUTSIDE-LEAK-SENTINEL"})
        (runs / "run-evil.json").symlink_to(outside)
        with http_server(root) as (_, url):
            status, payload, raw = request(url, "/api/workflow-runs")
            detail_status, detail_payload, detail_raw = request(url, "/api/workflow-run?run=run-evil")
        assert status == 200
        assert detail_status == 404
        assert payload["workflow_runs"][0]["view_error"] == "outside_root"
        assert detail_payload == {"error": "run_not_found"}
        assert "OUTSIDE-LEAK-SENTINEL" not in raw + detail_raw


def test_g6_g7_transcript_and_prompt_are_confined() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        run = make_run(root, "run-sensitive", run_state="complete")
        run["prompt"] = PROMPT_LEAK_SENTINEL
        run["user_prompt"] = PROMPT_ALIAS_LEAK_SENTINEL
        run["activation"]["userPrompt"] = CAMEL_PROMPT_LEAK_SENTINEL
        write_json(root / "runs" / "run-sensitive.json", run)
        transcript = root / "provider-evidence" / "run-sensitive" / "transcript.txt"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(TRANSCRIPT_LEAK_SENTINEL, encoding="utf-8")
        write_json(
            root / "provider-evidence" / "run-sensitive" / "review-provider-evidence.json",
            {
                "provider": "fake",
                "transcript_path": str(transcript),
                "raw_transcript": TRANSCRIPT_LEAK_SENTINEL,
                "rawTranscript": CAMEL_TRANSCRIPT_LEAK_SENTINEL,
                "nested": {
                    "userTranscript": USER_TRANSCRIPT_LEAK_SENTINEL,
                    "transcriptText": TRANSCRIPT_TEXT_LEAK_SENTINEL,
                    "providerTranscriptText": PROVIDER_TRANSCRIPT_TEXT_LEAK_SENTINEL,
                    "userPromptText": USER_PROMPT_TEXT_LEAK_SENTINEL,
                },
            },
        )
        with http_server(root) as (_, url):
            responses = [
                request(url, "/api/workflow-runs"),
                request(url, "/api/workflow-run?run=run-sensitive"),
                request(url, "/api/workflow-lock"),
            ]
        assert all(status == 200 for status, _, _ in responses)
        combined = "".join(raw for _, _, raw in responses)
        assert TRANSCRIPT_LEAK_SENTINEL not in combined
        assert PROMPT_LEAK_SENTINEL not in combined
        assert PROMPT_ALIAS_LEAK_SENTINEL not in combined
        assert CAMEL_PROMPT_LEAK_SENTINEL not in combined
        assert CAMEL_TRANSCRIPT_LEAK_SENTINEL not in combined
        assert USER_TRANSCRIPT_LEAK_SENTINEL not in combined
        assert TRANSCRIPT_TEXT_LEAK_SENTINEL not in combined
        assert PROVIDER_TRANSCRIPT_TEXT_LEAK_SENTINEL not in combined
        assert USER_PROMPT_TEXT_LEAK_SENTINEL not in combined
        assert responses[1][1]["workflow_run"]["evidence"]["transcript_path"] == str(transcript)
        assert responses[0][1]["workflow_runs"][0]["run_state"] == "complete"
        assert responses[1][1]["workflow_run"]["run"]["terminal"]["status"] == "complete"


def test_missing_and_traversal_artifact_refs_are_not_followed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        root = base / "orch"
        outside = base / "outside.txt"
        outside.write_text("ARTIFACT-LEAK-SENTINEL", encoding="utf-8")
        run = make_run(root, "run-artifacts", run_state="complete")
        run["context_refs"] = [
            {"type": "file", "value": "../outside.txt"},
            {"type": "file", "value": str(outside)},
        ]
        run["work_order_path"] = str(root / "work-orders" / "run-artifacts" / "review.json")
        run["report_path"] = str(root / "reports" / "run-artifacts" / "review-external-review-report.json")
        run["evidence_path"] = str(root / "provider-evidence" / "run-artifacts" / "review-provider-evidence.json")
        write_json(root / "runs" / "run-artifacts.json", run)
        with http_server(root) as (_, url):
            status, payload, raw = request(url, "/api/workflow-run?run=run-artifacts")
        assert status == 200
        detail = payload["workflow_run"]
        assert detail["work_order"] is None
        assert detail["report"] is None
        assert detail["evidence"] is None
        assert "ARTIFACT-LEAK-SENTINEL" not in raw


def test_artifact_symlink_escapes_are_not_followed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        root = base / "orch"
        make_run(root, "run-linked", run_state="complete")
        outside = base / "outside.json"
        write_json(outside, {"summary": "ARTIFACT-SYMLINK-LEAK-SENTINEL"})
        paths = [
            root / "work-orders" / "run-linked" / "review.json",
            root / "reports" / "run-linked" / "review-external-review-report.json",
            root / "provider-evidence" / "run-linked" / "review-provider-evidence.json",
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(outside)
        with http_server(root) as (_, url):
            status, payload, raw = request(url, "/api/workflow-run?run=run-linked")
        assert status == 200
        detail = payload["workflow_run"]
        assert detail["work_order"] is None
        assert detail["report"] is None
        assert detail["evidence"] is None
        assert "ARTIFACT-SYMLINK-LEAK-SENTINEL" not in raw


def test_g8_post_cannot_mutate_workflow_api() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-runs", method="POST")
        assert status == 405
        assert payload == {"error": "method_not_allowed"}


def test_g9_orchestrator_root_is_not_a_session_root() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        orch = base / "orch"
        sessions = base / "sessions"
        write_json(orch / "active-execution-context.json", {"session_id": "orch-must-not-appear"})
        sessions.mkdir()
        with http_server(orch, state_roots=[("codex", sessions)]) as (_, url):
            status, payload, raw = request(url, "/api/sessions")
        assert status == 200
        assert "orch-must-not-appear" not in raw
        assert all(item.get("id") != "orch-must-not-appear" for item in payload.get("sessions", []))


def test_g10_lock_missing_owner_is_nonfatal() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        lock = root / "locks" / "global-advisory.lock.d"
        lock.mkdir(parents=True)
        old = time.time() - 600
        os.utime(lock, (old, old))
        with http_server(root) as (_, url):
            status, payload, raw = request(url, "/api/workflow-lock")
        assert status == 200
        assert "Traceback" not in raw
        assert len(payload["locks"]) == 1
        assert payload["locks"][0]["locked"] is True
        assert payload["locks"][0]["stale"] is True
        assert payload["locks"][0].get("owner") in ({}, None)


def test_lock_owner_uses_safe_projection() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        lock = root / "locks" / "global-advisory.lock.d"
        write_json(
            lock / "owner.json",
            {
                "lock_version": "1",
                "lock_type": "workflow-run-global",
                "pid": 999999,
                "hostname": "fixture-host",
                "created_at": "2026-07-14T00:00:00+0000",
                "operation": "fixture",
                "run_id": "run-lock",
                "principal_type": "fixture",
                "prompt": "LOCK-PROMPT-LEAK",
                "user_prompt": "LOCK-ALIAS-LEAK",
                "unexpected": "LOCK-UNEXPECTED-LEAK",
            },
        )
        with http_server(root) as (_, url):
            status, payload, raw = request(url, "/api/workflow-lock")
        assert status == 200
        owner = payload["locks"][0]["owner"]
        assert owner["run_id"] == "run-lock"
        assert "LOCK-PROMPT-LEAK" not in raw
        assert "LOCK-ALIAS-LEAK" not in raw
        assert "LOCK-UNEXPECTED-LEAK" not in raw


def test_deep_lock_owner_redaction_is_nonfatal() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        nested: object = "leaf"
        for _ in range(1_200):
            nested = {"nested": nested}

        class DeepOwnerLock:
            @staticmethod
            def inspect_global_lock(_root: Path) -> dict:
                return {
                    "decision": "ok",
                    "locked": True,
                    "stale": False,
                    "owner": {"operation": nested},
                }

        with http_server(root) as (server, url):
            server._run_lock = lambda: DeepOwnerLock
            status, payload, raw = request(url, "/api/workflow-lock")
        assert status == 200
        assert payload["locks"][0]["owner"] is None
        assert "Traceback" not in raw


def test_g11_large_inventory_is_fast_and_not_truncated_at_500() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        for index in range(500):
            make_run(root, f"run-{index:04d}")
        started = time.monotonic()
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-runs")
        duration = time.monotonic() - started
        assert status == 200
        assert len(payload["workflow_runs"]) == 500
        assert payload["truncated"] is False
        assert duration < 5


def test_discovery_limit_reports_truncation() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        server = load_server()
        for index in range(server.MAX_RUN_DISCOVERY_FILES + 1):
            make_run(root, f"run-{index:04d}")
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-runs")
        assert status == 200
        assert len(payload["workflow_runs"]) == server.MAX_RUN_DISCOVERY_FILES
        assert payload["truncated"] is True


def test_run_filters_are_applied_before_result_cap() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        for index in range(500):
            make_run(
                root,
                f"run-noise-{index:04d}",
                task_id="TSK-noise",
                session_id="session-noise",
            )
        make_run(root, "run-task-a", task_id="TSK-target")
        make_run(root, "run-task-b", task_id="TSK-target")
        make_run(root, "run-session", session_id="session-target")
        make_run(root, "run-state", run_state="complete")
        with http_server(root) as (server, url):
            paths, scan_truncated = server._run_files_with_metadata(root)
            task_status, task_payload, _ = request(url, "/api/workflow-runs?task=TSK-target")
            session_status, session_payload, _ = request(url, "/api/workflow-runs?session=session-target")
            state_status, state_payload, _ = request(url, "/api/workflow-runs?state=complete")
        assert len(paths) == 504
        assert scan_truncated is False
        assert task_status == session_status == state_status == 200
        assert [row["run_id"] for row in task_payload["workflow_runs"]] == ["run-task-b", "run-task-a"]
        assert [row["run_id"] for row in session_payload["workflow_runs"]] == ["run-session"]
        assert [row["run_id"] for row in state_payload["workflow_runs"]] == ["run-state"]
        assert task_payload["truncated"] is False
        assert session_payload["truncated"] is False
        assert state_payload["truncated"] is False


def test_total_directory_scan_is_bounded() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        server = load_server()
        runs = root / "runs"
        runs.mkdir(parents=True)
        for index in range(server.MAX_RUN_SCAN_ENTRIES + 1):
            (runs / f"ignored-{index:04d}.txt").write_text("ignored", encoding="utf-8")
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-runs")
        assert status == 200
        assert payload["workflow_runs"] == []
        assert payload["truncated"] is True


def test_detail_artifact_count_is_bounded_and_reported() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        server = load_server()
        make_run(root, "run-history", run_state="complete")
        for index in range(server.MAX_DETAIL_ARTIFACT_FILES + 1):
            write_json(
                root / "transitions" / "run-history" / f"{index:04d}.json",
                {"seq": index, "outcome": "fixture"},
            )
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-run?run=run-history")
        assert status == 200
        detail = payload["workflow_run"]
        assert len(detail["transitions"]) == server.MAX_DETAIL_ARTIFACT_FILES
        assert detail["artifact_truncation"]["transitions"]["truncated"] is True
        assert detail["artifact_truncation"]["transitions"]["files_returned"] == server.MAX_DETAIL_ARTIFACT_FILES


def test_detail_artifact_byte_limit_counts_json_scalars() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        directory = root / "transitions" / "run-scalars"
        directory.mkdir(parents=True)
        for index in range(3):
            (directory / f"{index:04d}.json").write_text('"abc"', encoding="utf-8")
        server = load_server()
        server.MAX_DETAIL_ARTIFACT_BYTES = 10
        artifacts, metadata = server._artifact_list_with_metadata(directory, "*.json", root)
        assert artifacts == []
        assert metadata == {"truncated": True, "files_returned": 0, "bytes_read": 10}


def test_detail_artifact_scan_limit_is_reported() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        server = load_server()
        make_run(root, "run-scan", run_state="complete")
        directory = root / "transitions" / "run-scan"
        directory.mkdir(parents=True)
        for index in range(server.MAX_DETAIL_ARTIFACT_SCAN_ENTRIES + 1):
            (directory / f"ignored-{index:04d}.txt").write_text("ignored", encoding="utf-8")
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-run?run=run-scan")
        assert status == 200
        metadata = payload["workflow_run"]["artifact_truncation"]["transitions"]
        assert metadata == {"truncated": True, "files_returned": 0, "bytes_read": 0}


def test_rejection_artifact_count_limit_is_reported() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        server = load_server()
        make_run(root, "run-rejections", run_state="complete")
        for index in range(server.MAX_DETAIL_ARTIFACT_FILES + 1):
            write_json(
                root / "reports" / "run-rejections" / f"review-rejection-{index:04d}.json",
                {"reason": "fixture"},
            )
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-run?run=run-rejections")
        assert status == 200
        detail = payload["workflow_run"]
        assert len(detail["rejections"]) == server.MAX_DETAIL_ARTIFACT_FILES
        metadata = detail["artifact_truncation"]["rejections"]
        assert metadata["truncated"] is True
        assert metadata["files_returned"] == server.MAX_DETAIL_ARTIFACT_FILES


def test_rejection_artifact_byte_limit_is_reported() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-rejection-bytes", run_state="complete")
        for index in range(2):
            write_json(
                root / "reports" / "run-rejection-bytes" / f"review-rejection-{index:04d}.json",
                {"reason": "fixture"},
            )
        with http_server(root) as (server, url):
            first = root / "reports" / "run-rejection-bytes" / "review-rejection-0000.json"
            server.MAX_DETAIL_ARTIFACT_BYTES = first.stat().st_size
            status, payload, _ = request(url, "/api/workflow-run?run=run-rejection-bytes")
        assert status == 200
        detail = payload["workflow_run"]
        assert len(detail["rejections"]) == 1
        metadata = detail["artifact_truncation"]["rejections"]
        assert metadata["truncated"] is True
        assert metadata["files_returned"] == 1
        assert metadata["bytes_read"] == first.stat().st_size


def test_g12_unknown_query_params_are_ignored() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        make_run(root, "run-known")
        with http_server(root) as (_, url):
            status, payload, _ = request(url, "/api/workflow-runs?foo=bar")
        assert status == 200
        assert [item["run_id"] for item in payload["workflow_runs"]] == ["run-known"]


def test_server_boots_with_no_orchestrator_root() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        missing = Path(raw_tmp) / "missing"
        with http_server(missing) as (_, url):
            list_status, list_payload, _ = request(url, "/api/workflow-runs")
            detail_status, detail_payload, _ = request(url, "/api/workflow-run?run=run-missing")
            lock_status, lock_payload, _ = request(url, "/api/workflow-lock")
        assert list_status == lock_status == 200
        assert list_payload["workflow_runs"] == []
        assert list_payload["roots"] == []
        assert list_payload["truncated"] is False
        assert detail_status == 404
        assert detail_payload == {"error": "run_not_found"}
        assert lock_payload["locks"] == []


def main() -> None:
    tests = [
        test_g1_run_id_validation,
        test_g2_raw_path_inputs_are_rejected,
        test_g3_corrupt_run_is_metadata_not_http_500,
        test_schema_mismatch_is_metadata_not_http_500,
        test_deep_json_is_stable_metadata_not_http_500,
        test_g4_oversize_run_does_not_hide_healthy_rows,
        test_g5_symlink_escape_is_rejected_as_outside_root,
        test_g6_g7_transcript_and_prompt_are_confined,
        test_missing_and_traversal_artifact_refs_are_not_followed,
        test_artifact_symlink_escapes_are_not_followed,
        test_g8_post_cannot_mutate_workflow_api,
        test_g9_orchestrator_root_is_not_a_session_root,
        test_g10_lock_missing_owner_is_nonfatal,
        test_lock_owner_uses_safe_projection,
        test_deep_lock_owner_redaction_is_nonfatal,
        test_g11_large_inventory_is_fast_and_not_truncated_at_500,
        test_discovery_limit_reports_truncation,
        test_run_filters_are_applied_before_result_cap,
        test_total_directory_scan_is_bounded,
        test_detail_artifact_count_is_bounded_and_reported,
        test_detail_artifact_byte_limit_counts_json_scalars,
        test_detail_artifact_scan_limit_is_reported,
        test_rejection_artifact_count_limit_is_reported,
        test_rejection_artifact_byte_limit_is_reported,
        test_g12_unknown_query_params_are_ignored,
        test_server_boots_with_no_orchestrator_root,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
