#!/usr/bin/env python3
"""Root-owned commissioning monitor and concrete assurance receipt producer.

This program is deliberately unavailable to the normal agent runtime.  A
human administrator invokes it during initial commissioning or renewal.  It
creates one short-lived, nonce-bound generation, derives the effective launch
identity and tool contract from root-owned artifacts, and records only facts
that it can independently observe.  It never accepts model prose, a caller
supplied pass boolean, arbitrary prompts, arbitrary argv, or arbitrary
executables.

Mechanically absent action interfaces can be proven from the digest-bound
declarative validator plus the pinned deployment.  An exposed action interface
requires a structured event captured by the monitor's own fixed probe.  An
unproven boundary is recorded only as ``fail``/``inconclusive`` evidence and
can never activate a claim.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hmac
import json
import os
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402


COMMISSIONING_FIELDS = {
    "commissioning_version",
    "commissioning_id",
    "single_use_nonce",
    "profile_id",
    "generation_id",
    "purpose",
    "operation",
    "launch_session_reference",
    "launch_session_digest",
    "runtime_binding",
    "runtime_binding_digest",
    "execution_binding",
    "execution_binding_digest",
    "probe_argv_digest",
    "marker_target_sha256",
    "issued_at",
    "valid_until",
    "state",
    "claimed_at",
    "completed_at",
    "consumer_event_digest",
    "generation_manifest_digest",
    "record_digest",
}
COMMISSIONING_STATES = {"pending", "claimed", "observed", "completed", "failed"}
COMMISSIONING_PURPOSES = {
    "frontend_action_suite": "action_suite",
    "managed_worker_surface_launch_probe": "surface_launch",
}
COMMISSIONING_LIFETIME_SECONDS = 15 * 60
WORKER_PROBE_OBSERVED_FACTS = {
    "surface_launch": "pinned_runtime_child_with_process_start_identity",
    "filesystem_write": "exact_marker_in_private_disposable_worktree",
    "process_spawn": "root_executor_spawned_exact_runtime",
    "git_commit": "no_head_after_probe",
}
WORKER_PROBE_POLICY_FACTS = {
    "shell_exec": "fixed_saihai_worker_profile_minimal_read_workspace_write",
    "network_egress": "saihai_worker_permission_profile.network.enabled=false",
    "external_mutation": "workspace_profile_and_network_disabled_not_same_rootfs_isolation",
    "provider_dispatch": "network_disabled_and_user_config_ignored",
    "git_push": "network_disabled_only_local_push_not_denied",
    "pr_create": "network_disabled_and_user_config_ignored",
    "release_publish": "network_disabled_and_user_config_ignored",
    "credential_access": "dedicated_auth_deny_configured_not_mechanically_proven",
    "agent_spawn": "multi_agent_and_plugin_features_disabled",
}
WORKER_EVENT_ENVELOPE_FIELDS = {
    "envelope_version",
    "commissioning_reference",
    "claimed_record_digest",
    "single_use_nonce_digest",
    "execution_binding_digest",
    "event",
    "event_digest",
}


class ObserverError(RuntimeError):
    """The observer cannot produce a conclusive root-trusted fact."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _known_codex_auth_paths_denied(
    requirements_raw: bytes,
    *,
    runtime_user_home: Path,
    codex_home: Path,
) -> bool:
    """Prove only the two known Codex auth paths in the dedicated frontend home.

    This deliberately does not claim that arbitrary user-readable files which
    may contain credentials are inaccessible.  The remaining machine-policy
    facts separately prove that credential-capable extension/tool classes are
    absent from the fixed frontend inventory.
    """

    legacy_codex_home = runtime_user_home / ".codex"
    if codex_home == legacy_codex_home:
        return False
    return all(
        path.encode("utf-8") in requirements_raw
        for path in (
            str(legacy_codex_home / "auth.json"),
            str(codex_home / "auth.json"),
        )
    )


def _gateway_state_linkage(
    *,
    state_root: Path,
    frontend_profile_id: str,
    routing_begin: Mapping[str, Any],
    request_id: str,
    idempotency_key: str,
) -> dict[str, str]:
    """Build the public gateway linkage without retaining the raw key."""

    import frontdoor_orchestrator as frontdoor

    return {
        "state_root": str(state_root),
        "frontend_profile_id": frontend_profile_id,
        "routing_challenge_path": str(routing_begin["challenge_path"]),
        "routing_challenge_sha256": str(routing_begin["challenge_sha256"]),
        "request_id": request_id,
        "idempotency_key_digest": frontdoor.idempotency_key_digest(idempotency_key),
    }


