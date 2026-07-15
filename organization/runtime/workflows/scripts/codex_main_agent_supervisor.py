#!/usr/bin/env python3
"""Root launch supervisor for the single supported Codex A-prime CLI surface.

The human invokes the installed zero-argument launcher through ``sudo``.  This
module verifies the immutable deployment, resolves the active supported
checkout, creates an administrator-owned launch-session record, then forks and
drops to the manifest-bound runtime account before executing the fixed stock
Codex argv.  The session id is not a credential: authority comes from the
root-owned record plus a live PID/process-start match.

Production paths and argv are not caller configurable.  Library helpers accept
explicit roots only so the fixture tests can exercise the same record writer
without administrator access.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pwd
import signal
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402
import codex_main_agent_deployment as deployment  # noqa: E402
import frontdoor_orchestrator as frontdoor  # noqa: E402
import run_lock  # noqa: E402


SESSION_VERSION = "2"
SESSION_LIFETIME_SECONDS = 24 * 60 * 60
COMMISSIONING_LIFETIME_SECONDS = 15 * 60
SESSION_DIRECTORY_NAME = "launch-sessions"
COMMISSIONING_DIRECTORY_NAME = "commissioning-launches"
SESSION_KINDS = {"standard", "commissioning"}
COMMISSIONING_PROBE_IDS = {
    "frontend_filesystem_denial",
    "frontend_gateway_routing",
}
SESSION_RECORD_FIELDS = {
    "launch_session_version",
    "session_id",
    "deployment_id",
    "profile_id",
    "principal_id",
    "workspace_id",
    "subject_pid",
    "process_start_token",
    "native_realpath",
    "native_digest",
    "profile_realpath",
    "profile_digest",
    "launch_argv_digest",
    "checkout_realpath",
    "checkout_identity_digest",
    "issued_at",
    "valid_until",
    "status",
    "session_kind",
    "commissioning_launch_reference",
    "commissioning_launch_digest",
    "supervisor_pid",
    "supervisor_start_token",
    "record_reference",
    "record_digest",
}
COMMISSIONING_LAUNCH_FIELDS = {
    "commissioning_launch_version",
    "session_id",
    "commissioning_id",
    "generation_id",
    "profile_id",
    "probe_id",
    "nonce_digest",
    "probe_argv_digest",
    "issued_at",
    "valid_until",
    "state",
    "record_reference",
    "binding_digest",
    "launch_session_digest",
    "live_observation",
    "live_observation_digest",
    "record_digest",
}
COMMISSIONING_BINDING_FIELDS = {
    "commissioning_launch_version",
    "session_id",
    "commissioning_id",
    "generation_id",
    "profile_id",
    "probe_id",
    "nonce_digest",
    "probe_argv_digest",
    "issued_at",
    "valid_until",
    "record_reference",
}


class SupervisorError(RuntimeError):
    """The launch session cannot be established safely."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _session_material(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in sorted(SESSION_RECORD_FIELDS - {"record_digest"})}


def _commissioning_binding_material(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in sorted(COMMISSIONING_BINDING_FIELDS)}


def _commissioning_record_material(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in sorted(COMMISSIONING_LAUNCH_FIELDS - {"record_digest"})
    }


