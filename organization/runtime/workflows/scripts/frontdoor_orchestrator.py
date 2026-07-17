#!/usr/bin/env python3
"""Host-owned frontdoor and P0 harness for deterministic workflow control."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import host_state_root
import safe_paths
import run_store
import run_lock
import run_lifecycle
import completion_gate
import report_gate
import task_state_bridge
import work_order_builder
import workflow_selector
import provider_runner
import scoped_worker_executor
import frontdoor_surface_registry

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
HOST_HOME = host_state_root.HOST_HOME
DEFAULT_STATE_ROOT = host_state_root.DEFAULT_STATE_ROOT
MANAGED_PRIMARY_CHECKOUT_ROOT = host_state_root.MANAGED_PRIMARY_CHECKOUT_ROOT
PRIMARY_DIRECTORY_CATALOG_PATH = host_state_root.PRIMARY_DIRECTORY_CATALOG_PATH
DIRECTORY_CATALOG = host_state_root.DIRECTORY_CATALOG
DIRECTORY_ENV_DIAGNOSTICS = host_state_root.DIRECTORY_ENV_DIAGNOSTICS
host_home_directory = host_state_root.host_home_directory
validate_host_state_root_value = host_state_root.validate_host_state_root_value
load_primary_directory_catalog = host_state_root.load_primary_directory_catalog
SCOPED_WORKER_REPO_FULL_NAME = "Saber5656/Saihai"
SCOPED_WORKER_ASSURANCE_PROFILE_ID = "codex-scoped-worker"
SCOPED_WORKER_ASSURANCE_ROOT = Path("/Library/Application Support/Saihai/Assurance")
LAUNCH_SESSION_DIRECTORY = SCOPED_WORKER_ASSURANCE_ROOT / "launch-sessions"
TRANSITION_SIGNATURE_ALGORITHM = "sha256-hmac-sha256-local-principal-key"
BRIDGE_PRINCIPAL_TYPE = "main_agent_bridge"
HTTP_CHANNEL_PRINCIPALS = {
    "bridge": (BRIDGE_PRINCIPAL_TYPE, "http-bridge", "local_http_channel"),
    "operator": ("manual_operator", "http-operator", "local_http_channel"),
    "human_ui": ("human_operator", "human-ui", "local_http_channel"),
    "harness": ("harness_runner", "local-harness", "local_http_channel"),
    "action_gateway": ("action_gateway_executor", "child-thread-gateway", "local_http_channel"),
}

EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
}
WORKFLOW_DEFINITION_PRINCIPAL_TYPES = {"human_deploy_review"}
ACTION_GATEWAY_PRINCIPAL_TYPES = {"action_gateway_executor"}
BRIDGE_REQUEST_KINDS = {
    "agent_task_request",
    "external_review_request",
    "orchestrator_status_request",
}
BRIDGE_ALLOWED_ACTIONS = ["submit_request", "read_projection", "ack_output"]
BRIDGE_PENDING_REQUEST_STATUSES = {
    "active",
    "approved",
    "proposed",
    "selected",
    "waiting_human",
}
DEFAULT_BRIDGE_MAX_PENDING_REQUESTS = 128
DEFAULT_BRIDGE_MAX_PENDING_BYTES = 8 * 1024 * 1024
DEFAULT_BRIDGE_MAX_DURABLE_ARTIFACTS = 10_000
DEFAULT_BRIDGE_MAX_DURABLE_BYTES = 128 * 1024 * 1024
DEFAULT_BRIDGE_READS_PER_MINUTE = 240
DEFAULT_BRIDGE_ACKS_PER_MINUTE = 120
BRIDGE_RATE_WINDOW_SECONDS = 60
BRIDGE_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
BRIDGE_AUDIT_ROTATE_BYTES = 8 * 1024 * 1024
BRIDGE_AUDIT_ROTATION_RETENTION_SECONDS = 30 * 24 * 60 * 60
BRIDGE_TERMINAL_STATUSES = {"complete", "failed", "aborted", "rejected"}
RUN_REQUEST_TERMINAL_STATUS = {
    "complete": "complete",
    "failed": "failed",
    "aborted": "aborted",
}
BRIDGE_SUBMIT_ALLOWED_FIELDS = {
    "task_id",
    "request_id",
    "request_kind",
    "prompt",
    "refs",
    "allowed_paths",
    "expires_at",
    "frontdoor",
    "chat_session_id",
    "idempotency_key",
}
BRIDGE_FORBIDDEN_FIELDS = {
    "classification",
    "workflow_selection",
    "activation",
    "approved_activation",
    "human_action_id",
    "run_id",
    "workflow_id",
    "initial_step",
    "steps",
    "gates",
    "max_steps",
    "template",
    "work_order",
    "adapter_request",
    "child_thread_create",
    "child_thread_plan",
    "create_thread",
    "fork_thread",
    "git_command",
    "report_path",
    "shell_command",
    "command",
    "command_argv",
    "raw_cli",
    "raw_prompt",
    "worker_prompt",
    "worker_backend",
    "branch",
    "branch_name",
    "provider",
    "provider_id",
    "network",
    "evidence_path",
    "transcript_path",
    "worktree_path",
    "token",
    "api_key",
    "secret",
    "credential",
    "authorization",
    "principal_type",
    "principal_id",
    "authn_method",
    "owner_principal",
    "workspace_id",
    "checkout_identity",
    "checkout_identity_digest",
    "launch_session",
    "launch_session_identity",
    "launch_session_digest",
    "surface_identity",
    "assurance_state",
}
MAX_APPROVAL_FAILURES = 3
MAX_CONTEXT_REF_COUNT = 50
MAX_ALLOWED_PATH_COUNT = 50
MAX_CONTEXT_REF_FILE_BYTES = 1_000_000
MAX_CONTEXT_REF_TOTAL_BYTES = 5_000_000
# canonical copy lives in run_store.py
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REF_DENYLIST_NAMES = {
    ".git",
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
}
REF_DENYLIST_PREFIXES = (".env", "id_rsa", "id_ed25519")
REF_DENYLIST_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
REF_DENYLIST_SUBSTRINGS = (
    "private_key",
    "private-key",
    "deploy_key",
    "deploy-key",
    "secret_key",
    "secret-key",
    "auth_key",
    "auth-key",
    "credential",
    "secret",
    "token",
)
REPO_FULL_NAME_COMPONENT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
MAX_REPO_FULL_NAME_COMPONENT_CHARS = 100


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def load_repo_json(path: Path) -> Any:
    try:
        repo_root = WORKFLOW_ROOT.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise FrontdoorError("repository JSON must be a regular file")
        with resolved.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise FrontdoorError(f"repository JSON must stay within workflow root: {path}") from exc


def load_json_arg(raw: str) -> Any:
    return workflow_selector.load_json_arg(raw)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    run_store.atomic_write_json(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = run_store.read_json(path)
    except (OSError, run_store.RunStoreError) as exc:
        raise FrontdoorError(f"missing file: {path}") from exc
    if not isinstance(data, dict):
        raise FrontdoorError(f"expected object json: {path}")
    return data


def state_file_exists(path: Path) -> bool:
    try:
        return run_store.private_artifact_exists(path)
    except run_store.RunStoreError as exc:
        raise FrontdoorError(f"unsafe state artifact: {path}") from exc


def state_file_stat(path: Path) -> os.stat_result | None:
    try:
        return run_store.private_artifact_stat(path)
    except run_store.RunStoreError as exc:
        raise FrontdoorError(f"unsafe state artifact: {path}") from exc


def list_state_files(
    directory: Path,
    *,
    prefix: str = "",
    suffix: str = "",
) -> list[Path]:
    try:
        return run_store.list_private_artifacts(
            directory,
            prefix=prefix,
            suffix=suffix,
        )
    except run_store.RunStoreError as exc:
        raise FrontdoorError(f"unsafe state artifact directory: {directory}") from exc


def unlink_state_file(path: Path, *, missing_ok: bool = False) -> bool:
    try:
        return run_store.unlink_private_file(path, missing_ok=missing_ok)
    except run_store.RunStoreError as exc:
        raise FrontdoorError(f"unsafe state artifact deletion: {path}") from exc


def rotate_state_file(source: Path, target: Path) -> None:
    try:
        run_store.rotate_private_file(source, target)
    except run_store.RunStoreError as exc:
        raise FrontdoorError(f"unsafe state artifact rotation: {source}") from exc


class FrontdoorError(RuntimeError):
    pass


def load_surface_registry() -> frontdoor_surface_registry.SurfaceRegistry:
    try:
        return frontdoor_surface_registry.default_registry()
    except frontdoor_surface_registry.SurfaceRegistryError as exc:
        raise FrontdoorError(str(exc)) from exc


def registered_surface_kinds() -> tuple[str, ...]:
    return load_surface_registry().frontend_kinds


def resolve_surface_identity(
    frontend_kind: str,
    *,
    registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
    expected_checkout: Path | None = None,
    launch_session_present: bool | None = None,
) -> dict[str, Any]:
    active_registry = registry or load_surface_registry()
    try:
        identity = active_registry.identify(
            frontend_kind,
            expected_checkout=expected_checkout,
            launch_session_present=launch_session_present,
        )
    except frontdoor_surface_registry.SurfaceRegistryError as exc:
        raise FrontdoorError(str(exc)) from exc
    return identity.as_dict()


def surface_contract_binding(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: identity.get(key)
        for key in (
            "identity_version",
            "frontend_kind",
            "descriptor_digest",
            "target_assurance_state",
            "assurance_profile_id",
            "submit_contract_version",
        )
    }


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _parse_host_timestamp(value: Any, *, label: str) -> float:
    if not isinstance(value, str) or not value:
        raise FrontdoorError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FrontdoorError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise FrontdoorError(f"{label} must include timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def normalize_launch_session_identity(value: Any) -> dict[str, Any]:
    """Validate the exact root-supervisor launch-session contract."""

    try:
        import codex_main_agent_supervisor as supervisor

        normalized = supervisor.validate_session_record_shape(value)
    except Exception as exc:
        raise FrontdoorError("launch_session_identity_invalid") from exc
    return dict(normalized)


def launch_session_identity_digest(value: Any) -> str:
    normalized = normalize_launch_session_identity(value)
    return str(normalized["record_digest"])


def normalize_commissioning_launch(value: Any) -> dict[str, Any]:
    """Validate the exact root-supervisor commissioning companion contract."""

    try:
        import codex_main_agent_supervisor as supervisor

        normalized = supervisor.validate_commissioning_launch_shape(value)
    except Exception as exc:
        raise FrontdoorError("commissioning_launch_record_invalid") from exc
    return dict(normalized)


def _trusted_root_artifact_bytes(
    path: Path,
    *,
    expected_owner_uid: int = 0,
    expected_mode: int = 0o644,
    max_bytes: int = 1024 * 1024,
) -> bytes:
    supplied = path.absolute()
    current = Path(supplied.anchor)
    for part in supplied.parts[1:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise FrontdoorError("launch_session_trust_chain_invalid") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid not in {0, expected_owner_uid}
            or metadata.st_mode & 0o022
        ):
            raise FrontdoorError("launch_session_trust_chain_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(supplied, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_owner_uid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size > max_bytes
        ):
            raise FrontdoorError("launch_session_record_metadata_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise FrontdoorError("launch_session_record_too_large")
        after = os.fstat(descriptor)
    except OSError as exc:
        raise FrontdoorError("launch_session_record_unavailable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FrontdoorError("launch_session_record_changed_during_read")
    return b"".join(chunks)


def _secure_file_digest(path: Path, *, allowed_owner_uids: set[int]) -> str:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in allowed_owner_uids
        or metadata.st_mode & 0o022
    ):
        raise FrontdoorError("launch_session_artifact_metadata_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FrontdoorError("launch_session_artifact_changed_during_read")
    return "sha256:" + digest.hexdigest()


def live_process_executable(pid: int) -> Path:
    if pid <= 0:
        raise FrontdoorError("launch_session_process_identity_mismatch")
    proc_link = Path(f"/proc/{pid}/exe")
    if proc_link.exists():
        try:
            return proc_link.resolve(strict=True)
        except OSError as exc:
            raise FrontdoorError("launch_session_process_executable_unavailable") from exc
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buffer = ctypes.create_string_buffer(4096)
        length = library.proc_pidpath(pid, buffer, len(buffer))
    except (OSError, AttributeError) as exc:
        raise FrontdoorError("launch_session_process_executable_unavailable") from exc
    if length <= 0:
        raise FrontdoorError("launch_session_process_executable_unavailable")
    try:
        return Path(os.fsdecode(buffer.value)).resolve(strict=True)
    except OSError as exc:
        raise FrontdoorError("launch_session_process_executable_unavailable") from exc


def live_parent_pid(pid: int) -> int:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            timeout=1,
            check=False,
        )
        parent = int(completed.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise FrontdoorError("launch_session_process_parent_unavailable") from exc
    if completed.returncode or parent <= 0:
        raise FrontdoorError("launch_session_process_parent_unavailable")
    return parent


class HostLaunchSessionVerifier:
    """Reopen and revalidate root-owned launch records by Codex parent PID."""

    def __init__(
        self,
        session_directory: Path = LAUNCH_SESSION_DIRECTORY,
        *,
        expected_owner_uid: int = 0,
        record_mode: int = 0o644,
        commissioning_directory: Path | None = None,
    ) -> None:
        self.session_directory = session_directory
        self.expected_owner_uid = expected_owner_uid
        self.record_mode = record_mode
        self.commissioning_directory = (
            commissioning_directory
            if commissioning_directory is not None
            else session_directory.parent / "commissioning-launches"
        )

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            raw = _trusted_root_artifact_bytes(
                path,
                expected_owner_uid=self.expected_owner_uid,
                expected_mode=self.record_mode,
                max_bytes=128 * 1024,
            )
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FrontdoorError("launch_session_record_invalid") from exc
        normalized = normalize_launch_session_identity(value)
        expected_reference = f"launch-sessions/{path.name}"
        if normalized["record_reference"] != expected_reference:
            raise FrontdoorError("launch_session_record_reference_mismatch")
        return normalized

    def _normal_launch_argv_digest(self, identity: dict[str, Any]) -> str:
        try:
            import codex_main_agent_deployment as deployment

            argv = deployment.native_codex_argv(identity["native_realpath"])
        except Exception as exc:
            raise FrontdoorError("launch_session_normal_argv_unavailable") from exc
        return "sha256:" + stable_digest(argv)

    def _load_commissioning_launch(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        reference = identity.get("commissioning_launch_reference")
        if reference is None:
            path = self.commissioning_directory / f"{identity['session_id']}.json"
        else:
            path = self.commissioning_directory / Path(str(reference)).name
        if not path.exists():
            return None
        try:
            raw = _trusted_root_artifact_bytes(
                path,
                expected_owner_uid=self.expected_owner_uid,
                expected_mode=self.record_mode,
                max_bytes=64 * 1024,
            )
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FrontdoorError("commissioning_launch_record_invalid") from exc
        return normalize_commissioning_launch(value)

    def _validate_commissioning_launch(
        self,
        identity: dict[str, Any],
        *,
        now_epoch: float,
    ) -> dict[str, Any] | None:
        companion = self._load_commissioning_launch(identity)
        normal_digest = self._normal_launch_argv_digest(identity)
        session_kind = identity["session_kind"]
        if session_kind == "standard":
            if not hmac.compare_digest(
                str(identity["launch_argv_digest"]), normal_digest
            ):
                raise FrontdoorError("standard_launch_session_argv_mismatch")
            if companion is not None:
                raise FrontdoorError("commissioning_launch_companion_unexpected")
            return None
        if session_kind != "commissioning" or companion is None:
            raise FrontdoorError("commissioning_launch_companion_required")
        expected_reference = f"commissioning-launches/{identity['session_id']}.json"
        if (
            companion["state"] != "active"
            or companion["session_id"] != identity["session_id"]
            or companion["profile_id"] != identity["profile_id"]
            or companion["record_reference"] != expected_reference
            or identity["commissioning_launch_reference"] != expected_reference
            or identity["commissioning_launch_digest"] != companion["binding_digest"]
            or companion["launch_session_digest"] != identity["record_digest"]
            or companion["probe_argv_digest"] != identity["launch_argv_digest"]
        ):
            raise FrontdoorError("commissioning_launch_binding_mismatch")
        issued = _parse_host_timestamp(
            companion["issued_at"], label="commissioning_launch.issued_at"
        )
        valid = _parse_host_timestamp(
            companion["valid_until"], label="commissioning_launch.valid_until"
        )
        session_issued = _parse_host_timestamp(
            identity["issued_at"], label="launch_session.issued_at"
        )
        session_valid = _parse_host_timestamp(
            identity["valid_until"], label="launch_session.valid_until"
        )
        if not (
            session_issued <= issued <= now_epoch < valid <= session_valid
        ):
            raise FrontdoorError("commissioning_launch_expired_or_not_yet_valid")
        return companion

    def _validate_live(
        self,
        identity: dict[str, Any],
        *,
        subject_pid: int,
        profile_id: str,
        principal_id: str,
        workspace_id: str,
        checkout_identity: dict[str, Any],
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        normalized_checkout = validate_checkout_identity(checkout_identity)
        expected = {
            "subject_pid": subject_pid,
            "profile_id": profile_id,
            "principal_id": principal_id,
            "workspace_id": workspace_id,
            "checkout_realpath": normalized_checkout["checkout_realpath"],
            "checkout_identity_digest": normalized_checkout["identity_digest"],
            "status": "active",
        }
        if any(identity.get(field) != value for field, value in expected.items()):
            raise FrontdoorError("launch_session_subject_mismatch")
        current_time = time.time() if now_epoch is None else now_epoch
        if not (
            _parse_host_timestamp(identity["issued_at"], label="launch_session.issued_at")
            <= current_time
            < _parse_host_timestamp(identity["valid_until"], label="launch_session.valid_until")
        ):
            raise FrontdoorError("launch_session_expired_or_not_yet_valid")
        commissioning = self._validate_commissioning_launch(
            identity,
            now_epoch=current_time,
        )
        for pid_field, token_field in (
            ("subject_pid", "process_start_token"),
            ("supervisor_pid", "supervisor_start_token"),
        ):
            pid = int(identity[pid_field])
            if not run_lock.process_is_alive(pid) or not hmac.compare_digest(
                run_lock.process_start_token(pid), str(identity[token_field])
            ):
                raise FrontdoorError("launch_session_process_identity_mismatch")
        if live_parent_pid(int(identity["subject_pid"])) != int(identity["supervisor_pid"]):
            raise FrontdoorError("launch_session_supervisor_ancestry_mismatch")
        try:
            recorded_native = Path(str(identity["native_realpath"])).resolve(strict=True)
        except OSError as exc:
            raise FrontdoorError("launch_session_artifact_unavailable") from exc
        if live_process_executable(int(identity["subject_pid"])) != recorded_native:
            raise FrontdoorError("launch_session_live_executable_mismatch")
        for path_field, digest_field in (
            ("native_realpath", "native_digest"),
            ("profile_realpath", "profile_digest"),
        ):
            path = Path(str(identity[path_field]))
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise FrontdoorError("launch_session_artifact_unavailable") from exc
            if resolved != path or not hmac.compare_digest(
                _secure_file_digest(
                    path,
                    allowed_owner_uids={
                        0,
                        Path(normalized_checkout["checkout_realpath"]).stat().st_uid,
                    },
                ),
                str(identity[digest_field]),
            ):
                raise FrontdoorError("launch_session_artifact_identity_mismatch")
        if commissioning is not None:
            reopened_commissioning = self._validate_commissioning_launch(
                identity,
                now_epoch=current_time,
            )
            if reopened_commissioning != commissioning:
                raise FrontdoorError("commissioning_launch_record_drift")
        return identity

    def verify_parent_session(
        self,
        *,
        subject_pid: int,
        profile_id: str,
        principal_id: str,
        workspace_id: str,
        checkout_identity: dict[str, Any],
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        try:
            candidates = [
                self._load(path)
                for path in sorted(self.session_directory.glob("*.json"))
                if path.is_file() and not path.is_symlink()
            ]
        except OSError as exc:
            raise FrontdoorError("launch_session_directory_unavailable") from exc
        current_token = run_lock.process_start_token(subject_pid)
        if not current_token:
            raise FrontdoorError("launch_session_process_identity_mismatch")
        matches = [
            item
            for item in candidates
            if item.get("subject_pid") == subject_pid
            and hmac.compare_digest(
                str(item.get("process_start_token") or ""),
                current_token,
            )
        ]
        if len(matches) != 1:
            raise FrontdoorError("launch_session_parent_record_not_unique")
        return self._validate_live(
            matches[0],
            subject_pid=subject_pid,
            profile_id=profile_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            checkout_identity=checkout_identity,
            now_epoch=now_epoch,
        )

    def revalidate(
        self,
        identity: dict[str, Any],
        *,
        checkout_identity: dict[str, Any],
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_launch_session_identity(identity)
        path = self.session_directory / Path(str(normalized["record_reference"])).name
        reopened = self._load(path)
        if reopened != normalized:
            raise FrontdoorError("launch_session_record_drift")
        return self._validate_live(
            reopened,
            subject_pid=int(normalized["subject_pid"]),
            profile_id=str(normalized["profile_id"]),
            principal_id=str(normalized["principal_id"]),
            workspace_id=str(normalized["workspace_id"]),
            checkout_identity=checkout_identity,
            now_epoch=now_epoch,
        )


def safe_id(value: str) -> str:
    allowed = [
        char
        if char.isascii() and (char.isalnum() or char in {"-", "_", "."})
        else "-"
        for char in value
    ]
    compact = "".join(allowed).strip(".-")
    return validate_artifact_id(compact[:96] or "anonymous", "safe_id")


def validate_artifact_id(value: str, label: str) -> str:
    try:
        safe_value = safe_paths.safe_component(value, label=label)
    except safe_paths.SafePathError as exc:
        raise FrontdoorError(
            f"{label} must match {SAFE_ID_RE.pattern} and cannot contain path separators"
        ) from exc
    if not SAFE_ID_RE.fullmatch(safe_value):
        raise FrontdoorError(
            f"{label} must match {SAFE_ID_RE.pattern} and cannot contain path separators"
        )
    if ".." in safe_value.split("."):
        raise FrontdoorError(f"{label} cannot contain path traversal segments")
    return safe_value


def make_principal(
    principal_type: str,
    principal_id: str,
    *,
    authn_method: str = "local_cli",
) -> dict[str, str]:
    return {
        "principal_type": principal_type,
        "principal_id": principal_id,
        "authn_method": authn_method,
    }


def bridge_principal(
    frontdoor: str,
    chat_session_id: str = "",
    *,
    principal_id: str = "",
) -> dict[str, str]:
    """Return a bridge principal.

    Installed frontends must supply ``principal_id`` from host launch
    configuration. ``chat_session_id`` remains a correlation label only.  The
    local fallback is retained for non-installed CLI fixtures; it is stable per
    frontend and never derives authorization from the chat correlation label.
    """

    if principal_id:
        validate_artifact_id(principal_id, "principal_id")
        return make_principal(
            BRIDGE_PRINCIPAL_TYPE,
            principal_id,
            authn_method="installed_frontend_profile",
        )
    bridge_id = f"{frontdoor}:local-bridge"
    return make_principal(BRIDGE_PRINCIPAL_TYPE, bridge_id, authn_method="main_agent_bridge")


def default_manual_principal() -> dict[str, str]:
    return make_principal("manual_operator", "manual-cli", authn_method="local_cli")


def redacted_principal(principal: dict[str, Any]) -> dict[str, str]:
    return {
        "principal_type": str(principal.get("principal_type") or "unknown"),
        "principal_id": str(principal.get("principal_id") or "unknown"),
        "authn_method": str(principal.get("authn_method") or "unknown"),
    }


def signing_key_path(state_root: Path, principal: dict[str, Any]) -> Path:
    principal_id = str(principal.get("principal_id") or "anonymous")
    digest = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:24]
    return state_paths(state_root)["signing_keys"] / f"{safe_id(str(principal.get('principal_type') or 'principal'))}-{digest}.key"


def principal_key(state_root: Path, principal: dict[str, Any]) -> bytes:
    path = signing_key_path(state_root, principal)
    try:
        run_store.create_private_file(
            path,
            (secrets.token_hex(32) + "\n").encode("utf-8"),
        )
    except run_store.RunStoreError as exc:
        raise FrontdoorError("principal signing key cannot be created safely") from exc
    return read_private_file_text(path, label="principal signing key").encode("utf-8")


def sign_transition(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> dict[str, str]:
    material = {
        "principal": redacted_principal(principal),
        "transition": transition,
        "subject": subject,
    }
    signature_material = {
        "algorithm": TRANSITION_SIGNATURE_ALGORITHM,
        "material": material,
    }
    keyed_digest = hmac.new(
        principal_key(state_root, principal),
        canonical_json(signature_material),
        hashlib.sha256,
    ).digest()
    signature = hashlib.new("sha256", keyed_digest).hexdigest()
    return {
        "algorithm": TRANSITION_SIGNATURE_ALGORITHM,
        "signature": "sha256:" + signature,
        "signed_at": now_iso(),
    }


def append_audit_event(
    *,
    state_root: Path,
    event_type: str,
    principal: dict[str, Any],
    subject: dict[str, Any],
    outcome: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "audit_event_version": "1",
        "event_id": "evt-" + stable_digest(
            {
                "event_type": event_type,
                "principal": redacted_principal(principal),
                "subject": subject,
                "created_at": time.time_ns(),
            }
        )[:20],
        "created_at": now_iso(),
        "event_type": event_type,
        "principal": redacted_principal(principal),
        "subject": subject,
        "outcome": outcome,
        "details": details or {},
    }
    path = state_paths(state_root)["audit"] / "events.jsonl"
    encoded_size = len(
        (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    )
    metadata = state_file_stat(path)
    current_size = metadata.st_size if metadata is not None else 0
    if current_size and current_size + encoded_size > BRIDGE_AUDIT_ROTATE_BYTES:
        rotated = path.with_name(f"events.{time.time_ns()}.jsonl")
        if state_file_exists(rotated):
            raise FrontdoorError("audit rotation conflict")
        rotate_state_file(path, rotated)
    run_store.append_json_line(
        path,
        event,
        max_file_bytes=BRIDGE_AUDIT_ROTATE_BYTES + 1024 * 1024,
    )
    return event


def assert_allowed_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    allowed_types: set[str],
    transition: str,
    subject: dict[str, Any],
    blocked_reason: str,
) -> dict[str, str]:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type not in allowed_types:
        append_audit_event(
            state_root=state_root,
            event_type=transition,
            principal=principal,
            subject=subject,
            outcome="blocked",
            details={"reason": blocked_reason, "principal_type": principal_type},
        )
        raise FrontdoorError(f"{blocked_reason}: {principal_type}")
    return sign_transition(
        state_root=state_root,
        principal=principal,
        transition=transition,
        subject=subject,
    )


def execution_principal_blocked_reason(principal: dict[str, Any]) -> str:
    return (
        "bridge principal cannot perform execution transition"
        if principal.get("principal_type") == BRIDGE_PRINCIPAL_TYPE
        else "unsupported execution principal"
    )


def assert_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> dict[str, str]:
    return assert_allowed_principal(
        state_root=state_root,
        principal=principal,
        allowed_types=EXECUTION_PRINCIPAL_TYPES,
        transition=transition,
        subject=subject,
        blocked_reason=execution_principal_blocked_reason(principal),
    )


def precheck_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> None:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type in EXECUTION_PRINCIPAL_TYPES:
        return
    blocked_reason = execution_principal_blocked_reason(principal)
    append_audit_event(
        state_root=state_root,
        event_type=transition,
        principal=principal,
        subject=subject,
        outcome="blocked",
        details={"reason": blocked_reason, "principal_type": principal_type},
    )
    raise FrontdoorError(f"{blocked_reason}: {principal_type}")


def assert_workflow_definition_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    subject: dict[str, Any],
) -> dict[str, str]:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type not in WORKFLOW_DEFINITION_PRINCIPAL_TYPES:
        append_audit_event(
            state_root=state_root,
            event_type="workflow_definition_change",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details={"reason": "workflow_definitions_are_human_deploy_only"},
        )
        raise FrontdoorError("workflow definition changes require human-owned deploy/review path")
    return sign_transition(
        state_root=state_root,
        principal=principal,
        transition="workflow_definition_change",
        subject=subject,
    )


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "requests": state_root / "requests",
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "adapter_requests": state_root / "adapter-requests",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
        "transitions": state_root / "transitions",
        "envelopes": state_root / "envelopes",
        "audit": state_root / "audit",
        "idempotency": state_root / "idempotency",
        "bridge_transactions": state_root / "bridge-transactions",
        "bridge_rate_limits": state_root / "bridge-rate-limits",
        "acks": state_root / "acks",
        "signing_keys": state_root / "principal-keys",
        "channel_tokens": state_root / "channel-tokens",
        "child_thread_actions": state_root / "child-thread-actions",
        "worker_capabilities": state_root / "worker-capabilities",
        "worker_executions": state_root / "worker-executions",
        "worker_evidence": state_root / "worker-evidence",
    }


def record_run_link_status(state_root: Path, run: dict[str, Any]) -> str:
    try:
        path = task_state_bridge.record_run_link(state_root, run)
    except Exception as exc:  # defensive isolation: view refresh must not fail transitions
        return f"error:{type(exc).__name__}:{exc}"
    if path is None:
        return "skipped:no_session"
    return f"linked:{path}"


def task_view(
    *,
    state_root: Path,
    task_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(task_id, "task_id")
    actor = principal or default_manual_principal()
    payload = task_state_bridge.task_view_payload(state_root, task_id)
    append_audit_event(
        state_root=state_root,
        event_type="task_view",
        principal=actor,
        subject={"task_id": task_id},
        outcome="ok",
        details={
            "run_count": len(payload.get("runs") or []),
            "queue_evidence_count": len(payload.get("queue_evidence") or []),
        },
    )
    return payload


def state_permission_repair(
    *,
    state_root: Path,
    apply: bool,
    principal: dict[str, Any],
) -> dict[str, Any]:
    """Audit legacy state modes and repair only after an explicit apply gate."""

    actor = redacted_principal(principal)
    if actor["principal_type"] != "manual_operator" or actor["authn_method"] != "local_cli":
        raise FrontdoorError("state_permission_operator_required")
    try:
        report = host_state_root.audit_or_repair_state_permissions(
            state_root,
            apply=apply,
        )
    except host_state_root.HostStateRootError as exc:
        raise FrontdoorError(str(exc)) from exc
    report_material = {
        "permission_report_version": "1",
        "generated_at": now_iso(),
        **report,
    }
    report_digest = "sha256:" + stable_digest(report_material)
    evidence: dict[str, Any] = {
        "delivery": "stdout_json",
        "report_digest": report_digest,
        "durable_report_path": None,
        "audit_event_digest": None,
    }
    if apply:
        report_id = f"permission-repair-{time.time_ns()}"
        report_path = state_root / "permission-repair-evidence" / f"{report_id}.json"
        durable_report = {**report_material, "report_digest": report_digest}
        run_store.atomic_write_json(report_path, durable_report)
        event = append_audit_event(
            state_root=state_root,
            event_type="state_permission_repair",
            principal=actor,
            subject={"state_root": str(state_root), "report_id": report_id},
            outcome="ok",
            details={
                "report_digest": report_digest,
                "finding_count": report["finding_count"],
                "repaired_count": report["repaired_count"],
            },
        )
        evidence.update(
            {
                "delivery": "durable_private_report_and_audit",
                "durable_report_path": str(report_path),
                "audit_event_digest": "sha256:" + stable_digest(event),
            }
        )
    return {
        "schema_version": 1,
        "decision": report["decision"],
        "permission_report": report_material,
        "evidence": evidence,
        "next_action": (
            "rerun_with_explicit_apply"
            if not apply and report["decision"] == "repair_required"
            else "none"
        ),
    }


def request_path(state_root: Path, request_id: str) -> Path:
    return state_paths(state_root)["requests"] / f"{validate_artifact_id(request_id, 'request_id')}.json"


def envelope_dir(state_root: Path, request_id: str) -> Path:
    return state_paths(state_root)["envelopes"] / validate_artifact_id(request_id, "request_id")


def list_envelope_snapshots(state_root: Path, request_id: str) -> list[str]:
    directory = envelope_dir(state_root, request_id)
    return [str(path) for path in list_state_files(directory, suffix=".json")]


def snapshot_envelope(state_root: Path, request_id: str, envelope: dict[str, Any]) -> Path:
    directory = envelope_dir(state_root, request_id)
    run_store.ensure_private_directory(directory)
    existing = list_state_files(directory, suffix=".json")
    if existing:
        try:
            latest = read_json(existing[-1])
        except FrontdoorError:
            latest = {}
        if isinstance(latest.get("envelope"), dict) and stable_digest(latest["envelope"]) == stable_digest(envelope):
            return existing[-1]
    status = safe_id(str(envelope.get("activation_status") or envelope.get("request_status") or "unknown"))
    path = directory / f"{len(existing) + 1:04d}-{status}.json"
    payload = {
        "snapshot_version": "1",
        "request_id": request_id,
        "written_at": now_iso(),
        "envelope": envelope,
    }
    write_json(path, payload)
    return path


def run_path(state_root: Path, run_id: str) -> Path:
    return state_paths(state_root)["runs"] / f"{validate_artifact_id(run_id, 'run_id')}.json"


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["work_orders"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}.json"
    )


def report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["reports"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-external-review-report.json"
    )


def provider_evidence_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-provider-evidence.json"
    )


def provider_transcript_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-provider-transcript.json"
    )


def adapter_request_path(state_root: Path, run_id: str, step_id: str, adapter_id: str) -> Path:
    return state_artifact_path(
        state_root,
        "adapter_requests",
        validate_artifact_id(run_id, "run_id"),
        f"{validate_artifact_id(step_id, 'step_id')}-{validate_artifact_id(adapter_id, 'adapter_id')}.json",
    )


def configured_state_root() -> Path:
    try:
        return host_state_root.configured_state_root()
    except host_state_root.HostStateRootError as exc:
        raise FrontdoorError(str(exc)) from exc


def trusted_state_root(requested: str | Path | None) -> Path:
    try:
        return host_state_root.resolve_configured_state_root(requested)
    except host_state_root.HostStateRootError as exc:
        raise FrontdoorError(str(exc)) from exc


def state_artifact_path(state_root: Path, category: str, *components: str) -> Path:
    root = state_root.resolve(strict=False)
    raw_base = state_paths(root)[category]
    if raw_base.is_symlink():
        raise FrontdoorError("state artifact category must not be a symlink")
    base = raw_base.resolve(strict=False)
    safe_components = [
        safe_paths.safe_component(component, label=f"{category}_component_{index}")
        for index, component in enumerate(components)
    ]
    candidate = base.joinpath(*safe_components).resolve(strict=False)
    try:
        base.relative_to(root)
        candidate.relative_to(root)
        candidate.relative_to(base)
    except ValueError as exc:
        raise FrontdoorError("state artifact path must stay within configured root") from exc
    return candidate


def resolve_contained_path(raw_path: str, *, parent: Path, label: str) -> Path:
    try:
        return safe_paths.confined_trusted_root_path(
            parent,
            raw_path,
            label=label,
            strict=False,
        )
    except safe_paths.SafePathError as exc:
        raise FrontdoorError(f"{label} must stay within repo_root") from exc


def channel_token_path(state_root: Path, channel: str) -> Path:
    if channel not in HTTP_CHANNEL_PRINCIPALS:
        raise FrontdoorError(f"unsupported channel: {channel}")
    safe_channel = validate_artifact_id(channel, "channel")
    return state_paths(state_root)["channel_tokens"] / f"{safe_channel}.token"


def read_private_file_text(path: Path, *, label: str) -> str:
    try:
        return run_store.read_bytes(path, max_bytes=64 * 1024).decode("utf-8").strip()
    except (UnicodeError, run_store.RunStoreError) as exc:
        raise FrontdoorError(f"{label} cannot be opened safely: {path}") from exc


def ensure_channel_token_file(state_root: Path, channel: str) -> None:
    path = channel_token_path(state_root, channel)
    try:
        run_store.create_private_file(
            path,
            (secrets.token_urlsafe(32) + "\n").encode("utf-8"),
        )
    except run_store.RunStoreError as exc:
        raise FrontdoorError("channel token cannot be created safely") from exc


def channel_token(state_root: Path, channel: str) -> str:
    ensure_channel_token_file(state_root, channel)
    path = channel_token_path(state_root, channel)
    return read_private_file_text(path, label="channel token")


def principal_from_authenticated_channel(
    state_root: Path,
    channel: str,
    token: str,
    *,
    bind_credential: bool = False,
) -> dict[str, str]:
    if channel not in HTTP_CHANNEL_PRINCIPALS:
        raise FrontdoorError(f"unsupported channel: {channel}")
    if not token:
        raise FrontdoorError("missing orchestrator channel token")
    expected = channel_token(state_root, channel)
    if not hmac.compare_digest(token, expected):
        raise FrontdoorError("invalid orchestrator channel token")
    principal_type, principal_id, authn_method = HTTP_CHANNEL_PRINCIPALS[channel]
    if bind_credential:
        if channel != "action_gateway":
            raise FrontdoorError("credential binding is limited to action_gateway")
        credential_binding = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        principal_id = f"scoped-worker-gateway:{credential_binding}"
    return make_principal(principal_type, principal_id, authn_method=authn_method)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _denylisted_ref_part(relative_path: Path) -> str | None:
    for part in relative_path.parts:
        lowered = part.lower()
        if lowered in REF_DENYLIST_NAMES:
            return part
        if (
            lowered.startswith(REF_DENYLIST_PREFIXES)
            or lowered.endswith(REF_DENYLIST_SUFFIXES)
            or any(marker in lowered for marker in REF_DENYLIST_SUBSTRINGS)
        ):
            return part
    return None


def validate_repo_full_name(value: Any) -> str:
    repo_full_name = str(value or "")
    if repo_full_name.count("/") != 1:
        raise FrontdoorError("repo_full_name must be owner/repo")
    owner, repo = repo_full_name.split("/", 1)
    if (
        not owner
        or not repo
        or len(owner) > MAX_REPO_FULL_NAME_COMPONENT_CHARS
        or len(repo) > MAX_REPO_FULL_NAME_COMPONENT_CHARS
        or any(char not in REPO_FULL_NAME_COMPONENT_CHARS for char in owner)
        or any(char not in REPO_FULL_NAME_COMPONENT_CHARS for char in repo)
    ):
        raise FrontdoorError("repo_full_name must be owner/repo")
    return repo_full_name


def _resolve_repo_path(raw: str, *, ref_root: Path, label: str) -> tuple[Path, Path]:
    if not isinstance(raw, str) or not raw.strip():
        raise FrontdoorError(f"{label} must be a non-empty string")
    if "\x00" in raw:
        raise FrontdoorError(f"{label} cannot contain NUL bytes")
    root = ref_root.expanduser().resolve()
    try:
        resolved = safe_paths.confined_trusted_root_path(
            root,
            raw,
            label=label,
            strict=True,
        )
    except FileNotFoundError as exc:
        raise FrontdoorError(f"{label} does not exist: {raw}") from exc
    except safe_paths.SafePathError as exc:
        raise FrontdoorError(f"{label} outside approved ref root: {raw}") from exc
    relative = resolved.relative_to(root)
    denied_part = _denylisted_ref_part(relative)
    if denied_part:
        raise FrontdoorError(f"{label} denylisted path component: {denied_part}")
    return resolved, relative


def resolve_context_refs(refs: list[str], *, ref_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    if len(refs) > MAX_CONTEXT_REF_COUNT:
        raise FrontdoorError(f"too many context refs: {len(refs)} > {MAX_CONTEXT_REF_COUNT}")
    resolved_refs: list[dict[str, Any]] = []
    total_size = 0
    root = ref_root.expanduser().resolve()
    for raw in refs:
        resolved, relative = _resolve_repo_path(raw, ref_root=root, label="context ref")
        if not resolved.is_file():
            raise FrontdoorError(f"context ref must be a file: {raw}")
        size = resolved.stat().st_size
        if size > MAX_CONTEXT_REF_FILE_BYTES:
            raise FrontdoorError(
                f"context ref exceeds file size cap: {relative.as_posix()} > {MAX_CONTEXT_REF_FILE_BYTES}"
            )
        total_size += size
        if total_size > MAX_CONTEXT_REF_TOTAL_BYTES:
            raise FrontdoorError(f"context refs exceed total size cap: {total_size} > {MAX_CONTEXT_REF_TOTAL_BYTES}")
        resolved_refs.append(
            {
                "type": "repo_file",
                "original": raw,
                "path": relative.as_posix(),
                "size_bytes": size,
                "digest": file_sha256(resolved),
            }
        )
    return resolved_refs


def resolve_allowed_paths(paths: list[str], *, ref_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    if len(paths) > MAX_ALLOWED_PATH_COUNT:
        raise FrontdoorError(f"too many allowed paths: {len(paths)} > {MAX_ALLOWED_PATH_COUNT}")
    resolved_paths: list[dict[str, Any]] = []
    root = ref_root.expanduser().resolve()
    for raw in paths:
        resolved, relative = _resolve_repo_path(raw, ref_root=root, label="allowed path")
        resolved_paths.append(
            {
                "type": "repo_dir" if resolved.is_dir() else "repo_file",
                "original": raw,
                "path": relative.as_posix(),
            }
        )
    return resolved_paths


def resolved_ref_paths(resolved_refs: list[dict[str, Any]]) -> list[str]:
    return [str(item["path"]) for item in resolved_refs]


def bounded_context(
    refs: list[str],
    allowed_paths: list[str],
    *,
    require_refs: bool = False,
    ref_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if require_refs and not refs:
        raise FrontdoorError("refs must be non-empty")
    resolved_refs = resolve_context_refs(refs, ref_root=ref_root) if refs else []
    resolved_allowed_paths = resolve_allowed_paths(allowed_paths, ref_root=ref_root) if allowed_paths else []
    return {
        "requested_context_refs": list(refs),
        "context_refs": resolved_ref_paths(resolved_refs),
        "resolved_context_refs": resolved_refs,
        "requested_allowed_paths": list(allowed_paths),
        "allowed_paths": resolved_ref_paths(resolved_allowed_paths),
        "resolved_allowed_paths": resolved_allowed_paths,
    }


def approval_ref_summaries(resolved_refs: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in resolved_refs:
        if not isinstance(item, dict):
            continue
        summary = {
            "type": str(item.get("type") or "repo_file"),
            "path": str(item.get("path") or ""),
        }
        if "size_bytes" in item:
            summary["size_bytes"] = item["size_bytes"]
        if "digest" in item:
            summary["digest"] = item["digest"]
        summaries.append(summary)
    return summaries


def approval_allowed_path_summaries(resolved_paths: list[Any]) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for item in resolved_paths:
        if not isinstance(item, dict):
            continue
        summaries.append(
            {
                "type": str(item.get("type") or "repo_path"),
                "path": str(item.get("path") or ""),
            }
        )
    return summaries


def ref_integrity_view(refs: list[Any]) -> list[dict[str, Any]]:
    view: list[dict[str, Any]] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        view.append(
            {
                "path": str(item.get("path") or ""),
                "size_bytes": item.get("size_bytes"),
                "digest": str(item.get("digest") or ""),
            }
        )
    return view


def recorded_checkout_ref_root(
    request_record: dict[str, Any],
    *,
    require_unchanged_identity: bool,
) -> Path:
    """Return the recorded active checkout after host registration validation."""

    recorded = request_record.get("checkout_identity")
    if not isinstance(recorded, dict) or not recorded:
        return REPO_ROOT
    workspace_id = request_record.get("workspace_id")
    primary = recorded.get("managed_primary_realpath")
    checkout = recorded.get("checkout_realpath")
    if not all(isinstance(value, str) and value for value in (workspace_id, primary, checkout)):
        raise FrontdoorError("recorded checkout identity is invalid")
    current = resolve_checkout_identity(
        workspace_id=str(workspace_id),
        managed_primary=str(primary),
        checkout_root=str(checkout),
    )
    if require_unchanged_identity and current.get("identity_digest") != recorded.get("identity_digest"):
        raise FrontdoorError("checkout identity changed after approval")
    return Path(current["checkout_realpath"])


def verified_context_refs_for_work_order(request_record: dict[str, Any]) -> list[dict[str, Any]]:
    approved_refs = request_record.get("resolved_context_refs")
    requested_refs = list(request_record.get("requested_context_refs") or request_record.get("context_refs") or [])
    ref_root = recorded_checkout_ref_root(
        request_record,
        require_unchanged_identity=True,
    )
    current_refs = resolve_context_refs(requested_refs, ref_root=ref_root) if requested_refs else []
    if isinstance(approved_refs, list) and approved_refs:
        if ref_integrity_view(current_refs) != ref_integrity_view(approved_refs):
            raise FrontdoorError("context refs changed after approval")
    return current_refs


def stable_run_id(request_id: str, workflow_id: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{workflow_id}".encode("utf-8")).hexdigest()[:12]
    return f"run-{digest}"


def policy_digest(envelope: dict[str, Any]) -> str:
    policy_material = {
        "policy": envelope.get("policy"),
        "activation_scope": envelope.get("activation_scope"),
        "context_scope": envelope.get("context_scope"),
        "workflow_selection": envelope.get("workflow_selection"),
    }
    encoded = json.dumps(policy_material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sanitize_activation_for_run(envelope: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "activation_version",
        "activation_source",
        "activation_status",
        "approved_by",
        "approved_at",
        "workflow_selection",
        "classification_provenance",
        "context_scope",
        "activation_scope",
        "next_action",
    }
    return {key: envelope[key] for key in allowed if key in envelope}


def load_registry() -> dict[str, Any]:
    return workflow_selector.load_registry()


def load_template(workflow_id: str) -> dict[str, Any]:
    registry = load_registry()
    for entry in registry.get("templates", []):
        if entry.get("workflow_id") != workflow_id:
            continue
        path = REPO_ROOT / entry["path"]
        data = load_repo_json(path)
        if not isinstance(data, dict):
            raise FrontdoorError(f"expected object json: {path}")
        return data
    raise FrontdoorError(f"active workflow template not found: {workflow_id}")


def proposed_request(
    *,
    state_root: Path,
    task_id: str,
    request_id: str,
    user_prompt: str,
    refs: list[str],
    classification: dict[str, Any] | None,
    allowed_paths: list[str],
    expires_at: str,
    frontdoor: str,
    chat_session_id: str,
    principal: dict[str, Any] | None = None,
    surface_registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    validate_artifact_id(task_id, "task_id")
    actor = principal or default_manual_principal()
    surface_identity = resolve_surface_identity(
        frontdoor,
        registry=surface_registry,
        launch_session_present=False,
    )
    bounded = bounded_context(refs, allowed_paths)
    path = request_path(state_root, request_id)
    existing_record = read_json(path) if state_file_exists(path) else None
    if existing_record is not None:
        if classification is None:
            if existing_record.get("status") == "waiting_human" and isinstance(existing_record.get("proposal"), dict):
                append_audit_event(
                    state_root=state_root,
                    event_type="request_waiting_human",
                    principal=actor,
                    subject={"request_id": request_id, "task_id": task_id},
                    outcome="replayed",
                    details={"reason": "request_id_already_waiting_human"},
                )
                return existing_record["proposal"]
            append_audit_event(
                state_root=state_root,
                event_type="request_waiting_human",
                principal=actor,
                subject={"request_id": request_id, "task_id": task_id},
                outcome="blocked",
                details={"reason": "request_id_conflict"},
            )
            raise FrontdoorError("request_id conflict for propose")
        if existing_record.get("status") != "waiting_human" or existing_record.get("approved_activation"):
            append_audit_event(
                state_root=state_root,
                event_type="request_proposed",
                principal=actor,
                subject={"request_id": request_id, "task_id": task_id},
                outcome="blocked",
                details={
                    "reason": "request_id_conflict",
                    "existing_status": existing_record.get("status"),
                },
            )
            raise FrontdoorError("request_id conflict for propose")
        immutable_mismatches = []
        expected = {
            "task_id": task_id,
            "user_prompt": user_prompt,
            "context_refs": bounded["context_refs"],
            "allowed_paths": bounded["allowed_paths"],
            "expires_at": expires_at,
        }
        existing_surface = existing_record.get("surface_identity")
        if "surface_identity" in existing_record:
            if not isinstance(existing_surface, dict) or surface_contract_binding(
                existing_surface
            ) != surface_contract_binding(surface_identity):
                immutable_mismatches.append("surface_identity")
        else:
            legacy_requester = existing_record.get("requester")
            legacy_frontend = (
                str(legacy_requester.get("frontdoor") or "")
                if isinstance(legacy_requester, dict)
                else ""
            )
            if legacy_frontend != frontdoor:
                immutable_mismatches.append("surface_identity")
        for key, value in expected.items():
            if existing_record.get(key) != value:
                immutable_mismatches.append(key)
        if immutable_mismatches:
            append_audit_event(
                state_root=state_root,
                event_type="request_proposed",
                principal=actor,
                subject={"request_id": request_id, "task_id": task_id},
                outcome="blocked",
                details={
                    "reason": "request_id_conflict",
                    "immutable_mismatches": immutable_mismatches,
                },
            )
            raise FrontdoorError("request_id conflict for propose")
    if classification is None:
        payload = {
            "schema_version": 1,
            "decision": "waiting_human",
            "request_status": "waiting_human",
            "reason": "typed_classification_required",
            "task_id": task_id,
            "request_id": request_id,
            "next_action": "ask_human",
            "surface_identity": surface_identity,
        }
        record = {
            "request_version": "1",
            "task_id": task_id,
            "request_id": request_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "user_prompt": user_prompt,
            **bounded,
            "expires_at": expires_at,
            "classification": None,
            "requester": requester(frontdoor, chat_session_id),
            "surface_identity": surface_identity,
            "status": "waiting_human",
            "proposal": payload,
        }
        write_json(path, record)
        append_audit_event(
            state_root=state_root,
            event_type="request_waiting_human",
            principal=actor,
            subject={"request_id": request_id, "task_id": task_id},
            outcome="ok",
            details={"reason": "typed_classification_required"},
        )
        return payload

    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source="frontdoor_prompt",
        task_id=task_id,
        request_id=request_id,
        refs=list(bounded["context_refs"]),
        allowed_paths=list(bounded["allowed_paths"]),
        expires_at=expires_at,
    )
    if existing_record is None:
        record = {
            "request_version": "1",
            "task_id": task_id,
            "request_id": request_id,
            "created_at": now_iso(),
            "user_prompt": user_prompt,
            **bounded,
            "expires_at": expires_at,
            "requester": requester(frontdoor, chat_session_id),
            "surface_identity": surface_identity,
        }
    else:
        record = dict(existing_record)
    record.update(
        {
            "updated_at": now_iso(),
            "classification": classification,
            "status": envelope["activation_status"],
            "proposal": envelope,
        }
    )
    attach_approval_summary(record)
    write_json(path, record)
    snapshot_path = snapshot_envelope(state_root, request_id, envelope)
    append_audit_event(
        state_root=state_root,
        event_type="request_proposed",
        principal=actor,
        subject={"request_id": request_id, "task_id": task_id},
        outcome="ok" if envelope["activation_status"] != "blocked" else "blocked",
        details={
            "request_status": envelope["activation_status"],
            "workflow_selection": envelope.get("workflow_selection"),
        },
    )
    return {
        "schema_version": 1,
        "decision": "blocked" if envelope["activation_status"] == "blocked" else "ok",
        "request_status": envelope["activation_status"],
        "request_path": str(path),
        "envelope_snapshot_path": str(snapshot_path),
        "activation": envelope,
        "approval": record.get("approval"),
        "surface_identity": record.get("surface_identity", surface_identity),
    }


def requester(frontdoor: str, chat_session_id: str = "") -> dict[str, str]:
    payload = {"frontdoor": frontdoor}
    if chat_session_id:
        payload["chat_session_id"] = chat_session_id
    return payload


def approval_action_id(record: dict[str, Any]) -> str:
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    material = {
        "task_id": record.get("task_id"),
        "request_id": record.get("request_id"),
        "workflow_selection": proposal.get("workflow_selection"),
        "classification_provenance": proposal.get("classification_provenance"),
        "context_refs": record.get("context_refs") or [],
        "resolved_context_refs": record.get("resolved_context_refs") or [],
        "allowed_paths": record.get("allowed_paths") or [],
        "expires_at": record.get("expires_at"),
    }
    return "approve-" + stable_digest(material)[:20]


def approval_summary(record: dict[str, Any]) -> dict[str, Any]:
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    workflow_selection = proposal.get("workflow_selection") if isinstance(proposal.get("workflow_selection"), dict) else {}
    activation_scope = proposal.get("activation_scope") if isinstance(proposal.get("activation_scope"), dict) else {}
    allowed_ops = activation_scope.get("allowed_ops") if isinstance(activation_scope.get("allowed_ops"), dict) else {}
    denied_ops = sorted(op for op, allowed in allowed_ops.items() if allowed is False)
    checkout_identity = record.get("checkout_identity")
    approval_workspace_root = (
        str(checkout_identity.get("checkout_realpath"))
        if isinstance(checkout_identity, dict) and checkout_identity.get("checkout_realpath")
        else str(REPO_ROOT)
    )
    return {
        "approval_view_version": "1",
        "source": "orchestrator_structured_state",
        "main_agent_prose_used": False,
        "human_action_id": approval_action_id(record),
        "rate_limit": {
            "max_failed_attempts": MAX_APPROVAL_FAILURES,
            "failed_attempts": int((record.get("approval_rate_limit") or {}).get("failed_attempts") or 0),
        },
        "what_will_execute": {
            "workflow_id": workflow_selection.get("workflow_id"),
            "initial_step": workflow_selection.get("initial_step"),
            "permission_mode": "readonly",
            "step_budget": activation_scope.get("step_budget"),
            "context_refs": list(record.get("context_refs") or []),
            "resolved_context_refs": approval_ref_summaries(list(record.get("resolved_context_refs") or [])),
            "allowed_paths": list(record.get("allowed_paths") or []),
            "resolved_allowed_paths": approval_allowed_path_summaries(
                list(record.get("resolved_allowed_paths") or [])
            ),
            "denied_ops": denied_ops,
            "provider_adapter": "claude_headless_p0",
            "ref_boundary": {
                "workspace_root": approval_workspace_root,
                "max_ref_count": MAX_CONTEXT_REF_COUNT,
                "max_ref_file_bytes": MAX_CONTEXT_REF_FILE_BYTES,
                "max_ref_total_bytes": MAX_CONTEXT_REF_TOTAL_BYTES,
            },
        },
        "classification_provenance": proposal.get("classification_provenance"),
        "next_action": proposal.get("next_action"),
    }


def attach_approval_summary(record: dict[str, Any]) -> None:
    proposal = record.get("proposal")
    if isinstance(proposal, dict) and proposal.get("activation_status") == "proposed":
        record.setdefault("approval_rate_limit", {"failed_attempts": 0})
        record["approval"] = approval_summary(record)


def idempotency_path(state_root: Path, key: str) -> Path:
    return state_paths(state_root)["idempotency"] / f"key-{idempotency_key_digest(key).removeprefix('sha256:')}.json"


def bridge_idempotency_key_digest(
    key: str,
    surface_identity: dict[str, Any],
) -> str:
    """Bind a bridge idempotency key digest to the static surface descriptor."""

    descriptor_digest = normalize_sha256_digest(
        surface_identity.get("descriptor_digest"),
        "surface_identity.descriptor_digest",
    )
    return "sha256:" + stable_digest(
        {
            "key_digest": idempotency_key_digest(key),
            "surface_descriptor_digest": descriptor_digest,
        }
    )


def child_thread_idempotency_path(state_root: Path, key: str) -> Path:
    return (
        state_paths(state_root)["child_thread_actions"]
        / "idempotency"
        / f"key-{idempotency_key_digest(key).removeprefix('sha256:')}.json"
    )


def idempotency_key_digest(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def child_thread_action_path(state_root: Path, action_id: str) -> Path:
    return state_paths(state_root)["child_thread_actions"] / f"{validate_artifact_id(action_id, 'action_id')}.json"


def normalize_sha256_digest(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise FrontdoorError(f"{label} must be sha256:<64 lowercase hex>")
    return text


def validate_safe_branch_name(value: Any) -> str:
    branch = str(value or "")
    if not branch or branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".")):
        raise FrontdoorError("branch_name must be a non-empty safe git branch")
    if any(part in {"", ".", ".."} for part in branch.split("/")):
        raise FrontdoorError("branch_name cannot contain empty or traversal segments")
    if any(marker in branch for marker in ("..", "\\", " ", "~", "^", ":", "?", "*", "[", "@{")):
        raise FrontdoorError("branch_name contains unsupported characters")
    return branch


def normalize_issue_id(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"#?[0-9]{1,10}", text):
        raise FrontdoorError("issue_id must be a GitHub issue number")
    return text.lstrip("#")


def validate_child_thread_path(raw_path: Any, *, repo_root: Path, label: str, must_exist: bool = False) -> str:
    text = str(raw_path or "")
    if not text:
        raise FrontdoorError(f"{label} must be non-empty")
    if "\x00" in text:
        raise FrontdoorError(f"{label} cannot contain NUL bytes")
    resolved = resolve_contained_path(text, parent=repo_root, label=label)
    if must_exist and not resolved.is_file():
        raise FrontdoorError(f"{label} must exist as a file")
    return str(resolved)


def validate_child_pending_worktree_id(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", text):
        raise FrontdoorError("pending_worktree_id must be an opaque safe identifier")
    return text


def git_common_dir(repo_root: Path) -> Path | None:
    canonical_root = repo_root.resolve(strict=False)
    try:
        result = host_state_root.run_git_as_checkout_owner(
            canonical_root,
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, host_state_root.HostStateRootError):
        return None
    if result.returncode:
        return None
    return Path(result.stdout.strip()).resolve(strict=False)


def git_worktree_roots(repo_root: Path) -> set[Path]:
    canonical_root = repo_root.resolve(strict=False)
    try:
        result = host_state_root.run_git_as_checkout_owner(
            canonical_root,
            ["worktree", "list", "--porcelain", "-z"],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, host_state_root.HostStateRootError):
        return set()
    if result.returncode:
        return set()
    return {
        Path(field.removeprefix("worktree ")).resolve(strict=False)
        for field in result.stdout.split("\0")
        if field.startswith("worktree ")
    }


def is_approved_checkout(repo_root: Path) -> bool:
    expected = git_common_dir(REPO_ROOT)
    candidate = git_common_dir(repo_root)
    registered = git_worktree_roots(REPO_ROOT)
    return (
        expected is not None
        and candidate == expected
        and repo_root.resolve(strict=False) in registered
    )


def _git_bytes(repo_root: Path, args: list[str], *, timeout: float = 10.0) -> bytes:
    canonical_root = repo_root.resolve(strict=False)
    try:
        result = host_state_root.run_git_as_checkout_owner(
            canonical_root,
            args,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, host_state_root.HostStateRootError) as exc:
        raise FrontdoorError("unable to inspect host-pinned checkout") from exc
    if result.returncode:
        raise FrontdoorError("unable to inspect host-pinned checkout")
    return result.stdout


def _exact_checkout_realpath(raw_path: Path | str, label: str) -> Path:
    supplied = Path(raw_path).expanduser()
    if not supplied.is_absolute():
        raise FrontdoorError(f"{label} must be an absolute canonical realpath")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise FrontdoorError(f"{label} must identify an existing checkout") from exc
    if supplied != resolved:
        raise FrontdoorError(f"{label} must be the exact checkout realpath")
    if not resolved.is_dir():
        raise FrontdoorError(f"{label} must identify a directory")
    return resolved


def git_worktree_catalog(repo_root: Path) -> list[dict[str, str]]:
    raw = _git_bytes(repo_root, ["worktree", "list", "--porcelain", "-z"])
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_field in raw.split(b"\0"):
        if not raw_field:
            if current:
                entries.append(current)
                current = {}
            continue
        field = raw_field.decode("utf-8", errors="strict")
        key, separator, value = field.partition(" ")
        if key == "worktree" and current:
            entries.append(current)
            current = {}
        current[key] = value if separator else "true"
    if current:
        entries.append(current)
    normalized: list[dict[str, str]] = []
    for entry in entries:
        path_text = entry.get("worktree")
        if not path_text:
            continue
        normalized.append(
            {
                "worktree": str(Path(path_text).resolve(strict=False)),
                "HEAD": str(entry.get("HEAD") or ""),
                "branch": str(entry.get("branch") or "detached"),
                "locked": str(entry.get("locked") or ""),
                "prunable": str(entry.get("prunable") or ""),
            }
        )
    return sorted(normalized, key=lambda item: item["worktree"])


def _checkout_git_value(repo_root: Path, args: list[str], label: str) -> str:
    value = _git_bytes(repo_root, args).decode("utf-8", errors="strict").strip()
    if not value:
        raise FrontdoorError(f"host-pinned checkout has no {label}")
    return value


def _checkout_worktree_state_digest(repo_root: Path) -> str:
    status = _git_bytes(
        repo_root,
        ["status", "--porcelain=v2", "-z", "--untracked-files=all"],
    )
    diff = _git_bytes(
        repo_root,
        ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
        timeout=30.0,
    )
    untracked = _git_bytes(
        repo_root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    # Keep the launch/request boundary bounded. A checkout whose local diff is
    # too large must be made reviewable before it can be a managed frontend.
    if len(status) + len(diff) + len(untracked) > 64 * 1024 * 1024:
        raise FrontdoorError("host-pinned checkout state exceeds digest boundary")
    digest = hashlib.sha256()
    digest.update(b"status\0")
    digest.update(status)
    digest.update(b"diff\0")
    digest.update(diff)
    digest.update(b"untracked\0")
    total_bytes = len(status) + len(diff) + len(untracked)
    for raw_relative in sorted(item for item in untracked.split(b"\0") if item):
        relative = Path(os.fsdecode(raw_relative))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise FrontdoorError("host-pinned checkout has an unsafe untracked path")
        candidate = repo_root / relative
        digest.update(raw_relative)
        digest.update(b"\0")
        if candidate.is_symlink():
            target = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
            total_bytes += len(target)
            if total_bytes > 64 * 1024 * 1024:
                raise FrontdoorError("host-pinned checkout state exceeds digest boundary")
            digest.update(b"symlink\0")
            digest.update(target)
            continue
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_descriptor = os.open(candidate, flags)
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_descriptor)
                raise FrontdoorError("host-pinned checkout has an unsupported untracked entry")
            with os.fdopen(file_descriptor, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > 64 * 1024 * 1024:
                        raise FrontdoorError("host-pinned checkout state exceeds digest boundary")
                    digest.update(chunk)
        except OSError as exc:
            raise FrontdoorError("unable to hash host-pinned checkout state") from exc
    return "sha256:" + digest.hexdigest()


def resolve_checkout_identity(
    *,
    workspace_id: str,
    managed_primary: Path | str,
    checkout_root: Path | str,
) -> dict[str, str]:
    """Resolve a host-pinned primary or registered linked-worktree identity."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", workspace_id):
        raise FrontdoorError("workspace_id must be owner/repo")
    primary = _exact_checkout_realpath(managed_primary, "managed_primary")
    checkout = _exact_checkout_realpath(checkout_root, "checkout_root")
    primary_common = git_common_dir(primary)
    checkout_common = git_common_dir(checkout)
    if primary_common is None or checkout_common is None or primary_common != checkout_common:
        raise FrontdoorError("checkout_root must share the managed primary git-common-dir")
    catalog = git_worktree_catalog(primary)
    catalog_roots = {Path(entry["worktree"]) for entry in catalog}
    if primary not in catalog_roots:
        raise FrontdoorError("managed_primary is not registered in git worktree list")
    if checkout not in catalog_roots:
        raise FrontdoorError("checkout_root is not a host-registered git worktree")

    head_sha = _checkout_git_value(checkout, ["rev-parse", "--verify", "HEAD"], "HEAD")
    tree_sha = _checkout_git_value(checkout, ["rev-parse", "--verify", "HEAD^{tree}"], "tree")
    if not re.fullmatch(r"[0-9a-f]{40,64}", head_sha) or not re.fullmatch(
        r"[0-9a-f]{40,64}", tree_sha
    ):
        raise FrontdoorError("host-pinned checkout has invalid git object identity")
    try:
        branch = _checkout_git_value(
            checkout,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            "branch",
        )
    except FrontdoorError:
        branch = "detached"

    material = {
        "identity_version": "2",
        "workspace_id": workspace_id,
        "checkout_kind": (
            "managed_primary" if checkout == primary else "registered_linked_worktree"
        ),
        "checkout_realpath": str(checkout),
        "managed_primary_realpath": str(primary),
        "branch": branch,
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "git_common_dir_digest": "sha256:"
        + hashlib.sha256(str(primary_common).encode("utf-8")).hexdigest(),
        "worktree_state_digest": _checkout_worktree_state_digest(checkout),
    }
    return {**material, "identity_digest": "sha256:" + stable_digest(material)}


