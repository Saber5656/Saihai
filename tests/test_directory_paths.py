from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import directory_paths


class DirectoryPathsTests(unittest.TestCase):
    def write_env(self, root: Path, text: str) -> Path:
        path = root / "directory-path.env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_process_environment_including_empty_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = self.write_env(root, "AGENTS_VAULT_ROOT=from-file\nSKILLS_ROOT=from-file\n")
            env = {"SAHAI_DIRECTORY_PATH_ENV": str(env_file), "AGENTS_VAULT_ROOT": "", "SKILLS_ROOT": "/process"}
            result = directory_paths.load_environment(environ=env)
            self.assertEqual(env["AGENTS_VAULT_ROOT"], "")
            self.assertEqual(env["SKILLS_ROOT"], "/process")
            self.assertEqual(set(result["skipped_process_keys"]), {"AGENTS_VAULT_ROOT", "SKILLS_ROOT"})

    def test_quotes_comments_home_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = self.write_env(root, "# c\nAGENTS_VAULT_ROOT='vault dir'\nUSER_VAULT_ROOT=\"${HOME}/personal\" # c\nSKILLS_ROOT=~/skills\n")
            env = {"SAHAI_DIRECTORY_PATH_ENV": str(env_file)}
            with mock.patch("pathlib.Path.home", return_value=Path("/home/tester")):
                directory_paths.load_environment(environ=env)
            self.assertEqual(env["AGENTS_VAULT_ROOT"], str((root / "vault dir").resolve()))
            self.assertEqual(env["USER_VAULT_ROOT"], "/home/tester/personal")
            self.assertEqual(env["SKILLS_ROOT"], "/home/tester/skills")

    def test_rejects_malformed_unknown_export_and_command_substitution(self) -> None:
        cases = ["NO_EQUALS", "UNKNOWN_KEY=x", "export AGENTS_VAULT_ROOT=x", "AGENTS_VAULT_ROOT=$(id)"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(directory_paths.EnvError):
                directory_paths.parse_env(text)

    def test_rejects_runtime_settings_and_supports_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            invalid = self.write_env(root, "ITB_TASK_DETAIL_LINE_CAP=1\n")
            with self.assertRaises(directory_paths.EnvError):
                directory_paths.load_environment(environ={"SAHAI_DIRECTORY_PATH_ENV": str(invalid)})
            invalid.unlink()
            env = {"SKILLS_REPO_SKILLS_ROOT": str(root / "skills")}
            result = directory_paths.load_environment(checkout_root=root, environ=env)
            self.assertEqual(env["SKILLS_ROOT"], str(root / "skills"))
            self.assertIn("deprecated_alias:SKILLS_REPO_SKILLS_ROOT:use=SKILLS_ROOT", result["warnings"])

    def test_legacy_values_populate_canonical_names_and_expand_aliases(self) -> None:
        root = "/tmp/sahai-canonical"
        env = {"SAIHAI_ROOT": root, "DEV_REPO_ROOT": "/tmp/dev"}
        directory_paths.load_environment(checkout_root=Path("/tmp/missing"), environ=env)
        self.assertEqual(env["SAHAI_ROOT"], root)
        self.assertEqual(env["DEV_ROOT"], "/tmp/dev")
        self.assertEqual(
            directory_paths.expand_path_aliases("${SAHAI_ROOT}/organization:$DEV_REPO_ROOT/x", env),
            f"{root}/organization:/tmp/dev/x",
        )

    def test_process_alias_precedes_env_canonical_including_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_file = self.write_env(root, "USER_VAULT_ROOT=from-file\n")
            for alias_value in (str(root / "process"), ""):
                env = {"SAHAI_DIRECTORY_PATH_ENV": str(env_file), "YASU_VAULT_ROOT": alias_value}
                directory_paths.load_environment(environ=env)
                self.assertEqual(env["USER_VAULT_ROOT"], alias_value)

    def test_optional_missing_env_and_vault_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = directory_paths.load_environment(checkout_root=Path(raw), environ={})
            self.assertEqual(result["status"], "not_configured")
            with self.assertRaises(directory_paths.EnvError):
                directory_paths.load_environment(checkout_root=Path(raw), environ={}, require_vault=True)

    def test_vault_must_exist_be_directory_and_read_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for value in ("", str(root / "missing")):
                with self.subTest(value=value), self.assertRaises(directory_paths.EnvError):
                    directory_paths.validate_vault({"AGENTS_VAULT_ROOT": value})
            file_path = root / "file"
            file_path.write_text("x")
            with self.assertRaises(directory_paths.EnvError):
                directory_paths.validate_vault({"AGENTS_VAULT_ROOT": str(file_path)})
            directory_paths.validate_vault({"AGENTS_VAULT_ROOT": str(root)})

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
            self.assertEqual(directory_paths.resolve_env_file(linked, {}), env_file.resolve())

    def test_diagnostics_do_not_contain_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secretish = "private-value-must-not-leak"
            env_file = self.write_env(root, f"AGENTS_VAULT_ROOT={secretish}\n")
            result = directory_paths.load_environment(environ={"SAHAI_DIRECTORY_PATH_ENV": str(env_file)})
            self.assertNotIn(secretish, repr(result))

    def test_empty_required_path_stays_empty_until_required_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_file = self.write_env(Path(raw), "AGENTS_VAULT_ROOT=\n")
            env = {"SAHAI_DIRECTORY_PATH_ENV": str(env_file)}
            directory_paths.load_environment(environ=env)
            self.assertEqual(env["AGENTS_VAULT_ROOT"], "")
            with self.assertRaises(directory_paths.EnvError):
                directory_paths.validate_vault(env)

    def test_required_catalog_rejects_missing_key_and_accepts_all_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = {key: str(root) for key, field in directory_paths.SCHEMA.items() if field.required}
            missing = dict(env)
            missing.pop("SKILLS_ROOT")
            with self.assertRaisesRegex(directory_paths.EnvError, "SKILLS_ROOT"):
                directory_paths.load_environment(checkout_root=root, environ=missing, require_catalog=True)
            directory_paths.load_environment(checkout_root=root, environ=env, require_catalog=True)

    def test_runtime_and_role_dispatcher_are_identical(self) -> None:
        runtime = ROOT / "organization/runtime/infra-task-dispatcher/scripts/itd_monitor.py"
        role = ROOT / "organization/roles/infra-task-dispatcher/scripts/itd_monitor.py"
        self.assertEqual(runtime.read_bytes(), role.read_bytes())


class SetupDirectoryPathsTests(unittest.TestCase):
    def run_setup(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for key in ("AGENTS_VAULT_ROOT", "SAHAI_DIRECTORY_PATH_ENV", "SAHAI_ROOT"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/setup_directory_paths.py"), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def required_args(self, root: Path, vault: Path) -> tuple[str, ...]:
        return (
            "--non-interactive",
            "--agents-vault", str(vault),
            "--user-vault", str(root),
            "--skills-repo-root", str(root),
            "--skills-root", str(root),
            "--dotfiles-root", str(root),
            "--dev-root", str(root),
            "--dev-worktrees-root", str(root),
            "--task-worktree-root", str(root),
        )

    def test_create_non_destructive_permissions_and_check(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vault = root / "vault"
            vault.mkdir()
            env_file = root / "directory-path.env"
            options = self.required_args(root, vault)
            first = self.run_setup(*options, "--env-file", str(env_file))
            self.assertEqual(first.returncode, 0, first.stderr)
            parsed = directory_paths.parse_env(env_file.read_text(encoding="utf-8"))
            self.assertEqual(Path(parsed["SAHAI_ROOT"]), directory_paths.default_catalog_path(ROOT).parent)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            before = env_file.read_bytes()
            second = self.run_setup(*options, "--env-file", str(env_file))
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
            env_file = root / "directory-path.env"
            created = self.run_setup(*self.required_args(root, vault), "--env-file", str(env_file))
            self.assertEqual(created.returncode, 0, created.stderr)
            parsed = directory_paths.parse_env(env_file.read_text(encoding="utf-8"))
            self.assertEqual(parsed["AGENTS_VAULT_ROOT"], str(vault.resolve()))

    def test_check_ignores_exported_saihai_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid_vault = root / "valid-vault"
            valid_vault.mkdir()
            env_file = root / "directory-path.env"
            created = self.run_setup(*self.required_args(root, valid_vault), "--env-file", str(env_file))
            self.assertEqual(created.returncode, 0, created.stderr)
            env = os.environ.copy()
            env.update({"AGENTS_VAULT_ROOT": str(root / "missing"), "YASU_VAULT_ROOT": ""})
            checked = subprocess.run(
                [sys.executable, str(ROOT / "scripts/setup_directory_paths.py"), "--check", "--env-file", str(env_file)],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)


class SyncOrganizationSourcesTests(unittest.TestCase):
    def run_sync(self, repo_root: Path, vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for key in set(directory_paths.SCHEMA) | set(directory_paths.ALIASES) | {"SAHAI_DIRECTORY_PATH_ENV"}:
            env.pop(key, None)
        env["SAHAI_DIRECTORY_PATH_ENV"] = ""
        env.update({key: str(repo_root) for key, field in directory_paths.SCHEMA.items() if field.required})
        env["AGENTS_VAULT_ROOT"] = str(vault)
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/sync_organization_sources.py"), "--repo-root", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_roles_sync_requires_external_non_overlapping_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vault = root / "vault"
            vault.mkdir()
            roles = root / "repo" / "organization" / "roles"
            roles.mkdir(parents=True)
            marker = roles / "keep.md"
            marker.write_text("keep", encoding="utf-8")
            missing = self.run_sync(root / "repo", vault, "--scope", "roles")
            self.assertNotEqual(missing.returncode, 0)
            self.assertTrue(marker.is_file())
            overlap = self.run_sync(
                root / "repo", vault, "--scope", "roles", "--skills-root", str(roles)
            )
            self.assertNotEqual(overlap.returncode, 0)
            self.assertTrue(marker.is_file())


if __name__ == "__main__":
    unittest.main()
