#!/usr/bin/env python3
"""Tests for the workflow-run global advisory lock."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"


def load_module(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


run_lock = load_module("run_lock", SCRIPT_DIR / "run_lock.py")
run_store = load_module("run_store", SCRIPT_DIR / "run_store.py")


def valid_run(**overrides) -> dict:
    candidate = {
        "run_version": "1",
        "run_id": "run-lock",
        "task_id": "TSK-run-lock",
        "request_id": "req-run-lock",
        "workflow_id": "single_step_external_review",
        "goal_state": "approved",
        "run_state": "created",
        "current_step": "review",
        "iteration": 1,
        "max_steps": 1,
        "step_history": [],
        "activation": {
            "activation_version": "1",
            "activation_source": "manual_cli",
            "activation_status": "approved",
            "approved_by": "manual_operator",
            "approved_at": "2026-07-04T00:00:00+0900",
            "workflow_selection": {
                "status": "selected",
                "workflow_id": "single_step_external_review",
                "initial_step": "review",
                "safety_class": "readonly",
                "required_safety_class": "readonly",
                "publication_gate_required": False,
                "required_gates": [],
            },
            "classification_provenance": {
                "source": "deterministic_fixture",
                "confidence": 1.0,
                "evidence_refs": ["test-fixture"],
                "selector_threshold": 0.85,
                "tie_break": "deterministic_status_order:selected_waiting_human_blocked",
            },
            "context_scope": {
                "mode": "refs_only",
                "refs": ["organization/runtime/workflows/README.md"],
                "raw_transcript_sharing": "forbidden",
            },
            "activation_scope": {
                "allowed_paths": [],
                "allowed_ops": {"edit": False, "commit": False, "push": False, "network": False},
                "step_budget": 1,
                "expires_at": "run_terminal",
            },
            "next_action": "create_workflow_run",
        },
        "terminal": {"status": None, "reason": None},
        "requester": {"frontdoor": "manual"},
        "scheduling": {
            "scheduler_mode": "invocation-drain",
            "concurrency_group": "global",
            "state_persistence": "durable_state",
            "lock_policy": "global_advisory_lock",
            "concurrency": 1,
            "resume_policy": "manual",
        },
        "context_sharing": {
            "shared_run_state": "typed_durable_state",
            "step_local_snapshot": "immutable_step_attempt_snapshot",
            "provider_transcript": "confined_evidence_path_only",
        },
    }
    candidate.update(overrides)
    return candidate


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def write_stale_lock(state_root: Path, *, pid: int, owner_overrides: dict | None = None) -> Path:
    lock_path = run_lock.global_lock_path(state_root)
    lock_path.mkdir(parents=True)
    owner = {
        "lock_version": "1",
        "lock_type": "workflow-run-global",
        "pid": pid,
        "hostname": run_lock.current_hostname(),
        "process_start_token": run_lock.process_start_token(pid),
        "owner_nonce": "test-owner-nonce",
        "created_at": "2026-07-04T00:00:00+0900",
        "stale_after_seconds": 300.0,
        "operation": "test",
        "run_id": "run-stale",
        "principal_type": "manual_operator",
    }
    owner.update(owner_overrides or {})
    run_store.atomic_write_json(lock_path / "owner.json", owner)
    old = time.time() - 3600
    os.utime(lock_path, (old, old))
    return lock_path


def test_acquire_and_release() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        owner = run_lock.acquire_global_lock(state_root, operation="test", run_id="run-a")
        assert_equal(owner["pid"], os.getpid(), "owner pid")
        assert (run_lock.global_lock_path(state_root) / "owner.json").exists()
        run_lock.release_global_lock(state_root)
        assert_equal(run_lock.inspect_global_lock(state_root)["locked"], False, "released lock")


def test_contention_blocks_second_acquire() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        first = run_lock.acquire_global_lock(state_root, operation="first", run_id="run-first")
        try:
            run_lock.acquire_global_lock(state_root, operation="second", timeout_seconds=0.2)
        except run_lock.LockContentionError as exc:
            assert_equal(exc.reason_class, "lock_contention", "contention reason")
            assert_equal(exc.owner["pid"], first["pid"], "contention owner")
        else:
            raise AssertionError("second acquire should block")
        finally:
            run_lock.release_global_lock(state_root)


def test_context_manager_releases_on_exception() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        try:
            with run_lock.hold_global_lock(state_root, operation="boom"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        else:
            raise AssertionError("context body should raise")
        assert_equal(run_lock.inspect_global_lock(state_root)["locked"], False, "context released")


def test_stale_lock_reclaim() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        write_stale_lock(state_root, pid=999999999)
        owner = run_lock.acquire_global_lock(state_root, operation="reclaim", stale_after_seconds=300)
        assert_equal(owner["operation"], "reclaim", "reclaimed owner")
        run_lock.release_global_lock(state_root)


def test_stale_reclaim_skips_changed_owner() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        lock_path = write_stale_lock(state_root, pid=999999999)
        original_stale_lock_reason = run_lock.stale_lock_reason

        def mutate_then_stale(path: Path, *, stale_after_seconds: float) -> str:
            reason = original_stale_lock_reason(path, stale_after_seconds=stale_after_seconds)
            owner = run_lock.read_lock_owner(path)
            owner["owner_nonce"] = "changed-owner"
            run_store.atomic_write_json(path / "owner.json", owner)
            return reason

        run_lock.stale_lock_reason = mutate_then_stale
        try:
            reclaimed = run_lock.try_reclaim_stale_lock(lock_path, stale_after_seconds=300)
        finally:
            run_lock.stale_lock_reason = original_stale_lock_reason
        assert_equal(reclaimed, False, "changed owner reclaim")
        assert_equal(run_lock.read_lock_owner(lock_path)["owner_nonce"], "changed-owner", "changed owner preserved")


def test_live_lock_not_reclaimed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        write_stale_lock(state_root, pid=os.getpid())
        try:
            run_lock.acquire_global_lock(
                state_root,
                operation="live",
                timeout_seconds=0.2,
                stale_after_seconds=300,
            )
        except run_lock.LockContentionError as exc:
            assert_equal(exc.reason_class, "lock_contention", "live contention")
        else:
            raise AssertionError("live owner lock should not be reclaimed")
        finally:
            run_lock.release_global_lock(state_root)


def test_inspect_reports_stale() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        write_stale_lock(state_root, pid=999999999)
        status = run_lock.inspect_global_lock(state_root, stale_after_seconds=300)
        assert_equal(status["locked"], True, "stale locked")
        assert_equal(status["stale"], True, "stale status")
        assert status["stale_reason"], "stale reason should be non-empty"


def test_pid_reuse_start_token_reports_stale() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        original_process_start_token = run_lock.process_start_token
        run_lock.process_start_token = lambda pid: "current-start-token"
        try:
            lock_path = write_stale_lock(
                state_root,
                pid=os.getpid(),
                owner_overrides={"process_start_token": "old-start-token"},
            )
            reason = run_lock.stale_lock_reason(lock_path, stale_after_seconds=300)
        finally:
            run_lock.process_start_token = original_process_start_token
        assert "lock_owner_pid_reused" in reason, reason


def test_concurrency_limit() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_store.store_run(
            state_root,
            valid_run(run_id="run-queued", request_id="req-queued", run_state="step_queued", goal_state="active"),
        )
        try:
            run_lock.assert_p0_concurrency(state_root, target_run_id="run-target")
        except run_lock.LockContentionError as exc:
            assert_equal(exc.reason_class, "concurrency_limit_reached", "queued concurrency reason")
            assert_equal(exc.owner["inflight_run_ids"], ["run-queued"], "queued inflight runs")
        else:
            raise AssertionError("queued run should block concurrency")

        run_queued = run_store.load_run(state_root, "run-queued")
        run_queued["run_state"] = "complete"
        run_queued["goal_state"] = "complete"
        run_queued["terminal"] = {"status": "complete", "reason": "report_valid"}
        run_store.store_run(state_root, run_queued, expected_current_state="step_queued")

        run_store.store_run(
            state_root,
            valid_run(run_id="run-A", request_id="req-A", run_state="waiting_provider", goal_state="active"),
        )
        run_store.store_run(
            state_root,
            valid_run(run_id="run-B", request_id="req-B", run_state="step_queued", goal_state="active"),
        )
        try:
            run_lock.assert_p0_concurrency(state_root, target_run_id="run-B")
        except run_lock.LockContentionError as exc:
            assert_equal(exc.reason_class, "concurrency_limit_reached", "concurrency reason")
            assert_equal(exc.owner["inflight_run_ids"], ["run-A"], "inflight runs")
        else:
            raise AssertionError("inflight run should block concurrency")

        run_a = run_store.load_run(state_root, "run-A")
        run_a["run_state"] = "complete"
        run_a["goal_state"] = "complete"
        run_a["terminal"] = {"status": "complete", "reason": "report_valid"}
        run_store.store_run(state_root, run_a, expected_current_state="waiting_provider")
        run_lock.assert_p0_concurrency(state_root, target_run_id="run-B")


def test_safe_retry_after_release() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_lock.acquire_global_lock(state_root, operation="first")
        run_lock.release_global_lock(state_root)
        owner = run_lock.acquire_global_lock(state_root, operation="second")
        assert_equal(owner["operation"], "second", "second owner")
        run_lock.release_global_lock(state_root)


def main() -> None:
    tests = [
        test_acquire_and_release,
        test_contention_blocks_second_acquire,
        test_context_manager_releases_on_exception,
        test_stale_lock_reclaim,
        test_stale_reclaim_skips_changed_owner,
        test_live_lock_not_reclaimed,
        test_inspect_reports_stale,
        test_pid_reuse_start_token_reports_stale,
        test_concurrency_limit,
        test_safe_retry_after_release,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