def validate_checkout_identity(value: Any) -> dict[str, str]:
    required = {
        "identity_version",
        "workspace_id",
        "checkout_kind",
        "checkout_realpath",
        "managed_primary_realpath",
        "branch",
        "head_sha",
        "tree_sha",
        "git_common_dir_digest",
        "worktree_state_digest",
        "identity_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise FrontdoorError("checkout_identity has an invalid field set")
    normalized = {key: str(value[key]) for key in required}
    if normalized["identity_version"] != "2":
        raise FrontdoorError("checkout_identity version is unsupported")
    if normalized["checkout_kind"] not in {
        "managed_primary",
        "registered_linked_worktree",
    }:
        raise FrontdoorError("checkout_identity kind is unsupported")
    for field in (
        "git_common_dir_digest",
        "worktree_state_digest",
        "identity_digest",
    ):
        normalize_sha256_digest(normalized[field], f"checkout_identity.{field}")
    material = {key: normalized[key] for key in required if key != "identity_digest"}
    expected = "sha256:" + stable_digest(material)
    if not hmac.compare_digest(normalized["identity_digest"], expected):
        raise FrontdoorError("checkout_identity digest mismatch")
    return normalized


def validate_child_thread_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise FrontdoorError("child_thread_plan must be object")
    required = {
        "task_id",
        "request_id",
        "issue_id",
        "repo_full_name",
        "repo_root",
        "base_branch",
        "branch_name",
        "worktree_path",
        "child_chat_kind",
        "model_assignment",
        "initial_instruction_ref",
        "instruction_digest",
        "idempotency_key",
    }
    missing = sorted(key for key in required if key not in plan)
    if missing:
        raise FrontdoorError("child_thread_plan missing fields:" + ",".join(missing))
    allowed = required | {"issue_url", "instruction_ref_digest", "expected_pr_title"}
    extra = sorted(set(plan) - allowed)
    if extra:
        raise FrontdoorError("child_thread_plan unexpected fields:" + ",".join(extra))
    repo_full_name = validate_repo_full_name(plan["repo_full_name"])
    try:
        repo_root = safe_paths.exact_allowlisted_path(
            plan["repo_root"],
            allowed_paths=git_worktree_roots(REPO_ROOT),
            label="repo_root",
        )
    except safe_paths.SafePathError as exc:
        raise FrontdoorError(
            "repo_root must identify the approved Saihai checkout family"
        ) from exc
    if not is_approved_checkout(repo_root):
        raise FrontdoorError("repo_root must identify the approved Saihai checkout family")
    instruction_ref = validate_child_thread_path(
        plan["initial_instruction_ref"],
        repo_root=repo_root,
        label="initial_instruction_ref",
        must_exist=True,
    )
    instruction_digest = normalize_sha256_digest(plan["instruction_digest"], "instruction_digest")
    if file_sha256(Path(instruction_ref)) != instruction_digest:
        raise FrontdoorError("instruction_digest does not match initial_instruction_ref")
    worktree_path = validate_child_thread_path(
        plan["worktree_path"],
        repo_root=repo_root,
        label="worktree_path",
    )
    child_chat_kind = str(plan["child_chat_kind"])
    if child_chat_kind not in {"create", "fork"}:
        raise FrontdoorError("child_chat_kind must be create or fork")
    model_assignment = plan["model_assignment"]
    if not isinstance(model_assignment, dict):
        raise FrontdoorError("model_assignment must be object")
    model_surface = str(model_assignment.get("surface") or "")
    model_id = str(model_assignment.get("model_id") or "")
    if not model_surface or not model_id:
        raise FrontdoorError("model_assignment.surface and model_assignment.model_id are required")
    return {
        "plan_version": "1",
        "task_id": validate_artifact_id(str(plan["task_id"]), "task_id"),
        "request_id": validate_artifact_id(
            str(plan["request_id"]), "request_id"
        ),
        "issue_id": normalize_issue_id(plan["issue_id"]),
        "issue_url": str(
            plan.get("issue_url")
            or f"https://github.com/{repo_full_name}/issues/{normalize_issue_id(plan['issue_id'])}"
        ),
        "repo_full_name": repo_full_name,
        "repo_root": str(repo_root),
        "base_branch": validate_safe_branch_name(plan["base_branch"]),
        "branch_name": validate_safe_branch_name(plan["branch_name"]),
        "worktree_path": worktree_path,
        "child_chat_kind": child_chat_kind,
        "model_assignment": {
            "surface": model_surface,
            "model_id": model_id,
            "reason": str(model_assignment.get("reason") or ""),
        },
        "initial_instruction_ref": instruction_ref,
        "instruction_digest": instruction_digest,
        "instruction_ref_digest": normalize_sha256_digest(
            plan.get("instruction_ref_digest") or plan["instruction_digest"],
            "instruction_ref_digest",
        ),
        "expected_pr_title": str(
            plan.get("expected_pr_title") or f"[issue #{normalize_issue_id(plan['issue_id'])}] Implement child work"
        ),
        "idempotency_key": normalize_child_thread_idempotency_key(plan["idempotency_key"]),
    }


def normalize_child_thread_idempotency_key(value: Any) -> str:
    text = str(value or "")
    if not text.strip():
        raise FrontdoorError("child_thread idempotency_key must be non-empty")
    return text


def child_thread_plan_digest(plan: dict[str, Any]) -> str:
    material = {key: value for key, value in plan.items() if key != "idempotency_key"}
    return "sha256:" + stable_digest(material)


def child_thread_result_digest(result: dict[str, Any]) -> str:
    return "sha256:" + stable_digest(result)


def validate_child_thread_result(result: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise FrontdoorError("child_thread_result must be object")
    allowed = {
        "status",
        "thread_id",
        "host_id",
        "created",
        "reused",
        "pending_worktree_id",
        "worktree_path",
        "branch_name",
        "instruction_ref",
        "instruction_digest",
    }
    extra = sorted(set(result) - allowed)
    if extra:
        raise FrontdoorError("child_thread_result unexpected fields:" + ",".join(extra))
    status = str(result.get("status") or "")
    if status not in {"created", "reused", "pending"}:
        raise FrontdoorError("child_thread_result.status must be created, reused, or pending")
    thread_id = str(result.get("thread_id") or "")
    pending_worktree_id = str(result.get("pending_worktree_id") or "")
    if status in {"created", "reused"} and not thread_id:
        raise FrontdoorError("thread_id is required when child thread is created or reused")
    if status == "pending" and not pending_worktree_id:
        raise FrontdoorError("pending_worktree_id is required when child thread is pending")
    if pending_worktree_id:
        pending_worktree_id = validate_child_pending_worktree_id(pending_worktree_id)
    worktree_path = str(result.get("worktree_path") or plan["worktree_path"])
    if worktree_path != plan["worktree_path"]:
        raise FrontdoorError("child_thread_result.worktree_path must match validated plan")
    branch_name = str(result.get("branch_name") or plan["branch_name"])
    if branch_name != plan["branch_name"]:
        raise FrontdoorError("child_thread_result.branch_name must match validated plan")
    instruction_digest = str(result.get("instruction_digest") or plan["instruction_digest"])
    if instruction_digest != plan["instruction_digest"]:
        raise FrontdoorError("child_thread_result.instruction_digest must match validated plan")
    instruction_ref = str(result.get("instruction_ref") or plan["initial_instruction_ref"])
    if instruction_ref != plan["initial_instruction_ref"]:
        raise FrontdoorError("child_thread_result.instruction_ref must match validated plan")
    created = result.get("created")
    reused = result.get("reused")
    if created is not None and not isinstance(created, bool):
        raise FrontdoorError("child_thread_result.created must be boolean")
    if reused is not None and not isinstance(reused, bool):
        raise FrontdoorError("child_thread_result.reused must be boolean")
    effective_created = created if created is not None else status == "created"
    effective_reused = reused if reused is not None else status == "reused"
    expected_created = status == "created"
    expected_reused = status == "reused"
    if effective_created is not expected_created:
        raise FrontdoorError("child_thread_result.created must match status")
    if effective_reused is not expected_reused:
        raise FrontdoorError("child_thread_result.reused must match status")
    return {
        "status": status,
        "thread_id": thread_id or None,
        "host_id": str(result.get("host_id") or "") or None,
        "created": effective_created,
        "reused": effective_reused,
        "pending_worktree_id": pending_worktree_id or None,
        "worktree_path": worktree_path,
        "branch_name": branch_name,
        "instruction_ref": instruction_ref,
        "instruction_digest": instruction_digest,
    }


def child_thread_redacted_summary(record: dict[str, Any]) -> dict[str, Any]:
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    worktree_path = str(plan.get("worktree_path") or "")
    return {
        "action_id": record.get("action_id"),
        "issue_id": plan.get("issue_id"),
        "repo_full_name": plan.get("repo_full_name"),
        "branch_name": plan.get("branch_name"),
        "child_chat_kind": plan.get("child_chat_kind"),
        "status": result.get("status"),
        "thread_id": result.get("thread_id"),
        "host_id": result.get("host_id"),
        "pending_worktree_id_digest": "sha256:" + stable_digest(result.get("pending_worktree_id") or ""),
        "worktree_label": Path(worktree_path).name if worktree_path else None,
        "worktree_path_digest": "sha256:" + stable_digest(worktree_path),
        "instruction_digest": plan.get("instruction_digest"),
        "plan_digest": record.get("plan_digest"),
        "idempotency_replayed": bool(record.get("idempotency_replayed")),
    }


def list_child_thread_summaries(
    state_root: Path,
    projection_binding: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        expected_binding = work_order_builder.validate_projection_binding(
            projection_binding
        )
    except work_order_builder.WorkOrderError:
        return []
    directory = state_paths(state_root)["child_thread_actions"]
    summaries: list[dict[str, Any]] = []
    for path in list_state_files(
        directory,
        prefix="child-thread-",
        suffix=".json",
    ):
        try:
            record = read_json(path)
        except FrontdoorError:
            continue
        plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
        try:
            record_binding = work_order_builder.validate_projection_binding(
                record.get("projection_binding")
            )
            plan_binding = work_order_builder.validate_projection_binding(
                plan.get("projection_binding")
            )
        except work_order_builder.WorkOrderError:
            continue
        if (
            record_binding == expected_binding
            and plan_binding == expected_binding
            and plan.get("request_id") == expected_binding["request_id"]
            and plan.get("task_id") == expected_binding["task_id"]
        ):
            summaries.append(child_thread_redacted_summary(record))
    return summaries


def child_thread_create_action(
    *,
    state_root: Path,
    plan: dict[str, Any],
    result: dict[str, Any],
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor = principal or make_principal(
        "action_gateway_executor",
        "child-thread-gateway",
        authn_method="local_cli",
    )
    normalized_plan = validate_child_thread_plan(plan)
    request_record = read_json(
        request_path(state_root, normalized_plan["request_id"])
    )
    approved_activation = request_record.get("approved_activation")
    if (
        request_record.get("request_id") != normalized_plan["request_id"]
        or request_record.get("task_id") != normalized_plan["task_id"]
        or not isinstance(approved_activation, dict)
        or approved_activation.get("activation_status") != "approved"
    ):
        raise FrontdoorError("child_thread_request_not_approved")
    try:
        projection_binding = work_order_builder.projection_binding_from_request_record(
            request_record
        )
    except work_order_builder.WorkOrderError as exc:
        raise FrontdoorError("child_thread_projection_binding_invalid") from exc
    normalized_plan["projection_binding"] = projection_binding
    plan_digest = child_thread_plan_digest(normalized_plan)
    normalized_result = validate_child_thread_result(result, normalized_plan)
    result_digest = child_thread_result_digest(normalized_result)
    idempotency_key = normalized_plan["idempotency_key"]
    idempotency_file = child_thread_idempotency_path(state_root, idempotency_key)
    subject = {
        "task_id": normalized_plan["task_id"],
        "request_id": normalized_plan["request_id"],
        "issue_id": normalized_plan["issue_id"],
        "plan_digest": plan_digest,
    }
    signature = assert_allowed_principal(
        state_root=state_root,
        principal=actor,
        allowed_types=ACTION_GATEWAY_PRINCIPAL_TYPES,
        transition="child_thread_create",
        subject=subject,
        blocked_reason="child_thread_create requires action gateway executor",
    )
    if state_file_exists(idempotency_file):
        replay = read_json(idempotency_file)
        action_id = str(replay["action_id"])
        record = read_json(child_thread_action_path(state_root, action_id))
        replay_result_digest = replay.get("result_digest") or record.get("result_digest")
        if not replay_result_digest and isinstance(record.get("result"), dict):
            replay_result_digest = child_thread_result_digest(record["result"])
        if replay.get("plan_digest") != plan_digest or replay_result_digest != result_digest:
            append_audit_event(
                state_root=state_root,
                event_type="child_thread_create",
                principal=actor,
                subject=subject,
                outcome="blocked",
                details={"reason": "idempotency_conflict"},
            )
            raise FrontdoorError("idempotency conflict for child_thread_create")
        record["idempotency_replayed"] = True
        append_audit_event(
            state_root=state_root,
            event_type="child_thread_create",
            principal=actor,
            subject=subject,
            outcome="replayed",
            details={"action_id": action_id, "plan_digest": plan_digest},
        )
        return {
            "schema_version": 1,
            "decision": "ok",
            "action_id": action_id,
            "action_path": str(child_thread_action_path(state_root, action_id)),
            "plan_digest": plan_digest,
            "result_digest": result_digest,
            "idempotency_replayed": True,
            "child_thread": child_thread_redacted_summary(record),
        }

    action_id = "child-thread-" + stable_digest(
        {
            "plan_digest": plan_digest,
            "result_digest": result_digest,
        }
    )[:20]
    action_path = child_thread_action_path(state_root, action_id)
    if state_file_exists(action_path):
        existing = read_json(action_path)
        if existing.get("plan_digest") != plan_digest or existing.get("result_digest") != result_digest:
            append_audit_event(
                state_root=state_root,
                event_type="child_thread_create",
                principal=actor,
                subject=subject,
                outcome="blocked",
                details={"reason": "action_id_conflict", "action_id": action_id},
            )
            raise FrontdoorError("child_thread action_id conflict")
    record = {
        "action_version": "1",
        "action_id": action_id,
        "created_at": now_iso(),
        "principal": redacted_principal(actor),
        "projection_binding": projection_binding,
        "plan_digest": plan_digest,
        "result_digest": result_digest,
        "plan": {
            **{key: value for key, value in normalized_plan.items() if key != "idempotency_key"},
        },
        "result": normalized_result,
        "authority": {
            "orchestrator_role": "validated_action_plan_only",
            "executor_principal_required": "action_gateway_executor",
            "raw_thread_tools_exposed_to_main_agent": False,
            "raw_shell_git_exposed_to_main_agent": False,
            "instruction_authority": "artifact_ref_and_digest",
        },
        "signature": signature,
    }
    write_json(action_path, record)
    write_json(
        idempotency_file,
        {
            "idempotency_version": "1",
            "action_id": action_id,
            "plan_digest": plan_digest,
            "result_digest": result_digest,
            "created_at": record["created_at"],
        },
    )
    append_audit_event(
        state_root=state_root,
        event_type="child_thread_create",
        principal=actor,
        subject=subject,
        outcome="ok",
        details={
            "action_id": action_id,
            "plan_digest": plan_digest,
            "result_digest": result_digest,
            "thread_id": normalized_result.get("thread_id"),
            "pending_worktree_id_digest": "sha256:" + stable_digest(normalized_result.get("pending_worktree_id") or ""),
            "created": normalized_result.get("created"),
            "reused": normalized_result.get("reused"),
        },
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "action_id": action_id,
        "action_path": str(action_path),
        "plan_digest": plan_digest,
        "result_digest": result_digest,
        "idempotency_replayed": False,
        "child_thread": child_thread_redacted_summary(record),
    }


def request_digest(
    payload: dict[str, Any],
    *,
    surface_identity: dict[str, Any],
    workspace_id: str = "",
    checkout_identity: dict[str, Any] | None = None,
    launch_session_identity: dict[str, Any] | None = None,
) -> str:
    frontend_kind = str(surface_identity.get("frontend_kind") or "")
    if not frontend_kind:
        raise FrontdoorError("surface_identity frontend_kind is required")
    descriptor_digest = normalize_sha256_digest(
        surface_identity.get("descriptor_digest"),
        "surface_identity.descriptor_digest",
    )
    material = {
        "task_id": payload.get("task_id"),
        "request_id": payload.get("request_id"),
        "request_kind": payload.get("request_kind"),
        "prompt": payload.get("prompt") or "",
        "refs": list(payload.get("refs") or []),
        "allowed_paths": list(payload.get("allowed_paths") or []),
        "expires_at": payload.get("expires_at") or "run_terminal",
        "frontdoor": frontend_kind,
        "surface_descriptor_digest": descriptor_digest,
        "workspace_id": workspace_id,
        "checkout_identity_digest": (
            str((checkout_identity or {}).get("identity_digest") or "")
        ),
        "launch_session_digest": (
            str((launch_session_identity or {}).get("record_digest") or "")
        ),
    }
    return "sha256:" + stable_digest(material)


def bridge_owner_principal(record: dict[str, Any]) -> dict[str, str]:
    owner = record.get("owner_principal")
    if not isinstance(owner, dict):
        owner = record.get("principal")
    if not isinstance(owner, dict):
        return redacted_principal({})
    return redacted_principal(owner)


def assert_bridge_request_owner(
    record: dict[str, Any],
    principal: dict[str, Any],
) -> None:
    if bridge_owner_principal(record) != redacted_principal(principal):
        raise FrontdoorError("bridge request is not owned by the installed frontend principal")


def bridge_request_surface_kind(
    record: dict[str, Any],
    *,
    registry: frontdoor_surface_registry.SurfaceRegistry,
) -> str:
    if "surface_identity" in record:
        stored = record.get("surface_identity")
        if not isinstance(stored, dict):
            raise FrontdoorError("bridge request surface identity is invalid")
        frontend_kind = str(stored.get("frontend_kind") or "")
    else:
        requester_metadata = record.get("requester")
        frontend_kind = str(
            requester_metadata.get("frontdoor") or ""
            if isinstance(requester_metadata, dict)
            else ""
        )
    try:
        registry.descriptor(frontend_kind)
    except frontdoor_surface_registry.SurfaceRegistryError as exc:
        raise FrontdoorError("bridge request surface identity is invalid") from exc
    return frontend_kind


def assert_bridge_launch_session(
    record: dict[str, Any],
    launch_session_identity: dict[str, Any] | None,
    *,
    frontend_kind: str,
    registry: frontdoor_surface_registry.SurfaceRegistry,
) -> None:
    stored = record.get("launch_session_identity")
    stored_digest = str(record.get("launch_session_digest") or "")
    if launch_session_identity is None:
        if stored is not None or stored_digest:
            raise FrontdoorError("bridge launch session is required")
        return
    try:
        normalized = registry.normalize_launch_session(
            frontend_kind,
            launch_session_identity,
        )
    except frontdoor_surface_registry.SurfaceRegistryError as exc:
        raise FrontdoorError(str(exc)) from exc
    if normalized is None or "record_digest" not in normalized:
        raise FrontdoorError("bridge launch session is invalid")
    if stored != normalized or not hmac.compare_digest(
        stored_digest,
        str(normalized["record_digest"]),
    ):
        raise FrontdoorError("bridge launch session does not match request authority")


def bridge_transaction_path(
    state_root: Path,
    request_id: str,
    idempotency_digest: str,
) -> Path:
    safe_request_id = validate_artifact_id(request_id, "request_id")
    normalized_digest = normalize_sha256_digest(
        idempotency_digest,
        "idempotency_key_digest",
    )
    digest_component = safe_paths.safe_component(
        normalized_digest.removeprefix("sha256:")[:24],
        label="idempotency_key_digest_component",
    )
    return (
        state_paths(state_root)["bridge_transactions"]
        / f"{safe_request_id}-{digest_component}.json"
    )


def _remove_bridge_atomic_temps(state_root: Path) -> None:
    for category in ("requests", "idempotency", "bridge_transactions"):
        directory = state_paths(state_root)[category]
        for path in list_state_files(directory, prefix=".", suffix=".tmp"):
            unlink_state_file(path, missing_ok=True)


def recover_bridge_submit_transactions(state_root: Path) -> int:
    """Finish valid write-ahead bridge transactions after process failure.

    The caller must hold the global state-root lock. Invalid or conflicting
    journals fail closed instead of guessing which durable object is correct.
    """

    _remove_bridge_atomic_temps(state_root)
    directory = state_paths(state_root)["bridge_transactions"]
    repaired = 0
    for transaction_path in list_state_files(directory, suffix=".json"):
        transaction = read_json(transaction_path)
        if transaction.get("transaction_version") != "1":
            raise FrontdoorError("unsupported bridge transaction journal")
        request_record = transaction.get("request_record")
        idempotency_record = transaction.get("idempotency_record")
        if not isinstance(request_record, dict) or not isinstance(idempotency_record, dict):
            raise FrontdoorError("invalid bridge transaction journal")
        request_id = validate_artifact_id(
            str(transaction.get("request_id") or ""),
            "request_id",
        )
        request_digest_value = normalize_sha256_digest(
            transaction.get("request_digest"),
            "request_digest",
        )
        idempotency_digest = normalize_sha256_digest(
            transaction.get("idempotency_key_digest"),
            "idempotency_key_digest",
        )
        expected_transaction_path = bridge_transaction_path(
            state_root,
            request_id,
            idempotency_digest,
        )
        if transaction_path != expected_transaction_path:
            raise FrontdoorError("bridge transaction path mismatch")
        if (
            request_record.get("request_id") != request_id
            or request_record.get("request_digest") != request_digest_value
            or request_record.get("idempotency_key_digest") != idempotency_digest
            or idempotency_record.get("request_id") != request_id
            or idempotency_record.get("request_digest") != request_digest_value
            or idempotency_record.get("key_digest") != idempotency_digest
        ):
            raise FrontdoorError("bridge transaction binding mismatch")

        durable_request_path = request_path(state_root, request_id)
        durable_idempotency_path = (
            state_paths(state_root)["idempotency"]
            / f"key-{idempotency_digest.removeprefix('sha256:')}.json"
        )
        if state_file_exists(durable_request_path):
            if read_json(durable_request_path) != request_record:
                raise FrontdoorError("bridge transaction request conflict")
        else:
            write_json(durable_request_path, request_record)
            repaired += 1
        if state_file_exists(durable_idempotency_path):
            if read_json(durable_idempotency_path) != idempotency_record:
                raise FrontdoorError("bridge transaction idempotency conflict")
        else:
            write_json(durable_idempotency_path, idempotency_record)
            repaired += 1
        unlink_state_file(transaction_path, missing_ok=True)
    return repaired


def bridge_pending_usage(
    state_root: Path,
    principal: dict[str, Any],
) -> tuple[int, int]:
    count = 0
    total_bytes = 0
    requests_dir = state_paths(state_root)["requests"]
    owner = redacted_principal(principal)
    for path in list_state_files(requests_dir, suffix=".json"):
        record = read_json(path)
        if bridge_owner_principal(record) != owner:
            continue
        if str(record.get("status") or "") not in BRIDGE_PENDING_REQUEST_STATUSES:
            continue
        record = reconcile_pending_request_terminal_run(state_root, record)
        if str(record.get("status") or "") not in BRIDGE_PENDING_REQUEST_STATUSES:
            continue
        count += 1
        metadata = state_file_stat(path)
        if metadata is None:
            raise FrontdoorError("pending bridge request disappeared during measurement")
        total_bytes += metadata.st_size
    return count, total_bytes


def durable_state_usage(state_root: Path) -> tuple[int, int]:
    """Count every durable regular artifact without following symlinks."""

    try:
        root = host_state_root.ensure_runtime_state_root(state_root)
    except host_state_root.HostStateRootError as exc:
        raise FrontdoorError(str(exc)) from exc
    count = 0
    total_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise FrontdoorError("unable to enumerate durable state") from exc
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise FrontdoorError("unable to measure durable state") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise FrontdoorError("durable state contains symlink")
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
                    raise FrontdoorError("durable state directory metadata invalid")
                pending.append(Path(entry.path))
            elif stat.S_ISREG(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
                    raise FrontdoorError("durable state artifact metadata invalid")
                count += 1
                total_bytes += metadata.st_size
            else:
                raise FrontdoorError("durable state contains unsupported artifact")
    return count, total_bytes


def assert_durable_capacity(
    state_root: Path,
    *,
    additional_count: int,
    additional_bytes: int,
    max_count: int,
    max_bytes: int,
) -> tuple[int, int]:
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (additional_count, additional_bytes, max_count, max_bytes)
    ):
        raise FrontdoorError("durable state quota invalid")
    count, used_bytes = durable_state_usage(state_root)
    if count + additional_count > max_count:
        raise FrontdoorError("durable state artifact count quota exceeded")
    if used_bytes + additional_bytes > max_bytes:
        raise FrontdoorError("durable state byte quota exceeded")
    return count, used_bytes


def _bridge_rate_path(
    state_root: Path,
    principal: dict[str, Any],
    operation: str,
) -> Path:
    digest = stable_digest(redacted_principal(principal))[:24]
    return state_paths(state_root)["bridge_rate_limits"] / f"{digest}-{operation}.json"


def consume_bridge_rate_limit(
    state_root: Path,
    *,
    principal: dict[str, Any],
    operation: str,
    limit: int,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    if operation not in {"read", "ack"} or not isinstance(limit, int) or limit < 1:
        raise FrontdoorError("bridge rate limit invalid")
    current = time.time() if now_epoch is None else now_epoch
    path = _bridge_rate_path(state_root, principal, operation)
    if state_file_exists(path):
        record = read_json(path)
    else:
        record = {}
    window_started = record.get("window_started_epoch")
    count = record.get("count")
    if (
        not isinstance(window_started, (int, float))
        or isinstance(window_started, bool)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or current < float(window_started)
        or current - float(window_started) >= BRIDGE_RATE_WINDOW_SECONDS
    ):
        window_started = current
        count = 0
    if count >= limit:
        raise FrontdoorError(f"bridge {operation} rate limit exceeded")
    updated = {
        "rate_version": "1",
        "operation": operation,
        "principal": redacted_principal(principal),
        "window_started_epoch": float(window_started),
        "count": count + 1,
        "updated_at": now_iso(),
    }
    write_json(path, updated)
    return updated


def purge_bridge_retained_artifacts(
    *,
    state_root: Path,
    principal: dict[str, Any],
    now_epoch: float | None = None,
    terminal_retention_seconds: int = BRIDGE_TERMINAL_RETENTION_SECONDS,
    audit_retention_seconds: int = BRIDGE_AUDIT_ROTATION_RETENTION_SECONDS,
) -> dict[str, Any]:
    """Explicitly redact terminal prompts and purge only non-active indexes/receipts."""

    actor = redacted_principal(principal)
    if actor["principal_type"] not in EXECUTION_PRINCIPAL_TYPES:
        raise FrontdoorError("bridge retention requires host operator principal")
    if terminal_retention_seconds < 0 or audit_retention_seconds < 0:
        raise FrontdoorError("bridge retention interval invalid")
    current = time.time() if now_epoch is None else now_epoch
    redacted_requests: list[str] = []
    terminal_ids: set[str] = set()
    deleted: list[dict[str, str]] = []
    with run_lock.hold_global_lock(
        state_root,
        operation="bridge_retention_purge",
        principal=actor,
    ):
        requests_dir = state_paths(state_root)["requests"]
        for path in list_state_files(requests_dir, suffix=".json"):
            record = read_json(path)
            if str(record.get("status") or "") not in BRIDGE_TERMINAL_STATUSES:
                continue
            timestamp = _parse_host_timestamp(
                record.get("updated_at") or record.get("created_at"),
                label="terminal_request_timestamp",
            )
            if current - timestamp < terminal_retention_seconds:
                continue
            request_id = str(record.get("request_id") or "")
            terminal_ids.add(request_id)
            prompt = str(record.get("user_prompt") or "")
            if prompt:
                record["user_prompt"] = ""
                record["retention"] = {
                    "retention_version": "1",
                    "prompt_redacted": True,
                    "prompt_digest": "sha256:" + hashlib.sha256(
                        prompt.encode("utf-8")
                    ).hexdigest(),
                    "redacted_at": now_iso(),
                }
                write_json(path, record)
                redacted_requests.append(request_id)

        for category in ("idempotency", "acks"):
            directory = state_paths(state_root)[category]
            for path in list_state_files(directory, suffix=".json"):
                record = read_json(path)
                if str(record.get("request_id") or "") not in terminal_ids:
                    continue
                try:
                    raw = run_store.read_and_unlink_private_file(path)
                except run_store.RunStoreError as exc:
                    raise FrontdoorError("retention artifact changed during deletion") from exc
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                deleted.append({"category": category, "digest": digest})

        rate_dir = state_paths(state_root)["bridge_rate_limits"]
        for path in list_state_files(rate_dir, suffix=".json"):
            metadata = state_file_stat(path)
            if metadata is None:
                raise FrontdoorError("rate artifact disappeared during retention scan")
            if current - metadata.st_mtime < terminal_retention_seconds:
                continue
            try:
                raw = run_store.read_and_unlink_private_file(path)
            except run_store.RunStoreError as exc:
                raise FrontdoorError("retention artifact changed during deletion") from exc
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            deleted.append({"category": "bridge_rate_limits", "digest": digest})

        audit_dir = state_paths(state_root)["audit"]
        for path in list_state_files(audit_dir, prefix="events.", suffix=".jsonl"):
            metadata = state_file_stat(path)
            if metadata is None:
                raise FrontdoorError("audit artifact disappeared during retention scan")
            if current - metadata.st_mtime < audit_retention_seconds:
                continue
            try:
                raw = run_store.read_and_unlink_private_file(
                    path,
                    max_bytes=BRIDGE_AUDIT_ROTATE_BYTES + 1024 * 1024,
                )
            except run_store.RunStoreError as exc:
                raise FrontdoorError("retention artifact changed during deletion") from exc
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            deleted.append({"category": "audit_rotation", "digest": digest})

        event = append_audit_event(
            state_root=state_root,
            event_type="bridge_retention_purge",
            principal=actor,
            subject={"terminal_request_count": len(terminal_ids)},
            outcome="ok",
            details={
                "redacted_request_ids": sorted(redacted_requests),
                "deleted_artifact_digests": deleted,
                "active_authority_artifacts_deleted": False,
            },
        )
    count, used_bytes = durable_state_usage(state_root)
    return {
        "schema_version": 1,
        "decision": "ok",
        "redacted_request_count": len(redacted_requests),
        "deleted_artifact_count": len(deleted),
        "audit_event_id": event["event_id"],
        "durable_artifact_count": count,
        "durable_bytes": used_bytes,
    }


def _serialized_json_bytes(payload: dict[str, Any]) -> int:
    return len(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def bridge_audit_details(
    *,
    frontdoor: str,
    chat_session_id: str = "",
    peer: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"requester": requester(frontdoor, chat_session_id)}
    if peer:
        details["peer"] = {str(key): str(value) for key, value in peer.items()}
    if extra:
        details.update(extra)
    return details


def validate_bridge_submit_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    extra = sorted(set(payload) - BRIDGE_SUBMIT_ALLOWED_FIELDS)
    if extra:
        errors.append("unexpected_fields:" + ",".join(extra))
    forbidden = sorted(set(payload) & BRIDGE_FORBIDDEN_FIELDS)
    if forbidden:
        errors.append("forbidden_fields:" + ",".join(forbidden))
    for field in ("task_id", "request_id", "request_kind", "prompt", "idempotency_key"):
        if not isinstance(payload.get(field), str) or not str(payload.get(field)).strip():
            errors.append(f"{field} must be non-empty string")
    for field in ("task_id", "request_id"):
        value = payload.get(field)
        if isinstance(value, str):
            try:
                validate_artifact_id(value, field)
            except FrontdoorError as exc:
                errors.append(str(exc))
    if payload.get("request_kind") not in BRIDGE_REQUEST_KINDS:
        errors.append(f"request_kind unsupported:{payload.get('request_kind')!r}")
    if "refs" not in payload:
        errors.append("refs is required")
    for field in ("refs", "allowed_paths"):
        value = payload.get(field) or []
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            errors.append(f"{field} must be a list of non-empty strings")
        if field == "refs" and isinstance(value, list) and not value:
            errors.append("refs must be non-empty")
    for key, value in payload.items():
        if any(secret_word in key.lower() for secret_word in ("token", "secret", "api_key", "authorization")):
            errors.append(f"forbidden_secret_field:{key}")
        if isinstance(value, str) and any(marker in value.lower() for marker in ("authorization:", "bearer ", "api_key=", "private key")):
            errors.append(f"forbidden_secret_material:{key}")
    return errors


def bridge_submit_request(
    *,
    state_root: Path,
    payload: dict[str, Any],
    frontend_kind: str,
    principal: dict[str, Any] | None = None,
    peer: dict[str, Any] | None = None,
    workspace_id: str = "",
    checkout_identity: dict[str, Any] | None = None,
    launch_session_identity: dict[str, Any] | None = None,
    max_pending_requests: int = DEFAULT_BRIDGE_MAX_PENDING_REQUESTS,
    max_pending_bytes: int = DEFAULT_BRIDGE_MAX_PENDING_BYTES,
    max_durable_artifacts: int = DEFAULT_BRIDGE_MAX_DURABLE_ARTIFACTS,
    max_durable_bytes: int = DEFAULT_BRIDGE_MAX_DURABLE_BYTES,
    surface_registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
) -> dict[str, Any]:
    errors = validate_bridge_submit_payload(payload)
    frontdoor_name = str(frontend_kind or "")
    if not frontdoor_name:
        errors.append("host frontend_kind must be non-empty string")
    if "frontdoor" in payload and payload.get("frontdoor") != frontdoor_name:
        errors.append("payload frontdoor conflicts with host frontend_kind")
    chat_session_id = str(payload.get("chat_session_id") or "")
    principal = principal or bridge_principal(frontdoor_name, chat_session_id)
    normalized_checkout: dict[str, str] | None = None
    normalized_launch_session: dict[str, Any] | None = None
    normalized_surface: dict[str, Any] | None = None
    active_surface_registry = surface_registry or load_surface_registry()
    try:
        if checkout_identity is not None:
            normalized_checkout = validate_checkout_identity(checkout_identity)
            if not workspace_id:
                raise FrontdoorError("workspace_id is required with checkout_identity")
            if normalized_checkout["workspace_id"] != workspace_id:
                raise FrontdoorError("workspace_id does not match checkout_identity")
        elif workspace_id:
            raise FrontdoorError("checkout_identity is required with workspace_id")
        try:
            active_surface_registry.descriptor(frontdoor_name)
        except frontdoor_surface_registry.SurfaceRegistryError as exc:
            raise FrontdoorError(str(exc)) from exc
        if launch_session_identity is not None:
            try:
                normalized_launch_session = active_surface_registry.normalize_launch_session(
                    frontdoor_name,
                    launch_session_identity,
                )
            except frontdoor_surface_registry.SurfaceRegistryError as exc:
                raise FrontdoorError(str(exc)) from exc
            if normalized_checkout is None:
                raise FrontdoorError("checkout_identity is required with launch_session_identity")
            required_launch_bindings = {
                "principal_id",
                "workspace_id",
                "checkout_realpath",
                "checkout_identity_digest",
                "record_digest",
            }
            if normalized_launch_session is None or not required_launch_bindings.issubset(
                normalized_launch_session
            ):
                raise FrontdoorError("launch_session_identity missing common host bindings")
            if (
                normalized_launch_session["principal_id"] != principal.get("principal_id")
                or normalized_launch_session["workspace_id"] != workspace_id
                or normalized_launch_session["checkout_realpath"]
                != normalized_checkout["checkout_realpath"]
                or normalized_launch_session["checkout_identity_digest"]
                != normalized_checkout["identity_digest"]
            ):
                raise FrontdoorError("launch_session_identity does not match host bindings")
        normalized_surface = resolve_surface_identity(
            frontdoor_name,
            registry=active_surface_registry,
            expected_checkout=(
                Path(normalized_checkout["checkout_realpath"])
                if normalized_checkout is not None
                else None
            ),
            launch_session_present=normalized_launch_session is not None,
        )
        if (
            not isinstance(max_pending_requests, int)
            or isinstance(max_pending_requests, bool)
            or max_pending_requests < 1
            or not isinstance(max_pending_bytes, int)
            or isinstance(max_pending_bytes, bool)
            or max_pending_bytes < 1
            or not isinstance(max_durable_artifacts, int)
            or isinstance(max_durable_artifacts, bool)
            or max_durable_artifacts < 1
            or not isinstance(max_durable_bytes, int)
            or isinstance(max_durable_bytes, bool)
            or max_durable_bytes < 1
        ):
            raise FrontdoorError("bridge storage quota must be a positive integer")
    except FrontdoorError as exc:
        errors.append(str(exc))
    subject = {
        "request_id": str(payload.get("request_id") or ""),
        "task_id": str(payload.get("task_id") or ""),
    }
    if errors:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_submit_request",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"errors": errors},
            ),
        )
        raise FrontdoorError("invalid bridge submit_request: " + "; ".join(errors))

    digest = request_digest(
        payload,
        workspace_id=workspace_id,
        checkout_identity=normalized_checkout,
        launch_session_identity=normalized_launch_session,
        surface_identity=normalized_surface,
    )
    idempotency_key = str(payload["idempotency_key"])
    idempotency_digest = bridge_idempotency_key_digest(
        idempotency_key,
        normalized_surface,
    )

    def block(reason: str) -> None:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_submit_request",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"reason": reason},
            ),
        )
        raise FrontdoorError(reason)

    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="bridge_submit_request",
            principal=principal,
        ):
            repaired = recover_bridge_submit_transactions(state_root)
            idempotency_file = idempotency_path(state_root, idempotency_key)
            if state_file_exists(idempotency_file):
                existing = read_json(idempotency_file)
                existing_key_digest = existing.get("key_digest")
                if existing_key_digest is None:
                    existing_key_digest = existing.get("idempotency_key_digest")
                if existing_key_digest not in {None, idempotency_digest}:
                    block("idempotency key surface descriptor drifted")
                existing_request_id = validate_artifact_id(
                    str(existing.get("request_id") or ""),
                    "request_id",
                )
                existing_record = read_json(request_path(state_root, existing_request_id))
                try:
                    assert_bridge_request_owner(existing_record, principal)
                except FrontdoorError:
                    block("idempotency key is owned by another installed frontend principal")
                if existing.get("request_digest") != digest:
                    block("idempotency conflict for bridge submit_request")
                projection = bridge_read_projection(
                    state_root=state_root,
                    request_id=existing_request_id,
                    frontdoor=frontdoor_name,
                    chat_session_id=chat_session_id,
                    principal=principal,
                    peer=peer,
                    launch_session_identity=normalized_launch_session,
                    enforce_rate_limit=False,
                    _lock_held=True,
                    surface_registry=active_surface_registry,
                )
                projection["replayed"] = True
                append_audit_event(
                    state_root=state_root,
                    event_type="bridge_submit_request",
                    principal=principal,
                    subject=subject,
                    outcome="replayed",
                    details=bridge_audit_details(
                        frontdoor=frontdoor_name,
                        chat_session_id=chat_session_id,
                        peer=peer,
                        extra={
                            "request_digest": digest,
                            "repaired_transaction_artifacts": repaired,
                        },
                    ),
                )
                return projection

            path = request_path(state_root, str(payload["request_id"]))
            if state_file_exists(path):
                existing_record = read_json(path)
                try:
                    assert_bridge_request_owner(existing_record, principal)
                except FrontdoorError:
                    block("request_id is owned by another installed frontend principal")
                existing_surface = existing_record.get("surface_identity")
                if "surface_identity" in existing_record and (
                    not isinstance(existing_surface, dict)
                    or surface_contract_binding(existing_surface)
                    != surface_contract_binding(normalized_surface)
                ):
                    block("bridge request surface contract drifted")
                if existing_record.get("request_digest") != digest:
                    block("request_id conflict for bridge submit_request")
                existing_idempotency_digest = existing_record.get("idempotency_key_digest")
                if existing_idempotency_digest not in {None, idempotency_digest}:
                    block("request_id is bound to another idempotency key")
                repaired_idempotency = {
                    "idempotency_version": "1",
                    "key_digest": idempotency_digest,
                    "request_id": str(payload["request_id"]),
                    "request_digest": digest,
                    "owner_principal": bridge_owner_principal(existing_record),
                    "workspace_id": str(existing_record.get("workspace_id") or workspace_id),
                    "checkout_identity_digest": str(
                        existing_record.get("checkout_identity_digest") or ""
                    ),
                    "created_at": str(existing_record.get("created_at") or now_iso()),
                }
                write_json(idempotency_file, repaired_idempotency)
                projection = bridge_read_projection(
                    state_root=state_root,
                    request_id=str(payload["request_id"]),
                    frontdoor=frontdoor_name,
                    chat_session_id=chat_session_id,
                    principal=principal,
                    peer=peer,
                    launch_session_identity=normalized_launch_session,
                    enforce_rate_limit=False,
                    _lock_held=True,
                    surface_registry=active_surface_registry,
                )
                projection["replayed"] = True
                return projection

            try:
                active_ref_root = (
                    Path(str(normalized_checkout["checkout_realpath"]))
                    if isinstance(normalized_checkout, dict)
                    else REPO_ROOT
                )
                bounded = bounded_context(
                    list(payload.get("refs") or []),
                    list(payload.get("allowed_paths") or []),
                    require_refs=True,
                    ref_root=active_ref_root,
                )
            except FrontdoorError as exc:
                block(str(exc))

            now = now_iso()
            owner_principal = redacted_principal(principal)
            record = {
                "request_version": "1",
                "task_id": str(payload["task_id"]),
                "request_id": str(payload["request_id"]),
                "request_kind": str(payload["request_kind"]),
                "created_at": now,
                "updated_at": now,
                "user_prompt": str(payload.get("prompt") or ""),
                "request_digest": digest,
                "idempotency_key_digest": idempotency_digest,
                **bounded,
                "expires_at": str(payload.get("expires_at") or "run_terminal"),
                "workspace_id": workspace_id or SCOPED_WORKER_REPO_FULL_NAME,
                "checkout_identity": normalized_checkout,
                "checkout_identity_digest": str(
                    (normalized_checkout or {}).get("identity_digest") or ""
                ),
                "launch_session_identity": normalized_launch_session,
                "launch_session_digest": str(
                    (normalized_launch_session or {}).get("record_digest") or ""
                ),
                "classification": None,
                "requester": requester(frontdoor_name, chat_session_id),
                "surface_identity": normalized_surface,
                "principal": owner_principal,
                "owner_principal": owner_principal,
                "status": "waiting_human",
                "proposal": {
                    "schema_version": 1,
                    "decision": "waiting_human",
                    "request_status": "waiting_human",
                    "reason": "typed_classification_required_from_non_bridge_principal",
                    "task_id": str(payload["task_id"]),
                    "request_id": str(payload["request_id"]),
                    "next_action": "ask_human",
                },
                "bridge_contract": {
                    "submit_contract_version": str(
                        (normalized_surface or {}).get("submit_contract_version") or ""
                    ),
                    "surface_identity_required": True,
                    "allowed_actions": BRIDGE_ALLOWED_ACTIONS,
                    "forbidden_actions": [
                        "classify",
                        "approve",
                        "create_run",
                        "drain",
                        "prepare_provider",
                        "validate_report",
                        "workflow_definition_change",
                    ],
                    "authorization_identity": "owner_principal",
                    "chat_session_id_role": "correlation_only",
                    "checkout_identity_required_for_action_gateway": True,
                },
            }

            pending_count, pending_bytes = bridge_pending_usage(state_root, principal)
            record_bytes = _serialized_json_bytes(record)
            if pending_count >= max_pending_requests:
                block("pending request count quota exceeded for installed frontend principal")
            if pending_bytes + record_bytes > max_pending_bytes:
                block("pending request byte quota exceeded for installed frontend principal")
            assert_durable_capacity(
                state_root,
                additional_count=4,
                additional_bytes=(record_bytes * 3) + 256 * 1024,
                max_count=max_durable_artifacts,
                max_bytes=max_durable_bytes,
            )

            idempotency_record = {
                "idempotency_version": "1",
                "key_digest": idempotency_digest,
                "request_id": str(payload["request_id"]),
                "request_digest": digest,
                "owner_principal": owner_principal,
                "workspace_id": workspace_id or SCOPED_WORKER_REPO_FULL_NAME,
                "checkout_identity_digest": record["checkout_identity_digest"],
                "created_at": now,
            }
            transaction = {
                "transaction_version": "1",
                "transaction_state": "prepared",
                "created_at": now,
                "request_id": str(payload["request_id"]),
                "request_digest": digest,
                "idempotency_key_digest": idempotency_digest,
                "owner_principal": owner_principal,
                "request_record": record,
                "idempotency_record": idempotency_record,
            }
            transaction_file = bridge_transaction_path(
                state_root,
                str(payload["request_id"]),
                idempotency_digest,
            )
            write_json(transaction_file, transaction)
            write_json(path, record)
            write_json(idempotency_file, idempotency_record)
            unlink_state_file(transaction_file, missing_ok=True)

            append_audit_event(
                state_root=state_root,
                event_type="bridge_submit_request",
                principal=principal,
                subject=subject,
                outcome="ok",
                details=bridge_audit_details(
                    frontdoor=frontdoor_name,
                    chat_session_id=chat_session_id,
                    peer=peer,
                    extra={
                        "request_digest": digest,
                        "owner_principal": owner_principal,
                        "workspace_id": record["workspace_id"],
                        "checkout_identity_digest": record["checkout_identity_digest"],
                        "checkout_kind": str(
                            (normalized_checkout or {}).get("checkout_kind") or "unbound"
                        ),
                        "checkout_branch": str(
                            (normalized_checkout or {}).get("branch") or ""
                        ),
                        "checkout_head_sha": str(
                            (normalized_checkout or {}).get("head_sha") or ""
                        ),
                        "pending_request_count_before": pending_count,
                        "pending_request_bytes_before": pending_bytes,
                        "repaired_transaction_artifacts": repaired,
                    },
                ),
            )
            return bridge_read_projection(
                state_root=state_root,
                request_id=str(payload["request_id"]),
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                principal=principal,
                peer=peer,
                launch_session_identity=normalized_launch_session,
                enforce_rate_limit=False,
                _lock_held=True,
                surface_registry=active_surface_registry,
            )
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_submit_request",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"reason": "bridge_submit_lock_contention"},
            ),
        )
        raise FrontdoorError("bridge submit_request lock contention") from exc


