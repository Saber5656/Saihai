#!/usr/bin/env python3
"""Fail-closed hook guard for secret-bearing file references.

The guard never opens a referenced target. It inspects hook input, records a
session-scoped latch without the attempted path/command, and denies the tool
call. Once latched, every later hook invocation for that session is denied
until a human clears the latch from a terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

SAIHAI_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
if str(SAIHAI_CHECKOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(SAIHAI_CHECKOUT_ROOT))
from directory_paths import load_environment  # noqa: E402

ENV_DIAGNOSTICS = load_environment(checkout_root=SAIHAI_CHECKOUT_ROOT)
from typing import Any, Iterable


DENY_COMPONENT_PATTERNS = (
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    re.compile(r"^directory-path\.env$", re.IGNORECASE),
    re.compile(r"^(?:auth|authentication|authorization)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^(?:token|tokens)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^(?:oauth|pat|password|passwd|passphrase)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^(?:github[._-]?pat|service[._-]?account)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^(?:credential|credentials)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^(?:secret|secrets)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^(?:private[._-]?key|public[._-]?key)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$", re.IGNORECASE),
    re.compile(r"^(?:authorized_keys|known_hosts|ssh_config|sshd_config)$", re.IGNORECASE),
    re.compile(r"^(?:key|keys)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^.*[._-](?:key|keys)(?:[._-].*)?$", re.IGNORECASE),
    re.compile(r"^\.(?:netrc|npmrc|pypirc)$", re.IGNORECASE),
    re.compile(r"^\.git-credentials$", re.IGNORECASE),
)

DENY_PATH_COMPONENTS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
}

DENY_EXTENSIONS = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
}

SENSITIVE_WORD = re.compile(
    r"(?:^|[^a-z0-9])(?:access[_-]?token|api[_-]?key|auth[_-]?token|bearer[_-]?token|"
    r"client[_-]?secret|deploy[_-]?key|github[_-]?token|gh[_-]?token|private[_-]?key|"
    r"github[_-]?pat|service[_-]?account|secret[_-]?key|ssh[_-]?key|password|passphrase)"
    r"(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class GuardError(RuntimeError):
    """Raised when hook input cannot be evaluated safely."""


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def normalize_components(text: str) -> list[str]:
    normalized = text.replace("\\", "/")
    return [
        part.strip("'\"`[]{}(),;:")
        for part in re.split(r"[/\s]+", normalized)
        if part
    ]


def sensitive_reason(text: str) -> str | None:
    if "\x00" in text:
        return "invalid_nul_input"
    if SENSITIVE_WORD.search(text):
        return "sensitive_auth_or_key_name"
    normalized_path = text.replace("\\", "/").lower()
    if re.search(r"(?:^|/)\.config/gcloud(?:/|$)", normalized_path):
        return "protected_auth_store"
    if re.search(r"(?:^|/)\.config/gh/hosts\.yml(?:$|[^a-z0-9])", normalized_path):
        return "protected_auth_store"
    for component in normalize_components(text):
        lower = component.lower()
        if lower in DENY_PATH_COMPONENTS:
            return "protected_auth_directory"
        if Path(lower).suffix in DENY_EXTENSIONS:
            return "protected_key_extension"
        if any(pattern.fullmatch(component) for pattern in DENY_COMPONENT_PATTERNS):
            return "protected_secret_filename"
    return None


PATH_FIELD_NAMES = {"file_path", "path", "notebook_path", "filename", "file"}
READ_COMMAND = re.compile(
    r"(?:^|[/\s;&|])(?:cat|head|tail|less|more|sed|awk|grep|rg|find|xargs|cp|mv|"
    r"install|dd|tar|zip|7z|rsync|scp|sftp|openssl|base64)(?=$|\s)"
)
INDIRECT_READ = re.compile(r"(?:\*|\?|\$[({A-Za-z_`]|`|\bfind\b.*-exec\b|\bxargs\b)")


def iter_path_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in PATH_FIELD_NAMES and isinstance(item, str):
                yield item
            yield from iter_path_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_path_values(item)


def resolved_path_reason(path_text: str, cwd: str) -> str | None:
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        if not candidate.exists() and not candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return "unresolvable_path"
    if candidate.is_symlink():
        reason = sensitive_reason(str(resolved))
        if reason:
            return "symlink_to_protected_path"
    return sensitive_reason(str(resolved))


def command_reason(command: str, cwd: str) -> str | None:
    if READ_COMMAND.search(command) and INDIRECT_READ.search(command):
        return "indirect_filesystem_read"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "unparseable_shell_command"
    for token in tokens:
        reason = resolved_path_reason(token, cwd)
        if reason:
            return reason
    return None


def state_root(runtime: str) -> Path:
    override = os.environ.get("SENSITIVE_ACCESS_GUARD_STATE_ROOT")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    return home / f".{runtime}" / "state" / "sensitive-access-guard"


def prepare_state_root(runtime: str) -> Path:
    root = state_root(runtime)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
    except OSError as exc:
        raise GuardError("state root unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise GuardError("state root must be a real directory")
    if info.st_uid != os.getuid():
        raise GuardError("state root owner mismatch")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise GuardError("state root mode must be 0700")
    return root


@contextlib.contextmanager
def session_lock(runtime: str, session_id: str) -> Iterable[None]:
    root = prepare_state_root(runtime)
    lock_path = root / f"{session_key(session_id)}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise GuardError("session lock unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise GuardError("invalid session lock")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise GuardError("session lock mode must be 0600")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def session_key(session_id: str) -> str:
    if not session_id:
        raise GuardError("missing session_id")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def latch_path(runtime: str, session_id: str) -> Path:
    return state_root(runtime) / f"{session_key(session_id)}.json"


def write_latch(runtime: str, session_id: str, reason_class: str) -> None:
    root = prepare_state_root(runtime)
    target = root / f"{session_key(session_id)}.json"
    payload = {
        "schema_version": 1,
        "runtime": runtime,
        "session_key": session_key(session_id),
        "status": "blocked",
        "reason_class": reason_class,
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".guard-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def deny(runtime: str, reason: str, event_name: str = "PreToolUse") -> int:
    message = (
        "Sensitive access guard stopped this session. A protected env, token, "
        "auth, credential, secret, SSH, or cryptographic-key reference was detected. "
        "A human must clear the session latch from a terminal before work can resume. "
        f"reason_class={reason}"
    )
    if event_name == "UserPromptSubmit":
        output = {"decision": "block", "reason": message, "systemMessage": message}
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            },
            "systemMessage": message,
        }
    sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
    return 0


def evaluate(runtime: str, payload: dict[str, Any]) -> int:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise GuardError("missing or invalid session_id")

    event_name = payload.get("hook_event_name", "PreToolUse")
    if not isinstance(event_name, str):
        raise GuardError("invalid hook_event_name")

    with session_lock(runtime, session_id):
        if latch_path(runtime, session_id).exists():
            return deny(runtime, "session_latched", event_name)

        tool_input = payload.get("tool_input")
        if tool_input is None:
            # UserPromptSubmit and other non-tool events are allowed while unlatched.
            return 0

        cwd = payload.get("cwd", os.getcwd())
        if not isinstance(cwd, str):
            raise GuardError("invalid cwd")
        for path_text in iter_path_values(tool_input):
            reason = resolved_path_reason(path_text, cwd)
            if reason:
                write_latch(runtime, session_id, reason)
                return deny(runtime, reason, event_name)
        if isinstance(tool_input, dict):
            for command_key in ("command", "cmd"):
                command = tool_input.get(command_key)
                if not isinstance(command, str):
                    continue
                reason = command_reason(command, cwd)
                if reason:
                    write_latch(runtime, session_id, reason)
                    return deny(runtime, reason, event_name)
        for text in iter_strings(tool_input):
            reason = sensitive_reason(text)
            if reason:
                write_latch(runtime, session_id, reason)
                return deny(runtime, reason, event_name)
        return 0


def clear_latch(runtime: str, session_id: str) -> int:
    with session_lock(runtime, session_id):
        target = latch_path(runtime, session_id)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=("codex", "claude"), required=True)
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--session-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.clear:
        if not args.session_id:
            raise GuardError("--clear requires --session-id")
        return clear_latch(args.runtime, args.session_id)

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise GuardError("hook input must be a JSON object")
        return evaluate(args.runtime, payload)
    except Exception as exc:  # fail closed on parser, state, or policy failure
        session_id = ""
        try:
            session_id = payload.get("session_id", "") if isinstance(payload, dict) else ""
        except UnboundLocalError:
            pass
        if session_id:
            try:
                write_latch(args.runtime, session_id, "guard_internal_error")
            except Exception:
                pass
        sys.stderr.write(f"Sensitive access guard failed closed: {type(exc).__name__}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