def validate_session_record_shape(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping) or set(record) != SESSION_RECORD_FIELDS:
        raise SupervisorError("launch_session_fields_invalid")
    normalized = dict(record)
    if normalized["launch_session_version"] != SESSION_VERSION:
        raise SupervisorError("launch_session_version_invalid")
    for field in (
        "session_id",
        "deployment_id",
        "profile_id",
        "principal_id",
        "workspace_id",
        "process_start_token",
        "supervisor_start_token",
    ):
        if not isinstance(normalized[field], str) or not normalized[field]:
            raise SupervisorError(f"launch_session_{field}_invalid")
    for field in ("subject_pid", "supervisor_pid"):
        value = normalized[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SupervisorError(f"launch_session_{field}_invalid")
    for field in (
        "native_realpath",
        "profile_realpath",
        "checkout_realpath",
    ):
        value = normalized[field]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise SupervisorError(f"launch_session_{field}_invalid")
    for field in (
        "native_digest",
        "profile_digest",
        "launch_argv_digest",
        "checkout_identity_digest",
        "record_digest",
    ):
        if not isinstance(normalized[field], str) or not assurance.SHA256.fullmatch(
            normalized[field]
        ):
            raise SupervisorError(f"launch_session_{field}_invalid")
    if normalized["status"] != "active":
        raise SupervisorError("launch_session_status_invalid")
    if normalized["session_kind"] not in SESSION_KINDS:
        raise SupervisorError("launch_session_kind_invalid")
    reference = normalized["record_reference"]
    if (
        not isinstance(reference, str)
        or not reference.startswith(f"{SESSION_DIRECTORY_NAME}/")
        or not reference.endswith(".json")
        or "/" in reference.removeprefix(f"{SESSION_DIRECTORY_NAME}/")
    ):
        raise SupervisorError("launch_session_reference_invalid")
    if normalized["record_digest"] != _stable_digest(_session_material(normalized)):
        raise SupervisorError("launch_session_digest_invalid")
    commissioning_reference = normalized["commissioning_launch_reference"]
    commissioning_digest = normalized["commissioning_launch_digest"]
    if normalized["session_kind"] == "standard":
        if commissioning_reference is not None or commissioning_digest is not None:
            raise SupervisorError("standard_launch_session_commissioning_binding_forbidden")
        expected_argv = deployment.native_codex_argv(normalized["native_realpath"])
        if normalized["launch_argv_digest"] != _stable_digest(expected_argv):
            raise SupervisorError("standard_launch_session_argv_invalid")
    else:
        expected_reference = (
            f"{COMMISSIONING_DIRECTORY_NAME}/{normalized['session_id']}.json"
        )
        if commissioning_reference != expected_reference or not (
            isinstance(commissioning_digest, str)
            and assurance.SHA256.fullmatch(commissioning_digest)
        ):
            raise SupervisorError("commissioning_launch_session_binding_invalid")
    return normalized


def validate_commissioning_launch_shape(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the short-lived root-owned companion for a probe session."""

    if not isinstance(record, Mapping) or set(record) != COMMISSIONING_LAUNCH_FIELDS:
        raise SupervisorError("commissioning_launch_fields_invalid")
    normalized = dict(record)
    if normalized["commissioning_launch_version"] != "1":
        raise SupervisorError("commissioning_launch_version_invalid")
    for field in (
        "session_id",
        "commissioning_id",
        "generation_id",
        "profile_id",
        "probe_id",
    ):
        if not isinstance(normalized[field], str) or not assurance.SAFE_ID.fullmatch(
            normalized[field]
        ):
            raise SupervisorError(f"commissioning_launch_{field}_invalid")
    if normalized["probe_id"] not in COMMISSIONING_PROBE_IDS:
        raise SupervisorError("commissioning_launch_probe_id_invalid")
    for field in (
        "nonce_digest",
        "probe_argv_digest",
        "binding_digest",
        "launch_session_digest",
        "record_digest",
    ):
        if not isinstance(normalized[field], str) or not assurance.SHA256.fullmatch(
            normalized[field]
        ):
            raise SupervisorError(f"commissioning_launch_{field}_invalid")
    live_observation = normalized["live_observation"]
    live_digest = normalized["live_observation_digest"]
    if (live_observation is None) != (live_digest is None):
        raise SupervisorError("commissioning_launch_live_observation_invalid")
    if live_observation is not None:
        expected_live_fields = {
            "observed_at",
            "subject_pid",
            "process_start_token",
            "executable_realpath",
            "parent_pid",
            "supervisor_start_token",
        }
        if not isinstance(live_observation, Mapping) or set(live_observation) != expected_live_fields:
            raise SupervisorError("commissioning_launch_live_observation_invalid")
        if (
            not isinstance(live_observation["subject_pid"], int)
            or isinstance(live_observation["subject_pid"], bool)
            or live_observation["subject_pid"] <= 0
            or not isinstance(live_observation["parent_pid"], int)
            or isinstance(live_observation["parent_pid"], bool)
            or live_observation["parent_pid"] <= 0
            or not isinstance(live_observation["process_start_token"], str)
            or not live_observation["process_start_token"]
            or not isinstance(live_observation["supervisor_start_token"], str)
            or not live_observation["supervisor_start_token"]
            or not isinstance(live_observation["executable_realpath"], str)
            or not Path(live_observation["executable_realpath"]).is_absolute()
            or not isinstance(live_digest, str)
            or not assurance.SHA256.fullmatch(live_digest)
            or live_digest != _stable_digest(dict(live_observation))
        ):
            raise SupervisorError("commissioning_launch_live_observation_invalid")
        try:
            assurance._parse_timestamp(
                live_observation["observed_at"],
                label="commissioning_launch_live_observed_at",
            )
        except assurance.AssuranceContractError as exc:
            raise SupervisorError("commissioning_launch_live_observation_invalid") from exc
    expected_reference = (
        f"{COMMISSIONING_DIRECTORY_NAME}/{normalized['session_id']}.json"
    )
    if normalized["record_reference"] != expected_reference:
        raise SupervisorError("commissioning_launch_reference_invalid")
    if normalized["state"] not in {"active", "consumed", "failed"}:
        raise SupervisorError("commissioning_launch_state_invalid")
    try:
        issued_at = assurance._parse_timestamp(
            normalized["issued_at"], label="commissioning_launch_issued_at"
        )
        valid_until = assurance._parse_timestamp(
            normalized["valid_until"], label="commissioning_launch_valid_until"
        )
    except assurance.AssuranceContractError as exc:
        raise SupervisorError("commissioning_launch_time_invalid") from exc
    if valid_until <= issued_at or valid_until - issued_at > timedelta(
        seconds=COMMISSIONING_LIFETIME_SECONDS
    ):
        raise SupervisorError("commissioning_launch_time_invalid")
    if normalized["binding_digest"] != _stable_digest(
        _commissioning_binding_material(normalized)
    ):
        raise SupervisorError("commissioning_launch_binding_digest_invalid")
    if normalized["record_digest"] != _stable_digest(
        _commissioning_record_material(normalized)
    ):
        raise SupervisorError("commissioning_launch_record_digest_invalid")
    return normalized


def build_session_record(
    *,
    manifest: Mapping[str, Any],
    checkout_identity: Mapping[str, Any],
    session_id: str,
    subject_pid: int,
    process_start_token: str,
    supervisor_pid: int,
    supervisor_start_token: str,
    issued_at: datetime,
    launch_argv: list[str] | None = None,
    session_kind: str = "standard",
    commissioning_launch_reference: str | None = None,
    commissioning_launch_digest: str | None = None,
    lifetime_seconds: int = SESSION_LIFETIME_SECONDS,
) -> dict[str, Any]:
    """Build the immutable record shared by supervisor, bridge, and attester."""

    artifacts = manifest.get("artifacts")
    bindings = manifest.get("bindings")
    if not isinstance(artifacts, Mapping) or not isinstance(bindings, Mapping):
        raise SupervisorError("deployment_manifest_binding_invalid")
    native = artifacts.get("native_codex")
    profile = artifacts.get("codex_profile")
    if not isinstance(native, Mapping) or not isinstance(profile, Mapping):
        raise SupervisorError("deployment_launch_artifact_missing")
    canonical_argv = deployment.native_codex_argv(str(native.get("path") or ""))
    argv = list(launch_argv) if launch_argv is not None else canonical_argv
    if (
        not argv
        or any(not isinstance(value, str) or not value for value in argv)
        or argv[0] != str(native.get("path") or "")
    ):
        raise SupervisorError("launch_session_argv_invalid")
    if session_kind == "standard" and argv != canonical_argv:
        raise SupervisorError("standard_launch_session_argv_invalid")
    if not isinstance(lifetime_seconds, int) or isinstance(lifetime_seconds, bool) or not (
        1 <= lifetime_seconds <= SESSION_LIFETIME_SECONDS
    ):
        raise SupervisorError("launch_session_lifetime_invalid")
    reference = f"{SESSION_DIRECTORY_NAME}/{session_id}.json"
    record: dict[str, Any] = {
        "launch_session_version": SESSION_VERSION,
        "session_id": session_id,
        "deployment_id": manifest.get("deployment_id"),
        "profile_id": manifest.get("deployment_id"),
        "principal_id": bindings.get("principal_id"),
        "workspace_id": bindings.get("workspace_id"),
        "subject_pid": subject_pid,
        "process_start_token": process_start_token,
        "native_realpath": native.get("path"),
        "native_digest": native.get("sha256"),
        "profile_realpath": profile.get("path"),
        "profile_digest": profile.get("sha256"),
        "launch_argv_digest": _stable_digest(argv),
        "checkout_realpath": checkout_identity.get("checkout_realpath"),
        "checkout_identity_digest": checkout_identity.get("identity_digest"),
        "issued_at": assurance.format_timestamp(issued_at),
        "valid_until": assurance.format_timestamp(
            issued_at + timedelta(seconds=lifetime_seconds)
        ),
        "status": "active",
        "session_kind": session_kind,
        "commissioning_launch_reference": commissioning_launch_reference,
        "commissioning_launch_digest": commissioning_launch_digest,
        "supervisor_pid": supervisor_pid,
        "supervisor_start_token": supervisor_start_token,
        "record_reference": reference,
        "record_digest": "sha256:" + "0" * 64,
    }
    record["record_digest"] = _stable_digest(_session_material(record))
    return validate_session_record_shape(record)


def build_commissioning_launch(
    *,
    session_id: str,
    commissioning_id: str,
    generation_id: str,
    profile_id: str,
    probe_id: str,
    nonce_digest: str,
    probe_argv: list[str],
    launch_session_digest: str,
    issued_at: datetime,
    state: str = "active",
) -> dict[str, Any]:
    """Build the non-secret companion bound into one probe launch record."""

    reference = f"{COMMISSIONING_DIRECTORY_NAME}/{session_id}.json"
    record: dict[str, Any] = {
        "commissioning_launch_version": "1",
        "session_id": session_id,
        "commissioning_id": commissioning_id,
        "generation_id": generation_id,
        "profile_id": profile_id,
        "probe_id": probe_id,
        "nonce_digest": nonce_digest,
        "probe_argv_digest": _stable_digest(probe_argv),
        "issued_at": assurance.format_timestamp(issued_at),
        "valid_until": assurance.format_timestamp(
            issued_at + timedelta(seconds=COMMISSIONING_LIFETIME_SECONDS)
        ),
        "state": state,
        "record_reference": reference,
        "binding_digest": "sha256:" + "0" * 64,
        "launch_session_digest": launch_session_digest,
        "live_observation": None,
        "live_observation_digest": None,
        "record_digest": "sha256:" + "0" * 64,
    }
    record["binding_digest"] = _stable_digest(_commissioning_binding_material(record))
    record["record_digest"] = _stable_digest(_commissioning_record_material(record))
    return validate_commissioning_launch_shape(record)


def _atomic_write_session(
    assurance_root: Path,
    record: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
) -> Path:
    root = assurance_root.resolve(strict=True)
    session_dir = root / SESSION_DIRECTORY_NAME
    assurance._validate_root_chain(session_dir, policy)
    observed_dir = session_dir.lstat()
    production_mode_invalid = (
        policy.trusted_ancestor == Path("/")
        and stat.S_IMODE(observed_dir.st_mode) != 0o755
    )
    if (
        stat.S_ISLNK(observed_dir.st_mode)
        or not stat.S_ISDIR(observed_dir.st_mode)
        or stat.S_IMODE(observed_dir.st_mode) & 0o022
        or production_mode_invalid
    ):
        raise SupervisorError("launch_session_directory_untrusted")
    normalized = validate_session_record_shape(record)
    filename = Path(normalized["record_reference"]).name
    raw = (
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    directory_fd = os.open(session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    temporary = f".{filename}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o644, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o644)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SupervisorError("launch_session_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise SupervisorError("launch_session_exists") from exc
    except OSError as exc:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise SupervisorError("launch_session_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return session_dir / filename


def _atomic_write_commissioning_launch(
    assurance_root: Path,
    record: Mapping[str, Any],
    *,
    policy: assurance.TrustPolicy,
) -> Path:
    root = assurance_root.resolve(strict=True)
    launch_dir = root / COMMISSIONING_DIRECTORY_NAME
    assurance._validate_root_chain(launch_dir, policy)
    observed_dir = launch_dir.lstat()
    production_mode_invalid = (
        policy.trusted_ancestor == Path("/")
        and stat.S_IMODE(observed_dir.st_mode) != 0o755
    )
    if (
        stat.S_ISLNK(observed_dir.st_mode)
        or not stat.S_ISDIR(observed_dir.st_mode)
        or stat.S_IMODE(observed_dir.st_mode) & 0o022
        or production_mode_invalid
    ):
        raise SupervisorError("commissioning_launch_directory_untrusted")
    normalized = validate_commissioning_launch_shape(record)
    filename = Path(normalized["record_reference"]).name
    raw = (
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    directory_fd = os.open(launch_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    temporary = f".{filename}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o644, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o644)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SupervisorError("commissioning_launch_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise SupervisorError("commissioning_launch_exists") from exc
    except OSError as exc:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise SupervisorError("commissioning_launch_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return launch_dir / filename


def _atomic_replace_commissioning_launch(
    root: Path,
    *,
    companion_reference: str,
    before_raw: bytes,
    updated: Mapping[str, Any],
    policy: assurance.TrustPolicy,
) -> dict[str, Any]:
    normalized = validate_commissioning_launch_shape(updated)
    path = root / companion_reference
    raw = (
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    temporary = f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o644, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o644)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SupervisorError("commissioning_launch_transition_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        current_raw = assurance.secure_read_relative(
            root, companion_reference, policy=policy
        )
        if current_raw != before_raw:
            raise SupervisorError("commissioning_launch_changed_during_transition")
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except SupervisorError:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise SupervisorError("commissioning_launch_transition_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return normalized


def finalize_commissioning_launch(
    assurance_root: Path,
    *,
    companion_reference: str,
    expected_binding_digest: str,
    final_state: str,
    policy: assurance.TrustPolicy,
) -> dict[str, Any]:
    """Move one live companion to consumed/failed after stable revalidation."""

    if final_state not in {"consumed", "failed"}:
        raise SupervisorError("commissioning_launch_final_state_invalid")
    if not isinstance(companion_reference, str) or not companion_reference.startswith(
        f"{COMMISSIONING_DIRECTORY_NAME}/"
    ):
        raise SupervisorError("commissioning_launch_reference_invalid")
    root = assurance_root.resolve(strict=True)
    try:
        before_raw = assurance.secure_read_relative(root, companion_reference, policy=policy)
        before = validate_commissioning_launch_shape(json.loads(before_raw))
    except (assurance.AssuranceContractError, json.JSONDecodeError) as exc:
        raise SupervisorError("commissioning_launch_reopen_failed") from exc
    if before["state"] != "active" or not hmac.compare_digest(
        before["binding_digest"], expected_binding_digest
    ):
        raise SupervisorError("commissioning_launch_transition_binding_mismatch")
    updated = dict(before)
    updated["state"] = final_state
    updated["record_digest"] = _stable_digest(_commissioning_record_material(updated))
    return _atomic_replace_commissioning_launch(
        root,
        companion_reference=companion_reference,
        before_raw=before_raw,
        updated=updated,
        policy=policy,
    )


def record_commissioning_live_observation(
    assurance_root: Path,
    *,
    companion_reference: str,
    expected_binding_digest: str,
    subject_pid: int,
    expected_process_start_token: str,
    expected_executable: Path,
    supervisor_pid: int,
    expected_supervisor_start_token: str,
    policy: assurance.TrustPolicy,
) -> dict[str, Any]:
    """Persist what the root supervisor observed while the probe was live."""

    root = assurance_root.resolve(strict=True)
    try:
        before_raw = assurance.secure_read_relative(root, companion_reference, policy=policy)
        before = validate_commissioning_launch_shape(json.loads(before_raw))
    except (assurance.AssuranceContractError, json.JSONDecodeError) as exc:
        raise SupervisorError("commissioning_launch_reopen_failed") from exc
    if (
        before["state"] != "active"
        or before["live_observation"] is not None
        or not hmac.compare_digest(before["binding_digest"], expected_binding_digest)
        or not run_lock.process_is_alive(subject_pid)
        or not hmac.compare_digest(
            run_lock.process_start_token(subject_pid), expected_process_start_token
        )
        or not run_lock.process_is_alive(supervisor_pid)
        or not hmac.compare_digest(
            run_lock.process_start_token(supervisor_pid),
            expected_supervisor_start_token,
        )
        or frontdoor.live_parent_pid(subject_pid) != supervisor_pid
        or frontdoor.live_process_executable(subject_pid) != expected_executable.resolve(
            strict=True
        )
    ):
        raise SupervisorError("commissioning_launch_live_identity_mismatch")
    observation = {
        "observed_at": assurance.format_timestamp(datetime.now(timezone.utc)),
        "subject_pid": subject_pid,
        "process_start_token": expected_process_start_token,
        "executable_realpath": str(expected_executable.resolve(strict=True)),
        "parent_pid": supervisor_pid,
        "supervisor_start_token": expected_supervisor_start_token,
    }
    updated = dict(before)
    updated["live_observation"] = observation
    updated["live_observation_digest"] = _stable_digest(observation)
    updated["record_digest"] = _stable_digest(_commissioning_record_material(updated))
    return _atomic_replace_commissioning_launch(
        root,
        companion_reference=companion_reference,
        before_raw=before_raw,
        updated=updated,
        policy=policy,
    )


def _required_sudo_runtime_user(manifest: Mapping[str, Any]) -> pwd.struct_passwd:
    if os.geteuid() != 0:
        raise SupervisorError("root_supervisor_required")
    bindings = manifest["bindings"]
    expected_uid = int(bindings["runtime_user_uid"])
    try:
        sudo_uid = int(os.environ.get("SUDO_UID", ""))
    except ValueError as exc:
        raise SupervisorError("human_sudo_context_required") from exc
    if sudo_uid != expected_uid or sudo_uid <= 0:
        raise SupervisorError("human_sudo_runtime_user_mismatch")
    try:
        account = pwd.getpwuid(expected_uid)
    except KeyError as exc:
        raise SupervisorError("runtime_user_account_unavailable") from exc
    if account.pw_name != bindings["runtime_user_name"]:
        raise SupervisorError("runtime_user_name_mismatch")
    if Path(account.pw_dir).resolve(strict=True) != Path(
        bindings["runtime_user_home"]
    ).resolve(strict=True):
        raise SupervisorError("runtime_user_home_mismatch")
    return account


def _child_environment(manifest: Mapping[str, Any]) -> dict[str, str]:
    bindings = manifest["bindings"]
    return {
        "HOME": str(bindings["runtime_user_home"]),
        "CODEX_HOME": str(bindings["codex_home"]),
        "USER": str(bindings["runtime_user_name"]),
        "LOGNAME": str(bindings["runtime_user_name"]),
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "TERM": os.environ.get("TERM", "xterm-256color"),
    }


def commissioning_probe_argv(
    manifest: Mapping[str, Any],
    checkout_root: Path,
    probe_id: str,
) -> list[str]:
    """Resolve one fixed probe argv without accepting prompt/tool/path input."""

    if probe_id not in COMMISSIONING_PROBE_IDS:
        raise SupervisorError("commissioning_launch_probe_id_invalid")
    try:
        import agent_integration_canary as canary

        native = str(manifest["artifacts"]["native_codex"]["path"])
        base_argv = deployment.native_codex_argv(native)
        if probe_id == "frontend_filesystem_denial":
            return canary.frontend_filesystem_probe_argv(
                base_argv, str(checkout_root)
            )
        return canary.frontend_gateway_probe_argv(base_argv, str(checkout_root))
    except SupervisorError:
        raise
    except Exception as exc:
        raise SupervisorError("commissioning_probe_argv_unavailable") from exc


def run_commissioning_probe(
    *,
    commissioning_id: str,
    generation_id: str,
    probe_id: str,
    nonce_digest: str,
    deployment_manifest_path: Path = deployment.MANIFEST_PATH,
) -> dict[str, Any]:
    """Run a fixed root-supervised Codex probe behind a publish barrier.

    The normal agent and caller cannot supply an executable, prompt, tool,
    argv, state root, or workspace path.  Those values are resolved only from
    the verified deployment, current supported checkout, and the fixed probe
    catalog above.  The returned companion remains ``active`` until the
    observer parses the structured result and calls
    :func:`finalize_commissioning_launch`.
    """

    if os.geteuid() != 0:
        raise SupervisorError("root_supervisor_required")
    if (
        not isinstance(commissioning_id, str)
        or not assurance.SAFE_ID.fullmatch(commissioning_id)
        or not isinstance(generation_id, str)
        or not assurance.SAFE_ID.fullmatch(generation_id)
        or not isinstance(nonce_digest, str)
        or not assurance.SHA256.fullmatch(nonce_digest)
    ):
        raise SupervisorError("commissioning_probe_binding_invalid")
    manifest = deployment.verify_deployment(deployment_manifest_path)
    account = _required_sudo_runtime_user(manifest)
    checkout_root = Path.cwd().resolve(strict=True)
    checkout_identity = frontdoor.resolve_checkout_identity(
        workspace_id=manifest["bindings"]["workspace_id"],
        managed_primary=Path(manifest["bindings"]["managed_primary"]),
        checkout_root=checkout_root,
    )
    assurance_root = Path(manifest["bindings"]["assurance_root"]).resolve(
        strict=True
    )
    policy = assurance.TrustPolicy.production()
    argv = commissioning_probe_argv(manifest, checkout_root, probe_id)
    native = Path(manifest["artifacts"]["native_codex"]["path"])
    session_id = "launch-" + uuid.uuid4().hex
    issued_at = datetime.now(timezone.utc)
    companion_reference = f"{COMMISSIONING_DIRECTORY_NAME}/{session_id}.json"

    read_barrier, write_barrier = os.pipe()
    stdout_file = tempfile.TemporaryFile(mode="w+b")
    stderr_file = tempfile.TemporaryFile(mode="w+b")
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(write_barrier)
            barrier = os.read(read_barrier, 1)
            os.close(read_barrier)
            if barrier != b"1":
                os._exit(78)
            os.dup2(stdout_file.fileno(), 1)
            os.dup2(stderr_file.fileno(), 2)
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            os.chdir(checkout_root)
            os.execve(str(native), argv, _child_environment(manifest))
        except BaseException:
            os._exit(78)

    os.close(read_barrier)
    child_start = ""
    for _attempt in range(100):
        child_start = run_lock.process_start_token(child_pid)
        if child_start:
            break
        time.sleep(0.01)
    supervisor_start = run_lock.process_start_token(os.getpid())
    if not child_start or not supervisor_start:
        os.close(write_barrier)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(child_pid, 0)
        stdout_file.close()
        stderr_file.close()
        raise SupervisorError("process_start_identity_unavailable")

    # The immutable binding can be computed before the session digest.  The
    # companion's full record digest then covers the completed reverse link.
    draft_companion = build_commissioning_launch(
        session_id=session_id,
        commissioning_id=commissioning_id,
        generation_id=generation_id,
        profile_id=str(manifest["deployment_id"]),
        probe_id=probe_id,
        nonce_digest=nonce_digest,
        probe_argv=argv,
        launch_session_digest="sha256:" + "0" * 64,
        issued_at=issued_at,
    )
    session = build_session_record(
        manifest=manifest,
        checkout_identity=checkout_identity,
        session_id=session_id,
        subject_pid=child_pid,
        process_start_token=child_start,
        supervisor_pid=os.getpid(),
        supervisor_start_token=supervisor_start,
        issued_at=issued_at,
        launch_argv=argv,
        session_kind="commissioning",
        commissioning_launch_reference=companion_reference,
        commissioning_launch_digest=draft_companion["binding_digest"],
        lifetime_seconds=COMMISSIONING_LIFETIME_SECONDS,
    )
    companion = build_commissioning_launch(
        session_id=session_id,
        commissioning_id=commissioning_id,
        generation_id=generation_id,
        profile_id=str(manifest["deployment_id"]),
        probe_id=probe_id,
        nonce_digest=nonce_digest,
        probe_argv=argv,
        launch_session_digest=session["record_digest"],
        issued_at=issued_at,
    )
    if companion["binding_digest"] != draft_companion["binding_digest"]:
        raise SupervisorError("commissioning_launch_binding_unstable")
    try:
        _atomic_write_commissioning_launch(
            assurance_root, companion, policy=policy
        )
        _atomic_write_session(assurance_root, session, policy=policy)
        os.write(write_barrier, b"1")
        os.close(write_barrier)
        write_barrier = -1
    except Exception:
        if write_barrier >= 0:
            os.close(write_barrier)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(child_pid, 0)
        try:
            finalize_commissioning_launch(
                assurance_root,
                companion_reference=companion_reference,
                expected_binding_digest=companion["binding_digest"],
                final_state="failed",
                policy=policy,
            )
        except Exception:
            pass
        stdout_file.close()
        stderr_file.close()
        raise

    observed_companion: dict[str, Any] | None = None
    for _attempt in range(500):
        try:
            observed_companion = record_commissioning_live_observation(
                assurance_root,
                companion_reference=companion_reference,
                expected_binding_digest=companion["binding_digest"],
                subject_pid=child_pid,
                expected_process_start_token=child_start,
                expected_executable=native,
                supervisor_pid=os.getpid(),
                expected_supervisor_start_token=supervisor_start,
                policy=policy,
            )
            break
        except (SupervisorError, frontdoor.FrontdoorError):
            time.sleep(0.01)
    if observed_companion is None:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(child_pid, 0)
        except ChildProcessError:
            pass
        finalize_commissioning_launch(
            assurance_root,
            companion_reference=companion_reference,
            expected_binding_digest=companion["binding_digest"],
            final_state="failed",
            policy=policy,
        )
        stdout_file.close()
        stderr_file.close()
        raise SupervisorError("commissioning_probe_live_identity_unobserved")
    companion = observed_companion

    deadline = time.monotonic() + COMMISSIONING_LIFETIME_SECONDS
    status: int | None = None
    while status is None and time.monotonic() < deadline:
        observed_pid, observed_status = os.waitpid(child_pid, os.WNOHANG)
        if observed_pid == child_pid:
            status = observed_status
            break
        time.sleep(0.05)
    if status is None:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(child_pid, 0)
        finalize_commissioning_launch(
            assurance_root,
            companion_reference=companion_reference,
            expected_binding_digest=companion["binding_digest"],
            final_state="failed",
            policy=policy,
        )
        stdout_file.close()
        stderr_file.close()
        raise SupervisorError("commissioning_probe_timeout")
    stdout_file.seek(0)
    stderr_file.seek(0)
    stdout = stdout_file.read(8 * 1024 * 1024 + 1)
    stderr = stderr_file.read(2 * 1024 * 1024 + 1)
    stdout_file.close()
    stderr_file.close()
    if len(stdout) > 8 * 1024 * 1024 or len(stderr) > 2 * 1024 * 1024:
        finalize_commissioning_launch(
            assurance_root,
            companion_reference=companion_reference,
            expected_binding_digest=companion["binding_digest"],
            final_state="failed",
            policy=policy,
        )
        raise SupervisorError("commissioning_probe_output_too_large")
    if os.WIFEXITED(status):
        exit_code = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status):
        exit_code = 128 + os.WTERMSIG(status)
    else:
        exit_code = 1
    return {
        "session": session,
        "companion": companion,
        "argv": argv,
        "pid": child_pid,
        "process_start_token": child_start,
        "exit_code": exit_code,
        "stdout_bytes": stdout,
        "stderr_bytes": stderr,
        "state_root": str(manifest["bindings"]["state_root"]),
        "checkout_realpath": str(checkout_root),
    }


def supervise_production() -> int:
    """Create one root-authenticated launch session and supervise its child."""

    manifest = deployment.verify_deployment(deployment.MANIFEST_PATH)
    account = _required_sudo_runtime_user(manifest)
    checkout_root = Path.cwd().resolve(strict=True)
    checkout_identity = frontdoor.resolve_checkout_identity(
        workspace_id=manifest["bindings"]["workspace_id"],
        managed_primary=Path(manifest["bindings"]["managed_primary"]),
        checkout_root=checkout_root,
    )
    assurance_root = assurance.production_assurance_root()
    native = Path(manifest["artifacts"]["native_codex"]["path"])
    argv = deployment.native_codex_argv(native)
    read_barrier, write_barrier = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(write_barrier)
            barrier = os.read(read_barrier, 1)
            os.close(read_barrier)
            if barrier != b"1":
                os._exit(78)
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            os.chdir(checkout_root)
            os.execve(str(native), argv, _child_environment(manifest))
        except BaseException:
            os._exit(78)

    os.close(read_barrier)
    child_start = ""
    for _attempt in range(100):
        child_start = run_lock.process_start_token(child_pid)
        if child_start:
            break
        time.sleep(0.01)
    supervisor_start = run_lock.process_start_token(os.getpid())
    if not child_start or not supervisor_start:
        os.close(write_barrier)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(child_pid, 0)
        raise SupervisorError("process_start_identity_unavailable")

    session_id = "launch-" + uuid.uuid4().hex
    record = build_session_record(
        manifest=manifest,
        checkout_identity=checkout_identity,
        session_id=session_id,
        subject_pid=child_pid,
        process_start_token=child_start,
        supervisor_pid=os.getpid(),
        supervisor_start_token=supervisor_start,
        issued_at=datetime.now(timezone.utc),
    )
    _atomic_write_session(
        assurance_root,
        record,
        policy=assurance.TrustPolicy.production(),
    )
    os.write(write_barrier, b"1")
    os.close(write_barrier)

    def _terminate(_signum: int, _frame: Any) -> None:
        try:
            os.kill(child_pid, signal.SIGTERM)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    _pid, status = os.waitpid(child_pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch the root-authenticated Saihai Codex frontend session"
    )
    parser.parse_args(argv)
    try:
        return supervise_production()
    except (SupervisorError, assurance.AssuranceGateError, deployment.DeploymentError) as exc:
        reason = str(getattr(exc, "reason", exc))
        print(f"blocked: {reason}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
