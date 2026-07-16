#!/usr/bin/env python3
"""Tests for the durable workflow-run store."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "organization/runtime/workflows/scripts/run_store.py"
SCRIPT_DIR = SCRIPT.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


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


def write_private_fixture(path: Path, payload: str | bytes) -> None:
    run_store.ensure_private_directory(path.parent)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)


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
        for index in (1, 2):
            write_private_fixture(canonical, '{"run_id": tru')
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
        write_private_fixture(canonical, b"\xff\xfe\xfa")
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
        write_private_fixture(canonical, json.dumps(valid_run(run_id="run-embedded")) + "\n")
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
        write_private_fixture(tmp, "{garbage")
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


def test_private_artifact_stat_and_exists_are_nofollow_and_noncreating() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        missing = state_root / "missing-parent" / "record.json"
        assert_equal(run_store.private_artifact_stat(missing), None, "missing private stat")
        assert_equal(run_store.private_artifact_exists(missing), False, "missing private exists")
        assert not missing.parent.exists(), "read-only metadata checks must not create a parent"

        artifact = state_root / "records" / "record.json"
        write_private_fixture(artifact, "private\n")
        metadata = run_store.private_artifact_stat(artifact)
        assert metadata is not None
        assert_equal(metadata.st_size, len(b"private\n"), "private artifact size")
        assert_equal(run_store.private_artifact_exists(artifact), True, "private artifact exists")

        artifact.chmod(0o644)
        try:
            run_store.private_artifact_stat(artifact)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "unsafe mode error class")
        else:
            raise AssertionError("wrong-mode private artifact must fail closed")

        artifact.unlink()
        victim = state_root / "victim.json"
        victim.write_text("victim\n", encoding="utf-8")
        artifact.symlink_to(victim)
        for operation in (
            run_store.private_artifact_stat,
            run_store.private_artifact_exists,
        ):
            try:
                operation(artifact)
            except run_store.RunStoreError as exc:
                assert_equal(exc.reason_class, "io_error", "symlink metadata error class")
            else:
                raise AssertionError("private artifact symlink must fail closed")
        assert_equal(victim.read_text(encoding="utf-8"), "victim\n", "symlink victim")

        artifact.unlink()
        victim.chmod(0o600)
        os.link(victim, artifact)
        try:
            run_store.private_artifact_stat(artifact)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "hardlink metadata error class")
        else:
            raise AssertionError("private artifact hardlink must fail closed")
        assert_equal(victim.read_text(encoding="utf-8"), "victim\n", "hardlink victim")


def test_list_private_artifacts_validates_matching_entries() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        missing = state_root / "missing-list"
        assert_equal(run_store.list_private_artifacts(missing, suffix=".json"), [], "missing list")
        assert not missing.exists(), "listing must not create a missing directory"

        directory = state_root / "records"
        first = directory / "item-02.json"
        second = directory / "item-01.json"
        ignored = directory / "notes.txt"
        write_private_fixture(first, "{}\n")
        write_private_fixture(second, "{}\n")
        write_private_fixture(ignored, "ignored\n")
        assert_equal(
            run_store.list_private_artifacts(directory, prefix="item-", suffix=".json"),
            [second, first],
            "literal filtered list",
        )

        ignored.unlink()
        ignored.symlink_to(first)
        assert_equal(
            run_store.list_private_artifacts(directory, prefix="item-", suffix=".json"),
            [second, first],
            "nonmatching symlink ignored",
        )

        matching_link = directory / "item-03.json"
        matching_link.symlink_to(first)
        try:
            run_store.list_private_artifacts(directory, prefix="item-", suffix=".json")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "matching symlink list class")
        else:
            raise AssertionError("matching list symlink must fail closed")
        matching_link.unlink()

        first.chmod(0o644)
        try:
            run_store.list_private_artifacts(directory, prefix="item-", suffix=".json")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "wrong-mode list class")
        else:
            raise AssertionError("matching wrong-mode artifact must fail closed")
        first.chmod(0o600)

        matching_directory = directory / "item-directory.json"
        matching_directory.mkdir(mode=0o700)
        try:
            run_store.list_private_artifacts(directory, prefix="item-", suffix=".json")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "nonregular list class")
        else:
            raise AssertionError("matching nonregular artifact must fail closed")
        matching_directory.rmdir()

        for unsafe_filter in ("../", "nested/"):
            try:
                run_store.list_private_artifacts(directory, prefix=unsafe_filter)
            except run_store.RunStoreError as exc:
                assert_equal(exc.reason_class, "io_error", "unsafe listing filter class")
            else:
                raise AssertionError("path-like listing filters must be rejected")

    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        actual = state_root / "actual"
        run_store.ensure_private_directory(actual)
        alias = state_root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        try:
            run_store.list_private_artifacts(alias, suffix=".json")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "symlink directory list class")
        else:
            raise AssertionError("symlinked listing directory must fail closed")


def test_unlink_private_file_is_descriptor_relative_and_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        missing = state_root / "missing-parent" / "missing.json"
        assert_equal(
            run_store.unlink_private_file(missing, missing_ok=True),
            False,
            "missing-ok unlink",
        )
        assert not missing.parent.exists(), "missing unlink must not create a parent"
        try:
            run_store.unlink_private_file(missing)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "missing unlink class")
        else:
            raise AssertionError("required missing unlink must fail")

        artifact = state_root / "records" / "delete.json"
        write_private_fixture(artifact, "delete me\n")
        assert_equal(run_store.unlink_private_file(artifact), True, "private unlink")
        assert not artifact.exists()

        victim = state_root / "victim.json"
        victim.write_text("keep me\n", encoding="utf-8")
        artifact.symlink_to(victim)
        try:
            run_store.unlink_private_file(artifact)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "symlink unlink class")
        else:
            raise AssertionError("symlink unlink must fail closed")
        assert artifact.is_symlink(), "failed unlink must preserve the symlink for inspection"
        assert_equal(victim.read_text(encoding="utf-8"), "keep me\n", "unlink victim")


def test_create_private_file_is_exclusive_nofollow_and_validates_existing() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        artifact = state_root / "secrets" / "principal.key"
        assert_equal(
            run_store.create_private_file(artifact, b"first-secret\n"),
            True,
            "created private file",
        )
        metadata = artifact.stat(follow_symlinks=False)
        assert_equal(stat.S_IMODE(metadata.st_mode), 0o600, "created private mode")
        assert_equal(metadata.st_nlink, 1, "created private link count")
        assert_equal(artifact.read_bytes(), b"first-secret\n", "created private payload")
        assert_equal(
            run_store.create_private_file(artifact, b"must-not-overwrite\n"),
            False,
            "validated existing private file",
        )
        assert_equal(artifact.read_bytes(), b"first-secret\n", "existing private payload")

        artifact.chmod(0o644)
        try:
            run_store.create_private_file(artifact, b"must-not-write\n")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "wrong-mode create class")
        else:
            raise AssertionError("wrong-mode existing private file must fail closed")
        artifact.unlink()

        victim = state_root / "victim.key"
        victim.write_bytes(b"victim\n")
        victim.chmod(0o600)
        artifact.symlink_to(victim)
        try:
            run_store.create_private_file(artifact, b"must-not-write\n")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "symlink create class")
        else:
            raise AssertionError("symlink existing private file must fail closed")
        assert_equal(victim.read_bytes(), b"victim\n", "create symlink victim")

        artifact.unlink()
        os.link(victim, artifact)
        try:
            run_store.create_private_file(artifact, b"must-not-write\n")
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "hardlink create class")
        else:
            raise AssertionError("hardlink existing private file must fail closed")
        assert_equal(victim.read_bytes(), b"victim\n", "create hardlink victim")


def test_rotate_private_file_is_same_directory_nofollow_and_durable() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        source = state_root / "audit" / "events.jsonl"
        target = state_root / "audit" / "events.1.jsonl"
        write_private_fixture(source, "event\n")
        run_store.rotate_private_file(source, target)
        assert not source.exists()
        assert_equal(target.read_text(encoding="utf-8"), "event\n", "rotated content")

        write_private_fixture(source, "new event\n")
        try:
            run_store.rotate_private_file(source, target)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "rotation conflict class")
        else:
            raise AssertionError("existing rotation target must fail closed")
        assert_equal(source.read_text(encoding="utf-8"), "new event\n", "conflict source")
        assert_equal(target.read_text(encoding="utf-8"), "event\n", "conflict target")

        run_store.rotate_private_file(source, target, target_must_not_exist=False)
        assert not source.exists()
        assert_equal(target.read_text(encoding="utf-8"), "new event\n", "replaced rotation")

        write_private_fixture(source, "third event\n")
        target.unlink()
        victim = state_root / "victim.jsonl"
        victim.write_text("victim\n", encoding="utf-8")
        target.symlink_to(victim)
        try:
            run_store.rotate_private_file(source, target, target_must_not_exist=False)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "symlink rotation target class")
        else:
            raise AssertionError("symlink rotation target must fail closed")
        assert_equal(victim.read_text(encoding="utf-8"), "victim\n", "rotation target victim")

        other = state_root / "other" / "events.2.jsonl"
        try:
            run_store.rotate_private_file(source, other)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "cross-directory rotation class")
        else:
            raise AssertionError("cross-directory rotation must be rejected")
        assert not other.parent.exists(), "rejected rotation must not create another directory"

        source.unlink()
        source.symlink_to(victim)
        safe_target = source.with_name("events.3.jsonl")
        try:
            run_store.rotate_private_file(source, safe_target)
        except run_store.RunStoreError as exc:
            assert_equal(exc.reason_class, "io_error", "symlink rotation source class")
        else:
            raise AssertionError("symlink rotation source must fail closed")
        assert_equal(victim.read_text(encoding="utf-8"), "victim\n", "rotation source victim")


def test_existing_private_helpers_reject_hardlinked_artifacts() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        victim = state_root / "outside.json"
        victim.write_text('{"outside": true}\n', encoding="utf-8")
        victim.chmod(0o600)
        artifact = state_root / "records" / "record.json"
        run_store.ensure_private_directory(artifact.parent)
        os.link(victim, artifact)

        for operation in (
            lambda: run_store.read_json(artifact),
            lambda: run_store.append_json_line(artifact, {"appended": True}),
            lambda: run_store.read_and_unlink_private_file(artifact),
        ):
            try:
                operation()
            except run_store.RunStoreError as exc:
                assert_equal(exc.reason_class, "io_error", "hardlink helper error class")
            else:
                raise AssertionError("existing private helper must reject hardlink artifacts")
        assert_equal(
            victim.read_text(encoding="utf-8"),
            '{"outside": true}\n',
            "hardlink helper victim",
        )
        assert artifact.exists(), "failed hardlink operations must preserve the artifact"


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
        test_private_artifact_stat_and_exists_are_nofollow_and_noncreating,
        test_list_private_artifacts_validates_matching_entries,
        test_unlink_private_file_is_descriptor_relative_and_fail_closed,
        test_create_private_file_is_exclusive_nofollow_and_validates_existing,
        test_rotate_private_file_is_same_directory_nofollow_and_durable,
        test_existing_private_helpers_reject_hardlinked_artifacts,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
