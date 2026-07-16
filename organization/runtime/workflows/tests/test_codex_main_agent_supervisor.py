#!/usr/bin/env python3
"""Tests for the root-authenticated Codex launch-session supervisor."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = WORKFLOW_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402
import codex_main_agent_supervisor as supervisor  # noqa: E402


def fixture_manifest(root: Path) -> dict:
    return {
        "deployment_id": "codex-main-agent-a-prime",
        "bindings": {
            "principal_id": "codex-main-agent-a-prime",
            "workspace_id": "Saber5656/Saihai",
        },
        "artifacts": {
            "native_codex": {
                "path": str(root / "runtime" / "bin" / "codex"),
                "sha256": "sha256:" + "1" * 64,
            },
            "codex_profile": {
                "path": str(root / "home" / "saihai-main-agent.config.toml"),
                "sha256": "sha256:" + "2" * 64,
            },
        },
    }


def fixture_checkout(root: Path) -> dict[str, str]:
    return {
        "checkout_realpath": str(root / "checkout"),
        "identity_digest": "sha256:" + "3" * 64,
    }


def record(root: Path) -> dict:
    return supervisor.build_session_record(
        manifest=fixture_manifest(root),
        checkout_identity=fixture_checkout(root),
        session_id="launch-fixture-001",
        subject_pid=1234,
        process_start_token="proc-" + "4" * 64,
        supervisor_pid=1200,
        supervisor_start_token="proc-" + "5" * 64,
        issued_at=datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc),
    )


def test_record_is_exact_and_self_digest_bound() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-supervisor-") as raw:
        root = Path(raw).resolve()
        value = record(root)
        assert set(value) == supervisor.SESSION_RECORD_FIELDS
        assert value["session_id"] == "launch-fixture-001"
        assert value["status"] == "active"
        assert value["session_kind"] == "standard"
        assert value["commissioning_launch_reference"] is None
        assert value["commissioning_launch_digest"] is None
        assert value["record_reference"] == "launch-sessions/launch-fixture-001.json"
        assert value["record_digest"] == supervisor._stable_digest(
            supervisor._session_material(value)
        )
        assert supervisor.validate_session_record_shape(value) == value


def test_record_tamper_and_path_injection_fail() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-supervisor-") as raw:
        root = Path(raw).resolve()
        value = record(root)
        value["subject_pid"] = 9999
        try:
            supervisor.validate_session_record_shape(value)
        except supervisor.SupervisorError as exc:
            assert exc.reason == "launch_session_digest_invalid"
        else:
            raise AssertionError("tampered session record must fail")

        value = record(root)
        value["record_reference"] = "launch-sessions/../escape.json"
        value["record_digest"] = supervisor._stable_digest(
            supervisor._session_material(value)
        )
        try:
            supervisor.validate_session_record_shape(value)
        except supervisor.SupervisorError as exc:
            assert exc.reason == "launch_session_reference_invalid"
        else:
            raise AssertionError("unsafe reference must fail")


def test_atomic_session_write_is_no_replace_and_mode_bound() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-supervisor-") as raw:
        root = Path(raw).resolve()
        root.chmod(0o700)
        assurance_root = root / "Assurance"
        session_dir = assurance_root / "launch-sessions"
        session_dir.mkdir(parents=True)
        assurance_root.chmod(0o700)
        session_dir.chmod(0o700)
        value = record(root)
        path = supervisor._atomic_write_session(
            assurance_root,
            value,
            policy=assurance.TrustPolicy.fixture(root),
        )
        assert json.loads(path.read_text(encoding="utf-8")) == value
        assert stat.S_IMODE(path.lstat().st_mode) == 0o644
        try:
            supervisor._atomic_write_session(
                assurance_root,
                value,
                policy=assurance.TrustPolicy.fixture(root),
            )
        except supervisor.SupervisorError as exc:
            assert exc.reason == "launch_session_exists"
        else:
            raise AssertionError("session writer must not replace an existing record")


def test_commissioning_session_and_companion_are_mutually_bound() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-supervisor-") as raw:
        root = Path(raw).resolve()
        manifest = fixture_manifest(root)
        base = supervisor.deployment.native_codex_argv(
            manifest["artifacts"]["native_codex"]["path"]
        )
        argv = [*base, "exec", "--ephemeral", "--json", "fixed prompt"]
        issued_at = datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc)
        draft = supervisor.build_commissioning_launch(
            session_id="launch-commissioning-001",
            commissioning_id="commissioning-001",
            generation_id="generation-001",
            profile_id="codex-main-agent-a-prime",
            probe_id="frontend_gateway_routing",
            nonce_digest="sha256:" + "6" * 64,
            probe_argv=argv,
            launch_session_digest="sha256:" + "0" * 64,
            issued_at=issued_at,
        )
        session = supervisor.build_session_record(
            manifest=manifest,
            checkout_identity=fixture_checkout(root),
            session_id="launch-commissioning-001",
            subject_pid=1234,
            process_start_token="proc-" + "4" * 64,
            supervisor_pid=1200,
            supervisor_start_token="proc-" + "5" * 64,
            issued_at=issued_at,
            launch_argv=argv,
            session_kind="commissioning",
            commissioning_launch_reference=draft["record_reference"],
            commissioning_launch_digest=draft["binding_digest"],
            lifetime_seconds=supervisor.COMMISSIONING_LIFETIME_SECONDS,
        )
        companion = supervisor.build_commissioning_launch(
            session_id="launch-commissioning-001",
            commissioning_id="commissioning-001",
            generation_id="generation-001",
            profile_id="codex-main-agent-a-prime",
            probe_id="frontend_gateway_routing",
            nonce_digest="sha256:" + "6" * 64,
            probe_argv=argv,
            launch_session_digest=session["record_digest"],
            issued_at=issued_at,
        )
        assert session["session_kind"] == "commissioning"
        assert session["commissioning_launch_digest"] == companion["binding_digest"]
        assert companion["launch_session_digest"] == session["record_digest"]
        assert supervisor.validate_session_record_shape(session) == session
        assert supervisor.validate_commissioning_launch_shape(companion) == companion


def test_companion_create_transition_is_single_use() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-supervisor-") as raw:
        root = Path(raw).resolve()
        root.chmod(0o700)
        assurance_root = root / "Assurance"
        launch_dir = assurance_root / "commissioning-launches"
        launch_dir.mkdir(parents=True)
        assurance_root.chmod(0o700)
        launch_dir.chmod(0o700)
        companion = supervisor.build_commissioning_launch(
            session_id="launch-commissioning-002",
            commissioning_id="commissioning-002",
            generation_id="generation-002",
            profile_id="codex-main-agent-a-prime",
            probe_id="frontend_filesystem_denial",
            nonce_digest="sha256:" + "7" * 64,
            probe_argv=["/absolute/codex", "exec", "fixed prompt"],
            launch_session_digest="sha256:" + "8" * 64,
            issued_at=datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        policy = assurance.TrustPolicy.fixture(root)
        path = supervisor._atomic_write_commissioning_launch(
            assurance_root, companion, policy=policy
        )
        assert stat.S_IMODE(path.lstat().st_mode) == 0o644
        consumed = supervisor.finalize_commissioning_launch(
            assurance_root,
            companion_reference=companion["record_reference"],
            expected_binding_digest=companion["binding_digest"],
            final_state="consumed",
            policy=policy,
        )
        assert consumed["state"] == "consumed"
        assert consumed["binding_digest"] == companion["binding_digest"]
        try:
            supervisor.finalize_commissioning_launch(
                assurance_root,
                companion_reference=companion["record_reference"],
                expected_binding_digest=companion["binding_digest"],
                final_state="failed",
                policy=policy,
            )
        except supervisor.SupervisorError as exc:
            assert exc.reason == "commissioning_launch_transition_binding_mismatch"
        else:
            raise AssertionError("consumed commissioning launch must not be reusable")


def test_standard_session_rejects_probe_argv() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-supervisor-") as raw:
        root = Path(raw).resolve()
        base = supervisor.deployment.native_codex_argv(
            fixture_manifest(root)["artifacts"]["native_codex"]["path"]
        )
        try:
            supervisor.build_session_record(
                manifest=fixture_manifest(root),
                checkout_identity=fixture_checkout(root),
                session_id="launch-invalid-standard",
                subject_pid=1234,
                process_start_token="proc-" + "4" * 64,
                supervisor_pid=1200,
                supervisor_start_token="proc-" + "5" * 64,
                issued_at=datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc),
                launch_argv=[*base, "exec", "untrusted"],
            )
        except supervisor.SupervisorError as exc:
            assert exc.reason == "standard_launch_session_argv_invalid"
        else:
            raise AssertionError("standard session must bind the exact normal argv")


def test_non_root_call_cannot_supervise() -> None:
    if os.geteuid() == 0:
        return
    try:
        supervisor._required_sudo_runtime_user(
            {
                "bindings": {
                    "runtime_user_uid": os.getuid(),
                    "runtime_user_name": "fixture",
                    "runtime_user_home": str(Path.home()),
                }
            }
        )
    except supervisor.SupervisorError as exc:
        assert exc.reason == "root_supervisor_required"
    else:
        raise AssertionError("non-root supervisor call must fail")


def main() -> None:
    tests = [
        test_record_is_exact_and_self_digest_bound,
        test_record_tamper_and_path_injection_fail,
        test_atomic_session_write_is_no_replace_and_mode_bound,
        test_commissioning_session_and_companion_are_mutually_bound,
        test_companion_create_transition_is_single_use,
        test_standard_session_rejects_probe_argv,
        test_non_root_call_cannot_supervise,
    ]
    for test in tests:
        test()
    print(f"codex main-agent supervisor tests passed: {len(tests)}")


if __name__ == "__main__":
    main()
