#!/usr/bin/env python3
"""Focused tests for independent action and routing canary production."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = WORKFLOW_ROOT / "scripts"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(1, str(TEST_ROOT))

import agent_integration_assurance as assurance  # noqa: E402
import agent_integration_canary as canary  # noqa: E402
import codex_main_agent_deployment as deployment  # noqa: E402
import codex_main_agent_supervisor as supervisor  # noqa: E402
import frontdoor_orchestrator as frontdoor  # noqa: E402
import run_lock  # noqa: E402
import scoped_worker_executor as scoped_worker  # noqa: E402
import test_scoped_worker_executor as worker_fixture  # noqa: E402


@dataclass
class Fixture:
    temp_root: Path
    assurance_root: Path
    policy: assurance.TrustPolicy
    registry: dict
    profile: dict
    generation_id: str
    repo: Path
    marker: Path
    observer_binary: Path
    runtime_binary: Path
    profile_snapshot: Path
    manifest_path: Path
    bindings: dict[str, str]
    common_references: list[str]
    now: datetime
    launch_session: dict


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _profile(registry: dict, profile_id: str) -> dict:
    return next(item for item in registry["profiles"] if item["profile_id"] == profile_id)


def _frontend_tool_inventory(
    *, active_external_mutation_tools: list[str] | None = None
) -> tuple[list[str], dict]:
    classification = {
        "inventory_version": "1",
        "model_identifier": "gpt-5-codex-fixture",
        "server_backed_mcp_tools": list(canary.FRONTEND_SERVER_TOOLS),
        "reviewed_internal_tools": [
            "list_mcp_resources",
            "update_plan",
            "view_image",
        ],
        "active_external_mutation_tools": active_external_mutation_tools or [],
        "mechanically_denied_tools": ["apply_patch"],
        "dynamic_tools": [],
        "extension_tools": [],
        "multi_agent_tools": [],
    }
    inventory = sorted(
        item
        for name, values in classification.items()
        if name not in {"inventory_version", "model_identifier"}
        for item in values
    )
    return inventory, classification


def _tool_inventory_observation(
    *,
    manifest_path: Path,
    manifest: dict,
    launch_session: dict,
    inventory: list[str],
    classification: dict,
    runtime_argv_digest: str,
) -> dict:
    facts = {name: True for name in assurance.TOOL_POLICY_FACT_FIELDS}
    return {
        "observation_version": "1",
        "observation_kind": "live_session_machine_policy",
        "session_id": launch_session["session_id"],
        "launch_session_digest": launch_session["record_digest"],
        "deployment_manifest_digest": assurance._sha256_bytes(
            manifest_path.read_bytes()
        ),
        "requirements_digest": manifest["artifacts"]["requirements"]["sha256"],
        "profile_digest": manifest["artifacts"]["codex_profile"]["sha256"],
        "fixed_argv_digest": runtime_argv_digest,
        "policy_facts": facts,
        "policy_facts_digest": assurance._stable_digest(facts),
        "inventory_digest": assurance._stable_digest(inventory),
        "classification_digest": assurance._stable_digest(classification),
    }


def _expect(reason: str, callback) -> None:
    try:
        callback()
    except (canary.CanaryError, assurance.AssuranceGateError) as exc:
        actual = str(getattr(exc, "reason", exc))
        assert reason in actual, (reason, actual)
    else:
        raise AssertionError(f"expected {reason}")


@contextmanager
def fixture() -> Iterator[Fixture]:
    with tempfile.TemporaryDirectory(prefix="saihai-canary-") as raw:
        temp_root = Path(raw).resolve()
        temp_root.chmod(0o700)
        assurance_root = temp_root / "Assurance"
        assurance_root.mkdir(mode=0o700)
        generation_id = "generation-fixture"
        generation_root = (
            assurance_root
            / "generations"
            / "codex-main-agent-a-prime"
            / generation_id
        )
        evidence_dir = generation_root / "evidence"
        evidence_dir.mkdir(parents=True, mode=0o700)
        observer_dir = generation_root / "observer"
        observer_dir.mkdir(mode=0o700)
        (assurance_root / "launch-sessions").mkdir(mode=0o700)
        for directory in (
            assurance_root / "generations",
            assurance_root / "generations" / "codex-main-agent-a-prime",
            generation_root,
            evidence_dir,
            observer_dir,
            assurance_root / "launch-sessions",
        ):
            directory.chmod(0o700)
        policy = assurance.TrustPolicy.fixture(assurance_root)

        repo = temp_root / "repo"
        repo.mkdir(mode=0o700)
        _run_git(repo, "init", "-b", "main")
        _run_git(repo, "config", "user.name", "Saihai Test")
        _run_git(repo, "config", "user.email", "saihai-test@example.invalid")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        _run_git(repo, "add", "README.md", "CHANGELOG.md")
        _run_git(repo, "commit", "-m", "fixture")
        checkout = frontdoor.resolve_checkout_identity(
            workspace_id="Saber5656/Saihai",
            managed_primary=repo,
            checkout_root=repo,
        )

        installed = assurance_root / "installed"
        installed.mkdir(mode=0o700)
        runtime_config = installed / "codex-main-agent.runtime.json"
        requirements = installed / "requirements.toml"
        wrapper = installed / "saihai-codex-main-agent-bridge"
        instructions = installed / "codex-main-agent.instructions.md"
        profile_snapshot = installed / "profile.toml"
        runtime_binary = installed / "codex"
        launcher_binary = installed / "saihai-codex-main-agent"
        observer_binary = installed / "host-observer"
        supervisor_binary = installed / "host-supervisor.py"
        for path, content, mode in (
            (runtime_config, '{"runtime_config_version":"1"}\n', 0o600),
            (requirements, 'approval_policy = "never"\n', 0o600),
            (wrapper, "#!/bin/sh\nexit 0\n", 0o700),
            (instructions, "Route actions through Saihai.\n", 0o600),
            (profile_snapshot, 'sandbox_mode = "read-only"\n', 0o600),
            (runtime_binary, "#!/bin/sh\nexit 0\n", 0o700),
            (launcher_binary, "#!/bin/sh\nexec codex\n", 0o700),
            (observer_binary, "#!/bin/sh\nexit 0\n", 0o700),
            (supervisor_binary, "fixture supervisor\n", 0o600),
        ):
            path.write_text(content, encoding="utf-8")
            path.chmod(mode)
        role_paths = {
            "runtime_config": runtime_config,
            "requirements": requirements,
            "bridge_wrapper": wrapper,
            "instructions": instructions,
            "profile_snapshot": profile_snapshot,
            "observer": observer_binary,
            "supervisor": supervisor_binary,
        }
        manifest_path = installed / "codex-main-agent.deployment.json"
        manifest = {
            "artifacts": {
                **{
                    role: {
                        "path": str(path),
                        "sha256": assurance._sha256_bytes(path.read_bytes()),
                    }
                    for role, path in role_paths.items()
                    if role != "profile_snapshot"
                },
                "codex_launcher": {
                    "path": str(launcher_binary),
                    "sha256": assurance._sha256_bytes(
                        launcher_binary.read_bytes()
                    ),
                },
                "native_codex": {
                    "path": str(runtime_binary),
                    "sha256": assurance._sha256_bytes(
                        runtime_binary.read_bytes()
                    ),
                },
                "codex_profile": {
                    "path": str(profile_snapshot),
                    "sha256": assurance._sha256_bytes(
                        profile_snapshot.read_bytes()
                    ),
                },
            },
            "bindings": {"managed_primary": str(repo)},
            "tool_contract": {
                "enabled_tools": ["ack_output", "read_projection", "submit_request"]
            },
        }
        _write_json(manifest_path, manifest)
        config_artifacts = [
            {
                "role": "deployment_manifest",
                "path": str(manifest_path),
                "sha256": assurance._sha256_bytes(manifest_path.read_bytes()),
            },
            *[
                {
                    "role": role,
                    "path": str(path),
                    "sha256": assurance._sha256_bytes(path.read_bytes()),
                }
                for role, path in role_paths.items()
            ],
        ]
        config_artifacts.sort(key=lambda item: (item["role"], item["path"]))
        inventory, _inventory_classification = _frontend_tool_inventory()
        bindings = {
            "configuration_digest": assurance._stable_digest(config_artifacts),
            "runtime_binary_digest": assurance._sha256_bytes(runtime_binary.read_bytes()),
            "tool_inventory_digest": assurance._stable_digest(inventory),
            "checkout_digest": checkout["identity_digest"],
        }
        registry = assurance.load_registry()
        profile = _profile(registry, "codex-main-agent-a-prime")
        profile["configuration_requirements"]["deployment_manifest_path"] = str(manifest_path)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        launch_argv = canary._frontend_launch_argv(manifest)
        subject_pid = os.getpid()
        process_start_token = run_lock.process_start_token(subject_pid)
        assert process_start_token
        launch_session = {
            "launch_session_version": "2",
            "session_id": "launch-fixture",
            "deployment_id": profile["profile_id"],
            "profile_id": profile["profile_id"],
            "principal_id": profile["profile_id"],
            "workspace_id": "Saber5656/Saihai",
            "subject_pid": subject_pid,
            "process_start_token": process_start_token,
            "native_realpath": str(runtime_binary),
            "native_digest": bindings["runtime_binary_digest"],
            "profile_realpath": str(profile_snapshot),
            "profile_digest": assurance._sha256_bytes(profile_snapshot.read_bytes()),
            "launch_argv_digest": assurance._stable_digest(launch_argv),
            "checkout_realpath": str(repo),
            "checkout_identity_digest": bindings["checkout_digest"],
            "issued_at": assurance.format_timestamp(now),
            "valid_until": assurance.format_timestamp(now + timedelta(hours=1)),
            "status": "active",
            "session_kind": "standard",
            "commissioning_launch_reference": None,
            "commissioning_launch_digest": None,
            "supervisor_pid": subject_pid,
            "supervisor_start_token": process_start_token,
            "record_reference": "launch-sessions/launch-fixture.json",
            "record_digest": "sha256:" + "0" * 64,
        }
        launch_session["record_digest"] = assurance._stable_digest(
            supervisor._session_material(launch_session)
        )
        _write_json(assurance_root / launch_session["record_reference"], launch_session)
        (assurance_root / launch_session["record_reference"]).chmod(0o644)
        common_receipt = {
            "receipt_version": "1",
            "receipt_id": "fixture-common-observation",
            "profile_id": profile["profile_id"],
            "observed_at": assurance.format_timestamp(now),
            "observer": {
                "kind": "sandbox_policy_audit",
                "binary_realpath": str(observer_binary),
                "binary_sha256": assurance._sha256_bytes(
                    observer_binary.read_bytes()
                ),
            },
            "event": {
                "event_id": "fixture-common-event",
                "event_kind": "effective_agent_identity",
                "subject_pid": subject_pid,
                "process_start_token": process_start_token,
                "runtime_binary_realpath": str(runtime_binary),
                "runtime_binary_digest": bindings["runtime_binary_digest"],
                "runtime_argv": launch_argv,
                "runtime_argv_digest": assurance._stable_digest(launch_argv),
                "profile_snapshot_path": str(profile_snapshot),
                "profile_snapshot_digest": assurance._sha256_bytes(
                    profile_snapshot.read_bytes()
                ),
                "tool_inventory": inventory,
                "tool_inventory_classification": _inventory_classification,
                "tool_inventory_observation": _tool_inventory_observation(
                    manifest_path=manifest_path,
                    manifest=manifest,
                    launch_session=launch_session,
                    inventory=inventory,
                    classification=_inventory_classification,
                    runtime_argv_digest=assurance._stable_digest(launch_argv),
                ),
                "launcher_realpath": str(launcher_binary),
                "launcher_digest": assurance._sha256_bytes(
                    launcher_binary.read_bytes()
                ),
                "launcher_process_start_token": process_start_token,
                "host_verified_launch_token": "fixture-launch-token",
                "launch_argv_digest": assurance._stable_digest(launch_argv),
                "effective_notify_empty": True,
                **bindings,
                "launch_session": launch_session,
                "worker_execution_binding": None,
            },
        }
        common_receipt_path = observer_dir / "fixture-common-observation.json"
        _write_json(common_receipt_path, common_receipt)
        common_receipt_reference = {
            "path": str(common_receipt_path.relative_to(assurance_root)),
            "sha256": assurance._sha256_bytes(common_receipt_path.read_bytes()),
        }
        details = {
            "configuration_snapshot": {"artifacts": config_artifacts},
            "runtime_binary_identity": {"binary_realpath": str(runtime_binary)},
            "tool_inventory": {
                "inventory": inventory,
                "classification": _inventory_classification,
                "process_binding": {
                    "subject_pid": subject_pid,
                    "process_start_token": process_start_token,
                },
                "launcher_binding": {
                    "subject_pid": subject_pid,
                    "process_start_token": process_start_token,
                    "launcher_realpath": str(launcher_binary),
                    "launcher_digest": assurance._sha256_bytes(
                        launcher_binary.read_bytes()
                    ),
                    "launcher_process_start_token": process_start_token,
                    "host_verified_launch_token": "fixture-launch-token",
                    "launch_argv": launch_argv,
                    "launch_argv_digest": assurance._stable_digest(launch_argv),
                    "effective_notify_empty": True,
                },
                "observer_receipt": common_receipt_reference,
            },
            "checkout_binding": {"checkout_identity": checkout},
        }
        observed = {
            "configuration_snapshot": bindings["configuration_digest"],
            "runtime_binary_identity": bindings["runtime_binary_digest"],
            "tool_inventory": bindings["tool_inventory_digest"],
            "checkout_binding": bindings["checkout_digest"],
        }
        references: list[str] = []
        for index, evidence_type in enumerate(sorted(assurance.COMMON_EVIDENCE_TYPES)):
            evidence_id = f"common-{index}-{evidence_type}"
            payload = {
                "evidence_version": "2",
                "evidence_id": evidence_id,
                "generation_id": generation_id,
                "profile_id": profile["profile_id"],
                "claim": "common",
                "evidence_type": evidence_type,
                "operation": None,
                "result": "pass",
                "observed_at": assurance.format_timestamp(now),
                "valid_until": assurance.format_timestamp(now + timedelta(minutes=10)),
                "registry_subject_digest": assurance.profile_subject_digest(profile),
                "bindings": bindings,
                "launch_session": launch_session,
                "worker_execution_binding": None,
                "observed_digest": observed[evidence_type],
                "details": details[evidence_type],
            }
            filename = f"{evidence_id}.json"
            _write_json(evidence_dir / filename, payload)
            references.append(
                f"generations/{profile['profile_id']}/{generation_id}/evidence/{filename}"
            )
        marker = temp_root / "action.marker"
        yield Fixture(
            temp_root=temp_root,
            assurance_root=assurance_root,
            policy=policy,
            registry=registry,
            profile=profile,
            generation_id=generation_id,
            repo=repo,
            marker=marker,
            observer_binary=observer_binary,
            runtime_binary=runtime_binary,
            profile_snapshot=profile_snapshot,
            manifest_path=manifest_path,
            bindings=bindings,
            common_references=references,
            now=now,
            launch_session=launch_session,
        )


def _begin(fx: Fixture, *, evidence_type: str = "direct_action_denial", operation: str | None = "filesystem_write", identifier: str = "canary-test") -> dict:
    return canary.begin_action_canary(
        fx.assurance_root,
        profile_id=fx.profile["profile_id"],
        generation_id=fx.generation_id,
        evidence_type=evidence_type,
        operation=operation,
        marker_path=fx.marker,
        common_evidence_references=fx.common_references,
        expected_checkout=fx.repo,
        registry=fx.registry,
        trust_policy=fx.policy,
        now=fx.now,
        challenge_id=identifier,
    )


def _challenge(fx: Fixture, begin: dict) -> dict:
    return json.loads(
        (fx.assurance_root / begin["challenge_reference"]).read_text(encoding="utf-8")
    )


def _commissioning_probe_binding(
    fx: Fixture,
    *,
    challenge: dict,
    probe_argv: list[str],
) -> dict[str, object]:
    probe_id = (
        "frontend_filesystem_denial"
        if challenge["evidence_type"] == "direct_action_denial"
        else "frontend_gateway_routing"
    )
    session_id = f"launch-{challenge['challenge_id']}"
    commissioning_id = f"commission-{challenge['challenge_id']}"
    nonce_digest = assurance._stable_digest(
        {"challenge_id": challenge["challenge_id"]}
    )
    draft = supervisor.build_commissioning_launch(
        session_id=session_id,
        commissioning_id=commissioning_id,
        generation_id=challenge["generation_id"],
        profile_id=challenge["profile_id"],
        probe_id=probe_id,
        nonce_digest=nonce_digest,
        probe_argv=probe_argv,
        launch_session_digest="sha256:" + "0" * 64,
        issued_at=fx.now,
    )
    process_start_token = challenge["common_process_binding"][
        "process_start_token"
    ]
    session = {
        **fx.launch_session,
        "session_id": session_id,
        "launch_argv_digest": assurance._stable_digest(probe_argv),
        "session_kind": "commissioning",
        "commissioning_launch_reference": draft["record_reference"],
        "commissioning_launch_digest": draft["binding_digest"],
        "record_reference": f"launch-sessions/{session_id}.json",
        "record_digest": "sha256:" + "0" * 64,
    }
    session["record_digest"] = assurance._stable_digest(
        supervisor._session_material(session)
    )
    supervisor.validate_session_record_shape(session)
    companion = supervisor.build_commissioning_launch(
        session_id=session_id,
        commissioning_id=commissioning_id,
        generation_id=challenge["generation_id"],
        profile_id=challenge["profile_id"],
        probe_id=probe_id,
        nonce_digest=nonce_digest,
        probe_argv=probe_argv,
        launch_session_digest=session["record_digest"],
        issued_at=fx.now,
    )
    live_observation = {
        "observed_at": assurance.format_timestamp(fx.now + timedelta(seconds=1)),
        "subject_pid": session["subject_pid"],
        "process_start_token": process_start_token,
        "executable_realpath": session["native_realpath"],
        "parent_pid": session["supervisor_pid"],
        "supervisor_start_token": session["supervisor_start_token"],
    }
    companion["live_observation"] = live_observation
    companion["live_observation_digest"] = assurance._stable_digest(
        live_observation
    )
    companion["record_digest"] = assurance._stable_digest(
        supervisor._commissioning_record_material(companion)
    )
    supervisor.validate_commissioning_launch_shape(companion)
    session_path = fx.assurance_root / session["record_reference"]
    _write_json(session_path, session)
    session_path.chmod(0o644)
    companion_path = fx.assurance_root / companion["record_reference"]
    companion_path.parent.mkdir(mode=0o755, exist_ok=True)
    companion_path.parent.chmod(0o755)
    _write_json(companion_path, companion)
    companion_path.chmod(0o644)
    return {
        "commissioning_id": commissioning_id,
        "nonce_digest": nonce_digest,
        "launch_session_reference": session["record_reference"],
        "launch_session_digest": session["record_digest"],
        "commissioning_launch_reference": companion["record_reference"],
        "commissioning_binding_digest": companion["binding_digest"],
        "commissioning_live_observation_digest": companion[
            "live_observation_digest"
        ],
    }


def _receipt(
    fx: Fixture,
    begin: dict,
    *,
    kind: str = "sandbox_policy_audit",
    decision: str = "denied",
    state_linkage=None,
    capability_verification=None,
    execution_observation=None,
) -> tuple[dict, dict[str, str]]:
    challenge = _challenge(fx, begin)
    structured = (
        challenge["evidence_type"] == "direct_action_denial"
        and challenge["operation"] == "filesystem_write"
    ) or (
        challenge["evidence_type"] == "gateway_positive_path"
        and challenge["operation"] is None
    )
    probe_contract = challenge["probe_contract"]
    if challenge["evidence_type"] == "gateway_positive_path":
        probe_argv = canary.frontend_gateway_probe_argv(
            probe_contract["runtime_argv"], probe_contract["checkout_realpath"]
        )
    elif structured:
        probe_argv = canary.frontend_filesystem_probe_argv(
            probe_contract["runtime_argv"], probe_contract["checkout_realpath"]
        )
    else:
        probe_argv = None
    commissioning_binding = (
        _commissioning_probe_binding(
            fx, challenge=challenge, probe_argv=probe_argv
        )
        if structured
        else None
    )
    payload = {
        "receipt_version": "1",
        "receipt_id": f"receipt-{challenge['challenge_id']}",
        "challenge_id": challenge["challenge_id"],
        "challenge_sha256": begin["challenge_sha256"],
        "profile_id": challenge["profile_id"],
        "evidence_type": challenge["evidence_type"],
        "claim": challenge["claim"],
        "operation": challenge["operation"],
        "observed_at": assurance.format_timestamp(fx.now + timedelta(seconds=1)),
        "observer": {
            "kind": kind,
            "binary_realpath": str(fx.observer_binary),
            "binary_sha256": assurance._sha256_bytes(fx.observer_binary.read_bytes()),
        },
        "event": {
            "event_id": "observer-event-001",
            "event_kind": (
                "operation_attempt"
                if structured
                else "mechanical_absence_proof"
            ),
            "operation": challenge["operation"],
            "decision": decision,
            "observation_basis": (
                "structured_attempt" if structured else "mechanically_absent"
            ),
            "structured_event_digest": (
                "sha256:" + "a" * 64 if structured else None
            ),
            "probe_process_binding": (
                {
                    "subject_pid": challenge["common_process_binding"]["subject_pid"],
                    "process_start_token": challenge["common_process_binding"][
                        "process_start_token"
                    ],
                    "runtime_realpath": probe_contract["runtime_realpath"],
                    "runtime_digest": probe_contract["runtime_digest"],
                    "argv": probe_argv,
                    "argv_digest": assurance._stable_digest(probe_argv),
                    "profile_realpath": probe_contract["profile_realpath"],
                    "profile_digest": probe_contract["profile_digest"],
                    **commissioning_binding,
                }
                if structured
                else None
            ),
            "subject_pid": challenge["common_process_binding"]["subject_pid"],
            "process_start_token": challenge["common_process_binding"][
                "process_start_token"
            ],
            "capability_verification": capability_verification,
            "execution_observation": execution_observation,
            **fx.bindings,
        },
        "effective_bindings": fx.bindings,
        "state_linkage": state_linkage,
    }
    path = fx.assurance_root / begin["expected_observer_receipt"]
    _write_json(path, payload)
    return payload, {
        "path": begin["expected_observer_receipt"],
        "sha256": assurance._sha256_bytes(path.read_bytes()),
    }


def _finish(fx: Fixture, begin: dict, reference: dict[str, str]) -> dict:
    return canary.finish_action_canary(
        fx.assurance_root,
        challenge_reference=begin["challenge_reference"],
        observer_receipt_reference=reference,
        expected_checkout=fx.repo,
        registry=fx.registry,
        trust_policy=fx.policy,
        now=fx.now + timedelta(seconds=2),
    )


def _common_receipt(
    fx: Fixture, *, inventory: list[str] | None = None
) -> tuple[dict, dict[str, str]]:
    observed_inventory, classification = _frontend_tool_inventory(
        active_external_mutation_tools=(
            ["shell"] if inventory is not None and "shell" in inventory else []
        )
    )
    if inventory is not None:
        observed_inventory = inventory
    manifest = json.loads(fx.manifest_path.read_text(encoding="utf-8"))
    event = {
        "event_id": "observer-common-001",
        "event_kind": "effective_agent_identity",
        "subject_pid": fx.launch_session["subject_pid"],
        "process_start_token": fx.launch_session["process_start_token"],
        "runtime_binary_realpath": str(fx.runtime_binary),
        "runtime_binary_digest": fx.bindings["runtime_binary_digest"],
        "runtime_argv": canary._frontend_launch_argv(manifest),
        "runtime_argv_digest": assurance._stable_digest(
            canary._frontend_launch_argv(manifest)
        ),
        "profile_snapshot_path": str(fx.profile_snapshot),
        "profile_snapshot_digest": assurance._sha256_bytes(
            fx.profile_snapshot.read_bytes()
        ),
        "tool_inventory": observed_inventory,
        "tool_inventory_classification": classification,
        "tool_inventory_observation": _tool_inventory_observation(
            manifest_path=fx.manifest_path,
            manifest=manifest,
            launch_session=fx.launch_session,
            inventory=observed_inventory,
            classification=classification,
            runtime_argv_digest=assurance._stable_digest(
                canary._frontend_launch_argv(manifest)
            ),
        ),
        "launcher_realpath": manifest["artifacts"]["codex_launcher"]["path"],
        "launcher_digest": manifest["artifacts"]["codex_launcher"]["sha256"],
        "launcher_process_start_token": fx.launch_session["process_start_token"],
        "host_verified_launch_token": "launch-common-001",
        "launch_argv_digest": assurance._stable_digest(
            canary._frontend_launch_argv(manifest)
        ),
        "effective_notify_empty": True,
        "configuration_digest": fx.bindings["configuration_digest"],
        "tool_inventory_digest": assurance._stable_digest(observed_inventory),
        "checkout_digest": fx.bindings["checkout_digest"],
        "launch_session": fx.launch_session,
        "worker_execution_binding": None,
    }
    payload = {
        "receipt_version": "1",
        "receipt_id": "common-observation-001",
        "profile_id": fx.profile["profile_id"],
        "observed_at": assurance.format_timestamp(fx.now),
        "observer": {
            "kind": "sandbox_policy_audit",
            "binary_realpath": str(fx.observer_binary),
            "binary_sha256": assurance._sha256_bytes(
                fx.observer_binary.read_bytes()
            ),
        },
        "event": event,
    }
    directory = (
        fx.assurance_root
        / "generations"
        / fx.profile["profile_id"]
        / fx.generation_id
        / "observer"
    )
    path = directory / "common-observation-001.json"
    _write_json(path, payload)
    return payload, {
        "path": str(path.relative_to(fx.assurance_root)),
        "sha256": assurance._sha256_bytes(path.read_bytes()),
    }


def test_common_record_requires_observed_effective_identity_and_emits_four_facts() -> None:
    with fixture() as fx:
        _payload, reference = _common_receipt(fx)
        result = canary.record_common_evidence(
            fx.assurance_root,
            profile_id=fx.profile["profile_id"],
            generation_id=fx.generation_id,
            observer_receipt_reference=reference,
            expected_checkout=fx.repo,
            registry=fx.registry,
            trust_policy=fx.policy,
            now=fx.now,
            deployment_verifier=lambda path: json.loads(
                path.read_text(encoding="utf-8")
            ),
        )
        assert result["decision"] == "common_evidence_recorded"
        assert len(result["evidence"]) == 4
        types = {
            json.loads(
                (fx.assurance_root / item["reference"]).read_text(encoding="utf-8")
            )["evidence_type"]
            for item in result["evidence"]
        }
        assert types == set(assurance.COMMON_EVIDENCE_TYPES)
        tool_reference = next(
            item["reference"]
            for item in result["evidence"]
            if json.loads(
                (fx.assurance_root / item["reference"]).read_text(
                    encoding="utf-8"
                )
            )["evidence_type"]
            == "tool_inventory"
        )
        tool_details = json.loads(
            (fx.assurance_root / tool_reference).read_text(encoding="utf-8")
        )["details"]
        assert tool_details["process_binding"] == {
            "subject_pid": fx.launch_session["subject_pid"],
            "process_start_token": fx.launch_session["process_start_token"],
        }
        assert tool_details["launcher_binding"]["subject_pid"] == fx.launch_session[
            "subject_pid"
        ]
        assert tool_details["observer_receipt"] == reference

    with fixture() as fx:
        _payload, reference = _common_receipt(
            fx,
            inventory=sorted(
                _frontend_tool_inventory(active_external_mutation_tools=["shell"])[0]
            ),
        )
        _expect(
            "tool_inventory_active_external_mutation_tools_exact_mismatch",
            lambda: canary.record_common_evidence(
                fx.assurance_root,
                profile_id=fx.profile["profile_id"],
                generation_id=fx.generation_id,
                observer_receipt_reference=reference,
                expected_checkout=fx.repo,
                registry=fx.registry,
                trust_policy=fx.policy,
                now=fx.now,
                deployment_verifier=lambda path: json.loads(
                    path.read_text(encoding="utf-8")
                ),
            ),
        )

    with fixture() as fx:
        payload, reference = _common_receipt(fx)
        payload["event"]["tool_inventory_classification"][
            "reviewed_internal_tools"
        ].append("exec_command")
        payload["event"]["tool_inventory_classification"][
            "reviewed_internal_tools"
        ].sort()
        payload["event"]["tool_inventory"].append("exec_command")
        payload["event"]["tool_inventory"].sort()
        payload["event"]["tool_inventory_digest"] = assurance._stable_digest(
            payload["event"]["tool_inventory"]
        )
        path = fx.assurance_root / reference["path"]
        _write_json(path, payload)
        reference["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _expect(
            "tool_inventory_reviewed_internal_tools_subset_mismatch",
            lambda: canary.record_common_evidence(
                fx.assurance_root,
                profile_id=fx.profile["profile_id"],
                generation_id=fx.generation_id,
                observer_receipt_reference=reference,
                expected_checkout=fx.repo,
                registry=fx.registry,
                trust_policy=fx.policy,
                now=fx.now,
                deployment_verifier=lambda manifest_path: json.loads(
                    manifest_path.read_text(encoding="utf-8")
                ),
            ),
        )

    with fixture() as fx:
        payload, reference = _common_receipt(fx)
        payload["event"]["launch_argv_digest"] = "sha256:" + "f" * 64
        path = fx.assurance_root / reference["path"]
        _write_json(path, payload)
        reference["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _expect(
            "effective_launch_contract_mismatch",
            lambda: canary.record_common_evidence(
                fx.assurance_root,
                profile_id=fx.profile["profile_id"],
                generation_id=fx.generation_id,
                observer_receipt_reference=reference,
                expected_checkout=fx.repo,
                registry=fx.registry,
                trust_policy=fx.policy,
                now=fx.now,
                deployment_verifier=lambda manifest_path: json.loads(
                    manifest_path.read_text(encoding="utf-8")
                ),
            ),
        )

    with fixture() as fx:
        worker = _profile(fx.registry, "codex-scoped-worker")
        worker_home = fx.temp_root / "worker-home"
        worker_home.mkdir(mode=0o700)
        worker_environment = scoped_worker.worker_environment(str(worker_home))
        worker_template = scoped_worker.worker_argv_template(str(fx.runtime_binary))
        worker_execution_binding = {
            "binding_version": "1",
            "runtime_realpath": str(fx.runtime_binary),
            "runtime_digest": assurance._sha256_bytes(
                fx.runtime_binary.read_bytes()
            ),
            "profile_mode": "ignored_by_fixed_argv",
            "profile_realpath": None,
            "profile_digest": scoped_worker.ignored_profile_digest(
                environment_digest=scoped_worker.sha256_digest(worker_environment)
            ),
            "codex_home_realpath": str(worker_home),
            "environment": worker_environment,
            "environment_digest": scoped_worker.sha256_digest(worker_environment),
            "argv_template": worker_template,
            "argv_digest": scoped_worker.sha256_digest(worker_template),
            "runner_profile_digest": scoped_worker.sha256_digest(
                scoped_worker.RUNNER_PROFILE
            ),
        }
        inventory = ["filesystem", "process", "shell"]
        artifacts = [
            {
                "role": "profile_snapshot",
                "path": str(fx.profile_snapshot),
                "sha256": assurance._sha256_bytes(
                    fx.profile_snapshot.read_bytes()
                ),
            }
        ]
        bindings = {
            "configuration_digest": assurance._stable_digest(artifacts),
            "runtime_binary_digest": worker_execution_binding["runtime_digest"],
            "tool_inventory_digest": assurance._stable_digest(inventory),
            "checkout_digest": assurance.worker_checkout_binding()[
                "identity_digest"
            ],
        }
        receipt = {
            "receipt_version": "1",
            "receipt_id": "worker-common-001",
            "profile_id": worker["profile_id"],
            "observed_at": assurance.format_timestamp(fx.now),
            "observer": {
                "kind": "host_gateway_executor",
                "binary_realpath": str(fx.observer_binary),
                "binary_sha256": assurance._sha256_bytes(
                    fx.observer_binary.read_bytes()
                ),
            },
            "event": {
                "event_id": "observer-worker-common-001",
                "event_kind": "effective_agent_identity",
                "subject_pid": 4545,
                "process_start_token": "proc-worker-common-001",
                "runtime_binary_realpath": worker_execution_binding[
                    "runtime_realpath"
                ],
                "runtime_binary_digest": bindings["runtime_binary_digest"],
                "runtime_argv": worker_execution_binding["argv_template"],
                "runtime_argv_digest": worker_execution_binding["argv_digest"],
                "profile_snapshot_path": str(fx.profile_snapshot),
                "profile_snapshot_digest": artifacts[0]["sha256"],
                "tool_inventory": inventory,
                "tool_inventory_classification": {
                    "inventory_version": "1",
                    "model_identifier": "gpt-5-codex-worker-fixture",
                    "server_backed_mcp_tools": [],
                    "reviewed_internal_tools": [],
                    "active_external_mutation_tools": inventory,
                    "mechanically_denied_tools": [],
                    "dynamic_tools": [],
                    "extension_tools": [],
                    "multi_agent_tools": [],
                },
                "tool_inventory_observation": None,
                "launcher_realpath": None,
                "launcher_digest": None,
                "launcher_process_start_token": None,
                "host_verified_launch_token": None,
                "launch_argv_digest": None,
                "effective_notify_empty": None,
                **bindings,
                "launch_session": None,
                "worker_execution_binding": worker_execution_binding,
            },
        }
        directory = (
            fx.assurance_root
            / "generations"
            / worker["profile_id"]
            / fx.generation_id
            / "observer"
        )
        directory.mkdir(parents=True, mode=0o700)
        for ancestor in (
            fx.assurance_root / "generations" / worker["profile_id"],
            fx.assurance_root
            / "generations"
            / worker["profile_id"]
            / fx.generation_id,
            directory,
        ):
            ancestor.chmod(0o700)
        path = directory / "worker-common-001.json"
        _write_json(path, receipt)
        result = canary.record_common_evidence(
            fx.assurance_root,
            profile_id=worker["profile_id"],
            generation_id=fx.generation_id,
            observer_receipt_reference={
                "path": str(path.relative_to(fx.assurance_root)),
                "sha256": assurance._sha256_bytes(path.read_bytes()),
            },
            registry=fx.registry,
            trust_policy=fx.policy,
            now=fx.now,
        )
        assert result["decision"] == "common_evidence_recorded"
        assert len(result["evidence"]) == 4


def test_direct_action_requires_structured_independent_attempt() -> None:
    with fixture() as fx:
        begin = _begin(fx)
        _payload, reference = _receipt(fx, begin)
        result = _finish(fx, begin, reference)
        assert result["decision"] == "evidence_recorded"
        evidence = json.loads(
            (fx.assurance_root / result["evidence_reference"]).read_text(encoding="utf-8")
        )
        assert evidence["evidence_type"] == "direct_action_denial"
        assert assurance.validate_evidence_payload(evidence) == []

    with fixture() as fx:
        tool_reference = next(
            reference
            for reference in fx.common_references
            if json.loads(
                (fx.assurance_root / reference).read_text(encoding="utf-8")
            )["evidence_type"]
            == "tool_inventory"
        )
        tool_path = fx.assurance_root / tool_reference
        tool_evidence = json.loads(tool_path.read_text(encoding="utf-8"))
        tool_evidence["details"]["launcher_binding"][
            "process_start_token"
        ] = "different-process-start"
        _write_json(tool_path, tool_evidence)
        _expect("common_launcher_binding_mismatch", lambda: _begin(fx))

    with fixture() as fx:
        begin = _begin(fx)
        payload, reference = _receipt(fx, begin)
        payload["event"]["event_kind"] = "model_statement"
        path = fx.assurance_root / reference["path"]
        _write_json(path, payload)
        reference["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _expect("external_observer_attempt_missing", lambda: _finish(fx, begin, reference))

    with fixture() as fx:
        begin = _begin(fx)
        payload, reference = _receipt(fx, begin)
        payload["event"]["subject_pid"] = 9999
        path = fx.assurance_root / reference["path"]
        _write_json(path, payload)
        reference["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _expect(
            "probe_process_binding_mismatch",
            lambda: _finish(fx, begin, reference),
        )

    with fixture() as fx:
        begin = _begin(fx, identifier="direct-popen-no-companion")
        payload, reference = _receipt(fx, begin)
        companion_reference = payload["event"]["probe_process_binding"][
            "commissioning_launch_reference"
        ]
        (fx.assurance_root / companion_reference).unlink()
        _expect(
            "commissioning_probe_binding_untrusted",
            lambda: _finish(fx, begin, reference),
        )

    with fixture() as fx:
        begin = _begin(fx)
        payload, reference = _receipt(fx, begin)
        payload["model_prose"] = "I tried and was denied"
        path = fx.assurance_root / reference["path"]
        _write_json(path, payload)
        reference["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _expect("observer_receipt_fields_invalid", lambda: _finish(fx, begin, reference))


def test_receipt_tamper_identity_mismatch_and_marker_change_fail_closed() -> None:
    with fixture() as fx:
        begin = _begin(fx)
        payload, reference = _receipt(fx, begin)
        payload["event"]["decision"] = "allowed"
        _write_json(fx.assurance_root / reference["path"], payload)
        _expect("observer_receipt_digest_mismatch", lambda: _finish(fx, begin, reference))

    with fixture() as fx:
        begin = _begin(fx)
        payload, reference = _receipt(fx, begin)
        payload["effective_bindings"]["tool_inventory_digest"] = "sha256:" + "f" * 64
        path = fx.assurance_root / reference["path"]
        _write_json(path, payload)
        reference["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _expect("observer_effective_binding_mismatch", lambda: _finish(fx, begin, reference))

    with fixture() as fx:
        begin = _begin(fx)
        _payload, reference = _receipt(fx, begin)
        fx.marker.write_text("unexpected side effect\n", encoding="utf-8")
        _expect("denial_marker_changed", lambda: _finish(fx, begin, reference))


def _derive_real_gateway_capability(fx: Fixture) -> tuple[Path, dict]:
    previous_repo = os.environ.get("SAIHAI_SCOPED_REPO_ROOT")
    original_resolve_refs = frontdoor.resolve_context_refs
    original_resolve_paths = frontdoor.resolve_allowed_paths
    os.environ["SAIHAI_SCOPED_REPO_ROOT"] = str(fx.repo)
    frontdoor.resolve_context_refs = lambda refs, **_kwargs: original_resolve_refs(
        refs, ref_root=fx.repo
    )
    frontdoor.resolve_allowed_paths = (
        lambda paths, **_kwargs: original_resolve_paths(paths, ref_root=fx.repo)
    )
    try:
        return worker_fixture.derive_e2e_capability(
            fx.temp_root / "gateway-real",
            fx.repo,
            issued_at_epoch=fx.now.timestamp(),
        )
    finally:
        frontdoor.resolve_context_refs = original_resolve_refs
        frontdoor.resolve_allowed_paths = original_resolve_paths
        if previous_repo is None:
            os.environ.pop("SAIHAI_SCOPED_REPO_ROOT", None)
        else:
            os.environ["SAIHAI_SCOPED_REPO_ROOT"] = previous_repo


def _complete_real_gateway_execution(
    fx: Fixture, state_root: Path, capability: dict
) -> tuple[dict, dict, dict]:
    verification_epoch = datetime.now(timezone.utc).replace(microsecond=0).timestamp()
    verification = canary.build_host_capability_verification(
        state_root=state_root,
        capability_id=capability["capability_id"],
        principal=worker_fixture.EXECUTOR,
        gateway_principal=worker_fixture.GATEWAY,
        signing_key=worker_fixture.SIGNING_KEY,
        current_assurance_binding=capability["assurance_binding"],
        now_epoch=verification_epoch,
    )
    executed = worker_fixture.execute_capability(
        state_root=state_root,
        capability_id=capability["capability_id"],
        principal=worker_fixture.EXECUTOR,
        gateway_principal=worker_fixture.GATEWAY,
        signing_key=worker_fixture.SIGNING_KEY,
        current_assurance_binding=capability["assurance_binding"],
        runner=worker_fixture.FakeCodexRunner(),
        now_epoch=verification_epoch + 1,
    )
    execution_id = executed["worker_execution"]["execution_id"]
    events, _audit_digest = canary._load_audit_events(state_root)
    approval = next(
        event
        for event in events
        if event.get("event_type") in canary.APPROVAL_EVENT_TYPES
        and event.get("outcome") == "ok"
        and event.get("subject", {}).get("request_id") == capability["request_id"]
    )
    operation = next(
        event
        for event in events
        if event.get("event_type") == "scoped_worker_execute"
        and event.get("outcome") == "ok"
        and event.get("subject", {}).get("execution_id") == execution_id
    )
    execution, execution_digest = canary._load_state_json(
        state_root, Path("worker-executions") / f"{execution_id}.json"
    )
    _evidence, evidence_digest = canary._load_state_json(
        state_root,
        Path("worker-evidence")
        / capability["run_id"]
        / f"{capability['step_id']}-{execution_id}.json",
    )
    linkage = {
        "state_root": str(state_root),
        "frontend_profile_id": fx.profile["profile_id"],
        "request_id": capability["request_id"],
        "capability_id": capability["capability_id"],
        "approval_audit_event_id": approval["event_id"],
        "operation_audit_event_id": operation["event_id"],
        "execution_id": execution_id,
    }
    execution_observation = {
        "execution_id": execution_id,
        "execution_digest": execution_digest,
        "execution_evidence_digest": evidence_digest,
        "audit_event_id": operation["event_id"],
        "audit_event_digest": scoped_worker.sha256_digest(operation),
    }
    assert execution["status"] == "completed"
    return linkage, verification, execution_observation


def _legacy_gateway_positive_path_required_capability_execution() -> None:
    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway",
        )
        state_root, capability = _derive_real_gateway_capability(fx)
        linkage, verification, execution_observation = (
            _complete_real_gateway_execution(fx, state_root, capability)
        )
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="allowed_via_saihai",
            state_linkage=linkage,
            capability_verification=verification,
            execution_observation=execution_observation,
        )
        fx.marker.write_text("gateway side effect\n", encoding="utf-8")
        result = _finish(fx, begin, reference)
        assert result["decision"] == "evidence_recorded"

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-mismatch",
        )
        state_root, capability = _derive_real_gateway_capability(fx)
        linkage, verification, execution_observation = (
            _complete_real_gateway_execution(fx, state_root, capability)
        )
        request_path = state_root / "requests" / f"{capability['request_id']}.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["workspace_id"] = "example/other"
        _write_json(request_path, request)
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="allowed_via_saihai",
            state_linkage=linkage,
            capability_verification=verification,
            execution_observation=execution_observation,
        )
        fx.marker.write_text("gateway side effect\n", encoding="utf-8")
        _expect("request_capability_linkage_mismatch", lambda: _finish(fx, begin, reference))

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-issuance-only",
        )
        state_root, capability = _derive_real_gateway_capability(fx)
        linkage, verification, execution_observation = (
            _complete_real_gateway_execution(fx, state_root, capability)
        )
        events, _audit_digest = canary._load_audit_events(state_root)
        issuance = next(
            event
            for event in events
            if event.get("event_type") == "scoped_worker_capability_issued"
            and event.get("subject", {}).get("capability_id")
            == capability["capability_id"]
        )
        linkage["operation_audit_event_id"] = issuance["event_id"]
        execution_observation["audit_event_id"] = issuance["event_id"]
        execution_observation["audit_event_digest"] = scoped_worker.sha256_digest(
            issuance
        )
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="allowed_via_saihai",
            state_linkage=linkage,
            capability_verification=verification,
            execution_observation=execution_observation,
        )
        fx.marker.write_text("gateway side effect\n", encoding="utf-8")
        _expect("operation_audit_capability_mismatch", lambda: _finish(fx, begin, reference))

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-replayable",
        )
        state_root, capability = _derive_real_gateway_capability(fx)
        linkage, verification, execution_observation = (
            _complete_real_gateway_execution(fx, state_root, capability)
        )
        capability_path = (
            state_root
            / "worker-capabilities"
            / f"{capability['capability_id']}.json"
        )
        replayable = json.loads(capability_path.read_text(encoding="utf-8"))
        replayable["execution_state"] = {
            "execution_count": 0,
            "nonce_state": "unused",
            "last_execution_id": None,
        }
        _write_json(capability_path, replayable)
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="allowed_via_saihai",
            state_linkage=linkage,
            capability_verification=verification,
            execution_observation=execution_observation,
        )
        fx.marker.write_text("gateway side effect\n", encoding="utf-8")
        _expect(
            "capability_execution_state_mismatch",
            lambda: _finish(fx, begin, reference),
        )

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-future-chronology",
        )
        state_root, capability = _derive_real_gateway_capability(fx)
        linkage, verification, execution_observation = (
            _complete_real_gateway_execution(fx, state_root, capability)
        )
        verification["verified_at"] = assurance.format_timestamp(
            fx.now + timedelta(days=1)
        )
        events, _audit_digest = canary._load_audit_events(state_root)
        operation = next(
            event
            for event in events
            if event.get("event_id") == linkage["operation_audit_event_id"]
        )
        operation["created_at"] = assurance.format_timestamp(
            fx.now + timedelta(days=1, seconds=1)
        )
        execution_observation["audit_event_digest"] = scoped_worker.sha256_digest(
            operation
        )
        audit_path = state_root / "audit/events.jsonl"
        audit_path.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="allowed_via_saihai",
            state_linkage=linkage,
            capability_verification=verification,
            execution_observation=execution_observation,
        )
        fx.marker.write_text("gateway side effect\n", encoding="utf-8")
        _expect(
            "operation_audit_after_observer_receipt",
            lambda: _finish(fx, begin, reference),
        )

    with fixture() as fx:
        state_root, capability = _derive_real_gateway_capability(fx)
        capability_path = (
            state_root
            / "worker-capabilities"
            / f"{capability['capability_id']}.json"
        )
        forged = json.loads(capability_path.read_text(encoding="utf-8"))
        forged["signature"]["value"] = "sha256:" + "f" * 64
        _write_json(capability_path, forged)
        _expect(
            "official_capability_verification_failed",
            lambda: canary.build_host_capability_verification(
                state_root=state_root,
                capability_id=capability["capability_id"],
                principal=worker_fixture.EXECUTOR,
                gateway_principal=worker_fixture.GATEWAY,
                signing_key=worker_fixture.SIGNING_KEY,
                current_assurance_binding=capability["assurance_binding"],
                now_epoch=fx.now.timestamp(),
            ),
        )


def _submit_routing_request(fx: Fixture, state_root: Path) -> tuple[dict, str]:
    identity = frontdoor.resolve_checkout_identity(
        workspace_id="Saber5656/Saihai",
        managed_primary=fx.repo,
        checkout_root=fx.repo,
    )
    principal = frontdoor.bridge_principal(
        "codex", principal_id=fx.profile["profile_id"]
    )
    key = "routing-idempotency-001"
    projection = frontdoor.bridge_submit_request(
        state_root=state_root,
        frontend_kind="codex",
        payload={
            "task_id": "TSK-routing",
            "request_id": "req-routing",
            "request_kind": "agent_task_request",
            "prompt": canary.ROUTING_ACCEPTANCE_PROMPT,
            "refs": ["README.md", "CHANGELOG.md"],
            "allowed_paths": [],
            "frontdoor": "codex",
            "chat_session_id": "routing-thread",
            "idempotency_key": key,
        },
        principal=principal,
        workspace_id="Saber5656/Saihai",
        checkout_identity=identity,
    )
    return projection, key


def _routing_setup(fx: Fixture, record_root: Path) -> tuple[dict, dict, str]:
    state_root = fx.temp_root / "routing-state"
    state_root.mkdir(mode=0o700)
    begin = canary.begin_routing_acceptance(
        record_root,
        state_root=state_root,
        profile_id=fx.profile["profile_id"],
        workspace_id="Saber5656/Saihai",
        managed_primary=fx.repo,
        checkout_root=fx.repo,
        marker_path=fx.repo / "README.md",
        now=fx.now,
        challenge_id="routing-test",
    )
    projection, key = _submit_routing_request(fx, state_root)
    return begin, projection, key


def _gateway_waiting_human_setup(
    fx: Fixture, action_begin: dict, *, identifier: str
) -> tuple[Path, dict, dict]:
    state_root = fx.temp_root / f"{identifier}-state"
    state_root.mkdir(mode=0o700)
    routing = canary.begin_routing_acceptance(
        fx.temp_root / f"{identifier}-routing",
        state_root=state_root,
        profile_id=fx.profile["profile_id"],
        workspace_id="Saber5656/Saihai",
        managed_primary=fx.repo,
        checkout_root=fx.repo,
        marker_path=fx.marker,
        now=fx.now,
        challenge_id=f"{identifier}-routing",
    )
    projection, key = _submit_routing_request(fx, state_root)
    linkage = {
        "state_root": str(state_root),
        "frontend_profile_id": fx.profile["profile_id"],
        "routing_challenge_path": routing["challenge_path"],
        "routing_challenge_sha256": routing["challenge_sha256"],
        "request_id": "req-routing",
        "idempotency_key_digest": projection["idempotency_key_digest"],
    }
    return state_root, projection, linkage


def test_gateway_positive_path_requires_exact_request_capability_audit_linkage() -> None:
    """A positive frontend gateway probe routes once and stops for a human."""

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-waiting",
        )
        state_root, projection, linkage = _gateway_waiting_human_setup(
            fx, begin, identifier="gateway-waiting"
        )
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="routed_to_saihai",
            state_linkage=linkage,
        )
        result = _finish(fx, begin, reference)
        observation = json.loads(
            (fx.assurance_root / result["observation_reference"]).read_text(
                encoding="utf-8"
            )
        )
        assert result["decision"] == "evidence_recorded"
        assert observation["outcome"] == "routed_to_saihai"
        assert observation["approval_prompted"] is False
        assert observation["side_effect_observed"] is False
        assert observation["capability_bound"] is False
        assert projection["request_status"] == "waiting_human"
        request = json.loads(
            (state_root / "requests" / "req-routing.json").read_text(
                encoding="utf-8"
            )
        )
        assert request["status"] == "waiting_human"
        for directory in (
            "classifications",
            "work-orders",
            "runs",
            "worker-capabilities",
            "worker-executions",
        ):
            assert not (state_root / directory).exists()

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-downstream",
        )
        state_root, _projection, linkage = _gateway_waiting_human_setup(
            fx, begin, identifier="gateway-downstream"
        )
        _write_json(
            state_root / "runs" / "run-forbidden.json",
            {"run_id": "run-forbidden", "request_id": "req-routing"},
        )
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="routed_to_saihai",
            state_linkage=linkage,
        )
        _expect(
            "premature_downstream_artifact:runs",
            lambda: _finish(fx, begin, reference),
        )

    with fixture() as fx:
        begin = _begin(
            fx,
            evidence_type="gateway_positive_path",
            operation=None,
            identifier="canary-gateway-side-effect",
        )
        _state_root, _projection, linkage = _gateway_waiting_human_setup(
            fx, begin, identifier="gateway-side-effect"
        )
        _payload, reference = _receipt(
            fx,
            begin,
            kind="host_gateway_executor",
            decision="routed_to_saihai",
            state_linkage=linkage,
        )
        fx.marker.write_text("forbidden frontend side effect\n", encoding="utf-8")
        _expect(
            "routing_workspace_marker_changed",
            lambda: _finish(fx, begin, reference),
        )


def test_routing_acceptance_observes_only_waiting_human_artifacts() -> None:
    with fixture() as fx:
        record_root = fx.temp_root / "routing-records"
        begin, projection, key = _routing_setup(fx, record_root)
        principal = frontdoor.bridge_principal(
            "codex", principal_id=fx.profile["profile_id"]
        )
        state_root = fx.temp_root / "routing-state"
        ack = frontdoor.bridge_ack_output(
            state_root=state_root,
            request_id="req-routing",
            projection_digest=projection["projection_digest"],
            frontdoor="codex",
            chat_session_id="routing-thread",
            principal=principal,
        )
        result = canary.finish_routing_acceptance(
            begin["challenge_path"],
            challenge_sha256=begin["challenge_sha256"],
            request_id="req-routing",
            idempotency_key_digest=projection["idempotency_key_digest"],
            ack_projection_digest=ack["expected_projection_digest"],
            now=fx.now + timedelta(seconds=2),
        )
        assert result["decision"] == "routing_observed"
        assert result["claim"] is None
        assert result["request_status"] == "waiting_human"
        assert result["downstream_artifacts"] == "absent_before_human_approval"
        assert result["ack_reference"] is not None

    with fixture() as fx:
        record_root = fx.temp_root / "routing-records"
        begin, projection, _key = _routing_setup(fx, record_root)
        audit_path = fx.temp_root / "routing-state" / "audit" / "events.jsonl"
        events = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = [
            event
            for event in events
            if event.get("event_type") != "bridge_read_projection"
        ]
        audit_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        result = canary.finish_routing_acceptance(
            begin["challenge_path"],
            challenge_sha256=begin["challenge_sha256"],
            request_id="req-routing",
            idempotency_key_digest=projection["idempotency_key_digest"],
            now=fx.now + timedelta(seconds=2),
        )
        assert result["decision"] == "routing_observed"
        assert result["ack_reference"] is None


def test_routing_acceptance_rejects_tamper_and_premature_downstream_artifact() -> None:
    with fixture() as fx:
        record_root = fx.temp_root / "routing-records"
        begin, _projection, key = _routing_setup(fx, record_root)
        path = Path(begin["challenge_path"])
        challenge = json.loads(path.read_text(encoding="utf-8"))
        challenge["profile_id"] = "tampered-profile"
        _write_json(path, challenge)
        _expect(
            "routing_challenge_digest_mismatch",
            lambda: canary.finish_routing_acceptance(
                path,
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        record_root = fx.temp_root / "routing-records"
        begin, _projection, key = _routing_setup(fx, record_root)
        request_path = fx.temp_root / "routing-state" / "requests" / "req-routing.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["request_kind"] = "external_review_request"
        _write_json(request_path, request)
        _expect(
            "routing_request_contract_mismatch",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        record_root = fx.temp_root / "routing-records"
        begin, _projection, key = _routing_setup(fx, record_root)
        downstream = fx.temp_root / "routing-state" / "runs" / "run-premature.json"
        _write_json(downstream, {"run_id": "run-premature", "request_id": "req-routing"})
        _expect(
            "premature_downstream_artifact:runs",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )


def test_routing_acceptance_requires_fresh_prompt_and_append_only_audit() -> None:
    with fixture() as fx:
        state_root = fx.temp_root / "routing-state"
        state_root.mkdir(mode=0o700)
        _projection, key = _submit_routing_request(fx, state_root)
        begin = canary.begin_routing_acceptance(
            fx.temp_root / "routing-records",
            state_root=state_root,
            profile_id=fx.profile["profile_id"],
            workspace_id="Saber5656/Saihai",
            managed_primary=fx.repo,
            checkout_root=fx.repo,
            marker_path=fx.repo / "README.md",
            now=fx.now,
            challenge_id="routing-old-request",
        )
        _expect(
            "routing_request_not_fresh",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        begin, _projection, key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        request_path = fx.temp_root / "routing-state/requests/req-routing.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["user_prompt"] = "A different research prompt."
        _write_json(request_path, request)
        _expect(
            "routing_request_contract_mismatch",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        state_root = fx.temp_root / "routing-state"
        state_root.mkdir(mode=0o700)
        frontdoor.append_audit_event(
            state_root=state_root,
            event_type="baseline_probe",
            principal=frontdoor.default_manual_principal(),
            subject={"request_id": "req-baseline", "task_id": "TSK-baseline"},
            outcome="ok",
        )
        begin = canary.begin_routing_acceptance(
            fx.temp_root / "routing-records",
            state_root=state_root,
            profile_id=fx.profile["profile_id"],
            workspace_id="Saber5656/Saihai",
            managed_primary=fx.repo,
            checkout_root=fx.repo,
            marker_path=fx.repo / "README.md",
            now=fx.now,
            challenge_id="routing-audit-prefix",
        )
        _projection, key = _submit_routing_request(fx, state_root)
        audit_path = state_root / "audit/events.jsonl"
        raw = audit_path.read_text(encoding="utf-8")
        assert "baseline_probe" in raw
        audit_path.write_text(
            raw.replace("baseline_probe", "baseline_pr0be", 1),
            encoding="utf-8",
        )
        _expect(
            "routing_audit_not_append_only",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        begin, _projection, key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        frontdoor.append_audit_event(
            state_root=fx.temp_root / "routing-state",
            event_type="request_approved",
            principal=frontdoor.default_manual_principal(),
            subject={"request_id": "req-routing", "task_id": "TSK-routing"},
            outcome="ok",
        )
        _expect(
            "routing_unexpected_audit_transition",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )


def test_routing_acceptance_rejects_extra_request_and_idempotency_artifacts() -> None:
    with fixture() as fx:
        begin, _projection, key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        _write_json(
            fx.temp_root / "routing-state/requests/req-extra.json",
            {"request_id": "req-extra"},
        )
        _expect(
            "routing_request_delta_not_exactly_one",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        begin, _projection, key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        _write_json(
            fx.temp_root / "routing-state/idempotency/key-extra.json",
            {"key_digest": "sha256:" + "0" * 64},
        )
        _expect(
            "routing_idempotency_delta_not_exactly_one",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )


def test_routing_acceptance_digest_is_canonical_and_legacy_raw_is_equivalent() -> None:
    with fixture() as fx:
        record_root = fx.temp_root / "routing-records"
        begin, projection, key = _routing_setup(fx, record_root)
        digest = projection["idempotency_key_digest"]
        result = canary.finish_routing_acceptance(
            begin["challenge_path"],
            challenge_sha256=begin["challenge_sha256"],
            request_id="req-routing",
            idempotency_key_digest=digest,
            now=fx.now + timedelta(seconds=2),
        )
        assert result["decision"] == "routing_observed"
        assert key not in json.dumps(projection, ensure_ascii=False)
        assert key not in json.dumps(result, ensure_ascii=False)
        raw_key = key.encode("utf-8")
        for root in (record_root, fx.temp_root / "routing-state"):
            for path in root.rglob("*"):
                if path.is_file():
                    assert raw_key not in path.read_bytes(), path

    with fixture() as fx:
        begin, _projection, key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        legacy = canary.finish_routing_acceptance(
            begin["challenge_path"],
            challenge_sha256=begin["challenge_sha256"],
            request_id="req-routing",
            idempotency_key=key,
            now=fx.now + timedelta(seconds=2),
        )
        assert legacy["decision"] == "routing_observed"
        assert legacy["authority"] == "untrusted_local_consistency"

    with fixture() as fx:
        begin, projection, key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        _expect(
            "routing_idempotency_contract_mismatch",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key_digest="sha256:" + "f" * 64,
                now=fx.now + timedelta(seconds=2),
            ),
        )
        _expect(
            "idempotency_key_digest_invalid",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key_digest="not-a-digest",
                now=fx.now + timedelta(seconds=2),
            ),
        )
        _expect(
            "routing_idempotency_input_conflict",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key_digest=projection["idempotency_key_digest"],
                idempotency_key=key,
                now=fx.now + timedelta(seconds=2),
            ),
        )

    with fixture() as fx:
        begin, projection, _key = _routing_setup(
            fx, fx.temp_root / "routing-records"
        )
        request_path = fx.temp_root / "routing-state/requests/req-routing.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["idempotency_key_digest"] = "sha256:" + "0" * 64
        _write_json(request_path, request)
        _expect(
            "routing_idempotency_contract_mismatch",
            lambda: canary.finish_routing_acceptance(
                begin["challenge_path"],
                challenge_sha256=begin["challenge_sha256"],
                request_id="req-routing",
                idempotency_key_digest=projection["idempotency_key_digest"],
                now=fx.now + timedelta(seconds=2),
            ),
        )


def test_action_cli_has_fixed_assurance_root_and_no_launch_or_key_surface() -> None:
    script = Path(canary.__file__)
    help_result = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", str(script), "action-begin", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--assurance-root" not in help_result.stdout
    common_help = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", str(script), "common-record", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[--expected-checkout EXPECTED_CHECKOUT]" in common_help.stdout
    assert "runtime-global bounded-worker profile" in common_help.stdout
    source = script.read_text(encoding="utf-8").lower()
    assert "subprocess.run" not in source
    assert "private_key" not in source
    assert "generate_key" not in source
    documentation = (
        WORKFLOW_ROOT / "profiles" / "agent-integration-canary.md"
    ).read_text(encoding="utf-8")
    assert "sudo /usr/bin/python3 -I -B" not in documentation
    assert documentation.count(
        "/usr/bin/python3 organization/runtime/workflows/scripts/agent_integration_canary.py"
    ) == 2


def test_atomic_publication_never_clobbers_a_concurrent_winner() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-canary-atomic-") as raw:
        root = Path(raw).resolve()
        root.chmod(0o700)
        policy = assurance.TrustPolicy.fixture(root)
        target = root / "winner.json"
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def publish(value: str) -> None:
            barrier.wait()
            try:
                canary._atomic_write(target, {"winner": value}, policy=policy)
                outcomes.append("ok:" + value)
            except canary.CanaryError as exc:
                outcomes.append(exc.reason + ":" + value)

        threads = [
            threading.Thread(target=publish, args=("a",)),
            threading.Thread(target=publish, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len([item for item in outcomes if item.startswith("ok:")]) == 1
        assert len(
            [item for item in outcomes if item.startswith("canary_artifact_already_exists:")]
        ) == 1
        stored = json.loads(target.read_text(encoding="utf-8"))["winner"]
        assert "ok:" + stored in outcomes


def test_state_reader_rejects_symlink_and_concurrent_change() -> None:
    with tempfile.TemporaryDirectory(prefix="saihai-canary-state-") as raw:
        root = Path(raw).resolve()
        root.chmod(0o700)
        data_dir = root / "requests"
        data_dir.mkdir()
        real = data_dir / "real.json"
        real.write_text('{"value":"original"}\n', encoding="utf-8")
        link = data_dir / "link.json"
        link.symlink_to(real)
        _expect(
            "state_artifact_unavailable",
            lambda: canary._load_state_json(root, Path("requests") / "link.json"),
        )

        original_read = canary.os.read
        changed = False

        def mutate_after_read(descriptor: int, size: int) -> bytes:
            nonlocal changed
            payload = original_read(descriptor, size)
            if payload and not changed:
                changed = True
                real.write_text('{"value":"changed-during-read"}\n', encoding="utf-8")
            return payload

        canary.os.read = mutate_after_read
        try:
            _expect(
                "state_artifact_changed_during_read",
                lambda: canary._load_state_json(
                    root, Path("requests") / "real.json"
                ),
            )
        finally:
            canary.os.read = original_read


def main() -> None:
    tests = [
        test_common_record_requires_observed_effective_identity_and_emits_four_facts,
        test_direct_action_requires_structured_independent_attempt,
        test_receipt_tamper_identity_mismatch_and_marker_change_fail_closed,
        test_gateway_positive_path_requires_exact_request_capability_audit_linkage,
        test_routing_acceptance_observes_only_waiting_human_artifacts,
        test_routing_acceptance_rejects_tamper_and_premature_downstream_artifact,
        test_routing_acceptance_requires_fresh_prompt_and_append_only_audit,
        test_routing_acceptance_rejects_extra_request_and_idempotency_artifacts,
        test_routing_acceptance_digest_is_canonical_and_legacy_raw_is_equivalent,
        test_action_cli_has_fixed_assurance_root_and_no_launch_or_key_surface,
        test_atomic_publication_never_clobbers_a_concurrent_winner,
        test_state_reader_rejects_symlink_and_concurrent_change,
    ]
    for test in tests:
        test()
    print(f"agent integration canary tests passed: {len(tests)}")


if __name__ == "__main__":
    main()
