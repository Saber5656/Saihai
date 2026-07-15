#!/usr/bin/env python3
"""Global advisory lock and P0 concurrency guard for workflow runs."""

from __future__ import annotations

import json
import hashlib
import os
import socket
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import run_store

LOCK_DIRNAME = "locks"
GLOBAL_LOCK_NAME = "global-advisory.lock.d"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_STALE_AFTER_SECONDS = 300.0
INFLIGHT_RUN_STATES = {"step_queued", "waiting_provider", "validating"}
_PROCESS_GLOBAL_LOCK = threading.RLock()


class LockContentionError(RuntimeError):
    """Raised when the global lock or P0 concurrency guard blocks progress."""

    def __init__(self, reason_class: str = "lock_contention", owner: dict[str, Any] | None = None) -> None:
        super().__init__(reason_class)
        self.reason_class = reason_class
        self.owner = owner or {}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def global_lock_path(state_root: Path) -> Path:
    return state_root / LOCK_DIRNAME / GLOBAL_LOCK_NAME


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def current_hostname() -> str:
    return socket.gethostname()


def process_start_token(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    observed = completed.stdout.strip()
    if not observed:
        return ""
    return "proc-" + hashlib.sha256(observed).hexdigest()


def read_lock_owner(lock_path: Path) -> dict[str, Any]:
    try:
        payload = run_store.read_json(lock_path / "owner.json", max_bytes=64 * 1024)
    except (OSError, ValueError, run_store.RunStoreError):
        return {}
    return payload if isinstance(payload, dict) else {}


def lock_snapshot(lock_path: Path) -> dict[str, Any] | None:
    try:
        stat = lock_path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "mtime_ns": stat.st_mtime_ns,
        "owner": read_lock_owner(lock_path),
    }


def stale_lock_reason(lock_path: Path, *, stale_after_seconds: float) -> str:
    try:
        stat = lock_path.stat()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"lock_stat_unreadable:{exc}"

    age = max(0.0, time.time() - stat.st_mtime)
    if age <= stale_after_seconds:
        return ""

    owner = read_lock_owner(lock_path)
    if not owner:
        return f"lock_owner_missing_or_unreadable:age_seconds={age:.3f}"

    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return f"lock_owner_pid_invalid:age_seconds={age:.3f}"
    if process_is_alive(pid):
        owner_hostname = str(owner.get("hostname") or "")
        if owner_hostname and owner_hostname != current_hostname():
            return f"lock_owner_hostname_mismatch:{owner_hostname}:age_seconds={age:.3f}"
        owner_start_token = str(owner.get("process_start_token") or "")
        current_start_token = process_start_token(pid)
        if owner_start_token and current_start_token:
            if owner_start_token != current_start_token:
                return f"lock_owner_pid_reused:{pid}:age_seconds={age:.3f}"
            return ""
        if not owner_start_token:
            return f"lock_owner_process_start_token_missing:{pid}:age_seconds={age:.3f}"
        return ""
    return f"lock_owner_pid_not_alive:{pid}:age_seconds={age:.3f}"


def try_reclaim_stale_lock(lock_path: Path, *, stale_after_seconds: float) -> bool:
    reclaim_lock = lock_path.with_name(f"{lock_path.name}.reclaim.lock.d")
    try:
        run_store.ensure_private_directory(reclaim_lock.parent)
        reclaim_lock.mkdir(mode=0o700)
    except (OSError, run_store.RunStoreError):
        return False
    try:
        before = lock_snapshot(lock_path)
        if before is None:
            return False
        if not stale_lock_reason(lock_path, stale_after_seconds=stale_after_seconds):
            return False
        if lock_snapshot(lock_path) != before:
            return False
        if not stale_lock_reason(lock_path, stale_after_seconds=stale_after_seconds):
            return False
        if lock_snapshot(lock_path) != before:
            return False
        reclaiming = lock_path.with_name(f"{lock_path.name}.reclaiming.{os.getpid()}")
        try:
            lock_path.rename(reclaiming)
            shutil.rmtree(reclaiming)
        except OSError:
            return False
        return True
    finally:
        try:
            reclaim_lock.rmdir()
        except OSError:
            pass


