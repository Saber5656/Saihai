#!/usr/bin/env python3
"""Durable, atomic, schema-validated store for workflow-run records."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RESERVED_ARTIFACT_SUFFIX_RE = re.compile(r"(?:\.error|\.corrupt-\d+)$")

RUN_STATES = {
    "created",
    "step_queued",
    "waiting_provider",
    "validating",
    "remediating",
    "waiting_human",
    "complete",
    "failed",
    "aborted",
}
GOAL_STATES = {"approved", "active", "blocked", "complete", "aborted"}
TERMINAL_RUN_STATES = {"complete", "failed", "aborted"}
APPROVED_ACTIVATION_SOURCES = {"orchestrator-start", "human_ui", "manual_cli"}

STORE_REASON_CLASSES = {
    "run_not_found",
    "corrupt_json",
    "schema_invalid",
    "state_conflict",
    "io_error",
}


class RunStoreError(RuntimeError):
    """Typed store failure. reason_class is one of STORE_REASON_CLASSES."""

    def __init__(self, reason_class: str, errors: list[str] | None = None) -> None:
        super().__init__(reason_class)
        self.reason_class = reason_class
        self.errors = errors or []


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def validate_artifact_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise RunStoreError(
            "schema_invalid",
            [f"{label} must match {SAFE_ID_RE.pattern} and cannot contain path separators"],
        )
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value.split("."):
        raise RunStoreError("schema_invalid", [f"{label} cannot contain path traversal segments"])
    if RESERVED_ARTIFACT_SUFFIX_RE.search(value):
        raise RunStoreError("schema_invalid", [f"{label} cannot use reserved run-store artifact suffixes"])
    return value


def _ensure_private_directory(path: Path) -> None:
    """Create a no-symlink directory chain with private new components."""

    absolute = path.expanduser()
    if not absolute.is_absolute():
        absolute = absolute.absolute()
    resolved = absolute.resolve(strict=False)
    if resolved != absolute:
        macos_var_alias = (
            str(absolute).startswith("/var/")
            and str(resolved) == "/private" + str(absolute)
        )
        if not macos_var_alias:
            raise RunStoreError("io_error", ["private directory symlink redirection forbidden"])
        absolute = resolved
    components: list[Path] = []
    current = Path(absolute.anchor)
    components.append(current)
    for part in absolute.parts[1:]:
        current = current / part
        components.append(current)

    private_subtree = False
    old_umask = os.umask(0o077)
    try:
        for component in components:
            created = False
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                try:
                    component.mkdir(mode=0o700)
                    created = True
                    metadata = component.lstat()
                except OSError as exc:
                    raise RunStoreError("io_error", [str(exc)]) from exc
            except OSError as exc:
                raise RunStoreError("io_error", [str(exc)]) from exc
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise RunStoreError("io_error", ["private directory chain contains non-directory"])
            mode = stat.S_IMODE(metadata.st_mode)
            if created or (metadata.st_uid == os.getuid() and mode == 0o700):
                private_subtree = True
            if private_subtree:
                if metadata.st_uid != os.getuid() or mode != 0o700:
                    raise RunStoreError(
                        "io_error",
                        [f"owned state directory chain must be exact mode 0700:{component.name}"],
                    )
            elif metadata.st_mode & 0o022 and not (
                metadata.st_uid == 0 and mode & stat.S_ISVTX
            ):
                raise RunStoreError(
                    "io_error",
                    ["private directory ancestor is writable by another principal"],
                )
    finally:
        os.umask(old_umask)

    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RunStoreError("io_error", ["artifact parent must be owned mode 0700"])


def ensure_private_directory(path: Path) -> None:
    old_umask = os.umask(0o077)
    try:
        _ensure_private_directory(path)
    finally:
        os.umask(old_umask)


def _open_parent(path: Path) -> tuple[int, str]:
    if not path.name or path.name in {".", ".."} or os.path.basename(path.name) != path.name:
        raise RunStoreError("io_error", ["artifact filename is unsafe"])
    _ensure_private_directory(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.parent, flags)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise RunStoreError("io_error", ["artifact parent descriptor is unsafe"])
    return descriptor, path.name


def _validate_open_artifact(descriptor: int, parent_descriptor: int) -> None:
    artifact = os.fstat(descriptor)
    parent = os.fstat(parent_descriptor)
    if (
        not stat.S_ISREG(artifact.st_mode)
        or artifact.st_uid != parent.st_uid
        or stat.S_IMODE(artifact.st_mode) != 0o600
    ):
        raise RunStoreError("io_error", ["artifact ownership or mode is unsafe"])


def read_bytes(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Read a private regular artifact through no-follow descriptors."""

    parent_fd, name = _open_parent(path)
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        _validate_open_artifact(descriptor, parent_fd)
        metadata = os.fstat(descriptor)
        if metadata.st_size > max_bytes:
            raise RunStoreError("io_error", ["artifact exceeds read boundary"])
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise RunStoreError("io_error", ["artifact exceeds read boundary"])
        return b"".join(chunks)
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def read_json(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> Any:
    try:
        return json.loads(read_bytes(path, max_bytes=max_bytes).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RunStoreError("corrupt_json", [str(exc)]) from exc


def atomic_write_json(path: Path, payload: Any) -> None:
    tmp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    parent_fd = -1
    descriptor = -1
    old_umask = os.umask(0o077)
    try:
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        parent_fd, name = _open_parent(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(tmp_name, flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except (TypeError, ValueError) as exc:
        raise RunStoreError("schema_invalid", [f"payload must be JSON serializable: {exc}"]) from exc
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.close(parent_fd)
        os.umask(old_umask)


def append_json_line(path: Path, payload: Any, *, max_file_bytes: int = 16 * 1024 * 1024) -> None:
    """Append one fsynced JSON line to a private no-follow regular file."""

    try:
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RunStoreError("schema_invalid", [f"payload must be JSON serializable: {exc}"]) from exc
    parent_fd = -1
    descriptor = -1
    old_umask = os.umask(0o077)
    try:
        parent_fd, name = _open_parent(path)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        _validate_open_artifact(descriptor, parent_fd)
        if os.fstat(descriptor).st_size + len(encoded) > max_file_bytes:
            raise RunStoreError("io_error", ["append artifact exceeds size boundary"])
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.fsync(parent_fd)
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.umask(old_umask)


def read_and_unlink_private_file(
    path: Path,
    *,
    max_bytes: int = 64 * 1024 * 1024,
) -> bytes:
    """Read and unlink one exact private file through a stable parent fd."""

    parent_fd, name = _open_parent(path)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        _validate_open_artifact(descriptor, parent_fd)
        before = os.fstat(descriptor)
        if before.st_nlink != 1 or before.st_size > max_bytes:
            raise RunStoreError("io_error", ["unlink artifact identity is unsafe"])
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise RunStoreError("io_error", ["unlink artifact exceeds boundary"])
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        ):
            raise RunStoreError("io_error", ["unlink artifact changed during read"])
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return b"".join(chunks)
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _is_int_at_least(value: Any, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def validate_run_record(run: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(run, dict):
        return ["run must be a json object"]

    required = [
        "run_version",
        "run_id",
        "task_id",
        "request_id",
        "workflow_id",
        "goal_state",
        "run_state",
        "current_step",
        "iteration",
        "max_steps",
        "step_history",
        "activation",
        "terminal",
        "requester",
        "scheduling",
        "context_sharing",
    ]
    missing = [field for field in required if field not in run]
    if missing:
        return [f"missing_required_field:{field}" for field in missing]

    if run.get("run_version") != "1":
        errors.append("run_version must be '1'")
    for field in ("run_id", "task_id", "request_id"):
        try:
            validate_artifact_id(run.get(field), field)
        except RunStoreError as exc:
            errors.extend(exc.errors)
    if not _non_empty_string(run.get("workflow_id")):
        errors.append("workflow_id must be a non-empty string")
    if not _non_empty_string(run.get("current_step")):
        errors.append("current_step must be a non-empty string")
    if run.get("run_state") not in RUN_STATES:
        errors.append("run_state must be a known workflow run state")
    if run.get("goal_state") not in GOAL_STATES:
        errors.append("goal_state must be a known workflow goal state")
    if not _is_int_at_least(run.get("iteration"), 1):
        errors.append("iteration must be an integer >= 1")
    if not _is_int_at_least(run.get("max_steps"), 1):
        errors.append("max_steps must be an integer >= 1")
    if not isinstance(run.get("step_history"), list):
        errors.append("step_history must be a list")
    if "transitions" in run and not isinstance(run.get("transitions"), list):
        errors.append("transitions must be a list")
    provider_execution = run.get("provider_execution")
    if provider_execution is not None:
        if not isinstance(provider_execution, dict):
            errors.append("provider_execution must be a json object")
        else:
            required_execution = {
                "execution_version",
                "step_id",
                "adapter_id",
                "work_order_digest",
                "adapter_request_digest",
                "context_snapshot_digest",
                "phase",
                "attempt_number",
                "attempt_id",
                "timeout_seconds",
                "lease",
                "retry",
                "last_outcome",
            }
            errors.extend(
                f"provider_execution.{field} is required"
                for field in sorted(required_execution - set(provider_execution))
            )
            if provider_execution.get("execution_version") != "1":
                errors.append("provider_execution.execution_version must be '1'")
            if provider_execution.get("phase") not in {
                "claimed", "invoking", "result_ready", "retry_scheduled", "human_gate", "completed", "abandoned"
            }:
                errors.append("provider_execution.phase must be known")
            if not _is_int_at_least(provider_execution.get("attempt_number"), 1):
                errors.append("provider_execution.attempt_number must be >= 1")
            timeout = provider_execution.get("timeout_seconds")
            if not _is_int_at_least(timeout, 1) or timeout > 86400:
                errors.append("provider_execution.timeout_seconds must be between 1 and 86400")
            for field in ("work_order_digest", "adapter_request_digest", "context_snapshot_digest"):
                value = provider_execution.get(field)
                if not isinstance(value, str) or not value.startswith("sha256:"):
                    errors.append(f"provider_execution.{field} must start with sha256:")
            lease = provider_execution.get("lease")
            if not isinstance(lease, dict):
                errors.append("provider_execution.lease must be a json object")
            else:
                for field in ("lease_id", "claimed_by", "claimed_at", "last_heartbeat_at", "lease_expires_at"):
                    if field not in lease:
                        errors.append(f"provider_execution.lease.{field} is required")
            retry = provider_execution.get("retry")
            if not isinstance(retry, dict):
                errors.append("provider_execution.retry must be a json object")
            else:
                for field in ("consecutive_failures", "auto_retries_used", "max_auto_retries"):
                    if not _is_int_at_least(retry.get(field), 0):
                        errors.append(f"provider_execution.retry.{field} must be >= 0")
    completion_verification = run.get("completion_verification")
    if completion_verification is not None:
        if not isinstance(completion_verification, dict):
            errors.append("completion_verification must be a json object")
        else:
            required_completion_fields = {
                "verified_at",
                "decision",
                "report_sha256",
                "evidence_sha256",
                "verifier",
            }
            missing_completion_fields = sorted(required_completion_fields - set(completion_verification))
            errors.extend(
                f"completion_verification.{field} is required"
                for field in missing_completion_fields
            )
            if completion_verification.get("decision") != "complete":
                errors.append("completion_verification.decision must be complete")
            for field in ("report_sha256", "evidence_sha256"):
                value = completion_verification.get(field)
                if not isinstance(value, str) or not value.startswith("sha256:"):
                    errors.append(f"completion_verification.{field} must start with sha256:")
            if not isinstance(completion_verification.get("verifier"), dict):
                errors.append("completion_verification.verifier must be a json object")
    if not isinstance(run.get("requester"), dict):
        errors.append("requester must be a json object")

    activation = run.get("activation")
    if not isinstance(activation, dict):
        errors.append("activation must be a json object")
    else:
        if activation.get("activation_status") != "approved":
            errors.append("activation.activation_status must be approved")
        if activation.get("activation_source") not in APPROVED_ACTIVATION_SOURCES:
            errors.append("activation.activation_source must be approved source")
        if activation.get("next_action") != "create_workflow_run":
            errors.append("activation.next_action must be create_workflow_run")
        workflow_selection = activation.get("workflow_selection")
        if not isinstance(workflow_selection, dict):
            errors.append("activation.workflow_selection must be a json object")
        else:
            if workflow_selection.get("status") != "selected":
                errors.append("activation.workflow_selection.status must be selected")
            if not _non_empty_string(workflow_selection.get("workflow_id")):
                errors.append("activation.workflow_selection.workflow_id must be non-empty")
            if not _non_empty_string(workflow_selection.get("initial_step")):
                errors.append("activation.workflow_selection.initial_step must be non-empty")
        context_scope = activation.get("context_scope")
        if not isinstance(context_scope, dict):
            errors.append("activation.context_scope must be a json object")
        else:
            refs = context_scope.get("refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(item, str) or not item for item in refs):
                errors.append("activation.context_scope.refs must be a non-empty list of strings")
            if context_scope.get("raw_transcript_sharing") != "forbidden":
                errors.append("activation.context_scope.raw_transcript_sharing must be forbidden")
        activation_scope = activation.get("activation_scope")
        if not isinstance(activation_scope, dict):
            errors.append("activation.activation_scope must be a json object")
        else:
            allowed_ops = activation_scope.get("allowed_ops")
            if not isinstance(allowed_ops, dict):
                errors.append("activation.activation_scope.allowed_ops must be a json object")
            else:
                for op in ("edit", "commit", "push", "network"):
                    if not isinstance(allowed_ops.get(op), bool):
                        errors.append(f"activation.activation_scope.allowed_ops.{op} must be boolean")
            if not _is_int_at_least(activation_scope.get("step_budget"), 1):
                errors.append("activation.activation_scope.step_budget must be an integer >= 1")

    scheduling = run.get("scheduling")
    if not isinstance(scheduling, dict):
        errors.append("scheduling must be a json object")
    else:
        fixed_values = {
            "scheduler_mode": "invocation-drain",
            "concurrency_group": "global",
            "state_persistence": "durable_state",
            "lock_policy": "global_advisory_lock",
            "concurrency": 1,
        }
        for field, expected in fixed_values.items():
            if scheduling.get(field) != expected:
                errors.append(f"scheduling.{field} must be {expected!r}")
        if scheduling.get("resume_policy") not in {"manual", "daemon_future"}:
            errors.append("scheduling.resume_policy must be manual or daemon_future")

    context_sharing = run.get("context_sharing")
    if not isinstance(context_sharing, dict):
        errors.append("context_sharing must be a json object")
    else:
        fixed_values = {
            "shared_run_state": "typed_durable_state",
            "step_local_snapshot": "immutable_step_attempt_snapshot",
            "provider_transcript": "confined_evidence_path_only",
        }
        for field, expected in fixed_values.items():
            if context_sharing.get(field) != expected:
                errors.append(f"context_sharing.{field} must be {expected!r}")

    terminal = run.get("terminal")
    if not isinstance(terminal, dict):
        errors.append("terminal must be a json object")
    else:
        for field in ("status", "reason"):
            if field not in terminal:
                errors.append(f"terminal.{field} is required")
        if run.get("run_state") in TERMINAL_RUN_STATES and not _non_empty_string(terminal.get("status")):
            errors.append("terminal_status_required_for_terminal_state")

    return errors


def run_path(state_root: Path, run_id: str) -> Path:
    return state_root / "runs" / f"{validate_artifact_id(run_id, 'run_id')}.json"


def error_artifact_path(state_root: Path, run_id: str) -> Path:
    return state_root / "runs" / f"{validate_artifact_id(run_id, 'run_id')}.error.json"


def write_error_artifact(
    state_root: Path,
    run_id: str,
    *,
    reason_class: str,
    errors: list[str],
    operation: str,
) -> Path:
    if reason_class not in STORE_REASON_CLASSES:
        raise ValueError(f"unsupported store reason_class: {reason_class}")
    payload = {
        "error_version": "1",
        "run_id": run_id,
        "operation": operation,
        "reason_class": reason_class,
        "errors": errors,
        "occurred_at": now_iso(),
    }
    path = error_artifact_path(state_root, run_id)
    atomic_write_json(path, payload)
    return path


def quarantine_corrupt_run(state_root: Path, run_id: str) -> Path:
    source = run_path(state_root, run_id)
    index = 1
    while True:
        target = state_root / "runs" / f"{validate_artifact_id(run_id, 'run_id')}.corrupt-{index}.json"
        if not target.exists():
            break
        index += 1
    try:
        ensure_private_directory(target.parent)
        os.replace(source, target)
    except OSError as exc:
        raise RunStoreError("io_error", [str(exc)]) from exc
    return target


def load_run(state_root: Path, run_id: str) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    path = run_path(state_root, run_id)
    if not path.exists():
        raise RunStoreError("run_not_found")
    try:
        run = read_json(path)
    except RunStoreError as exc:
        if exc.reason_class != "corrupt_json":
            raise
        errors = list(exc.errors)
        quarantine_corrupt_run(state_root, run_id)
        write_error_artifact(state_root, run_id, reason_class="corrupt_json", errors=errors, operation="load")
        raise RunStoreError("corrupt_json", errors) from exc

    errors = validate_run_record(run)
    if isinstance(run, dict) and run.get("run_id") != run_id:
        errors.append(f"run_id must match requested run_id {run_id!r}")
    if errors:
        write_error_artifact(state_root, run_id, reason_class="schema_invalid", errors=errors, operation="load")
        raise RunStoreError("schema_invalid", errors)
    return run


def _valid_error_artifact_run_id(run: Any) -> str | None:
    if not isinstance(run, dict):
        return None
    run_id = run.get("run_id")
    try:
        return validate_artifact_id(run_id, "run_id")
    except RunStoreError:
        return None


def store_run(
    state_root: Path,
    run: dict[str, Any],
    *,
    expected_current_state: str | None = None,
) -> Path:
    errors = validate_run_record(run)
    run_id = _valid_error_artifact_run_id(run)
    if errors:
        if run_id is not None:
            write_error_artifact(
                state_root,
                run_id,
                reason_class="schema_invalid",
                errors=errors,
                operation="store",
            )
        raise RunStoreError("schema_invalid", errors)
    assert run_id is not None

    path = run_path(state_root, run_id)
    if expected_current_state is not None:
        if not path.exists():
            conflict_errors = [f"expected existing run_state {expected_current_state!r}, found missing run"]
            write_error_artifact(
                state_root,
                run_id,
                reason_class="state_conflict",
                errors=conflict_errors,
                operation="store",
            )
            raise RunStoreError("state_conflict", conflict_errors)
        on_disk = load_run(state_root, run_id)
        if on_disk.get("run_state") != expected_current_state:
            conflict_errors = [
                f"expected run_state {expected_current_state!r}, found {on_disk.get('run_state')!r}"
            ]
            write_error_artifact(
                state_root,
                run_id,
                reason_class="state_conflict",
                errors=conflict_errors,
                operation="store",
            )
            raise RunStoreError("state_conflict", conflict_errors)

    atomic_write_json(path, run)
    return path
