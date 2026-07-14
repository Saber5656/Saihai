#!/usr/bin/env python3
"""Fail-closed live provider adapters with offline-testable parsers."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TIMEOUT_SECONDS = 1_800
MAX_TIMEOUT_SECONDS = 86_400
MAX_CONTEXT_BYTES = 1_048_576
MAX_INSTRUCTION_BYTES = 65_536
MAX_OUTPUT_BYTES = 4_194_304
ERROR_SCAN_BYTES = 65_536
HEARTBEAT_INTERVAL_SECONDS = 30
AUTH_OR_QUOTA_MARKERS = (
    "not logged in",
    "unauthorized",
    "authentication_error",
    "invalid api key",
    "invalid_api_key",
    "credit balance",
    "insufficient_quota",
    "billing limit",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "401",
    "402",
    "429",
)
_popen = subprocess.Popen


class AdapterConfigurationError(ValueError):
    """A fail-closed adapter configuration or request error."""


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    fence_start = stripped.find("```")
    if fence_start >= 0:
        content_start = stripped.find("\n", fence_start)
        fence_end = stripped.find("```", content_start + 1) if content_start >= 0 else -1
        if content_start >= 0 and fence_end >= 0:
            candidates.append(stripped[content_start + 1 : fence_end].strip())
    object_start = stripped.find("{")
    object_end = stripped.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(stripped[object_start : object_end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _verified_context(request: dict[str, Any]) -> str:
    snapshot = request.get("context_snapshot")
    if not isinstance(snapshot, dict):
        raise AdapterConfigurationError("context_snapshot_missing")
    content = snapshot.get("content")
    digest = snapshot.get("sha256")
    byte_length = snapshot.get("byte_length")
    if not isinstance(content, str) or not isinstance(digest, str):
        raise AdapterConfigurationError("context_snapshot_invalid")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise AdapterConfigurationError("context_snapshot_too_large")
    if byte_length != len(encoded):
        raise AdapterConfigurationError("context_snapshot_length_mismatch")
    if hashlib.sha256(encoded).hexdigest() != digest.lower():
        raise AdapterConfigurationError("context_snapshot_digest_mismatch")
    return content


def bounded_prompt(request: dict[str, Any]) -> str:
    content = _verified_context(request)
    instruction = request.get("instruction") or ""
    if not isinstance(instruction, str) or len(instruction.encode("utf-8")) > MAX_INSTRUCTION_BYTES:
        raise AdapterConfigurationError("instruction_too_large")
    prompt = "\n".join(
        [
            "You are a tool-disabled reviewer for one approved readonly work order.",
            "Do not call tools, read additional files, edit files, execute commands, or request broader context.",
            "Use only the digest-verified context snapshot embedded below.",
            "Return only one External Review Report JSON object matching",
            "organization/runtime/workflows/schemas/external-review-report.schema.json.",
            "Do not wrap the JSON in prose or markdown fences.",
            f"request_id: {request['request_id']}",
            f"run_id: {request['run_id']}",
            f"workflow_id: {request['workflow_id']}",
            f"step_id: {request['step_id']}",
            "provider_evidence.evidence_path: runner-bound",
            "provider_evidence.transcript_path: runner-bound",
            "Instruction:",
            instruction,
            "BEGIN APPROVED CONTEXT SNAPSHOT",
            content,
            "END APPROVED CONTEXT SNAPSHOT",
        ]
    )
    if len(prompt.encode("utf-8")) > MAX_CONTEXT_BYTES + MAX_INSTRUCTION_BYTES + 16_384:
        raise AdapterConfigurationError("prompt_too_large")
    return prompt


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "input", "prompt_tokens"),
        "output_tokens": ("output_tokens", "output", "completion_tokens"),
    }
    result: dict[str, int] = {}
    for canonical, keys in aliases.items():
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                result[canonical] = candidate
                break
    return result


def _structured_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _structured_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _structured_strings(child)]
    return [str(value)] if isinstance(value, (str, int)) else []


def _failure_status(stdout: bytes, stderr: bytes) -> tuple[str, str]:
    fragments: list[str] = []
    for raw in (stdout[:ERROR_SCAN_BYTES], stderr[:ERROR_SCAN_BYTES]):
        decoded = _text(raw)
        fragments.append(decoded)
        for line in decoded.splitlines():
            try:
                fragments.extend(_structured_strings(json.loads(line)))
            except json.JSONDecodeError:
                continue
    lowered = "\n".join(fragments).lower()
    if any(marker in lowered for marker in AUTH_OR_QUOTA_MARKERS):
        return "unavailable", "auth_or_quota"
    return "nonzero_exit", "nonzero_exit"


def _structured_provider_unavailable(stdout: bytes, stderr: bytes) -> bool:
    for raw in (stdout[:ERROR_SCAN_BYTES], stderr[:ERROR_SCAN_BYTES]):
        for line in _text(raw).splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = str(payload.get("type") or "").lower()
            if "error" not in payload and event_type not in {"error", "turn.failed", "request.failed"}:
                continue
            lowered = "\n".join(_structured_strings(payload)).lower()
            if any(marker in lowered for marker in AUTH_OR_QUOTA_MARKERS):
                return True
    return False


def extract_claude_result(stdout_text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return None, {}
    if not isinstance(payload, dict):
        return None, {}
    report = extract_json_object(str(payload.get("result") or ""))
    model_usage = payload.get("modelUsage")
    effective_model = str(payload.get("model") or "")
    if not effective_model and isinstance(model_usage, dict) and model_usage:
        effective_model = str(next(iter(model_usage)))
    evidence = {
        "provider": "anthropic",
        "effective_model": effective_model,
        "provider_session_id": str(payload.get("session_id") or ""),
        "provider_request_id": str(payload.get("uuid") or payload.get("request_id") or ""),
        "usage": _usage(payload.get("usage")),
    }
    return report, evidence


def _event_text(event: dict[str, Any]) -> str:
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
        if any(texts):
            return "\n".join(text for text in texts if text)
    for candidate in (item.get("text"), event.get("text"), event.get("message")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def extract_codex_result(stdout_text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events:
        return None, {}
    thread_id = request_id = model = final_text = ""
    usage: dict[str, int] = {}
    for event in events:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        thread_id = str(event.get("thread_id") or event.get("session_id") or thread_id)
        request_id = str(event.get("request_id") or event.get("turn_id") or item.get("id") or request_id)
        model = str(event.get("model") or item.get("model") or model)
        candidate_usage = _usage(event.get("usage") or event.get("token_usage"))
        if candidate_usage:
            usage = candidate_usage
        event_type = str(event.get("type") or "")
        item_type = str(item.get("type") or "")
        if event_type == "agent_message" or (
            event_type == "item.completed" and item_type in {"agent_message", "message"}
        ):
            final_text = _event_text(event) or final_text
    return extract_json_object(final_text), {
        "provider": "openai",
        "effective_model": model,
        "provider_session_id": thread_id,
        "provider_request_id": request_id,
        "usage": usage,
    }


def _secure_binding_from_env(path_name: str, digest_name: str) -> dict[str, Any]:
    raw_path = os.environ.get(path_name, "")
    expected = os.environ.get(digest_name, "").lower()
    path = Path(raw_path)
    if not raw_path or not path.is_absolute() or not expected:
        raise AdapterConfigurationError("host_binding_missing")
    try:
        if path.resolve(strict=True) != path:
            raise AdapterConfigurationError("host_binding_not_regular")
    except OSError as exc:
        raise AdapterConfigurationError("host_binding_unavailable") from exc
    digest = hashlib.sha256()
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AdapterConfigurationError("host_binding_not_regular")
        if metadata.st_uid not in {0, os.getuid()}:
            raise AdapterConfigurationError("host_binding_bad_owner")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise AdapterConfigurationError("host_binding_insecure_mode")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(131_072), b""):
                digest.update(chunk)
    except AdapterConfigurationError:
        raise
    except OSError as exc:
        reason = "host_binding_not_regular" if path.is_symlink() else "host_binding_unavailable"
        raise AdapterConfigurationError(reason) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    actual_digest = digest.hexdigest()
    if actual_digest != expected:
        raise AdapterConfigurationError("host_binding_digest_mismatch")
    return {
        "path": str(path),
        "sha256": "sha256:" + actual_digest,
        "uid": metadata.st_uid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _secure_file_from_env(path_name: str, digest_name: str) -> Path:
    return Path(_secure_binding_from_env(path_name, digest_name)["path"])


def resolve_execution_binding(adapter_id: str) -> dict[str, Any]:
    if adapter_id == "claude_headless_p0":
        return {
            "binding_version": "1",
            "binary": _secure_binding_from_env(
                "SAIHAI_CLAUDE_EXECUTABLE_PATH", "SAIHAI_CLAUDE_EXECUTABLE_SHA256"
            ),
            "confinement": "claude_tools_disabled",
        }
    if adapter_id == "codex_cli_openai_p0":
        return {
            "binding_version": "1",
            "binary": _secure_binding_from_env(
                "SAIHAI_CODEX_EXECUTABLE_PATH", "SAIHAI_CODEX_EXECUTABLE_SHA256"
            ),
            "wrapper": _secure_binding_from_env(
                "SAIHAI_CODEX_CONFINEMENT_WRAPPER_PATH", "SAIHAI_CODEX_CONFINEMENT_WRAPPER_SHA256"
            ),
            "profile": _secure_binding_from_env(
                "SAIHAI_CODEX_CONFINEMENT_PROFILE_PATH", "SAIHAI_CODEX_CONFINEMENT_PROFILE_SHA256"
            ),
            "confinement": "host_pinned_external_profile",
        }
    raise AdapterConfigurationError("live_adapter_not_supported")


def _minimal_environment(private_tmp: str) -> dict[str, str]:
    return {
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": private_tmp,
    }


def _valid_timeout(timeout_seconds: int) -> bool:
    return (
        isinstance(timeout_seconds, int)
        and not isinstance(timeout_seconds, bool)
        and 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    )


def _drain(
    stream: Any,
    destination: bytearray,
    exceeded: threading.Event,
    total: list[int],
    lock: threading.Lock,
) -> None:
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return
            with lock:
                remaining = MAX_OUTPUT_BYTES + 1 - total[0]
                if remaining > 0:
                    accepted = chunk[:remaining]
                    destination.extend(accepted)
                    total[0] += len(accepted)
                if total[0] > MAX_OUTPUT_BYTES:
                    exceeded.set()
    finally:
        stream.close()


def _write_prompt(stream: Any, prompt: bytes, errors: list[str]) -> None:
    try:
        stream.write(prompt)
        stream.flush()
    except OSError as exc:
        errors.append(str(exc))
    finally:
        stream.close()


def _stop_process(process: Any) -> None:
    pid = getattr(process, "pid", None)
    process_group = None
    if isinstance(pid, int) and pid > 0:
        try:
            candidate_group = os.getpgid(pid)
            if candidate_group == os.getpgrp():
                raise OSError("provider process shares the harness process group")
            process_group = candidate_group
            os.killpg(candidate_group, signal.SIGTERM)
        except OSError:
            process_group = None
    try:
        if process_group is None:
            process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process_group is not None:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        except OSError:
            pass
        try:
            os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    if process.poll() is None:
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _invoke(
    *,
    command: list[str],
    request: dict[str, Any],
    timeout_seconds: int,
    parser: Callable[[str], tuple[dict[str, Any] | None, dict[str, Any]]],
    heartbeat: Callable[[], bool | None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    process = None
    isolated_cwd = tempfile.TemporaryDirectory(prefix="saihai-provider-")
    os.chmod(isolated_cwd.name, 0o700)
    try:
        prompt = bounded_prompt(request).encode("utf-8")
        process = _popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=isolated_cwd.name,
            env=_minimal_environment(isolated_cwd.name),
            shell=False,
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    except AdapterConfigurationError as exc:
        return {"status": "unavailable", "reason": str(exc), "stdout": b"", "stderr": b""}
    except OSError as exc:
        if process is not None:
            _stop_process(process)
        return {
            "status": "unavailable",
            "reason": "execution_os_error",
            "exit_code": None,
            "stdout": b"",
            "stderr": _bytes(str(exc)),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    stdout_buffer, stderr_buffer = bytearray(), bytearray()
    exceeded = threading.Event()
    total = [0]
    output_lock = threading.Lock()
    write_errors: list[str] = []
    threads = [
        threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_buffer, exceeded, total, output_lock),
            daemon=True,
        ),
        threading.Thread(
            target=_write_prompt,
            args=(process.stdin, prompt, write_errors),
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_buffer, exceeded, total, output_lock),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout_seconds
    next_heartbeat = time.monotonic()
    terminal_reason = ""
    while process.poll() is None:
        if heartbeat is not None and time.monotonic() >= next_heartbeat:
            try:
                alive = heartbeat()
            except Exception:  # Lease loss is a typed provider boundary, not an adapter crash.
                alive = False
            if alive is False:
                terminal_reason = "lease_lost"
                _stop_process(process)
                break
            next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        if exceeded.is_set():
            terminal_reason = "output_limit_exceeded"
            _stop_process(process)
            break
        if time.monotonic() >= deadline:
            terminal_reason = "timeout"
            _stop_process(process)
            break
        time.sleep(0.01)
    for thread in threads:
        thread.join(timeout=2)
    if not terminal_reason and exceeded.is_set():
        terminal_reason = "output_limit_exceeded"
    stdout = bytes(stdout_buffer[:MAX_OUTPUT_BYTES])
    stderr = bytes(stderr_buffer[:MAX_OUTPUT_BYTES])
    duration_ms = int((time.monotonic() - started) * 1000)
    if terminal_reason:
        return {
            "status": (
                "timeout"
                if terminal_reason == "timeout"
                else "unavailable"
                if terminal_reason == "lease_lost"
                else "malformed_output"
            ),
            "reason": terminal_reason,
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
        }
    if write_errors and process.returncode == 0:
        return {
            "status": "unavailable",
            "reason": "execution_os_error",
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": _bytes(write_errors[0]),
            "duration_ms": duration_ms,
        }
    if process.returncode != 0:
        status, reason = _failure_status(stdout, stderr)
        return {
            "status": status,
            "reason": reason,
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
        }
    if _structured_provider_unavailable(stdout, stderr):
        return {
            "status": "unavailable",
            "reason": "auth_or_quota",
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
        }
    report, evidence = parser(_text(stdout))
    if report is None:
        return {
            "status": "malformed_output",
            "reason": "report_json_missing",
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
        }
    return {
        "status": "ok",
        "reason": "ok",
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "report": report,
        "evidence_fields": evidence,
    }


def _configuration_failure(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "stdout": b"", "stderr": b""}


def invoke_claude_cli(
    request: dict[str, Any],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    heartbeat: Callable[[], bool | None] | None = None,
) -> dict[str, Any]:
    if not _valid_timeout(timeout_seconds):
        return _configuration_failure("invalid_timeout")
    try:
        binding = resolve_execution_binding("claude_headless_p0")
        binary = Path(binding["binary"]["path"])
    except AdapterConfigurationError as exc:
        return _configuration_failure(str(exc))
    command = [
        str(binary),
        "--print",
        "--output-format",
        "json",
        "--permission-mode",
        "plan",
        "--tools",
        "",
        "--disable-slash-commands",
        "--safe-mode",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    return _invoke(
        command=command,
        request=request,
        timeout_seconds=timeout_seconds,
        parser=extract_claude_result,
        heartbeat=heartbeat,
    )


def invoke_codex_exec(
    request: dict[str, Any],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    heartbeat: Callable[[], bool | None] | None = None,
) -> dict[str, Any]:
    if not _valid_timeout(timeout_seconds):
        return _configuration_failure("invalid_timeout")
    try:
        binding = resolve_execution_binding("codex_cli_openai_p0")
        binary = Path(binding["binary"]["path"])
        wrapper = Path(binding["wrapper"]["path"])
        profile = Path(binding["profile"]["path"])
    except AdapterConfigurationError as exc:
        return _configuration_failure("codex_confinement_unavailable:" + str(exc))
    command = [
        str(wrapper),
        "--profile",
        str(profile),
        "--",
        str(binary),
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--config",
        'approval_policy="never"',
        "--config",
        'shell_environment_policy.inherit="none"',
        "--skip-git-repo-check",
        "-C",
        ".",
        "-",
    ]
    return _invoke(
        command=command,
        request=request,
        timeout_seconds=timeout_seconds,
        parser=extract_codex_result,
        heartbeat=heartbeat,
    )
