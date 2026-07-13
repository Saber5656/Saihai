from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("sensitive_access_guard.py")
SPEC = importlib.util.spec_from_file_location("sensitive_access_guard", SCRIPT)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class PolicyTest(unittest.TestCase):
    def test_denies_secret_bearing_names_without_examples_exception(self) -> None:
        denied = [
            ".env",
            ".env.local",
            ".env.example",
            "directory-path.env",
            ".envrc",
            ".env-local",
            ".env_local",
            "backend.env",
            "production.env.local",
            ".direnv",
            "config/auth.json",
            "config/foo_auth.json",
            "config/prod-secret.json",
            "config/db_credentials.json",
            "config/session_token",
            "config/my-token.txt",
            "gh-token.txt",
            "ACCESS_TOKEN",
            "oauth.json",
            "github_pat.txt",
            "PAT",
            "password.txt",
            ".config/gcloud/application_default_credentials.json",
            "ssh-key.pem",
            "client_secret",
            "deploy-key.pem",
            "id_ed25519.pub",
            "/Users/me/.ssh/config",
            "/Users/me/.config/gh/hosts.yml",
            "/Users/me/.docker/config.json",
            ".npmrc",
            ".git-credentials",
            "service-key.json",
            "keys.json",
            "certificates/service.p12",
            "certificates/service.ppk",
            "certificates/private.pkcs8",
            "certificates/key.asc",
            "vpn/client.ovpn",
            "terraform.tfvars",
            "production.auto.tfvars.json",
            ".dev.vars",
            ".flaskenv",
            ".authinfo",
            ".authinfo.gpg",
            "environment.production.yaml",
            ".bash_login",
            ".zshrc",
            ".zlogin",
            "/Users/me/.config/fish/config.fish",
            "/Users/me/.config/age/identity.txt",
            ".sops.yaml",
            "credentials.json",
            ".pgpass",
            ".vault-token",
            "/Users/me/.local/share/keyrings/login.keyring",
            "/Users/me/.config/op/config",
            "/Users/me/.config/rclone/rclone.conf",
            "/Users/me/.oci/config",
            "/Users/me/Library/Keychains/login.keychain-db",
            "keys/signing.pub",
            "keys/release.snk",
            "secrets/archive.age",
            ".netrc",
        ]
        for value in denied:
            with self.subTest(value=value):
                self.assertIsNotNone(GUARD.sensitive_reason(value))

    def test_allows_non_secret_key_substrings(self) -> None:
        allowed = [
            "keymap.json",
            "keyboard.ts",
            "monkey.png",
            "environment.ts",
            "tokenizer.py",
            "authoring-guide.md",
            "secretary.py",
            ".flaskenv-guide.md",
            "authinfo_parser.py",
            "zlogin-helper.md",
            "age/identity.txt",
            "publications/report.pub",
        ]
        for value in allowed:
            with self.subTest(value=value):
                self.assertIsNone(GUARD.sensitive_reason(value))

    def test_runtime_examples_have_required_hook_contract(self) -> None:
        for name in ("codex-hooks.example.json", "claude-settings.example.json"):
            with self.subTest(name=name):
                config = json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))
                hooks = config["hooks"]
                self.assertEqual(hooks["PreToolUse"][0]["matcher"], ".*")
                pre = hooks["PreToolUse"][0]["hooks"][0]
                self.assertEqual(pre["type"], "command")
                self.assertIn("sensitive_access_guard.py", pre["command"])
                self.assertIn("UserPromptSubmit", hooks)

    def test_claude_example_has_no_env_template_exception(self) -> None:
        config = json.loads(
            Path(__file__).with_name("claude-settings.example.json").read_text(encoding="utf-8")
        )
        deny = config["permissions"]["deny"]
        self.assertIn("Read(//**/.env*)", deny)
        self.assertFalse(any("allow" in item.lower() for item in deny))


