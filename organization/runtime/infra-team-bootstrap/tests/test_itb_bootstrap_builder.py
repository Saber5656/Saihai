from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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

    def test_active_task_set_after_metadata_only_session_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            task_detail_path = state_root / "vault" / "TSK-9999-test" / "task.md"
            task_detail_path.parent.mkdir(parents=True)
            task_detail_path.write_text("# TSK-9999 Test\n", encoding="utf-8")
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "headless-session", "cwd": "/tmp/project", "source": "startup"},
            )

            output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "headless-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "pre_execution",
                },
            )

            session_dir = state_root / "headless-session"
            self.assertNotIn("decision", output)
            self.assertEqual(output["activeTask"]["result"], "active_task_set")
            self.assertTrue((session_dir / "active-task.json").exists())
            self.assertFalse((session_dir / "bootstrap.json").exists())
            self.assertFalse((session_dir / "status").exists())

    def test_provider_activate_after_metadata_only_session_start_uses_registry_roster(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            builder.session_start_metadata_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "headless-session", "cwd": "/tmp/project", "source": "startup"},
            )
            session_dir = state_root / "headless-session"
            self.assertFalse((session_dir / "bootstrap.json").exists())
            self.assertFalse((session_dir / "roster.json").exists())

            stdout = json.dumps(
                {
                    "type": "result",
                    "result": "activation ok",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "duration_api_ms": 3,
                    "model": "gpt-5.5",
                    "request_id": "req-test",
                    "session_id": "provider-session",
                    "num_turns": 1,
                }
            ) + "\n"
            completed = subprocess.CompletedProcess(args=["codex"], returncode=0, stdout=stdout, stderr="")

            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                return_value=completed,
            ) as run_mock:
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "headless-session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )

            self.assertNotIn("decision", output)
            self.assertEqual(output["activation"]["provider"], "openai")
            self.assertTrue(run_mock.called)
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            self.assertEqual(state["bootstrap_status"], "headless_metadata")
            self.assertEqual(state["readiness_scope"], "response_evidence")
            self.assertTrue(any(item.get("agent_id") == "tech-backend" for item in roster))
            self.assertTrue((session_dir / "invocation-evidence.jsonl").exists())

    def test_provider_activate_invalid_existing_roster_blocks_before_subprocess(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "headless-session"
            session_dir.mkdir(parents=True)
            (session_dir / "active-execution-context.json").write_text(
                json.dumps({"session_id": "headless-session", "active_execution_context_pointer_path": "x"}),
                encoding="utf-8",
            )
            (session_dir / "roster.json").write_text(json.dumps({"agent_id": "tech-backend"}), encoding="utf-8")

            with mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=AssertionError("provider must not run"),
            ):
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "headless-session", "agent_id": "tech-backend"},
                )

        self.assertEqual(output["decision"], "block")
        self.assertEqual(output["reason"], "roster.json is not a list")

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

    def test_final_response_guard_blocks_active_task_without_execution_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "s"
            session_dir.mkdir(parents=True)
            (session_dir / "active-execution-context.json").write_text(
                json.dumps(
                    {
                        "session_id": "s",
                        "active_execution_context": None,
                        "active_execution_context_pointer_path": str(session_dir / "active-execution-context.json"),
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "active-task.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "task_id": "TSK-9999",
                        "task_detail_path": "/tmp/task.md",
                        "flow_phase": "pre_final_response",
                    }
                ),
                encoding="utf-8",
            )

            output = self.run_builder(state_root, "final-response-guard", {"session_id": "s", "enforce_final_gate": True})

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["finalGate"]["verdict"], "block")
            self.assertEqual(output["finalGate"]["blockers"][0]["id"], "active_task_without_execution_context")

    def test_final_response_guard_resolves_relative_execution_context_under_session_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "s"
            session_dir.mkdir(parents=True)
            (session_dir / "active-execution-context.json").write_text(
                json.dumps(
                    {
                        "session_id": "s",
                        "active_execution_context": {"path": "execution_context.json"},
                        "active_execution_context_pointer_path": str(session_dir / "active-execution-context.json"),
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "execution_context.json").write_text(
                json.dumps({"context_type": "execution", "task_id": "TSK-9999", "blocking_level": "none"}),
                encoding="utf-8",
            )

            output = self.run_builder(state_root, "final-response-guard", {"session_id": "s"})

            self.assertEqual(output["finalGate"]["verdict"], "allow")
            self.assertEqual(output["finalResponseGuard"]["source"], "execution_context")
            self.assertEqual(output["finalResponseGuard"]["execution_context_path"], str(session_dir / "execution_context.json"))
            self.assertEqual(output["finalResponseGuard"]["execution_context_read_issue"], "")

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

    def test_merge_hook_settings_prunes_retired_wrappers(self) -> None:
        builder = load_builder_module()
        old_settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "/tmp/itb-prompt-preflight.sh"}]},
                ],
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "/tmp/itb-pretooluse-guard.sh"}]},
                ],
                "SessionEnd": [
                    {"hooks": [{"type": "command", "command": "/tmp/itb-session-end.sh"}]},
                ],
            }
        }

        merged, changes = builder.merge_hook_settings(old_settings, "codex", HOOK_ROOT, BUILDER)

        self.assertNotIn("UserPromptSubmit", merged["hooks"])
        self.assertNotIn("PreToolUse", merged["hooks"])
        self.assertNotIn("SessionEnd", merged["hooks"])
        self.assertEqual([item["event"] for item in builder.hook_event_specs("codex", HOOK_ROOT, BUILDER)], ["SessionStart", "Stop"])
        self.assertTrue(any(item["action"] == "remove_retired_itb_hook" for item in changes))

    def test_codex_exec_dispatch_blocks_git_prompt_for_non_git_role_before_subprocess(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(
                json.dumps(
                    [
                        {
                            "agent_id": "tech-backend",
                            "provider": "openai",
                            "execution_mode": "codex_exec",
                            "intended_model": "gpt-5.5",
                            "allowed_tools": ["Read", "Grep", "Glob"],
                            "git_operations_allowed": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=AssertionError("provider must not run"),
            ):
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "prompt": "Please run `git push origin main`.",
                    },
                )

        self.assertEqual(output["decision"], "block")
        self.assertIn("git operation forbidden", output["reason"])

    def test_claude_activation_command_omits_tools_when_no_tools_are_configured(self) -> None:
        builder = load_builder_module()
        command = builder.claude_activation_command(
            {"agent_id": "ad-hoc", "intended_model": "claude-sonnet-4-5"},
            "hello",
            "0.01",
        )

        self.assertNotIn("--tools", command)

    def test_claude_activation_command_passes_configured_allowed_tools(self) -> None:
        builder = load_builder_module()
        command = builder.claude_activation_command(
            {
                "agent_id": "tech-reviewer",
                "intended_model": "claude-sonnet-4-5",
                "allowed_tools": ["Read", "Grep", "Glob"],
            },
            "hello",
            "0.01",
        )

        self.assertIn("--tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")

    def test_final_gate_required_artifact_missing_status_path_or_evidence_blocks(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.json"
            artifact.write_text("{}", encoding="utf-8")
            cases = [
                {"id": "missing_status", "path": str(artifact), "evidence_marker": "reviewed"},
                {"id": "missing_path", "status": "complete", "evidence_marker": "reviewed"},
                {"id": "missing_file", "status": "complete", "path": str(Path(tmp) / "missing.json"), "evidence_marker": "reviewed"},
                {"id": "missing_evidence", "status": "complete", "path": str(artifact)},
            ]

            for case in cases:
                with self.subTest(case=case["id"]):
                    blockers = builder.final_gate_artifact_blockers({"required_artifacts": [case]})
                    self.assertTrue(blockers)

    def test_final_gate_required_artifact_complete_with_evidence_allows(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.json"
            artifact.write_text("{}", encoding="utf-8")

            blockers = builder.final_gate_artifact_blockers(
                {
                    "required_artifacts": [
                        {
                            "id": "done",
                            "status": "complete",
                            "path": str(artifact),
                            "evidence_marker": "reviewed",
                        }
                    ]
                }
            )

        self.assertEqual(blockers, [])



class RequirementLedgerTest(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder_module()

    def envelope(self):
        return {
            "envelope_version": "2", "source_type": "human_prompt",
            "original_request": "A and B", "intent_summary": "Both units",
            "desired_outcome": {"deliverables": ["A", "B"], "done_criteria": ["both"]},
            "scope": {"in": ["A", "B"], "out": ["unrelated"]},
            "approval_required": False, "workflow_mode": "strict_flow",
            "routing_hint": "teams-project-manager", "review_requirements": ["independent"],
            "vault_update_targets": ["Agents-Vault"], "scope_contract_version": 1,
            "requirements_version": "v1", "selection_requirements_version": "v1",
            "requirements": [{"requirement_id": "R-A", "text": "A"}, {"requirement_id": "R-B", "text": "B"}],
            "requirements_history": [], "selected_unit_id": "B",
            "task_units": [{"unit_id": key, "title": key, "main_team": "tech",
                "assignee": "tech-backend", "repository": "Saber5656/Saihai",
                "binding": {"status": "pending"}, "requirement_ids": ["R-" + key],
                "scope": {"in": [key], "out": ["unrelated " + key]},
                "deliverables": [key], "done_criteria": ["accept " + key],
                "allowed_paths": ["src/" + key + ".py"], "depends_on": []} for key in ["A", "B"]],
        }

    def scaffold(self, envelope, root, **extra):
        return self.builder.gtc_scaffold_output(runtime="codex", state_root=root / "state",
            hook_input={"session_id": "ledger-test", "vault_root": str(root / "vault"),
                "task_id": "TSK-9138", "gate_intake_envelope": envelope, **extra})

    def test_valid_ledger_selects_b(self):
        env = self.envelope()
        self.assertEqual(self.builder.gtc_missing_envelope_fields(env), [])
        self.assertEqual(self.builder.first_task_unit(env)["unit_id"], "B")

    def test_all_units_validated_before_any_vault_write_even_ready(self):
        for mutate in [lambda e: e["task_units"][0].pop("scope"),
                       lambda e: e["task_units"].append("invalid"),
                       lambda e: e["task_units"][1].update(unit_id="A"),
                       lambda e: e["task_units"][1].update(scope={"in": ["B"], "out": []})]:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as tmp:
                env = self.envelope(); mutate(env); root = Path(tmp)
                result = self.scaffold(env, root, status="ready")
                self.assertEqual(result["decision"], "block")
                self.assertFalse((root / "vault" / "01-Projects").exists())
                self.assertFalse((root / "vault" / "00-Inbox&Tasks").exists())

    def test_selection_and_version_fail_closed(self):
        for field, value in [("selected_unit_id", ""), ("selected_unit_id", "absent"),
                             ("selection_requirements_version", "old"), ("requirements_version", "")]:
            with self.subTest(field=field, value=value):
                env = self.envelope(); env[field] = value
                self.assertTrue(self.builder.gtc_missing_envelope_fields(env))
                self.assertEqual(self.builder.first_task_unit(env), {})

    def test_coverage_and_dependencies(self):
        mutations = [lambda e: e["requirements"].append({"requirement_id": "R-C", "text": "C"}),
            lambda e: e["task_units"][1].update(requirement_ids=["unknown"]),
            lambda e: e["task_units"][1].update(depends_on=["unknown"]),
            lambda e: e["task_units"][1].update(depends_on=["A"]),
            lambda e: e["task_units"][0].update(depends_on=["A"]),
            lambda e: e["requirements"].append({"requirement_id": "R-A", "text": "duplicate"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                env = self.envelope(); mutate(env)
                self.assertTrue(self.builder.gtc_missing_envelope_fields(env))

    def test_malformed_binding_and_unit_fields_are_rejected(self):
        for field, value in [("binding", {"status": []}), ("unit_id", []),
                             ("requirement_ids", [{}]), ("depends_on", [False]),
                             ("allowed_paths", "src/B.py")]:
            with self.subTest(field=field):
                env = self.envelope(); env["task_units"][1][field] = value
                self.assertTrue(self.builder.gtc_missing_envelope_fields(env))

    def test_snapshot_retains_history_additions_non_goals_and_dependency_pending(self):
        env = self.envelope()
        env["requirements_version"] = env["selection_requirements_version"] = "v3"
        env["requirements_history"] = [{"version": "v1", "requirements": ["A", "B"]},
            {"version": "v2", "operation": "add", "requirement": "C"},
            {"version": "v3", "operation": "replace", "requirement": "B"}]
        env["requirements"].append({"requirement_id": "R-C", "text": "C"})
        env["task_units"][0]["requirement_ids"].append("R-C")
        env["task_units"][0]["depends_on"] = ["B"]
        env["earlier_findings"] = ["finding-1"]
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.scaffold(env, Path(tmp))["gtcScaffold"]
            ledger = payload["requirement_ledger"]
            for field in ["requirements_history", "requirements", "task_units", "scope", "earlier_findings"]:
                self.assertEqual(ledger[field], env[field])
            self.assertEqual(ledger["unit_dispositions"]["A"], "dependency_pending")

    def test_non_json_history_is_structurally_blocked_before_writes(self):
        import datetime
        for value in [datetime.date(2026, 9, 5), {1: "numeric key"}, ("tuple",), float("nan")]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                env = self.envelope(); env["requirements_history"] = [{"observation": value}]
                self.assertIn("requirement_ledger.json_value", self.builder.gtc_missing_envelope_fields(env))
                result = self.scaffold(env, Path(tmp), status="ready")
                self.assertEqual(result["decision"], "block")
                self.assertFalse((Path(tmp) / "vault" / "01-Projects").exists())

    def test_initial_status_cannot_override_invalidity(self):
        self.assertEqual(self.builder.gtc_initial_status(self.envelope(), ["task_units"], {"status": "ready"}), "triage")

    def test_partial_repair_does_not_invent_units(self):
        raw = "routing_hint: teams-project-manager\nassignee: tech-backend"
        normalized, repaired, errors = self.builder.normalize_gate_entry_response(raw, "A and B")
        self.assertFalse(repaired)
        self.assertEqual(normalized, raw)
        self.assertTrue(errors)

    def test_real_scaffold_preserves_every_unit_without_claiming_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.envelope(); result = self.scaffold(env, Path(tmp))
            ledger = result["gtcScaffold"]["requirement_ledger"]
            self.assertEqual(ledger["integration_status"], "integration_pending")
            self.assertEqual(ledger["requirements_version"], "v1")
            self.assertEqual([u["unit_id"] for u in ledger["task_units"]], ["A", "B"])
            self.assertEqual(ledger["unit_dispositions"], {"A": "pending", "B": "selected"})
            self.assertEqual(ledger["task_units"][0]["binding"], {"status": "pending"})
            text = Path(result["gtcScaffold"]["task_detail_path"]).read_text()
            self.assertIn('"requirement_id": "R-A"', text)
            self.assertIn('| Scope In | B |', text)
            self.assertIn('| Done Criteria | accept B |', text)
            self.assertIn('integration_pending', text)

    def test_replacement_cannot_drop_prior_ledger_on_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); env = self.envelope()
            first = self.scaffold(env, root)
            detail = Path(first["gtcScaffold"]["task_detail_path"]); before = detail.read_bytes()
            env["requirements_version"] = env["selection_requirements_version"] = "v2"
            env["task_units"] = env["task_units"][1:]; env["requirements"] = env["requirements"][1:]
            second = self.scaffold(env, root, update_existing=True)
            self.assertEqual(second["decision"], "block")
            self.assertEqual(detail.read_bytes(), before)

if __name__ == "__main__":
    unittest.main()