def redacted_ref_labels(refs: list[Any]) -> list[str]:
    labels: list[str] = []
    for ref in refs:
        value = str(ref)
        if value.startswith("/") or ":" in Path(value).anchor:
            labels.append("redacted:absolute-path")
        else:
            labels.append(value)
    return labels


def redacted_approval_summary(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    what = summary.get("what_will_execute") if isinstance(summary.get("what_will_execute"), dict) else {}
    return {
        "approval_view_version": summary.get("approval_view_version"),
        "source": summary.get("source"),
        "main_agent_prose_used": False,
        "human_action_id": summary.get("human_action_id"),
        "rate_limit": summary.get("rate_limit"),
        "what_will_execute": {
            "workflow_id": what.get("workflow_id"),
            "initial_step": what.get("initial_step"),
            "permission_mode": what.get("permission_mode"),
            "step_budget": what.get("step_budget"),
            "context_ref_count": len(what.get("context_refs") or []),
            "context_refs_digest": "sha256:" + stable_digest(what.get("context_refs") or []),
            "allowed_paths_digest": "sha256:" + stable_digest(what.get("allowed_paths") or []),
            "denied_ops": list(what.get("denied_ops") or []),
            "provider_adapter": what.get("provider_adapter"),
        },
        "classification_provenance": summary.get("classification_provenance"),
        "next_action": summary.get("next_action"),
    }


def redacted_checkout_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "identity_version": value.get("identity_version"),
        "workspace_id": value.get("workspace_id"),
        "checkout_kind": value.get("checkout_kind"),
        "branch": value.get("branch"),
        "head_sha": value.get("head_sha"),
        "tree_sha": value.get("tree_sha"),
        "git_common_dir_digest": value.get("git_common_dir_digest"),
        "worktree_state_digest": value.get("worktree_state_digest"),
        "identity_digest": value.get("identity_digest"),
        "paths_redacted": True,
    }