class HookTest(unittest.TestCase):
    def run_hook(self, state: str, payload: object) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["SENSITIVE_ACCESS_GUARD_STATE_ROOT"] = state
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--runtime", "codex"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_detection_latches_session_and_blocks_later_safe_tool(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            first = self.run_hook(
                state,
                {"session_id": "session-a", "tool_input": {"command": "cat .env.example"}},
            )
            self.assertEqual(first.returncode, 0)
            self.assertEqual(
                json.loads(first.stdout)["hookSpecificOutput"]["permissionDecision"], "deny"
            )
            latch = next(Path(state).glob("*.json"))
            saved = json.loads(latch.read_text(encoding="utf-8"))
            self.assertNotIn(".env", json.dumps(saved))
            self.assertNotIn("session-a", json.dumps(saved))

            second = self.run_hook(
                state,
                {"session_id": "session-a", "tool_input": {"command": "git status"}},
            )
            self.assertEqual(second.returncode, 0)
            self.assertIn("session_latched", second.stdout)

    def test_safe_input_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            result = self.run_hook(
                state,
                {"session_id": "session-safe", "tool_input": {"command": "git status"}},
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_latched_user_prompt_uses_prompt_block_schema(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            self.run_hook(
                state,
                {"session_id": "session-prompt", "tool_input": {"path": ".env"}},
            )
            result = self.run_hook(
                state,
                {
                    "session_id": "session-prompt",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "continue",
                },
            )
            output = json.loads(result.stdout)
            self.assertEqual(output["decision"], "block")
            self.assertNotIn("hookSpecificOutput", output)

    def test_malformed_input_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            env = os.environ.copy()
            env["SENSITIVE_ACCESS_GUARD_STATE_ROOT"] = state
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime", "claude"],
                input="not-json",
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("failed closed", result.stderr)

    def test_environment_load_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            env = os.environ.copy()
            env["SENSITIVE_ACCESS_GUARD_STATE_ROOT"] = state
            env["SAIHAI_DIRECTORY_PATH_ENV"] = str(Path(state) / "missing-config")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime", "codex"],
                input=json.dumps(
                    {"session_id": "session-env-error", "tool_input": {"command": "git status"}}
                ),
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("failed closed: EnvError", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_indirect_shell_reads_are_denied(self) -> None:
        for command in (
            "cat .*",
            "/bin/cat .*",
            "command cat .*",
            "sudo cat .*",
            "env cat .*",
            "find . -type f -exec cat {} +",
            "cat $TARGET",
            "cp .* /tmp/out",
            "tar cf /tmp/out.tar .*",
            "rsync -a .* /tmp/out",
            "ls .*",
            "wc .*",
            "stat .*",
            "du .*",
            "diff -u .* empty",
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as state:
                result = self.run_hook(
                    state,
                    {
                        "session_id": f"session-{command}",
                        "cwd": state,
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn("indirect_filesystem_read", result.stdout)

    def test_attached_shell_redirections_are_denied(self) -> None:
        for command in (
            "cat<.env",
            "head <.env",
            "cat<.ssh/config",
            "cat<*",
            "cat<.?nv",
            "cat<$TARGET",
            "head<${FILE}",
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as state:
                result = self.run_hook(
                    state,
                    {
                        "session_id": f"session-redirection-{command}",
                        "cwd": state,
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn('"permissionDecision":"deny"', result.stdout)

    def test_glob_patterns_in_non_shell_fields_are_denied(self) -> None:
        for pattern in (".env*", "production.env*", "*-secret.json"):
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as state:
                result = self.run_hook(
                    state,
                    {
                        "session_id": f"session-pattern-{pattern}",
                        "tool_name": "mcp_search",
                        "tool_input": {"pattern": pattern},
                    },
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn("deny", result.stdout)

    def test_codex_shaped_cmd_payload_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            result = self.run_hook(
                state,
                {
                    "session_id": "session-codex-cmd",
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "/bin/cat .*"},
                },
            )
            self.assertIn("indirect_filesystem_read", result.stdout)

    def test_command_and_cmd_are_checked_independently(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            result = self.run_hook(
                state,
                {
                    "session_id": "session-dual-command",
                    "tool_name": "extended_exec",
                    "tool_input": {"command": 123, "cmd": "cp .* /tmp/out"},
                },
            )
            self.assertIn("indirect_filesystem_read", result.stdout)

    def test_symlink_to_protected_name_is_denied_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            root = Path(state)
            protected = root / ".env.example"
            protected.write_text("not-a-real-secret\n", encoding="utf-8")
            link = root / "safe-link"
            link.symlink_to(protected)
            result = self.run_hook(
                str(root / "guard-state"),
                {
                    "session_id": "session-link",
                    "cwd": state,
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(link)},
                },
            )
            self.assertIn("symlink_to_protected_path", result.stdout)

    def test_list_valued_paths_resolve_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            root = Path(state)
            protected = root / ".env.local"
            protected.write_text("not-a-real-secret\n", encoding="utf-8")
            link = root / "safe-link"
            link.symlink_to(protected)
            result = self.run_hook(
                str(root / "guard-state"),
                {
                    "session_id": "session-list-link",
                    "cwd": state,
                    "tool_name": "read_multiple_files",
                    "tool_input": {"paths": [str(link)]},
                },
            )
            self.assertIn("symlink_to_protected_path", result.stdout)

    def test_unsafe_state_root_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            state = Path(parent) / "state"
            state.mkdir(mode=0o755)
            os.chmod(state, 0o755)
            result = self.run_hook(
                str(state),
                {"session_id": "session-mode", "tool_input": {"command": "git status"}},
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("failed closed", result.stderr)

    def test_symlink_state_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            real = Path(parent) / "real"
            real.mkdir(mode=0o700)
            state = Path(parent) / "state"
            state.symlink_to(real, target_is_directory=True)
            result = self.run_hook(
                str(state),
                {"session_id": "session-root-link", "tool_input": {"command": "git status"}},
            )
            self.assertEqual(result.returncode, 2)

    def test_unsafe_lock_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            session_id = "session-lock-mode"
            lock = Path(state) / f"{GUARD.session_key(session_id)}.lock"
            lock.write_text("", encoding="utf-8")
            os.chmod(lock, 0o644)
            result = self.run_hook(
                state,
                {"session_id": session_id, "tool_input": {"command": "git status"}},
            )
            self.assertEqual(result.returncode, 2)

    def test_parallel_invocation_waits_for_session_lock(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            session_id = "session-concurrent"
            env = os.environ.copy()
            env["SENSITIVE_ACCESS_GUARD_STATE_ROOT"] = state
            with mock.patch.dict(
                os.environ, {"SENSITIVE_ACCESS_GUARD_STATE_ROOT": state}
            ), GUARD.session_lock("codex", session_id):
                child = subprocess.Popen(
                    [sys.executable, str(SCRIPT), "--runtime", "codex"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                assert child.stdin is not None
                child.stdin.write(json.dumps({"session_id": session_id, "tool_input": {"command": "git status"}}))
                child.stdin.close()
                time.sleep(0.1)
                self.assertIsNone(child.poll())
            child.wait(timeout=2)
            self.assertEqual(child.returncode, 0)
            assert child.stdout is not None and child.stderr is not None
            child.stdout.close()
            child.stderr.close()

    def test_session_lock_wait_has_a_fail_closed_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            session_id = "session-lock-deadline"
            with mock.patch.dict(
                os.environ, {"SENSITIVE_ACCESS_GUARD_STATE_ROOT": state}
            ), mock.patch.object(GUARD, "LOCK_TIMEOUT_SECONDS", 0.05), GUARD.session_lock(
                "codex", session_id
            ):
                started = time.monotonic()
                with self.assertRaisesRegex(GUARD.GuardError, "deadline exceeded"):
                    with GUARD.session_lock("codex", session_id):
                        pass
                self.assertLess(time.monotonic() - started, 0.5)

    def test_human_clear_removes_latch(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            self.run_hook(
                state,
                {"session_id": "session-clear", "tool_input": {"path": ".ssh/config"}},
            )
            env = os.environ.copy()
            env["SENSITIVE_ACCESS_GUARD_STATE_ROOT"] = state
            cleared = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--runtime",
                    "codex",
                    "--clear",
                    "--session-id",
                    "session-clear",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(cleared.returncode, 0)
            self.assertEqual(list(Path(state).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
