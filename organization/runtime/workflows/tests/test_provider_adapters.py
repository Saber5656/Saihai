#!/usr/bin/env python3
"""Offline tests for fail-closed live provider adapters."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import provider_adapters


def request() -> dict:
    content = "README.md\nFixture content approved by the runner."
    encoded = content.encode()
    intended_model = "claude-sonnet-4-6"
    return {
        "request_id": "req-live",
        "run_id": "run-live",
        "workflow_id": "single_step_external_review",
        "step_id": "review",
        "intended_model": intended_model,
        "adapter": {"command_argv": provider_adapters.claude_argv_template(intended_model)},
        "instruction": "Review the bounded fixture.",
        "context_snapshot": {
            "content": content,
            "byte_length": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        },
        "evidence_path": "/tmp/evidence",
        "transcript_path": "/tmp/transcript",
    }


def codex_request() -> dict:
    candidate = request()
    candidate["intended_model"] = "operator-selected-openai"
    candidate["adapter"] = {
        "provider_adapter_id": "codex_cli_openai_p0",
        "default_model": "operator-selected-openai",
        "effective_model_policy": "record_without_equality",
    }
    return candidate


class FakeProcess:
    def __init__(
        self,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
        *,
        running: bool = False,
        pid: int | None = None,
    ):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if running else returncode
        self.pid = pid

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["fake"], 1)
        return self.returncode


def fake_popen(stdout: bytes, stderr: bytes = b"", returncode: int = 0, *, running: bool = False):
    calls: list[dict] = []

    def factory(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return FakeProcess(stdout, stderr, returncode, running=running)

    return factory, calls


@contextmanager
def patched_environ(values: dict[str, str]):
    original = os.environ.copy()
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def secure_file(root: Path, name: str, content: bytes = b"fixture executable") -> tuple[Path, str]:
    path = root / name
    path.write_bytes(content)
    path.chmod(0o700)
    return path.resolve(), hashlib.sha256(content).hexdigest()


def claude_binding(root: Path) -> dict[str, str]:
    binary, digest = secure_file(root, "claude")
    return {
        "SAIHAI_CLAUDE_EXECUTABLE_PATH": str(binary),
        "SAIHAI_CLAUDE_EXECUTABLE_SHA256": digest,
    }


def codex_binding(root: Path) -> dict[str, str]:
    binary, binary_digest = secure_file(root, "codex")
    wrapper, wrapper_digest = secure_file(root, "confine")
    profile, profile_digest = secure_file(root, "profile")
    return {
        "SAIHAI_CODEX_EXECUTABLE_PATH": str(binary),
        "SAIHAI_CODEX_EXECUTABLE_SHA256": binary_digest,
        "SAIHAI_CODEX_CONFINEMENT_WRAPPER_PATH": str(wrapper),
        "SAIHAI_CODEX_CONFINEMENT_WRAPPER_SHA256": wrapper_digest,
        "SAIHAI_CODEX_CONFINEMENT_PROFILE_PATH": str(profile),
        "SAIHAI_CODEX_CONFINEMENT_PROFILE_SHA256": profile_digest,
    }


def invoke_with_fake(
    adapter,
    binding: dict[str, str],
    stdout: bytes,
    stderr: bytes = b"",
    returncode: int = 0,
    *,
    request_value: dict | None = None,
):
    factory, calls = fake_popen(stdout, stderr, returncode)
    original = provider_adapters._popen
    provider_adapters._popen = factory
    try:
        with patched_environ(binding):
            result = adapter(request_value or request(), timeout_seconds=10)
    finally:
        provider_adapters._popen = original
    return result, calls


def test_extract_json_and_digest_verified_prompt() -> None:
    assert provider_adapters.extract_json_object('{"ok":true}') == {"ok": True}
    assert provider_adapters.extract_json_object('```json\n{"ok":true}\n```') == {"ok": True}
    prompt = provider_adapters.bounded_prompt(request())
    assert "Fixture content approved by the runner." in prompt
    assert "/tmp/evidence" not in prompt
    altered = request()
    altered["context_snapshot"]["content"] += " changed"
    try:
        provider_adapters.bounded_prompt(altered)
    except provider_adapters.AdapterConfigurationError as exc:
        assert str(exc) == "context_snapshot_length_mismatch"
    else:
        raise AssertionError("modified snapshot must fail closed")


def test_claude_fixture_fixed_command_and_minimal_env() -> None:
    with tempfile.TemporaryDirectory() as raw:
        binding = claude_binding(Path(raw))
        binding.update({"HOME": raw, "LANG": "C", "SECRET_TOKEN": "must-not-leak"})
        result, calls = invoke_with_fake(
            provider_adapters.invoke_claude_cli,
            binding,
            (FIXTURE_DIR / "claude_print_result.json").read_bytes(),
        )
    assert result["status"] == "ok"
    assert result["report"]["run_id"] == "run-live"
    assert result["evidence_fields"]["usage"] == {"input_tokens": 12, "output_tokens": 34}
    command = calls[0]["command"]
    assert command[0] == binding["SAIHAI_CLAUDE_EXECUTABLE_PATH"]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--model") + 1] == request()["intended_model"]
    assert calls[0]["shell"] is False
    assert calls[0]["start_new_session"] is True
    assert calls[0]["cwd"] != str(provider_adapters.REPO_ROOT)
    assert Path(calls[0]["cwd"]).name.startswith("saihai-provider-")
    assert calls[0]["env"]["HOME"] == pwd.getpwuid(os.getuid()).pw_dir
    assert calls[0]["env"]["LANG"] == "C.UTF-8"
    assert calls[0]["env"]["LC_ALL"] == "C.UTF-8"
    assert calls[0]["env"]["TMPDIR"] == calls[0]["cwd"]
    assert "SECRET_TOKEN" not in calls[0]["env"]

    tampered = request()
    tampered["adapter"]["command_argv"][3] = "claude-opus-tampered"
    with tempfile.TemporaryDirectory() as raw:
        blocked, blocked_calls = invoke_with_fake(
            provider_adapters.invoke_claude_cli,
            claude_binding(Path(raw)),
            (FIXTURE_DIR / "claude_print_result.json").read_bytes(),
            request_value=tampered,
        )
    assert blocked["status"] == "unavailable"
    assert blocked["reason"] == "claude_argv_template_mismatch"
    assert blocked_calls == []


def test_codex_requires_pinned_confinement_and_uses_wrapper() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        binary, digest = secure_file(root, "codex")
        incomplete = {
            "SAIHAI_CODEX_EXECUTABLE_PATH": str(binary),
            "SAIHAI_CODEX_EXECUTABLE_SHA256": digest,
        }
        with patched_environ(incomplete):
            unavailable = provider_adapters.invoke_codex_exec(
                codex_request(), timeout_seconds=10
            )
        assert unavailable["status"] == "unavailable"
        assert unavailable["reason"].startswith("codex_confinement_unavailable:")

        binding = codex_binding(root)
        result, calls = invoke_with_fake(
            provider_adapters.invoke_codex_exec,
            binding,
            (FIXTURE_DIR / "codex_exec_events.jsonl").read_bytes(),
            request_value=codex_request(),
        )
    assert result["status"] == "ok"
    command = calls[0]["command"]
    assert command[:4] == [
        binding["SAIHAI_CODEX_CONFINEMENT_WRAPPER_PATH"],
        "--profile",
        binding["SAIHAI_CODEX_CONFINEMENT_PROFILE_PATH"],
        "--",
    ]
    assert command[4] == binding["SAIHAI_CODEX_EXECUTABLE_PATH"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command and "--ignore-rules" in command


def test_host_binding_rejects_missing_digest_symlink_mode_and_digest_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        binary, digest = secure_file(root, "claude")
        cases = [
            ({"SAIHAI_CLAUDE_EXECUTABLE_PATH": str(binary)}, "host_binding_missing"),
            (
                {
                    "SAIHAI_CLAUDE_EXECUTABLE_PATH": str(binary),
                    "SAIHAI_CLAUDE_EXECUTABLE_SHA256": "0" * 64,
                },
                "host_binding_digest_mismatch",
            ),
        ]
        link = root / "claude-link"
        link.symlink_to(binary)
        cases.append(
            (
                {
                    "SAIHAI_CLAUDE_EXECUTABLE_PATH": str(link),
                    "SAIHAI_CLAUDE_EXECUTABLE_SHA256": digest,
                },
                "host_binding_not_regular",
            )
        )
        for values, reason in cases:
            with patched_environ(values):
                result = provider_adapters.invoke_claude_cli(request())
            assert result["reason"] == reason
        binary.chmod(0o722)
        with patched_environ(
            {
                "SAIHAI_CLAUDE_EXECUTABLE_PATH": str(binary),
                "SAIHAI_CLAUDE_EXECUTABLE_SHA256": digest,
            }
        ):
            assert provider_adapters.invoke_claude_cli(request())["reason"] == "host_binding_insecure_mode"


def test_structured_stdout_auth_quota_and_nonzero_classes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        binding = claude_binding(Path(raw))
        for stdout, stderr, returncode, expected in (
            (b'{"error":{"type":"authentication_error"}}', b"", 0, "unavailable"),
            (b'{"error":{"code":"insufficient_quota"}}', b"", 1, "unavailable"),
            (b"", b"rate limit exceeded", 1, "unavailable"),
            (b"", b"boom", 1, "nonzero_exit"),
        ):
            result, _calls = invoke_with_fake(
                provider_adapters.invoke_claude_cli, binding, stdout, stderr, returncode
            )
            assert result["status"] == expected


def test_codex_structured_auth_nonzero_and_malformed_classes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        binding = codex_binding(Path(raw))
        cases = (
            (b'{"type":"turn.failed","error":{"code":"insufficient_quota"}}\n', b"", 1, "unavailable", "auth_or_quota"),
            (b"", b"provider process failed", 7, "nonzero_exit", "nonzero_exit"),
            (b'{"type":"thread.started"}\n', b"", 0, "malformed_output", "report_json_missing"),
        )
        for stdout, stderr, returncode, expected_status, expected_reason in cases:
            result, _calls = invoke_with_fake(
                provider_adapters.invoke_codex_exec,
                binding,
                stdout,
                stderr,
                returncode,
                request_value=codex_request(),
            )
            assert result["status"] == expected_status
            assert result["reason"] == expected_reason


def test_timeout_validation_oserror_lease_loss_and_output_ceiling() -> None:
    assert provider_adapters.invoke_claude_cli(request(), timeout_seconds=0)["reason"] == "invalid_timeout"
    assert (
        provider_adapters.invoke_claude_cli(
            request(), timeout_seconds=provider_adapters.MAX_TIMEOUT_SECONDS + 1
        )["reason"]
        == "invalid_timeout"
    )
    with tempfile.TemporaryDirectory() as raw:
        binding = claude_binding(Path(raw))
        original = provider_adapters._popen
        provider_adapters._popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline"))
        try:
            with patched_environ(binding):
                failed = provider_adapters.invoke_claude_cli(request())
        finally:
            provider_adapters._popen = original
        assert failed["status"] == "unavailable" and failed["reason"] == "execution_os_error"

        running_factory, _calls = fake_popen(b"", running=True)
        provider_adapters._popen = running_factory
        try:
            with patched_environ(binding):
                lease_lost = provider_adapters.invoke_claude_cli(request(), heartbeat=lambda: False)
        finally:
            provider_adapters._popen = original
        assert lease_lost["status"] == "unavailable" and lease_lost["reason"] == "lease_lost"

        running_factory, _calls = fake_popen(b"partial", running=True)
        provider_adapters._popen = running_factory
        try:
            with patched_environ(binding):
                timed_out = provider_adapters.invoke_claude_cli(request(), timeout_seconds=1)
        finally:
            provider_adapters._popen = original
        assert timed_out["status"] == "timeout" and timed_out["reason"] == "timeout"

        oversized = b"x" * (provider_adapters.MAX_OUTPUT_BYTES + 1)
        running_factory, _calls = fake_popen(oversized, running=True)
        provider_adapters._popen = running_factory
        try:
            with patched_environ(binding):
                limited = provider_adapters.invoke_claude_cli(request())
        finally:
            provider_adapters._popen = original
        assert limited["reason"] == "output_limit_exceeded"
        assert len(limited["stdout"]) == provider_adapters.MAX_OUTPUT_BYTES


def test_stop_process_terminates_dedicated_process_group() -> None:
    process = FakeProcess(b"", running=True, pid=4242)
    signals: list[tuple[int, int]] = []
    original_getpgid = provider_adapters.os.getpgid
    original_getpgrp = provider_adapters.os.getpgrp
    original_killpg = provider_adapters.os.killpg

    def fake_killpg(group: int, sent_signal: int) -> None:
        signals.append((group, sent_signal))
        if sent_signal == provider_adapters.signal.SIGTERM:
            process.returncode = -sent_signal

    provider_adapters.os.getpgid = lambda _pid: 4242
    provider_adapters.os.getpgrp = lambda: 7
    provider_adapters.os.killpg = fake_killpg
    try:
        provider_adapters._stop_process(process)
    finally:
        provider_adapters.os.getpgid = original_getpgid
        provider_adapters.os.getpgrp = original_getpgrp
        provider_adapters.os.killpg = original_killpg
    assert signals == [
        (4242, provider_adapters.signal.SIGTERM),
        (4242, 0),
        (4242, provider_adapters.signal.SIGKILL),
    ]


if __name__ == "__main__":
    tests = (
        test_extract_json_and_digest_verified_prompt,
        test_claude_fixture_fixed_command_and_minimal_env,
        test_codex_requires_pinned_confinement_and_uses_wrapper,
        test_host_binding_rejects_missing_digest_symlink_mode_and_digest_mismatch,
        test_structured_stdout_auth_quota_and_nonzero_classes,
        test_codex_structured_auth_nonzero_and_malformed_classes,
        test_timeout_validation_oserror_lease_loss_and_output_ceiling,
        test_stop_process_terminates_dedicated_process_group,
    )
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))