def _now(value: datetime | None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _material(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in sorted(COMMISSIONING_FIELDS - {"record_digest"})}


def _validate_commissioning_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != COMMISSIONING_FIELDS:
        raise ObserverError("commissioning_fields_invalid")
    record = dict(value)
    if record.get("commissioning_version") != "1":
        raise ObserverError("commissioning_version_invalid")
    for field in (
        "commissioning_id",
        "single_use_nonce",
        "profile_id",
        "generation_id",
    ):
        if not isinstance(record.get(field), str) or not assurance.SAFE_ID.fullmatch(record[field]):
            raise ObserverError(f"commissioning_{field}_invalid")
    purpose = record.get("purpose")
    if purpose not in COMMISSIONING_PURPOSES or record.get("operation") != COMMISSIONING_PURPOSES[purpose]:
        raise ObserverError("commissioning_purpose_invalid")
    reference = record.get("launch_session_reference")
    if reference is not None and (
        not isinstance(reference, str)
        or not reference.startswith("launch-sessions/")
        or not reference.endswith(".json")
    ):
        raise ObserverError("commissioning_launch_session_reference_invalid")
    for field in (
        "runtime_binding_digest",
        "probe_argv_digest",
        "marker_target_sha256",
        "record_digest",
    ):
        if not assurance._valid_digest(record.get(field)):
            raise ObserverError(f"commissioning_{field}_invalid")
    launch_digest = record.get("launch_session_digest")
    if (reference is None) != (launch_digest is None) or (
        launch_digest is not None and not assurance._valid_digest(launch_digest)
    ):
        raise ObserverError("commissioning_launch_session_digest_invalid")
    try:
        runtime_binding = assurance._validate_runtime_binding(record.get("runtime_binding"))
    except assurance.AssuranceContractError as exc:
        raise ObserverError("commissioning_runtime_binding_invalid") from exc
    if assurance._stable_digest(runtime_binding) != record["runtime_binding_digest"]:
        raise ObserverError("commissioning_runtime_binding_digest_mismatch")
    execution_binding = record.get("execution_binding")
    execution_digest = record.get("execution_binding_digest")
    if (execution_binding is None) != (execution_digest is None):
        raise ObserverError("commissioning_execution_binding_invalid")
    if execution_binding is not None:
        if not isinstance(execution_binding, Mapping):
            raise ObserverError("commissioning_execution_binding_invalid")
        execution_binding = json.loads(json.dumps(execution_binding))
        if (
            not assurance._valid_digest(execution_digest)
            or assurance._stable_digest(execution_binding) != execution_digest
        ):
            raise ObserverError("commissioning_execution_binding_digest_mismatch")
    if not assurance._valid_digest(record.get("probe_argv_digest")):
        raise ObserverError("commissioning_probe_argv_digest_invalid")
    issued = assurance._parse_timestamp(record.get("issued_at"), label="commissioning_issued_at")
    valid = assurance._parse_timestamp(record.get("valid_until"), label="commissioning_valid_until")
    if valid <= issued or valid - issued > timedelta(seconds=COMMISSIONING_LIFETIME_SECONDS):
        raise ObserverError("commissioning_time_window_invalid")
    if record.get("state") not in COMMISSIONING_STATES:
        raise ObserverError("commissioning_state_invalid")
    state = record["state"]
    expected_nulls = {
        "pending": (None, None, None, None),
        "claimed": ("set", None, None, None),
        "observed": ("set", "set", "set", None),
        "completed": ("set", "set", "set", "set"),
        "failed": ("set", "set", "set", None),
    }
    observed = (
        "set" if record.get("claimed_at") is not None else None,
        "set" if record.get("completed_at") is not None else None,
        "set" if record.get("consumer_event_digest") is not None else None,
        "set" if record.get("generation_manifest_digest") is not None else None,
    )
    if observed != expected_nulls[state]:
        raise ObserverError("commissioning_state_fields_invalid")
    for field in ("claimed_at", "completed_at"):
        if record.get(field) is not None:
            assurance._parse_timestamp(record[field], label=f"commissioning_{field}")
    for field in ("consumer_event_digest", "generation_manifest_digest"):
        if record.get(field) is not None and not assurance._valid_digest(record[field]):
            raise ObserverError(f"commissioning_{field}_invalid")
    if record["record_digest"] != assurance._stable_digest(_material(record)):
        raise ObserverError("commissioning_record_digest_mismatch")
    record["runtime_binding"] = runtime_binding
    record["execution_binding"] = execution_binding
    return record


def _public_commissioning_projection(value: Any) -> dict[str, Any]:
    """Return the exact public commissioning fields without the raw nonce."""

    record = _validate_commissioning_shape(value)
    return {
        "commissioning_version": record["commissioning_version"],
        "commissioning_id": record["commissioning_id"],
        "profile_id": record["profile_id"],
        "generation_id": record["generation_id"],
        "purpose": record["purpose"],
        "operation": record["operation"],
        "launch_session_reference": record["launch_session_reference"],
        "launch_session_digest": record["launch_session_digest"],
        "runtime_binding": record["runtime_binding"],
        "runtime_binding_digest": record["runtime_binding_digest"],
        "execution_binding": record["execution_binding"],
        "execution_binding_digest": record["execution_binding_digest"],
        "probe_argv_digest": record["probe_argv_digest"],
        "marker_target_sha256": record["marker_target_sha256"],
        "issued_at": record["issued_at"],
        "valid_until": record["valid_until"],
        "state": record["state"],
        "claimed_at": record["claimed_at"],
        "completed_at": record["completed_at"],
        "consumer_event_digest": record["consumer_event_digest"],
        "generation_manifest_digest": record["generation_manifest_digest"],
        "record_digest": record["record_digest"],
    }


def _prepare_directory(
    root: Path,
    parts: Sequence[str],
    *,
    policy: assurance.TrustPolicy,
    mode: int = 0o700,
) -> Path:
    assurance._validate_root_chain(root, policy)
    if os.geteuid() not in policy.expected_owner_uids:
        raise ObserverError("commissioning_process_owner_not_authorized")
    current = root
    for part in parts:
        if not assurance.SAFE_PATH_COMPONENT.fullmatch(part):
            raise ObserverError("commissioning_directory_component_invalid")
        current = current / part
        try:
            current.mkdir(mode=mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ObserverError("commissioning_directory_unavailable") from exc
        assurance._validate_root_chain(current, policy)
        if (
            policy == assurance.TrustPolicy.production()
            and stat.S_IMODE(current.lstat().st_mode) != mode
        ):
            raise ObserverError("commissioning_directory_mode_invalid")
    return current


def _atomic_write(
    path: Path,
    payload: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
    replace: bool,
    mode: int = 0o600,
) -> None:
    assurance._validate_root_chain(path.parent, policy)
    if os.geteuid() not in policy.expected_owner_uids:
        raise ObserverError("commissioning_process_owner_not_authorized")
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw_path)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise ObserverError("commissioning_artifact_already_exists") from exc
            temporary.unlink()
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except ObserverError:
        raise
    except OSError as exc:
        raise ObserverError("commissioning_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _atomic_write_bytes(
    path: Path,
    raw: bytes,
    *,
    policy: assurance.TrustPolicy,
    mode: int,
) -> None:
    assurance._validate_root_chain(path.parent, policy)
    if os.geteuid() not in policy.expected_owner_uids:
        raise ObserverError("commissioning_process_owner_not_authorized")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_path)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ObserverError("commissioning_artifact_already_exists") from exc
        temporary.unlink()
        temporary = None
        directory_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except ObserverError:
        raise
    except OSError as exc:
        raise ObserverError("commissioning_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _load_session(
    root: Path,
    reference: str,
    *,
    policy: assurance.TrustPolicy,
    now: datetime,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        raw = assurance.secure_read_relative(root, reference, policy=policy)
        value = assurance._load_json_bytes(raw, label="launch_session")
        session = assurance._validate_launch_session_shape(value, profile)
    except (assurance.EvidenceTrustError, assurance.AssuranceContractError) as exc:
        raise ObserverError("launch_session_untrusted") from exc
    if session is None:
        raise ObserverError("launch_session_not_declared")
    validator = profile.get("launch_validator")
    if not isinstance(validator, Mapping) or validator.get("validator_id") != (
        "codex-root-supervisor-session"
    ):
        raise ObserverError("launch_session_runtime_verifier_unavailable")
    try:
        import codex_main_agent_deployment as deployment
        import agent_integration_canary as canary
        import frontdoor_orchestrator as frontdoor

        manifest_text = profile["configuration_requirements"]["deployment_manifest_path"]
        manifest_path = Path(manifest_text)
        deployment.verify_deployment(manifest_path)
        manifest = deployment.load_manifest(manifest_path)
        bindings = manifest["bindings"]
        checkout = frontdoor.resolve_checkout_identity(
            workspace_id=str(bindings["workspace_id"]),
            managed_primary=Path(str(bindings["managed_primary"])),
            checkout_root=Path(str(session["checkout_realpath"])),
        )
        if (
            session["deployment_id"] != manifest["deployment_id"]
            or session["profile_id"] != profile["profile_id"]
            or session["principal_id"] != bindings["principal_id"]
            or session["workspace_id"] != bindings["workspace_id"]
        ):
            raise ObserverError("launch_session_deployment_binding_mismatch")
        record_path = root / reference
        metadata = record_path.lstat()
        if stat.S_IMODE(metadata.st_mode) != 0o644:
            raise ObserverError("launch_session_record_mode_invalid")
        verifier = frontdoor.HostLaunchSessionVerifier(
            session_directory=root / "launch-sessions",
            expected_owner_uid=metadata.st_uid,
            record_mode=0o644,
        )
        verified = verifier.revalidate(
            session,
            checkout_identity=checkout,
            now_epoch=now.timestamp(),
        )
        if dict(verified) != session:
            raise ObserverError("launch_session_runtime_verifier_mismatch")
    except Exception as exc:
        if isinstance(exc, ObserverError):
            raise
        raise ObserverError("launch_session_runtime_verification_failed") from exc
    return session


def _process_executable_realpath(pid: int) -> str:
    """Read the live executable identity without trusting process output."""

    proc_link = Path(f"/proc/{pid}/exe")
    if proc_link.exists():
        try:
            return str(proc_link.resolve(strict=True))
        except OSError as exc:
            raise ObserverError("process_executable_unavailable") from exc
    if sys.platform != "darwin":
        raise ObserverError("process_executable_observer_unsupported")
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = proc_pidpath(pid, buffer, len(buffer))
    except (OSError, AttributeError) as exc:
        raise ObserverError("process_executable_unavailable") from exc
    if length <= 0:
        raise ObserverError("process_executable_unavailable")
    try:
        return str(Path(os.fsdecode(buffer.raw[:length])).resolve(strict=True))
    except OSError as exc:
        raise ObserverError("process_executable_unavailable") from exc


def _commissioning_path(root: Path, profile_id: str, commissioning_id: str) -> Path:
    return root / "commissioning" / profile_id / f"{commissioning_id}.json"


def begin_commissioning(
    state_root: Path | str,
    *,
    profile_id: str,
    launch_session_reference: str | None = None,
    runtime_binding: Mapping[str, Any] | None = None,
    generation_id: str | None = None,
    commissioning_id: str | None = None,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one root-only commissioning generation without requiring a claim."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, profile_id)
    if profile["integration_state"] != "configured":
        raise ObserverError("commissioning_profile_not_configured")
    generation = generation_id or "gen-" + uuid.uuid4().hex[:24]
    commissioning = commissioning_id or "commission-" + uuid.uuid4().hex[:24]
    for value in (generation, commissioning):
        if not assurance.SAFE_ID.fullmatch(value):
            raise ObserverError("commissioning_identity_invalid")
    _prepare_directory(root, ("commissioning", profile_id), policy=policy, mode=0o700)
    generation_root = _prepare_directory(
        root, ("generations", profile_id, generation), policy=policy, mode=0o755
    )
    for child in ("evidence", "observations", "markers", "observer", "canaries"):
        _prepare_directory(generation_root, (child,), policy=policy, mode=0o755)
    _prepare_directory(root, ("active",), policy=policy, mode=0o755)
    session: dict[str, Any] | None = None
    if profile.get("launch_validator") is not None:
        if launch_session_reference is None:
            raise ObserverError("commissioning_launch_session_required")
        session = _load_session(
            root,
            launch_session_reference,
            policy=policy,
            now=current,
            profile=profile,
        )
        if session["profile_id"] != profile_id:
            raise ObserverError("commissioning_launch_session_profile_mismatch")
    elif launch_session_reference is not None:
        raise ObserverError("commissioning_launch_session_unexpected")
    if runtime_binding is None and session is not None:
        try:
            import codex_main_agent_deployment as deployment

            argv = deployment.native_codex_argv(session["native_realpath"])
            runtime_binding = {
                "runtime_realpath": session["native_realpath"],
                "runtime_digest": session["native_digest"],
                "argv": argv,
                "argv_digest": assurance._stable_digest(argv),
                "profile_realpath": session["profile_realpath"],
                "profile_digest": session["profile_digest"],
            }
        except Exception as exc:
            raise ObserverError("commissioning_runtime_binding_unavailable") from exc
    if runtime_binding is None:
        raise ObserverError("commissioning_runtime_binding_required")
    execution_binding: dict[str, Any] | None = None
    probe_argv: list[str]
    if profile["surface_role"] == "bounded_worker":
        try:
            import scoped_worker_executor as scoped_worker

            worker_runtime = scoped_worker.normalize_worker_runtime_binding(runtime_binding)
            scoped_worker.verify_worker_runtime_binding(worker_runtime)
            execution_binding = worker_runtime
            snapshot = {
                "snapshot_version": "1",
                "profile_id": profile_id,
                "generation_id": generation,
                "execution_binding": worker_runtime,
                "execution_binding_digest": assurance._stable_digest(worker_runtime),
            }
            snapshot_path = generation_root / "observer" / "worker-runtime-binding.json"
            _atomic_write(
                snapshot_path,
                snapshot,
                policy=policy,
                replace=False,
                mode=0o644,
            )
            runtime_binding = {
                "runtime_realpath": worker_runtime["runtime_realpath"],
                "runtime_digest": worker_runtime["runtime_digest"],
                "argv": worker_runtime["argv_template"],
                "argv_digest": worker_runtime["argv_digest"],
                "profile_realpath": str(snapshot_path),
                "profile_digest": assurance._sha256_bytes(snapshot_path.read_bytes()),
            }
            probe_argv = list(scoped_worker.commissioning_probe_argv(worker_runtime))
            if (
                assurance._stable_digest(probe_argv)
                != scoped_worker.commissioning_probe_argv_digest(worker_runtime)
            ):
                raise ObserverError("commissioning_worker_probe_binding_invalid")
        except Exception as exc:
            raise ObserverError("commissioning_worker_runtime_binding_invalid") from exc
    else:
        if session is None:
            raise ObserverError("commissioning_launch_session_required")
        try:
            import agent_integration_canary as canary

            probe_argv = canary.frontend_filesystem_probe_argv(
                list(runtime_binding.get("argv", [])),
                str(session["checkout_realpath"]),
            )
        except Exception as exc:
            raise ObserverError("commissioning_frontend_probe_binding_invalid") from exc
    try:
        normalized_runtime = assurance._validate_runtime_binding(runtime_binding)
    except assurance.AssuranceContractError as exc:
        raise ObserverError("commissioning_runtime_binding_invalid") from exc
    if session is not None and (
        normalized_runtime["runtime_realpath"] != session["native_realpath"]
        or normalized_runtime["runtime_digest"] != session["native_digest"]
        or normalized_runtime["profile_realpath"] != session["profile_realpath"]
        or normalized_runtime["profile_digest"] != session["profile_digest"]
        or normalized_runtime["argv_digest"] != session["launch_argv_digest"]
    ):
        raise ObserverError("commissioning_runtime_launch_session_mismatch")
    purpose = (
        "frontend_action_suite"
        if profile["surface_role"] == "main_agent"
        else "managed_worker_surface_launch_probe"
    )
    marker_path = generation_root / "markers" / "commissioning.marker"
    marker_digest = assurance._sha256_bytes(str(marker_path).encode("utf-8"))
    record: dict[str, Any] = {
        "commissioning_version": "1",
        "commissioning_id": commissioning,
        "single_use_nonce": "nonce-" + uuid.uuid4().hex,
        "profile_id": profile_id,
        "generation_id": generation,
        "purpose": purpose,
        "operation": COMMISSIONING_PURPOSES[purpose],
        "launch_session_reference": launch_session_reference,
        "launch_session_digest": (
            session["record_digest"] if session is not None else None
        ),
        "runtime_binding": normalized_runtime,
        "runtime_binding_digest": assurance._stable_digest(normalized_runtime),
        "execution_binding": execution_binding,
        "execution_binding_digest": (
            assurance._stable_digest(execution_binding)
            if execution_binding is not None
            else None
        ),
        "probe_argv_digest": assurance._stable_digest(probe_argv),
        "marker_target_sha256": marker_digest,
        "issued_at": assurance.format_timestamp(current),
        "valid_until": assurance.format_timestamp(
            current + timedelta(seconds=COMMISSIONING_LIFETIME_SECONDS)
        ),
        "state": "pending",
        "claimed_at": None,
        "completed_at": None,
        "consumer_event_digest": None,
        "generation_manifest_digest": None,
        "record_digest": "sha256:" + "0" * 64,
    }
    record["record_digest"] = assurance._stable_digest(_material(record))
    record = _validate_commissioning_shape(record)
    path = _commissioning_path(root, profile_id, commissioning)
    _atomic_write(path, record, policy=policy, replace=False, mode=0o600)
    lock = path.with_suffix(".lock")
    try:
        lock_fd = os.open(
            lock,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(lock_fd, 0o600)
        os.fsync(lock_fd)
        os.close(lock_fd)
    except OSError as exc:
        raise ObserverError("commissioning_lock_create_failed") from exc
    return {
        "decision": "commissioning_pending",
        "commissioning_reference": str(path.relative_to(root)),
        "commissioning_sha256": assurance._sha256_bytes(path.read_bytes()),
        "commissioning_id": commissioning,
        "generation_id": generation,
        "purpose": purpose,
    }


def _load_commissioning(
    root: Path,
    reference: str,
    *,
    policy: assurance.TrustPolicy,
) -> dict[str, Any]:
    if not reference.startswith("commissioning/") or not reference.endswith(".json"):
        raise ObserverError("commissioning_reference_invalid")
    try:
        raw = assurance.secure_read_relative(root, reference, policy=policy)
        return _validate_commissioning_shape(
            assurance._load_json_bytes(raw, label="commissioning")
        )
    except (assurance.EvidenceTrustError, assurance.AssuranceContractError) as exc:
        raise ObserverError("commissioning_untrusted") from exc


def _machine_policy_tool_classification(
    *,
    manifest: Mapping[str, Any],
    session: Mapping[str, Any],
    runtime_binding: Mapping[str, Any],
    policy: assurance.TrustPolicy,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Derive observed tools from installed policy, never registry wishes."""

    try:
        import codex_main_agent_deployment as deployment
        import agent_integration_canary as canary

        artifacts = manifest["artifacts"]
        requirements_path = Path(artifacts["requirements"]["path"])
        profile_path = Path(artifacts["codex_profile"]["path"])
        requirements_raw = canary._read_external_regular(
            requirements_path, max_bytes=policy.max_json_bytes
        )
        profile_raw = canary._read_external_regular(
            profile_path, max_bytes=policy.max_json_bytes
        )
        bundle_root = Path(artifacts["runtime_bundle"]["path"])
        source_root = deployment.WORKFLOW_ROOT.parent.parent.parent
        requirements_template = canary._read_external_regular(
            bundle_root
            / deployment.REQUIREMENTS_TEMPLATE_PATH.relative_to(source_root),
            max_bytes=policy.max_json_bytes,
        ).decode("utf-8")
        profile_template = canary._read_external_regular(
            bundle_root / deployment.PROFILE_TEMPLATE_PATH.relative_to(source_root),
            max_bytes=policy.max_json_bytes,
        ).decode("utf-8")
        instructions_raw = canary._read_external_regular(
            Path(artifacts["instructions"]["path"]),
            max_bytes=policy.max_json_bytes,
        )
        expected_requirements = deployment._render_codex_text(
            requirements_template,
            user_home=Path(manifest["bindings"]["runtime_user_home"]),
            release_commit=str(manifest["release_commit"]),
        ).encode("utf-8")
        expected_profile = deployment._render_profile(
            profile_template,
            user_home=Path(manifest["bindings"]["runtime_user_home"]),
            release_commit=str(manifest["release_commit"]),
            developer_instructions=instructions_raw.decode("utf-8"),
        ).encode("utf-8")
    except Exception as exc:
        raise ObserverError("machine_tool_policy_unreadable") from exc
    policy_templates_exact = (
        requirements_raw == expected_requirements and profile_raw == expected_profile
    )
    enabled_tools = sorted(manifest["tool_contract"]["enabled_tools"])
    forbidden_tools = set(manifest["tool_contract"]["forbidden_tools"])
    launcher_argv = deployment.native_codex_argv(
        str(artifacts["native_codex"]["path"])
    )
    facts = {
        "approval_policy_never": policy_templates_exact,
        "sandbox_read_only": policy_templates_exact,
        "web_search_disabled": policy_templates_exact,
        "external_features_disabled": policy_templates_exact,
        "bridge_tools_exact": (
            policy_templates_exact
            and enabled_tools == sorted(deployment.ENABLED_TOOLS)
            and deployment.REQUIRED_FORBIDDEN_TOOLS.issubset(forbidden_tools)
        ),
        "plugins_disabled": policy_templates_exact,
        "multi_agent_disabled": policy_templates_exact,
        "notify_empty": policy_templates_exact,
        "live_session_bound": (
            session.get("status") == "active"
            and session.get("session_kind") == "standard"
            and session.get("native_realpath") == runtime_binding["runtime_realpath"]
            and session.get("native_digest") == runtime_binding["runtime_digest"]
            and session.get("profile_realpath") == runtime_binding["profile_realpath"]
            and session.get("profile_digest") == runtime_binding["profile_digest"]
            and session.get("launch_argv_digest")
            == assurance._stable_digest(launcher_argv)
        ),
        "known_codex_auth_paths_denied": policy_templates_exact
        and _known_codex_auth_paths_denied(
            requirements_raw,
            runtime_user_home=Path(manifest["bindings"]["runtime_user_home"]),
            codex_home=Path(manifest["bindings"]["codex_home"]),
        ),
    }
    if not all(facts.values()):
        failed = sorted(name for name, passed in facts.items() if not passed)
        raise ObserverError("machine_tool_policy_not_enforced:" + ",".join(failed))
    classification = {
        "inventory_version": "1",
        "model_identifier": str(artifacts["native_codex"].get("version") or "pinned-codex"),
        "server_backed_mcp_tools": enabled_tools,
        "reviewed_internal_tools": [],
        "active_external_mutation_tools": [],
        "mechanically_denied_tools": ["apply_patch"],
        "dynamic_tools": [],
        "extension_tools": [],
        "multi_agent_tools": [],
    }
    inventory = sorted(
        item
        for category in assurance.TOOL_CATEGORY_NAMES
        for item in classification[category]
    )
    observation = {
        "observation_version": "1",
        "observation_kind": "live_session_machine_policy",
        "session_id": session["session_id"],
        "launch_session_digest": session["record_digest"],
        "deployment_manifest_digest": assurance._hash_external_regular(
            str(Path(manifest["bindings"]["deployment_manifest_path"])), policy=policy
        ),
        "requirements_digest": assurance._sha256_bytes(requirements_raw),
        "profile_digest": assurance._sha256_bytes(profile_raw),
        "fixed_argv_digest": assurance._stable_digest(launcher_argv),
        "policy_facts": facts,
        "policy_facts_digest": assurance._stable_digest(facts),
        "inventory_digest": assurance._stable_digest(inventory),
        "classification_digest": assurance._stable_digest(classification),
    }
    return inventory, classification, observation


def observe_frontend_common_identity(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    observer_binary: Path | None = None,
) -> dict[str, Any]:
    """Derive and record frontend common evidence without caller observations."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    pending = _load_commissioning(root, commissioning_reference, policy=policy)
    if pending["purpose"] != "frontend_action_suite":
        raise ObserverError("frontend_commissioning_grant_required")
    claimed = claim_commissioning_grant(
        root,
        commissioning_reference,
        expected_profile_id=pending["profile_id"],
        expected_generation_id=pending["generation_id"],
        expected_purpose=pending["purpose"],
        expected_runtime_binding_digest=pending["runtime_binding_digest"],
        expected_marker_target_sha256=pending["marker_target_sha256"],
        trust_policy=policy,
        now=current,
    )
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, claimed["profile_id"])
    if claimed["launch_session_reference"] is None:
        raise ObserverError("frontend_launch_session_missing")
    session = _load_session(
        root,
        claimed["launch_session_reference"],
        policy=policy,
        now=current,
        profile=profile,
    )
    if session["record_digest"] != claimed["launch_session_digest"]:
        raise ObserverError("commissioning_launch_session_drift")
    checkout = Path(session["checkout_realpath"]).resolve(strict=True)
    if _process_executable_realpath(session["subject_pid"]) != session["native_realpath"]:
        raise ObserverError("frontend_live_runtime_mismatch")
    runtime_binding = assurance._validate_runtime_binding(claimed["runtime_binding"])
    requirements = profile["configuration_requirements"]
    manifest_text = requirements.get("deployment_manifest_path")
    if not isinstance(manifest_text, str):
        raise ObserverError("frontend_deployment_manifest_missing")
    try:
        import agent_integration_canary as canary
        import codex_main_agent_deployment as deployment
        import frontdoor_orchestrator as frontdoor

        manifest_path = Path(manifest_text)
        deployment.verify_deployment(manifest_path)
        manifest = deployment.load_manifest(manifest_path)
        manifest_raw = canary._read_external_regular(
            manifest_path, max_bytes=policy.max_json_bytes
        )
    except Exception as exc:
        raise ObserverError("frontend_deployment_verification_failed") from exc
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ObserverError("frontend_deployment_artifacts_invalid")
    configuration_artifacts = [
        {
            "role": "deployment_manifest",
            "path": str(manifest_path),
            "sha256": assurance._sha256_bytes(manifest_raw),
        }
    ]
    for role, key in (
        ("runtime_config", "runtime_config"),
        ("requirements", "requirements"),
        ("bridge_wrapper", "bridge_wrapper"),
        ("instructions", "instructions"),
        ("observer", "observer"),
        ("supervisor", "supervisor"),
    ):
        artifact = artifacts.get(key)
        if not isinstance(artifact, Mapping):
            raise ObserverError("frontend_deployment_artifact_missing")
        digest = assurance._hash_external_regular(artifact.get("path"), policy=policy)
        if digest != artifact.get("sha256"):
            raise ObserverError("frontend_deployment_artifact_drift")
        configuration_artifacts.append(
            {"role": role, "path": artifact["path"], "sha256": digest}
        )
    configuration_artifacts.append(
        {
            "role": "profile_snapshot",
            "path": runtime_binding["profile_realpath"],
            "sha256": runtime_binding["profile_digest"],
        }
    )
    configuration_artifacts.sort(key=lambda item: (item["role"], item["path"]))
    try:
        checkout_identity = frontdoor.resolve_checkout_identity(
            workspace_id=session["workspace_id"],
            managed_primary=Path(manifest["bindings"]["managed_primary"]),
            checkout_root=checkout,
        )
    except Exception as exc:
        raise ObserverError("frontend_checkout_identity_invalid") from exc
    if (
        checkout_identity["identity_digest"] != session["checkout_identity_digest"]
        or checkout_identity["checkout_realpath"] != session["checkout_realpath"]
    ):
        raise ObserverError("frontend_checkout_launch_session_mismatch")
    inventory, classification, tool_observation = (
        _machine_policy_tool_classification(
            manifest=manifest,
            session=session,
            runtime_binding=runtime_binding,
            policy=policy,
        )
    )
    try:
        assurance._validate_tool_classification(
            classification, inventory=inventory, profile=profile, manifest=manifest
        )
    except assurance.AssuranceGateError as exc:
        raise ObserverError(exc.reason) from exc
    bindings = {
        "configuration_digest": assurance._stable_digest(configuration_artifacts),
        "runtime_binary_digest": runtime_binding["runtime_digest"],
        "tool_inventory_digest": assurance._stable_digest(inventory),
        "checkout_digest": checkout_identity["identity_digest"],
    }
    observer_path = (observer_binary or Path(__file__)).resolve(strict=True)
    observer_digest = assurance._hash_external_regular(str(observer_path), policy=policy)
    observer_artifact = artifacts.get("observer")
    if (
        not isinstance(observer_artifact, Mapping)
        or observer_artifact.get("path") != str(observer_path)
        or observer_artifact.get("sha256") != observer_digest
    ):
        raise ObserverError("frontend_observer_deployment_mismatch")
    assurance._validate_observer_executable(
        str(observer_path), observer_digest, policy=policy, label="commissioning_observer"
    )
    launcher = artifacts.get("codex_launcher")
    if not isinstance(launcher, Mapping):
        raise ObserverError("frontend_launcher_artifact_missing")
    receipt_id = "common-" + claimed["commissioning_id"]
    event = {
        "event_id": "identity-" + claimed["commissioning_id"],
        "event_kind": "effective_agent_identity",
        "subject_pid": session["subject_pid"],
        "process_start_token": session["process_start_token"],
        "runtime_binary_realpath": runtime_binding["runtime_realpath"],
        "runtime_binary_digest": runtime_binding["runtime_digest"],
        "runtime_argv": runtime_binding["argv"],
        "runtime_argv_digest": runtime_binding["argv_digest"],
        "profile_snapshot_path": runtime_binding["profile_realpath"],
        "profile_snapshot_digest": runtime_binding["profile_digest"],
        "tool_inventory": inventory,
        "tool_inventory_classification": classification,
        "tool_inventory_observation": tool_observation,
        "launcher_realpath": launcher["path"],
        "launcher_digest": launcher["sha256"],
        "launcher_process_start_token": session["process_start_token"],
        "host_verified_launch_token": session["session_id"],
        "launch_argv_digest": runtime_binding["argv_digest"],
        "effective_notify_empty": True,
        **bindings,
        "launch_session": session,
        "worker_execution_binding": claimed.get("execution_binding"),
    }
    receipt = {
        "receipt_version": "1",
        "receipt_id": receipt_id,
        "profile_id": profile["profile_id"],
        "observed_at": assurance.format_timestamp(current),
        "observer": {
            "kind": "sandbox_policy_audit",
            "binary_realpath": str(observer_path),
            "binary_sha256": observer_digest,
        },
        "event": event,
    }
    observer_dir = root / "generations" / profile["profile_id"] / claimed["generation_id"] / "observer"
    path = observer_dir / f"{receipt_id}.json"
    _atomic_write(path, receipt, policy=policy, replace=False, mode=0o644)
    reference = {
        "path": str(path.relative_to(root)),
        "sha256": assurance._sha256_bytes(path.read_bytes()),
    }
    common = canary.record_common_evidence(
        root,
        profile_id=profile["profile_id"],
        generation_id=claimed["generation_id"],
        observer_receipt_reference=reference,
        expected_checkout=checkout,
        registry=selected_registry,
        trust_policy=policy,
        now=current,
    )
    return {
        "decision": "common_identity_observed",
        "commissioning": _public_commissioning_projection(claimed),
        "receipt_reference": reference,
        "common_evidence": common["evidence"],
    }


def observe_mechanically_absent_frontend_actions(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    common_evidence_references: Sequence[str],
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    observer_binary: Path | None = None,
) -> dict[str, Any]:
    """Record only action interfaces proven absent by the pinned validator."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    grant = _load_commissioning(root, commissioning_reference, policy=policy)
    if grant["state"] != "claimed" or grant["purpose"] != "frontend_action_suite":
        raise ObserverError("claimed_frontend_commissioning_required")
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, grant["profile_id"])
    if grant["launch_session_reference"] is None:
        raise ObserverError("frontend_launch_session_missing")
    session = _load_session(
        root,
        grant["launch_session_reference"],
        policy=policy,
        now=current,
        profile=profile,
    )
    checkout = Path(session["checkout_realpath"]).resolve(strict=True)
    try:
        import agent_integration_canary as canary
    except ImportError as exc:
        raise ObserverError("canary_module_unavailable") from exc
    observer_path = (observer_binary or Path(__file__)).resolve(strict=True)
    observer_digest = assurance._hash_external_regular(str(observer_path), policy=policy)
    assurance._validate_observer_executable(
        str(observer_path), observer_digest, policy=policy, label="commissioning_observer"
    )
    marker_target = (
        root
        / "generations"
        / profile["profile_id"]
        / grant["generation_id"]
        / "markers"
        / "commissioning.marker"
    )
    if assurance._sha256_bytes(str(marker_target).encode("utf-8")) != grant["marker_target_sha256"]:
        raise ObserverError("commissioning_marker_binding_mismatch")
    recorded: list[dict[str, Any]] = []
    inconclusive: list[str] = []
    for index, operation in enumerate(assurance.ACTION_OPERATIONS):
        begin = canary.begin_action_canary(
            root,
            profile_id=profile["profile_id"],
            generation_id=grant["generation_id"],
            evidence_type="direct_action_denial",
            operation=operation,
            marker_path=marker_target,
            common_evidence_references=common_evidence_references,
            expected_checkout=checkout,
            registry=selected_registry,
            trust_policy=policy,
            now=current,
            challenge_id=f"absent-{index:02d}-{operation}",
        )
        challenge_raw = assurance.secure_read_relative(
            root, begin["challenge_reference"], policy=policy
        )
        challenge = assurance._load_json_bytes(challenge_raw, label="action_challenge")
        if not canary._mechanical_absence_for_challenge(
            root, challenge, profile, policy=policy
        ):
            inconclusive.append(operation)
            continue
        common_process = challenge["common_process_binding"]
        event = {
            "event_id": f"absence-{index:02d}-{operation}",
            "event_kind": "mechanical_absence_proof",
            "operation": operation,
            "decision": "denied",
            "observation_basis": "mechanically_absent",
            "structured_event_digest": None,
            "probe_process_binding": None,
            "subject_pid": common_process["subject_pid"],
            "process_start_token": common_process["process_start_token"],
            **challenge["bindings"],
            "capability_verification": None,
            "execution_observation": None,
        }
        receipt = {
            "receipt_version": "1",
            "receipt_id": f"receipt-{index:02d}-{operation}",
            "challenge_id": challenge["challenge_id"],
            "challenge_sha256": assurance._sha256_bytes(challenge_raw),
            "profile_id": profile["profile_id"],
            "evidence_type": "direct_action_denial",
            "claim": "action_enforced",
            "operation": operation,
            "observed_at": assurance.format_timestamp(current),
            "observer": {
                "kind": "sandbox_policy_audit",
                "binary_realpath": str(observer_path),
                "binary_sha256": observer_digest,
            },
            "event": event,
            "effective_bindings": challenge["bindings"],
            "state_linkage": None,
        }
        receipt_path = root / challenge["expected_observer_receipt"]
        _atomic_write(receipt_path, receipt, policy=policy, replace=False, mode=0o644)
        result = canary.finish_action_canary(
            root,
            challenge_reference=begin["challenge_reference"],
            observer_receipt_reference={
                "path": str(receipt_path.relative_to(root)),
                "sha256": assurance._sha256_bytes(receipt_path.read_bytes()),
            },
            expected_checkout=checkout,
            registry=selected_registry,
            trust_policy=policy,
            now=current,
        )
        recorded.append(result)
    return {
        "decision": "mechanical_absence_observed",
        "evidence": recorded,
        "inconclusive_operations": inconclusive,
    }


def _parse_failed_file_change(
    raw: bytes, *, checkout: Path
) -> tuple[dict[str, Any], str]:
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ObserverError("frontend_probe_event_stream_invalid")
    events: list[dict[str, Any]] = []
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ObserverError("frontend_probe_event_stream_invalid")
            events.append(value)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ObserverError("frontend_probe_event_stream_invalid") from exc
    completed_items = [
        event
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), Mapping)
    ]
    action_items = [
        event
        for event in completed_items
        if event["item"].get("type")
        in {
            "file_change",
            "command_execution",
            "mcp_tool_call",
            "collab_tool_call",
            "web_search",
        }
    ]
    matches = [
        event
        for event in action_items
        if event["item"].get("type") == "file_change"
        and event["item"].get("status") == "failed"
    ]
    if len(matches) != 1 or action_items != matches:
        raise ObserverError("frontend_filesystem_probe_inconclusive")
    item = matches[0]["item"]
    changes = item.get("changes")
    target_relative = ".saihai-frontend-commissioning-probe"
    allowed_paths = {
        target_relative,
        str(checkout / target_relative),
    }
    if (
        not isinstance(changes, list)
        or len(changes) != 1
        or not isinstance(changes[0], Mapping)
        or changes[0].get("path") not in allowed_paths
        or changes[0].get("kind") != "add"
    ):
        raise ObserverError("frontend_filesystem_probe_target_mismatch")
    return matches[0], assurance._stable_digest(matches[0])


def _run_fixed_frontend_probe(
    *,
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
    probe_id: str,
    expected_argv: Sequence[str],
    expected_checkout: Path,
    runner: Callable[..., Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use only the root supervisor's fixed commissioning plan.

    Neither this function nor its public CLI accepts argv, prompt, tool,
    executable, state root, or checkout inputs.  ``expected_argv`` and
    ``expected_checkout`` are independently derived from the already-bound
    commissioning record and standard launch session, then compared with the
    supervisor result.
    """

    try:
        import codex_main_agent_supervisor as supervisor

        selected_runner = runner or supervisor.run_commissioning_probe
        nonce_digest = assurance._stable_digest(
            {
                "commissioning_id": record["commissioning_id"],
                "single_use_nonce": record["single_use_nonce"],
            }
        )
        result = dict(
            selected_runner(
                commissioning_id=record["commissioning_id"],
                generation_id=record["generation_id"],
                probe_id=probe_id,
                nonce_digest=nonce_digest,
                deployment_manifest_path=Path(
                    profile["configuration_requirements"][
                        "deployment_manifest_path"
                    ]
                ),
            )
        )
        session = supervisor.validate_session_record_shape(result.get("session"))
        companion = supervisor.validate_commissioning_launch_shape(
            result.get("companion")
        )
    except ObserverError:
        raise
    except Exception as exc:
        raise ObserverError("frontend_probe_supervision_failed") from exc
    expected_result_fields = {
        "session",
        "companion",
        "argv",
        "pid",
        "process_start_token",
        "exit_code",
        "stdout_bytes",
        "stderr_bytes",
        "state_root",
        "checkout_realpath",
    }
    if (
        set(result) != expected_result_fields
        or result.get("argv") != list(expected_argv)
        or result.get("pid") != session["subject_pid"]
        or result.get("process_start_token") != session["process_start_token"]
        or result.get("checkout_realpath") != str(expected_checkout)
        or result.get("exit_code") != 0
        or not isinstance(result.get("stdout_bytes"), bytes)
        or not isinstance(result.get("stderr_bytes"), bytes)
        or session["session_kind"] != "commissioning"
        or session["record_reference"]
        != f"launch-sessions/{session['session_id']}.json"
        or session["launch_argv_digest"] != assurance._stable_digest(list(expected_argv))
        or companion["state"] != "active"
        or companion["session_id"] != session["session_id"]
        or companion["commissioning_id"] != record["commissioning_id"]
        or companion["generation_id"] != record["generation_id"]
        or companion["profile_id"] != profile["profile_id"]
        or companion["probe_id"] != probe_id
        or companion["nonce_digest"] != nonce_digest
        or companion["probe_argv_digest"] != assurance._stable_digest(
            list(expected_argv)
        )
        or companion["launch_session_digest"] != session["record_digest"]
        or session["commissioning_launch_reference"]
        != companion["record_reference"]
        or session["commissioning_launch_digest"] != companion["binding_digest"]
        or companion.get("live_observation") is None
        or companion.get("live_observation_digest")
        != assurance._stable_digest(companion["live_observation"])
    ):
        raise ObserverError("frontend_probe_supervisor_binding_mismatch")
    probe_binding = {
        "commissioning_id": record["commissioning_id"],
        "nonce_digest": nonce_digest,
        "launch_session_reference": session["record_reference"],
        "launch_session_digest": session["record_digest"],
        "commissioning_launch_reference": companion["record_reference"],
        "commissioning_binding_digest": companion["binding_digest"],
        "commissioning_live_observation_digest": companion[
            "live_observation_digest"
        ],
    }
    return result, probe_binding


def _finalize_fixed_frontend_probe(
    root: Path,
    result: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
    final_state: str,
) -> None:
    try:
        import codex_main_agent_supervisor as supervisor

        companion = result["companion"]
        supervisor.finalize_commissioning_launch(
            root,
            companion_reference=companion["record_reference"],
            expected_binding_digest=companion["binding_digest"],
            final_state=final_state,
            policy=policy,
        )
    except Exception as exc:
        raise ObserverError("frontend_probe_finalization_failed") from exc


def observe_frontend_filesystem_denial(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    common_evidence_references: Sequence[str],
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    observer_binary: Path | None = None,
    supervisor_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the one fixed stock-Codex JSONL probe and require failed file_change."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    record = _load_commissioning(root, commissioning_reference, policy=policy)
    if record["state"] != "claimed" or record["purpose"] != "frontend_action_suite":
        raise ObserverError("claimed_frontend_commissioning_required")
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, record["profile_id"])
    if record["launch_session_reference"] is None:
        raise ObserverError("frontend_launch_session_missing")
    session = _load_session(
        root,
        record["launch_session_reference"],
        policy=policy,
        now=current,
        profile=profile,
    )
    checkout = Path(session["checkout_realpath"]).resolve(strict=True)
    target = checkout / ".saihai-frontend-commissioning-probe"
    if target.exists() or target.is_symlink():
        raise ObserverError("frontend_probe_target_not_clean")
    probe_result: dict[str, Any] | None = None
    try:
        import agent_integration_canary as canary
        import codex_main_agent_deployment as deployment
        manifest_path = Path(
            profile["configuration_requirements"]["deployment_manifest_path"]
        )
        deployment.verify_deployment(manifest_path)
        manifest = deployment.load_manifest(manifest_path)
        runtime_binding = assurance._validate_runtime_binding(record["runtime_binding"])
        command = canary.frontend_filesystem_probe_argv(
            runtime_binding["argv"], str(checkout)
        )
        if assurance._stable_digest(command) != record["probe_argv_digest"]:
            raise ObserverError("frontend_probe_argv_binding_mismatch")
        begin = canary.begin_action_canary(
            root,
            profile_id=profile["profile_id"],
            generation_id=record["generation_id"],
            evidence_type="direct_action_denial",
            operation="filesystem_write",
            marker_path=target,
            common_evidence_references=common_evidence_references,
            expected_checkout=checkout,
            registry=selected_registry,
            trust_policy=policy,
            now=current,
            challenge_id="structured-filesystem-write",
        )
        probe_result, commissioning_binding = _run_fixed_frontend_probe(
            record=record,
            profile=profile,
            probe_id="frontend_filesystem_denial",
            expected_argv=command,
            expected_checkout=checkout,
            runner=supervisor_runner,
        )
        probe_pid = probe_result["pid"]
        process_start = probe_result["process_start_token"]
        stdout = probe_result["stdout_bytes"]
        stderr = probe_result["stderr_bytes"]
        raw_path = (
            root
            / "commissioning"
            / profile["profile_id"]
            / f"{record['commissioning_id']}-filesystem.jsonl"
        )
        _atomic_write_bytes(raw_path, stdout, policy=policy, mode=0o600)
        structured_event, structured_digest = _parse_failed_file_change(
            stdout, checkout=checkout
        )
        if target.exists() or target.is_symlink():
            raise ObserverError("frontend_filesystem_probe_side_effect_observed")
        challenge_raw = assurance.secure_read_relative(
            root, begin["challenge_reference"], policy=policy
        )
        challenge = assurance._load_json_bytes(
            challenge_raw, label="action_challenge"
        )
        observer_path = (observer_binary or Path(__file__)).resolve(strict=True)
        observer_digest = assurance._hash_external_regular(
            str(observer_path), policy=policy
        )
        receipt = {
            "receipt_version": "1",
            "receipt_id": "structured-filesystem-write",
            "challenge_id": challenge["challenge_id"],
            "challenge_sha256": assurance._sha256_bytes(challenge_raw),
            "profile_id": profile["profile_id"],
            "evidence_type": "direct_action_denial",
            "claim": "action_enforced",
            "operation": "filesystem_write",
            "observed_at": assurance.format_timestamp(_now(None)),
            "observer": {
                "kind": "sandbox_policy_audit",
                "binary_realpath": str(observer_path),
                "binary_sha256": observer_digest,
            },
            "event": {
                "event_id": "structured-filesystem-write",
                "event_kind": "operation_attempt",
                "operation": "filesystem_write",
                "decision": "denied",
                "observation_basis": "structured_attempt",
                "structured_event_digest": structured_digest,
                "probe_process_binding": {
                    "subject_pid": probe_pid,
                    "process_start_token": process_start,
                    "runtime_realpath": runtime_binding["runtime_realpath"],
                    "runtime_digest": runtime_binding["runtime_digest"],
                    "argv": command,
                    "argv_digest": assurance._stable_digest(command),
                    "profile_realpath": runtime_binding["profile_realpath"],
                    "profile_digest": runtime_binding["profile_digest"],
                    **commissioning_binding,
                },
                "subject_pid": probe_pid,
                "process_start_token": process_start,
                **challenge["bindings"],
                "capability_verification": None,
                "execution_observation": None,
            },
            "effective_bindings": challenge["bindings"],
            "state_linkage": None,
        }
        receipt_path = root / challenge["expected_observer_receipt"]
        _atomic_write(receipt_path, receipt, policy=policy, replace=False, mode=0o644)
        result = canary.finish_action_canary(
            root,
            challenge_reference=begin["challenge_reference"],
            observer_receipt_reference={
                "path": str(receipt_path.relative_to(root)),
                "sha256": assurance._sha256_bytes(receipt_path.read_bytes()),
            },
            expected_checkout=checkout,
            registry=selected_registry,
            trust_policy=policy,
            now=_now(None),
        )
        _finalize_fixed_frontend_probe(
            root, probe_result, policy=policy, final_state="consumed"
        )
    except ObserverError:
        if probe_result is not None:
            _finalize_fixed_frontend_probe(
                root, probe_result, policy=policy, final_state="failed"
            )
        raise
    except Exception as exc:
        if probe_result is not None:
            _finalize_fixed_frontend_probe(
                root, probe_result, policy=policy, final_state="failed"
            )
        raise ObserverError("frontend_filesystem_probe_failed") from exc
    return {
        "decision": "structured_filesystem_denial_observed",
        "structured_event_digest": assurance._stable_digest(structured_event),
        "raw_event_reference": str(raw_path.relative_to(root)),
        "commissioning_launch_reference": probe_result["companion"][
            "record_reference"
        ],
        "evidence": result,
    }


def _parse_completed_submit_request(
    raw: bytes,
    *,
    expected_server: str,
) -> tuple[dict[str, Any], str, str, str]:
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ObserverError("frontend_probe_event_stream_invalid")
    events: list[dict[str, Any]] = []
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ObserverError("frontend_probe_event_stream_invalid")
            events.append(value)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ObserverError("frontend_probe_event_stream_invalid") from exc
    action_items = [
        event
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), Mapping)
        and event["item"].get("type")
        in {
            "file_change",
            "command_execution",
            "mcp_tool_call",
            "collab_tool_call",
            "web_search",
        }
    ]
    matches = [
        event
        for event in action_items
        if event["item"].get("type") == "mcp_tool_call"
        and event["item"].get("server") == expected_server
        and event["item"].get("tool") == "submit_request"
        and event["item"].get("status") == "completed"
        and event["item"].get("error") is None
    ]
    if len(matches) != 1 or action_items != matches:
        raise ObserverError("frontend_gateway_probe_inconclusive")
    arguments = matches[0]["item"].get("arguments")
    expected_fields = {
        "workspace",
        "task_id",
        "request_id",
        "prompt",
        "refs",
        "allowed_paths",
        "chat_session_id",
        "idempotency_key",
    }
    try:
        import agent_integration_canary as canary
    except ImportError as exc:
        raise ObserverError("canary_module_unavailable") from exc
    if (
        not isinstance(arguments, Mapping)
        or set(arguments) != expected_fields
        or arguments.get("workspace") != "Saber5656/Saihai"
        or arguments.get("prompt") != canary.ROUTING_ACCEPTANCE_PROMPT
        or arguments.get("refs") != ["README.md", "CHANGELOG.md"]
        or arguments.get("allowed_paths") != []
        or not isinstance(arguments.get("request_id"), str)
        or not assurance.SAFE_ID.fullmatch(arguments["request_id"])
        or not isinstance(arguments.get("task_id"), str)
        or not assurance.SAFE_ID.fullmatch(arguments["task_id"])
        or not isinstance(arguments.get("chat_session_id"), str)
        or not arguments["chat_session_id"]
        or not isinstance(arguments.get("idempotency_key"), str)
        or not arguments["idempotency_key"]
    ):
        raise ObserverError("frontend_gateway_probe_arguments_invalid")
    return (
        matches[0],
        assurance._stable_digest(matches[0]),
        arguments["request_id"],
        arguments["idempotency_key"],
    )


def observe_frontend_gateway_routing(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    common_evidence_references: Sequence[str],
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    observer_binary: Path | None = None,
    supervisor_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Require one structured submit_request and stop exactly at waiting_human."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    record = _load_commissioning(root, commissioning_reference, policy=policy)
    if record["state"] != "claimed" or record["purpose"] != "frontend_action_suite":
        raise ObserverError("claimed_frontend_commissioning_required")
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, record["profile_id"])
    if record["launch_session_reference"] is None:
        raise ObserverError("frontend_launch_session_missing")
    session = _load_session(
        root,
        record["launch_session_reference"],
        policy=policy,
        now=current,
        profile=profile,
    )
    checkout = Path(session["checkout_realpath"]).resolve(strict=True)
    target = checkout / ".saihai-frontend-gateway-commissioning-probe"
    if target.exists() or target.is_symlink():
        raise ObserverError("frontend_probe_target_not_clean")
    probe_result: dict[str, Any] | None = None
    try:
        import agent_integration_canary as canary
        import codex_main_agent_deployment as deployment

        manifest_path = Path(
            profile["configuration_requirements"]["deployment_manifest_path"]
        )
        deployment.verify_deployment(manifest_path)
        manifest = deployment.load_manifest(manifest_path)
        runtime_binding = assurance._validate_runtime_binding(record["runtime_binding"])
        command = canary.frontend_gateway_probe_argv(
            runtime_binding["argv"], str(checkout)
        )
        action_begin = canary.begin_action_canary(
            root,
            profile_id=profile["profile_id"],
            generation_id=record["generation_id"],
            evidence_type="gateway_positive_path",
            operation=None,
            marker_path=target,
            common_evidence_references=common_evidence_references,
            expected_checkout=checkout,
            registry=selected_registry,
            trust_policy=policy,
            now=current,
            challenge_id="structured-gateway-submit",
        )
        routing_root = (
            root
            / "commissioning"
            / profile["profile_id"]
            / f"{record['commissioning_id']}-routing"
        )
        routing_begin = canary.begin_routing_acceptance(
            routing_root,
            state_root=Path(manifest["bindings"]["state_root"]),
            profile_id=profile["profile_id"],
            workspace_id=str(manifest["bindings"]["workspace_id"]),
            managed_primary=Path(manifest["bindings"]["managed_primary"]),
            checkout_root=checkout,
            marker_path=target,
            now=current,
            challenge_id="structured-gateway-submit",
        )
        probe_result, commissioning_binding = _run_fixed_frontend_probe(
            record=record,
            profile=profile,
            probe_id="frontend_gateway_routing",
            expected_argv=command,
            expected_checkout=checkout,
            runner=supervisor_runner,
        )
        probe_pid = probe_result["pid"]
        process_start = probe_result["process_start_token"]
        stdout = probe_result["stdout_bytes"]
        raw_path = (
            root
            / "commissioning"
            / profile["profile_id"]
            / f"{record['commissioning_id']}-gateway.jsonl"
        )
        _atomic_write_bytes(raw_path, stdout, policy=policy, mode=0o600)
        structured_event, structured_digest, request_id, idempotency_key = (
            _parse_completed_submit_request(
                stdout,
                expected_server=str(manifest["tool_contract"]["server_name"]),
            )
        )
        if target.exists() or target.is_symlink():
            raise ObserverError("frontend_gateway_probe_side_effect_observed")
        challenge_raw = assurance.secure_read_relative(
            root, action_begin["challenge_reference"], policy=policy
        )
        challenge = assurance._load_json_bytes(
            challenge_raw, label="action_challenge"
        )
        observer_path = (observer_binary or Path(__file__)).resolve(strict=True)
        observer_digest = assurance._hash_external_regular(
            str(observer_path), policy=policy
        )
        receipt = {
            "receipt_version": "1",
            "receipt_id": "structured-gateway-submit",
            "challenge_id": challenge["challenge_id"],
            "challenge_sha256": assurance._sha256_bytes(challenge_raw),
            "profile_id": profile["profile_id"],
            "evidence_type": "gateway_positive_path",
            "claim": "action_enforced",
            "operation": None,
            "observed_at": assurance.format_timestamp(_now(None)),
            "observer": {
                "kind": "host_gateway_executor",
                "binary_realpath": str(observer_path),
                "binary_sha256": observer_digest,
            },
            "event": {
                "event_id": "structured-gateway-submit",
                "event_kind": "operation_attempt",
                "operation": None,
                "decision": "routed_to_saihai",
                "observation_basis": "structured_attempt",
                "structured_event_digest": structured_digest,
                "probe_process_binding": {
                    "subject_pid": probe_pid,
                    "process_start_token": process_start,
                    "runtime_realpath": runtime_binding["runtime_realpath"],
                    "runtime_digest": runtime_binding["runtime_digest"],
                    "argv": command,
                    "argv_digest": assurance._stable_digest(command),
                    "profile_realpath": runtime_binding["profile_realpath"],
                    "profile_digest": runtime_binding["profile_digest"],
                    **commissioning_binding,
                },
                "subject_pid": probe_pid,
                "process_start_token": process_start,
                **challenge["bindings"],
                "capability_verification": None,
                "execution_observation": None,
            },
            "effective_bindings": challenge["bindings"],
            "state_linkage": _gateway_state_linkage(
                state_root=Path(manifest["bindings"]["state_root"]),
                frontend_profile_id=profile["profile_id"],
                routing_begin=routing_begin,
                request_id=request_id,
                idempotency_key=idempotency_key,
            ),
        }
        receipt_path = root / challenge["expected_observer_receipt"]
        _atomic_write(receipt_path, receipt, policy=policy, replace=False, mode=0o644)
        result = canary.finish_action_canary(
            root,
            challenge_reference=action_begin["challenge_reference"],
            observer_receipt_reference={
                "path": str(receipt_path.relative_to(root)),
                "sha256": assurance._sha256_bytes(receipt_path.read_bytes()),
            },
            expected_checkout=checkout,
            registry=selected_registry,
            trust_policy=policy,
            now=_now(None),
        )
        _finalize_fixed_frontend_probe(
            root, probe_result, policy=policy, final_state="consumed"
        )
    except ObserverError:
        if probe_result is not None:
            _finalize_fixed_frontend_probe(
                root, probe_result, policy=policy, final_state="failed"
            )
        raise
    except Exception as exc:
        if probe_result is not None:
            _finalize_fixed_frontend_probe(
                root, probe_result, policy=policy, final_state="failed"
            )
        raise ObserverError("frontend_gateway_probe_failed") from exc
    return {
        "decision": "gateway_waiting_human_observed",
        "structured_event_digest": assurance._stable_digest(structured_event),
        "request_id": request_id,
        "raw_event_reference": str(raw_path.relative_to(root)),
        "commissioning_launch_reference": probe_result["companion"][
            "record_reference"
        ],
        "evidence": result,
    }


def _commissioning_checkout(
    root: Path,
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
    now: datetime,
) -> tuple[Path | None, dict[str, Any] | None]:
    reference = record.get("launch_session_reference")
    if reference is None:
        if profile.get("launch_validator") is not None:
            raise ObserverError("commissioning_launch_session_missing")
        return None, None
    session = _load_session(
        root, str(reference), policy=policy, now=now, profile=profile
    )
    if session.get("record_digest") != record.get("launch_session_digest"):
        raise ObserverError("commissioning_launch_session_drift")
    try:
        checkout = Path(str(session["checkout_realpath"]))
        if checkout != checkout.resolve(strict=True):
            raise ObserverError("commissioning_checkout_not_canonical")
    except OSError as exc:
        raise ObserverError("commissioning_checkout_unavailable") from exc
    return checkout, session


def _collect_exact_generation_evidence(
    root: Path,
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
    now: datetime,
) -> dict[str, Any]:
    """Reopen and verify the exact evidence suite for one commissioning grant."""

    evidence_dir = (
        root
        / "generations"
        / str(record["profile_id"])
        / str(record["generation_id"])
        / "evidence"
    )
    assurance._validate_root_chain(evidence_dir, policy)
    try:
        names = sorted(item.name for item in evidence_dir.iterdir())
    except OSError as exc:
        raise ObserverError("commissioning_evidence_directory_unavailable") from exc
    if not names or any(
        not assurance.SAFE_PATH_COMPONENT.fullmatch(name) or not name.endswith(".json")
        for name in names
    ):
        raise ObserverError("commissioning_evidence_set_invalid")
    prefix = (
        f"generations/{record['profile_id']}/{record['generation_id']}/evidence/"
    )
    required = assurance._required_evidence_keys(profile)
    seen: dict[tuple[str, str, str | None], str] = {}
    references: list[dict[str, Any]] = []
    expected_bindings: dict[str, str] | None = None
    expected_launch: dict[str, Any] | None = None
    expected_worker: dict[str, Any] | None = None
    checkout, commissioning_session = _commissioning_checkout(
        root, record, profile, policy=policy, now=now
    )
    for name in names:
        reference = prefix + name
        try:
            raw = assurance.secure_read_relative(root, reference, policy=policy)
            value = assurance._load_json_bytes(raw, label="commissioning_evidence")
        except (assurance.EvidenceTrustError, assurance.AssuranceContractError) as exc:
            raise ObserverError("commissioning_evidence_untrusted") from exc
        key = (
            value.get("evidence_type"),
            value.get("claim"),
            value.get("operation"),
        )
        expected_result = (
            "fail"
            if (
                profile.get("surface_role") == "bounded_worker"
                and key[0] == "capability_boundary"
                and key[2] in assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS
            )
            else "pass"
        )
        if (
            value.get("profile_id") != record["profile_id"]
            or value.get("generation_id") != record["generation_id"]
            or value.get("result") != expected_result
        ):
            raise ObserverError("commissioning_evidence_identity_mismatch")
        if key in seen:
            raise ObserverError("commissioning_evidence_duplicate")
        seen[key] = reference
        try:
            _checkout_identity, _runtime, launch, worker = (
                assurance._verify_evidence_payload(
                    value,
                    profile=profile,
                    assurance_root=root,
                    now=now,
                    policy=policy,
                    expected_checkout=checkout,
                )
            )
            bindings = assurance._validate_bindings(
                value["bindings"], label="commissioning_evidence_bindings"
            )
        except (
            assurance.AssuranceContractError,
            assurance.AssuranceGateError,
        ) as exc:
            raise ObserverError("commissioning_evidence_verification_failed") from exc
        if expected_bindings is None:
            expected_bindings = bindings
            expected_launch = launch
            expected_worker = worker
        elif (
            bindings != expected_bindings
            or launch != expected_launch
            or worker != expected_worker
        ):
            raise ObserverError("commissioning_evidence_binding_mismatch")
        references.append(
            {
                "reference": reference,
                "sha256": assurance._sha256_bytes(raw),
                "key": [key[0], key[1], key[2]],
            }
        )
    if set(seen) != required or len(seen) != len(required):
        missing = sorted(repr(item) for item in required - set(seen))
        extra = sorted(repr(item) for item in set(seen) - required)
        raise ObserverError(
            "commissioning_evidence_coverage_mismatch:missing="
            + ",".join(missing)
            + ":extra="
            + ",".join(extra)
        )
    if profile.get("launch_validator") is not None:
        if (
            commissioning_session is None
            or expected_launch is None
            or expected_launch.get("record_digest")
            != commissioning_session.get("record_digest")
        ):
            raise ObserverError("commissioning_evidence_launch_session_mismatch")
        if expected_worker is not None:
            raise ObserverError("commissioning_frontend_worker_binding_unexpected")
    else:
        if expected_launch is not None:
            raise ObserverError("commissioning_worker_launch_session_unexpected")
        if expected_worker != record.get("execution_binding"):
            raise ObserverError("commissioning_worker_execution_binding_mismatch")
    try:
        final_names = sorted(item.name for item in evidence_dir.iterdir())
    except OSError as exc:
        raise ObserverError("commissioning_evidence_directory_unavailable") from exc
    if final_names != names:
        raise ObserverError("commissioning_evidence_changed_during_read")
    return {
        "references": sorted(references, key=lambda item: item["reference"]),
        "bindings": expected_bindings,
        "checkout": checkout,
        "launch_session": expected_launch,
        "worker_execution_binding": expected_worker,
    }


def finalize_frontend_commissioning_suite(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Consume one claimed frontend grant only after its exact suite passes."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    record = _load_commissioning(root, commissioning_reference, policy=policy)
    if record["state"] != "claimed" or record["purpose"] != "frontend_action_suite":
        raise ObserverError("claimed_frontend_commissioning_required")
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, record["profile_id"])
    suite = _collect_exact_generation_evidence(
        root, record, profile, policy=policy, now=current
    )
    consumer_event = {
        "event_version": "1",
        "event_kind": "exact_commissioning_suite_verified",
        "commissioning_id": record["commissioning_id"],
        "profile_id": record["profile_id"],
        "generation_id": record["generation_id"],
        "registry_subject_digest": assurance.profile_subject_digest(profile),
        "evidence": suite["references"],
        "bindings": suite["bindings"],
        "launch_session_digest": record["launch_session_digest"],
        "worker_execution_binding_digest": record["execution_binding_digest"],
    }
    event_digest = assurance._stable_digest(consumer_event)
    observed = finalize_commissioning_grant(
        root,
        commissioning_reference,
        consumer_event_digest=event_digest,
        trust_policy=policy,
        now=current,
    )
    return {
        "decision": "commissioning_suite_observed",
        "commissioning": _public_commissioning_projection(observed),
        "consumer_event_digest": event_digest,
        "evidence_references": [
            item["reference"] for item in suite["references"]
        ],
    }


def run_frontend_commissioning_suite(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute the fixed frontend commissioning suite with no caller probes."""

    selected_registry = registry if registry is not None else assurance.load_registry()
    common = observe_frontend_common_identity(
        state_root,
        commissioning_reference=commissioning_reference,
        registry=selected_registry,
        trust_policy=trust_policy,
        now=now,
    )
    common_references = [
        item["reference"] for item in common["common_evidence"]
    ]
    mechanical = observe_mechanically_absent_frontend_actions(
        state_root,
        commissioning_reference=commissioning_reference,
        common_evidence_references=common_references,
        registry=selected_registry,
        trust_policy=trust_policy,
        now=now,
    )
    if mechanical["inconclusive_operations"] != ["filesystem_write"]:
        raise ObserverError("frontend_mechanical_coverage_unexpected")
    filesystem = observe_frontend_filesystem_denial(
        state_root,
        commissioning_reference=commissioning_reference,
        common_evidence_references=common_references,
        registry=selected_registry,
        trust_policy=trust_policy,
        now=now,
    )
    gateway = observe_frontend_gateway_routing(
        state_root,
        commissioning_reference=commissioning_reference,
        common_evidence_references=common_references,
        registry=selected_registry,
        trust_policy=trust_policy,
        now=now,
    )
    finalized = finalize_frontend_commissioning_suite(
        state_root,
        commissioning_reference=commissioning_reference,
        registry=selected_registry,
        trust_policy=trust_policy,
        now=now,
    )
    return {
        "decision": "frontend_commissioning_suite_observed",
        "common": common,
        "mechanical_absence": mechanical,
        "filesystem_denial": filesystem,
        "gateway_routing": gateway,
        "finalized": finalized,
    }


def _load_worker_probe_event(
    root: Path,
    record: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
) -> tuple[dict[str, Any], dict[str, str]]:
    reference = (
        f"commissioning/{record['profile_id']}/"
        f"{record['commissioning_id']}-worker-event.json"
    )
    try:
        raw = assurance.secure_read_relative(root, reference, policy=policy)
        envelope = assurance._load_json_bytes(raw, label="worker_probe_event")
    except (assurance.EvidenceTrustError, assurance.AssuranceContractError) as exc:
        raise ObserverError("worker_probe_event_untrusted") from exc
    if not isinstance(envelope, Mapping) or set(envelope) != WORKER_EVENT_ENVELOPE_FIELDS:
        raise ObserverError("worker_probe_event_fields_invalid")
    event = envelope.get("event")
    if (
        envelope.get("envelope_version") != "1"
        or envelope.get("commissioning_reference")
        != f"commissioning/{record['profile_id']}/{record['commissioning_id']}.json"
        or envelope.get("single_use_nonce_digest")
        != assurance._stable_digest(
            {"single_use_nonce": record["single_use_nonce"]}
        )
        or envelope.get("execution_binding_digest")
        != record.get("execution_binding_digest")
        or not isinstance(event, Mapping)
        or set(event) != CommissioningAuthority.EVENT_FIELDS
        or envelope.get("event_digest") != assurance._stable_digest(dict(event))
        or record.get("consumer_event_digest") != envelope.get("event_digest")
    ):
        raise ObserverError("worker_probe_event_binding_mismatch")
    claimed_material = dict(record)
    claimed_material.update(
        {
            "state": "claimed",
            "completed_at": None,
            "consumer_event_digest": None,
            "generation_manifest_digest": None,
            "record_digest": "sha256:" + "0" * 64,
        }
    )
    claimed_digest = assurance._stable_digest(_material(claimed_material))
    if envelope.get("claimed_record_digest") != claimed_digest:
        raise ObserverError("worker_probe_claimed_record_mismatch")
    execution = record.get("execution_binding")
    try:
        import scoped_worker_executor as scoped_worker

        execution = scoped_worker.verify_worker_runtime_binding(execution)
        expected_argv = scoped_worker.commissioning_probe_argv(execution)
    except Exception as exc:
        raise ObserverError("worker_probe_execution_binding_invalid") from exc
    facts = event.get("probe_facts")
    if (
        event.get("profile_id") != record["profile_id"]
        or event.get("generation_id") != record["generation_id"]
        or event.get("purpose") != record["purpose"]
        or event.get("operation") != record["operation"]
        or event.get("runtime_binding_digest")
        != record["execution_binding_digest"]
        or event.get("runtime_realpath") != execution["runtime_realpath"]
        or event.get("runtime_digest") != execution["runtime_digest"]
        or event.get("argv") != expected_argv
        or event.get("argv_digest") != assurance._stable_digest(expected_argv)
        or event.get("probe_contract_result") != "pass"
        or event.get("exit_code") != 0
        or event.get("marker_before_digest")
        != event.get("marker_after_digest")
        or not isinstance(event.get("subject_pid"), int)
        or isinstance(event.get("subject_pid"), bool)
        or event["subject_pid"] <= 0
        or not isinstance(event.get("process_start_token"), str)
        or not assurance.SAFE_ID.fullmatch(event["process_start_token"])
        or not isinstance(facts, Mapping)
        or facts.get("facts_version") != "1"
        or facts.get("observed") != WORKER_PROBE_OBSERVED_FACTS
        or facts.get("mechanical_policy") != WORKER_PROBE_POLICY_FACTS
        or facts.get("not_model_prose") is not True
    ):
        raise ObserverError("worker_probe_event_mismatch")
    for digest_field in (
        "probe_result_digest",
        "probe_marker_digest",
        "probe_worktree_status_digest",
    ):
        if not assurance._valid_digest(event.get(digest_field)):
            raise ObserverError("worker_probe_event_digest_invalid")
    observed_at = assurance._parse_timestamp(
        event.get("observed_at"), label="worker_probe_observed_at"
    )
    if not (
        assurance._parse_timestamp(record["issued_at"], label="issued_at")
        <= observed_at
        <= assurance._parse_timestamp(record["valid_until"], label="valid_until")
    ):
        raise ObserverError("worker_probe_event_time_invalid")
    return dict(event), {
        "path": reference,
        "sha256": assurance._sha256_bytes(raw),
    }


def _worker_fact_for_operation(
    operation: str,
) -> tuple[str, str, bool]:
    if operation in {"filesystem_write", "process_spawn"}:
        return "observed", WORKER_PROBE_OBSERVED_FACTS[operation], True
    if operation == "shell_exec":
        return "mechanical_policy", WORKER_PROBE_POLICY_FACTS[operation], False
    if operation == "git_commit":
        return "postcondition_invariant", WORKER_PROBE_OBSERVED_FACTS[operation], False
    return "mechanical_policy", WORKER_PROBE_POLICY_FACTS[operation], False


def _write_worker_operation_evidence(
    root: Path,
    *,
    record: Mapping[str, Any],
    profile: Mapping[str, Any],
    bindings: Mapping[str, str],
    event: Mapping[str, Any],
    event_reference: Mapping[str, str],
    evidence_type: str,
    operation: str,
    policy: assurance.TrustPolicy,
    now: datetime,
    observer_path: Path,
    observer_digest: str,
) -> dict[str, Any]:
    coverage = profile["operation_requirements"][operation]
    promotion_blocked = (
        evidence_type == "capability_boundary"
        and operation in assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS
    )
    if evidence_type == "worker_launch_binding":
        fact_source = "observed"
        fact_value = WORKER_PROBE_OBSERVED_FACTS["surface_launch"]
        attempted = True
        outcome = "launched"
        side_effect = True
    else:
        fact_source, fact_value, attempted = _worker_fact_for_operation(operation)
        outcome = (
            "inconclusive"
            if promotion_blocked
            else (
                "capability_scoped"
                if coverage == "capability_scoped"
                else (
                    "post_execution_invariant"
                    if operation == "git_commit"
                    else "denied"
                )
            )
        )
        side_effect = attempted and coverage == "capability_scoped"
    identifier = (
        "worker-surface-launch"
        if evidence_type == "worker_launch_binding"
        else f"worker-boundary-{operation}"
    )
    marker_dir = (
        root
        / "generations"
        / record["profile_id"]
        / record["generation_id"]
        / "markers"
    )
    before_payload = {
        "snapshot_version": "1",
        "fact_source": fact_source,
        "state": "before",
        "operation": operation,
        "probe_event_digest": event_reference["sha256"],
    }
    after_payload = dict(before_payload)
    after_payload["state"] = "after" if side_effect else "before"
    if side_effect:
        after_payload["observed_fact"] = fact_value
    before_path = marker_dir / f"{identifier}-before.marker"
    after_path = marker_dir / f"{identifier}-after.marker"
    _atomic_write(before_path, before_payload, policy=policy, replace=False, mode=0o644)
    _atomic_write(after_path, after_payload, policy=policy, replace=False, mode=0o644)
    receipt_event = {
        "event_version": "1",
        "event_id": f"receipt-{identifier}",
        "event_kind": (
            "worker_commissioning_observed_fact"
            if attempted
            else (
                "worker_commissioning_postcondition_proof"
                if fact_source == "postcondition_invariant"
                else "worker_commissioning_policy_proof"
            )
        ),
        "operation": operation,
        "fact_source": fact_source,
        "fact_value": fact_value,
        "worker_probe_event": dict(event_reference),
        "commissioning_reference": (
            f"commissioning/{record['profile_id']}/{record['commissioning_id']}.json"
        ),
        "commissioning_record_digest": record["record_digest"],
        "worker_execution_binding_digest": record["execution_binding_digest"],
        "subject_pid": event["subject_pid"],
        "process_start_token": event["process_start_token"],
    }
    receipt = {
        "receipt_version": "1",
        "receipt_id": f"receipt-{identifier}",
        "profile_id": record["profile_id"],
        "evidence_type": evidence_type,
        "claim": "managed_worker",
        "operation": operation,
        "observed_at": event["observed_at"],
        "observer": {
            "kind": "sandbox_policy_audit",
            "binary_realpath": str(observer_path),
            "binary_sha256": observer_digest,
        },
        "event": receipt_event,
    }
    observer_dir = marker_dir.parent / "observer"
    receipt_path = observer_dir / f"{identifier}-receipt.json"
    _atomic_write(receipt_path, receipt, policy=policy, replace=False, mode=0o644)
    receipt_reference = {
        "path": str(receipt_path.relative_to(root)),
        "sha256": assurance._sha256_bytes(receipt_path.read_bytes()),
    }
    capability_id = (
        "commissioning-capability-"
        + str(record["execution_binding_digest"]).removeprefix("sha256:")[:24]
    )
    observation = {
        "observation_version": "1",
        "observation_id": f"obs-{identifier}",
        "profile_id": record["profile_id"],
        "evidence_type": evidence_type,
        "claim": "managed_worker",
        "operation": operation,
        "observed_at": event["observed_at"],
        "source": "host_harness",
        "attempted": attempted,
        "model_prose_authority": False,
        "outcome": outcome,
        "approval_prompted": False,
        "side_effect_observed": side_effect,
        "before_marker_path": str(before_path.relative_to(root)),
        "before_marker_digest": assurance._sha256_bytes(before_path.read_bytes()),
        "after_marker_path": str(after_path.relative_to(root)),
        "after_marker_digest": assurance._sha256_bytes(after_path.read_bytes()),
        "saihai_request_id": record["commissioning_id"],
        "capability_id": capability_id,
        "audit_id": event["event_id"],
        "capability_bound": True,
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
        "observer_receipt": receipt_reference,
        "probe_process_binding": None,
    }
    observation_dir = marker_dir.parent / "observations"
    observation_path = observation_dir / f"{identifier}-observation.json"
    _atomic_write(
        observation_path, observation, policy=policy, replace=False, mode=0o644
    )
    observed_at = assurance._parse_timestamp(
        event["observed_at"], label="worker_observed_at"
    )
    valid_until = min(
        assurance._parse_timestamp(record["valid_until"], label="valid_until"),
        observed_at
        + timedelta(seconds=profile["evidence_policy"]["max_age_seconds"]),
    )
    evidence = {
        "evidence_version": "2",
        "evidence_id": f"ev-{identifier}",
        "generation_id": record["generation_id"],
        "profile_id": record["profile_id"],
        "claim": "managed_worker",
        "evidence_type": evidence_type,
        "operation": operation,
        "result": "fail" if promotion_blocked else "pass",
        "observed_at": event["observed_at"],
        "valid_until": assurance.format_timestamp(valid_until),
        "registry_subject_digest": assurance.profile_subject_digest(profile),
        "bindings": dict(bindings),
        "launch_session": None,
        "worker_execution_binding": record["execution_binding"],
        "observed_digest": None,
        "details": {
            "host_observation": {
                "path": str(observation_path.relative_to(root)),
                "sha256": assurance._sha256_bytes(observation_path.read_bytes()),
            }
        },
    }
    try:
        assurance._verify_evidence_payload(
            evidence,
            profile=profile,
            assurance_root=root,
            now=now,
            policy=policy,
            expected_checkout=None,
        )
    except (assurance.AssuranceContractError, assurance.AssuranceGateError) as exc:
        raise ObserverError("worker_operation_evidence_invalid") from exc
    evidence_path = marker_dir.parent / "evidence" / f"ev-{identifier}.json"
    _atomic_write(evidence_path, evidence, policy=policy, replace=False, mode=0o644)
    return {
        "reference": str(evidence_path.relative_to(root)),
        "sha256": assurance._sha256_bytes(evidence_path.read_bytes()),
    }


def observe_worker_commissioning_evidence(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    observer_binary: Path | None = None,
) -> dict[str, Any]:
    """Convert one consumed fixed worker probe into the exact first-install suite."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    record = _load_commissioning(root, commissioning_reference, policy=policy)
    if (
        record["state"] != "observed"
        or record["purpose"] != "managed_worker_surface_launch_probe"
        or record.get("execution_binding") is None
    ):
        raise ObserverError("observed_worker_commissioning_required")
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, record["profile_id"])
    event, event_reference = _load_worker_probe_event(
        root, record, policy=policy
    )
    observer_path = (observer_binary or Path(__file__)).resolve(strict=True)
    observer_digest = assurance._hash_external_regular(
        str(observer_path), policy=policy
    )
    assurance._validate_observer_executable(
        str(observer_path),
        observer_digest,
        policy=policy,
        label="worker_commissioning_observer",
    )
    execution = record["execution_binding"]
    snapshot_path = Path(record["runtime_binding"]["profile_realpath"])
    snapshot_digest = assurance._hash_external_regular(
        str(snapshot_path), policy=policy
    )
    if snapshot_digest != record["runtime_binding"]["profile_digest"]:
        raise ObserverError("worker_runtime_snapshot_drift")
    inventory = ["filesystem", "process", "shell"]
    classification = {
        "inventory_version": "1",
        "model_identifier": "codex-scoped-worker-fixed-runtime",
        "server_backed_mcp_tools": [],
        "reviewed_internal_tools": [],
        "active_external_mutation_tools": inventory,
        "mechanically_denied_tools": [],
        "dynamic_tools": [],
        "extension_tools": [],
        "multi_agent_tools": [],
    }
    configuration_artifacts = [
        {
            "role": "profile_snapshot",
            "path": str(snapshot_path),
            "sha256": snapshot_digest,
        }
    ]
    checkout_binding = assurance.worker_checkout_binding()
    bindings = {
        "configuration_digest": assurance._stable_digest(configuration_artifacts),
        "runtime_binary_digest": execution["runtime_digest"],
        "tool_inventory_digest": assurance._stable_digest(inventory),
        "checkout_digest": checkout_binding["identity_digest"],
    }
    common_receipt = {
        "receipt_version": "1",
        "receipt_id": f"worker-common-{record['commissioning_id']}",
        "profile_id": record["profile_id"],
        "observed_at": event["observed_at"],
        "observer": {
            "kind": "sandbox_policy_audit",
            "binary_realpath": str(observer_path),
            "binary_sha256": observer_digest,
        },
        "event": {
            "event_id": f"worker-identity-{record['commissioning_id']}",
            "event_kind": "effective_agent_identity",
            "subject_pid": event["subject_pid"],
            "process_start_token": event["process_start_token"],
            "runtime_binary_realpath": execution["runtime_realpath"],
            "runtime_binary_digest": execution["runtime_digest"],
            "runtime_argv": execution["argv_template"],
            "runtime_argv_digest": execution["argv_digest"],
            "profile_snapshot_path": str(snapshot_path),
            "profile_snapshot_digest": snapshot_digest,
            "tool_inventory": inventory,
            "tool_inventory_classification": classification,
            "tool_inventory_observation": None,
            "launcher_realpath": None,
            "launcher_digest": None,
            "launcher_process_start_token": None,
            "host_verified_launch_token": None,
            "launch_argv_digest": None,
            "effective_notify_empty": None,
            **bindings,
            "launch_session": None,
            "worker_execution_binding": execution,
        },
    }
    common_path = (
        root
        / "generations"
        / record["profile_id"]
        / record["generation_id"]
        / "observer"
        / f"worker-common-{record['commissioning_id']}.json"
    )
    _atomic_write(
        common_path, common_receipt, policy=policy, replace=False, mode=0o644
    )
    try:
        import agent_integration_canary as canary

        common = canary.record_common_evidence(
            root,
            profile_id=record["profile_id"],
            generation_id=record["generation_id"],
            observer_receipt_reference={
                "path": str(common_path.relative_to(root)),
                "sha256": assurance._sha256_bytes(common_path.read_bytes()),
            },
            registry=selected_registry,
            trust_policy=policy,
            now=current,
        )
    except Exception as exc:
        raise ObserverError("worker_common_evidence_invalid") from exc
    operations: list[dict[str, str]] = []
    operations.append(
        _write_worker_operation_evidence(
            root,
            record=record,
            profile=profile,
            bindings=bindings,
            event=event,
            event_reference=event_reference,
            evidence_type="worker_launch_binding",
            operation="surface_launch",
            policy=policy,
            now=current,
            observer_path=observer_path,
            observer_digest=observer_digest,
        )
    )
    for operation in assurance.ACTION_OPERATIONS:
        operations.append(
            _write_worker_operation_evidence(
                root,
                record=record,
                profile=profile,
                bindings=bindings,
                event=event,
                event_reference=event_reference,
                evidence_type="capability_boundary",
                operation=operation,
                policy=policy,
                now=current,
                observer_path=observer_path,
                observer_digest=observer_digest,
            )
        )
    suite = _collect_exact_generation_evidence(
        root, record, profile, policy=policy, now=current
    )
    return {
        "decision": "worker_commissioning_suite_observed",
        "commissioning": _public_commissioning_projection(record),
        "common_evidence": common["evidence"],
        "operation_evidence": operations,
        "evidence_references": [
            item["reference"] for item in suite["references"]
        ],
    }


def seal_commissioning_generation(
    state_root: Path | str,
    *,
    commissioning_reference: str,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze, complete, seal, and activate one already-observed generation."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    selected_registry = registry if registry is not None else assurance.load_registry()
    record = _load_commissioning(root, commissioning_reference, policy=policy)
    if record["state"] not in {"observed", "completed"}:
        raise ObserverError("observed_commissioning_required")
    profile = assurance._select_profile(selected_registry, record["profile_id"])
    suite = _collect_exact_generation_evidence(
        root, record, profile, policy=policy, now=current
    )
    evidence_references = [item["reference"] for item in suite["references"]]
    try:
        import agent_integration_attester as attester

        manifest_path = (
            root
            / "generations"
            / record["profile_id"]
            / record["generation_id"]
            / "manifest.json"
        )
        if manifest_path.exists():
            manifest, _raw, _reference = attester.load_generation_manifest(
                root,
                record["profile_id"],
                record["generation_id"],
                registry=selected_registry,
                trust_policy=policy,
            )
            if (
                manifest["commissioning_id"] != record["commissioning_id"]
                or [item["reference"] for item in manifest["evidence"]]
                != evidence_references
            ):
                raise ObserverError("commissioning_generation_manifest_mismatch")
        else:
            created = attester.create_generation_manifest(
                root,
                record["profile_id"],
                record["generation_id"],
                record["commissioning_id"],
                evidence_references,
                registry=selected_registry,
                trust_policy=policy,
                now=current,
            )
            manifest = created["manifest"]
        if record["state"] == "observed":
            record = complete_commissioning_generation(
                root,
                commissioning_reference,
                generation_manifest_digest=manifest["manifest_digest"],
                trust_policy=policy,
                now=current,
            )
        elif record["generation_manifest_digest"] != manifest["manifest_digest"]:
            raise ObserverError("commissioning_generation_manifest_digest_mismatch")
        result = attester.seal_attestation(
            root,
            record["profile_id"],
            record["generation_id"],
            registry=selected_registry,
            trust_policy=policy,
            now=current,
            expected_checkout=suite["checkout"],
        )
    except ObserverError:
        raise
    except (assurance.AssuranceContractError, assurance.AssuranceGateError) as exc:
        raise ObserverError("commissioning_generation_seal_failed") from exc
    return {
        "decision": "commissioning_generation_active",
        "commissioning": _public_commissioning_projection(record),
        "generation_manifest_digest": manifest["manifest_digest"],
        **result,
    }


def _transition(
    root: Path,
    reference: str,
    *,
    policy: assurance.TrustPolicy,
    expected_state: str,
    transition: Any,
) -> dict[str, Any]:
    if not reference.startswith("commissioning/") or not reference.endswith(".json"):
        raise ObserverError("commissioning_reference_invalid")
    path = root / reference
    lock_path = path.with_suffix(".lock")
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ObserverError("commissioning_lock_unavailable") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        raw = assurance.secure_read_relative(root, reference, policy=policy)
        record = _validate_commissioning_shape(
            assurance._load_json_bytes(raw, label="commissioning")
        )
        if record["state"] != expected_state:
            raise ObserverError("commissioning_already_used")
        updated = transition(dict(record))
        updated["record_digest"] = assurance._stable_digest(_material(updated))
        normalized = _validate_commissioning_shape(updated)
        _atomic_write(path, normalized, policy=policy, replace=True, mode=0o600)
        return normalized
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def claim_commissioning_grant(
    state_root: Path | str,
    reference: str,
    *,
    expected_profile_id: str,
    expected_generation_id: str,
    expected_purpose: str,
    expected_runtime_binding_digest: str,
    expected_marker_target_sha256: str,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)

    def update(record: dict[str, Any]) -> dict[str, Any]:
        if (
            record["profile_id"] != expected_profile_id
            or record["generation_id"] != expected_generation_id
            or record["purpose"] != expected_purpose
            or record["runtime_binding_digest"] != expected_runtime_binding_digest
            or record["marker_target_sha256"] != expected_marker_target_sha256
        ):
            raise ObserverError("commissioning_grant_binding_mismatch")
        if current > assurance._parse_timestamp(record["valid_until"], label="valid_until"):
            raise ObserverError("commissioning_grant_expired")
        record["state"] = "claimed"
        record["claimed_at"] = assurance.format_timestamp(current)
        return record

    return _transition(
        root, reference, policy=policy, expected_state="pending", transition=update
    )


def finalize_commissioning_grant(
    state_root: Path | str,
    reference: str,
    *,
    consumer_event_digest: str,
    generation_manifest_digest: str | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    if not assurance._valid_digest(consumer_event_digest) or (
        generation_manifest_digest is not None
        and not assurance._valid_digest(generation_manifest_digest)
    ):
        raise ObserverError("commissioning_completion_digest_invalid")

    def update(record: dict[str, Any]) -> dict[str, Any]:
        if current > assurance._parse_timestamp(record["valid_until"], label="valid_until"):
            raise ObserverError("commissioning_grant_expired")
        record["state"] = (
            "completed" if generation_manifest_digest is not None else "observed"
        )
        record["completed_at"] = assurance.format_timestamp(current)
        record["consumer_event_digest"] = consumer_event_digest
        record["generation_manifest_digest"] = generation_manifest_digest
        return record

    return _transition(
        root, reference, policy=policy, expected_state="claimed", transition=update
    )


def complete_commissioning_generation(
    state_root: Path | str,
    reference: str,
    *,
    generation_manifest_digest: str,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current = _now(now)
    if not assurance._valid_digest(generation_manifest_digest):
        raise ObserverError("commissioning_completion_digest_invalid")

    def update(record: dict[str, Any]) -> dict[str, Any]:
        if current > assurance._parse_timestamp(record["valid_until"], label="valid_until"):
            raise ObserverError("commissioning_grant_expired")
        record["state"] = "completed"
        record["generation_manifest_digest"] = generation_manifest_digest
        return record

    return _transition(
        root, reference, policy=policy, expected_state="observed", transition=update
    )


def require_completed_commissioning(
    state_root: Path | str,
    *,
    profile_id: str,
    generation_id: str,
    commissioning_id: str,
    generation_manifest_digest: str,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    reference = f"commissioning/{profile_id}/{commissioning_id}.json"
    try:
        raw = assurance.secure_read_relative(root, reference, policy=policy)
        record = _validate_commissioning_shape(
            assurance._load_json_bytes(raw, label="commissioning")
        )
    except (assurance.EvidenceTrustError, assurance.AssuranceContractError, ObserverError) as exc:
        raise assurance.AssuranceGateError("commissioning_completion_untrusted") from exc
    current = _now(now)
    if (
        record["state"] != "completed"
        or record["profile_id"] != profile_id
        or record["generation_id"] != generation_id
        or record["generation_manifest_digest"] != generation_manifest_digest
        or current > assurance._parse_timestamp(record["valid_until"], label="valid_until")
    ):
        raise assurance.AssuranceGateError("commissioning_completion_mismatch")
    return record


class CommissioningAuthority:
    """In-process root authority exposed to the fixed worker probe only."""

    EVENT_FIELDS = {
        "event_version",
        "event_id",
        "profile_id",
        "generation_id",
        "purpose",
        "operation",
        "runtime_binding_digest",
        "runtime_realpath",
        "runtime_digest",
        "subject_pid",
        "process_start_token",
        "argv",
        "argv_digest",
        "marker_before_digest",
        "marker_after_digest",
        "probe_contract_result",
        "probe_result_digest",
        "probe_marker_digest",
        "probe_worktree_status_digest",
        "probe_facts",
        "exit_code",
        "observed_at",
    }

    def __init__(
        self,
        root: Path,
        *,
        trust_policy: assurance.TrustPolicy,
        now: datetime | None = None,
    ) -> None:
        self.root = root
        self.policy = trust_policy
        self.fixed_now = now
        self._claims: dict[str, tuple[str, dict[str, Any]]] = {}

    def claim(
        self,
        commissioning_id: str,
        expected_profile_id: str,
        expected_purpose: str,
        expected_operation: str,
    ) -> dict[str, Any]:
        if not assurance.SAFE_ID.fullmatch(commissioning_id):
            raise ObserverError("commissioning_id_invalid")
        reference = f"commissioning/{expected_profile_id}/{commissioning_id}.json"
        pending = _load_commissioning(self.root, reference, policy=self.policy)
        if pending["operation"] != expected_operation:
            raise ObserverError("commissioning_operation_mismatch")
        claimed = claim_commissioning_grant(
            self.root,
            reference,
            expected_profile_id=expected_profile_id,
            expected_generation_id=pending["generation_id"],
            expected_purpose=expected_purpose,
            expected_runtime_binding_digest=pending["runtime_binding_digest"],
            expected_marker_target_sha256=pending["marker_target_sha256"],
            trust_policy=self.policy,
            now=self.fixed_now,
        )
        claim_id = "claim-" + uuid.uuid4().hex
        self._claims[claim_id] = (reference, claimed)
        # The runtime receives no path and no nonce.  claim_id is only a local
        # handle in this authority object and has no filesystem authority.
        public_grant = {
            key: claimed[key]
            for key in (
                "commissioning_id",
                "profile_id",
                "generation_id",
                "purpose",
                "operation",
                "runtime_binding_digest",
                "probe_argv_digest",
                "marker_target_sha256",
                "issued_at",
                "valid_until",
                "state",
            )
        }
        execution_binding = claimed.get("execution_binding")
        if execution_binding is not None:
            public_grant["runtime_binding_digest"] = claimed[
                "execution_binding_digest"
            ]
        return {
            "claim_id": claim_id,
            "grant": public_grant,
            "runtime_binding": execution_binding or claimed["runtime_binding"],
        }

    def _claimed(self, claim: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        claim_id = claim.get("claim_id") if isinstance(claim, Mapping) else None
        if not isinstance(claim_id, str) or claim_id not in self._claims:
            raise ObserverError("commissioning_claim_handle_invalid")
        return self._claims[claim_id]

    def read_marker(self, claim: Mapping[str, Any]) -> bytes:
        _reference, record = self._claimed(claim)
        path = (
            self.root
            / "generations"
            / record["profile_id"]
            / record["generation_id"]
            / "markers"
            / "commissioning.marker"
        )
        if assurance._sha256_bytes(str(path).encode("utf-8")) != record["marker_target_sha256"]:
            raise ObserverError("commissioning_marker_binding_mismatch")
        if path.exists():
            raw = path.read_bytes()
            value = {
                "exists": True,
                "content_sha256": assurance._sha256_bytes(raw),
                "size": len(raw),
            }
        else:
            value = {"exists": False, "content_sha256": None, "size": 0}
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def finalize(self, claim: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
        reference, record = self._claimed(claim)
        if not isinstance(event, Mapping) or set(event) != self.EVENT_FIELDS:
            raise ObserverError("commissioning_probe_event_invalid")
        for field in ("event_id",):
            if not isinstance(event.get(field), str) or not assurance.SAFE_ID.fullmatch(event[field]):
                raise ObserverError("commissioning_probe_event_invalid")
        execution_binding = record.get("execution_binding")
        try:
            import scoped_worker_executor as scoped_worker

            normalized_execution = scoped_worker.verify_worker_runtime_binding(
                execution_binding
            )
            expected_argv = scoped_worker.commissioning_probe_argv(
                normalized_execution
            )
        except Exception as exc:
            raise ObserverError("commissioning_probe_runtime_binding_invalid") from exc
        probe_facts = event.get("probe_facts")
        if (
            event.get("event_version") != "1"
            or event.get("profile_id") != record["profile_id"]
            or event.get("generation_id") != record["generation_id"]
            or event.get("purpose") != record["purpose"]
            or event.get("operation") != record["operation"]
            or event.get("runtime_binding_digest")
            != (
                record["execution_binding_digest"]
                if record["execution_binding"] is not None
                else record["runtime_binding_digest"]
            )
            or event.get("runtime_realpath")
            != normalized_execution["runtime_realpath"]
            or event.get("runtime_digest") != normalized_execution["runtime_digest"]
            or not isinstance(event.get("subject_pid"), int)
            or isinstance(event.get("subject_pid"), bool)
            or event["subject_pid"] <= 0
            or not isinstance(event.get("process_start_token"), str)
            or not assurance.SAFE_ID.fullmatch(event["process_start_token"])
            or event.get("argv") != expected_argv
            or event.get("argv_digest") != record["probe_argv_digest"]
            or event.get("argv_digest") != assurance._stable_digest(expected_argv)
            or event.get("marker_before_digest") != event.get("marker_after_digest")
            or not assurance._valid_digest(event.get("marker_before_digest"))
            or event.get("probe_contract_result") != "pass"
            or any(
                not assurance._valid_digest(event.get(field))
                for field in (
                    "probe_result_digest",
                    "probe_marker_digest",
                    "probe_worktree_status_digest",
                )
            )
            or not isinstance(event.get("exit_code"), int)
            or isinstance(event.get("exit_code"), bool)
            or event.get("exit_code") != 0
            or not isinstance(probe_facts, Mapping)
            or set(probe_facts) != {
                "facts_version",
                "observed",
                "mechanical_policy",
                "not_model_prose",
            }
            or probe_facts.get("facts_version") != "1"
            or probe_facts.get("observed") != WORKER_PROBE_OBSERVED_FACTS
            or probe_facts.get("mechanical_policy") != WORKER_PROBE_POLICY_FACTS
            or probe_facts.get("not_model_prose") is not True
        ):
            raise ObserverError("commissioning_probe_event_mismatch")
        assurance._parse_timestamp(event.get("observed_at"), label="probe_observed_at")
        event_digest = assurance._stable_digest(dict(event))
        envelope = {
            "envelope_version": "1",
            "commissioning_reference": reference,
            "claimed_record_digest": record["record_digest"],
            "single_use_nonce_digest": assurance._stable_digest(
                {"single_use_nonce": record["single_use_nonce"]}
            ),
            "execution_binding_digest": record["execution_binding_digest"],
            "event": dict(event),
            "event_digest": event_digest,
        }
        event_path = (
            self.root
            / "commissioning"
            / record["profile_id"]
            / f"{record['commissioning_id']}-worker-event.json"
        )
        _atomic_write(
            event_path,
            envelope,
            policy=self.policy,
            replace=False,
            mode=0o600,
        )
        try:
            observed = finalize_commissioning_grant(
                self.root,
                reference,
                consumer_event_digest=event_digest,
                trust_policy=self.policy,
                now=self.fixed_now,
            )
        except Exception:
            try:
                event_path.unlink()
            except OSError:
                pass
            raise
        self._claims.pop(str(claim["claim_id"]), None)
        return {
            "state": observed["state"],
            "event_digest": event_digest,
            "event_reference": str(event_path.relative_to(self.root)),
        }


def commissioning_authority(
    *,
    state_root: Path | str | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> CommissioningAuthority:
    root = assurance.assurance_root_from_state_root(
        state_root if state_root is not None else assurance.production_assurance_root()
    )
    return CommissioningAuthority(
        root,
        trust_policy=trust_policy or assurance.TrustPolicy.production(),
        now=now,
    )


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Root-only Saihai assurance commissioning monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    begin = subparsers.add_parser("commission-begin")
    begin.add_argument("--profile", required=True)
    begin.add_argument("--launch-session")
    begin.add_argument("--worker-runtime-binding", type=Path)
    frontend = subparsers.add_parser("commission-run-frontend")
    frontend.add_argument("--commissioning", required=True)
    worker = subparsers.add_parser("commission-observe-worker")
    worker.add_argument("--commissioning", required=True)
    seal = subparsers.add_parser("commission-seal")
    seal.add_argument("--commissioning", required=True)
    args = parser.parse_args()
    try:
        if args.command == "commission-begin":
            runtime_binding = None
            if args.worker_runtime_binding is not None:
                try:
                    import agent_integration_canary as canary
                except ImportError as exc:
                    raise ObserverError("canary_module_unavailable") from exc
                try:
                    supplied = args.worker_runtime_binding.expanduser()
                    runtime_binding = assurance._load_json_bytes(
                        canary._read_external_regular(
                            supplied, max_bytes=assurance.MAX_JSON_BYTES
                        ),
                        label="worker_runtime_binding",
                    )
                except (
                    OSError,
                    assurance.AssuranceContractError,
                    canary.CanaryError,
                ) as exc:
                    raise ObserverError("worker_runtime_binding_file_invalid") from exc
            result = begin_commissioning(
                assurance.production_assurance_root(),
                profile_id=args.profile,
                launch_session_reference=args.launch_session,
                runtime_binding=runtime_binding,
            )
        elif args.command == "commission-run-frontend":
            result = run_frontend_commissioning_suite(
                assurance.production_assurance_root(),
                commissioning_reference=args.commissioning,
            )
        elif args.command == "commission-observe-worker":
            result = observe_worker_commissioning_evidence(
                assurance.production_assurance_root(),
                commissioning_reference=args.commissioning,
            )
        elif args.command == "commission-seal":
            result = seal_commissioning_generation(
                assurance.production_assurance_root(),
                commissioning_reference=args.commissioning,
            )
        else:  # pragma: no cover - argparse makes this unreachable
            raise ObserverError("command_invalid")
    except (ObserverError, assurance.AssuranceGateError, assurance.AssuranceContractError) as exc:
        _print({"decision": "inconclusive", "reason": getattr(exc, "reason", str(exc))})
        raise SystemExit(2) from exc
    _print(result)


if __name__ == "__main__":
    main()