def build_bridge_projection(
    *,
    state_root: Path,
    request_id: str,
    principal: dict[str, Any],
    surface_registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = read_json(request_path(state_root, request_id))
    assert_bridge_request_owner(record, principal)
    active_surface_registry = surface_registry or load_surface_registry()
    frontend_kind = bridge_request_surface_kind(
        record,
        registry=active_surface_registry,
    )
    projection_surface = resolve_surface_identity(
        frontend_kind,
        registry=active_surface_registry,
        expected_checkout=(
            Path(str(record["checkout_identity"]["checkout_realpath"]))
            if isinstance(record.get("checkout_identity"), dict)
            and record["checkout_identity"].get("checkout_realpath")
            else None
        ),
        launch_session_present=record.get("launch_session_identity") is not None,
    )
    stored_surface = record.get("surface_identity")
    if isinstance(stored_surface, dict) and surface_contract_binding(
        stored_surface
    ) != surface_contract_binding(projection_surface):
        raise FrontdoorError("bridge request surface contract drifted")
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    try:
        projection_binding = work_order_builder.projection_binding_from_request_record(
            record
        )
    except work_order_builder.WorkOrderError:
        projection_binding = None
    return {
        "schema_version": 1,
        "decision": "ok",
        "projection_version": "1",
        "safe_for_principal": redacted_principal(principal),
        "request_id": record.get("request_id"),
        "idempotency_key_digest": record.get("idempotency_key_digest"),
        "task_id": record.get("task_id"),
        "request_kind": record.get("request_kind"),
        "request_status": record.get("status"),
        "workspace_id": record.get("workspace_id"),
        "surface_identity": projection_surface,
        "checkout_identity": redacted_checkout_identity(record.get("checkout_identity")),
        "checkout_identity_digest": record.get("checkout_identity_digest"),
        "orchestrator_decision": proposal.get("decision"),
        "reason": proposal.get("reason") or proposal.get("approval_required_reason"),
        "next_action": proposal.get("next_action"),
        "next_allowed_bridge_actions": BRIDGE_ALLOWED_ACTIONS,
        "context": {
            "ref_count": len(record.get("context_refs") or []),
            "ref_labels": redacted_ref_labels(list(record.get("context_refs") or [])),
            "refs_digest": "sha256:" + stable_digest(record.get("context_refs") or []),
        },
        "approval": redacted_approval_summary(record.get("approval")),
        "child_thread_summaries": list_child_thread_summaries(
            state_root,
            projection_binding or {},
        ),
        "worker_execution_summaries": scoped_worker_executor.list_redacted_summaries(
            state_root,
            projection_binding=projection_binding or {},
        ),
        "redacted_fields": [
            "user_prompt",
            "request_path",
            "run_path",
            "work_order_path",
            "adapter_request_path",
            "report_path",
            "evidence_path",
            "transcript_path",
            "provider_session_id",
            "principal_keys",
            "worktree_path",
            "repo_root",
            "initial_instruction_ref",
            "worker_instruction",
            "worker_result",
            "worker_evidence_path",
            "capability_nonce",
            "capability_signature",
            "executor_key",
            "checkout_realpath",
            "managed_primary_realpath",
        ],
        "transition_effect": "none",
    }, record


def bridge_projection_digest(projection: dict[str, Any]) -> str:
    material = {key: value for key, value in projection.items() if key != "projection_digest"}
    return "sha256:" + stable_digest(material)


def bridge_read_projection(
    *,
    state_root: Path,
    request_id: str,
    frontdoor: str,
    chat_session_id: str,
    principal: dict[str, Any] | None = None,
    peer: dict[str, Any] | None = None,
    launch_session_identity: dict[str, Any] | None = None,
    read_limit_per_minute: int = DEFAULT_BRIDGE_READS_PER_MINUTE,
    max_durable_artifacts: int = DEFAULT_BRIDGE_MAX_DURABLE_ARTIFACTS,
    max_durable_bytes: int = DEFAULT_BRIDGE_MAX_DURABLE_BYTES,
    enforce_rate_limit: bool = True,
    _lock_held: bool = False,
    surface_registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
) -> dict[str, Any]:
    active_surface_registry = surface_registry or load_surface_registry()
    resolve_surface_identity(frontdoor, registry=active_surface_registry)
    principal = principal or bridge_principal(frontdoor, chat_session_id)
    if enforce_rate_limit and not _lock_held:
        try:
            with run_lock.hold_global_lock(
                state_root,
                operation="bridge_read_projection",
                principal=principal,
            ):
                assert_durable_capacity(
                    state_root,
                    additional_count=2,
                    additional_bytes=256 * 1024,
                    max_count=max_durable_artifacts,
                    max_bytes=max_durable_bytes,
                )
                consume_bridge_rate_limit(
                    state_root,
                    principal=principal,
                    operation="read",
                    limit=read_limit_per_minute,
                )
                return bridge_read_projection(
                    state_root=state_root,
                    request_id=request_id,
                    frontdoor=frontdoor,
                    chat_session_id=chat_session_id,
                    principal=principal,
                    peer=peer,
                    launch_session_identity=launch_session_identity,
                    read_limit_per_minute=read_limit_per_minute,
                    max_durable_artifacts=max_durable_artifacts,
                    max_durable_bytes=max_durable_bytes,
                    enforce_rate_limit=False,
                    _lock_held=True,
                    surface_registry=active_surface_registry,
                )
        except run_lock.LockContentionError as exc:
            raise FrontdoorError("bridge read_projection lock contention") from exc
    try:
        projection, record = build_bridge_projection(
            state_root=state_root,
            request_id=request_id,
            principal=principal,
            surface_registry=active_surface_registry,
        )
        stored_frontend_kind = bridge_request_surface_kind(
            record,
            registry=active_surface_registry,
        )
        if stored_frontend_kind != frontdoor:
            raise FrontdoorError("bridge request surface identity mismatch")
        assert_bridge_launch_session(
            record,
            launch_session_identity,
            frontend_kind=stored_frontend_kind,
            registry=active_surface_registry,
        )
    except FrontdoorError:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_read_projection",
            principal=principal,
            subject={"request_id": request_id, "task_id": ""},
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"reason": "request_owner_mismatch_or_missing"},
            ),
        )
        raise
    digest = bridge_projection_digest(projection)
    append_audit_event(
        state_root=state_root,
        event_type="bridge_read_projection",
        principal=principal,
        subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
        outcome="ok",
        details=bridge_audit_details(
            frontdoor=frontdoor,
            chat_session_id=chat_session_id,
            peer=peer,
            extra={"projection_digest": digest},
        ),
    )
    projection["projection_digest"] = digest
    return projection