def acquire_global_lock(
    state_root: Path,
    *,
    operation: str,
    run_id: str = "",
    principal: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    lock_path = global_lock_path(state_root)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    principal = principal or {}
    owner = {
        "lock_version": "1",
        "lock_type": "workflow-run-global",
        "pid": os.getpid(),
        "hostname": current_hostname(),
        "process_start_token": process_start_token(os.getpid()),
        "owner_nonce": uuid.uuid4().hex,
        "created_at": now_iso(),
        "stale_after_seconds": float(stale_after_seconds),
        "operation": operation,
        "run_id": run_id,
        "principal_type": str(principal.get("principal_type") or "unknown"),
    }

    while True:
        try:
            run_store.ensure_private_directory(lock_path.parent)
            lock_path.mkdir(mode=0o700)
        except FileExistsError:
            if try_reclaim_stale_lock(lock_path, stale_after_seconds=stale_after_seconds):
                continue
            if time.monotonic() >= deadline:
                raise LockContentionError("lock_contention", read_lock_owner(lock_path))
            time.sleep(0.05)
            continue
        except OSError as exc:
            raise LockContentionError("lock_contention", {"error": str(exc)}) from exc

        try:
            run_store.atomic_write_json(lock_path / "owner.json", owner)
        except Exception:
            release_global_lock(state_root)
            raise
        return owner


def release_global_lock(state_root: Path) -> None:
    lock_path = global_lock_path(state_root)
    try:
        (lock_path / "owner.json").unlink()
    except FileNotFoundError:
        pass
    try:
        lock_path.rmdir()
    except FileNotFoundError:
        return


@contextmanager
def hold_global_lock(
    state_root: Path,
    *,
    operation: str,
    run_id: str = "",
    principal: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    stale_after_seconds: float | None = None,
) -> Iterator[dict[str, Any]]:
    # Threads in one host process must queue before starting the cross-process
    # timeout.  Otherwise a burst of valid idempotent requests can all spend
    # their timeout waiting behind work owned by the same PID.
    with _PROCESS_GLOBAL_LOCK:
        owner = acquire_global_lock(
            state_root,
            operation=operation,
            run_id=run_id,
            principal=principal,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS if stale_after_seconds is None else stale_after_seconds,
        )
        try:
            yield owner
        finally:
            release_global_lock(state_root)


def inspect_global_lock(
    state_root: Path,
    *,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    lock_path = global_lock_path(state_root)
    locked = lock_path.exists()
    owner = read_lock_owner(lock_path) if locked else {}
    reason = stale_lock_reason(lock_path, stale_after_seconds=stale_after_seconds) if locked else ""
    return {
        "schema_version": 1,
        "decision": "ok",
        "locked": locked,
        "owner": owner or None,
        "stale": bool(reason),
        "stale_reason": reason,
        "lock_path": str(lock_path),
    }


def count_inflight_runs(state_root: Path, *, exclude_run_id: str = "") -> list[str]:
    runs_dir = state_root / "runs"
    if not runs_dir.exists():
        return []
    inflight: list[str] = []
    for path in runs_dir.glob("*.json"):
        name = path.name
        if name.startswith(".") or name.endswith(".error.json") or ".corrupt-" in name or name.endswith(".tmp"):
            continue
        try:
            run = run_store.read_json(path)
        except (OSError, ValueError, run_store.RunStoreError):
            continue
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("run_id") or path.stem)
        if run_id == exclude_run_id:
            continue
        if run.get("run_state") in INFLIGHT_RUN_STATES:
            inflight.append(run_id)
    return sorted(inflight)


def assert_p0_concurrency(state_root: Path, *, target_run_id: str) -> None:
    inflight = count_inflight_runs(state_root, exclude_run_id=target_run_id)
    if inflight:
        raise LockContentionError(
            "concurrency_limit_reached",
            {"inflight_run_ids": inflight},
        )
