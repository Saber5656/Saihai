#!/usr/bin/env python3
"""Host evidence, attester, and runtime gate tests."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = WORKFLOW_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402
import agent_integration_attester as attester  # noqa: E402
import agent_integration_observer as observer  # noqa: E402
import codex_main_agent_deployment as deployment  # noqa: E402
import codex_main_agent_supervisor as supervisor  # noqa: E402
import frontdoor_orchestrator as frontdoor  # noqa: E402
import run_lock  # noqa: E402
import scoped_worker_executor as scoped_worker  # noqa: E402


@dataclass
class HostFixture:
    temp_root: Path
    state_root: Path
    assurance_root: Path
    evidence_dir: Path
    generation_id: str
    deployment_epoch_id: str
    commissioning_id: str
    repo: Path
    config_file: Path
    runtime_binary: Path
    registry: dict
    profile: dict
    checkout_identity: dict[str, str]
    bindings: dict[str, str]
    inventory: list[str]
    references: list[str]
    policy: assurance.TrustPolicy
    now: datetime
    live_context: assurance.LiveClaimContext | None


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


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _profile(registry: dict, profile_id: str) -> dict:
    return next(item for item in registry["profiles"] if item["profile_id"] == profile_id)


def _evidence_details(
    evidence_type: str,
    *,
    config_artifacts: list[dict[str, str]],
    runtime_binary: Path,
    inventory: list[str],
    tool_details: dict,
    checkout_identity: dict[str, str],
    host_observation: dict[str, str] | None = None,
) -> dict:
    if evidence_type == "configuration_snapshot":
        return {"artifacts": config_artifacts}
    if evidence_type == "runtime_binary_identity":
        return {"binary_realpath": str(runtime_binary)}
    if evidence_type == "tool_inventory":
        assert tool_details["inventory"] == inventory
        return copy.deepcopy(tool_details)
    if evidence_type == "checkout_binding":
        return (
            {"checkout_binding": checkout_identity}
            if checkout_identity.get("checkout_binding") == "capability_per_execution"
            else {"checkout_identity": checkout_identity}
        )
    if host_observation is None:
        raise AssertionError("operation evidence requires host observation")
    return {"host_observation": host_observation}


def _make_host_observation(
    *,
    assurance_root: Path,
    observation_dir: Path,
    marker_dir: Path,
    profile: dict,
    bindings: dict[str, str],
    evidence_id: str,
    evidence_type: str,
    claim: str,
    operation: str | None,
    observed_at: str,
) -> dict[str, str]:
    coverage = profile["operation_requirements"].get(operation) if operation else None
    denied = evidence_type in {"direct_action_denial", "gateway_positive_path"} or (
        evidence_type == "capability_boundary" and coverage == "denied"
    )
    worker_attempted = True
    if evidence_type == "capability_boundary" and profile["surface_role"] == "bounded_worker":
        worker_attempted = operation in {"filesystem_write", "process_spawn"}
    before = marker_dir / f"{evidence_id}-before.marker"
    after = marker_dir / f"{evidence_id}-after.marker"
    effect_expected = (not denied) and worker_attempted
    before.write_bytes(b"unchanged\n" if not effect_expected else b"before\n")
    after.write_bytes(b"unchanged\n" if not effect_expected else b"after\n")
    before.chmod(0o600)
    after.chmod(0o600)
    generation_id = observation_dir.parent.name
    marker_prefix = (
        f"generations/{profile['profile_id']}/{generation_id}/markers"
    )
    capability_bound = evidence_type in {
        "capability_boundary",
        "worker_launch_binding",
    }
    side_effect = evidence_type in {
        "worker_launch_binding",
        "ingress_operation",
    } or (
        evidence_type == "capability_boundary"
        and coverage == "capability_scoped"
        and worker_attempted
    )
    outcome = {
        "direct_action_denial": "denied",
        "gateway_positive_path": "routed_to_saihai",
        "ingress_operation": "allowed_via_saihai",
        "worker_launch_binding": "launched",
    }.get(evidence_type)
    if evidence_type == "capability_boundary":
        outcome = (
            "capability_scoped"
            if coverage == "capability_scoped"
            else ("post_execution_invariant" if operation == "git_commit" else "denied")
        )
    uses_gateway_ids = evidence_type in {
        "gateway_positive_path",
        "ingress_operation",
        "capability_boundary",
        "worker_launch_binding",
    }
    observation = {
        "observation_version": "1",
        "observation_id": f"obs-{evidence_id}",
        "profile_id": profile["profile_id"],
        "evidence_type": evidence_type,
        "claim": claim,
        "operation": operation,
        "observed_at": observed_at,
        "source": "host_harness",
        "attempted": worker_attempted,
        "model_prose_authority": False,
        "outcome": outcome,
        "approval_prompted": False,
        "side_effect_observed": side_effect,
        "before_marker_path": f"{marker_prefix}/{before.name}",
        "before_marker_digest": assurance._sha256_bytes(before.read_bytes()),
        "after_marker_path": f"{marker_prefix}/{after.name}",
        "after_marker_digest": assurance._sha256_bytes(after.read_bytes()),
        "saihai_request_id": "req-host-001" if uses_gateway_ids else None,
        "capability_id": "cap-host-001" if capability_bound else None,
        "audit_id": "audit-host-001" if uses_gateway_ids else None,
        "capability_bound": capability_bound,
        "ambient_authority": False,
        "launch_binding": (
            {
                "binary_digest": bindings["runtime_binary_digest"],
                "configuration_digest": bindings["configuration_digest"],
                "checkout_identity_digest": bindings["checkout_digest"],
                "host_owned": True,
            }
            if evidence_type == "worker_launch_binding"
            else None
        ),
        "observer_receipt": None,
        "probe_process_binding": None,
    }
    path = observation_dir / f"{evidence_id}-observation.json"
    _write_json(path, observation)
    return {
        "path": (
            f"generations/{profile['profile_id']}/{generation_id}/observations/{path.name}"
        ),
        "sha256": assurance._sha256_bytes(path.read_bytes()),
    }


@contextmanager
def host_fixture(profile_id: str = "codex-main-agent-a-prime") -> Iterator[HostFixture]:
    with tempfile.TemporaryDirectory(prefix="saihai-assurance-") as raw_tmp:
        temp_root = Path(raw_tmp).resolve()
        temp_root.chmod(0o700)
        state_root = temp_root / "state"
        assurance_root = state_root / assurance.ASSURANCE_NAMESPACE
        generation_id = "generation-fixture"
        commissioning_id = "commission-fixture"
        generation_root = assurance_root / "generations" / profile_id / generation_id
        evidence_dir = generation_root / "evidence"
        observation_dir = generation_root / "observations"
        marker_dir = generation_root / "markers"
        external_observer_dir = generation_root / "observer"
        attestation_dir = generation_root
        evidence_dir.mkdir(parents=True)
        observation_dir.mkdir(parents=True)
        marker_dir.mkdir(parents=True)
        external_observer_dir.mkdir(parents=True)
        (assurance_root / "active").mkdir(parents=True)
        (assurance_root / "epochs").mkdir(parents=True)
        (assurance_root / "launch-sessions").mkdir(parents=True)
        (assurance_root / "commissioning" / profile_id).mkdir(parents=True)
        for directory in (
            state_root,
            assurance_root,
            assurance_root / "generations",
            assurance_root / "generations" / profile_id,
            generation_root,
            evidence_dir,
            attestation_dir,
            observation_dir,
            marker_dir,
            external_observer_dir,
            assurance_root / "active",
            assurance_root / "epochs",
            assurance_root / "launch-sessions",
            assurance_root / "commissioning",
            assurance_root / "commissioning" / profile_id,
        ):
            directory.chmod(0o700)

        repo = temp_root / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-b", "main")
        _run_git(repo, "config", "user.name", "Saihai Test")
        _run_git(repo, "config", "user.email", "saihai-test@example.invalid")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-m", "fixture")

        config_file = temp_root / "effective-config.toml"
        config_file.write_text('sandbox_mode = "read-only"\n', encoding="utf-8")
        runtime_binary = temp_root / "agent-runtime"
        runtime_binary.write_bytes(b"fixture-runtime-v1\n")
        runtime_binary.chmod(0o700)
        inventory = (
            ["filesystem", "process", "shell"]
            if profile_id == "codex-scoped-worker"
            else ["ack_output", "apply_patch", "read_projection", "submit_request"]
        )
        checkout_identity = frontdoor.resolve_checkout_identity(
            workspace_id="fixture/repo",
            managed_primary=repo,
            checkout_root=repo,
        )
        registry = assurance.load_registry()
        profile = _profile(registry, profile_id)
        if profile["surface_role"] == "bounded_worker":
            checkout_identity = assurance.worker_checkout_binding()
        config_artifacts: list[dict[str, str]]
        launcher_path: Path | None = None
        launcher_digest: str | None = None
        if profile_id == "codex-main-agent-a-prime":
            installed = temp_root / "installed"
            installed.mkdir()
            runtime_config = installed / "codex-main-agent.runtime.json"
            requirements = installed / "requirements.toml"
            bridge_wrapper = installed / "saihai-codex-main-agent-bridge"
            instructions = installed / "codex-main-agent.instructions.md"
            launcher_path = installed / "saihai-codex-main-agent"
            observer_binary = installed / "agent_integration_observer.py"
            supervisor_binary = installed / "codex_main_agent_supervisor.py"
            runtime_config.write_text('{"runtime_config_version":"1"}\n', encoding="utf-8")
            requirements.write_text('approval_policy = "never"\n', encoding="utf-8")
            bridge_wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            instructions.write_text("Use only the Saihai bridge.\n", encoding="utf-8")
            launcher_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            observer_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            supervisor_binary.write_text("fixture supervisor\n", encoding="utf-8")
            launcher_path.chmod(0o700)
            observer_binary.chmod(0o555)
            supervisor_binary.chmod(0o644)
            launcher_digest = assurance._sha256_bytes(launcher_path.read_bytes())
            role_paths = {
                "runtime_config": runtime_config,
                "requirements": requirements,
                "bridge_wrapper": bridge_wrapper,
                "instructions": instructions,
                "profile_snapshot": config_file,
                "observer": observer_binary,
                "supervisor": supervisor_binary,
            }
            manifest_path = installed / "codex-main-agent.deployment.json"
            manifest = {
                "artifacts": {
                    role: {
                        "path": str(path),
                        "sha256": assurance._sha256_bytes(path.read_bytes()),
                    }
                    for role, path in role_paths.items()
                    if role != "profile_snapshot"
                },
                "tool_contract": {
                    "enabled_tools": ["submit_request", "read_projection", "ack_output"]
                },
            }
            manifest["artifacts"]["codex_launcher"] = {
                "path": str(launcher_path),
                "sha256": launcher_digest,
            }
            manifest["artifacts"]["native_codex"] = {
                "path": str(runtime_binary),
                "sha256": assurance._sha256_bytes(runtime_binary.read_bytes()),
            }
            manifest["artifacts"]["codex_profile"] = {
                "path": str(config_file),
                "sha256": assurance._sha256_bytes(config_file.read_bytes()),
            }
            _write_json(manifest_path, manifest)
            profile["configuration_requirements"]["deployment_manifest_path"] = str(
                manifest_path
            )
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
        else:
            config_artifacts = [
                {
                    "role": "profile_snapshot",
                    "path": str(config_file),
                    "sha256": assurance._sha256_bytes(config_file.read_bytes()),
                }
            ]
        config_artifacts.sort(key=lambda item: (item["role"], item["path"]))
        bindings = {
            "configuration_digest": assurance._stable_digest(config_artifacts),
            "runtime_binary_digest": assurance._sha256_bytes(runtime_binary.read_bytes()),
            "tool_inventory_digest": assurance._stable_digest(inventory),
            "checkout_digest": checkout_identity["identity_digest"],
        }
        now = datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc)
        deployment_epoch_id = "epoch-fixture"
        epoch = {
            "epoch_version": "1",
            "profile_id": profile_id,
            "epoch_id": deployment_epoch_id,
            "state": "active_uncommissioned",
            "operation": "activate",
            "transaction_id": "transaction-fixture",
            "previous_epoch_id": None,
            "rotated_at": assurance.format_timestamp(now),
            "finalized_at": assurance.format_timestamp(now),
        }
        epoch_path = assurance_root / "epochs" / f"{profile_id}.json"
        _write_json(epoch_path, epoch)
        epoch_path.chmod(0o644)
        classification = {
            "inventory_version": "1",
            "model_identifier": "fixture-model",
            "server_backed_mcp_tools": (
                []
                if profile_id == "codex-scoped-worker"
                else ["ack_output", "read_projection", "submit_request"]
            ),
            "reviewed_internal_tools": [],
            "active_external_mutation_tools": (
                ["filesystem", "process", "shell"]
                if profile_id == "codex-scoped-worker"
                else []
            ),
            "mechanically_denied_tools": (
                [] if profile_id == "codex-scoped-worker" else ["apply_patch"]
            ),
            "dynamic_tools": [],
            "extension_tools": [],
            "multi_agent_tools": [],
        }
        subject_pid = os.getpid()
        process_start_token = run_lock.process_start_token(subject_pid)
        assert process_start_token
        if profile_id != "codex-main-agent-a-prime":
            observer_binary = temp_root / "host-observer"
            observer_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            observer_binary.chmod(0o700)
        launcher_binding = None
        event_launcher = {
            "launcher_realpath": None,
            "launcher_digest": None,
            "launcher_process_start_token": None,
            "host_verified_launch_token": None,
            "launch_argv_digest": None,
            "effective_notify_empty": None,
        }
        if launcher_path is not None and launcher_digest is not None:
            launch_argv = deployment.native_codex_argv(runtime_binary)
            launch_argv_digest = assurance._stable_digest(launch_argv)
            launcher_binding = {
                "subject_pid": subject_pid,
                "process_start_token": process_start_token,
                "launcher_realpath": str(launcher_path),
                "launcher_digest": launcher_digest,
                "launcher_process_start_token": process_start_token,
                "host_verified_launch_token": "fixture-host-launch",
                "launch_argv": launch_argv,
                "launch_argv_digest": launch_argv_digest,
                "effective_notify_empty": True,
            }
            event_launcher = {
                "launcher_realpath": str(launcher_path),
                "launcher_digest": launcher_digest,
                "launcher_process_start_token": process_start_token,
                "host_verified_launch_token": "fixture-host-launch",
                "launch_argv_digest": launch_argv_digest,
                "effective_notify_empty": True,
            }
        worker_execution_binding = None
        if profile_id == "codex-scoped-worker":
            worker_home = temp_root / "worker-home"
            worker_home.mkdir(mode=0o700)
            worker_environment = scoped_worker.worker_environment(str(worker_home))
            worker_template = scoped_worker.worker_argv_template(str(runtime_binary))
            worker_execution_binding = {
                "binding_version": "1",
                "runtime_realpath": str(runtime_binary),
                "runtime_digest": bindings["runtime_binary_digest"],
                "profile_mode": "ignored_by_fixed_argv",
                "profile_realpath": None,
                "profile_digest": scoped_worker.ignored_profile_digest(
                    environment_digest=scoped_worker.sha256_digest(
                        worker_environment
                    )
                ),
                "codex_home_realpath": str(worker_home),
                "environment": worker_environment,
                "environment_digest": scoped_worker.sha256_digest(
                    worker_environment
                ),
                "argv_template": worker_template,
                "argv_digest": scoped_worker.sha256_digest(worker_template),
                "runner_profile_digest": scoped_worker.sha256_digest(
                    scoped_worker.RUNNER_PROFILE
                ),
            }
        runtime_argv = (
            deployment.native_codex_argv(runtime_binary)
            if profile_id == "codex-main-agent-a-prime"
            else worker_execution_binding["argv_template"]
        )
        runtime_argv_digest = assurance._stable_digest(runtime_argv)
        launch_session = None
        live_context = None
        if profile.get("launch_validator") is not None:
            launch_session = {
                "launch_session_version": "2",
                "session_id": "launch-fixture",
                "deployment_id": profile_id,
                "profile_id": profile_id,
                "principal_id": profile_id,
                "workspace_id": "fixture/repo",
                "subject_pid": subject_pid,
                "process_start_token": process_start_token,
                "native_realpath": str(runtime_binary),
                "native_digest": bindings["runtime_binary_digest"],
                "profile_realpath": str(config_file),
                "profile_digest": assurance._sha256_bytes(config_file.read_bytes()),
                "launch_argv_digest": runtime_argv_digest,
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
            supervisor.validate_session_record_shape(launch_session)
            _write_json(
                assurance_root / launch_session["record_reference"], launch_session
            )
            (assurance_root / launch_session["record_reference"]).chmod(0o644)
            live_context = assurance.LiveClaimContext(
                subject_pid=launch_session["subject_pid"],
                process_start_token=launch_session["process_start_token"],
                supervisor_pid=launch_session["supervisor_pid"],
                supervisor_start_token=launch_session["supervisor_start_token"],
                executable_realpath=launch_session["native_realpath"],
                launch_argv_digest=launch_session["launch_argv_digest"],
                profile_realpath=launch_session["profile_realpath"],
                profile_digest=launch_session["profile_digest"],
                checkout_identity_digest=launch_session[
                    "checkout_identity_digest"
                ],
            )
        tool_inventory_observation = None
        if launch_session is not None:
            policy_facts = {
                name: True for name in assurance.TOOL_POLICY_FACT_FIELDS
            }
            tool_inventory_observation = {
                "observation_version": "1",
                "observation_kind": "live_session_machine_policy",
                "session_id": launch_session["session_id"],
                "launch_session_digest": launch_session["record_digest"],
                "deployment_manifest_digest": assurance._sha256_bytes(
                    manifest_path.read_bytes()
                ),
                "requirements_digest": manifest["artifacts"]["requirements"][
                    "sha256"
                ],
                "profile_digest": manifest["artifacts"]["codex_profile"][
                    "sha256"
                ],
                "fixed_argv_digest": runtime_argv_digest,
                "policy_facts": policy_facts,
                "policy_facts_digest": assurance._stable_digest(policy_facts),
                "inventory_digest": assurance._stable_digest(inventory),
                "classification_digest": assurance._stable_digest(classification),
            }
        receipt = {
            "receipt_version": "1",
            "receipt_id": "fixture-common-receipt",
            "profile_id": profile_id,
            "observed_at": assurance.format_timestamp(now),
            "observer": {
                "kind": "sandbox_policy_audit",
                "binary_realpath": str(observer_binary),
                "binary_sha256": assurance._sha256_bytes(observer_binary.read_bytes()),
            },
            "event": {
                "event_id": "fixture-common-event",
                "event_kind": "effective_agent_identity",
                "subject_pid": subject_pid,
                "process_start_token": process_start_token,
                "runtime_binary_realpath": str(runtime_binary),
                "runtime_binary_digest": bindings["runtime_binary_digest"],
                "runtime_argv": runtime_argv,
                "runtime_argv_digest": runtime_argv_digest,
                "profile_snapshot_path": str(config_file),
                "profile_snapshot_digest": assurance._sha256_bytes(config_file.read_bytes()),
                "tool_inventory": inventory,
                "tool_inventory_classification": classification,
                "tool_inventory_observation": tool_inventory_observation,
                **event_launcher,
                **bindings,
                "launch_session": launch_session,
                "worker_execution_binding": worker_execution_binding,
            },
        }
        receipt_path = external_observer_dir / "fixture-common-receipt.json"
        _write_json(receipt_path, receipt)
        tool_details = {
            "inventory": inventory,
            "classification": classification,
            "process_binding": {
                "subject_pid": subject_pid,
                "process_start_token": process_start_token,
            },
            "launcher_binding": launcher_binding,
            "observer_receipt": {
                "path": (
                    f"generations/{profile_id}/{generation_id}/observer/{receipt_path.name}"
                ),
                "sha256": assurance._sha256_bytes(receipt_path.read_bytes()),
            },
        }
        references: list[str] = []
        for index, (evidence_type, claim, operation) in enumerate(
            sorted(
                assurance._required_evidence_keys(profile),
                key=lambda item: (item[0], item[1], item[2] or ""),
            )
        ):
            evidence_id = f"ev-{index:02d}-{evidence_type}"
            observed_digest = None
            binding_by_type = {
                "configuration_snapshot": "configuration_digest",
                "runtime_binary_identity": "runtime_binary_digest",
                "tool_inventory": "tool_inventory_digest",
                "checkout_binding": "checkout_digest",
            }
            if evidence_type in binding_by_type:
                observed_digest = bindings[binding_by_type[evidence_type]]
            payload = {
                "evidence_version": "2",
                "evidence_id": evidence_id,
                "generation_id": generation_id,
                "profile_id": profile_id,
                "claim": claim,
                "evidence_type": evidence_type,
                "operation": operation,
                "result": "pass",
                "observed_at": assurance.format_timestamp(now),
                "valid_until": assurance.format_timestamp(now + timedelta(minutes=10)),
                "registry_subject_digest": assurance.profile_subject_digest(profile),
                "bindings": bindings,
                "launch_session": launch_session,
                "worker_execution_binding": worker_execution_binding,
                "observed_digest": observed_digest,
                "details": _evidence_details(
                    evidence_type,
                    config_artifacts=config_artifacts,
                    runtime_binary=runtime_binary,
                    inventory=inventory,
                    tool_details=tool_details,
                    checkout_identity=checkout_identity,
                    host_observation=(
                        None
                        if evidence_type in assurance.COMMON_EVIDENCE_TYPES
                        else _make_host_observation(
                            assurance_root=assurance_root,
                            observation_dir=observation_dir,
                            marker_dir=marker_dir,
                            profile=profile,
                            bindings=bindings,
                            evidence_id=evidence_id,
                            evidence_type=evidence_type,
                            claim=claim,
                            operation=operation,
                            observed_at=assurance.format_timestamp(now),
                        )
                    ),
                ),
            }
            filename = f"{evidence_id}.json"
            _write_json(evidence_dir / filename, payload)
            references.append(
                f"generations/{profile_id}/{generation_id}/evidence/{filename}"
            )
        policy = assurance.TrustPolicy.fixture(temp_root)
        manifest_result = attester.create_generation_manifest(
            assurance_root,
            profile_id,
            generation_id,
            commissioning_id,
            references,
            registry=registry,
            trust_policy=policy,
            now=now,
        )
        runtime_binding = {
            "runtime_realpath": str(runtime_binary),
            "runtime_digest": bindings["runtime_binary_digest"],
            "argv": runtime_argv,
            "argv_digest": runtime_argv_digest,
            "profile_realpath": str(config_file),
            "profile_digest": assurance._sha256_bytes(config_file.read_bytes()),
        }
        commissioning_record = {
            "commissioning_version": "1",
            "commissioning_id": commissioning_id,
            "single_use_nonce": "nonce-fixture",
            "profile_id": profile_id,
            "generation_id": generation_id,
            "purpose": (
                "frontend_action_suite"
                if profile["surface_role"] == "main_agent"
                else "managed_worker_surface_launch_probe"
            ),
            "operation": (
                "action_suite"
                if profile["surface_role"] == "main_agent"
                else "surface_launch"
            ),
            "launch_session_reference": (
                launch_session["record_reference"] if launch_session else None
            ),
            "launch_session_digest": (
                launch_session["record_digest"] if launch_session else None
            ),
            "runtime_binding": runtime_binding,
            "runtime_binding_digest": assurance._stable_digest(runtime_binding),
            "execution_binding": worker_execution_binding,
            "execution_binding_digest": (
                assurance._stable_digest(worker_execution_binding)
                if worker_execution_binding is not None
                else None
            ),
            "probe_argv_digest": assurance._stable_digest(runtime_argv),
            "marker_target_sha256": assurance._sha256_bytes(
                str(generation_root / "markers/commissioning.marker").encode("utf-8")
            ),
            "issued_at": assurance.format_timestamp(now),
            "valid_until": assurance.format_timestamp(now + timedelta(minutes=15)),
            "state": "completed",
            "claimed_at": assurance.format_timestamp(now),
            "completed_at": assurance.format_timestamp(now),
            "consumer_event_digest": "sha256:" + "a" * 64,
            "generation_manifest_digest": manifest_result["manifest"]["manifest_digest"],
            "record_digest": "sha256:" + "0" * 64,
        }
        commissioning_record["record_digest"] = assurance._stable_digest(
            observer._material(commissioning_record)
        )
        observer._validate_commissioning_shape(commissioning_record)
        _write_json(
            assurance_root
            / "commissioning"
            / profile_id
            / f"{commissioning_id}.json",
            commissioning_record,
        )
        original_live_readers = (
            assurance._live_process_start_token,
            assurance._live_parent_pid,
            assurance._live_process_executable,
            assurance._live_process_argv,
        )
        assurance._live_process_start_token = lambda _pid: process_start_token  # type: ignore[assignment]
        assurance._live_parent_pid = lambda _pid: subject_pid  # type: ignore[assignment]
        assurance._live_process_executable = lambda _pid: runtime_binary.resolve()  # type: ignore[assignment]
        assurance._live_process_argv = lambda _pid: tuple(runtime_argv)  # type: ignore[assignment]
        try:
            yield HostFixture(
                temp_root=temp_root,
                state_root=state_root,
                assurance_root=assurance_root,
                evidence_dir=evidence_dir,
                generation_id=generation_id,
                deployment_epoch_id=deployment_epoch_id,
                commissioning_id=commissioning_id,
                repo=repo,
                config_file=config_file,
                runtime_binary=runtime_binary,
                registry=registry,
                profile=profile,
                checkout_identity=checkout_identity,
                bindings=bindings,
                inventory=inventory,
                references=references,
                policy=policy,
                now=now,
                live_context=live_context,
            )
        finally:
            (
                assurance._live_process_start_token,
                assurance._live_parent_pid,
                assurance._live_process_executable,
                assurance._live_process_argv,
            ) = original_live_readers


def _refresh_generation_manifest(fixture: HostFixture) -> None:
    manifest_path = (
        fixture.assurance_root
        / "generations"
        / fixture.profile["profile_id"]
        / fixture.generation_id
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence"] = [
        {
            "reference": reference,
            "sha256": assurance._sha256_bytes(
                (fixture.assurance_root / reference).read_bytes()
            ),
        }
        for reference in sorted(fixture.references)
        if (fixture.assurance_root / reference).exists()
    ]
    manifest["manifest_digest"] = assurance._stable_digest(
        assurance._generation_manifest_material(manifest)
    )
    _write_json(manifest_path, manifest)
    commissioning_path = (
        fixture.assurance_root
        / "commissioning"
        / fixture.profile["profile_id"]
        / f"{fixture.commissioning_id}.json"
    )
    commissioning = json.loads(commissioning_path.read_text(encoding="utf-8"))
    commissioning["generation_manifest_digest"] = manifest["manifest_digest"]
    commissioning["record_digest"] = assurance._stable_digest(
        observer._material(commissioning)
    )
    _write_json(commissioning_path, commissioning)


def _seal(fixture: HostFixture) -> dict:
    _refresh_generation_manifest(fixture)
    return attester.seal_attestation(
        fixture.state_root,
        fixture.profile["profile_id"],
        fixture.generation_id,
        registry=fixture.registry,
        trust_policy=fixture.policy,
        now=fixture.now,
        expected_checkout=(
            None
            if fixture.profile["surface_role"] == "bounded_worker"
            else fixture.repo
        ),
    )


def _require(
    fixture: HostFixture,
    claim: str = "action_enforced",
    *,
    expected_checkout: Path | None = None,
    live_context: assurance.LiveClaimContext | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> assurance.VerifiedClaim:
    context = fixture.live_context if live_context is None else live_context
    checkout = (
        None
        if fixture.profile["surface_role"] == "bounded_worker"
        else (fixture.repo if expected_checkout is None else expected_checkout)
    )
    return assurance.require_claim(
        fixture.state_root,
        fixture.profile["profile_id"],
        claim,
        checkout,
        live_context=context,  # type: ignore[arg-type]
        registry=fixture.registry,
        trust_policy=fixture.policy if trust_policy is None else trust_policy,
        now=fixture.now if now is None else now,
    )


def _expect_gate_reason(action, expected: str) -> assurance.AssuranceGateError:
    try:
        action()
    except assurance.AssuranceGateError as exc:
        if expected not in exc.reason and not any(expected in item for item in exc.reasons):
            raise AssertionError(f"expected {expected!r}, got {exc.reason!r}/{exc.reasons!r}")
        return exc
    raise AssertionError(f"expected AssuranceGateError containing {expected!r}")


def _direct_denial_artifacts(fixture: HostFixture) -> tuple[Path, dict, Path, dict]:
    evidence_path = next(
        path
        for path in fixture.evidence_dir.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["evidence_type"]
        == "direct_action_denial"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    observation_path = fixture.assurance_root / evidence["details"]["host_observation"]["path"]
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    return evidence_path, evidence, observation_path, observation


def _tool_inventory_artifacts(fixture: HostFixture) -> tuple[Path, dict, Path, dict]:
    evidence_path = next(
        path
        for path in fixture.evidence_dir.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["evidence_type"]
        == "tool_inventory"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    receipt_path = fixture.assurance_root / evidence["details"]["observer_receipt"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    return evidence_path, evidence, receipt_path, receipt


def _rewrite_observation_and_rebind(
    evidence_path: Path,
    evidence: dict,
    observation_path: Path,
    observation: dict,
) -> None:
    _write_json(observation_path, observation)
    evidence["details"]["host_observation"]["sha256"] = assurance._sha256_bytes(
        observation_path.read_bytes()
    )
    _write_json(evidence_path, evidence)


def test_complete_frontend_attestation_and_import_safe_gate() -> None:
    with host_fixture() as fixture:
        sealed = _seal(fixture)
        assert _seal(fixture) == sealed
        assert {item["claim"] for item in sealed["verified_claims"]} == {
            "action_enforced"
        }
        record = _require(fixture)
        manifest = json.loads(
            (
                fixture.assurance_root
                / "generations"
                / fixture.profile["profile_id"]
                / fixture.generation_id
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert manifest["deployment_epoch_id"] == fixture.deployment_epoch_id
        assert record.checkout_identity_digest == fixture.checkout_identity["identity_digest"]
        assert record.checkout_binding == fixture.checkout_identity
        assert record.bindings == fixture.bindings
        assert record.attestation_digest.startswith("sha256:")
        assert len(record.evidence_digests) == len(fixture.references)
        assert record.as_dict()["bindings"]["configuration_digest"] == (
            fixture.bindings["configuration_digest"]
        )
        assert record.subject_binding_digest == assurance._stable_digest(
            fixture.live_context.as_dict()  # type: ignore[union-attr]
        )


def test_live_claim_context_is_required_and_every_field_is_exactly_bound() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        try:
            assurance.require_claim(
                fixture.state_root,
                fixture.profile["profile_id"],
                "action_enforced",
                fixture.repo,
                registry=fixture.registry,
                trust_policy=fixture.policy,
                now=fixture.now,
            )
        except TypeError:
            pass
        else:
            raise AssertionError("live_context must be a required keyword")
        assert fixture.live_context is not None
        mismatches = {
            "subject_pid": fixture.live_context.subject_pid + 100000,
            "process_start_token": "drift-subject-start",
            "supervisor_pid": fixture.live_context.supervisor_pid + 1,
            "supervisor_start_token": "drift-supervisor-start",
            "executable_realpath": str(fixture.temp_root / "drift-runtime"),
            "launch_argv_digest": "sha256:" + "1" * 64,
            "profile_realpath": str(fixture.temp_root / "drift-profile"),
            "profile_digest": "sha256:" + "2" * 64,
            "checkout_identity_digest": "sha256:" + "3" * 64,
        }
        for field, value in mismatches.items():
            _expect_gate_reason(
                lambda field=field, value=value: _require(
                    fixture,
                    live_context=replace(
                        fixture.live_context, **{field: value}
                    ),
                ),
                "claim_context_binding_mismatch",
            )


def test_live_os_identity_drift_each_suppresses_authority() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        cases = (
            (
                "_live_process_start_token",
                lambda _pid: "drift-live-start",
                "claim_live_start_token_mismatch",
            ),
            (
                "_live_parent_pid",
                lambda _pid: fixture.live_context.supervisor_pid + 1,  # type: ignore[union-attr]
                "claim_live_parent_mismatch",
            ),
            (
                "_live_process_executable",
                lambda _pid: fixture.temp_root,
                "claim_live_executable_mismatch",
            ),
            (
                "_live_process_argv",
                lambda _pid: ("drift-live-argv",),
                "claim_live_argv_mismatch",
            ),
        )
        for name, replacement_reader, reason in cases:
            original = getattr(assurance, name)
            setattr(assurance, name, replacement_reader)
            try:
                _expect_gate_reason(lambda: _require(fixture), reason)
            finally:
                setattr(assurance, name, original)


def _epoch_path(fixture: HostFixture) -> Path:
    return (
        fixture.assurance_root
        / "epochs"
        / f"{fixture.profile['profile_id']}.json"
    )


def _rewrite_epoch(fixture: HostFixture, **updates) -> dict:
    path = _epoch_path(fixture)
    epoch = json.loads(path.read_text(encoding="utf-8"))
    epoch.update(updates)
    _write_json(path, epoch)
    path.chmod(0o644)
    return epoch


def test_transitioning_uninstalled_missing_and_mode_drift_epochs_fail_closed() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        _rewrite_epoch(
            fixture,
            epoch_id="epoch-transition",
            state="transitioning",
            operation="activate",
            previous_epoch_id=fixture.deployment_epoch_id,
            finalized_at=None,
        )
        _expect_gate_reason(
            lambda: _require(fixture), "deployment_epoch_not_commissionable"
        )
        _expect_gate_reason(
            lambda: _seal(fixture), "deployment_epoch_not_commissionable"
        )

    with host_fixture() as fixture:
        _rewrite_epoch(
            fixture,
            epoch_id="epoch-uninstalled",
            state="uninstalled",
            operation="uninstall",
            previous_epoch_id=fixture.deployment_epoch_id,
        )
        _expect_gate_reason(
            lambda: _seal(fixture), "deployment_epoch_not_commissionable"
        )

    with host_fixture() as fixture:
        _epoch_path(fixture).unlink()
        _expect_gate_reason(lambda: _seal(fixture), "deployment_epoch_missing")

    with host_fixture() as fixture:
        _epoch_path(fixture).chmod(0o664)
        _expect_gate_reason(lambda: _seal(fixture), "deployment_epoch_untrusted")


def test_finalized_epoch_invalidates_old_generation_and_old_epoch_replay() -> None:
    with host_fixture() as fixture:
        old_epoch_raw = _epoch_path(fixture).read_bytes()
        _rewrite_epoch(
            fixture,
            epoch_id="epoch-current",
            state="active_uncommissioned",
            operation="activate",
            previous_epoch_id=fixture.deployment_epoch_id,
        )
        manifest_path = (
            fixture.assurance_root
            / "generations"
            / fixture.profile["profile_id"]
            / fixture.generation_id
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["deployment_epoch_id"] = "epoch-current"
        manifest["manifest_digest"] = assurance._stable_digest(
            assurance._generation_manifest_material(manifest)
        )
        _write_json(manifest_path, manifest)
        _seal(fixture)
        _epoch_path(fixture).write_bytes(old_epoch_raw)
        _epoch_path(fixture).chmod(0o644)
        _expect_gate_reason(lambda: _require(fixture), "deployment_epoch_mismatch")
        _expect_gate_reason(lambda: _seal(fixture), "deployment_epoch_mismatch")


def test_complete_worker_suite_cannot_promote_weak_denial_facts() -> None:
    with host_fixture("codex-scoped-worker") as fixture:
        boundary_files = [
            path
            for path in fixture.evidence_dir.glob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["evidence_type"]
            == "capability_boundary"
        ]
        assert len(boundary_files) == 12
        _expect_gate_reason(
            lambda: _seal(fixture),
            "worker_denial_fact_not_promotable",
        )
        assert not (
            fixture.assurance_root
            / "active"
            / "codex-scoped-worker.json"
        ).exists()
        assert not (
            fixture.assurance_root
            / "generations"
            / fixture.profile["profile_id"]
            / fixture.generation_id
            / "attestation.json"
        ).exists()


def test_preexisting_weak_worker_attestation_is_suppressed_on_every_gate_call() -> None:
    """A legacy active record cannot bypass the current per-call verifier."""

    with host_fixture("codex-scoped-worker") as fixture:
        blocked = assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS
        assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS = frozenset()
        try:
            _seal(fixture)
        finally:
            assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS = blocked
        assert (
            fixture.assurance_root / "active/codex-scoped-worker.json"
        ).exists()
        _expect_gate_reason(
            lambda: _require(fixture, "managed_worker"),
            "worker_denial_fact_not_promotable",
        )


def test_missing_or_failed_single_action_operation_cannot_seal() -> None:
    with host_fixture() as fixture:
        target = next(
            path
            for path in fixture.evidence_dir.glob("*.json")
            if (payload := json.loads(path.read_text(encoding="utf-8")))["evidence_type"]
            == "direct_action_denial"
            and payload["operation"] == "credential_access"
        )
        reference = (
            f"generations/{fixture.profile['profile_id']}/{fixture.generation_id}/evidence/{target.name}"
        )
        fixture.references.remove(reference)
        target.unlink()
        _expect_gate_reason(lambda: _seal(fixture), "claim_evidence_incomplete")
        assert not (
            fixture.assurance_root
            / "generations"
            / fixture.profile["profile_id"]
            / fixture.generation_id
            / "attestation.json"
        ).exists()

    with host_fixture() as fixture:
        target = next(
            path
            for path in fixture.evidence_dir.glob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["evidence_type"]
            == "direct_action_denial"
        )
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["result"] = "fail"
        _write_json(target, payload)
        _expect_gate_reason(lambda: _seal(fixture), "claim_evidence_incomplete")


def test_tampered_evidence_is_rejected_on_every_gate_call() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        _require(fixture)
        target = next(fixture.evidence_dir.glob("*.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["details"]["tampered"] = True
        _write_json(target, payload)
        _expect_gate_reason(
            lambda: _require(fixture),
            "evidence_digest_mismatch",
        )


def test_common_tool_identity_receipt_classification_and_launcher_are_bound() -> None:
    with host_fixture() as fixture:
        _evidence_path, _evidence, receipt_path, receipt = _tool_inventory_artifacts(
            fixture
        )
        receipt["event"]["tool_inventory_classification"]["model_identifier"] = (
            "tampered-model"
        )
        _write_json(receipt_path, receipt)
        _expect_gate_reason(
            lambda: _seal(fixture), "common_observer_receipt_digest_mismatch"
        )

    with host_fixture() as fixture:
        evidence_path, evidence, _receipt_path, _receipt = _tool_inventory_artifacts(
            fixture
        )
        evidence["details"]["classification"]["dynamic_tools"] = ["functions.exec"]
        _write_json(evidence_path, evidence)
        _expect_gate_reason(
            lambda: _seal(fixture), "common_observer_receipt_evidence_mismatch"
        )

    with host_fixture() as fixture:
        evidence_path, evidence, receipt_path, receipt = _tool_inventory_artifacts(
            fixture
        )
        evidence["details"]["launcher_binding"]["effective_notify_empty"] = False
        receipt["event"]["effective_notify_empty"] = False
        _write_json(receipt_path, receipt)
        evidence["details"]["observer_receipt"]["sha256"] = assurance._sha256_bytes(
            receipt_path.read_bytes()
        )
        _write_json(evidence_path, evidence)
        _expect_gate_reason(lambda: _seal(fixture), "common_launcher_binding_mismatch")

    with host_fixture() as fixture:
        evidence_path, evidence, receipt_path, receipt = _tool_inventory_artifacts(
            fixture
        )
        launch_argv = [*evidence["details"]["launcher_binding"]["launch_argv"], "--drift"]
        launch_argv_digest = assurance._stable_digest(launch_argv)
        evidence["details"]["launcher_binding"]["launch_argv"] = launch_argv
        evidence["details"]["launcher_binding"]["launch_argv_digest"] = launch_argv_digest
        receipt["event"]["launch_argv_digest"] = launch_argv_digest
        _write_json(receipt_path, receipt)
        evidence["details"]["observer_receipt"]["sha256"] = assurance._sha256_bytes(
            receipt_path.read_bytes()
        )
        _write_json(evidence_path, evidence)
        _expect_gate_reason(lambda: _seal(fixture), "common_launcher_binding_mismatch")

    with host_fixture() as fixture:
        _evidence_path, _evidence, receipt_path, receipt = _tool_inventory_artifacts(
            fixture
        )
        observer_path = Path(receipt["observer"]["binary_realpath"])
        observer_path.chmod(0o600)
        _expect_gate_reason(lambda: _seal(fixture), "common_observer_binary_trust_invalid")


def test_host_observation_nonexistent_tampered_unattempted_prose_and_mismatch_fail() -> None:
    with host_fixture() as fixture:
        _evidence_path, _evidence, observation_path, _observation = _direct_denial_artifacts(
            fixture
        )
        observation_path.unlink()
        _expect_gate_reason(lambda: _seal(fixture), "host_observation_trusted_artifact_missing")

    with host_fixture() as fixture:
        _seal(fixture)
        _evidence_path, _evidence, observation_path, observation = _direct_denial_artifacts(
            fixture
        )
        observation["outcome"] = "inconclusive"
        _write_json(observation_path, observation)
        _expect_gate_reason(
            lambda: _require(fixture),
            "host_observation_digest_mismatch",
        )

    for field, value, reason in (
        ("attempted", False, "host_observation_inconclusive"),
        ("model_prose_authority", True, "model_prose_not_evidence"),
        ("operation", "shell_exec", "host_observation_operation_mismatch"),
    ):
        with host_fixture() as fixture:
            evidence_path, evidence, observation_path, observation = _direct_denial_artifacts(
                fixture
            )
            observation[field] = value
            _rewrite_observation_and_rebind(
                evidence_path,
                evidence,
                observation_path,
                observation,
            )
            _expect_gate_reason(lambda: _seal(fixture), reason)

    with host_fixture() as fixture:
        evidence_path, evidence, observation_path, observation = _direct_denial_artifacts(
            fixture
        )
        after_path = fixture.assurance_root / observation["after_marker_path"]
        after_path.write_bytes(b"changed\n")
        after_path.chmod(0o600)
        observation["after_marker_digest"] = assurance._sha256_bytes(after_path.read_bytes())
        _rewrite_observation_and_rebind(
            evidence_path,
            evidence,
            observation_path,
            observation,
        )
        _expect_gate_reason(lambda: _seal(fixture), "direct_action_marker_changed")


def test_stale_evidence_and_attestation_suppress() -> None:
    with host_fixture() as fixture:
        for path in fixture.evidence_dir.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["observed_at"] = assurance.format_timestamp(
                fixture.now - timedelta(minutes=30)
            )
            payload["valid_until"] = assurance.format_timestamp(
                fixture.now + timedelta(minutes=1)
            )
            _write_json(path, payload)
        _expect_gate_reason(lambda: _seal(fixture), "evidence_stale")

    with host_fixture() as fixture:
        _seal(fixture)
        _expect_gate_reason(
            lambda: _require(
                fixture, now=fixture.now + timedelta(minutes=20)
            ),
            "attestation_expired",
        )


def test_configuration_binary_and_checkout_drift_each_suppress() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        fixture.config_file.write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")
        _expect_gate_reason(
            lambda: _require(fixture),
            "launch_session_artifact_drift",
        )

    with host_fixture() as fixture:
        _seal(fixture)
        fixture.runtime_binary.write_bytes(b"fixture-runtime-v2\n")
        _expect_gate_reason(
            lambda: _require(fixture),
            "launch_session_artifact_drift",
        )

    with host_fixture() as fixture:
        _seal(fixture)
        (fixture.repo / "untracked.txt").write_text("drift\n", encoding="utf-8")
        _expect_gate_reason(
            lambda: _require(fixture),
            "checkout_binding_drift",
        )


def test_attestation_claim_declaration_is_recomputed() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        path = (
            fixture.assurance_root
            / "generations"
            / fixture.profile["profile_id"]
            / fixture.generation_id
            / "attestation.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["claim_results"]["action_enforced"] = "fail"
        _write_json(path, payload)
        active_path = fixture.assurance_root / "active" / f"{fixture.profile['profile_id']}.json"
        active = json.loads(active_path.read_text(encoding="utf-8"))
        active["attestation"]["sha256"] = assurance._sha256_bytes(path.read_bytes())
        _write_json(active_path, active)
        _expect_gate_reason(
            lambda: _require(fixture),
            "attestation_claim_result_mismatch",
        )


def test_relative_contained_regular_digest_checked_references() -> None:
    with host_fixture() as fixture:
        manifest_path = (
            fixture.assurance_root
            / "generations"
            / fixture.profile["profile_id"]
            / fixture.generation_id
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["evidence"][0]["reference"] = (
            f"generations/{fixture.profile['profile_id']}/{fixture.generation_id}/evidence/../escape.json"
        )
        manifest["manifest_digest"] = assurance._stable_digest(
            assurance._generation_manifest_material(manifest)
        )
        _write_json(manifest_path, manifest)
        _expect_gate_reason(
            lambda: attester.build_attestation(
                fixture.state_root,
                fixture.profile["profile_id"],
                fixture.generation_id,
                registry=fixture.registry,
                trust_policy=fixture.policy,
                now=fixture.now,
            ),
            "generation_evidence_invalid",
        )

    with host_fixture() as fixture:
        target = next(fixture.evidence_dir.glob("*.json"))
        replacement = fixture.temp_root / "replacement.json"
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o600)
        target.unlink()
        target.symlink_to(replacement)
        _expect_gate_reason(lambda: _seal(fixture), "evidence_trusted_artifact")


def test_owner_mode_and_entire_trust_chain_are_enforced() -> None:
    with host_fixture() as fixture:
        fixture.assurance_root.chmod(0o770)
        _expect_gate_reason(lambda: _seal(fixture), "generation_manifest_unavailable")

    with host_fixture() as fixture:
        # Production policy starts at / and accepts only root-owned components;
        # a user-owned fixture below /private or /tmp must never be accepted.
        _expect_gate_reason(
            lambda: _require(
                fixture,
                trust_policy=assurance.TrustPolicy.production(),
            ),
            "deployment_epoch_untrusted",
        )


def test_expected_checkout_and_digest_are_exactly_bound() -> None:
    with host_fixture() as fixture:
        _seal(fixture)
        other = fixture.temp_root / "other"
        other.mkdir()
        _expect_gate_reason(
            lambda: _require(fixture, expected_checkout=other),
            "expected_checkout_mismatch",
        )


def test_attester_cli_has_no_root_override_or_key_material() -> None:
    script = Path(attester.__file__)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--state-root" not in result.stdout
    source = script.read_text(encoding="utf-8").lower()
    assert "private_key" not in source
    assert "generate_key" not in source


def test_activation_failure_rolls_back_and_identical_retry_recovers() -> None:
    with host_fixture() as fixture:
        original = assurance._verify_claim_unbound_informational

        def fail_activation(*_args, **_kwargs):
            raise assurance.AssuranceGateError("simulated_activation_failure")

        assurance._verify_claim_unbound_informational = fail_activation  # type: ignore[assignment]
        try:
            _expect_gate_reason(lambda: _seal(fixture), "simulated_activation_failure")
        finally:
            assurance._verify_claim_unbound_informational = original  # type: ignore[assignment]
        active = fixture.assurance_root / f"active/{fixture.profile['profile_id']}.json"
        attestation_path = (
            fixture.assurance_root
            / "generations"
            / fixture.profile["profile_id"]
            / fixture.generation_id
            / "attestation.json"
        )
        assert not active.exists()
        assert attestation_path.exists()
        recovered = _seal(fixture)
        assert active.exists()
        assert recovered["verified_claims"]


def main() -> None:
    tests = [
        test_complete_frontend_attestation_and_import_safe_gate,
        test_live_claim_context_is_required_and_every_field_is_exactly_bound,
        test_live_os_identity_drift_each_suppresses_authority,
        test_transitioning_uninstalled_missing_and_mode_drift_epochs_fail_closed,
        test_finalized_epoch_invalidates_old_generation_and_old_epoch_replay,
        test_complete_worker_suite_cannot_promote_weak_denial_facts,
        test_preexisting_weak_worker_attestation_is_suppressed_on_every_gate_call,
        test_missing_or_failed_single_action_operation_cannot_seal,
        test_tampered_evidence_is_rejected_on_every_gate_call,
        test_common_tool_identity_receipt_classification_and_launcher_are_bound,
        test_host_observation_nonexistent_tampered_unattempted_prose_and_mismatch_fail,
        test_stale_evidence_and_attestation_suppress,
        test_configuration_binary_and_checkout_drift_each_suppress,
        test_attestation_claim_declaration_is_recomputed,
        test_relative_contained_regular_digest_checked_references,
        test_owner_mode_and_entire_trust_chain_are_enforced,
        test_expected_checkout_and_digest_are_exactly_bound,
        test_attester_cli_has_no_root_override_or_key_material,
        test_activation_failure_rolls_back_and_identical_retry_recovers,
    ]
    for test in tests:
        test()
    print(f"agent integration attester tests passed: {len(tests)}")


if __name__ == "__main__":
    main()
