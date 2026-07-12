from __future__ import annotations

import os
import importlib.util
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import saihai_env


class SaihaiEnvTests(unittest.TestCase):
    def write_env(self, root: Path, text: str) -> Path:
        path = root / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_process_environment_including_empty_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = self.write_env(root, "AGENTS_VAULT_ROOT=from-file\nSKILLS_ROOT=from-file\n")
            env = {"SAIHAI_ENV_FILE": str(env_file), "AGENTS_VAULT_ROOT": "", "SKILLS_ROOT": "/process"}
            result = saihai_env.load_environment(environ=env)
            self.assertEqual(env["AGENTS_VAULT_ROOT"], "")
            self.assertEqual(env["SKILLS_ROOT"], "/process")
            self.assertEqual(set(result["skipped_process_keys"]), {"AGENTS_VAULT_ROOT", "SKILLS_ROOT"})

    def test_quotes_comments_home_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = self.write_env(root, "# c\nAGENTS_VAULT_ROOT='vault dir'\nUSER_VAULT_ROOT=\"${HOME}/personal\" # c\nSKILLS_ROOT=~/skills\n")
            env = {"SAIHAI_ENV_FILE": str(env_file)}
            with mock.patch("pathlib.Path.home", return_value=Path("/home/tester")):
                saihai_env.load_environment(environ=env)
            self.assertEqual(env["AGENTS_VAULT_ROOT"], str((root / "vault dir").resolve()))
            self.assertEqual(env["USER_VAULT_ROOT"], "/home/tester/personal")
            self.assertEqual(env["SKILLS_ROOT"], "/home/tester/skills")

    def test_rejects_malformed_unknown_export_and_command_substitution(self) -> None:
        cases = ["NO_EQUALS", "UNKNOWN_KEY=x", "export AGENTS_VAULT_ROOT=x", "AGENTS_VAULT_ROOT=$(id)"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(saihai_env.EnvError):
                saihai_env.parse_env(text)

    def test_schema_bounds_and_compatibility_alias_transform(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            invalid = self.write_env(root, "ITB_TASK_DETAIL_LINE_CAP=1\n")
            with self.assertRaises(saihai_env.EnvError):
                saihai_env.load_environment(environ={"SAIHAI_ENV_FILE": str(invalid)})
            invalid.unlink()
            env = {"SKILLS_REPO_ROOT": str(root / "skills-repo")}
            result = saihai_env.load_environment(checkout_root=root, environ=env)
            self.assertEqual(env["SKILLS_ROOT"], str(root / "skills-repo" / "skills"))
            self.assertIn("deprecated_alias:SKILLS_REPO_ROOT:use=SKILLS_ROOT", result["warnings"])

    def test_process_alias_precedes_env_canonical_including_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = self.write_env(root, "USER_VAULT_ROOT=from-file\n")
            for alias_value in (str(root / "process"), ""):
                env = {"SAIHAI_ENV_FILE": str(env_file), "YASU_VAULT_ROOT": alias_value}
                saihai_env.load_environment(environ=env)
                self.assertEqual(env["USER_VAULT_ROOT"], alias_value)

    def test_optional_missing_env_and_vault_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = saihai_env.load_environment(checkout_root=Path(raw), environ={})
            self.assertEqual(result["status"], "not_configured")
            with self.assertRaises(saihai_env.EnvError):
                saihai_env.load_environment(checkout_root=Path(raw), environ={}, require_vault=True)

    def test_vault_must_exist_be_directory_and_read_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for value in ("", str(root / "missing")):
                with self.subTest(value=value), self.assertRaises(saihai_env.EnvError):
                    saihai_env.validate_vault({"AGENTS_VAULT_ROOT": value})
            file_path = root / "file"
            file_path.write_text("x")
            with self.assertRaises(saihai_env.EnvError):
                saihai_env.validate_vault({"AGENTS_VAULT_ROOT": str(file_path)})
            saihai_env.validate_vault({"AGENTS_VAULT_ROOT": str(root)})

    def test_primary_worktree_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            primary = Path(raw) / "primary"
            linked = Path(raw) / "linked"
            subprocess.run(["git", "init", "-q", str(primary)], check=True)
            subprocess.run(["git", "-C", str(primary), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(primary), "config", "user.name", "Test"], check=True)
            (primary / "seed").write_text("x")
            subprocess.run(["git", "-C", str(primary), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(primary), "commit", "-qm", "seed"], check=True)
            subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)], check=True)
            env_file = self.write_env(primary, "AGENTS_VAULT_ROOT=vault\n")
            self.assertEqual(saihai_env.resolve_env_file(linked, {}), env_file.resolve())

    def test_diagnostics_do_not_contain_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secretish = "private-value-must-not-leak"
            env_file = self.write_env(root, f"AGENTS_VAULT_ROOT={secretish}\n")
            result = saihai_env.load_environment(environ={"SAIHAI_ENV_FILE": str(env_file)})
            self.assertNotIn(secretish, repr(result))

    def test_empty_required_path_stays_empty_and_artifacts_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_file = self.write_env(Path(raw), "AGENTS_VAULT_ROOT=\n")
            env = {"SAIHAI_ENV_FILE": str(env_file)}
            saihai_env.load_environment(environ=env)
            self.assertEqual(env["AGENTS_VAULT_ROOT"], "")
            with self.assertRaises(saihai_env.EnvError):
                saihai_env.validate_vault(env)
            configured = "/private/local/value"
            payload = {"report": configured, "run_state": [f"root={configured}"], "context_refs": configured}
            redacted = saihai_env.redact_environment_values(payload, {"AGENTS_VAULT_ROOT": configured})
            self.assertNotIn(configured, repr(redacted))
            self.assertIn("${AGENTS_VAULT_ROOT}", repr(redacted))
            ordinary = saihai_env.redact_environment_values(
                {"status": "enabled", "message": "already enabled"},
                {"AGENT_ORG_STATE": "enabled"},
            )
            self.assertEqual(ordinary, {"status": "enabled", "message": "already enabled"})

    def test_real_itb_artifact_writers_redact_loaded_path_canary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            canary = root / "machine-specific-canary"
            canary.mkdir()
            state_a = root / "itb-state-a"
            state_b = root / "itb-state-b"
            env_file = self.write_env(
                root,
                f'AGENTS_VAULT_ROOT="{canary}"\nSAIHAI_ITB_STATE_ROOTS="{state_a}{os.pathsep}{state_b}"\n',
            )
            saihai_env.load_environment(environ={"SAIHAI_ENV_FILE": str(env_file)})
            builder_path = ROOT / "organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"
            with mock.patch.dict(os.environ, {"SAIHAI_ENV_FILE": str(env_file)}, clear=True):
                spec = importlib.util.spec_from_file_location("itb_env_sink_test", builder_path)
                assert spec and spec.loader
                builder = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(builder)
                artifacts = {
                    "run-state.json": {
                        "run_state": "created",
                        "cwd": str(canary),
                        "context_refs": [str(canary / "ref")],
                        "state_roots": [str(state_a), str(state_b)],
                    },
                    "evidence.jsonl": {"evidence_path": str(canary / "evidence"), "state_root": str(state_a)},
                    "report.md": f"report root: {canary}; states: {state_a}, {state_b}\n",
                }
                builder.write_json_yaml(root / "run-state.json", artifacts["run-state.json"])
                builder.append_jsonl_unlocked(root / "evidence.jsonl", artifacts["evidence.jsonl"])
                builder.atomic_write_text(root / "report.md", artifacts["report.md"])
            combined = "".join((root / name).read_text(encoding="utf-8") for name in artifacts)
            self.assertNotIn(str(canary), combined)
            self.assertNotIn(str(state_a), combined)
            self.assertNotIn(str(state_b), combined)
            self.assertIn("${AGENTS_VAULT_ROOT}", combined)
            self.assertIn("${SAIHAI_ITB_STATE_ROOTS}", combined)