def bridge_ack_output(
    *,
    state_root: Path,
    request_id: str,
    projection_digest: str,
    frontdoor: str,
    chat_session_id: str,
    principal: dict[str, Any] | None = None,
    peer: dict[str, Any] | None = None,
    launch_session_identity: dict[str, Any] | None = None,
    ack_limit_per_minute: int = DEFAULT_BRIDGE_ACKS_PER_MINUTE,
    max_durable_artifacts: int = DEFAULT_BRIDGE_MAX_DURABLE_ARTIFACTS,
    max_durable_bytes: int = DEFAULT_BRIDGE_MAX_DURABLE_BYTES,
    enforce_rate_limit: bool = True,
    _lock_held: bool = False,
    surface_registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
) -> dict[str, Any]:
    active_surface_registry = surface_registry or load_surface_registry()
    resolve_surface_identity(frontdoor, registry=active_surface_registry)
    principal = principal or bridge_principal(frontdoor, chat_session_id)
    if enforce_rate_limit and not _lock_held:
        try:
            with run_lock.hold_global_lock(
                state_root,
                operation="bridge_ack_output",
                principal=principal,
            ):
                assert_durable_capacity(
                    state_root,
                    additional_count=3,
                    additional_bytes=256 * 1024,
                    max_count=max_durable_artifacts,
                    max_bytes=max_durable_bytes,
                )
                consume_bridge_rate_limit(
                    state_root,
                    principal=principal,
                    operation="ack",
                    limit=ack_limit_per_minute,
                )
                return bridge_ack_output(
                    state_root=state_root,
                    request_id=request_id,
                    projection_digest=projection_digest,
                    frontdoor=frontdoor,
                    chat_session_id=chat_session_id,
                    principal=principal,
                    peer=peer,
                    launch_session_identity=launch_session_identity,
                    ack_limit_per_minute=ack_limit_per_minute,
                    max_durable_artifacts=max_durable_artifacts,
                    max_durable_bytes=max_durable_bytes,
                    enforce_rate_limit=False,
                    _lock_held=True,
                    surface_registry=active_surface_registry,
                )
        except run_lock.LockContentionError as exc:
            raise FrontdoorError("bridge ack_output lock contention") from exc
    try:
        projection, before = build_bridge_projection(
            state_root=state_root,
            request_id=request_id,
            principal=principal,
            surface_registry=active_surface_registry,
        )
        stored_frontend_kind = bridge_request_surface_kind(
            before,
            registry=active_surface_registry,
        )
        if stored_frontend_kind != frontdoor:
            raise FrontdoorError("bridge request surface identity mismatch")
        assert_bridge_launch_session(
            before,
            launch_session_identity,
            frontend_kind=stored_frontend_kind,
            registry=active_surface_registry,
        )
    except FrontdoorError:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_ack_output",
            principal=principal,
            subject={"request_id": request_id, "task_id": ""},
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"reason": "request_owner_mismatch_or_missing"},
            ),
        )
        raise
    expected_projection_digest = bridge_projection_digest(projection)
    ack_verified = hmac.compare_digest(projection_digest, expected_projection_digest)
    if not ack_verified:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_ack_output",
            principal=principal,
            subject={"request_id": request_id, "task_id": str(before.get("task_id") or "")},
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={
                    "transition_effect": "none",
                    "ack_verified": False,
                    "supplied_projection_digest": projection_digest,
                    "expected_projection_digest": expected_projection_digest,
                },
            ),
        )
        raise FrontdoorError("projection digest mismatch for bridge ack_output")
    ack_binding = {
        "request_id": request_id,
        "projection_digest": projection_digest,
        "principal": redacted_principal(principal),
    }
    ack_path = state_paths(state_root)["acks"] / (
        f"{safe_id(request_id)}-{stable_digest(ack_binding)[:24]}.json"
    )
    if state_file_exists(ack_path):
        existing_ack = read_json(ack_path)
        if (
            existing_ack.get("request_id") != request_id
            or existing_ack.get("projection_digest") != projection_digest
            or existing_ack.get("principal") != redacted_principal(principal)
            or existing_ack.get("ack_verified") is not True
        ):
            raise FrontdoorError("ack idempotency artifact conflict")
        return {
            "schema_version": 1,
            "decision": "ok",
            "ack_path": str(ack_path),
            "ack_verified": True,
            "ack_replayed": True,
            "expected_projection_digest": expected_projection_digest,
            "transition_effect": "none",
            "request_status": before.get("status"),
        }
    ack = {
        "ack_version": "1",
        "request_id": request_id,
        "projection_digest": projection_digest,
        "expected_projection_digest": expected_projection_digest,
        "ack_verified": True,
        "principal": redacted_principal(principal),
        "acked_at": now_iso(),
        "transition_effect": "none",
    }
    write_json(ack_path, ack)
    after = read_json(request_path(state_root, request_id))
    append_audit_event(
        state_root=state_root,
        event_type="bridge_ack_output",
        principal=principal,
        subject={"request_id": request_id, "task_id": str(before.get("task_id") or "")},
        outcome="ok",
        details=bridge_audit_details(
            frontdoor=frontdoor,
            chat_session_id=chat_session_id,
            peer=peer,
            extra={
                "transition_effect": "none",
                "ack_verified": True,
                "projection_digest": projection_digest,
                "expected_projection_digest": expected_projection_digest,
                "request_digest_before": "sha256:" + stable_digest(before),
                "request_digest_after": "sha256:" + stable_digest(after),
            },
        ),
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "ack_path": str(ack_path),
        "ack_verified": True,
        "ack_replayed": False,
        "expected_projection_digest": expected_projection_digest,
        "transition_effect": "none",
        "request_status": after.get("status"),
    }


