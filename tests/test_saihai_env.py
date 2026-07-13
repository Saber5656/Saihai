from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import saihai_env


class SaihaiRuntimeEnvTests(unittest.TestCase):
    def write_env(self, root: Path, text: str) -> Path:
        path = root / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_process_environment_including_empty_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_file = self.write_env(Path(raw), "AGENT_ORG_STATE=enabled\nITB_CODEX_MODEL=file-model\n")
            env = {
                "SAIHAI_ENV_FILE": str(env_file),
                "AGENT_ORG_STATE": "",
                "ITB_CODEX_MODEL": "process-model",
            }
            result = saihai_env.load_environment(environ=env)
            self.assertEqual(env["AGENT_ORG_STATE"], "")
            self.assertEqual(env["ITB_CODEX_MODEL"], "process-model")
            self.assertEqual(set(result["skipped_process_keys"]), {"AGENT_ORG_STATE", "ITB_CODEX_MODEL"})

    def test_non_path_types_and_bounds_are_preserved(self) -> None:
        valid = "ITB_FINAL_GATE_HARD_BLOCK=true\nITB_TASK_DETAIL_LINE_CAP=220\nITB_ROLE_AGENT_POLL_INTERVAL_SECONDS=2.5\n"
        parsed = saihai_env.parse_env(valid)
        env = dict(parsed)
        saihai_env.load_environment(checkout_root=Path("/tmp/missing"), environ=env)
        for text in (
            "ITB_FINAL_GATE_HARD_BLOCK=maybe\n",
            "ITB_TASK_DETAIL_LINE_CAP=1\n",
            "ITB_ROLE_AGENT_POLL_INTERVAL_SECONDS=0\n",
            "AGENT_ORG_STATE=unknown\n",
        ):
            with self.subTest(text=text), self.assertRaises(saihai_env.EnvError):
                parsed = saihai_env.parse_env(text)
                saihai_env.load_environment(checkout_root=Path("/tmp/missing"), environ=parsed)

    def test_path_keys_are_rejected_from_runtime_env(self) -> None:
        for key in ("AGENTS_VAULT_ROOT", "SKILLS_ROOT", "ITB_QUEUE_ROOT"):
            with self.subTest(key=key), self.assertRaisesRegex(saihai_env.EnvError, "unknown_or_path_key"):
                saihai_env.parse_env(f"{key}=/tmp/value\n")

    def test_rejects_unknown_export_duplicate_and_shell_expansion(self) -> None:
        cases = (
            "UNKNOWN=x\n",
            "export AGENT_ORG_STATE=enabled\n",
            "AGENT_ORG_STATE=enabled\nAGENT_ORG_STATE=disabled\n",
            "ITB_CODEX_MODEL=$(id)\n",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(saihai_env.EnvError):
                saihai_env.parse_env(text)

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
            env_file = self.write_env(primary, "AGENT_ORG_STATE=enabled\n")
            self.assertEqual(saihai_env.resolve_env_file(linked, {}), env_file.resolve())

    def test_diagnostics_do_not_contain_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_file = self.write_env(Path(raw), "ITB_CODEX_MODEL=private-value-must-not-leak\n")
            result = saihai_env.load_environment(environ={"SAIHAI_ENV_FILE": str(env_file)})
            self.assertNotIn("private-value-must-not-leak", repr(result))

    def test_setup_is_non_destructive_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            command = [sys.executable, str(ROOT / "scripts/setup_env.py"), "--env-file", str(env_file)]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            second = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(second.returncode, 0)
            checked = subprocess.run([*command, "--check"], capture_output=True, text=True, check=False)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_secret_bearing_environment_variants_remain_gitignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"))
            for relative in (".env", ".env.production", ".envrc", ".direnv/cache", "terraform.tfvars"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x")
                result = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", relative], check=False)
                self.assertEqual(result.returncode, 0, relative)


if __name__ == "__main__":
    unittest.main()
