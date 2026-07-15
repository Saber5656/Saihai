#!/usr/bin/env python3
"""Focused tests for root-owned assurance commissioning lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = WORKFLOW_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402
import agent_integration_canary as canary  # noqa: E402
import agent_integration_observer as observer  # noqa: E402
import run_lock  # noqa: E402
import scoped_worker_executor as scoped_worker  # noqa: E402
import test_agent_integration_attester as attester_tests  # noqa: E402


def _write_json(path: Path, payload: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


@contextmanager
def worker_commissioning_fixture() -> Iterator[dict]:
    with tempfile.TemporaryDirectory(prefix="saihai-observer-") as raw:
        temporary = Path(raw).resolve()
        temporary.chmod(0o700)
        root = temporary / "Assurance"
        root.mkdir(mode=0o700)
        policy = assurance.TrustPolicy.fixture(temporary)
        runtime = temporary / "codex"
        runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime.chmod(0o700)
        worker_home = temporary / "worker-home"
        worker_home.mkdir(mode=0o700)
        environment = scoped_worker.worker_environment(str(worker_home))
        argv_template = scoped_worker.worker_argv_template(str(runtime))
        execution_binding = {
            "binding_version": "1",
            "runtime_realpath": str(runtime),
            "runtime_digest": scoped_worker.file_sha256(runtime),
            "profile_mode": "ignored_by_fixed_argv",
            "profile_realpath": None,
            "profile_digest": scoped_worker.ignored_profile_digest(
                environment_digest=scoped_worker.sha256_digest(environment)
            ),
            "codex_home_realpath": str(worker_home),
            "environment": environment,
            "environment_digest": scoped_worker.sha256_digest(environment),
            "argv_template": argv_template,
            "argv_digest": scoped_worker.sha256_digest(argv_template),
            "runner_profile_digest": scoped_worker.sha256_digest(
                scoped_worker.RUNNER_PROFILE
            ),
        }
        scoped_worker.verify_worker_runtime_binding(execution_binding)
        registry = assurance.load_registry()
        now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        begun = observer.begin_commissioning(
            root,
            profile_id="codex-scoped-worker",
            runtime_binding=execution_binding,
            generation_id="generation-worker-fixture",
            commissioning_id="commission-worker-fixture",
            registry=registry,
            trust_policy=policy,
            now=now,
        )
        authority = observer.commissioning_authority(
            state_root=root, trust_policy=policy, now=now
        )
        claim = authority.claim(
            begun["commissioning_id"],
            expected_profile_id="codex-scoped-worker",
            expected_purpose="managed_worker_surface_launch_probe",
            expected_operation="surface_launch",
        )
        marker = authority.read_marker(claim)
        command = scoped_worker.commissioning_probe_argv(execution_binding)
        pid = os.getpid()
        token = run_lock.process_start_token(pid)
        assert token
        event = {
            "event_version": "1",
            "event_id": "probe-worker-fixture",
            "profile_id": "codex-scoped-worker",
            "generation_id": begun["generation_id"],
            "purpose": "managed_worker_surface_launch_probe",
            "operation": "surface_launch",
            "runtime_binding_digest": assurance._stable_digest(execution_binding),
            "runtime_realpath": execution_binding["runtime_realpath"],
            "runtime_digest": execution_binding["runtime_digest"],
            "subject_pid": pid,
            "process_start_token": token,
            "argv": command,
            "argv_digest": assurance._stable_digest(command),
            "marker_before_digest": assurance._sha256_bytes(marker),
            "marker_after_digest": assurance._sha256_bytes(marker),
            "exit_code": 0,
            "probe_contract_result": "pass",
            "probe_result_digest": assurance._stable_digest({"result": "pass"}),
            "probe_marker_digest": assurance._sha256_bytes(
                scoped_worker.COMMISSIONING_PROBE_MARKER_BYTES
            ),
            "probe_worktree_status_digest": assurance._stable_digest(
                [
                    f"?? {scoped_worker.COMMISSIONING_PROBE_MARKER_NAME}",
                    f"?? {scoped_worker.COMMISSIONING_PROBE_OUTPUT_NAME}",
                ]
            ),
            "probe_facts": {
                "facts_version": "1",
                "observed": dict(observer.WORKER_PROBE_OBSERVED_FACTS),
                "mechanical_policy": dict(observer.WORKER_PROBE_POLICY_FACTS),
                "not_model_prose": True,
            },
            "observed_at": assurance.format_timestamp(now),
        }
        authority.finalize(claim, event)
        observer_binary = temporary / "root-observer"
        observer_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        observer_binary.chmod(0o700)
        yield {
            "root": root,
            "policy": policy,
            "registry": registry,
            "now": now,
            "begun": begun,
            "event": event,
            "observer_binary": observer_binary,
        }


def _expect(reason: str, action) -> None:
    try:
        action()
    except observer.ObserverError as exc:
        if reason not in exc.reason:
            raise AssertionError(f"expected {reason!r}, got {exc.reason!r}") from exc
        return
    raise AssertionError(f"expected ObserverError containing {reason!r}")


def test_worker_weak_denial_facts_cannot_seal_or_activate() -> None:
    with worker_commissioning_fixture() as fixture:
        reference = fixture["begun"]["commissioning_reference"]
        observed = observer.observe_worker_commissioning_evidence(
            fixture["root"],
            commissioning_reference=reference,
            registry=fixture["registry"],
            trust_policy=fixture["policy"],
            now=fixture["now"],
            observer_binary=fixture["observer_binary"],
        )
        assert len(observed["evidence_references"]) == 17
        assert len(set(observed["evidence_references"])) == 17
        blocked = {
            payload["operation"]: payload
            for relative in observed["evidence_references"]
            if (
                (payload := json.loads((fixture["root"] / relative).read_text()))[
                    "operation"
                ]
                in assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS
            )
        }
        assert set(blocked) == set(assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS)
        assert all(item["result"] == "fail" for item in blocked.values())
        for payload in blocked.values():
            observation_reference = payload["details"]["host_observation"]["path"]
            observation = json.loads(
                (fixture["root"] / observation_reference).read_text()
            )
            assert observation["outcome"] == "inconclusive"
            assert observation["attempted"] is False
        _expect(
            "commissioning_generation_seal_failed",
            lambda: observer.seal_commissioning_generation(
                fixture["root"],
                commissioning_reference=reference,
                registry=fixture["registry"],
                trust_policy=fixture["policy"],
                now=fixture["now"],
            ),
        )
        assert not (fixture["root"] / "active/codex-scoped-worker.json").exists()


def test_worker_suite_missing_extra_and_replay_fail_closed() -> None:
    with worker_commissioning_fixture() as fixture:
        reference = fixture["begun"]["commissioning_reference"]
        observed = observer.observe_worker_commissioning_evidence(
            fixture["root"],
            commissioning_reference=reference,
            registry=fixture["registry"],
            trust_policy=fixture["policy"],
            now=fixture["now"],
            observer_binary=fixture["observer_binary"],
        )
        missing = fixture["root"] / observed["evidence_references"][0]
        missing.unlink()
        _expect(
            "commissioning_evidence_coverage_mismatch",
            lambda: observer.seal_commissioning_generation(
                fixture["root"],
                commissioning_reference=reference,
                registry=fixture["registry"],
                trust_policy=fixture["policy"],
                now=fixture["now"],
            ),
        )

    with worker_commissioning_fixture() as fixture:
        reference = fixture["begun"]["commissioning_reference"]
        observed = observer.observe_worker_commissioning_evidence(
            fixture["root"],
            commissioning_reference=reference,
            registry=fixture["registry"],
            trust_policy=fixture["policy"],
            now=fixture["now"],
            observer_binary=fixture["observer_binary"],
        )
        source = fixture["root"] / observed["evidence_references"][0]
        replay = source.parent / "replayed-evidence.json"
        replay.write_bytes(source.read_bytes())
        replay.chmod(0o644)
        _expect(
            "commissioning_evidence_duplicate",
            lambda: observer.seal_commissioning_generation(
                fixture["root"],
                commissioning_reference=reference,
                registry=fixture["registry"],
                trust_policy=fixture["policy"],
                now=fixture["now"],
            ),
        )


def test_worker_probe_event_tamper_and_grant_reuse_fail_closed() -> None:
    with worker_commissioning_fixture() as fixture:
        authority = observer.commissioning_authority(
            state_root=fixture["root"],
            trust_policy=fixture["policy"],
            now=fixture["now"],
        )
        _expect(
            "commissioning_already_used",
            lambda: authority.claim(
                fixture["begun"]["commissioning_id"],
                expected_profile_id="codex-scoped-worker",
                expected_purpose="managed_worker_surface_launch_probe",
                expected_operation="surface_launch",
            ),
        )
        event_path = (
            fixture["root"]
            / "commissioning"
            / "codex-scoped-worker"
            / "commission-worker-fixture-worker-event.json"
        )
        envelope = json.loads(event_path.read_text(encoding="utf-8"))
        envelope["event"]["probe_facts"]["not_model_prose"] = False
        _write_json(event_path, envelope, mode=0o600)
        _expect(
            "worker_probe_event_binding_mismatch",
            lambda: observer.observe_worker_commissioning_evidence(
                fixture["root"],
                commissioning_reference=fixture["begun"][
                    "commissioning_reference"
                ],
                registry=fixture["registry"],
                trust_policy=fixture["policy"],
                now=fixture["now"],
                observer_binary=fixture["observer_binary"],
            ),
        )

    with worker_commissioning_fixture() as fixture:
        record_path = fixture["root"] / fixture["begun"]["commissioning_reference"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.update(
            {
                "state": "pending",
                "claimed_at": None,
                "completed_at": None,
                "consumer_event_digest": None,
                "generation_manifest_digest": None,
                "record_digest": "sha256:" + "0" * 64,
            }
        )
        record["record_digest"] = assurance._stable_digest(observer._material(record))
        _write_json(record_path, record, mode=0o600)
        expired_authority = observer.commissioning_authority(
            state_root=fixture["root"],
            trust_policy=fixture["policy"],
            now=fixture["now"] + timedelta(minutes=16),
        )
        _expect(
            "commissioning_grant_expired",
            lambda: expired_authority.claim(
                fixture["begun"]["commissioning_id"],
                expected_profile_id="codex-scoped-worker",
                expected_purpose="managed_worker_surface_launch_probe",
                expected_operation="surface_launch",
            ),
        )


def test_partial_worker_evidence_failure_never_activates() -> None:
    with worker_commissioning_fixture() as fixture:
        original = observer._write_worker_operation_evidence
        calls = 0

        def fail_after_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise observer.ObserverError("simulated_partial_failure")
            return original(*args, **kwargs)

        observer._write_worker_operation_evidence = fail_after_first  # type: ignore[assignment]
        try:
            _expect(
                "simulated_partial_failure",
                lambda: observer.observe_worker_commissioning_evidence(
                    fixture["root"],
                    commissioning_reference=fixture["begun"][
                        "commissioning_reference"
                    ],
                    registry=fixture["registry"],
                    trust_policy=fixture["policy"],
                    now=fixture["now"],
                    observer_binary=fixture["observer_binary"],
                ),
            )
        finally:
            observer._write_worker_operation_evidence = original  # type: ignore[assignment]
        assert not (fixture["root"] / "active/codex-scoped-worker.json").exists()
        _expect(
            "commissioning_evidence_coverage_mismatch",
            lambda: observer.seal_commissioning_generation(
                fixture["root"],
                commissioning_reference=fixture["begun"][
                    "commissioning_reference"
                ],
                registry=fixture["registry"],
                trust_policy=fixture["policy"],
                now=fixture["now"],
            ),
        )


def test_frontend_exact_suite_rejects_missing_extra_and_replay() -> None:
    with attester_tests.host_fixture() as fixture:
        commissioning_reference = (
            f"commissioning/{fixture.profile['profile_id']}/"
            f"{fixture.commissioning_id}.json"
        )
        record = observer._load_commissioning(
            fixture.assurance_root,
            commissioning_reference,
            policy=fixture.policy,
        )
        launch = json.loads(
            (
                fixture.assurance_root
                / "launch-sessions"
                / "launch-fixture.json"
            ).read_text(encoding="utf-8")
        )
        original = observer._commissioning_checkout
        observer._commissioning_checkout = (  # type: ignore[assignment]
            lambda *_args, **_kwargs: (fixture.repo, launch)
        )
        try:
            suite = observer._collect_exact_generation_evidence(
                fixture.assurance_root,
                record,
                fixture.profile,
                policy=fixture.policy,
                now=fixture.now,
            )
            assert len(suite["references"]) == len(
                assurance._required_evidence_keys(fixture.profile)
            )
            first = fixture.assurance_root / suite["references"][0]["reference"]
            first.unlink()
            _expect(
                "commissioning_evidence_coverage_mismatch",
                lambda: observer._collect_exact_generation_evidence(
                    fixture.assurance_root,
                    record,
                    fixture.profile,
                    policy=fixture.policy,
                    now=fixture.now,
                ),
            )
        finally:
            observer._commissioning_checkout = original  # type: ignore[assignment]

    with attester_tests.host_fixture() as fixture:
        commissioning_reference = (
            f"commissioning/{fixture.profile['profile_id']}/"
            f"{fixture.commissioning_id}.json"
        )
        record = observer._load_commissioning(
            fixture.assurance_root,
            commissioning_reference,
            policy=fixture.policy,
        )
        launch = json.loads(
            (fixture.assurance_root / "launch-sessions/launch-fixture.json").read_text(
                encoding="utf-8"
            )
        )
        source = next(fixture.evidence_dir.glob("*.json"))
        replay = fixture.evidence_dir / "replayed-evidence.json"
        replay.write_bytes(source.read_bytes())
        replay.chmod(0o644)
        original = observer._commissioning_checkout
        observer._commissioning_checkout = (  # type: ignore[assignment]
            lambda *_args, **_kwargs: (fixture.repo, launch)
        )
        try:
            _expect(
                "commissioning_evidence_duplicate",
                lambda: observer._collect_exact_generation_evidence(
                    fixture.assurance_root,
                    record,
                    fixture.profile,
                    policy=fixture.policy,
                    now=fixture.now,
                ),
            )
        finally:
            observer._commissioning_checkout = original  # type: ignore[assignment]


def test_fixed_frontend_parsers_reject_prose_extra_actions_and_wrong_targets() -> None:
    checkout = Path("/tmp/saihai-parser-fixture")
    file_event = {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "status": "failed",
            "changes": [
                {
                    "path": ".saihai-frontend-commissioning-probe",
                    "kind": "add",
                }
            ],
        },
    }
    raw = (json.dumps(file_event) + "\n").encode()
    _event, digest = observer._parse_failed_file_change(raw, checkout=checkout)
    assert assurance._valid_digest(digest)
    for invalid in (
        b"I could not edit the file\n",
        (json.dumps({**file_event, "item": {**file_event["item"], "status": "completed"}}) + "\n").encode(),
        (
            json.dumps(
                {
                    **file_event,
                    "item": {
                        **file_event["item"],
                        "changes": [{"path": "wrong", "kind": "add"}],
                    },
                }
            )
            + "\n"
        ).encode(),
        raw
        + (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "status": "failed"},
                }
            )
            + "\n"
        ).encode(),
    ):
        _expect(
            "frontend_",
            lambda invalid=invalid: observer._parse_failed_file_change(
                invalid, checkout=checkout
            ),
        )

    arguments = {
        "workspace": "Saber5656/Saihai",
        "task_id": "task-fixture",
        "request_id": "request-fixture",
        "prompt": canary.ROUTING_ACCEPTANCE_PROMPT,
        "refs": ["README.md", "CHANGELOG.md"],
        "allowed_paths": [],
        "chat_session_id": "chat-fixture",
        "idempotency_key": "idempotency-fixture",
    }
    submit = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "saihai-main-agent",
            "tool": "submit_request",
            "status": "completed",
            "error": None,
            "arguments": arguments,
        },
    }
    parsed = observer._parse_completed_submit_request(
        (json.dumps(submit) + "\n").encode(),
        expected_server="saihai-main-agent",
    )
    assert parsed[2:] == ("request-fixture", "idempotency-fixture")
    wrong = json.loads(json.dumps(submit))
    wrong["item"]["tool"] = "read_projection"
    _expect(
        "frontend_gateway_probe_inconclusive",
        lambda: observer._parse_completed_submit_request(
            (json.dumps(wrong) + "\n").encode(),
            expected_server="saihai-main-agent",
        ),
    )


def test_cli_exposes_generation_and_fixed_commissioning_lifecycle() -> None:
    observer_help = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "agent_integration_observer.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for command in (
        "commission-begin",
        "commission-run-frontend",
        "commission-observe-worker",
        "commission-seal",
    ):
        assert command in observer_help
    for command in ("common-record", "action-begin"):
        help_text = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "agent_integration_canary.py"),
                command,
                "--help",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "--generation" in help_text


def test_frontend_credential_fact_is_limited_to_known_codex_auth_paths() -> None:
    user_home = Path("/Users/frontend")
    dedicated_home = Path("/Users/frontend/.codex-saihai-main-agent")
    requirements = (
        'deny_read = ["/Users/frontend/.codex/auth.json", '
        '"/Users/frontend/.codex-saihai-main-agent/auth.json"]\n'
    ).encode("utf-8")
    assert observer._known_codex_auth_paths_denied(
        requirements,
        runtime_user_home=user_home,
        codex_home=dedicated_home,
    )
    assert not observer._known_codex_auth_paths_denied(
        requirements.replace(b"/Users/frontend/.codex/auth.json", b"missing"),
        runtime_user_home=user_home,
        codex_home=dedicated_home,
    )
    assert not observer._known_codex_auth_paths_denied(
        requirements.replace(
            b"/Users/frontend/.codex-saihai-main-agent/auth.json", b"missing"
        ),
        runtime_user_home=user_home,
        codex_home=dedicated_home,
    )
    assert not observer._known_codex_auth_paths_denied(
        requirements,
        runtime_user_home=user_home,
        codex_home=user_home / ".codex",
    )


def test_public_gateway_receipt_linkage_retains_only_idempotency_digest() -> None:
    raw_key = "raw-commissioning-key-must-stay-private"
    linkage = observer._gateway_state_linkage(
        state_root=Path("/private/state"),
        frontend_profile_id="codex-main-agent-a-prime",
        routing_begin={
            "challenge_path": "/private/challenge.json",
            "challenge_sha256": "sha256:" + "1" * 64,
        },
        request_id="request-fixture",
        idempotency_key=raw_key,
    )
    assert set(linkage) == {
        "state_root",
        "frontend_profile_id",
        "routing_challenge_path",
        "routing_challenge_sha256",
        "request_id",
        "idempotency_key_digest",
    }
    assert linkage["idempotency_key_digest"].startswith("sha256:")
    assert raw_key not in json.dumps(linkage, sort_keys=True)


def main() -> None:
    tests = [
        test_worker_weak_denial_facts_cannot_seal_or_activate,
        test_worker_suite_missing_extra_and_replay_fail_closed,
        test_worker_probe_event_tamper_and_grant_reuse_fail_closed,
        test_partial_worker_evidence_failure_never_activates,
        test_frontend_exact_suite_rejects_missing_extra_and_replay,
        test_fixed_frontend_parsers_reject_prose_extra_actions_and_wrong_targets,
        test_cli_exposes_generation_and_fixed_commissioning_lifecycle,
        test_frontend_credential_fact_is_limited_to_known_codex_auth_paths,
        test_public_gateway_receipt_linkage_retains_only_idempotency_digest,
    ]
    for test in tests:
        test()
    print(f"agent integration observer tests passed: {len(tests)}")


if __name__ == "__main__":
    main()