def principal_from_cli(principal_type: str, principal_id: str, authn_method: str) -> dict[str, str]:
    return make_principal(
        principal_type or "manual_operator",
        principal_id or "manual-cli",
        authn_method=authn_method or "local_cli",
    )


def refresh_approval_context_refs(record: dict[str, Any]) -> dict[str, Any]:
    record.setdefault("requested_context_refs", list(record.get("context_refs") or []))
    record.setdefault("requested_allowed_paths", list(record.get("allowed_paths") or []))
    requested_refs = list(record.get("requested_context_refs") or record.get("context_refs") or [])
    requested_allowed_paths = list(record.get("requested_allowed_paths") or record.get("allowed_paths") or [])
    previous_refs = record.get("resolved_context_refs")
    ref_root = recorded_checkout_ref_root(
        record,
        require_unchanged_identity=False,
    )
    refreshed = bounded_context(
        requested_refs,
        requested_allowed_paths,
        ref_root=ref_root,
    )
    if isinstance(previous_refs, list) and previous_refs:
        if ref_integrity_view(refreshed["resolved_context_refs"]) != ref_integrity_view(previous_refs):
            raise FrontdoorError("context_refs_changed_since_proposal")
    for key in (
        "requested_context_refs",
        "requested_allowed_paths",
        "context_refs",
        "resolved_context_refs",
        "allowed_paths",
        "resolved_allowed_paths",
    ):
        record[key] = refreshed[key]
    return record


def approval_record_for(
    *,
    record: dict[str, Any],
    human_action_id: str,
    principal: dict[str, Any],
    signature: dict[str, Any],
    activation_source: str,
) -> dict[str, Any]:
    return {
        "approval_record_version": "1",
        "activation_source": activation_source,
        "human_action_id": human_action_id,
        "approved_at": now_iso(),
        "approved_by_principal": redacted_principal(principal),
        "proposal_digest": "sha256:" + stable_digest(record.get("proposal") or {}),
        "request_digest": record.get("request_digest") or "sha256:" + stable_digest(
            {
                "task_id": record.get("task_id"),
                "request_id": record.get("request_id"),
                "user_prompt": record.get("user_prompt"),
                "context_refs": record.get("context_refs") or [],
                "resolved_context_refs": record.get("resolved_context_refs") or [],
                "allowed_paths": record.get("allowed_paths") or [],
            }
        ),
        "refs_digest": "sha256:" + stable_digest(record.get("resolved_context_refs") or record.get("context_refs") or []),
        "display_digest": "sha256:" + stable_digest(record.get("approval") or {}),
        "signature": signature,
    }


