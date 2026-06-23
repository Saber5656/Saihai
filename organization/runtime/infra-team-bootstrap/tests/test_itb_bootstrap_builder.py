from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
BUILDER = SKILL_ROOT / "scripts" / "itb_bootstrap_builder.py"
HOOK_ROOT = SKILL_ROOT / "hooks"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("itb_bootstrap_builder_for_test", BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load ITB builder module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ItbHeadlessHookResetTest(unittest.TestCase):
    def run_builder(
        self,
        state_root: Path,
        command: str,
        hook_input: dict[str, object],
        runtime: str = "codex",
    ) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                str(BUILDER),
                command,
                "--runtime",
                runtime,
                "--state-root",
                str(state_root),
            ],
            input=json.dumps(hook_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(completed.stdout)

    def test_hook_event_specs_are_initial_two_hook_set(self) -> None:
        builder = load_builder_module()
        specs = builder.hook_event_specs("codex", HOOK_ROOT, BUILDER)

        self.assertEqual([item["event"] for item in specs], ["SessionStart", "Stop"])
        self.assertEqual([item["script"] for item in specs], ["itb-session-start.sh", "itb-final-response-guard.sh"])

    def test_session_start_writes_metadata_pointer_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            output = self.run_builder(
                state_root,
                "session-start",
                {"session_id": "headless-session", "cwd": "/tmp/project", "source": "startup"},
            )
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")

            session_dir = state_root / "headless-session"
            pointer = json.loads((session_dir / "active-execution-context.json").read_text(encoding="utf-8"))
            self.assertEqual(pointer["session_id"], "headless-session")
            self.assertEqual(pointer["runtime"], "codex")
            self.assertEqual(pointer["cwd"], "/tmp/project")
            self.assertIsNone(pointer["active_execution_context"])
            self.assertIn("active_execution_context_pointer_path", pointer)
            self.assertFalse((session_dir / "bootstrap.json").exists())
            self.assertFalse((session_dir / "roster.json").exists())

    def test_role_agent_rows_expose_headless_metadata_only(self) -> None:
        builder = load_builder_module()
        rows = builder.role_agent_rows(organization_instance_id="org-test")
        self.assertTrue(rows)
        sample = rows[0]
        self.assertIn("provider", sample)
        self.assertIn("execution_mode", sample)
        self.assertIn("inbox_path", sample)
        legacy_target_key = "t" + "mux_target"
        legacy_profile_key = "startup" + "_profile"
        self.assertNotIn(legacy_target_key, sample)
        self.assertNotIn(legacy_profile_key, sample)

    def test_transport_status_reports_cli_providers_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = self.run_builder(Path(tmp), "transport-status", {"session_id": "s"})
            status = output["transportStatus"]
            self.assertEqual(set(status["providers"]), {"claude_cli", "codex_exec"})

    def test_agent_surfaces_do_not_expose_process_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = self.run_builder(Path(tmp), "agent-surfaces", {"session_id": "s"})
            roles = output["agentSurfaces"]["roles"]
            self.assertTrue(roles)
            legacy_target_key = "t" + "mux_target"
            legacy_profile_key = "startup" + "_profile"
            self.assertTrue(all(legacy_target_key not in row for row in roles))
            self.assertTrue(all(legacy_profile_key not in row for row in roles))

    def test_final_response_guard_allows_without_active_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = self.run_builder(Path(tmp), "final-response-guard", {"session_id": "s"})
            self.assertEqual(output["finalResponseGuard"]["result"], "final_gate_allowed")
            self.assertEqual(output["finalGate"]["verdict"], "allow")
            self.assertEqual(output["finalGate"]["reason_code"], "no_active_context")

    def test_model_registry_no_longer_requires_process_columns(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "model-registry.md"
            registry.write_text(
                "\n".join(
                    [
                        "| agent_id | team | status | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | long_run_preferred | notes |",
                        "|---|---|---|---|---|---|---|---|---|---|---|---|",
                        "| tech-backend | tech | active | false | openai | gpt-5.5 |  | codex | medium | medium |  | test |",
                    ]
                ),
                encoding="utf-8",
            )
            rows = builder.parse_model_registry_file(registry)
            self.assertEqual(rows[0]["agent_id"], "tech-backend")

    def test_retired_hook_wrappers_are_not_shipped(self) -> None:
        shipped = {path.name for path in HOOK_ROOT.glob("*.sh")}
        self.assertEqual(shipped, {"itb-hook-common.sh", "itb-session-start.sh", "itb-final-response-guard.sh"})


if __name__ == "__main__":
    unittest.main()