class SetupEnvTests(unittest.TestCase):
    def run_setup(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for key in ("AGENTS_VAULT_ROOT", "SAIHAI_ENV_FILE", "SAIHAI_ROOT"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/setup_env.py"), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_create_non_destructive_permissions_and_check(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vault = root / "vault"
            vault.mkdir()
            env_file = root / ".env"
            first = self.run_setup("--non-interactive", "--agents-vault", str(vault), "--env-file", str(env_file))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            before = env_file.read_bytes()
            second = self.run_setup("--non-interactive", "--agents-vault", str(vault), "--env-file", str(env_file))
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(env_file.read_bytes(), before)
            checked = self.run_setup("--check", "--env-file", str(env_file))
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertNotIn(str(vault), checked.stdout + checked.stderr)

    def test_setup_quotes_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vault = root / "vault with spaces"
            vault.mkdir()
            env_file = root / ".env"
            created = self.run_setup("--non-interactive", "--agents-vault", str(vault), "--env-file", str(env_file))
            self.assertEqual(created.returncode, 0, created.stderr)
            parsed = saihai_env.parse_env(env_file.read_text(encoding="utf-8"))
            self.assertEqual(parsed["AGENTS_VAULT_ROOT"], str(vault.resolve()))


if __name__ == "__main__":
    unittest.main()