def enforce_frontdoor_approval_gate(envelope: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("activation_status") != "approved":
        return envelope
    task_kind = classification.get("task_kind")
    if classification.get("publication_required") or task_kind == "publication":
        reason = "publication_requires_separate_human_gate"
    elif task_kind == "policy_change":
        reason = "policy_change_requires_separate_human_gate"
    else:
        return envelope
    gated = dict(envelope)
    gated["activation_status"] = "waiting_human"
    gated["approval_required_reason"] = reason
    gated["next_action"] = "ask_human"
    gated.pop("approved_by", None)
    gated.pop("approved_at", None)
    gated.pop("goal_state_transition", None)
    return gated


def _approve_core(
    *,
    state_root: Path,
    request_id: str,
    human_action_id: str,
    activation_source: str,
    approved_by_expected: str,
    principal: dict[str, Any],
    audit_event_type: str,
    allowed_principal_types: set[str],
    record_updates: dict[str, Any] | None = None,
    audit_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    actor = principal
    path = request_path(state_root, request_id)
    record = read_json(path)
    signature = assert_allowed_principal(
        state_root=state_root,
        principal=actor,
        allowed_types=allowed_principal_types,
        transition=audit_event_type,
        subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
        blocked_reason="unsupported approval principal",
    )
    classification = record.get("classification")
    if not isinstance(classification, dict):
        raise FrontdoorError("typed classification is required before approval")
    try:
        refresh_approval_context_refs(record)
    except FrontdoorError as exc:
        append_audit_event(
            state_root=state_root,
            event_type=audit_event_type,
            principal=actor,
            subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
            outcome="blocked",
            details={"reason": str(exc)},
        )
        raise
    attach_approval_summary(record)
    expected_action_id = approval_action_id(record)
    if human_action_id != expected_action_id:
        rate = record.setdefault("approval_rate_limit", {"failed_attempts": 0})
        rate["failed_attempts"] = int(rate.get("failed_attempts") or 0) + 1
        record["updated_at"] = now_iso()
        record["approval"] = approval_summary(record)
        write_json(path, record)
        append_audit_event(
            state_root=state_root,
            event_type=audit_event_type,
            principal=actor,
            subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
            outcome="blocked",
            details={"reason": "approval_challenge_mismatch", "failed_attempts": rate["failed_attempts"]},
        )
        raise FrontdoorError("approval challenge mismatch")
    failed_attempts = int((record.get("approval_rate_limit") or {}).get("failed_attempts") or 0)
    if failed_attempts >= MAX_APPROVAL_FAILURES:
        append_audit_event(
            state_root=state_root,
            event_type=audit_event_type,
            principal=actor,
            subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
            outcome="blocked",
            details={"reason": "approval_rate_limited", "failed_attempts": failed_attempts},
        )
        raise FrontdoorError("approval challenge rate limit exceeded")
    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source=activation_source,
        task_id=record["task_id"],
        request_id=record["request_id"],
        refs=list(record.get("context_refs") or []),
        allowed_paths=list(record.get("allowed_paths") or []),
        expires_at=str(record.get("expires_at") or "run_terminal"),
    )
    envelope = enforce_frontdoor_approval_gate(envelope, classification)
    if envelope.get("activation_status") == "approved" and envelope.get("approved_by") != approved_by_expected:
        raise FrontdoorError("approved_by mapping mismatch")
    record["updated_at"] = now_iso()
    record["status"] = envelope["activation_status"]
    record["human_action_id"] = human_action_id
    if envelope["activation_status"] == "approved":
        record["approved_activation"] = envelope
    else:
        record.pop("approved_activation", None)
    if record_updates:
        record.update(record_updates)
    record["approval_record"] = approval_record_for(
        record=record,
        human_action_id=human_action_id,
        principal=actor,
        signature=signature,
        activation_source=activation_source,
    )
    write_json(path, record)
    snapshot_path = snapshot_envelope(state_root, request_id, envelope)
    details = {"request_status": envelope["activation_status"]}
    if audit_details:
        details.update(audit_details)
    append_audit_event(
        state_root=state_root,
        event_type=audit_event_type,
        principal=actor,
        subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
        outcome="ok" if envelope["activation_status"] == "approved" else "blocked",
        details=details,
    )
    return {
        "schema_version": 1,
        "decision": "ok" if envelope["activation_status"] == "approved" else "blocked",
        "request_status": envelope["activation_status"],
        "request_path": str(path),
        "envelope_snapshot_path": str(snapshot_path),
        "activation": envelope,
        "approval_record": record["approval_record"],
    }


def approve_request(
    *,
    state_root: Path,
    request_id: str,
    human_action_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _approve_core(
        state_root=state_root,
        request_id=request_id,
        human_action_id=human_action_id,
        activation_source="human_ui",
        approved_by_expected="human_ui_action",
        principal=principal or make_principal("human_operator", "human-ui", authn_method="local_ui"),
        audit_event_type="approve_request",
        allowed_principal_types={"human_operator", "manual_operator"},
    )


def validate_orchestrator_start_invocation(invocation: dict[str, Any]) -> dict[str, str]:
    required = {"skill", "invoked_at", "chat_session_id"}
    missing = sorted(key for key in required if not str(invocation.get(key) or "").strip())
    if missing:
        raise FrontdoorError("orchestrator_start_invocation_missing:" + ",".join(missing))
    if invocation.get("skill") != "orchestrator-start":
        raise FrontdoorError("orchestrator_start_invocation_skill_mismatch")
    return {key: str(invocation[key]) for key in sorted(required)}


def orchestrator_start_approve(
    *,
    state_root: Path,
    request_id: str,
    human_action_id: str,
    invocation: dict[str, Any],
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    invocation_evidence = validate_orchestrator_start_invocation(invocation)
    return _approve_core(
        state_root=state_root,
        request_id=request_id,
        human_action_id=human_action_id,
        activation_source="orchestrator-start",
        approved_by_expected="human_explicit_skill_invocation",
        principal=principal
        or make_principal(
            "orchestrator_start",
            "orchestrator-start-skill",
            authn_method="local_cli",
        ),
        audit_event_type="orchestrator_start_approve",
        allowed_principal_types={"orchestrator_start"},
        record_updates={"orchestrator_start_invocation": invocation_evidence},
        audit_details={"orchestrator_start_invocation": invocation_evidence},
    )


def manual_cli_approve(
    *,
    state_root: Path,
    request_id: str,
    human_action_id: str,
    confirm_nonce: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    actor = principal or default_manual_principal()
    expected_confirm = f"approve-{request_id}"
    if confirm_nonce != expected_confirm:
        append_audit_event(
            state_root=state_root,
            event_type="manual_cli_approve",
            principal=actor,
            subject={"request_id": request_id},
            outcome="blocked",
            details={
                "reason": "manual_confirmation_mismatch",
                "expected_confirm": expected_confirm,
            },
        )
        raise FrontdoorError("manual_confirmation_mismatch")
    return _approve_core(
        state_root=state_root,
        request_id=request_id,
        human_action_id=human_action_id,
        activation_source="manual_cli",
        approved_by_expected="manual_operator",
        principal=actor,
        audit_event_type="manual_cli_approve",
        allowed_principal_types={"manual_operator", "human_operator"},
        audit_details={"confirm_nonce": confirm_nonce},
    )


def link_request_run(record: dict[str, Any], run_id: str) -> bool:
    linked_runs = record.setdefault("linked_runs", [])
    if not isinstance(linked_runs, list):
        linked_runs = []
        record["linked_runs"] = linked_runs
    if run_id in linked_runs:
        return False
    linked_runs.append(run_id)
    return True


def sync_terminal_request_locked(
    state_root: Path,
    run: dict[str, Any],
    *,
    request_record: dict[str, Any] | None = None,
) -> bool:
    """Synchronize one exactly linked request after its run becomes terminal."""

    run_state = str(run.get("run_state") or "")
    request_status = RUN_REQUEST_TERMINAL_STATUS.get(run_state)
    if request_status is None:
        return False
    run_id = validate_artifact_id(str(run.get("run_id") or ""), "run_id")
    request_id = validate_artifact_id(str(run.get("request_id") or ""), "request_id")
    path = request_path(state_root, request_id)
    record = request_record if request_record is not None else read_json(path)
    if record.get("request_id") != request_id:
        raise FrontdoorError("terminal request run binding invalid")
    if record.get("run_id") != run_id or record.get("linked_runs") != [run_id]:
        raise FrontdoorError("terminal request run binding invalid")
    current_status = str(record.get("status") or "")
    if current_status == request_status:
        return False
    if current_status in BRIDGE_TERMINAL_STATUSES:
        raise FrontdoorError("terminal request status conflicts with terminal run")
    if current_status not in BRIDGE_PENDING_REQUEST_STATUSES:
        raise FrontdoorError("terminal request status is not synchronizable")
    record["status"] = request_status
    record["updated_at"] = now_iso()
    write_json(path, record)
    return True


def synchronize_terminal_request(
    *,
    state_root: Path,
    run_id: str,
    principal: dict[str, Any],
    operation: str,
) -> bool:
    validate_artifact_id(run_id, "run_id")
    with run_lock.hold_global_lock(
        state_root,
        operation=operation,
        run_id=run_id,
        principal=principal,
    ):
        run = run_store.load_run(state_root, run_id)
        return sync_terminal_request_locked(state_root, run)


def reconcile_pending_request_terminal_run(
    state_root: Path,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Repair a stale pending status only when its terminal run binding is exact."""

    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return record
    if record.get("linked_runs") != [run_id]:
        return record
    try:
        validate_artifact_id(run_id, "run_id")
        run = run_store.load_run(state_root, run_id)
    except (FrontdoorError, run_store.RunStoreError):
        return record
    if (
        run.get("run_id") != run_id
        or run.get("request_id") != record.get("request_id")
        or str(run.get("run_state") or "") not in RUN_REQUEST_TERMINAL_STATUS
    ):
        return record
    try:
        sync_terminal_request_locked(state_root, run, request_record=record)
    except FrontdoorError:
        return record
    return record


def create_run(
    *,
    state_root: Path,
    request_id: str,
    run_id: str,
    resume_policy: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    if run_id:
        validate_artifact_id(run_id, "run_id")
    if resume_policy not in {"manual", "daemon_future"}:
        raise FrontdoorError("resume_policy unsupported")
    actor = principal or default_manual_principal()

    record = read_json(request_path(state_root, request_id))
    envelope = record.get("approved_activation")
    if not isinstance(envelope, dict) or envelope.get("activation_status") != "approved":
        raise FrontdoorError("approved activation envelope required")
    selection = envelope.get("workflow_selection") or {}
    workflow_id = selection.get("workflow_id")
    initial_step = selection.get("initial_step")
    if not workflow_id or not initial_step:
        raise FrontdoorError("approved activation must contain selected workflow and initial step")

    template = load_template(str(workflow_id))
    effective_run_id = run_id or stable_run_id(request_id, str(workflow_id))
    signature = assert_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="create_run",
        subject={"request_id": request_id, "run_id": effective_run_id},
    )
    subject = {"request_id": request_id, "run_id": effective_run_id}
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="create_run",
            run_id=effective_run_id,
            principal=actor,
        ):
            record = read_json(request_path(state_root, request_id))
            bound_run_id = str(record.get("run_id") or "")
            if bound_run_id and bound_run_id != effective_run_id:
                raise FrontdoorError("request_id is already bound to a different run_id")
            path = run_store.run_path(state_root, effective_run_id)
            if state_file_exists(path):
                existing = run_store.load_run(state_root, effective_run_id)
                if existing.get("request_id") != request_id:
                    raise FrontdoorError("run_id conflict for different request")
                link_changed = link_request_run(record, effective_run_id)
                if not bound_run_id or link_changed:
                    record["run_id"] = effective_run_id
                    record["updated_at"] = now_iso()
                    write_json(request_path(state_root, request_id), record)
                sync_terminal_request_locked(state_root, existing)
                link_status = record_run_link_status(state_root, existing)
                append_audit_event(
                    state_root=state_root,
                    event_type="create_run",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={"created": False, "run_link": link_status},
                )
                return {
                    "schema_version": 1,
                    "decision": "ok",
                    "created": False,
                    "run_path": str(path),
                    "request_record_path": str(request_path(state_root, request_id)),
                    "envelope_snapshots": list_envelope_snapshots(state_root, request_id),
                    "workflow_run": existing,
                }

            run = {
                "run_version": "1",
                "run_id": effective_run_id,
                "task_id": record["task_id"],
                "request_id": request_id,
                "workflow_id": workflow_id,
                "goal_state": "approved",
                "run_state": "created",
                "current_step": initial_step,
                "iteration": 1,
                "max_steps": int(template.get("max_steps") or 1),
                "step_history": [],
                "activation": sanitize_activation_for_run(envelope),
                "terminal": {"status": None, "reason": None},
                "requester": record.get("requester") or requester("manual"),
                "scheduling": {
                    "scheduler_mode": "invocation-drain",
                    "concurrency_group": "global",
                    "state_persistence": "durable_state",
                    "lock_policy": "global_advisory_lock",
                    "concurrency": 1,
                    "resume_policy": resume_policy,
                },
                "context_sharing": {
                    "shared_run_state": "typed_durable_state",
                    "step_local_snapshot": "immutable_step_attempt_snapshot",
                    "provider_transcript": "confined_evidence_path_only",
                },
                "transitions": [],
                "transition_provenance": [
                    {
                        "transition": "create_run",
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                ],
            }
            path = run_store.store_run(state_root, run)
            record["run_id"] = effective_run_id
            link_request_run(record, effective_run_id)
            record["updated_at"] = now_iso()
            write_json(request_path(state_root, request_id), record)
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="create_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise

    link_status = record_run_link_status(state_root, run)
    append_audit_event(
        state_root=state_root,
        event_type="create_run",
        principal=actor,
        subject=subject,
        outcome="ok",
        details={"created": True, "run_link": link_status},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "created": True,
        "run_path": str(path),
        "request_record_path": str(request_path(state_root, request_id)),
        "envelope_snapshots": list_envelope_snapshots(state_root, request_id),
        "workflow_run": run,
    }


def drain_run(
    *,
    state_root: Path,
    run_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    path = run_store.run_path(state_root, run_id)
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="drain_run",
        subject=subject,
    )
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="drain_run",
            run_id=run_id,
            principal=actor,
        ):
            run = run_store.load_run(state_root, run_id)
            subject = {"run_id": run_id, "request_id": str(run.get("request_id") or "")}
            signature = assert_execution_principal(
                state_root=state_root,
                principal=actor,
                transition="drain_run",
                subject=subject,
            )
            if run.get("run_state") not in {"created", "step_queued"}:
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="drain_run",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={
                        "reason": "run_state_not_queueable",
                        "run_state": run.get("run_state"),
                        "run_link": link_status,
                    },
                )
                return {
                    "schema_version": 1,
                    "decision": "ok",
                    "drained": False,
                    "reason": "run_state_not_queueable",
                    "workflow_run": run,
                }

            run_lock.assert_p0_concurrency(state_root, target_run_id=run_id)
            workflow_id = str(run.get("workflow_id") or "")
            current_step_id = str(run.get("current_step") or "")
            errors: list[str] = []
            try:
                template = load_template(workflow_id)
            except FrontdoorError:
                template = {}
                errors.append(f"template_not_active:{workflow_id}")
            step = (
                next((item for item in template.get("steps", []) if item.get("id") == current_step_id), None)
                if not errors
                else None
            )
            if not errors and not isinstance(step, dict):
                errors.append(f"step_not_in_template:{current_step_id}")

            order_step_id = str((step or {}).get("id") or current_step_id)
            order_path = work_order_path(state_root, run_id, order_step_id)
            snapshot_path: Path | None = None
            work_order: dict[str, Any] = {}
            drained = False
            if not errors:
                order_exists = state_file_exists(order_path)
                if order_exists:
                    work_order = read_json(order_path)
                else:
                    request_record = read_json(request_path(state_root, str(run["request_id"])))
                    try:
                        work_order = build_work_order(
                            state_root=state_root,
                            run=run,
                            request_record=request_record,
                            template=template,
                            step=step,
                            issuer_principal=actor,
                        )
                    except FrontdoorError as exc:
                        errors.append(str(exc))
                if not errors:
                    errors.extend(
                        work_order_builder.validate_work_order(
                            work_order,
                            template=template,
                            step=step,
                            state_root=state_root,
                            run=run,
                        )
                    )
                if not errors and not order_exists:
                    write_json(order_path, work_order)
                    drained = True
                if not errors:
                    try:
                        snapshot_path = work_order_builder.freeze_step_snapshot(
                            state_root,
                            work_order,
                            iteration=int(run.get("iteration") or 1),
                        )
                    except work_order_builder.WorkOrderError as exc:
                        errors.append(str(exc))

            if errors:
                transition = run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="waiting_human",
                    reason_class="work_order_invalid",
                    transition="drain_run",
                    principal=actor,
                    artifact_refs=[str(order_path)] if state_file_exists(order_path) else [],
                    run=run,
                )
                path = run_store.run_path(state_root, run_id)
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="drain_run",
                    principal=actor,
                    subject=subject,
                    outcome="blocked",
                    details={"reason": "work_order_invalid", "errors": errors, "run_link": link_status},
                )
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "work_order_invalid",
                    "errors": errors,
                    "transition": transition,
                    "run_path": str(path),
                    "workflow_run": run,
                }

            if run["run_state"] == "created":
                run["step_history"].append(
                    {
                        "step_id": step["id"],
                        "status": "queued",
                        "queued_at": now_iso(),
                        "work_order_path": str(order_path),
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                )
                run.setdefault("transition_provenance", []).append(
                    {
                        "transition": "drain_run",
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                )
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="step_queued",
                    reason_class="step_queued",
                    transition="drain_run",
                    principal=actor,
                    artifact_refs=[str(order_path), str(snapshot_path)] if snapshot_path else [str(order_path)],
                    run=run,
                )
                path = run_store.run_path(state_root, run_id)
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="drain_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise

    link_status = record_run_link_status(state_root, run)
    append_audit_event(
        state_root=state_root,
        event_type="drain_run",
        principal=actor,
        subject=subject,
        outcome="ok" if drained else "replayed",
        details={"drained": drained, "run_link": link_status},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "drained": drained,
        "run_path": str(path),
        "work_order_path": str(order_path),
        "step_snapshot_path": str(snapshot_path) if snapshot_path else None,
        "workflow_run": run,
        "work_order": work_order,
    }


def resume_run(
    *,
    state_root: Path,
    run_id: str,
    requeue: bool = False,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="resume_run",
        subject=subject,
    )
    try:
        payload = run_lifecycle.resume_run(
            state_root,
            run_id,
            principal=actor,
            requeue=requeue,
        )
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="resume_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise
    except run_lifecycle.LifecycleError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="resume_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "errors": exc.errors},
        )
        raise

    workflow_run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
    synchronize_terminal_request(
        state_root=state_root,
        run_id=run_id,
        principal=actor,
        operation="resume_run_terminal_request_sync",
    )
    subject = {"run_id": run_id, "request_id": str(workflow_run.get("request_id") or "")}
    outcome = "ok"
    if payload.get("decision") == "blocked":
        outcome = "blocked"
    elif payload.get("reason") == "terminal_run_already_set":
        outcome = "replayed"
    append_audit_event(
        state_root=state_root,
        event_type="resume_run",
        principal=actor,
        subject=subject,
        outcome=outcome,
        details={
            "resumed": bool(payload.get("resumed")),
            "reason": payload.get("reason"),
            "next_action": payload.get("next_action"),
            "requeue": requeue,
        },
    )
    return payload


def abort_run(
    *,
    state_root: Path,
    run_id: str,
    reason: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="abort_run",
        subject=subject,
    )
    try:
        payload = run_lifecycle.abort_run(
            state_root,
            run_id,
            reason=reason,
            principal=actor,
        )
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="abort_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise
    except run_lifecycle.LifecycleError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="abort_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "errors": exc.errors},
        )
        raise

    workflow_run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
    synchronize_terminal_request(
        state_root=state_root,
        run_id=run_id,
        principal=actor,
        operation="abort_run_terminal_request_sync",
    )
    subject = {"run_id": run_id, "request_id": str(workflow_run.get("request_id") or "")}
    append_audit_event(
        state_root=state_root,
        event_type="abort_run",
        principal=actor,
        subject=subject,
        outcome="ok" if payload.get("aborted") else "replayed",
        details={
            "aborted": bool(payload.get("aborted")),
            "reason": payload.get("reason"),
        },
    )
    return payload


def claude_headless_capability() -> dict[str, Any]:
    adapters = provider_runner.load_provider_adapters()
    adapter = adapters.get("claude_headless_p0")
    if adapter:
        return adapter
    return {
        "adapter_contract_version": "1",
        "provider_adapter_id": "claude_headless_p0",
        "provider_target": "claude_headless",
        "transport": "headless_cli",
        "sync_mode": "sync",
        "context_freshness": "fresh_process",
        "concurrency_unit": "process",
        "permission_enforcement": "harness",
        "supports_structured_output": True,
        "requires_marker": False,
        "reset_strategy": "new_session",
        "report_authority": "typed_report_and_evidence_file",
    }


def manual_provider_evidence_contract(
    *,
    run: dict[str, Any],
    step_id: str,
    capability: dict[str, Any],
    report_path_value: str,
    evidence_path: Path,
    transcript_path: Path,
) -> dict[str, Any]:
    fixed_fields = {
        "evidence_version": "1",
        "provider_adapter_id": capability["provider_adapter_id"],
        "provider_target": capability["provider_target"],
        "request_id": run["request_id"],
        "run_id": run["run_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "transcript_path": str(transcript_path),
        "evidence_path": str(evidence_path),
        "outcome": "ok",
        "raw_transcript_policy": "signal_only_not_shared",
    }
    for field in ("transport", "bridge_pattern", "surface_metadata"):
        if capability.get(field) is not None:
            fixed_fields[field] = capability[field]
    return {
        "schema_path": "organization/runtime/workflows/schemas/provider-evidence.schema.json",
        "fixed_fields": fixed_fields,
        "provider_supplied_fields": {
            "required": [
                "provider",
                "effective_model",
                "provider_request_id",
                "provider_session_id",
                "duration_ms",
                "usage",
            ],
            "optional": [
                "reason_class",
                "stdout_sha256",
                "exit_code",
                "timed_out",
            ],
        },
        "allowed_usage_fields": ["input_tokens", "output_tokens"],
        "allowed_surface_metadata_fields": [
            "surface",
            "async_callback_supported",
            "domain_ownership",
            "routing_candidate_for",
        ],
        "canonical_paths": {
            "report_path": report_path_value,
            "evidence_path": str(evidence_path),
            "transcript_path": str(transcript_path),
        },
        "raw_content_policy": {
            "unlisted_fields": "forbidden",
            "raw_provider_content": "forbidden",
            "raw_transcript_policy": "signal_only_not_shared",
        },
    }


def prepare_claude_adapter(
    *,
    state_root: Path,
    run_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="prepare_claude_adapter",
        subject={"run_id": run_id},
    )
    with run_lock.hold_global_lock(
        state_root,
        operation="prepare_claude_adapter",
        run_id=run_id,
        principal=actor,
    ):
        return _prepare_claude_adapter_locked(
            state_root=state_root,
            run_id=run_id,
            actor=actor,
        )


def _prepare_claude_adapter_locked(
    *,
    state_root: Path,
    run_id: str,
    actor: dict[str, Any],
) -> dict[str, Any]:
    run = run_store.load_run(state_root, run_id)
    run_state = str(run.get("run_state") or "")
    if run_state != "step_queued":
        append_audit_event(
            state_root=state_root,
            event_type="prepare_claude_adapter",
            principal=actor,
            subject={"run_id": run_id, "request_id": str(run.get("request_id") or "")},
            outcome="blocked",
            details={"reason": "run_not_preparable", "run_state": run_state},
        )
        return {
            "schema_version": 1,
            "decision": "blocked",
            "reason": "run_not_preparable",
            "run_state": run_state,
        }
    signature = assert_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="prepare_claude_adapter",
        subject={"run_id": run_id, "request_id": str(run.get("request_id") or "")},
    )
    step_id = str(run["current_step"])
    order_path = work_order_path(state_root, run_id, step_id)
    work_order = read_json(order_path)
    errors = validate_work_order_for_adapter(work_order)
    if errors:
        return {
            "schema_version": 1,
            "decision": "blocked",
            "reason": "work_order_not_adapter_safe",
            "errors": errors,
        }

    capability = claude_headless_capability()
    evidence_path = provider_evidence_path(state_root, run_id, step_id)
    transcript_path = provider_transcript_path(state_root, run_id, step_id)
    request_path = adapter_request_path(
        state_root,
        run_id,
        step_id,
        capability["provider_adapter_id"],
    )
    artifact_paths = {
        "adapter_request": request_path,
        "report": report_path(state_root, run_id, step_id),
        "evidence": evidence_path,
        "transcript": transcript_path,
    }
    existing_artifacts = sorted(
        label for label, artifact_path in artifact_paths.items() if state_file_exists(artifact_path)
    )
    if existing_artifacts:
        append_audit_event(
            state_root=state_root,
            event_type="prepare_claude_adapter",
            principal=actor,
            subject={"run_id": run_id, "request_id": str(run.get("request_id") or "")},
            outcome="blocked",
            details={
                "reason": "manual_handoff_artifacts_exist",
                "artifacts": existing_artifacts,
            },
        )
        return {
            "schema_version": 1,
            "decision": "blocked",
            "reason": "manual_handoff_artifacts_exist",
            "artifacts": existing_artifacts,
        }
    evidence_contract = manual_provider_evidence_contract(
        run=run,
        step_id=step_id,
        capability=capability,
        report_path_value=str(work_order["report_path"]),
        evidence_path=evidence_path,
        transcript_path=transcript_path,
    )
    prompt = bounded_claude_prompt(work_order, evidence_contract=evidence_contract)
    adapter_request = {
        "adapter_request_version": "1",
        "deprecated": True,
        "execution_allowed": False,
        "replacement": "run-provider --live",
        "adapter": capability,
        "run_id": run_id,
        "request_id": run["request_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "work_order_path": str(order_path),
        "report_path": work_order["report_path"],
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "evidence_contract": evidence_contract,
        "prompt": prompt,
        "authority": {
            "provider_may_write": [],
            "runner_writes": ["typed_report_file", "normalized_provider_evidence_file", "provider_transcript_file"],
            "provider_must_not": ["select_workflow", "approve_activation", "mutate_run_state", "edit_repo", "commit", "push"],
            "issued_by_principal": redacted_principal(actor),
            "prepare_signature": signature,
            "work_order_signature": work_order.get("work_order_authority", {}).get("signature"),
        },
    }
    provider_runner.secure_artifact_tree(state_root, request_path, "adapter-requests")
    provider_runner.private_atomic_write_json(state_root, request_path, adapter_request)
    provider_runner.write_signal_transcript(
        state_root,
        transcript_path,
        {
            "outcome": "manual_handoff_prepared",
            "adapter_request_path": str(request_path),
        },
    )
    append_audit_event(
        state_root=state_root,
        event_type="prepare_claude_adapter",
        principal=actor,
        subject={"run_id": run_id, "request_id": str(run.get("request_id") or "")},
        outcome="ok",
        details={"adapter_request_path": str(request_path)},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "adapter_request_path": str(request_path),
        "adapter_request": adapter_request,
    }


def validate_work_order_for_adapter(work_order: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if work_order.get("workflow_id") != "single_step_external_review":
        errors.append("only single_step_external_review is supported")
    if work_order.get("step_id") != "review":
        errors.append("only review step is supported")
    if work_order.get("permission_mode") != "readonly":
        errors.append("permission_mode must be readonly")
    allowed_ops = ((work_order.get("activation_scope") or {}).get("allowed_ops") or {})
    for op in ("edit", "commit", "push", "network"):
        if allowed_ops.get(op) is not False:
            errors.append(f"activation_scope.allowed_ops.{op} must be false")
    context_scope = work_order.get("context_scope") or {}
    if context_scope.get("raw_transcript_sharing") != "forbidden":
        errors.append("raw transcript sharing must be forbidden")
    if not work_order.get("context_refs"):
        errors.append("context_refs must be non-empty")
    authority = work_order.get("work_order_authority")
    if not isinstance(authority, dict):
        errors.append("work_order_authority must be object")
    else:
        issuer = authority.get("issuer_principal")
        if not isinstance(issuer, dict):
            errors.append("work_order_authority.issuer_principal must be object")
        elif issuer.get("principal_type") == BRIDGE_PRINCIPAL_TYPE:
            errors.append("bridge principal cannot issue work orders")
        signature = authority.get("signature")
        if not isinstance(signature, dict) or not str(signature.get("signature", "")).startswith("sha256:"):
            errors.append("work_order_authority.signature must be present")
        runner_claim = authority.get("runner_claim")
        if not isinstance(runner_claim, dict) or runner_claim.get("claim_state") not in {"unclaimed", "claimed"}:
            errors.append("work_order_authority.runner_claim must be present")
    return errors


def bounded_claude_prompt(work_order: dict[str, Any], *, evidence_contract: dict[str, Any]) -> str:
    refs = "\n".join(f"- {item.get('value', item)}" for item in work_order.get("context_refs", []))
    fixed_fields = json.dumps(
        evidence_contract["fixed_fields"],
        ensure_ascii=False,
        sort_keys=True,
    )
    provider_fields = evidence_contract["provider_supplied_fields"]
    canonical_paths = evidence_contract["canonical_paths"]
    return "\n".join(
        [
            "You are the bounded reviewer for a deterministic P0 orchestrator work order.",
            "Do not select workflows, approve activation, mutate run state, edit repository or task files, commit, push, or publish.",
            "Do not write files. This deprecated prompt artifact is not executable.",
            "Use only the bounded context refs listed below. Do not request or infer raw transcript sharing.",
            "",
            f"task_id: {work_order['task_id']}",
            f"request_id: {work_order['request_id']}",
            f"run_id: {work_order['run_id']}",
            f"workflow_id: {work_order['workflow_id']}",
            f"step_id: {work_order['step_id']}",
            "",
            "Context refs:",
            refs,
            "",
            "Instruction:",
            work_order["instruction"],
            "",
            "Return only an External Review Report JSON object matching",
            "organization/runtime/workflows/schemas/external-review-report.schema.json.",
            "The live runner alone writes Normalized Provider Evidence matching",
            f"{evidence_contract['schema_path']}.",
            f"Use these evidence fields exactly: {fixed_fields}",
            "Supply these required runtime evidence fields: " + ", ".join(provider_fields["required"]),
            "Optional runtime evidence fields are limited to: " + ", ".join(provider_fields["optional"]),
            "usage fields are limited to non-negative integer counters: "
            + ", ".join(evidence_contract["allowed_usage_fields"]),
            "surface_metadata fields are limited to: "
            + ", ".join(evidence_contract["allowed_surface_metadata_fields"]),
            "Do not embed raw prompts, raw provider output, stdout, stderr, pane output, or raw transcript content",
            "in the report or evidence artifact. Unlisted evidence fields are forbidden.",
            f"The runner-owned normalized evidence path is: {canonical_paths['evidence_path']}",
            f"Set provider_evidence.evidence_path to: {canonical_paths['evidence_path']}",
            f"Set provider_evidence.transcript_path to: {canonical_paths['transcript_path']}",
            f"The canonical report path is: {canonical_paths['report_path']}",
        ]
    )


def validate_report(
    *,
    state_root: Path,
    run_id: str,
    report_path_arg: str = "",
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor = principal or make_principal("harness_runner", "local-harness", authn_method="local_cli")
    try:
        payload = report_gate.gate_report(
            state_root,
            run_id,
            report_path_arg=report_path_arg,
            principal=actor,
        )
    except report_gate.ReportGateError as exc:
        raise FrontdoorError(str(exc)) from exc
    synchronize_terminal_request(
        state_root=state_root,
        run_id=run_id,
        principal=actor,
        operation="validate_report_terminal_request_sync",
    )
    return payload


def run_provider(
    *,
    state_root: Path,
    run_id: str,
    adapter_id: str = provider_runner.DEFAULT_ADAPTER_ID,
    timeout_seconds: int = provider_runner.DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    fake_provider_mode: str = "",
    live: bool = False,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor = principal or make_principal("harness_runner", "local-harness", authn_method="local_cli")
    try:
        payload = provider_runner.run_provider(
            state_root=state_root,
            run_id=run_id,
            adapter_id=adapter_id,
            timeout_seconds=timeout_seconds,
            fake_provider_mode=fake_provider_mode,
            live=live,
            principal=actor,
        )
    except provider_runner.ProviderRunnerError as exc:
        raise FrontdoorError(str(exc)) from exc
    synchronize_terminal_request(
        state_root=state_root,
        run_id=run_id,
        principal=actor,
        operation="run_provider_terminal_request_sync",
    )
    return payload


def verify_completion(
    *,
    state_root: Path,
    run_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor = principal or make_principal("harness_runner", "local-harness", authn_method="local_cli")
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="verify_completion",
        subject={"run_id": run_id},
    )
    payload = completion_gate.verify_completion(
        state_root,
        run_id,
        principal=actor,
        annotate=True,
    )
    append_audit_event(
        state_root=state_root,
        event_type="verify_completion",
        principal=actor,
        subject={"run_id": run_id, "task_id": str(payload.get("task_id") or "")},
        outcome="ok" if payload.get("decision") == "complete" else "blocked",
        details={
            "decision": payload.get("decision"),
            "reasons": payload.get("reasons") or [],
            "skipped": payload.get("skipped") or [],
        },
    )
    return payload


def _assert_action_gateway_host(principal: dict[str, Any], *, state_root: Path, transition: str, subject: dict[str, Any]) -> None:
    assert_allowed_principal(
        state_root=state_root,
        principal=principal,
        allowed_types=ACTION_GATEWAY_PRINCIPAL_TYPES,
        transition=transition,
        subject=subject,
        blocked_reason=f"{transition} requires action gateway executor",
    )


def _scoped_worker_worktree_root() -> Path:
    configured = os.environ.get("SAIHAI_SCOPED_WORKTREE_ROOT")
    if not configured:
        raise FrontdoorError("SAIHAI_SCOPED_WORKTREE_ROOT is required")
    return Path(configured).expanduser()


def _scoped_worker_repo_root() -> Path:
    configured = os.environ.get("SAIHAI_SCOPED_REPO_ROOT")
    return Path(configured).expanduser() if configured else REPO_ROOT


def _require_assurance_claim(
    profile_id: str,
    claim: str,
    *,
    expected_checkout: Path,
    live_context: Any,
) -> Any:
    # Lazy import avoids a module cycle: assurance recomputes checkout identity
    # through this module on every require call.
    import agent_integration_assurance as assurance

    return assurance.require_claim(
        SCOPED_WORKER_ASSURANCE_ROOT,
        profile_id,
        claim,
        live_context=live_context,
        expected_checkout=expected_checkout,
    )


def _claim_capability_binding(verified: Any) -> dict[str, Any]:
    evidence_digests = sorted(str(item) for item in verified.evidence_digests)
    return {
        "profile_id": str(verified.profile_id),
        "claim": str(verified.claim),
        "attestation_digest": str(verified.attestation_digest),
        "profile_subject_digest": str(verified.profile_subject_digest),
        "subject_binding_digest": str(verified.subject_binding_digest),
        "bindings_digest": scoped_worker_executor.sha256_digest(dict(verified.bindings)),
        "evidence_set_digest": scoped_worker_executor.sha256_digest(evidence_digests),
        "checkout_identity_digest": str(verified.checkout_identity_digest),
    }


def _claim_worker_runtime_binding(verified: Any) -> dict[str, Any]:
    value = getattr(verified, "worker_execution_binding", None)
    try:
        normalized = scoped_worker_executor.verify_worker_runtime_binding(value)
    except scoped_worker_executor.ScopedWorkerError as exc:
        raise FrontdoorError("worker_attested_runtime_binding_missing") from exc
    return normalized


def _live_claim_context_from_launch(identity: dict[str, Any]) -> Any:
    """Build the typed assurance subject only from a revalidated host record."""

    import agent_integration_assurance as assurance

    return assurance.LiveClaimContext(
        subject_pid=int(identity["subject_pid"]),
        process_start_token=str(identity["process_start_token"]),
        supervisor_pid=int(identity["supervisor_pid"]),
        supervisor_start_token=str(identity["supervisor_start_token"]),
        executable_realpath=str(identity["native_realpath"]),
        launch_argv_digest=str(identity["launch_argv_digest"]),
        profile_realpath=str(identity["profile_realpath"]),
        profile_digest=str(identity["profile_digest"]),
        checkout_identity_digest=str(identity["checkout_identity_digest"]),
    )


def _require_managed_worker_claim(*, worker_repo_root: Path) -> Any:
    """Keep the pre-launch worker claim suppressed until a real worker exists."""

    import agent_integration_assurance as assurance

    if worker_repo_root.resolve(strict=True) != worker_repo_root:
        raise FrontdoorError("worker repository identity is not canonical")
    if assurance.WORKER_PROMOTION_BLOCKED_OPERATIONS:
        raise FrontdoorError("worker_denial_facts_not_promotable")
    # A capability is derived before the worker process exists.  Never invent a
    # PID/start-token context to cross the central authority gate.  A future
    # promotion requires a second, post-launch gate bound to the actual worker.
    raise FrontdoorError("managed_worker_live_context_unavailable")


def _audit_assurance_block(
    *,
    state_root: Path,
    principal: dict[str, Any],
    subject: dict[str, Any],
    profile_id: str,
    claim: str,
    error: Exception,
) -> None:
    append_audit_event(
        state_root=state_root,
        event_type="scoped_worker_assurance_gate",
        principal=principal,
        subject=subject,
        outcome="blocked",
        details={
            "profile_id": profile_id,
            "claim": claim,
            "reason": str(getattr(error, "reason", error)),
            "reasons": list(getattr(error, "reasons", ())),
        },
    )


def _require_scoped_worker_assurance(
    *,
    state_root: Path,
    request_id: str,
    frontend_profile_id: str,
    checkout_identity_digest: str,
    worker_repo_root: Path,
    principal: dict[str, Any],
    subject: dict[str, Any],
    launch_session_verifier: Any | None = None,
    surface_registry: frontdoor_surface_registry.SurfaceRegistry | None = None,
) -> dict[str, Any]:
    try:
        record = read_json(request_path(state_root, request_id))
        active_surface_registry = surface_registry or load_surface_registry()
        frontend_kind = bridge_request_surface_kind(
            record,
            registry=active_surface_registry,
        )
        descriptor = active_surface_registry.descriptor(frontend_kind)
        if descriptor.assurance_profile_id != frontend_profile_id:
            raise FrontdoorError(
                "frontend surface assurance profile does not match capability subject"
            )
        expected_owner = {
            "principal_type": BRIDGE_PRINCIPAL_TYPE,
            "principal_id": frontend_profile_id,
            "authn_method": "installed_frontend_profile",
        }
        if bridge_owner_principal(record) != expected_owner:
            raise FrontdoorError("frontend request owner does not match capability subject")
        identity = validate_checkout_identity(record.get("checkout_identity"))
        if (
            record.get("workspace_id") != SCOPED_WORKER_REPO_FULL_NAME
            or record.get("checkout_identity_digest") != checkout_identity_digest
            or identity.get("identity_digest") != checkout_identity_digest
        ):
            raise FrontdoorError("frontend checkout identity does not match capability subject")
        try:
            launch_identity = active_surface_registry.normalize_launch_session(
                frontend_kind,
                record.get("launch_session_identity"),
            )
        except frontdoor_surface_registry.SurfaceRegistryError as exc:
            raise FrontdoorError(str(exc)) from exc
        if launch_identity is None:
            raise FrontdoorError("frontend launch-session request binding missing")
        if record.get("launch_session_digest") != launch_identity["record_digest"]:
            raise FrontdoorError("frontend launch-session request binding mismatch")
        current_checkout = resolve_checkout_identity(
            workspace_id=str(record["workspace_id"]),
            managed_primary=identity["managed_primary_realpath"],
            checkout_root=identity["checkout_realpath"],
        )
        try:
            verifier = launch_session_verifier or active_surface_registry.make_launch_session_verifier(
                frontend_kind
            )
        except frontdoor_surface_registry.SurfaceRegistryError as exc:
            raise FrontdoorError(str(exc)) from exc
        if verifier is None:
            raise FrontdoorError("frontend launch-session verifier required")
        revalidated_launch = verifier.revalidate(
            launch_identity,
            checkout_identity=current_checkout,
        )
        live_context = _live_claim_context_from_launch(revalidated_launch)
        frontend = _require_assurance_claim(
            frontend_profile_id,
            "action_enforced",
            expected_checkout=Path(identity["checkout_realpath"]),
            live_context=live_context,
        )
        if frontend.checkout_identity_digest != checkout_identity_digest:
            raise FrontdoorError("frontend attestation checkout identity mismatch")
        if frontend.subject_binding_digest != scoped_worker_executor.sha256_digest(
            live_context.as_dict()
        ):
            raise FrontdoorError("frontend live subject binding mismatch")
    except Exception as exc:
        _audit_assurance_block(
            state_root=state_root,
            principal=principal,
            subject=subject,
            profile_id=frontend_profile_id,
            claim="action_enforced",
            error=exc,
        )
        reason = str(getattr(exc, "reason", exc))
        raise FrontdoorError(f"frontend_action_assurance_required:{reason}") from exc

    try:
        worker = _require_managed_worker_claim(worker_repo_root=worker_repo_root)
        worker_runtime_binding = _claim_worker_runtime_binding(worker)
    except Exception as exc:
        _audit_assurance_block(
            state_root=state_root,
            principal=principal,
            subject=subject,
            profile_id=SCOPED_WORKER_ASSURANCE_PROFILE_ID,
            claim="managed_worker",
            error=exc,
        )
        reason = str(getattr(exc, "reason", exc))
        raise FrontdoorError(f"worker_managed_assurance_required:{reason}") from exc

    binding = {
        "binding_version": scoped_worker_executor.ASSURANCE_BINDING_VERSION,
        "frontend_action": _claim_capability_binding(frontend),
        "worker_managed": _claim_capability_binding(worker),
    }
    return {
        "assurance_binding": scoped_worker_executor.normalize_assurance_binding(binding),
        "worker_runtime_binding": worker_runtime_binding,
        "launch_session_identity": revalidated_launch,
    }


def derive_scoped_worker_capability(
    *,
    state_root: Path,
    run_id: str,
    step_id: str,
    principal: dict[str, Any],
    launch_session_verifier: Any | None = None,
) -> dict[str, Any]:
    os.umask(0o077)
    subject = {"run_id": run_id, "step_id": step_id}
    _assert_action_gateway_host(
        principal,
        state_root=state_root,
        transition="derive_scoped_worker_capability",
        subject=subject,
    )
    try:
        work_order, _work_order_digest = scoped_worker_executor.load_frozen_work_order(
            state_root,
            run_id=run_id,
            step_id=step_id,
        )
        frontend_request_binding = work_order.get("frontend_request_binding")
        owner_principal = (
            frontend_request_binding.get("owner_principal")
            if isinstance(frontend_request_binding, dict)
            else None
        )
        if not isinstance(owner_principal, dict):
            raise scoped_worker_executor.ScopedWorkerError("frontend_request_binding_missing")
        worker_repo_root = _scoped_worker_repo_root().resolve(strict=True)
        authority = _require_scoped_worker_assurance(
            state_root=state_root,
            request_id=str(work_order.get("request_id") or ""),
            frontend_profile_id=str(owner_principal.get("principal_id") or ""),
            checkout_identity_digest=str(
                frontend_request_binding.get("checkout_identity_digest") or ""
            ),
            worker_repo_root=worker_repo_root,
            principal=principal,
            subject=subject,
            launch_session_verifier=launch_session_verifier,
        )
        capability = scoped_worker_executor.derive_capability_from_state(
            state_root=state_root,
            run_id=run_id,
            step_id=step_id,
            repo_root=worker_repo_root,
            repo_full_name=SCOPED_WORKER_REPO_FULL_NAME,
            worktree_root=_scoped_worker_worktree_root(),
            principal=scoped_worker_executor.executor_principal(principal),
            gateway_principal=principal,
            signing_key=scoped_worker_executor.load_executor_key(),
            assurance_binding=authority["assurance_binding"],
            worker_runtime_binding=authority["worker_runtime_binding"],
        )
    except scoped_worker_executor.ScopedWorkerError as exc:
        raise FrontdoorError(exc.reason_class) from exc
    return {
        "schema_version": 1,
        "decision": "ok",
        "capability_id": capability["capability_id"],
        "capability_digest": capability["capability_digest"],
        "task_id": capability["task_id"],
        "run_id": capability["run_id"],
        "step_id": capability["step_id"],
        "backend_id": capability["worker_backend"]["backend_id"],
        "assurance_binding_digest": capability["assurance_binding_digest"],
        "expires_at": capability["expires_at"],
    }


def execute_scoped_worker(
    *,
    state_root: Path,
    capability_id: str,
    principal: dict[str, Any],
    launch_session_verifier: Any | None = None,
) -> dict[str, Any]:
    os.umask(0o077)
    subject = {"capability_id": capability_id}
    _assert_action_gateway_host(
        principal,
        state_root=state_root,
        transition="execute_scoped_worker",
        subject=subject,
    )
    try:
        capability = scoped_worker_executor._load_canonical_capability(
            state_root,
            capability_id,
        )
        stored_assurance = scoped_worker_executor.normalize_assurance_binding(
            capability.get("assurance_binding")
        )
        authority = _require_scoped_worker_assurance(
            state_root=state_root,
            request_id=str(capability.get("request_id") or ""),
            frontend_profile_id=str(stored_assurance["frontend_action"]["profile_id"]),
            checkout_identity_digest=str(
                stored_assurance["frontend_action"]["checkout_identity_digest"]
            ),
            worker_repo_root=Path(str(capability.get("repository", {}).get("repo_root") or "")),
            principal=principal,
            subject=subject,
            launch_session_verifier=launch_session_verifier,
        )
        return scoped_worker_executor.execute_capability(
            state_root=state_root,
            capability_id=capability_id,
            principal=scoped_worker_executor.executor_principal(principal),
            gateway_principal=principal,
            signing_key=scoped_worker_executor.load_executor_key(),
            current_assurance_binding=authority["assurance_binding"],
            current_launch_session_identity=authority["launch_session_identity"],
            current_worker_runtime_binding=authority["worker_runtime_binding"],
        )
    except scoped_worker_executor.ScopedWorkerError as exc:
        raise FrontdoorError(exc.reason_class) from exc


def render_vault_evidence_markdown(block: dict[str, Any]) -> str:
    return completion_gate.render_vault_evidence_markdown(block)


def validate_external_review_report(
    report: dict[str, Any],
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    return report_gate.validate_external_review_report(
        report,
        run=run,
        work_order=work_order,
        state_root=state_root,
    )


def validate_provider_evidence(
    value: Any,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    return report_gate.validate_provider_evidence(value, run, work_order, state_root)


def validate_findings(value: Any, result: Any) -> list[str]:
    return report_gate.validate_findings(value, result)


def validate_authority(value: Any) -> list[str]:
    return report_gate.validate_authority(value)


def build_work_order(
    *,
    state_root: Path,
    run: dict[str, Any],
    request_record: dict[str, Any],
    template: dict[str, Any],
    step: dict[str, Any],
    issuer_principal: dict[str, Any],
) -> dict[str, Any]:
    resolved_refs = verified_context_refs_for_work_order(request_record)
    if not isinstance(resolved_refs, list) or not resolved_refs:
        refs = run["activation"]["context_scope"]["refs"]
        resolved_refs = [{"type": "repo_file", "value": ref, "path": ref} for ref in refs]
    step_id = str(step["id"])
    worker_execution_plan = None
    if str(step.get("permission_mode") or "") == "edit" and os.environ.get("SAIHAI_SCOPED_CODEX_EXECUTABLE"):
        owner_principal = request_record.get("owner_principal")
        checkout_identity_digest = request_record.get("checkout_identity_digest")
        if not isinstance(owner_principal, dict) or not isinstance(
            checkout_identity_digest, str
        ):
            raise FrontdoorError("frontend_request_binding_missing")
        try:
            worker_execution_plan = scoped_worker_executor.build_execution_plan(
                task_id=str(run["task_id"]),
                request_id=str(run["request_id"]),
                run_id=str(run["run_id"]),
                step_id=step_id,
                owner_principal=owner_principal,
                checkout_identity_digest=checkout_identity_digest,
                repo_root=_scoped_worker_repo_root(),
                repo_full_name=SCOPED_WORKER_REPO_FULL_NAME,
            )
        except scoped_worker_executor.ScopedWorkerError as exc:
            raise FrontdoorError(exc.reason_class) from exc
    work_order = work_order_builder.build_work_order(
        run=run,
        request_record=request_record,
        template=template,
        step=step,
        issuer_principal_redacted=redacted_principal(issuer_principal),
        resolved_refs=resolved_refs,
        policy_digest_value=policy_digest(request_record["approved_activation"]),
        signature=None,
        report_path_value=str(report_path(state_root, str(run["run_id"]), step_id)),
        worker_execution_plan=worker_execution_plan,
    )
    unsigned_digest = stable_digest(work_order)
    work_order["work_order_authority"]["signature"] = sign_transition(
        state_root=state_root,
        principal=issuer_principal,
        transition="issue_work_order",
        subject={"unsigned_work_order_digest": "sha256:" + unsigned_digest},
    )
    return work_order


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Host-owned P0 frontdoor orchestrator")
    surface_choices = registered_surface_kinds()
    parser.add_argument("--state-root", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    propose = sub.add_parser("propose")
    propose.add_argument("--task-id", required=True)
    propose.add_argument("--request-id", required=True)
    propose.add_argument("--prompt", default="")
    propose.add_argument("--classification", default="")
    propose.add_argument("--ref", action="append", default=[])
    propose.add_argument("--allowed-path", action="append", default=[])
    propose.add_argument("--expires-at", default="run_terminal")
    propose.add_argument(
        "--frontdoor",
        choices=surface_choices,
        default="codex",
    )
    propose.add_argument("--chat-session-id", default="")
    propose.add_argument("--principal-type", default="manual_operator")
    propose.add_argument("--principal-id", default="manual-cli")
    propose.add_argument("--authn-method", default="local_cli")

    approve = sub.add_parser("approve")
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--human-action-id", required=True)
    approve.add_argument("--principal-type", default="human_operator")
    approve.add_argument("--principal-id", default="human-ui")
    approve.add_argument("--authn-method", default="local_ui")

    orchestrator_start = sub.add_parser("orchestrator-start-approve")
    orchestrator_start.add_argument("--request-id", required=True)
    orchestrator_start.add_argument("--human-action-id", required=True)
    orchestrator_start.add_argument("--invoked-at", required=True)
    orchestrator_start.add_argument("--chat-session-id", required=True)

    manual = sub.add_parser("manual-approve")
    manual.add_argument("--request-id", required=True)
    manual.add_argument("--human-action-id", required=True)
    manual.add_argument("--confirm", required=True)
    manual.add_argument("--principal-type", default="manual_operator")
    manual.add_argument("--principal-id", default="manual-cli")
    manual.add_argument("--authn-method", default="local_cli")

    create = sub.add_parser("create-run")
    create.add_argument("--request-id", required=True)
    create.add_argument("--run-id", default="")
    create.add_argument("--resume-policy", choices=["manual", "daemon_future"], default="manual")
    create.add_argument("--principal-type", default="manual_operator")
    create.add_argument("--principal-id", default="manual-cli")
    create.add_argument("--authn-method", default="local_cli")

    drain = sub.add_parser("drain")
    drain.add_argument("--run-id", required=True)
    drain.add_argument("--principal-type", default="manual_operator")
    drain.add_argument("--principal-id", default="manual-cli")
    drain.add_argument("--authn-method", default="local_cli")

    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--requeue", action="store_true")
    resume.add_argument("--principal-type", default="manual_operator")
    resume.add_argument("--principal-id", default="manual-cli")
    resume.add_argument("--authn-method", default="local_cli")

    abort = sub.add_parser("abort")
    abort.add_argument("--run-id", required=True)
    abort.add_argument("--reason", required=True)
    abort.add_argument("--principal-type", default="manual_operator")
    abort.add_argument("--principal-id", default="manual-cli")
    abort.add_argument("--authn-method", default="local_cli")

    sub.add_parser("adapter-capability")

    adapter = sub.add_parser("prepare-claude-adapter")
    adapter.add_argument("--run-id", required=True)
    adapter.add_argument("--principal-type", default="manual_operator")
    adapter.add_argument("--principal-id", default="manual-cli")
    adapter.add_argument("--authn-method", default="local_cli")

    report = sub.add_parser("validate-report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--report-path", default="")
    report.add_argument("--principal-type", default="harness_runner")
    report.add_argument("--principal-id", default="local-harness")
    report.add_argument("--authn-method", default="local_cli")

    run_provider_parser = sub.add_parser("run-provider")
    run_provider_parser.add_argument("--run-id", required=True)
    run_provider_parser.add_argument("--adapter-id", default=provider_runner.DEFAULT_ADAPTER_ID)
    run_provider_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=provider_runner.DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    )
    run_provider_parser.add_argument("--live", action="store_true")
    run_provider_parser.add_argument(
        "--fake-provider-mode",
        choices=["", "success", "findings", "blocked", "timeout", "nonzero", "malformed", "unavailable"],
        default="",
    )
    run_provider_parser.add_argument("--principal-type", default="harness_runner")
    run_provider_parser.add_argument("--principal-id", default="local-harness")
    run_provider_parser.add_argument("--authn-method", default="local_cli")

    completion = sub.add_parser("verify-completion")
    completion.add_argument("--run-id", required=True)
    completion.add_argument("--format", choices=["json", "markdown"], default="json")
    completion.add_argument("--principal-type", default="harness_runner")
    completion.add_argument("--principal-id", default="local-harness")
    completion.add_argument("--authn-method", default="local_cli")

    task = sub.add_parser("task-view")
    task.add_argument("--task-id", required=True)
    task.add_argument("--format", choices=["json"], default="json")
    task.add_argument("--principal-type", default="manual_operator")
    task.add_argument("--principal-id", default="manual-cli")
    task.add_argument("--authn-method", default="local_cli")

    sub.add_parser("lock-status")

    bridge_submit = sub.add_parser("bridge-submit-request")
    bridge_submit.add_argument("--task-id", required=True)
    bridge_submit.add_argument("--request-id", required=True)
    bridge_submit.add_argument("--request-kind", choices=sorted(BRIDGE_REQUEST_KINDS), required=True)
    bridge_submit.add_argument("--prompt", default="")
    bridge_submit.add_argument("--ref", action="append", default=[])
    bridge_submit.add_argument("--allowed-path", action="append", default=[])
    bridge_submit.add_argument("--expires-at", default="run_terminal")
    bridge_submit.add_argument(
        "--frontdoor",
        choices=surface_choices,
        default="codex",
    )
    bridge_submit.add_argument("--chat-session-id", default="")
    bridge_submit.add_argument("--idempotency-key", required=True)

    bridge_projection = sub.add_parser("bridge-read-projection")
    bridge_projection.add_argument("--request-id", required=True)
    bridge_projection.add_argument(
        "--frontdoor",
        choices=surface_choices,
        default="codex",
    )
    bridge_projection.add_argument("--chat-session-id", default="")

    bridge_ack = sub.add_parser("bridge-ack-output")
    bridge_ack.add_argument("--request-id", required=True)
    bridge_ack.add_argument("--projection-digest", required=True)
    bridge_ack.add_argument(
        "--frontdoor",
        choices=surface_choices,
        default="codex",
    )
    bridge_ack.add_argument("--chat-session-id", default="")

    child_thread = sub.add_parser("child-thread-create")
    child_thread.add_argument("--plan-json", required=True)
    child_thread.add_argument("--result-json", required=True)
    child_thread.add_argument("--principal-type", default="action_gateway_executor")
    child_thread.add_argument("--principal-id", default="child-thread-gateway")
    child_thread.add_argument("--authn-method", default="local_cli")

    channel = sub.add_parser("channel-token")
    channel.add_argument("--channel", choices=sorted(HTTP_CHANNEL_PRINCIPALS), required=True)
    retention = sub.add_parser("bridge-retention-purge")
    retention.add_argument(
        "--terminal-retention-seconds",
        type=int,
        default=BRIDGE_TERMINAL_RETENTION_SECONDS,
    )
    retention.add_argument(
        "--audit-retention-seconds",
        type=int,
        default=BRIDGE_AUDIT_ROTATION_RETENTION_SECONDS,
    )
    retention.add_argument("--principal-type", default="manual_operator")
    retention.add_argument("--principal-id", default="retention-operator")
    retention.add_argument("--authn-method", default="local_cli")
    permissions = sub.add_parser("state-permission-repair")
    permissions.add_argument(
        "--apply",
        action="store_true",
        help="explicitly repair audited legacy modes; omitted means dry-run",
    )
    permissions.add_argument("--principal-type", default="manual_operator")
    permissions.add_argument("--principal-id", default="state-permission-operator")
    permissions.add_argument("--authn-method", default="local_cli")
    return parser


def main() -> None:
    os.umask(0o077)
    args = parser().parse_args()
    try:
        state_root = trusted_state_root(args.state_root)
        if args.command == "propose":
            classification = load_json_arg(args.classification) if args.classification else None
            payload = proposed_request(
                state_root=state_root,
                task_id=args.task_id,
                request_id=args.request_id,
                user_prompt=args.prompt,
                refs=args.ref,
                classification=classification,
                allowed_paths=args.allowed_path,
                expires_at=args.expires_at,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "approve":
            payload = approve_request(
                state_root=state_root,
                request_id=args.request_id,
                human_action_id=args.human_action_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "orchestrator-start-approve":
            payload = orchestrator_start_approve(
                state_root=state_root,
                request_id=args.request_id,
                human_action_id=args.human_action_id,
                invocation={
                    "skill": "orchestrator-start",
                    "invoked_at": args.invoked_at,
                    "chat_session_id": args.chat_session_id,
                },
            )
        elif args.command == "manual-approve":
            payload = manual_cli_approve(
                state_root=state_root,
                request_id=args.request_id,
                human_action_id=args.human_action_id,
                confirm_nonce=args.confirm,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "create-run":
            payload = create_run(
                state_root=state_root,
                request_id=args.request_id,
                run_id=args.run_id,
                resume_policy=args.resume_policy,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "drain":
            payload = drain_run(
                state_root=state_root,
                run_id=args.run_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "resume":
            payload = resume_run(
                state_root=state_root,
                run_id=args.run_id,
                requeue=args.requeue,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "abort":
            payload = abort_run(
                state_root=state_root,
                run_id=args.run_id,
                reason=args.reason,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "adapter-capability":
            payload = {
                "schema_version": 1,
                "decision": "ok",
                "adapter": claude_headless_capability(),
            }
        elif args.command == "prepare-claude-adapter":
            payload = prepare_claude_adapter(
                state_root=state_root,
                run_id=args.run_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "validate-report":
            payload = validate_report(
                state_root=state_root,
                run_id=args.run_id,
                report_path_arg=args.report_path,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "run-provider":
            payload = run_provider(
                state_root=state_root,
                run_id=args.run_id,
                adapter_id=args.adapter_id,
                timeout_seconds=args.timeout_seconds,
                fake_provider_mode=args.fake_provider_mode,
                live=args.live,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "verify-completion":
            payload = verify_completion(
                state_root=state_root,
                run_id=args.run_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
            if args.format == "markdown" and payload.get("decision") == "complete":
                print(render_vault_evidence_markdown(payload["evidence"]), end="")
                return
        elif args.command == "task-view":
            payload = task_view(
                state_root=state_root,
                task_id=args.task_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "lock-status":
            payload = run_lock.inspect_global_lock(state_root)
        elif args.command == "bridge-submit-request":
            payload = bridge_submit_request(
                state_root=state_root,
                frontend_kind=args.frontdoor,
                payload={
                    "task_id": args.task_id,
                    "request_id": args.request_id,
                    "request_kind": args.request_kind,
                    "prompt": args.prompt,
                    "refs": args.ref,
                    "allowed_paths": args.allowed_path,
                    "expires_at": args.expires_at,
                    "frontdoor": args.frontdoor,
                    "chat_session_id": args.chat_session_id,
                    "idempotency_key": args.idempotency_key,
                },
            )
        elif args.command == "bridge-read-projection":
            payload = bridge_read_projection(
                state_root=state_root,
                request_id=args.request_id,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
            )
        elif args.command == "bridge-ack-output":
            payload = bridge_ack_output(
                state_root=state_root,
                request_id=args.request_id,
                projection_digest=args.projection_digest,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
            )
        elif args.command == "child-thread-create":
            payload = child_thread_create_action(
                state_root=state_root,
                plan=load_json_arg(args.plan_json),
                result=load_json_arg(args.result_json),
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "channel-token":
            ensure_channel_token_file(state_root, args.channel)
            token_path = channel_token_path(state_root, args.channel)
            payload = {
                "schema_version": 1,
                "decision": "ok",
                "channel": args.channel,
                "token_path": str(token_path),
                "token_exposed": False,
            }
        elif args.command == "bridge-retention-purge":
            payload = purge_bridge_retained_artifacts(
                state_root=state_root,
                principal=principal_from_cli(
                    args.principal_type,
                    args.principal_id,
                    args.authn_method,
                ),
                terminal_retention_seconds=args.terminal_retention_seconds,
                audit_retention_seconds=args.audit_retention_seconds,
            )
        elif args.command == "state-permission-repair":
            payload = state_permission_repair(
                state_root=state_root,
                apply=args.apply,
                principal=principal_from_cli(
                    args.principal_type,
                    args.principal_id,
                    args.authn_method,
                ),
            )
        else:
            raise FrontdoorError(f"unsupported command: {args.command}")
    except run_store.RunStoreError as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "errors": exc.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except run_lock.LockContentionError as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "owner": exc.owner,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except run_lifecycle.LifecycleError as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "errors": exc.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except FrontdoorError as exc:
        payload = {"schema_version": 1, "decision": "blocked", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except host_state_root.HostStateRootError as exc:
        payload = {"schema_version": 1, "decision": "blocked", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("decision") in {"blocked", "repair_required"} or payload.get(
        "request_status"
    ) == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
