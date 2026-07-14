#!/usr/bin/env python3
"""Tests for the durable workflow-run store."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "organization/runtime/workflows/scripts/run_store.py"


def load_run_store_module():
    spec = importlib.util.spec_from_file_location("run_store", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


run_store = load_run_store_module()


def valid_run(**overrides) -> dict:
    candidate = {
        "run_version": "1",
        "run_id": "run-store",
        "task_id": "TSK-run-store",
        "request_id": "req-run-store",
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


def test_store_and_reload_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run()
        run_store.store_run(state_root, run)
        assert_equal(run_store.load_run(state_root, run["run_id"]), run, "stored run")
        tmp_files = list((state_root / "runs").glob("*.tmp")) + list((state_root / "runs").glob(".*.tmp"))
        assert_equal(tmp_files, [], "tmp files")


def test_store_rejects_schema_invalid() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run()
        run.pop("activation")
        try:
            run_store.store_run(state_root, run)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", "schema error class")
        else:
            raise AssertionError("schema-invalid run should be rejected")
        canonical = state_root / "runs" / "run-store.json"
        assert not canonical.exists(), "canonical invalid run must not be created"
        error_artifact = state_root / "runs" / "run-store.error.json"
        error_payload = json.loads(error_artifact.read_text(encoding="utf-8"))
        assert_equal(error_payload["operation"], "store", "error artifact operation")
        assert "missing_required_field:activation" in error_payload["errors"]


def test_rejects_reserved_artifact_suffix_ids() -> None:
    for run_id in ("run.error", "run.corrupt-1"):
        try:
            run_store.validate_artifact_id(run_id, "run_id")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", f"reserved suffix class {run_id}")
            assert "reserved run-store artifact suffixes" in exc.errors[0]
        else:
            raise AssertionError(f"reserved artifact suffix should be rejected: {run_id}")


def test_store_wraps_json_serialization_failures() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(x_future={"not-json": object()})
        try:
            run_store.store_run(state_root, run)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", "serialization schema class")
            assert "payload must be JSON serializable" in exc.errors[0]
        else:
            raise AssertionError("non-serializable payload should be rejected")
        assert not (state_root / "runs" / "run-store.json").exists()


def test_load_missing_run() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        try:
            run_store.load_run(state_root, "missing-run")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "run_not_found", "missing error class")
        else:
            raise AssertionError("missing run should be rejected")
        assert not (state_root / "runs" / "missing-run.error.json").exists()


def test_load_corrupt_json_quarantines() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-corrupt"
        canonical = state_root / "runs" / f"{run_id}.json"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        for index in (1, 2):
            canonical.write_text('{"run_id": tru', encoding="utf-8")
            try:
                run_store.load_run(state_root, run_id)
            except run_store.RunStoreError as exc:
                assert_equal(exc.reason_class, "corrupt_json", f"corrupt error class {index}")
            else:
                raise AssertionError("corrupt run should be rejected")
            assert (state_root / "runs" / f"{run_id}.corrupt-{index}.json").exists()
        error_payload = json.loads((state_root / "runs" / f"{run_id}.error.json").read_text(encoding="utf-8"))
        assert_equal(error_payload["operation"], "load", "corrupt error operation")
        assert_equal(error_payload["reason_class"], "corrupt_json", "corrupt error artifact")


def test_load_non_utf8_payload_quarantines() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run_id = "run-non-utf8"
        canonical = state_root / "runs" / f"{run_id}.json"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"\xff\xfe\xfa")
        try:
            run_store.load_run(state_root, run_id)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "corrupt_json", "non-utf8 error class")
        else:
            raise AssertionError("non-UTF-8 run should be rejected")
        assert (state_root / "runs" / f"{run_id}.corrupt-1.json").exists()
        error_payload = json.loads((state_root / "runs" / f"{run_id}.error.json").read_text(encoding="utf-8"))
        assert_equal(error_payload["reason_class"], "corrupt_json", "non-utf8 error artifact")


def test_load_rejects_embedded_run_id_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        canonical = state_root / "runs" / "run-requested.json"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(json.dumps(valid_run(run_id="run-embedded")) + "\n", encoding="utf-8")
        try:
            run_store.load_run(state_root, "run-requested")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", "mismatched id class")
            assert "run_id must match requested run_id 'run-requested'" in exc.errors
        else:
            raise AssertionError("embedded run_id mismatch should be rejected")


def test_interrupted_write_preserves_previous_state() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(run_id="run-interrupt")
        run_store.store_run(state_root, run)
        tmp = state_root / "runs" / ".run-interrupt.json.deadbeef.tmp"
        tmp.write_text("{garbage", encoding="utf-8")
        assert_equal(run_store.load_run(state_root, "run-interrupt"), run, "canonical run after tmp")


def test_state_conflict_guard() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(run_id="run-conflict")
        run_store.store_run(state_root, run)
        updated = dict(run)
        updated["run_state"] = "step_queued"
        updated["goal_state"] = "active"
        try:
            run_store.store_run(state_root, updated, expected_current_state="step_queued")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "state_conflict", "state conflict class")
        else:
            raise AssertionError("conflicting state transition should be rejected")
        assert_equal(run_store.load_run(state_root, "run-conflict"), run, "unchanged canonical run")


def test_state_conflict_guard_rejects_missing_canonical_file() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(run_id="run-missing-guard")
        try:
            run_store.store_run(state_root, run, expected_current_state="created")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "state_conflict", "missing guarded file class")
            assert "found missing run" in exc.errors[0]
        else:
            raise AssertionError("guarded store should reject missing canonical run")
        error_payload = json.loads((state_root / "runs" / "run-missing-guard.error.json").read_text(encoding="utf-8"))
        assert_equal(error_payload["reason_class"], "state_conflict", "missing guarded file artifact")


def test_terminal_requires_terminal_status() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(
            run_id="run-terminal",
            run_state="complete",
            goal_state="complete",
            terminal={"status": None, "reason": None},
        )
        try:
            run_store.store_run(state_root, run)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", "terminal schema class")
            assert "terminal_status_required_for_terminal_state" in exc.errors
        else:
            raise AssertionError("terminal run without terminal status should be rejected")


def test_activation_fields_used_by_drain_are_required() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(run_id="run-missing-context")
        activation = dict(run["activation"])
        activation.pop("context_scope")
        run["activation"] = activation
        try:
            run_store.store_run(state_root, run)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", "missing context class")
            assert "activation.context_scope must be a json object" in exc.errors
        else:
            raise AssertionError("missing activation.context_scope should be rejected")

    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(run_id="run-missing-scope")
        activation = dict(run["activation"])
        activation.pop("activation_scope")
        run["activation"] = activation
        try:
            run_store.store_run(state_root, run)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "schema_invalid", "missing activation scope class")
            assert "activation.activation_scope must be a json object" in exc.errors
        else:
            raise AssertionError("missing activation.activation_scope should be rejected")


def test_extra_keys_are_allowed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(
            run_id="run-future",
            transition_provenance=[{"transition": "create_run"}],
            x_future=1,
        )
        run_store.store_run(state_root, run)
        assert_equal(run_store.load_run(state_root, "run-future"), run, "future fields")


def test_provider_execution_state_is_durable_and_bounded() -> None:
    execution = {
        "execution_version": "1",
        "step_id": "review",
        "adapter_id": "claude_headless_p0",
        "work_order_digest": "sha256:" + "a" * 64,
        "adapter_request_digest": "sha256:" + "b" * 64,
        "context_snapshot_digest": "sha256:" + "c" * 64,
        "phase": "invoking",
        "attempt_number": 1,
        "attempt_id": "provider-attempt-test",
        "timeout_seconds": 1800,
        "lease": {
            "lease_id": "provider-lease-test",
            "claimed_by": {"principal_type": "harness_runner"},
            "claimed_at": "2026-07-14T00:00:00+00:00",
            "last_heartbeat_at": "2026-07-14T00:00:00+00:00",
            "lease_expires_at": "2026-07-14T00:01:30+00:00",
        },
        "retry": {
            "last_failure_fingerprint": None,
            "consecutive_failures": 0,
            "auto_retries_used": 0,
            "max_auto_retries": 5,
        },
        "last_outcome": None,
    }
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        run = valid_run(provider_execution=execution)
        run_store.store_run(state_root, run)
        assert_equal(run_store.load_run(state_root, run["run_id"])["provider_execution"], execution, "execution roundtrip")
        run["provider_execution"]["timeout_seconds"] = 86401
        try:
            run_store.store_run(state_root, run)
        except run_store.RunStoreError as exc:
            assert "provider_execution.timeout_seconds must be between 1 and 86400" in exc.errors
        else:
            raise AssertionError("provider execution timeout ceiling must be enforced")


def main() -> None:
    tests = [
        test_store_and_reload_roundtrip,
        test_store_rejects_schema_invalid,
        test_rejects_reserved_artifact_suffix_ids,
        test_store_wraps_json_serialization_failures,
        test_load_missing_run,
        test_load_corrupt_json_quarantines,
        test_load_non_utf8_payload_quarantines,
        test_load_rejects_embedded_run_id_mismatch,
        test_interrupted_write_preserves_previous_state,
        test_state_conflict_guard,
        test_state_conflict_guard_rejects_missing_canonical_file,
        test_terminal_requires_terminal_status,
        test_activation_fields_used_by_drain_are_required,
        test_extra_keys_are_allowed,
        test_provider_execution_state_is_durable_and_bounded,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
