from __future__ import annotations

import hashlib
import json
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


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


class ItbBootstrapBuilderCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patcher = mock.patch.dict(os.environ, {"ITB_ROLE_QUEUE_ACTIVATE_PROVIDER": "0"}, clear=False)
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()

    def run_builder(
        self,
        state_root: Path,
        command: str,
        hook_input: dict[str, object],
        runtime: str = "codex",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        args = [
            sys.executable,
            str(BUILDER),
            command,
            "--runtime",
            runtime,
            "--state-root",
            str(state_root),
        ]
        if extra_args:
            args.extend(extra_args)
        completed = subprocess.run(
            args,
            input=json.dumps(hook_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        return json.loads(completed.stdout)

    def run_builder_without_input(
        self,
        state_root: Path,
        command: str,
        runtime: str = "codex",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        args = [
            sys.executable,
            str(BUILDER),
            command,
            "--runtime",
            runtime,
            "--state-root",
            str(state_root),
        ]
        if extra_args:
            args.extend(extra_args)
        completed = subprocess.run(
            args,
            input="",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        return json.loads(completed.stdout)

    def bootstrap(self, state_root: Path, session_id: str = "test-session") -> Path:
        output = self.run_builder(
            state_root,
            "session-start",
            {"session_id": session_id, "cwd": "/tmp", "source": "SessionStart"},
        )
        self.assertIn("hookSpecificOutput", output)
        return state_root / session_id

    def provider_ready_launch_result(self, agent_id: str = "gate-prompt-formatter") -> dict[str, object]:
        return {
            "agent_id": agent_id,
            "process_status": "process_ready",
            "launch_status": "already_running",
            "runtime_kind": "claude_tmux",
            "process_mode": "provider_cli",
            "provider_runtime_kind": "claude_tmux",
            "provider_status": "provider_process_ready",
            "tool_sidecar_status": "not_verified",
            "tmux_session": "itb-org-test",
            "tmux_window": agent_id,
            "tmux_target": f"itb-org-test:{agent_id}.0",
            "process_evidence_source": "tmux",
            "process_started_at": "2026-01-01T00:00:00+09:00",
            "launch_error": "",
            "tools": "Read,Grep,Glob,Bash",
        }

    def init_test_vault(self, root: Path) -> None:
        inbox = root / "00-Inbox&Tasks"
        inbox.mkdir(parents=True)
        (root / "01-Projects" / "AI-Agent-Organization").mkdir(parents=True)
        (inbox / "Task-Index.md").write_text(
            "---\ntype: task-index\n---\n\n# Task Index\n\n"
            "| Task ID | Title | Main Team | Assignee | Status | Detail |\n"
            "|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
        (inbox / "Kanban.md").write_text(
            "---\ntype: kanban\n---\n\n# Kanban\n\n"
            "## Inbox\n\n## Ready\n\n## In Progress\n\n## Review\n\n## Waiting Human\n\n## Done\n",
            encoding="utf-8",
        )

    def copy_gate_contract_lint_fixture(self, skills_root: Path) -> None:
        fixture_skills = {
            "gate-task-assessor": """---
name: gate-task-assessor
allowed-tools: Read, Grep, Glob
status: reference
---

# Gate Task Assessor

runtime resident agent としては使わず、compatibility reference として扱う。
""",
            "gate-task-guardian": """---
name: gate-task-guardian
allowed-tools: Read, Grep, Glob
status: reference
---

# Gate Task Guardian

runtime resident agent としては使わず、compatibility reference として扱う。
""",
            "gate-task-creator": """---
name: gate-task-creator
allowed-tools: Read, Grep, Glob, Bash
status: active
---

# Gate Task Creator

GTC uses gtc-scaffold as the builder command owner.
LLM が手書きしない task artifacts are created by the builder command.
""",
            "gate-task-evaluator": """---
name: gate-task-evaluator
allowed-tools: Read, Grep, Glob
status: active
---

# Gate Task Evaluator

## Thin Verdict Scope

GTE checks role-report の compact fields.
provider が手書きしない mechanical artifacts are owned by builder commands.
""",
            "gate-response-humanizer": """---
name: gate-response-humanizer
allowed-tools: Read, Grep, Glob
status: reference
---

# Gate Response Humanizer

Final response rendering follows `finalization-check` と `final-transport-render-check`.
""",
            "tech-tester": """---
name: tech-tester
allowed-tools: Read, Grep, Glob, Bash
status: active
---

# Tech Tester

Validation evidence requires `finalization-check` complete.
""",
            "git-publisher": """---
name: git-publisher
allowed-tools: Read, Grep, Glob, Bash
status: active
---

# Git Publisher

Publication requires `finalization-check` / `Completion Envelope`.
""",
            "teams-project-manager": """---
name: teams-project-manager
allowed-tools: Read, Grep, Glob, Write, Edit, Agent
status: active
---

# Teams Project Manager

`finalization-check` は complete にしない。
""",
            "tech-director": """---
name: tech-director
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, Agent
status: active
---

# Tech Director

Completion returns to `teams-project-manager` via structured Completion Report.
Completion status relies on `team-completion-check` command evidence.
""",
            "contents-director": """---
name: contents-director
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, Agent
status: active
---

# Contents Director

Completion returns to `teams-project-manager` via structured Completion Report.
Completion status relies on `team-completion-check` command evidence.
""",
            "business-director": """---
name: business-director
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, Agent
status: active
---

# Business Director

Completion returns to `teams-project-manager` via structured Completion Report.
Completion status relies on `team-completion-check` command evidence.
""",
        }
        for role_id, text in fixture_skills.items():
            target = skills_root / role_id / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        required_paths = [
            "infra-local-qa/SKILL.md",
            "infra-director/SKILL.md",
            "infra-team-bootstrap/config/role-agent-registry.yaml",
            "infra-team-bootstrap/references/model-registry.md",
            "infra-team-bootstrap/references/team-config.md",
            "infra-team-bootstrap/references/resident-organization-comprehensive-plan.md",
        ]
        atv_role_root = SKILL_ROOT.parents[1] / "roles"
        for relative in required_paths:
            if relative.endswith("/SKILL.md"):
                role_id = relative.split("/", 1)[0]
                source = atv_role_root / f"{role_id}.md"
            elif relative.startswith("infra-team-bootstrap/"):
                source = SKILL_ROOT / relative.removeprefix("infra-team-bootstrap/")
            else:
                source = SKILL_ROOT.parent / relative
            target = skills_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def sample_gate_intake_envelope(self, workflow_mode: str = "strict_flow") -> str:
        risk_tier = "low" if workflow_mode == "controlled_micro_flow" else "normal"
        return f"""envelope_version: "2"
source_type: human_prompt
original_request: |
  Please create a compact task.
intent_summary: "Create deterministic GTC scaffold"
desired_outcome:
  deliverables: ["Task Detail", "Project Manager Handoff"]
  done_criteria: ["Task is ready for teams-project-manager"]
scope:
  in: ["Create initial task artifacts"]
  out: ["Perform specialist implementation"]
approval_required: false
approval_reason: "none"
workflow_mode: {workflow_mode}
risk_tier: {risk_tier}
task_units:
  - unit_id: unit-1
    title: "Deterministic scaffold"
    main_team: infra
    assignee: infra-team-bootstrap
    priority: P1
    done_criteria: ["Artifacts exist"]
routing_hint: "teams-project-manager"
review_requirements: ["domain_review", "independent_review"]
vault_update_targets: ["Agents-Vault"]
missing_information: []
risks: []
handoff_notes:
  gate-task-creator: "Run gtc-scaffold"
"""

    def init_git_repo(self, repo_root: Path) -> None:
        repo_root.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_cli_records_hook_errors_for_invalid_input_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "prompt-preflight",
                    "--runtime",
                    "codex",
                    "--state-root",
                    str(state_root),
                ],
                input="{",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 1)
            output = json.loads(completed.stdout)
            self.assertEqual(output["decision"], "block")
            error_path = state_root / "unknown-session" / "hook-errors.jsonl"
            self.assertTrue(error_path.exists())
            event = json.loads(error_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["command"], "prompt-preflight")
            self.assertEqual(event["error_type"], "JSONDecodeError")
            self.assertIn("traceback", event)

            scoped = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "prompt-preflight",
                    "--runtime",
                    "codex",
                    "--state-root",
                    str(state_root),
                    "--session-id",
                    "target-session",
                ],
                input="{",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(scoped.returncode, 1)
            scoped_output = json.loads(scoped.stdout)
            self.assertEqual(scoped_output["hookError"]["session_id"], "target-session")
            scoped_error_path = state_root / "target-session" / "hook-errors.jsonl"
            self.assertTrue(scoped_error_path.exists())
            scoped_event = json.loads(scoped_error_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(scoped_event["command"], "prompt-preflight")
            self.assertEqual(scoped_event["error_type"], "JSONDecodeError")

    def write_task_detail(
        self,
        path: Path,
        *,
        pm_handoff: bool = True,
        pm_status: str = "sent_to_project_manager",
        routing: bool = True,
        director: str = "infra-director",
        guardian_complete: bool = False,
        completion_envelope: bool = False,
    ) -> None:
        pm_section = ""
        if pm_handoff:
            pm_section = f"""
## Project Manager Handoff

| Field | Value |
|---|---|
| Handoff To | teams-project-manager |
| Handoff Status | {pm_status} |
"""
        completion_chain = " -> ".join(load_builder_module().COMPLETION_CHAIN)
        routing_section = ""
        if routing:
            routing_section = f"""
## Team Routing Decision

| Field | Value |
|---|---|
| Handoff To Director | {director} |
| Completion Gate | {completion_chain} |
"""
        finalization_checked = "true" if guardian_complete else "false"
        finalization_status = "complete" if guardian_complete else "pending"
        vault_final = "complete" if guardian_complete else "pending"
        envelope_section = """
## Completion Envelope

| Field | Value |
|---|---|
| finalization_status_checked | true |
""" if completion_envelope else ""
        final_evidence_sections = """
## Team Completion Check

| Field | Value |
|---|---|
| Completion Status | ready_for_evaluation |
| All Director Reports Complete | true |
| Handoff To | gate-task-evaluator |

## Quality Evaluation

| Field | Value |
|---|---|
| Evaluation Status | quality_ok |

## Task Change Manifest

| Field | Value |
|---|---|
| task_id | TSK-9999 |

## Role Execution Evidence

| Role | Result | Usage Source |
|---|---|---|
| infra-director | complete | claude_tmux_interactive |

## Finalization Check

| Field | Value |
|---|---|
| Finalization Status | complete |
| GTE Final Review Complete | true |
| Vault Final Update | true |
| Handoff To | main_transport_renderer |

## Final Transport Render Check

| Field | Value |
|---|---|
| Renderer | main_transport_renderer |
| Source Envelope | Completion Envelope |
| Source Finalization Check | Finalization Check |
| Facts Preserved | true |
| No New Task Judgment | true |
| Worker Persona Leakage | false |
| Style Profile | user-interface-imouto |
| Safety Exception | false |

## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_print_json | complete |
| gate-task-creator | registry | claude-haiku-4-5 | req-gtc | sess-gtc | claude_print_json | complete |
| teams-project-manager | registry | claude-sonnet-4-6 | req-tpm | sess-tpm | claude_print_json | complete |
| gate-task-evaluator | registry | claude-sonnet-4-6 | req-gte | sess-gte | claude_print_json | complete |
""" if completion_envelope else ""
        path.write_text(
            f"""# TSK-9999 Test Task

## Execution Preflight

| Check | Status | Evidence |
|---|---|---|
| organization_instance_bootstrapped | true | test |
| gate_intake_envelope_created | true | test |
| task_detail_created_or_updated | true | test |
| task_index_synced | true | test |
| kanban_synced | true | test |
| project_manager_handoff_created | true | test |
| review_line_defined | true | test |
| team_roster_recorded | true | test |
| active_set_declared | true | test |
| queue_evidence_recorded | true | test |
{pm_section}
{routing_section}
## Completion Gate

| Field | Value |
|---|---|
| Finalization Status Checked | {finalization_checked} |
| Finalization Status | {finalization_status} |
| Vault Final Update | {vault_final} |
{envelope_section}
{final_evidence_sections}
""",
            encoding="utf-8",
        )

    def write_provider_activation_evidence(
        self,
        session_dir: Path,
        *,
        event_type: str = "provider_activation",
        usage_source: str = "claude_print_json",
    ) -> None:
        entries = [
            ("gate-prompt-formatter", "claude-sonnet-4-6", "req-gpf", "sess-gpf"),
            ("gate-task-creator", "claude-haiku-4-5", "req-gtc", "sess-gtc"),
            ("teams-project-manager", "claude-sonnet-4-6", "req-tpm", "sess-tpm"),
            ("gate-task-evaluator", "claude-sonnet-4-6", "req-gte", "sess-gte"),
        ]
        path = session_dir / "invocation-evidence.jsonl"
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        records = []
        for agent_id, model, request_id, provider_session_id in entries:
            records.append(
                json.dumps(
                    {
                        "ts": "2026-01-01T00:00:00+09:00",
                        "runtime": "codex",
                        "event_type": event_type,
                        "agent_id": agent_id,
                        "organization_instance_id": "org-test",
                        "session_id": "test-session",
                        "provider_session_id": provider_session_id,
                        "request_id": request_id,
                        "result": "provider_response_ready",
                        "effective_model": model,
                        "usage_source": usage_source,
                        "input_tokens": 1,
                        "output_tokens": 1,
                    },
                    ensure_ascii=False,
                )
            )
        path.write_text("\n".join(existing + records) + "\n", encoding="utf-8")

    def add_controlled_micro_flow_section(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "## Completion Gate",
            """## Controlled Micro-Flow

| Field | Value |
|---|---|
| Workflow Mode | controlled_micro_flow |
| Risk Tier | low |
| Organization Policy | preserved |
| Strict Flow Escalation Checked | true |
| Local Gate Evidence Allowed | true |
| External Provider Dispatch | not_required_for_micro_flow |
| Escalation Required | false |
| Escalation Triggers | none |

## Completion Gate""",
            1,
        )
        path.write_text(text, encoding="utf-8")

    def replace_invocation_evidence_with_local_micro_flow(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        marker = "## Invocation Evidence"
        start = text.find(marker)
        prefix = text[:start] if start >= 0 else text.rstrip() + "\n\n"
        agents = [
            "gate-prompt-formatter",
            "gate-task-creator",
            "teams-project-manager",
            "gate-task-evaluator",
        ]
        rows = "\n".join(
            f"| {agent_id} | local | codex-session | local-{agent_id} | test-session | local_controlled_micro_flow | local_controlled_micro_flow |"
            for agent_id in agents
        )
        path.write_text(
            prefix
            + f"""## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
{rows}
""",
            encoding="utf-8",
        )

    def test_session_resolver_prioritizes_hook_parent_env_then_last_session(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            (state_root / "last-session").write_text("last-session\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"ITB_PARENT_SESSION_ID": "parent-session"}, clear=False):
                self.assertEqual(
                    builder.resolve_session_id(state_root, {"sessionId": "hook-session"}),
                    ("hook-session", "hook_input"),
                )
                self.assertEqual(
                    builder.resolve_session_id(state_root, {}),
                    ("parent-session", "env:ITB_PARENT_SESSION_ID"),
                )

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    builder.resolve_session_id(state_root, {}),
                    ("last-session", "last-session"),
                )

    def test_state_commands_prefer_parent_session_env_when_hook_session_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            parent_dir = self.bootstrap(state_root, session_id="parent-session")
            last_dir = self.bootstrap(state_root, session_id="wrong-session")
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            env = os.environ.copy()
            env["ITB_PARENT_SESSION_ID"] = "parent-session"

            active_output = self.run_builder(
                state_root,
                "active-task",
                {
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
                env=env,
            )
            self.assertEqual(active_output["activeTask"]["session_id"], "parent-session")
            self.assertTrue((parent_dir / "active-task.json").exists())
            self.assertFalse((last_dir / "active-task.json").exists())

            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"prompt": "ok"},
                env=env,
            )
            self.assertIn("hookSpecificOutput", preflight_output)
            self.assertTrue((parent_dir / "preflight-events.jsonl").exists())
            self.assertFalse((last_dir / "preflight-events.jsonl").exists())

            role_queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-parent-session-submit",
                    "message_id": "msg-parent-submit",
                    "report_id": "rep-parent-submit",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
                env=env,
            )
            self.assertEqual(role_queue_output["roleQueue"]["result"], "queued")
            self.assertIn("/parent-session/", role_queue_output["roleQueue"]["inbox_path"])
            self.assertFalse((last_dir / "queue-events.jsonl").exists())

            (state_root / "last-session").write_text("missing-stale-session\n", encoding="utf-8")
            provider_output = self.run_builder(
                state_root,
                "provider-activate",
                {"agent_id": "no-such-agent"},
                env=env,
            )
            self.assertEqual(provider_output["decision"], "block")
            self.assertEqual(provider_output["reason"], "agent not found in roster: no-such-agent")

    def test_state_commands_prefer_cli_session_over_stale_last_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            target_dir = self.bootstrap(state_root, session_id="target-session")
            (state_root / "last-session").write_text("stale-last-session\n", encoding="utf-8")
            stale_dir = state_root / "stale-last-session"
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)

            active_output = self.run_builder(
                state_root,
                "active-task",
                {
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
                extra_args=["--session-id", "target-session"],
            )
            self.assertEqual(active_output["activeTask"]["session_id"], "target-session")
            self.assertTrue((target_dir / "active-task.json").exists())
            self.assertFalse((stale_dir / "active-task.json").exists())

            preflight_output = self.run_builder_without_input(
                state_root,
                "prompt-preflight",
                extra_args=["--session-id", "target-session"],
            )
            self.assertIn("hookSpecificOutput", preflight_output)
            self.assertTrue((target_dir / "preflight-events.jsonl").exists())
            self.assertFalse((stale_dir / "preflight-events.jsonl").exists())

            role_queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-target-session-cli",
                    "message_id": "msg-target-cli",
                    "report_id": "rep-target-cli",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--dry-run",
                ],
            )
            self.assertEqual(role_queue_output["roleQueue"]["role_id"], "gate-prompt-formatter")
            self.assertIn("target-session/queue", role_queue_output["roleQueue"]["inbox_path"])
            self.assertFalse((stale_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").exists())

            provider_output = self.run_builder_without_input(
                state_root,
                "provider-activate",
                extra_args=["--session-id", "target-session", "--role-id", "no-such-agent"],
            )
            self.assertEqual(provider_output["decision"], "block")
            self.assertEqual(provider_output["reason"], "agent not found in roster: no-such-agent")

            dispatch_output = self.run_builder(
                state_root,
                "agent-dispatch",
                {"prompt": "Dispatch using explicit CLI scope."},
                extra_args=["--session-id", "target-session", "--role-id", "no-such-agent"],
            )
            self.assertEqual(dispatch_output["decision"], "block")
            self.assertEqual(dispatch_output["reason"], "agent not found in roster: no-such-agent")

    def test_session_start_writes_metadata_ready_roster_and_invocation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()

            self.assertEqual(state["bootstrap_status"], "ready")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["resident_agents_registered"], state["resident_agents_total"])
            self.assertEqual(state["resident_agents_response_ready"], 0)
            self.assertEqual(state["resident_agents_ready"], state["resident_agents_registered"])
            self.assertGreater(len(roster), 0)
            self.assertNotIn("active", {row["activation_status"] for row in roster})
            self.assertTrue(
                any(
                    row["always_active"] and row["activation_status"] == "metadata_ready"
                    for row in roster
                )
            )
            for row in roster:
                self.assertEqual(row["metadata_status"], "metadata_ready")
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["effective_model"], "")
                self.assertEqual(row["session_id"], "")
                self.assertEqual(row["last_request_id"], "")
                self.assertEqual(row["usage_source"], "bootstrap_metadata_only")
            self.assertEqual(len(evidence), 1)
            self.assertEqual(json.loads(evidence[0])["result"], "metadata_ready_only")

    def test_role_agent_registry_derives_models_from_model_registry(self) -> None:
        builder = load_builder_module()

        rows = builder.role_agent_rows(organization_instance_id="org-test")
        by_role = {row["role_id"]: row for row in rows}
        registry_config = builder.load_role_agent_registry()

        self.assertIn("gate-prompt-formatter", by_role)
        self.assertIn("tech-backend", by_role)
        self.assertIn("git-publisher", by_role)
        self.assertNotIn("gate-task-assessor", by_role)
        self.assertNotIn("gate-task-guardian", by_role)
        self.assertEqual(by_role["gate-prompt-formatter"]["provider"], "anthropic")
        self.assertEqual(by_role["gate-prompt-formatter"]["role_layer"], "gate")
        self.assertEqual(by_role["teams-project-manager"]["role_layer"], "tpm")
        self.assertEqual(by_role["infra-director"]["role_layer"], "director")
        self.assertEqual(by_role["tech-backend"]["role_layer"], "worker")
        self.assertEqual(by_role["tech-reviewer"]["role_layer"], "worker")
        self.assertEqual(by_role["gate-prompt-formatter"]["intended_model"], "claude-sonnet-4-6")
        self.assertEqual(by_role["gate-task-creator"]["intended_model"], "claude-haiku-4-5")
        self.assertEqual(by_role["tech-backend"]["provider"], "openai")
        self.assertEqual(by_role["tech-backend"]["execution_mode"], "codex")
        self.assertEqual(by_role["tech-backend"]["tmux_target"], "itb-org-test:tech-backend.0")
        self.assertEqual(by_role["gate-prompt-formatter"]["queue_finalizer"], "role-report")
        self.assertEqual(by_role["gate-prompt-formatter"]["report_write_mode"], "builder_atomic")
        self.assertEqual(by_role["gate-prompt-formatter"]["allowed_tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(
            by_role["gate-task-creator"]["allowed_tools"],
            ["Read", "Grep", "Glob"],
        )
        self.assertEqual(by_role["gate-task-creator"]["startup_profile"], "lazy_activation")
        self.assertFalse(by_role["gate-task-creator"]["always_active"])
        self.assertEqual(by_role["gate-task-evaluator"]["allowed_tools"], ["Read", "Grep", "Glob"])
        self.assertFalse(by_role["gate-task-creator"]["git_operations_allowed"])
        self.assertFalse(by_role["infra-director"]["git_operations_allowed"])
        self.assertEqual(by_role["git-publisher"]["startup_profile"], "lazy_activation")
        self.assertEqual(by_role["git-publisher"]["allowed_tools"], ["Bash", "Read", "Grep"])
        self.assertTrue(by_role["git-publisher"]["git_operations_allowed"])
        self.assertEqual(by_role["gate-prompt-formatter"]["context_dirs"], [])
        self.assertEqual(by_role["infra-director"]["context_dirs"], [str(builder.AGENTS_VAULT_ROOT)])
        self.assertEqual(by_role["infra-local-qa"]["context_dirs"], [str(builder.AGENTS_VAULT_ROOT)])
        self.assertEqual(by_role["infra-task-dispatcher"]["context_dirs"], [str(builder.AGENTS_VAULT_ROOT)])
        self.assertNotIn("intended_model", json.dumps(registry_config))

    def test_completion_chain_config_loads_default_contract(self) -> None:
        builder = load_builder_module()

        config = builder.load_completion_chain_config()

        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(
            config["completion_chain"],
            [
                "team-completion-check",
                "gate-task-evaluator",
                "git-publisher",
                "vault_final_update",
                "finalization-check",
                "main_transport_renderer",
            ],
        )
        self.assertIn("team-completion-check", config["completion_gate_required_hops"])
        self.assertIn("finalization-check", config["completion_gate_required_hops"])
        self.assertIn("main_transport_renderer", config["completion_gate_required_hops"])
        self.assertIn("Final Transport Render Check", config["pre_final_required_sections"])
        self.assertIn("vault_final_update", config["valid_routing_directors"])
        self.assertIn("main_transport_renderer", config["valid_routing_directors"])
        self.assertIn("main-agent", config["main_agent_evidence_roles"])
        self.assertIn("Team Completion Check", config["pre_final_required_sections"])
        self.assertIn("Finalization Check", config["pre_final_required_sections"])
        self.assertNotIn("Completion Assessment", config["pre_final_required_sections"])
        self.assertEqual(config["assessor_integration_policy"]["mode"], "tpm_team_completion_check")
        self.assertEqual(
            [(item["from_role"], item["to_role"]) for item in config["auto_queue_handoffs"]],
            [
                ("gate-prompt-formatter", "teams-project-manager"),
                ("teams-project-manager", "gate-task-evaluator"),
                ("gate-task-evaluator", "git-publisher"),
                ("gate-task-evaluator", "vault_final_update"),
                ("git-publisher", "vault_final_update"),
            ],
        )
        self.assertEqual(config["auto_queue_handoffs"][0]["handoff_type"], "command_then_queue")
        self.assertEqual(config["auto_queue_handoffs"][0]["command"], "gtc-scaffold")
        self.assertEqual(config["auto_queue_handoffs"][0]["command_owner_role"], "gate-task-creator")
        self.assertTrue(config["auto_queue_handoffs"][0]["queue_after_command"])
        self.assertEqual(config["auto_queue_handoffs"][1]["precheck_command"], "team-completion-check")
        self.assertEqual(config["auto_queue_handoffs"][1]["command_flow_phase"], "post_routing")
        self.assertTrue(config["auto_queue_handoffs"][1]["require_next_phase_allowed"])
        self.assertEqual(config["auto_queue_handoffs"][2]["handoff_type"], "queue")
        self.assertEqual(config["auto_queue_handoffs"][2]["required_report_result"], "quality_ok")
        self.assertEqual(config["auto_queue_handoffs"][2]["required_handoff_to"], "git-publisher")
        self.assertEqual(config["auto_queue_handoffs"][2]["publication_gate_phase"], "manifest")
        self.assertEqual(config["auto_queue_handoffs"][3]["handoff_type"], "command")
        self.assertEqual(config["auto_queue_handoffs"][3]["command"], "vault-final-update")
        self.assertEqual(config["auto_queue_handoffs"][3]["required_report_result"], "quality_ok")
        self.assertEqual(config["auto_queue_handoffs"][3]["required_handoff_to"], "vault_final_update")
        self.assertTrue(config["auto_queue_handoffs"][3]["auto_finalization_check"])
        self.assertTrue(config["auto_queue_handoffs"][3]["auto_final_transport_render_check"])
        self.assertEqual(config["auto_queue_handoffs"][4]["handoff_type"], "command")
        self.assertEqual(config["auto_queue_handoffs"][4]["command"], "vault-final-update")
        self.assertEqual(config["auto_queue_handoffs"][4]["required_handoff_to"], "vault_final_update")
        self.assertTrue(config["auto_queue_handoffs"][4]["auto_finalization_check"])
        self.assertTrue(config["auto_queue_handoffs"][4]["auto_final_transport_render_check"])
        self.assertEqual(builder.COMPLETION_GATE_REQUIRED_HOPS, tuple(config["completion_gate_required_hops"]))
        self.assertEqual(builder.COMPLETION_CHAIN, tuple(config["completion_chain"]))
        self.assertEqual(builder.PRE_FINAL_REQUIRED_SECTIONS, tuple(config["pre_final_required_sections"]))
        self.assertEqual(builder.VALID_ROUTING_DIRECTORS, set(config["valid_routing_directors"]))
        self.assertEqual(builder.MAIN_AGENT_EVIDENCE_ROLES, set(config["main_agent_evidence_roles"]))
        self.assertEqual(builder.ASSESSOR_INTEGRATION_POLICY["mode"], "tpm_team_completion_check")
        self.assertEqual(config["gate_sla"]["role_pending_seconds"]["gate-prompt-formatter"], 120.0)
        self.assertEqual(config["gate_sla"]["hop_pending_seconds"]["gate-task-creator->teams-project-manager"], 120.0)
        self.assertEqual(config["gate_sla"]["breach_notification_class"], "flow_alert")
        self.assertEqual(builder.notification_class_for_event(status="ambiguous"), "flow_alert")
        self.assertEqual(builder.notification_class_for_event(result="completed_with_errors"), "flow_alert")
        self.assertEqual(builder.notification_class_for_event(result="done"), "done")

    def test_notification_dispatch_skips_silent_and_records_event(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(builder.subprocess, "run") as run:
            state_root = Path(tmp)
            output = builder.notification_dispatch_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "notify-session", "notification_class": "silent"},
            )

            summary = output["notificationDispatch"]
            self.assertEqual(summary["result"], "skipped_silent")
            run.assert_not_called()
            events = [
                json.loads(line)
                for line in (state_root / "notify-session" / "notification-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["result"], "skipped_silent")

    def test_notification_dispatch_dry_run_for_flow_alert(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(builder.subprocess, "run") as run:
            state_root = Path(tmp)
            output = builder.notification_dispatch_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "notify-dry-run",
                    "notification_class": "flow_alert",
                    "title": "Queue alert",
                    "body": "Pending task exceeded SLA",
                    "dry_run": True,
                },
            )

            summary = output["notificationDispatch"]
            self.assertEqual(summary["result"], "dry_run")
            self.assertEqual(summary["title"], "Queue alert")
            self.assertIn("display notification", summary["osascript"])
            run.assert_not_called()

    def test_notification_dispatch_cli_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            output = self.run_builder(
                state_root,
                "notification-dispatch",
                {
                    "session_id": "notify-cli",
                    "notification_class": "flow_alert",
                    "body": "CLI dry-run alert",
                },
                extra_args=["--dry-run"],
            )

            summary = output["notificationDispatch"]
            self.assertEqual(summary["result"], "dry_run")
            self.assertEqual(summary["body"], "CLI dry-run alert")

    def test_notification_dispatch_sends_when_enabled(self) -> None:
        builder = load_builder_module()
        completed = subprocess.CompletedProcess(["osascript"], 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder.shutil,
            "which",
            return_value="/usr/bin/osascript",
        ), mock.patch.object(
            builder.subprocess,
            "run",
            return_value=completed,
        ) as run:
            state_root = Path(tmp)
            output = builder.notification_dispatch_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "notify-send",
                    "notification_class": "flow_alert",
                    "event_type": "queue_sla_breach",
                    "task_id": "TSK-1263",
                    "body": "Queue SLA breach",
                    "enable_os_notification": True,
                },
            )

            summary = output["notificationDispatch"]
            self.assertEqual(summary["result"], "sent")
            command_args = run.call_args.args[0]
            self.assertEqual(command_args[0], "/usr/bin/osascript")
            self.assertEqual(command_args[1], "-e")
            self.assertIn("Queue SLA breach", command_args[2])
            self.assertIn("TSK-1263", command_args[2])

    def test_gate_output_schema_config_validates_pre_final_sections(self) -> None:
        builder = load_builder_module()

        config = builder.load_gate_output_schemas()

        self.assertEqual(config["schema_version"], 1)
        self.assertIn("Final Transport Render Check", config["sections"])
        self.assertIn("notification_class", config["sections"]["Finalization Check"]["fields"])
        final_render = {
            "Renderer": "main_transport_renderer",
            "Source Envelope": "Completion Envelope",
            "Source Finalization Check": "Finalization Check",
            "Facts Preserved": "true",
            "No New Task Judgment": "true",
            "Worker Persona Leakage": "false",
            "Style Profile": "plain",
        }
        self.assertEqual(builder.validate_gate_output_section_schema("Final Transport Render Check", final_render), [])

        final_render["Created By"] = "codex-main"
        errors = builder.validate_gate_output_section_schema("Final Transport Render Check", final_render)
        self.assertIn("Final Transport Render Check cannot be self-certified by main agent", errors)

    def test_completion_chain_config_loads_standard_yaml(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "completion-chain.yaml"
            config_path.write_text(
                """
schema_version: 1
completion_chain:
  - team-completion-check
  - gate-task-evaluator
  - git-publisher
  - vault_final_update
  - finalization-check
  - main_transport_renderer
completion_gate_required_hops:
  - team-completion-check
  - finalization-check
pre_final_required_sections:
  - Completion Envelope
  - Final Transport Render Check
valid_routing_directors:
  - teams-project-manager
main_agent_evidence_roles:
  - main-agent
main_agent_executor_roles:
  - main_agent
""",
                encoding="utf-8",
            )

            config = builder.load_completion_chain_config(config_path)

            self.assertEqual(config["schema_version"], 1)
            self.assertEqual(config["completion_chain"][0], "team-completion-check")
            self.assertEqual(config["pre_final_required_sections"], ["Completion Envelope", "Final Transport Render Check"])
            self.assertEqual(config["auto_queue_handoffs"], [])

    def test_completion_chain_config_rejects_invalid_gate_sla_seconds(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_config = Path(tmp) / "completion-chain.yaml"
            bad_config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "completion_chain": ["team-completion-check", "main_transport_renderer"],
                        "valid_routing_directors": ["teams-project-manager"],
                        "completion_gate_required_hops": ["team-completion-check"],
                        "pre_final_required_sections": ["Completion Envelope"],
                        "main_agent_evidence_roles": ["main-agent"],
                        "main_agent_executor_roles": ["main_agent"],
                        "gate_sla": {
                            "default_pending_seconds": 900,
                            "role_pending_seconds": {"gate-prompt-formatter": 0},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "gate_sla.role_pending_seconds.gate-prompt-formatter must be positive"):
                builder.load_completion_chain_config(bad_config)

    def test_completion_chain_config_rejects_invalid_required_list(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_config = Path(tmp) / "completion-chain.yaml"
            bad_config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "completion_chain": ["team-completion-check"],
                        "valid_routing_directors": [],
                        "completion_gate_required_hops": ["team-completion-check"],
                        "pre_final_required_sections": ["Completion Envelope"],
                        "main_agent_evidence_roles": ["main-agent"],
                        "main_agent_executor_roles": ["main_agent"],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "valid_routing_directors must be a non-empty list"):
                builder.load_completion_chain_config(bad_config)

    def test_completion_chain_config_rejects_required_hop_outside_chain(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_config = Path(tmp) / "completion-chain.yaml"
            bad_config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "completion_chain": ["team-completion-check"],
                        "valid_routing_directors": ["team-completion-check"],
                        "completion_gate_required_hops": ["finalization-check"],
                        "pre_final_required_sections": ["Completion Envelope"],
                        "main_agent_evidence_roles": ["main-agent"],
                        "main_agent_executor_roles": ["main_agent"],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "completion_gate_required_hops not present"):
                builder.load_completion_chain_config(bad_config)

    def test_role_agent_registry_blocks_invalid_model_registry_ref(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_registry = Path(tmp) / "role-agent-registry.yaml"
            bad_registry.write_text(
                json.dumps(
                    {
                        "defaults": {"tmux_session": "itb-{org_instance_id}", "role_layer": "worker"},
                        "agents": {"tech-lead": {"model_registry_ref": "missing-model-row"}},
                    }
                ),
                encoding="utf-8",
            )
            original_registry = builder.ROLE_AGENT_REGISTRY
            builder.ROLE_AGENT_REGISTRY = bad_registry
            try:
                with self.assertRaisesRegex(ValueError, "model_registry_ref not found"):
                    builder.role_agent_rows(organization_instance_id="org-test")
            finally:
                builder.ROLE_AGENT_REGISTRY = original_registry

    def test_role_agent_registry_loads_standard_yaml_with_comments(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "role-agent-registry.yaml"
            registry_path.write_text(
                """
registry_version: "1"
format: yaml # normal YAML is accepted, not only JSON-compatible YAML
defaults:
  tmux_session: itb-{org_instance_id}
  tmux_window: "{role_id}"
  tmux_pane: 0
  role_layer: worker
  queue_consumer: false
  inbox_path: inbox/{role_id}.yaml
  report_dir: reports/{role_id}
  queue_finalizer: role-report
  report_write_mode: builder_atomic
  allowed_tools:
    - Read
    - Grep
    - Glob
  git_operations_allowed: false
role_layers:
  gate-prompt-formatter: gate
agents:
  gate-prompt-formatter:
    model_registry_ref: gate-prompt-formatter
    queue_consumer: true
""",
                encoding="utf-8",
            )
            original_registry = builder.ROLE_AGENT_REGISTRY
            builder.ROLE_AGENT_REGISTRY = registry_path
            try:
                rows = builder.role_agent_rows(organization_instance_id="org-test")
            finally:
                builder.ROLE_AGENT_REGISTRY = original_registry

        by_role = {row["role_id"]: row for row in rows}
        self.assertEqual(by_role["gate-prompt-formatter"]["tmux_target"], "itb-org-test:gate-prompt-formatter.0")
        self.assertEqual(by_role["gate-prompt-formatter"]["role_layer"], "gate")
        self.assertEqual(by_role["gate-prompt-formatter"]["allowed_tools"], ["Read", "Grep", "Glob"])
        self.assertFalse(by_role["gate-prompt-formatter"]["git_operations_allowed"])

    def test_role_agent_registry_requires_role_layer_metadata(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_registry = Path(tmp) / "role-agent-registry.yaml"
            bad_registry.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "tmux_session": "itb-{org_instance_id}",
                            "tmux_window": "{role_id}",
                            "tmux_pane": 0,
                            "inbox_path": "inbox/{role_id}.yaml",
                            "report_dir": "reports/{role_id}",
                            "queue_finalizer": "role-report",
                            "report_write_mode": "builder_atomic",
                            "allowed_tools": ["Read", "Grep", "Glob"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            model_registry = Path(tmp) / "model-registry.md"
            model_registry.write_text(
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| active-role | test | active | true | true | codex | gpt-5 |  | codex | normal | high | provider_cli | false | test |\n",
                encoding="utf-8",
            )
            original_registry = builder.ROLE_AGENT_REGISTRY
            original_model_registry = builder.MODEL_REGISTRY
            builder.ROLE_AGENT_REGISTRY = bad_registry
            builder.MODEL_REGISTRY = model_registry
            try:
                with self.assertRaisesRegex(ValueError, "role_layer missing/invalid"):
                    builder.role_agent_rows(organization_instance_id="org-test")
            finally:
                builder.ROLE_AGENT_REGISTRY = original_registry
                builder.MODEL_REGISTRY = original_model_registry

    def test_role_agent_registry_requires_queue_finalizer_metadata(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_registry = Path(tmp) / "role-agent-registry.yaml"
            bad_registry.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "tmux_session": "itb-{org_instance_id}",
                            "tmux_window": "{role_id}",
                            "tmux_pane": 0,
                            "role_layer": "worker",
                            "inbox_path": "inbox/{role_id}.yaml",
                            "report_dir": "reports/{role_id}",
                        },
                        "agents": {
                            "infra-director": {
                                "model_registry_ref": "infra-director",
                                "queue_consumer": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_registry = builder.ROLE_AGENT_REGISTRY
            builder.ROLE_AGENT_REGISTRY = bad_registry
            try:
                with self.assertRaisesRegex(ValueError, "queue_finalizer invalid"):
                    builder.role_agent_rows(organization_instance_id="org-test")
            finally:
                builder.ROLE_AGENT_REGISTRY = original_registry

    def test_role_agent_registry_rejects_allowed_tools_mismatch(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = Path(tmp) / "skills"
            skill_dir = skills_root / "active-role"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: active-role\n"
                "allowed-tools: Read, Grep, Glob\n"
                "---\n",
                encoding="utf-8",
            )
            bad_registry = Path(tmp) / "role-agent-registry.yaml"
            bad_registry.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "tmux_session": "itb-{org_instance_id}",
                            "tmux_window": "{role_id}",
                            "tmux_pane": 0,
                            "role_layer": "worker",
                            "inbox_path": "inbox/{role_id}.yaml",
                            "report_dir": "reports/{role_id}",
                            "queue_finalizer": "role-report",
                            "report_write_mode": "builder_atomic",
                            "allowed_tools": ["Read"],
                        },
                        "agents": {
                            "active-role": {
                                "model_registry_ref": "active-role",
                                "queue_consumer": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            model_registry = Path(tmp) / "model-registry.md"
            model_registry.write_text(
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| active-role | test | active | true | true | codex | gpt-5 |  | codex-cli | normal | high | provider_cli | false | test |\n",
                encoding="utf-8",
            )
            original_skills_root = builder.SKILLS_ROOT
            original_registry = builder.ROLE_AGENT_REGISTRY
            original_model_registry = builder.MODEL_REGISTRY
            builder.SKILLS_ROOT = skills_root
            builder.ROLE_AGENT_REGISTRY = bad_registry
            builder.MODEL_REGISTRY = model_registry
            try:
                with self.assertRaisesRegex(ValueError, "allowed_tools mismatch"):
                    builder.role_agent_rows(organization_instance_id="org-test")
            finally:
                builder.SKILLS_ROOT = original_skills_root
                builder.ROLE_AGENT_REGISTRY = original_registry
                builder.MODEL_REGISTRY = original_model_registry

    def test_agent_surfaces_lists_role_layers_and_assignment_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            output = self.run_builder(
                state_root,
                "agent-surfaces",
                {"session_id": "surface-session"},
            )

            surfaces = output["agentSurfaces"]
            by_role = {item["role_id"]: item for item in surfaces["roles"]}
            self.assertEqual(output["decision"], "ok")
            self.assertEqual(by_role["teams-project-manager"]["role_layer"], "tpm")
            self.assertEqual(by_role["tech-director"]["role_layer"], "director")
            self.assertEqual(by_role["tech-backend"]["role_layer"], "worker")
            self.assertTrue(by_role["tech-backend"]["agent_call_supported"])
            self.assertIn("reviewer", by_role["tech-backend"]["assignment_roles"])
            self.assertEqual(by_role["teams-project-manager"]["assignment_roles"], [])

    def test_agent_call_blocks_director_to_worker_without_assignment_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "agent-call",
                {
                    "session_id": "test-session",
                    "from_role": "tech-director",
                    "to_role": "tech-backend",
                    "task_id": "TSK-1290",
                    "instruction": "Implement the approved runtime facade.",
                    "expected_output": "implementation_report",
                },
                extra_args=["--dry-run"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("requires assignment_role", output["reason"])

    def test_agent_call_queues_manifest_with_assignment_role_and_context_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "agent-call",
                {
                    "session_id": "test-session",
                    "from_role": "tech-director",
                    "to_role": "tech-backend",
                    "assignment_role": "implementer",
                    "task_id": "TSK-1290",
                    "message_id": "msg-agent-call",
                    "instruction": "Implement the approved runtime facade.",
                    "expected_output": "implementation_report",
                    "context_refs": [{"type": "task", "path": "task.md"}],
                },
                extra_args=["--dry-run"],
            )

            receipt = output["agentCall"]
            self.assertEqual(output["decision"], "ok")
            self.assertEqual(receipt["result"], "queued")
            self.assertEqual(receipt["to_role"], "tech-backend")
            self.assertEqual(receipt["role_layer"], "worker")
            self.assertEqual(receipt["assignment_role"], "implementer")
            self.assertEqual(receipt["message_id"], "msg-agent-call")
            self.assertEqual(receipt["nudge_status"], "dry_run")
            self.assertIn({"type": "task", "path": "task.md"}, receipt["context_refs"])
            self.assertTrue(
                any(ref["path"] == "organization/runtime/agent-call-contract.md" for ref in receipt["context_refs"])
            )
            inbox = json.loads(Path(receipt["inbox_path"]).read_text(encoding="utf-8"))
            message = inbox["messages"][0]
            self.assertEqual(message["payload"]["type"], "agent_call")
            self.assertEqual(message["payload"]["assignment_role"], "implementer")
            self.assertEqual(message["payload"]["role_layer"], "worker")
            self.assertEqual(message["payload"]["context_refs"], receipt["context_refs"])

    def test_agent_call_rejects_provider_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "agent-call",
                {
                    "session_id": "test-session",
                    "from_role": "tech-director",
                    "to_role": "tech-backend",
                    "assignment_role": "implementer",
                    "task_id": "TSK-1290",
                    "instruction": "Implement the approved runtime facade.",
                    "expected_output": "implementation_report",
                    "to": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                },
                extra_args=["--dry-run"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("does not accept provider/model override", output["reason"])

    def test_agent_switch_updates_session_roster_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "agent-switch",
                {
                    "session_id": "test-session",
                    "target_role": "tech-backend",
                    "reason": "anthropic_capacity_test",
                    "to": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                },
            )

            switch = output["agentSwitch"]
            self.assertEqual(output["decision"], "ok")
            self.assertEqual(switch["result"], "session_roster_updated")
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            tech_backend = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(tech_backend["provider"], "anthropic")
            self.assertEqual(tech_backend["intended_model"], "claude-sonnet-4-6")
            self.assertEqual(tech_backend["execution_mode"], "agent")
            events = [
                json.loads(line)
                for line in (session_dir / "provider-switch-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["target_role"], "tech-backend")

    def test_agent_switch_blocks_when_target_has_pending_queue_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "agent-call",
                {
                    "session_id": "test-session",
                    "from_role": "tech-director",
                    "to_role": "tech-backend",
                    "assignment_role": "implementer",
                    "task_id": "TSK-1290",
                    "message_id": "msg-pending",
                    "instruction": "Implement the approved runtime facade.",
                    "expected_output": "implementation_report",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "agent-switch",
                {
                    "session_id": "test-session",
                    "target_role": "tech-backend",
                    "reason": "weekly_limit",
                    "to": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                },
                extra_args=["--dry-run"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["agentSwitch"]["result"], "blocked_pending_or_processing")
            self.assertEqual(output["agentSwitch"]["active_message_ids"], ["msg-pending"])

    def test_role_queue_enqueue_writes_inbox_task_payload_and_dry_run_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "unit_id": "unit-1",
                    "message_id": "msg-fixed",
                    "instruction": "Review the registry design.",
                    "context_ref": "task.md",
                },
                extra_args=["--dry-run"],
            )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued")
            self.assertEqual(summary["nudge"]["result"], "dry_run")
            self.assertIn("[ITB_QUEUE_MESSAGE_READY]", summary["nudge"]["prompt_preview"])
            self.assertIn("nudge_manifest_path:", summary["nudge"]["prompt_preview"])
            self.assertIn("instruction_ref:", summary["nudge"]["prompt_preview"])
            self.assertIn("inbox_path:", summary["nudge"]["prompt_preview"])
            self.assertIn("report_ref:", summary["nudge"]["prompt_preview"])
            self.assertIn("atomic_report_writer:", summary["nudge"]["prompt_preview"])
            inbox_path = Path(summary["inbox_path"])
            task_payload_path = Path(summary["task_payload_path"])
            nudge_manifest_path = Path(summary["nudge"]["nudge_manifest_path"])
            self.assertTrue(inbox_path.exists())
            self.assertTrue(task_payload_path.exists())
            self.assertTrue(nudge_manifest_path.exists())
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            nudge_manifest = json.loads(nudge_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["role_id"], "tech-lead")
            self.assertEqual(len(inbox["messages"]), 1)
            message = inbox["messages"][0]
            self.assertEqual(message["message_id"], "msg-fixed")
            self.assertEqual(message["status"], "pending")
            self.assertEqual(message["payload"]["instruction_ref"], "tasks/TSK-9999/msg-fixed.yaml")
            self.assertEqual(nudge_manifest["instruction_ref"], "tasks/TSK-9999/msg-fixed.yaml")
            self.assertTrue(nudge_manifest["report_ref"].startswith("reports/tech-lead/TSK-9999/rep-"))
            payload = json.loads(task_payload_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["instruction"], "Review the registry design.")
            event = json.loads((session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["event_type"], "role_queue")
            self.assertEqual(event["result"], "queued")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            queued_metric = metrics[-1]
            self.assertEqual(queued_metric["event_type"], "queued")
            self.assertEqual(queued_metric["from_role"], "infra-director")
            self.assertEqual(queued_metric["to_role"], "tech-lead")
            self.assertEqual(queued_metric["hop_key"], "infra-director->tech-lead")

    def test_provider_usage_metric_fields_reads_transcript_path_fallback(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "provider-transcript.jsonl"
            transcript_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "message", "message": {"usage": {"input_tokens": 10}}}),
                        json.dumps(
                            {
                                "type": "result",
                                "usage": {"input_tokens": 55, "output_tokens": 13},
                                "duration_api_ms": 777,
                                "num_turns": 2,
                            }
                        ),
                        json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 9999}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            fields = builder.provider_usage_metric_fields(
                {
                    "transcript_path": str(transcript_path),
                    "input_tokens": 9,
                }
            )

            self.assertEqual(fields["input_tokens"], 9)
            self.assertEqual(fields["output_tokens"], 13)
            self.assertEqual(fields["total_tokens"], 22)
            self.assertEqual(fields["duration_api_ms"], 777)
            self.assertEqual(fields["turn_duration_ms"], 9999)
            self.assertEqual(fields["num_turns"], 2)

    def test_provider_usage_metric_fields_reads_transcript_when_only_turn_duration_missing(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "provider-transcript.jsonl"
            transcript_path.write_text(
                json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 8888}) + "\n",
                encoding="utf-8",
            )

            fields = builder.provider_usage_metric_fields(
                {
                    "transcript_path": str(transcript_path),
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "duration_api_ms": 777,
                    "num_turns": 3,
                }
            )

            self.assertEqual(fields["input_tokens"], 10)
            self.assertEqual(fields["output_tokens"], 20)
            self.assertEqual(fields["total_tokens"], 30)
            self.assertEqual(fields["duration_api_ms"], 777)
            self.assertEqual(fields["turn_duration_ms"], 8888)
            self.assertEqual(fields["num_turns"], 3)

    def test_role_report_finalizes_report_and_inbox_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-finalize",
                    "instruction": "Review the registry design.",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "message_id": "msg-finalize",
                    "status": "done",
                    "result": "review_complete",
                    "summary": "Reviewed registry design.",
                    "files_changed": ["skills/infra-team-bootstrap/config/role-agent-registry.yaml"],
                    "validation": {"review_evidence": "present"},
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:tech-lead.0",
                        "request_id": "req-finalize",
                        "effective_model": "claude-opus-4-8",
                        "usage_source": "claude_tmux_interactive",
                        "input_tokens": 123,
                        "output_tokens": 45,
                        "duration_api_ms": 678,
                        "num_turns": 2,
                    },
                    "report_extra": {"review_status": "approved"},
                },
            )

            summary = output["roleReport"]
            self.assertEqual(summary["result"], "done")
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "done")
            self.assertEqual(inbox["messages"][0]["report_path"], summary["report_ref"])
            report_path = Path(summary["report_path"])
            report_bytes = report_path.read_bytes()
            report = json.loads(report_bytes.decode("utf-8"))
            self.assertEqual(report["result"], "review_complete")
            self.assertEqual(report["validation"]["artifact_writer"], "itb_atomic_queue_writer")
            self.assertEqual(report["validation"]["inbox_status_updated_by"], "role-report")
            self.assertEqual(report["review_status"], "approved")
            self.assertEqual(report["schema_validation"]["status"], "valid")
            self.assertEqual(summary["report_integrity"]["sha256"], hashlib.sha256(report_bytes).hexdigest())
            self.assertEqual(inbox["messages"][0]["report_sha256"], summary["report_integrity"]["sha256"])
            self.assertEqual(inbox["messages"][0]["report_line_count"], summary["report_integrity"]["line_count"])
            self.assertGreater(summary["report_integrity"]["byte_count"], 0)
            metrics = [
                json.loads(line)
                for line in (state_root / "test-session" / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            finalized_metric = [item for item in metrics if item["event_type"] == "finalized"][-1]
            self.assertEqual(finalized_metric["from_role"], "infra-director")
            self.assertEqual(finalized_metric["to_role"], "tech-lead")
            self.assertEqual(finalized_metric["hop_key"], "infra-director->tech-lead")
            self.assertEqual(finalized_metric["result"], "done")
            self.assertEqual(finalized_metric["input_tokens"], 123)
            self.assertEqual(finalized_metric["output_tokens"], 45)
            self.assertEqual(finalized_metric["total_tokens"], 168)
            self.assertEqual(finalized_metric["duration_api_ms"], 678)
            self.assertEqual(finalized_metric["num_turns"], 2)
            self.assertEqual(finalized_metric["effective_model"], "claude-opus-4-8")

    def test_role_report_honors_message_payload_skip_auto_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-test-session-skip-handoff",
                    "message_id": "msg-skip-handoff",
                    "report_id": "rep-skip-handoff",
                    "instruction": "Live validation only.",
                    "expected_output": "gate_intake_envelope",
                    "skip_auto_queue_handoff": True,
                },
                extra_args=["--dry-run"],
            )
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertTrue(inbox["messages"][0]["payload"]["skip_auto_queue_handoff"])

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-skip-handoff",
                    "status": "done",
                    "result": "gate_intake_envelope_created",
                    "summary": "Validation envelope written.",
                    "gate_intake_envelope": "{\"original_request\":\"ping\"}",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "request_id": "req-skip-handoff",
                        "effective_model": "claude-sonnet-4-20250514",
                        "usage_source": "provider_authored_role_report",
                        "transcript_path": "/tmp/transcript.jsonl",
                    },
                },
            )

            summary = output["roleReport"]
            self.assertEqual(summary["result"], "done")
            self.assertEqual(summary["auto_handoff"]["result"], "skipped_by_input")
            tpm_inbox = state_root / "test-session" / "queue" / "inbox" / "teams-project-manager.yaml"
            self.assertFalse(tpm_inbox.exists())

    def test_role_report_auto_handoff_uses_queue_payload_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            malicious_vault_root = Path(tmp) / "malicious-vault"
            self.init_test_vault(vault_root)
            self.bootstrap(state_root)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-test-session-temp-vault",
                    "message_id": "msg-temp-vault",
                    "report_id": "rep-temp-vault",
                    "instruction": "Create a Gate Intake Envelope.",
                    "expected_output": "gate_intake_envelope",
                    "vault_root": str(vault_root),
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["payload"]["auto_handoff_context"]["vault_root"], str(vault_root))

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-temp-vault",
                    "status": "done",
                    "result": "gate_intake_envelope_created",
                    "summary": "Validation envelope written.",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                    "vault_root": str(malicious_vault_root),
                    "auto_chain_dry_run": True,
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "request_id": "req-temp-vault",
                        "effective_model": "claude-sonnet-4-20250514",
                        "usage_source": "provider_authored_role_report",
                    },
                },
            )

            auto_handoff = output["roleReport"]["auto_handoff"]
            self.assertEqual(auto_handoff["result"], "queued_nudge_unconfirmed")
            self.assertEqual(auto_handoff["command"]["command"], "gtc-scaffold")
            scaffold = auto_handoff["command"]["command_output"]["gtcScaffold"]
            self.assertTrue(Path(scaffold["task_detail_path"]).is_relative_to(vault_root))
            self.assertTrue((vault_root / "00-Inbox&Tasks" / "Task-Index.md").read_text(encoding="utf-8").count(scaffold["task_id"]) >= 1)
            task_text = Path(scaffold["task_detail_path"]).read_text(encoding="utf-8")
            self.assertIn("## Team Routing Decision", task_text)
            self.assertIn("| Handoff To Director | infra-director |", task_text)
            self.assertFalse((state_root / "00-Inbox&Tasks" / "Task-Index.md").exists())
            self.assertFalse((malicious_vault_root / "00-Inbox&Tasks" / "Task-Index.md").exists())
            tpm_inbox = json.loads(
                (state_root / "test-session" / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8")
            )
            tpm_message = tpm_inbox["messages"][0]
            self.assertEqual(
                tpm_message["payload"]["auto_handoff_context"]["vault_root"],
                str(vault_root),
            )
            self.assertEqual(tpm_message["payload"]["context_ref"], scaffold["task_detail_path"])
            malicious_task_detail_path = Path(tmp) / "malicious-task" / "TSK-0001" / "task.md"
            malicious_task_detail_path.parent.mkdir(parents=True)
            malicious_task_detail_path.write_text(
                """# TSK-0001 Wrong Task

## Team Completion Check

| Field | Value |
|---|---|
| Completion Status | pending |
| All Director Reports Complete | false |
| Handoff To | gate-task-evaluator |
""",
                encoding="utf-8",
            )

            tpm_output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "message_id": tpm_message["message_id"],
                    "status": "done",
                    "result": "ready_for_evaluation",
                    "summary": "All director reports are complete; hand off to evaluation.",
                    "task_detail_path": str(malicious_task_detail_path),
                    "report_extra": {
                        "team_completion_check": {
                            "all_director_reports_complete": True,
                            "completed_teams": ["infra"],
                        }
                    },
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:teams-project-manager.0",
                        "request_id": "req-temp-vault-tpm",
                        "effective_model": "claude-sonnet-4-20250514",
                        "usage_source": "provider_authored_role_report",
                    },
                },
            )

            tpm_report = tpm_output["roleReport"]
            self.assertEqual(tpm_report["team_completion_update"]["result"], "updated")
            self.assertEqual(tpm_report["team_completion_update"]["path_source"], "payload.auto_handoff_context.context_ref")
            self.assertEqual(tpm_report["auto_handoff"]["precheck"]["result"], "passed")
            self.assertEqual(tpm_report["auto_handoff"]["precheck"]["command_output"]["gateCommand"]["status"], "pass")
            self.assertIn(tpm_report["auto_handoff"]["result"], {"queued", "queued_nudge_unconfirmed"})
            updated_task_text = Path(scaffold["task_detail_path"]).read_text(encoding="utf-8")
            self.assertIn("| Completion Status | ready_for_evaluation |", updated_task_text)
            self.assertIn("| All Director Reports Complete | true |", updated_task_text)
            self.assertIn("| Handoff To | gate-task-evaluator |", updated_task_text)
            self.assertIn("| Completion Status | pending |", malicious_task_detail_path.read_text(encoding="utf-8"))
            evaluator_inbox = json.loads(
                (state_root / "test-session" / "queue" / "inbox" / "gate-task-evaluator.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(evaluator_inbox["messages"][0]["from_role"], "teams-project-manager")
            self.assertEqual(evaluator_inbox["messages"][0]["to_role"], "gate-task-evaluator")
            self.assertEqual(evaluator_inbox["messages"][0]["payload"]["precheck_result"]["result"], "passed")

    def test_prompt_submit_chain_id_reaches_gtc_metric_and_latency_report(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            self.bootstrap(state_root)
            entry_task_id = "ENTRY-test-session-chain"

            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": entry_task_id,
                    "message_id": "msg-chain",
                    "report_id": "rep-chain",
                    "instruction": "Create a Gate Intake Envelope.",
                    "expected_output": "gate_intake_envelope",
                    "vault_root": str(vault_root),
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            queued_payload = inbox["messages"][0]["payload"]
            self.assertEqual(queued_payload["prompt_submit_chain_id"], entry_task_id)
            self.assertEqual(
                queued_payload["auto_handoff_context"]["prompt_submit_chain_id"],
                entry_task_id,
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-chain",
                    "status": "done",
                    "result": "gate_intake_envelope_created",
                    "summary": "Validation envelope written.",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "request_id": "req-chain",
                        "effective_model": "claude-sonnet-4-20250514",
                        "usage_source": "provider_authored_role_report",
                        "duration_sec": 0.8,
                    },
                },
            )

            auto_handoff = output["roleReport"]["auto_handoff"]
            scaffold = auto_handoff["command"]["command_output"]["gtcScaffold"]
            self.assertEqual(output["roleReport"]["prompt_submit_chain_id"], entry_task_id)
            self.assertEqual(scaffold["prompt_submit_chain_id"], entry_task_id)

            tpm_inbox = json.loads(
                (state_root / "test-session" / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8")
            )
            tpm_payload = tpm_inbox["messages"][0]["payload"]
            self.assertEqual(tpm_payload["prompt_submit_chain_id"], entry_task_id)
            self.assertEqual(tpm_payload["auto_handoff_context"]["prompt_submit_chain_id"], entry_task_id)

            metrics_path = state_root / "test-session" / "gate-metrics.jsonl"
            metrics = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
            gpf_metric = [
                item
                for item in metrics
                if item["event_type"] == "finalized" and item["role_id"] == "gate-prompt-formatter"
            ][-1]
            gtc_metric = [
                item
                for item in metrics
                if item["event_type"] == "finalized"
                and item["role_id"] == "gate-task-creator"
                and item.get("usage_source") == "builder_command"
            ][-1]
            self.assertEqual(gpf_metric["prompt_submit_chain_id"], entry_task_id)
            self.assertEqual(gtc_metric["prompt_submit_chain_id"], entry_task_id)

            latency = builder.gate_latency_report_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "test-session", "metrics_path": str(metrics_path)},
            )
            prompt_submit = latency["gateLatencyComparison"]["prompt_submit_comparison"]
            self.assertEqual(prompt_submit["status"], "ready")
            self.assertEqual(prompt_submit["chain_count"], 1)
            self.assertEqual(prompt_submit["deterministic_total_sample_count"], 1)
            self.assertEqual(prompt_submit["deterministic_samples"][0]["chain_id"], entry_task_id)

    def test_synthetic_gpf_to_gte_no_publication_chain_reaches_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)

            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-test-session-full-chain",
                    "message_id": "msg-full-chain-gpf",
                    "instruction": "Create a Gate Intake Envelope.",
                    "expected_output": "gate_intake_envelope",
                    "vault_root": str(vault_root),
                    "defer_nudge": True,
                },
            )

            gpf_output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-full-chain-gpf",
                    "status": "done",
                    "result": "gate_intake_envelope_created",
                    "summary": "Validation envelope written.",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                    "provider_evidence": {
                        "provider_session_id": "sess-gpf",
                        "request_id": "req-gpf",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_print_json",
                    },
                },
            )

            scaffold = gpf_output["roleReport"]["auto_handoff"]["command"]["command_output"]["gtcScaffold"]
            task_id = scaffold["task_id"]
            task_detail_path = Path(scaffold["task_detail_path"])
            tpm_inbox = json.loads(
                (session_dir / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8")
            )
            tpm_message = tpm_inbox["messages"][0]

            tpm_output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "message_id": tpm_message["message_id"],
                    "status": "done",
                    "result": "ready_for_evaluation",
                    "summary": "All director reports are complete; hand off to evaluation.",
                    "report_extra": {
                        "team_completion_check": {
                            "all_director_reports_complete": True,
                            "completed_teams": ["infra"],
                        }
                    },
                    "provider_evidence": {
                        "provider_session_id": "sess-tpm",
                        "request_id": "req-tpm",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_print_json",
                    },
                },
            )

            self.assertEqual(tpm_output["roleReport"]["team_completion_update"]["result"], "updated")
            self.assertIn(tpm_output["roleReport"]["auto_handoff"]["result"], {"queued", "queued_nudge_unconfirmed"})
            evaluator_inbox = json.loads(
                (session_dir / "queue" / "inbox" / "gate-task-evaluator.yaml").read_text(encoding="utf-8")
            )
            evaluator_message = evaluator_inbox["messages"][0]
            self.write_provider_activation_evidence(session_dir)
            gate_dir = session_dir / "gates" / task_id
            gate_dir.mkdir(parents=True, exist_ok=True)
            (gate_dir / "evaluation.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "command": "evaluator-precheck",
                        "status": "pass",
                        "result": "gate_precheck_passed",
                        "next_action": "dispatch_thin_gate_task_evaluator_verdict",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            builder = load_builder_module()
            task_text = task_detail_path.read_text(encoding="utf-8")
            task_text = builder.replace_markdown_section(
                task_text,
                "Completion Gate",
                """## Completion Gate

| Field | Value |
|---|---|
| Finalization Status Checked | true |
| Finalization Status | complete |
| Vault Final Update | complete |
""",
            )
            task_text = builder.replace_markdown_section(
                task_text,
                "Quality Evaluation",
                """## Quality Evaluation

| Field | Value |
|---|---|
| Evaluation Status | quality_ok |
""",
            )
            task_text = builder.replace_markdown_section(
                task_text,
                "Task Change Manifest",
                f"""## Task Change Manifest

| Field | Value |
|---|---|
| task_id | {task_id} |
""",
            )
            task_text = builder.replace_markdown_section(
                task_text,
                "Role Execution Evidence",
                """## Role Execution Evidence

| Role | Result | Usage Source |
|---|---|---|
| infra-director | complete | provider_authored_role_report |
""",
            )
            task_text = builder.replace_markdown_section(
                task_text,
                "Finalization Check",
                """## Finalization Check

| Field | Value |
|---|---|
| Finalization Status | complete |
| GTE Final Review Complete | true |
| Vault Final Update | true |
| Handoff To | main_transport_renderer |
""",
            )
            task_text = builder.replace_markdown_section(
                task_text,
                "Completion Envelope",
                """## Completion Envelope

| Field | Value |
|---|---|
| finalization_status_checked | true |
""",
            )
            task_text = builder.replace_markdown_section(
                task_text,
                "Invocation Evidence",
                """## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_print_json | complete |
| gate-task-creator | builder | deterministic | gtc-scaffold | test-session | builder_command | complete |
| teams-project-manager | registry | claude-sonnet-4-6 | req-tpm | sess-tpm | claude_print_json | complete |
| gate-task-evaluator | registry | claude-sonnet-4-6 | req-gte | sess-gte | claude_print_json | complete |
""",
            )
            task_detail_path.write_text(task_text, encoding="utf-8")

            gte_output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-evaluator",
                    "message_id": evaluator_message["message_id"],
                    "status": "done",
                    "result": "quality_ok",
                    "summary": "Publication is not required; continue to Vault final update.",
                    "report_extra": {
                        "git_publication_manifest": {
                            "task_id": task_id,
                            "publication_required": False,
                            "commit_required": False,
                            "push_required": False,
                            "pr_required": False,
                            "handoff_to": "vault_final_update",
                        }
                    },
                    "provider_evidence": {
                        "provider_session_id": "sess-gte",
                        "request_id": "req-gte",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_print_json",
                    },
                },
            )

            auto_handoff = gte_output["roleReport"]["auto_handoff"]
            self.assertEqual(auto_handoff["result"], "command_passed", auto_handoff)
            self.assertEqual(auto_handoff["command"]["command"], "vault-final-update")
            vault_update = auto_handoff["command"]["command_output"]["vaultFinalUpdate"]
            self.assertEqual(vault_update["status"], "complete")
            self.assertEqual(vault_update["result"], "updated_finalization_passed")
            self.assertEqual(vault_update["finalization_check_status"], "pass")
            self.assertEqual(vault_update["finalization_check"]["gateCommand"]["status"], "pass")
            self.assertEqual(
                vault_update["finalization_check"]["gateCommand"]["final_transport_render_check"]["status"],
                "complete",
            )
            final_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("## Vault Final Update", final_text)
            self.assertIn("## Final Transport Render Check", final_text)
            self.assertIn("| Gate Artifacts | team-completion-check:pass, evaluator-precheck:pass |", final_text)
            self.assertTrue((gate_dir / "vault_final_update.json").exists())
            self.assertTrue((gate_dir / "finalization.json").exists())
            self.assertTrue((gate_dir / "final_transport_render_check.json").exists())

    def test_tpm_completion_report_requires_explicit_director_completion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            self.bootstrap(state_root)
            scaffold = self.run_builder(
                state_root,
                "gtc-scaffold",
                {
                    "session_id": "test-session",
                    "vault_root": str(vault_root),
                    "project": "AI-Agent-Organization",
                    "task_id": "TSK-9996",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                },
            )
            task_detail_path = Path(scaffold["gtcScaffold"]["task_detail_path"])
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "from_role": "gate-task-creator",
                    "task_id": "TSK-9996",
                    "message_id": "msg-tpm-missing-director-evidence",
                    "instruction": "Record Team Completion Check evidence.",
                    "context_ref": str(task_detail_path),
                    "defer_nudge": True,
                },
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "message_id": "msg-tpm-missing-director-evidence",
                    "status": "done",
                    "result": "ready_for_evaluation",
                    "summary": "Ready, but missing explicit director completion evidence.",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:teams-project-manager.0",
                        "request_id": "req-tpm-missing-director-evidence",
                        "effective_model": "claude-sonnet-4-20250514",
                        "usage_source": "provider_authored_role_report",
                    },
                },
            )

            report = output["roleReport"]
            self.assertEqual(report["team_completion_update"]["result"], "blocked_missing_director_completion_evidence")
            self.assertEqual(report["auto_handoff"]["result"], "blocked_by_precheck")
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("| Completion Status | pending |", task_text)
            self.assertFalse((state_root / "test-session" / "queue" / "inbox" / "gate-task-evaluator.yaml").exists())

    def test_role_report_cli_args_select_session_and_role_without_session_in_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="target-session")
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-finalize-cli",
                    "instruction": "Review the registry design.",
                },
                extra_args=["--dry-run"],
            )
            self.bootstrap(state_root, session_id="stale-last-session")

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "message_id": "msg-finalize-cli",
                    "status": "done",
                    "result": "review_complete",
                    "summary": "Reviewed registry design.",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:tech-lead.0",
                        "request_id": "req-finalize-cli",
                        "effective_model": "claude-opus-4-8",
                        "usage_source": "claude_tmux_interactive",
                    },
                },
                extra_args=["--session-id", "target-session", "--role-id", "tech-lead"],
            )

            summary = output["roleReport"]
            self.assertEqual(summary["result"], "done")
            self.assertIn("target-session/queue", summary["report_path"])
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "done")
            self.assertFalse((state_root / "stale-last-session" / "queue" / "inbox" / "tech-lead.yaml").exists())
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "finalized" and item["message_id"] == "msg-finalize-cli" for item in metrics))

    def test_role_report_blocks_empty_body_even_with_cli_role_and_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="target-session")
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-empty-body",
                    "instruction": "Review the registry design.",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {},
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "tech-lead",
                    "--message-id",
                    "msg-empty-body",
                ],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("report JSON body", output["reason"])

    def test_role_report_accepts_cli_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="target-session")
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-report-json",
                    "instruction": "Review the registry design.",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {},
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "tech-lead",
                    "--message-id",
                    "msg-report-json",
                    "--report-json",
                    '{"status":"done","result":"review_complete","summary":"Reviewed through CLI JSON."}',
                ],
            )

            summary = output["roleReport"]
            self.assertEqual(summary["result"], "done")
            report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["summary"], "Reviewed through CLI JSON.")

    def test_role_report_cli_report_json_cannot_override_cli_role_or_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="target-session")
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-cli-authoritative",
                    "instruction": "Review the registry design.",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {},
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "tech-lead",
                    "--message-id",
                    "msg-cli-authoritative",
                    "--report-json",
                    json.dumps(
                        {
                            "session_id": "wrong-session",
                            "role_id": "gate-prompt-formatter",
                            "message_id": "msg-malicious",
                            "status": "done",
                            "result": "review_complete",
                            "summary": "Reviewed through protected CLI JSON.",
                        }
                    ),
                ],
            )

            summary = output["roleReport"]
            self.assertEqual(summary["result"], "done")
            inbox = json.loads((session_dir / "queue" / "inbox" / "tech-lead.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "done")
            self.assertEqual(inbox["messages"][0]["message_id"], "msg-cli-authoritative")
            report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["summary"], "Reviewed through protected CLI JSON.")

    def test_role_report_blocks_duplicate_protected_cli_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="target-session")

            output = self.run_builder(
                state_root,
                "role-report",
                {},
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "tech-lead",
                    "--message-id",
                    "msg-safe",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--report-json",
                    '{"status":"done","result":"review_complete","summary":"duplicate role id"}',
                ],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("duplicate protected CLI option", output["reason"])

    def test_role_report_reads_usage_from_provider_transcript_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            transcript_path = session_dir / "provider-transcripts" / "tech-lead.jsonl"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text(
                json.dumps(
                    {
                        "type": "result",
                        "usage": {"input_tokens": 44, "output_tokens": 16},
                        "duration_api_ms": 901,
                        "num_turns": 3,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-transcript-usage",
                    "instruction": "Review the registry design.",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "message_id": "msg-transcript-usage",
                    "status": "done",
                    "result": "review_complete",
                    "summary": "Reviewed registry design.",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:tech-lead.0",
                        "request_id": "req-transcript-usage",
                        "effective_model": "claude-opus-4-8",
                        "usage_source": "claude_tmux_interactive",
                        "transcript_path": str(transcript_path),
                    },
                },
            )

            self.assertEqual(output["roleReport"]["result"], "done")
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "done")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            finalized_metric = [item for item in metrics if item["event_type"] == "finalized"][-1]
            self.assertEqual(finalized_metric["input_tokens"], 44)
            self.assertEqual(finalized_metric["output_tokens"], 16)
            self.assertEqual(finalized_metric["total_tokens"], 60)
            self.assertEqual(finalized_metric["duration_api_ms"], 901)
            self.assertEqual(finalized_metric["num_turns"], 3)

    def test_role_report_discovers_claude_project_transcript_when_report_omits_path(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            session_id = "transcript-discovery-session"
            home.mkdir()
            session_dir = self.bootstrap(state_root, session_id=session_id)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": session_id,
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-transcript-discovery",
                    "message_id": "msg-auto-transcript",
                    "instruction": "Create a minimal Gate Intake Envelope.",
                },
                extra_args=["--dry-run"],
            )
            provider_cwd = state_root / session_id / "provider-state" / "gate-prompt-formatter" / "claude"
            project_dir = home / ".claude" / "projects" / builder.claude_project_dir_name_for_cwd(provider_cwd)
            project_dir.mkdir(parents=True)
            transcript_path = project_dir / "claude-session.jsonl"
            transcript_ts = builder.current_timestamp()
            transcript_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "model": "claude-sonnet-4-6",
                                    "usage": {"input_tokens": 999, "output_tokens": 999},
                                },
                                "requestId": "req-before-match",
                                "cwd": str(provider_cwd),
                                "timestamp": transcript_ts,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "message": {"role": "user", "content": "message_id: msg-auto-transcript"},
                                "cwd": str(provider_cwd),
                                "sessionId": "claude-session",
                                "timestamp": transcript_ts,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "model": "claude-sonnet-4-6",
                                    "usage": {"input_tokens": 123, "output_tokens": 45},
                                },
                                "duration_api_ms": 678,
                                "num_turns": 2,
                                "requestId": "req-auto-transcript",
                                "cwd": str(provider_cwd),
                                "sessionId": "claude-session",
                                "timestamp": transcript_ts,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "message": {"role": "user", "content": "message_id: msg-other-transcript"},
                                "cwd": str(provider_cwd),
                                "sessionId": "claude-session",
                                "timestamp": transcript_ts,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "model": "claude-sonnet-4-6",
                                    "usage": {"input_tokens": 888, "output_tokens": 888},
                                },
                                "requestId": "req-after-match",
                                "cwd": str(provider_cwd),
                                "sessionId": "claude-session",
                                "timestamp": transcript_ts,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": session_id,
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-auto-transcript",
                    "status": "done",
                    "result": "envelope_created",
                    "summary": "Envelope created.",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "request_id": "interactive-role-report",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "provider_turn",
                    },
                },
                env=os.environ | {"HOME": str(home), "ITB_ROLE_QUEUE_ACTIVATE_PROVIDER": "0"},
            )

            self.assertEqual(output["roleReport"]["result"], "done")
            report_ref = json.loads(Path(queue_output["roleQueue"]["inbox_path"]).read_text(encoding="utf-8"))["messages"][0]["report_path"]
            report = json.loads((session_dir / "queue" / report_ref).read_text(encoding="utf-8"))
            evidence = report["provider_evidence"]
            self.assertEqual(evidence["transcript_path"], str(transcript_path))
            self.assertEqual(evidence["transcript_discovery_status"], "found")
            self.assertEqual(evidence["transcript_usage_scope"], "matched_turn")
            self.assertEqual(evidence["usage_source"], "claude_transcript_jsonl")
            self.assertEqual(evidence["request_id"], "req-auto-transcript")
            self.assertEqual(evidence["input_tokens"], 123)
            self.assertEqual(evidence["output_tokens"], 45)
            self.assertEqual(evidence["total_tokens"], 168)
            self.assertEqual(evidence["duration_api_ms"], 678)
            self.assertEqual(evidence["num_turns"], 2)
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            finalized_metric = [item for item in metrics if item["event_type"] == "finalized"][-1]
            self.assertEqual(finalized_metric["input_tokens"], 123)
            self.assertEqual(finalized_metric["output_tokens"], 45)
            self.assertEqual(finalized_metric["usage_source"], "claude_transcript_jsonl")
            self.assertEqual(finalized_metric["transcript_path"], str(transcript_path))

    def test_claude_project_transcript_discovery_uses_bootstrap_workspace_cwd_by_default(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            session_id = "workspace-transcript-session"
            workspace = root / "skills-repo"
            session_dir = state_root / session_id
            home.mkdir()
            workspace.mkdir()
            session_dir.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(json.dumps({"cwd": str(workspace)}) + "\n", encoding="utf-8")
            project_dir = home / ".claude" / "projects" / builder.claude_project_dir_name_for_cwd(workspace)
            project_dir.mkdir(parents=True)
            transcript_path = project_dir / "claude-session.jsonl"
            transcript_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "message": {"role": "user", "content": "message_id: msg-workspace-cwd"},
                                "cwd": str(workspace),
                                "timestamp": "2026-06-14T12:00:01+09:00",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "model": "claude-sonnet-4-6",
                                    "usage": {"input_tokens": 11, "output_tokens": 7},
                                },
                                "requestId": "req-workspace-cwd",
                                "cwd": str(workspace),
                                "sessionId": "claude-session",
                                "timestamp": "2026-06-14T12:00:02+09:00",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(builder.Path, "home", return_value=home):
                found_path, status, metadata_fields, usage_fields = builder.discover_claude_transcript_path_for_role_report(
                    state_root=state_root,
                    session_id=session_id,
                    role_id="gate-prompt-formatter",
                    message_id="msg-workspace-cwd",
                    task_id="ENTRY-workspace-cwd",
                )

        self.assertEqual(found_path, str(transcript_path))
        self.assertEqual(status, "found")
        self.assertEqual(metadata_fields["request_id"], "req-workspace-cwd")
        self.assertEqual(usage_fields["input_tokens"], 11)
        self.assertEqual(usage_fields["output_tokens"], 7)

    def test_role_report_does_not_guess_ambiguous_claude_project_transcript(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            session_id = "transcript-ambiguous-session"
            home.mkdir()
            session_dir = self.bootstrap(state_root, session_id=session_id)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": session_id,
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-transcript-ambiguous",
                    "message_id": "msg-ambiguous-transcript",
                    "instruction": "Create a minimal Gate Intake Envelope.",
                },
                extra_args=["--dry-run"],
            )
            provider_cwd = state_root / session_id / "provider-state" / "gate-prompt-formatter" / "claude"
            project_dir = home / ".claude" / "projects" / builder.claude_project_dir_name_for_cwd(provider_cwd)
            project_dir.mkdir(parents=True)
            transcript_ts = builder.current_timestamp()
            for index in (1, 2):
                (project_dir / f"claude-session-{index}.jsonl").write_text(
                    "\n".join(
                        [
                            json.dumps(
                                {
                                    "type": "user",
                                    "message": {
                                        "role": "user",
                                        "content": f"message_id: msg-ambiguous-transcript session {index}",
                                    },
                                    "cwd": str(provider_cwd),
                                    "timestamp": transcript_ts,
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "assistant",
                                    "message": {
                                        "model": "claude-sonnet-4-6",
                                        "usage": {"input_tokens": 10 * index, "output_tokens": 5 * index},
                                    },
                                    "requestId": f"req-ambiguous-{index}",
                                    "cwd": str(provider_cwd),
                                    "timestamp": transcript_ts,
                                }
                            ),
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": session_id,
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-ambiguous-transcript",
                    "status": "done",
                    "result": "envelope_created",
                    "summary": "Envelope created.",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "request_id": "interactive-role-report",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "provider_turn",
                    },
                },
                env=os.environ | {"HOME": str(home), "ITB_ROLE_QUEUE_ACTIVATE_PROVIDER": "0"},
            )

            self.assertEqual(output["roleReport"]["result"], "done")
            report_ref = json.loads(Path(queue_output["roleQueue"]["inbox_path"]).read_text(encoding="utf-8"))["messages"][0]["report_path"]
            report = json.loads((session_dir / "queue" / report_ref).read_text(encoding="utf-8"))
            evidence = report["provider_evidence"]
            self.assertEqual(evidence["transcript_discovery_status"], "ambiguous")
            self.assertEqual(evidence["transcript_path"], "")
            self.assertEqual(evidence["usage_source"], "provider_turn")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            finalized_metric = [item for item in metrics if item["event_type"] == "finalized"][-1]
            self.assertNotIn("input_tokens", finalized_metric)
            self.assertEqual(finalized_metric["usage_source"], "provider_turn")

    def test_claude_project_transcript_discovery_rejects_unsafe_unique_matches(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            home.mkdir()
            created_at = "2026-06-14T12:00:00+09:00"

            def write_transcript(session_id: str, name: str, records: list[dict[str, object]]) -> None:
                provider_cwd = state_root / session_id / "provider-state" / "gate-prompt-formatter" / "claude"
                project_dir = home / ".claude" / "projects" / builder.claude_project_dir_name_for_cwd(provider_cwd)
                project_dir.mkdir(parents=True, exist_ok=True)
                (project_dir / name).write_text(
                    "\n".join(json.dumps(record) for record in records) + "\n",
                    encoding="utf-8",
                )

            with mock.patch.object(builder.Path, "home", return_value=home):
                partial_cwd = state_root / "partial-session" / "provider-state" / "gate-prompt-formatter" / "claude"
                write_transcript(
                    "partial-session",
                    "partial.jsonl",
                    [
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "message_id: msg-partial-10"},
                            "cwd": str(partial_cwd),
                            "timestamp": "2026-06-14T03:00:01Z",
                        }
                    ],
                )
                self.assertEqual(
                    builder.discover_claude_transcript_path_for_role_report(
                        state_root=state_root,
                        session_id="partial-session",
                        role_id="gate-prompt-formatter",
                        message_id="msg-partial-1",
                        task_id="ENTRY-partial",
                        message_created_at=created_at,
                    )[1],
                    "not_found",
                )

                metadata_cwd = state_root / "metadata-session" / "provider-state" / "gate-prompt-formatter" / "claude"
                write_transcript(
                    "metadata-session",
                    "metadata-only.jsonl",
                    [
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "no queue marker in content"},
                            "message_id": "msg-metadata-only",
                            "cwd": str(metadata_cwd),
                            "timestamp": "2026-06-14T03:00:01Z",
                        }
                    ],
                )
                self.assertEqual(
                    builder.discover_claude_transcript_path_for_role_report(
                        state_root=state_root,
                        session_id="metadata-session",
                        role_id="gate-prompt-formatter",
                        message_id="msg-metadata-only",
                        task_id="ENTRY-metadata",
                        message_created_at=created_at,
                    )[1],
                    "not_found",
                )

                cwd_cwd = state_root / "cwd-session" / "provider-state" / "gate-prompt-formatter" / "claude"
                write_transcript(
                    "cwd-session",
                    "wrong-cwd.jsonl",
                    [
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "message_id: msg-wrong-cwd"},
                            "cwd": str(root / "other-provider-state"),
                            "timestamp": "2026-06-14T03:00:01Z",
                        }
                    ],
                )
                self.assertEqual(
                    builder.discover_claude_transcript_path_for_role_report(
                        state_root=state_root,
                        session_id="cwd-session",
                        role_id="gate-prompt-formatter",
                        message_id="msg-wrong-cwd",
                        task_id="ENTRY-cwd",
                        message_created_at=created_at,
                    )[1],
                    "not_found",
                )

                stale_cwd = state_root / "stale-session" / "provider-state" / "gate-prompt-formatter" / "claude"
                write_transcript(
                    "stale-session",
                    "stale.jsonl",
                    [
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "message_id: msg-stale"},
                            "cwd": str(stale_cwd),
                            "timestamp": "2000-01-01T00:00:00Z",
                        }
                    ],
                )
                self.assertEqual(
                    builder.discover_claude_transcript_path_for_role_report(
                        state_root=state_root,
                        session_id="stale-session",
                        role_id="gate-prompt-formatter",
                        message_id="msg-stale",
                        task_id="ENTRY-stale",
                        message_created_at=created_at,
                    )[1],
                    "not_found",
                )

                mismatch_cwd = state_root / "mismatch-session" / "provider-state" / "gate-prompt-formatter" / "claude"
                write_transcript(
                    "mismatch-session",
                    "mismatch.jsonl",
                    [
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "message_id: msg-mismatch"},
                            "cwd": str(mismatch_cwd),
                            "timestamp": "2026-06-14T03:00:01Z",
                        },
                        {
                            "type": "assistant",
                            "message": {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 1, "output_tokens": 2}},
                            "cwd": str(mismatch_cwd),
                            "timestamp": "2026-06-14T03:00:02Z",
                            "requestId": "req-other",
                        },
                    ],
                )
                self.assertEqual(
                    builder.discover_claude_transcript_path_for_role_report(
                        state_root=state_root,
                        session_id="mismatch-session",
                        role_id="gate-prompt-formatter",
                        message_id="msg-mismatch",
                        task_id="ENTRY-mismatch",
                        message_created_at=created_at,
                        supplied_request_id="req-expected",
                    )[1],
                    "request_id_mismatch",
                )

    def test_role_report_auto_handoff_runs_team_completion_command_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail_path = Path(tmp) / "TSK-9999" / "task.md"
            task_detail_path.parent.mkdir(parents=True)
            self.write_task_detail(task_detail_path, completion_envelope=True)
            active = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "post_routing",
                    "owner_role": "teams-project-manager",
                    "last_gate": "team-completion-check",
                },
            )
            self.assertEqual(active["activeTask"]["result"], "active_task_set")
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "from_role": "gate-task-creator",
                    "task_id": "TSK-9999",
                    "message_id": "msg-tpm",
                    "instruction": "Record Team Completion Check evidence.",
                    "auto_chain_dry_run": True,
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "message_id": "msg-tpm",
                    "status": "done",
                    "result": "ready_for_evaluation",
                    "summary": "Team Completion Check ready.",
                    "auto_chain_dry_run": True,
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:teams-project-manager.0",
                        "request_id": "req-tpm",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                    },
                },
            )

            auto_handoff = output["roleReport"]["auto_handoff"]
            self.assertEqual(auto_handoff["result"], "queued")
            self.assertEqual(auto_handoff["precheck"]["result"], "passed")
            self.assertEqual(auto_handoff["precheck"]["command_output"]["gateCommand"]["status"], "pass")
            self.assertTrue(auto_handoff["precheck"]["command_output"]["gateCommand"]["next_phase_allowed"])
            evaluator_inbox_path = session_dir / "queue" / "inbox" / "gate-task-evaluator.yaml"
            self.assertTrue(evaluator_inbox_path.exists())
            evaluator_inbox = json.loads(evaluator_inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(evaluator_inbox["messages"][0]["from_role"], "teams-project-manager")
            self.assertEqual(evaluator_inbox["messages"][0]["to_role"], "gate-task-evaluator")
            self.assertEqual(
                evaluator_inbox["messages"][0]["payload"]["precheck_result"]["command_output"]["gateCommand"]["command"],
                "team-completion-check",
            )
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            handoffs = [item for item in events if item["event_type"] == "auto_queue_handoff"]
            self.assertEqual(handoffs[-1]["from_role"], "teams-project-manager")
            self.assertEqual(handoffs[-1]["to_role"], "gate-task-evaluator")
            self.assertEqual(handoffs[-1]["precheck"]["result"], "passed")

    def test_role_report_auto_handoff_runs_vault_final_update_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail_path = Path(tmp) / "TSK-9999" / "task.md"
            task_detail_path.parent.mkdir(parents=True)
            self.write_task_detail(task_detail_path, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            task_text = task_detail_path.read_text(encoding="utf-8")
            start = task_text.index("## Final Transport Render Check")
            end = task_text.index("## Invocation Evidence", start)
            task_detail_path.write_text(task_text[:start].rstrip() + "\n\n" + task_text[end:], encoding="utf-8")
            task_detail_path.write_text(
                task_detail_path.read_text(encoding="utf-8")
                + """
## Git Publication Manifest

| Field | Value |
|---|---|
| publication_required | false |
| commit_required | false |
| push_required | false |
| pr_required | false |
| handoff_to | vault_final_update |
""",
                encoding="utf-8",
            )
            gate_dir = session_dir / "gates" / "TSK-9999"
            gate_dir.mkdir(parents=True)
            for name, payload in {
                "tpm_completion": {"command": "team-completion-check", "status": "pass"},
                "evaluation": {"command": "evaluator-precheck", "status": "pass"},
            }.items():
                (gate_dir / f"{name}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "post_routing",
                    "owner_role": "gate-task-evaluator",
                    "last_gate": "gate-task-evaluator",
                },
            )
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-evaluator",
                    "from_role": "teams-project-manager",
                    "task_id": "TSK-9999",
                    "message_id": "msg-gte",
                    "instruction": "Record Quality Evaluation and publication decision.",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-evaluator",
                    "message_id": "msg-gte",
                    "status": "done",
                    "result": "quality_ok",
                    "summary": "Publication is not required; continue to Vault final update.",
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-task-evaluator.0",
                        "request_id": "req-gte",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                    },
                    "report_extra": {
                        "git_publication_manifest": {
                            "publication_required": False,
                            "commit_required": False,
                            "push_required": False,
                            "pr_required": False,
                            "handoff_to": "vault_final_update",
                        }
                    },
                },
            )

            auto_handoff = output["roleReport"]["auto_handoff"]
            self.assertEqual(auto_handoff["result"], "command_passed")
            self.assertEqual(auto_handoff["handoff_type"], "command")
            self.assertEqual(auto_handoff["condition"]["result"], "passed")
            self.assertEqual(auto_handoff["command"]["command"], "vault-final-update")
            self.assertTrue(auto_handoff["command"]["passed"])
            self.assertEqual(auto_handoff["command"]["command_output"]["vaultFinalUpdate"]["status"], "complete")
            self.assertEqual(
                auto_handoff["command"]["command_output"]["vaultFinalUpdate"]["finalization_check_status"],
                "pass",
            )
            self.assertEqual(
                auto_handoff["command"]["command_output"]["vaultFinalUpdate"]["finalization_check"]["gateCommand"]["status"],
                "pass",
            )
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("## Vault Final Update", task_text)
            self.assertIn("## Final Transport Render Check", task_text)
            self.assertIn("| Gate Artifacts | team-completion-check:pass, evaluator-precheck:pass |", task_text)
            self.assertTrue((gate_dir / "vault_final_update.json").exists())
            self.assertTrue((gate_dir / "finalization.json").exists())

    def test_role_report_auto_handoff_queues_git_publisher_when_publication_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail_path = Path(tmp) / "TSK-9999" / "task.md"
            task_detail_path.parent.mkdir(parents=True)
            self.write_task_detail(task_detail_path, completion_envelope=True)
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "post_routing",
                    "owner_role": "gate-task-evaluator",
                    "last_gate": "gate-task-evaluator",
                },
            )
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-evaluator",
                    "from_role": "teams-project-manager",
                    "task_id": "TSK-9999",
                    "message_id": "msg-gte-pub",
                    "instruction": "Record Quality Evaluation and publication decision.",
                    "auto_chain_dry_run": True,
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-evaluator",
                    "message_id": "msg-gte-pub",
                    "status": "done",
                    "result": "quality_ok",
                    "summary": "Publication is required; continue to git-publisher.",
                    "auto_chain_dry_run": True,
                    "provider_evidence": {
                        "provider_session_id": "itb-org-test:gate-task-evaluator.0",
                        "request_id": "req-gte-pub",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                    },
                    "report_extra": {
                        "task_change_manifest": {
                            "task_id": "TSK-9999",
                            "repo_root": str(task_detail_path.parent),
                            "owned_paths": ["task.md"],
                            "approved_scope": "task_owned_git_diff",
                            "approved_diff_snapshot": "fixture",
                            "commit_required": True,
                        },
                        "git_publication_manifest": {
                            "task_id": "TSK-9999",
                            "repo_root": str(task_detail_path.parent),
                            "branch_plan": "test-branch-plan",
                            "publication_required": True,
                            "commit_required": True,
                            "push_required": False,
                            "pr_required": False,
                            "publication_policy": "commit_required_for_task_owned_git_diff",
                            "publication_flow": "commit_only",
                            "handoff_to": "git-publisher",
                        }
                    },
                },
            )

            auto_handoff = output["roleReport"]["auto_handoff"]
            self.assertEqual(auto_handoff["result"], "queued")
            self.assertEqual(auto_handoff["to_role"], "git-publisher")
            self.assertEqual(auto_handoff["condition"]["result"], "passed")
            self.assertEqual(auto_handoff["queue_output"]["roleQueue"]["role_id"], "git-publisher")
            self.assertTrue(auto_handoff["queue_output"]["roleQueue"]["task_payload_path"])
            inbox_path = Path(auto_handoff["queue_output"]["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            queued_message = inbox["messages"][-1]
            self.assertEqual(queued_message["to_role"], "git-publisher")
            self.assertEqual(queued_message["payload"]["git_publication_manifest"]["handoff_to"], "git-publisher")
            self.assertEqual(queued_message["payload"]["task_change_manifest"]["task_id"], "TSK-9999")
            self.assertNotIn("## Vault Final Update", task_detail_path.read_text(encoding="utf-8"))

    def test_git_publisher_auto_handoff_runs_vault_final_update_command(self) -> None:
        builder = load_builder_module()

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = Path(tmp) / "TSK-9993"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            report_path = session_dir / "queue" / "reports" / "git-publisher" / "TSK-9993" / "publication.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "git_publication_result": {
                            "git_publication_status": "complete",
                            "executor_role": "git-publisher",
                            "executor_session_id": "claude-session-git-publisher",
                            "usage_source": "claude_tmux_interactive",
                            "commit_status": "complete",
                            "commit_hashes": ["abc123"],
                            "committed_diff_matches_snapshot": True,
                            "push_status": "not_required",
                            "pr_status": "not_required",
                            "finalization_status": "complete",
                            "finalization_commit_hashes": ["def456"],
                            "next_role": "vault_final_update",
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            report_sha = builder.file_sha256_if_exists(report_path)
            self.write_task_detail(task_detail_path, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            task_text = task_detail_path.read_text(encoding="utf-8").replace("TSK-9999", "TSK-9993")
            start = task_text.index("## Final Transport Render Check")
            end = task_text.index("## Invocation Evidence", start)
            task_text = task_text[:start].rstrip() + "\n\n" + task_text[end:]
            task_detail_path.write_text(
                task_text
                + "\n## Git Publication Manifest\n\n"
                "| Field | Value |\n|---|---|\n"
                "| commit_required | true |\n"
                "| push_required | false |\n"
                "| pr_required | false |\n"
                "| publication_flow | commit_only |\n"
                "| handoff_to | git-publisher |\n\n"
                "## Git Publication Result\n\n"
                "| Field | Value |\n|---|---|\n"
                "| Status | complete |\n"
                "| Summary | Publication complete; see report. |\n"
                f"| Report Path | {report_path} |\n"
                f"| Report SHA256 | {report_sha} |\n"
                "| Owner Role | git-publisher |\n",
                encoding="utf-8",
            )
            gate_dir = session_dir / "gates" / "TSK-9993"
            gate_dir.mkdir(parents=True)
            for name, payload in {
                "tpm_completion": {"command": "team-completion-check", "status": "pass"},
                "evaluation": {"command": "evaluator-precheck", "status": "pass"},
            }.items():
                (gate_dir / f"{name}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9993",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "publication",
                    "owner_role": "git-publisher",
                    "last_gate": "git-publisher",
                },
            )

            auto_handoff = builder.maybe_enqueue_auto_queue_handoff(
                runtime="codex",
                state_root=state_root,
                session_id="test-session",
                organization_instance_id=builder.organization_id("test-session"),
                from_role="git-publisher",
                finalized={
                    "result": "done",
                    "task_id": "TSK-9993",
                    "report_path": str(report_path),
                    "report_ref": "reports/git-publisher/TSK-9993/publication.json",
                },
                hook_input={
                    "session_id": "test-session",
                    "task_id": "TSK-9993",
                    "task_detail_path": str(task_detail_path),
                },
            )

            self.assertEqual(auto_handoff["result"], "command_passed")
            self.assertEqual(auto_handoff["handoff_type"], "command")
            self.assertEqual(auto_handoff["condition"]["result"], "passed")
            self.assertEqual(auto_handoff["command"]["command"], "vault-final-update")
            self.assertTrue(auto_handoff["command"]["passed"])
            self.assertEqual(auto_handoff["command"]["command_output"]["vaultFinalUpdate"]["status"], "complete")
            self.assertEqual(auto_handoff["command"]["command_output"]["vaultFinalUpdate"]["finalization_check_status"], "pass")
            self.assertTrue((gate_dir / "vault_final_update.json").exists())
            self.assertTrue((gate_dir / "finalization.json").exists())
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("## Git Publication Result", task_text)
            self.assertIn("## Vault Final Update", task_text)
            self.assertIn("## Final Transport Render Check", task_text)

    def test_git_publisher_auto_handoff_blocks_when_publication_gate_incomplete(self) -> None:
        builder = load_builder_module()

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = Path(tmp) / "TSK-9992"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            task_detail_path.write_text(
                "# TSK-9992 Publication\n\n"
                "## Metadata\n\n"
                "| Field | Value |\n|---|---|\n| Task ID | TSK-9992 |\n\n"
                "## Git Publication Manifest\n\n"
                "| Field | Value |\n|---|---|\n"
                "| commit_required | true |\n"
                "| push_required | false |\n"
                "| pr_required | false |\n"
                "| publication_flow | commit_only |\n"
                "| handoff_to | git-publisher |\n",
                encoding="utf-8",
            )
            report_path = session_dir / "queue" / "reports" / "git-publisher" / "TSK-9992" / "publication.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "git_publication_result": {
                            "git_publication_status": "complete",
                            "executor_role": "git-publisher",
                            "executor_session_id": "claude-session-git-publisher",
                            "usage_source": "claude_tmux_interactive",
                            "commit_status": "complete",
                            "commit_hashes": ["abc123"],
                            "committed_diff_matches_snapshot": True,
                            "push_status": "not_required",
                            "pr_status": "not_required",
                            "next_role": "vault_final_update",
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9992",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "publication",
                    "owner_role": "git-publisher",
                    "last_gate": "git-publisher",
                },
            )

            auto_handoff = builder.maybe_enqueue_auto_queue_handoff(
                runtime="codex",
                state_root=state_root,
                session_id="test-session",
                organization_instance_id=builder.organization_id("test-session"),
                from_role="git-publisher",
                finalized={
                    "result": "done",
                    "task_id": "TSK-9992",
                    "report_path": str(report_path),
                    "report_ref": "reports/git-publisher/TSK-9992/publication.json",
                },
                hook_input={
                    "session_id": "test-session",
                    "task_id": "TSK-9992",
                    "task_detail_path": str(task_detail_path),
                },
            )

            self.assertEqual(auto_handoff["result"], "blocked_by_condition")
            self.assertEqual(auto_handoff["condition"]["reason"], "git_publication_gate_incomplete")
            self.assertTrue(
                any(
                    "Git Publication Result missing while commit is required" in item
                    for item in auto_handoff["condition"]["validation_errors"]
                )
            )
            self.assertFalse((session_dir / "gates" / "TSK-9992" / "vault_final_update.json").exists())
            self.assertNotIn("## Vault Final Update", task_detail_path.read_text(encoding="utf-8"))

    def test_queue_watch_recovery_auto_handoff_runs_command_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail_path = Path(tmp) / "TSK-9999" / "task.md"
            task_detail_path.parent.mkdir(parents=True)
            self.write_task_detail(task_detail_path, completion_envelope=True)
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "post_routing",
                    "owner_role": "teams-project-manager",
                    "last_gate": "team-completion-check",
                },
            )
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "from_role": "gate-task-creator",
                    "task_id": "TSK-9999",
                    "message_id": "msg-tpm-recover",
                    "report_id": "rep-tpm-recover",
                    "instruction": "Record Team Completion Check evidence.",
                },
                extra_args=["--dry-run"],
            )
            report_path = Path(queued["roleQueue"]["report_path"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "report_version": "1",
                        "report_id": report_path.stem,
                        "report_type": "role_queue_terminal_report",
                        "from_role": "teams-project-manager",
                        "task_id": "TSK-9999",
                        "message_id": "msg-tpm-recover",
                        "created_at": "2026-01-01T00:00:00+09:00",
                        "result": "ready_for_evaluation",
                        "status": "done",
                        "summary": "Team Completion Check ready.",
                        "provider_evidence": {
                            "provider_session_id": "itb-org-test:teams-project-manager.0",
                            "request_id": "req-tpm-recover",
                            "effective_model": "claude-sonnet-4-6",
                            "usage_source": "claude_tmux_interactive",
                            "transcript_path": "/tmp/claude-transcript.jsonl",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "auto_chain_dry_run": True,
                },
            )

            self.assertEqual(output["queueWatch"]["result"], "recovered")
            recovered = output["queueWatch"]["recovered_messages"][0]
            self.assertEqual(recovered["auto_handoff"]["result"], "queued")
            self.assertEqual(recovered["auto_handoff"]["precheck"]["result"], "passed")
            evaluator_inbox_path = session_dir / "queue" / "inbox" / "gate-task-evaluator.yaml"
            evaluator_inbox = json.loads(evaluator_inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(evaluator_inbox["messages"][0]["from_role"], "teams-project-manager")
            self.assertEqual(evaluator_inbox["messages"][0]["payload"]["precheck_result"]["result"], "passed")

    def test_queue_watch_tpm_recovery_requires_explicit_director_completion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)
            scaffold = self.run_builder(
                state_root,
                "gtc-scaffold",
                {
                    "session_id": "test-session",
                    "vault_root": str(vault_root),
                    "project": "AI-Agent-Organization",
                    "task_id": "TSK-9995",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                },
            )
            task_detail_path = Path(scaffold["gtcScaffold"]["task_detail_path"])
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                    "from_role": "gate-task-creator",
                    "task_id": "TSK-9995",
                    "message_id": "msg-tpm-recover-missing-director-evidence",
                    "report_id": "rep-tpm-recover-missing-director-evidence",
                    "instruction": "Record Team Completion Check evidence.",
                    "context_ref": str(task_detail_path),
                    "defer_nudge": True,
                },
            )
            report_path = Path(queued["roleQueue"]["report_path"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "report_version": "1",
                        "report_id": report_path.stem,
                        "report_type": "role_queue_terminal_report",
                        "from_role": "teams-project-manager",
                        "task_id": "TSK-9995",
                        "message_id": "msg-tpm-recover-missing-director-evidence",
                        "created_at": "2026-01-01T00:00:00+09:00",
                        "result": "ready_for_evaluation",
                        "status": "done",
                        "summary": "Ready, but missing explicit director completion evidence.",
                        "provider_evidence": {
                            "provider_session_id": "itb-org-test:teams-project-manager.0",
                            "request_id": "req-tpm-recover-missing-director-evidence",
                            "effective_model": "claude-sonnet-4-6",
                            "usage_source": "claude_tmux_interactive",
                            "transcript_path": "/tmp/claude-transcript.jsonl",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "teams-project-manager",
                },
            )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "recovered")
            recovered = summary["recovered_messages"][0]
            self.assertEqual(
                recovered["team_completion_update"]["result"],
                "blocked_missing_director_completion_evidence",
            )
            self.assertEqual(recovered["auto_handoff"]["result"], "blocked_by_precheck")
            self.assertIn("| Completion Status | pending |", task_detail_path.read_text(encoding="utf-8"))
            self.assertFalse((session_dir / "queue" / "inbox" / "gate-task-evaluator.yaml").exists())

    def test_role_report_archives_old_terminal_inbox_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            env = os.environ.copy()
            env["ITB_INBOX_TERMINAL_KEEP"] = "1"
            message_ids = ["msg-one", "msg-two", "msg-three"]
            inbox_path: Path | None = None
            for message_id in message_ids:
                queued = self.run_builder(
                    state_root,
                    "role-queue",
                    {
                        "session_id": "test-session",
                        "role_id": "tech-lead",
                        "from_role": "infra-director",
                        "task_id": "TSK-9999",
                        "message_id": message_id,
                        "report_id": f"rep-{message_id}",
                        "instruction": f"Review item {message_id}.",
                    },
                    extra_args=["--dry-run"],
                )
                inbox_path = Path(queued["roleQueue"]["inbox_path"])
                self.run_builder(
                    state_root,
                    "role-report",
                    {
                        "session_id": "test-session",
                        "role_id": "tech-lead",
                        "message_id": message_id,
                        "status": "done",
                        "result": "review_complete",
                        "summary": f"Reviewed {message_id}.",
                        "provider_evidence": {
                            "provider_session_id": "itb-org-test:tech-lead.0",
                            "request_id": f"req-{message_id}",
                            "effective_model": "claude-opus-4-8",
                            "usage_source": "claude_tmux_interactive",
                        },
                    },
                    env=env,
                )

            assert inbox_path is not None
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual([item["message_id"] for item in inbox["messages"]], ["msg-three"])
            self.assertEqual(inbox["terminal_archive"]["terminal_keep_count"], 1)
            archive_path = Path(inbox["terminal_archive"]["archive_path"])
            archived = [json.loads(line) for line in archive_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["message"]["message_id"] for item in archived], ["msg-one", "msg-two"])

    def test_role_queue_appends_multiple_messages_to_same_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            for message_id in ("msg-one", "msg-two"):
                self.run_builder(
                    state_root,
                    "role-queue",
                    {
                        "session_id": "test-session",
                        "role_id": "tech-lead",
                        "from_role": "infra-director",
                        "task_id": "TSK-9999",
                        "message_id": message_id,
                        "instruction": f"instruction {message_id}",
                    },
                    extra_args=["--dry-run"],
                )

            output = self.run_builder(
                state_root,
                "role-queue",
                {"session_id": "test-session", "role_id": "tech-lead", "action": "inspect"},
            )

            inbox = output["roleQueue"]["inbox"]
            self.assertEqual(output["roleQueue"]["message_count"], 2)
            self.assertEqual([message["message_id"] for message in inbox["messages"]], ["msg-one", "msg-two"])

    def test_role_queue_blocks_unsafe_path_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "../escape",
                    "message_id": "msg-fixed",
                    "instruction": "must not escape queue root",
                },
                extra_args=["--dry-run"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("task_id contains unsafe path characters", output["reason"])
            self.assertFalse((session_dir / "escape").exists())

    def test_role_queue_blocks_duplicate_message_id_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            hook_input = {
                "session_id": "test-session",
                "role_id": "tech-lead",
                "from_role": "infra-director",
                "task_id": "TSK-9999",
                "message_id": "msg-fixed",
                "instruction": "first",
            }
            self.run_builder(state_root, "role-queue", hook_input, extra_args=["--dry-run"])

            output = self.run_builder(
                state_root,
                "role-queue",
                hook_input | {"instruction": "second"},
                extra_args=["--dry-run"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("duplicate message_id", output["reason"])
            inspect = self.run_builder(
                state_root,
                "role-queue",
                {"session_id": "test-session", "role_id": "tech-lead", "action": "inspect"},
            )
            self.assertEqual(inspect["roleQueue"]["message_count"], 1)

    def test_role_queue_blocks_cross_role_payload_collision_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            base_input = {
                "session_id": "test-session",
                "from_role": "infra-director",
                "task_id": "TSK-9999",
                "message_id": "msg-shared",
            }
            first = self.run_builder(
                state_root,
                "role-queue",
                base_input | {"role_id": "tech-lead", "instruction": "first instruction"},
                extra_args=["--dry-run"],
            )
            self.assertEqual(first["roleQueue"]["result"], "queued")
            task_payload_path = Path(first["roleQueue"]["task_payload_path"])

            second = self.run_builder(
                state_root,
                "role-queue",
                base_input | {"role_id": "tech-backend", "instruction": "second instruction"},
                extra_args=["--dry-run"],
            )

            self.assertEqual(second["decision"], "block")
            self.assertIn("task payload already exists", second["reason"])
            payload = json.loads(task_payload_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["to_role"], "tech-lead")
            self.assertEqual(payload["instruction"], "first instruction")
            inspect_backend = self.run_builder(
                state_root,
                "role-queue",
                {"session_id": "test-session", "role_id": "tech-backend", "action": "inspect"},
            )
            self.assertEqual(inspect_backend["roleQueue"]["message_count"], 0)

    def test_queue_lock_reclaims_stale_lock_without_owner_metadata(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "queue" / "locks" / "enqueue.lock"
            lock_path.mkdir(parents=True)
            old_time = time.time() - 600
            os.utime(lock_path, (old_time, old_time))

            builder.acquire_queue_lock(lock_path, timeout_seconds=0.1, stale_after_seconds=1)
            try:
                self.assertTrue((lock_path / "owner.json").exists())
                owner = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], os.getpid())
            finally:
                builder.release_queue_lock(lock_path)

    def test_queue_lock_does_not_reclaim_live_owner(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "queue" / "locks" / "enqueue.lock"
            lock_path.mkdir(parents=True)
            (lock_path / "owner.json").write_text(json.dumps({"pid": os.getpid()}) + "\n", encoding="utf-8")
            old_time = time.time() - 600
            os.utime(lock_path, (old_time, old_time))

            with self.assertRaises(TimeoutError):
                builder.acquire_queue_lock(lock_path, timeout_seconds=0.01, stale_after_seconds=1)

    def test_shared_resource_lock_requires_matching_lease_release(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)

            acquired = builder.shared_resource_lock_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "serializer-session",
                    "action": "acquire",
                    "resource_id": "repo:/tmp/example",
                    "lease_id": "lease-ok",
                    "holder": "git-publisher",
                },
            )
            wrong_release = builder.shared_resource_lock_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "serializer-session",
                    "action": "release",
                    "resource_id": "repo:/tmp/example",
                    "lease_id": "lease-wrong",
                },
            )
            status = builder.shared_resource_lock_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "serializer-session",
                    "action": "status",
                    "resource_id": "repo:/tmp/example",
                },
            )
            released = builder.shared_resource_lock_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "serializer-session",
                    "action": "release",
                    "resource_id": "repo:/tmp/example",
                    "lease_id": "lease-ok",
                },
            )

            self.assertEqual(acquired["sharedResourceLock"]["result"], "acquired")
            self.assertEqual(wrong_release["decision"], "block")
            self.assertEqual(status["sharedResourceLock"]["result"], "held")
            self.assertEqual(released["sharedResourceLock"]["result"], "released")

    def test_shared_file_update_replaces_under_shared_root_with_expected_hash(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            shared_root = root / "Agents-Vault"
            target = shared_root / "00-Inbox&Tasks" / "Task-Index.md"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")
            expected = hashlib.sha256(target.read_bytes()).hexdigest()

            original_roots = builder.SHARED_FILE_ROOTS
            builder.SHARED_FILE_ROOTS = (shared_root,)
            try:
                output = builder.shared_file_update_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "serializer-session",
                        "target_path": str(target),
                        "operation": "replace",
                        "replace_text": "new\n",
                        "expected_sha256": expected,
                        "holder": "gate-task-creator",
                    },
                )
                mismatch = builder.shared_file_update_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "serializer-session",
                        "target_path": str(target),
                        "operation": "replace",
                        "replace_text": "bad\n",
                        "expected_sha256": expected,
                    },
                )
                outside = builder.shared_file_update_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "serializer-session",
                        "target_path": str(root / "outside.md"),
                        "operation": "append",
                        "append_text": "x\n",
                    },
                )
            finally:
                builder.SHARED_FILE_ROOTS = original_roots

            event_path = state_root / "serializer-session" / "shared-serializer-events.jsonl"
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(output["sharedFileUpdate"]["result"], "updated")
            self.assertEqual(output["sharedFileUpdate"]["resource_id"], f"shared-file:{target.resolve(strict=False)}")
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(mismatch["decision"], "block")
            self.assertEqual(outside["decision"], "block")
            self.assertTrue(any(event["event_type"] == "shared_file_update" for event in events))

    def test_merge_roster_agent_row_locked_preserves_concurrent_rows(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = Path(tmp) / "roster.json"
            roster_path.write_text(
                json.dumps([{"agent_id": "gate-prompt-formatter", "notes": "old"}], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            first = builder.merge_roster_agent_row_locked(
                roster_path,
                [],
                "gate-prompt-formatter",
                {"agent_id": "gate-prompt-formatter", "notes": "updated"},
            )
            second = builder.merge_roster_agent_row_locked(
                roster_path,
                first,
                "gate-task-creator",
                {"agent_id": "gate-task-creator", "notes": "added"},
            )

            rows = {row["agent_id"]: row for row in second}
            self.assertEqual(rows["gate-prompt-formatter"]["notes"], "updated")
            self.assertEqual(rows["gate-task-creator"]["notes"], "added")
            self.assertFalse(roster_path.with_name("roster.json.lock.d").exists())

    def test_append_jsonl_defaults_to_locked_atomic_append(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            thread_count = 8
            writes_per_thread = 25

            def write_events(thread_id: int) -> None:
                for item_id in range(writes_per_thread):
                    builder.append_jsonl(event_path, {"thread_id": thread_id, "item_id": item_id})

            threads = [threading.Thread(target=write_events, args=(thread_id,)) for thread_id in range(thread_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
            observed = {(record["thread_id"], record["item_id"]) for record in records}

            self.assertEqual(len(records), thread_count * writes_per_thread)
            self.assertEqual(len(observed), thread_count * writes_per_thread)
            self.assertFalse(event_path.with_name("events.jsonl.lock.d").exists())

    def test_pretooluse_guard_blocks_raw_tmux_send_to_itb_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = self.run_builder(
                Path(tmp),
                "pretooluse-guard",
                {
                    "session_id": "guard-session",
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "tmux send-keys -t itb-org-test:gate-prompt-formatter.0 Enter",
                    },
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertEqual(output["pretooluseGuard"]["result"], "blocked_raw_itb_tmux_send")
            self.assertEqual(output["pretooluseGuard"]["itb_tmux_targets"], ["itb-org-test:gate-prompt-formatter.0"])

    def test_pretooluse_guard_allows_non_itb_tmux_and_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            non_itb = self.run_builder(
                state_root,
                "pretooluse-guard",
                {
                    "session_id": "guard-session",
                    "tool_input": {"command": "tmux send-keys -t scratch:0 Enter"},
                },
            )
            override = self.run_builder(
                state_root,
                "pretooluse-guard",
                {
                    "session_id": "guard-session",
                    "allow_itb_tmux_send": True,
                    "tool_input": {"command": "tmux paste-buffer -t itb-org-test:gate-prompt-formatter.0"},
                },
            )

            self.assertNotIn("decision", non_itb)
            self.assertEqual(non_itb["pretooluseGuard"]["result"], "allowed")
            self.assertNotIn("decision", override)
            self.assertEqual(override["pretooluseGuard"]["itb_tmux_targets"], ["itb-org-test:gate-prompt-formatter.0"])

    def test_hook_wrapper_final_response_guard_skips_without_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ITB_STATE_ROOT"] = tmp
            env["ITB_RUNTIME"] = "codex"
            completed = subprocess.run(
                ["bash", str(HOOK_ROOT / "itb-final-response-guard.sh")],
                input=json.dumps({"session_id": "hook-session"}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=True,
            )

            output = json.loads(completed.stdout)
            self.assertEqual(output["permissionDecision"], "allow")
            self.assertEqual(output["finalResponseGuard"]["result"], "skipped_no_active_pre_final_task")
            self.assertTrue((Path(tmp) / "hook-session" / "final-response-guard-events.jsonl").exists())

    def test_hook_wrapper_pretooluse_guard_blocks_raw_itb_tmux_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ITB_STATE_ROOT"] = tmp
            env["ITB_RUNTIME"] = "codex"
            completed = subprocess.run(
                ["bash", str(HOOK_ROOT / "itb-pretooluse-guard.sh")],
                input=json.dumps(
                    {
                        "session_id": "hook-session",
                        "tool_input": {"command": "tmux send-keys -t itb-org-test:gate-prompt-formatter.0 Enter"},
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=True,
            )

            output = json.loads(completed.stdout)
            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertEqual(output["pretooluseGuard"]["result"], "blocked_raw_itb_tmux_send")

    def test_hook_bundle_scripts_map_to_expected_builder_commands(self) -> None:
        expected = {
            "itb-session-start.sh": "itb_run_builder session-start --launch-agents",
            "itb-prompt-preflight.sh": "itb_run_builder prompt-preflight",
            "itb-final-response-guard.sh": "itb_run_builder final-response-guard",
            "itb-pretooluse-guard.sh": "itb_run_builder pretooluse-guard",
            "itb-session-end.sh": "itb_run_builder session-end",
        }
        for script_name, command in expected.items():
            script_path = HOOK_ROOT / script_name
            self.assertTrue(script_path.exists(), script_name)
            self.assertTrue(os.access(script_path, os.X_OK), script_name)
            text = script_path.read_text(encoding="utf-8")
            self.assertIn(command, text)
            self.assertIn("itb-hook-common.sh", text)
        final_guard = (HOOK_ROOT / "itb-final-response-guard.sh").read_text(encoding="utf-8")
        self.assertNotIn("archive-shutdown", final_guard)

    def test_hook_settings_examples_reference_canonical_wrappers(self) -> None:
        codex_hooks = json.loads((HOOK_ROOT / "codex-hooks.example.json").read_text(encoding="utf-8"))
        claude_hooks = json.loads((HOOK_ROOT / "claude-settings-hooks.example.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(codex_hooks["hooks"]),
            {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"},
        )
        self.assertIn("SubagentStop", claude_hooks["hooks"])
        codex_commands = "\n".join(
            hook["command"]
            for event_hooks in codex_hooks["hooks"].values()
            for entry in event_hooks
            for hook in entry["hooks"]
        )
        claude_commands = "\n".join(
            hook["command"]
            for event_hooks in claude_hooks["hooks"].values()
            for entry in event_hooks
            for hook in entry["hooks"]
        )
        self.assertIn("ITB_RUNTIME=codex", codex_commands)
        self.assertIn('ITB_STATE_ROOT="$HOME/.codex/state/itb"', codex_commands)
        self.assertIn("ITB_RUNTIME=claude", claude_commands)
        self.assertIn('ITB_STATE_ROOT="$HOME/.claude/state/itb"', claude_commands)
        self.assertNotIn("ITB_RUNTIME=codex", claude_commands)
        expected_by_event = {
            "SessionStart": "itb-session-start.sh",
            "UserPromptSubmit": "itb-prompt-preflight.sh",
            "PreToolUse": "itb-pretooluse-guard.sh",
            "Stop": "itb-final-response-guard.sh",
            "SubagentStop": "itb-final-response-guard.sh",
        }
        for sample in (codex_hooks, claude_hooks):
            serialized = json.dumps(sample, sort_keys=True)
            self.assertNotIn("archive-shutdown", serialized)
            self.assertNotIn("itb-session-end.sh", serialized)
            for event, script_name in expected_by_event.items():
                if event not in sample["hooks"]:
                    continue
                event_text = json.dumps(sample["hooks"][event], sort_keys=True)
                self.assertIn(script_name, event_text)
                self.assertTrue((HOOK_ROOT / script_name).exists())
        codex_config = (HOOK_ROOT / "codex-config.example.toml").read_text(encoding="utf-8")
        self.assertIn("codex_hooks = true", codex_config)

    def test_hook_install_updates_codex_symlinked_dotfiles_without_breaking_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            dotfiles = root / "dotfiles" / "codex"
            hooks_dir = dotfiles / "hooks"
            (home / ".codex").mkdir(parents=True)
            hooks_dir.mkdir(parents=True)
            dotfiles.mkdir(parents=True, exist_ok=True)
            hooks_target = dotfiles / "hooks.json"
            config_target = dotfiles / "config.toml"
            hooks_target.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "matcher": "startup|resume|clear",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": str(hooks_dir / "itb-session-start.sh"),
                                            "timeout": 30,
                                        }
                                    ],
                                }
                            ],
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": str(hooks_dir / "itb-prompt-preflight.sh"),
                                            "timeout": 30,
                                        }
                                    ]
                                }
                            ],
                            "SessionEnd": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": str(hooks_dir / "itb-session-end.sh"),
                                            "timeout": 30,
                                        }
                                    ]
                                }
                            ],
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            config_target.write_text("[features]\njs_repl = false\n", encoding="utf-8")
            (home / ".codex" / "hooks.json").symlink_to(hooks_target)
            (home / ".codex" / "config.toml").symlink_to(config_target)

            output = self.run_builder(
                root / "state",
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "hooks_dir": str(hooks_dir),
                    "apply": True,
                },
            )

            self.assertEqual(output["hookInstall"]["result"], "updated")
            self.assertFalse(output["hookInstall"]["dry_run"])
            self.assertTrue((home / ".codex" / "hooks.json").is_symlink())
            self.assertTrue((home / ".codex" / "config.toml").is_symlink())
            installed_hooks = json.loads(hooks_target.read_text(encoding="utf-8"))
            self.assertIn("PreToolUse", installed_hooks["hooks"])
            self.assertIn("Stop", installed_hooks["hooks"])
            self.assertIn("SessionEnd", installed_hooks["hooks"])
            session_start = installed_hooks["hooks"]["SessionStart"][0]
            self.assertEqual(session_start["matcher"], "startup|resume|clear|compact")
            self.assertEqual(session_start["hooks"][0]["timeout"], 30)
            self.assertIn("ITB_RUNTIME=codex", session_start["hooks"][0]["command"])
            self.assertIn(f'ITB_BUILDER="{BUILDER}"', session_start["hooks"][0]["command"])
            self.assertIn(str(hooks_dir / "itb-session-start.sh"), session_start["hooks"][0]["command"])
            session_end = installed_hooks["hooks"]["SessionEnd"][0]
            self.assertIn("ITB_RUNTIME=codex", session_end["hooks"][0]["command"])
            self.assertIn(f'ITB_BUILDER="{BUILDER}"', session_end["hooks"][0]["command"])
            self.assertIn(str(hooks_dir / "itb-session-end.sh"), session_end["hooks"][0]["command"])
            stop_command = installed_hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
            completed = subprocess.run(
                ["bash", "-lc", stop_command],
                input=json.dumps({"session_id": "installed-temp-final-guard"}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=os.environ | {"HOME": str(home)},
                check=True,
            )
            self.assertEqual(json.loads(completed.stdout)["finalResponseGuard"]["result"], "skipped_no_active_pre_final_task")
            config_text = config_target.read_text(encoding="utf-8")
            self.assertIn("codex_hooks = true", config_text)
            self.assertIn("js_repl = false", config_text)
            self.assertIn("itb_run_builder session-start --launch-agents", (hooks_dir / "itb-session-start.sh").read_text(encoding="utf-8"))
            self.assertIn("itb_run_builder pretooluse-guard", (hooks_dir / "itb-pretooluse-guard.sh").read_text(encoding="utf-8"))

    def test_hook_health_check_validates_installed_codex_hooks_with_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"

            install_output = self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            self.assertEqual(install_output["hookInstall"]["result"], "updated")

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "run_smoke": True,
                    "smoke_state_root": str(root / "smoke-state"),
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "pass")
            self.assertNotIn("decision", health)
            self.assertEqual(health["hookHealthCheck"]["smoke_state_root"], str(root / "smoke-state"))
            self.assertEqual(
                health["hookHealthCheck"]["smoke_scripts"],
                ["itb-final-response-guard.sh", "itb-pretooluse-guard.sh"],
            )
            self.assertEqual(health["hookHealthCheck"]["checked_count"], 4)
            self.assertEqual(health["hookHealthCheck"]["smoke_count"], 2)
            checks = {(item["event"], item["script"]): item for item in health["hookHealthCheck"]["checks"]}
            self.assertEqual(checks[("SessionStart", "itb-session-start.sh")]["builder_path"], str(BUILDER))
            self.assertEqual(checks[("PreToolUse", "itb-pretooluse-guard.sh")]["result"], "pass")
            smoke = {item["script"]: item for item in health["hookHealthCheck"]["smoke_results"]}
            self.assertEqual(smoke["itb-final-response-guard.sh"]["stdout"]["permissionDecision"], "allow")
            self.assertEqual(smoke["itb-pretooluse-guard.sh"]["stdout"]["pretooluseGuard"]["result"], "allowed")
            self.assertEqual(smoke["itb-final-response-guard.sh"]["state_root"], str(root / "smoke-state"))
            self.assertTrue((root / "smoke-state" / "hook-health-check-final-guard" / "final-response-guard-events.jsonl").exists())
            self.assertTrue((state_root / "health-session" / "hook-health-check-events.jsonl").exists())

    def test_hook_health_check_can_require_codex_hook_trust_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"

            install_output = self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            self.assertEqual(install_output["hookInstall"]["result"], "updated")

            hooks_path = home / ".codex" / "hooks.json"
            config_path = home / ".codex" / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + f'\n[hooks.state."{hooks_path}:session_start:0:0"]\n'
                + 'trusted_hash = "sha256:session-start"\n'
                + "enabled = true\n"
                + f'\n[hooks.state."{hooks_path}:user_prompt_submit:0:0"]\n'
                + 'trusted_hash = "sha256:user-prompt-submit"\n'
                + "enabled = true\n",
                encoding="utf-8",
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "require_hook_trust_state": True,
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "block")
            self.assertEqual(health["decision"], "block")
            self.assertIn("SessionStart:hook_trust_state_present_unverified", health["reason"])
            self.assertIn("UserPromptSubmit:hook_trust_state_present_unverified", health["reason"])
            self.assertIn("PreToolUse:hook_trust_state_missing", health["reason"])
            self.assertIn("Stop:hook_trust_state_missing", health["reason"])
            trust = health["hookHealthCheck"]["hook_trust_state"]
            self.assertEqual(trust["result"], "block")
            self.assertEqual(trust["verified_events"], [])
            self.assertEqual(trust["present_unverified_events"], ["SessionStart", "UserPromptSubmit"])
            self.assertEqual(trust["missing_events"], ["PreToolUse", "Stop"])
            self.assertEqual(
                health["hookHealthCheck"]["next_action"],
                "resolve_codex_hook_trust_state_and_rerun_hook_health_check",
            )
            self.assertEqual(
                health["hookHealthCheck"]["llm_dispatch_policy"],
                "skip_llm_dispatch_until_hook_trust_state_verified",
            )
            self.assertTrue(health["hookHealthCheck"]["remediation_required"])
            self.assertTrue(health["hookHealthCheck"]["operator_action_required"])
            self.assertIn("codex_hook_trust_state", health["hookHealthCheck"]["remediation_categories"])
            self.assertNotIn("hook_installation", health["hookHealthCheck"]["remediation_categories"])
            remediation = health["hookHealthCheck"]["remediation"]
            self.assertEqual(
                remediation["codex_hook_trust_state"]["action"],
                "accept_or_refresh_codex_hook_trust_entries_then_rerun_hook_health_check",
            )
            self.assertEqual(
                remediation["codex_hook_trust_state"]["present_unverified_events"],
                ["SessionStart", "UserPromptSubmit"],
            )
            self.assertEqual(remediation["codex_hook_trust_state"]["missing_events"], ["PreToolUse", "Stop"])
            self.assertEqual(remediation["hook_installation"]["blocked_checks"], [])
            self.assertEqual(remediation["hook_installation"]["action"], "no_action_required")
            checks = {item["event"]: item for item in health["hookHealthCheck"]["checks"]}
            self.assertEqual(checks["SessionStart"]["hook_trust_state_result"], "present_unverified")
            self.assertEqual(checks["PreToolUse"]["hook_trust_state_result"], "missing")

    def test_hook_health_check_reads_resolved_codex_hook_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_home = root / "real-home"
            home = root / "home"
            state_root = root / "state"
            real_home.mkdir()
            home.symlink_to(real_home, target_is_directory=True)

            install_output = self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            self.assertEqual(install_output["hookInstall"]["result"], "updated")

            hooks_path = home / ".codex" / "hooks.json"
            resolved_hooks_path = hooks_path.resolve(strict=False)
            config_path = home / ".codex" / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + f'\n[hooks.state."{resolved_hooks_path}:session_start:0:0"]\n'
                + 'trusted_hash = "sha256:session-start"\n'
                + "enabled = true\n",
                encoding="utf-8",
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                },
            )

            checks = {item["event"]: item for item in health["hookHealthCheck"]["checks"]}
            self.assertEqual(checks["SessionStart"]["hook_trust_state_result"], "present_unverified")
            self.assertEqual(checks["SessionStart"]["hook_trust_state_key"], f"{resolved_hooks_path}:session_start:0:0")

    def test_codex_hook_state_parser_resets_after_hook_state_table(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                '[hooks.state."/tmp/hooks.json:session_start:0:0"]\n'
                'trusted_hash = "sha256:session-start"\n'
                "enabled = true\n"
                "\n[features]\n"
                "enabled = false\n",
                encoding="utf-8",
            )

            entries, issues = builder.read_codex_hook_state_entries(config_path)

            self.assertEqual(issues, [])
            self.assertTrue(entries["/tmp/hooks.json:session_start:0:0"]["enabled"])
            self.assertEqual(entries["/tmp/hooks.json:session_start:0:0"]["trusted_hash"], "sha256:session-start")

    def test_hook_health_check_smokes_startup_and_preflight_in_temp_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            smoke_state_root = root / "smoke-state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "run_smoke": True,
                    "smoke_scripts": ["startup_preflight"],
                    "smoke_state_root": str(smoke_state_root),
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "pass")
            self.assertEqual(
                health["hookHealthCheck"]["smoke_scripts"],
                ["itb-session-start.sh", "itb-prompt-preflight.sh"],
            )
            self.assertEqual(health["hookHealthCheck"]["smoke_count"], 2)
            smoke = {item["script"]: item for item in health["hookHealthCheck"]["smoke_results"]}
            session_smoke = smoke["itb-session-start.sh"]
            preflight_smoke = smoke["itb-prompt-preflight.sh"]
            self.assertEqual(
                session_smoke["stdout"]["hookSpecificOutput"]["hookEventName"],
                "SessionStart",
            )
            self.assertEqual(
                preflight_smoke["stdout"]["hookSpecificOutput"]["hookEventName"],
                "UserPromptSubmit",
            )
            self.assertEqual(preflight_smoke["preflight_result"], "preflight_micro_fast_path")
            self.assertEqual(preflight_smoke["micro_fast_path_status"], "pass")
            session_dir = smoke_state_root / "hook-health-check-startup-preflight"
            bootstrap = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            self.assertTrue(bootstrap["launch_dry_run"])
            self.assertEqual(bootstrap["queue_watch_daemon_autostart"]["result"], "skipped_dry_run")
            self.assertEqual(bootstrap["interactive_readiness_followup_autostart"]["result"], "skipped_dry_run")
            self.assertFalse((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").exists())

    def test_hook_health_check_requires_temp_state_for_startup_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "run_smoke": True,
                    "smoke_scripts": ["SessionStart"],
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "block")
            self.assertEqual(health["decision"], "block")
            self.assertIn("smoke_state_root_required", health["reason"])
            self.assertEqual(
                health["hookHealthCheck"]["next_action"],
                "repair_hook_smoke_failures_and_rerun_hook_health_check",
            )
            self.assertEqual(
                health["hookHealthCheck"]["llm_dispatch_policy"],
                "skip_llm_dispatch_until_hook_health_passes",
            )
            self.assertTrue(health["hookHealthCheck"]["remediation_required"])
            self.assertTrue(health["hookHealthCheck"]["runtime_action_required"])
            self.assertFalse(health["hookHealthCheck"]["operator_action_required"])
            self.assertIn("hook_smoke", health["hookHealthCheck"]["remediation_categories"])
            self.assertNotIn("hook_installation", health["hookHealthCheck"]["remediation_categories"])
            remediation = health["hookHealthCheck"]["remediation"]
            self.assertEqual(
                remediation["hook_smoke"]["action"],
                "repair_hook_smoke_failures_and_rerun_hook_health_check",
            )
            self.assertEqual(remediation["hook_installation"]["action"], "no_action_required")

    def test_hook_health_check_requires_session_start_before_prompt_preflight_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "run_smoke": True,
                    "smoke_events": ["UserPromptSubmit"],
                    "smoke_state_root": str(root / "smoke-state"),
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "block")
            self.assertEqual(health["decision"], "block")
            self.assertIn("session_start_smoke_required", health["reason"])
            smoke = health["hookHealthCheck"]["smoke_results"][0]
            self.assertEqual(smoke["script"], "itb-prompt-preflight.sh")
            self.assertEqual(smoke["result"], "block")
            self.assertEqual(smoke["returncode"], None)
            self.assertIn("hook_smoke", health["hookHealthCheck"]["remediation_categories"])
            self.assertNotIn("hook_installation", health["hookHealthCheck"]["remediation_categories"])
            self.assertEqual(
                health["hookHealthCheck"]["remediation"]["hook_smoke"]["blocked_smoke_scripts"],
                [{"script": "itb-prompt-preflight.sh", "issues": ["session_start_smoke_required"]}],
            )
            self.assertFalse((root / "smoke-state" / "hook-health-check-startup-preflight" / "preflight-events.jsonl").exists())

    def test_hook_health_check_reports_missing_live_evidence_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "check_live_evidence": True,
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "pass")
            self.assertNotIn("decision", health)
            self.assertEqual(health["hookHealthCheck"]["next_action"], "hook_health_ready")
            self.assertEqual(health["hookHealthCheck"]["llm_dispatch_policy"], "allow_runtime_dispatch")
            self.assertFalse(health["hookHealthCheck"]["remediation_required"])
            live = health["hookHealthCheck"]["live_evidence"]
            self.assertEqual(live["result"], "missing")
            self.assertEqual(live["state_root"], str(home / ".codex" / "state" / "itb"))
            self.assertEqual(live["state_root_source"], "hook_settings:ITB_STATE_ROOT")
            self.assertFalse(live["state_root_exists"])
            self.assertEqual(live["required_events"], [])
            self.assertIn("SessionStart", live["missing_events"])
            self.assertIn("UserPromptSubmit", live["missing_events"])

    def test_hook_health_check_can_require_live_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "check_live_evidence": True,
                    "require_live_evidence": True,
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "block")
            self.assertEqual(health["decision"], "block")
            self.assertIn("live_evidence:SessionStart:missing", health["reason"])
            self.assertIn("live_evidence:UserPromptSubmit:missing", health["reason"])
            live = health["hookHealthCheck"]["live_evidence"]
            self.assertEqual(live["result"], "block")
            self.assertEqual(live["required_events"], ["SessionStart", "UserPromptSubmit"])
            self.assertEqual(live["required_missing_events"], ["SessionStart", "UserPromptSubmit"])
            self.assertEqual(
                health["hookHealthCheck"]["next_action"],
                "trigger_required_hook_events_and_rerun_hook_health_check",
            )
            self.assertEqual(
                health["hookHealthCheck"]["llm_dispatch_policy"],
                "skip_llm_dispatch_until_live_hook_evidence_observed",
            )
            self.assertTrue(health["hookHealthCheck"]["runtime_action_required"])
            self.assertIn("live_evidence", health["hookHealthCheck"]["remediation_categories"])
            self.assertNotIn("hook_installation", health["hookHealthCheck"]["remediation_categories"])
            remediation = health["hookHealthCheck"]["remediation"]
            self.assertEqual(
                remediation["live_evidence"]["action"],
                "trigger_or_restart_runtime_until_required_hook_events_are_observed_then_rerun_hook_health_check",
            )
            self.assertEqual(remediation["live_evidence"]["required_missing_events"], ["SessionStart", "UserPromptSubmit"])

    def test_hook_health_check_strict_live_evidence_allows_optional_missing_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            live_state_root = home / ".codex" / "state" / "itb"
            session_dir = live_state_root / "live-session"
            session_dir.mkdir(parents=True)
            (live_state_root / "last-session").write_text("live-session\n", encoding="utf-8")
            (session_dir / "status").write_text("ready\n", encoding="utf-8")
            (session_dir / "bootstrap.json").write_text(
                json.dumps({"bootstrap_status": "ready", "session_id": "live-session"}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (session_dir / "invocation-evidence.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-06-14T00:00:00Z",
                        "event_type": "session_start",
                        "session_id": "live-session",
                        "result": "ready",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            (session_dir / "preflight-events.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-06-14T00:00:01Z",
                        "event_type": "prompt_preflight",
                        "session_id": "live-session",
                        "result": "preflight_ready",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "check_live_evidence": True,
                    "require_live_evidence": True,
                    "max_live_evidence_age_seconds": 60,
                    "live_evidence_now": "2026-06-14T00:00:30+00:00",
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "pass")
            self.assertNotIn("decision", health)
            live = health["hookHealthCheck"]["live_evidence"]
            self.assertEqual(live["result"], "pass")
            self.assertEqual(live["max_age_seconds"], 60.0)
            self.assertEqual(live["required_events"], ["SessionStart", "UserPromptSubmit"])
            self.assertEqual(live["required_missing_events"], [])
            self.assertEqual(live["required_stale_events"], [])
            self.assertEqual(live["observed_count"], 2)
            self.assertIn("PreToolUse", live["missing_events"])
            self.assertIn("Stop", live["missing_events"])
            evidence = {item["event"]: item for item in live["events"]}
            self.assertEqual(evidence["SessionStart"]["freshness_result"], "fresh")
            self.assertEqual(evidence["UserPromptSubmit"]["freshness_result"], "fresh")

    def test_hook_health_check_strict_live_evidence_blocks_stale_required_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            live_state_root = home / ".codex" / "state" / "itb"
            session_dir = live_state_root / "live-session"
            session_dir.mkdir(parents=True)
            (live_state_root / "last-session").write_text("live-session\n", encoding="utf-8")
            (session_dir / "status").write_text("ready\n", encoding="utf-8")
            (session_dir / "bootstrap.json").write_text(
                json.dumps({"bootstrap_status": "ready", "session_id": "live-session"}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (session_dir / "invocation-evidence.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-06-14T00:00:00+00:00",
                        "event_type": "session_start",
                        "session_id": "live-session",
                        "result": "ready",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            (session_dir / "preflight-events.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-06-14T00:00:01+00:00",
                        "event_type": "prompt_preflight",
                        "session_id": "live-session",
                        "result": "preflight_ready",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "check_live_evidence": True,
                    "require_live_evidence": True,
                    "max_live_evidence_age_seconds": 60,
                    "live_evidence_now": "2026-06-14T00:05:00+00:00",
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "block")
            self.assertEqual(health["decision"], "block")
            self.assertIn("live_evidence:SessionStart:stale", health["reason"])
            self.assertIn("live_evidence:UserPromptSubmit:stale", health["reason"])
            live = health["hookHealthCheck"]["live_evidence"]
            self.assertEqual(live["result"], "block")
            self.assertEqual(live["required_stale_events"], ["SessionStart", "UserPromptSubmit"])
            self.assertEqual(
                health["hookHealthCheck"]["next_action"],
                "trigger_required_hook_events_and_rerun_hook_health_check",
            )
            self.assertEqual(
                health["hookHealthCheck"]["llm_dispatch_policy"],
                "skip_llm_dispatch_until_live_hook_evidence_observed",
            )
            self.assertIn("live_evidence", health["hookHealthCheck"]["remediation_categories"])
            remediation = health["hookHealthCheck"]["remediation"]
            self.assertEqual(remediation["live_evidence"]["required_stale_events"], ["SessionStart", "UserPromptSubmit"])
            evidence = {item["event"]: item for item in live["events"]}
            self.assertEqual(evidence["SessionStart"]["freshness_result"], "stale")
            self.assertGreater(evidence["SessionStart"]["age_seconds"], 60)

    def test_hook_health_check_reports_live_session_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            live_state_root = home / ".codex" / "state" / "itb"
            session_dir = live_state_root / "live-session"
            session_dir.mkdir(parents=True)
            (live_state_root / "last-session").write_text("live-session\n", encoding="utf-8")
            (session_dir / "status").write_text("ready\n", encoding="utf-8")
            (session_dir / "bootstrap.json").write_text(
                json.dumps({"bootstrap_status": "ready", "session_id": "live-session"}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (session_dir / "last_event").write_text("UserPromptSubmit 2026-06-14T00:00:01Z\n", encoding="utf-8")
            event_rows = {
                "invocation-evidence.jsonl": {
                    "ts": "2026-06-14T00:00:00Z",
                    "event_type": "session_start",
                    "session_id": "live-session",
                    "result": "ready",
                },
                "preflight-events.jsonl": {
                    "ts": "2026-06-14T00:00:01Z",
                    "event_type": "prompt_preflight",
                    "session_id": "live-session",
                    "result": "preflight_micro_fast_path",
                },
                "pretooluse-guard-events.jsonl": {
                    "ts": "2026-06-14T00:00:02Z",
                    "event_type": "pretooluse_guard",
                    "session_id": "live-session",
                    "result": "allowed",
                },
                "final-response-guard-events.jsonl": {
                    "ts": "2026-06-14T00:00:03Z",
                    "event_type": "final_response_guard",
                    "session_id": "live-session",
                    "result": "skipped_no_active_pre_final_task",
                },
            }
            for file_name, row in event_rows.items():
                (session_dir / file_name).write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "check_live_evidence": True,
                    "require_live_evidence": True,
                    "required_live_events": ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"],
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "pass")
            self.assertNotIn("decision", health)
            live = health["hookHealthCheck"]["live_evidence"]
            self.assertEqual(live["result"], "pass")
            self.assertEqual(live["session_id"], "live-session")
            self.assertEqual(live["observed_count"], 4)
            self.assertEqual(live["missing_events"], [])
            evidence = {item["event"]: item for item in live["events"]}
            self.assertEqual(evidence["SessionStart"]["source"], "invocation-evidence")
            self.assertEqual(evidence["UserPromptSubmit"]["source"], "preflight-events")
            self.assertEqual(evidence["PreToolUse"]["event_result"], "allowed")
            self.assertEqual(evidence["Stop"]["event_result"], "skipped_no_active_pre_final_task")

    def test_hook_health_check_blocks_copied_wrappers_without_builder_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            self.run_builder(
                state_root,
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "apply": True,
                },
            )
            hooks_path = home / ".codex" / "hooks.json"
            settings = json.loads(hooks_path.read_text(encoding="utf-8"))
            for event_hooks in settings["hooks"].values():
                for entry in event_hooks:
                    for hook in entry["hooks"]:
                        hook["command"] = hook["command"].replace(f'ITB_BUILDER="{BUILDER}" ', "")
            hooks_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            health = self.run_builder(
                state_root,
                "hook-health-check",
                {
                    "session_id": "health-session",
                    "home_dir": str(home),
                    "check_live_evidence": True,
                    "require_live_evidence": True,
                },
            )

            self.assertEqual(health["hookHealthCheck"]["result"], "block")
            self.assertEqual(health["decision"], "block")
            self.assertIn("missing_itb_builder", health["reason"])
            self.assertIn("live_evidence:SessionStart:missing", health["reason"])
            self.assertEqual(
                health["hookHealthCheck"]["next_action"],
                "repair_hook_installation_and_rerun_hook_health_check",
            )
            self.assertEqual(
                health["hookHealthCheck"]["llm_dispatch_policy"],
                "skip_llm_dispatch_until_hook_health_passes",
            )
            self.assertTrue(health["hookHealthCheck"]["operator_action_required"])
            self.assertTrue(health["hookHealthCheck"]["runtime_action_required"])
            self.assertIn("hook_installation", health["hookHealthCheck"]["remediation_categories"])
            self.assertIn("live_evidence", health["hookHealthCheck"]["remediation_categories"])
            remediation = health["hookHealthCheck"]["remediation"]
            self.assertEqual(
                remediation["hook_installation"]["action"],
                "repair_hook_installation_and_rerun_hook_health_check",
            )
            blocked = [item for item in health["hookHealthCheck"]["checks"] if "missing_itb_builder" in item["issues"]]
            self.assertTrue(blocked)

    def test_hook_install_claude_dry_run_preserves_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            settings_path = home / ".claude" / "settings.json"
            hooks_dir = home / ".claude" / "hooks"
            settings_path.parent.mkdir(parents=True)
            original_settings = {
                "hooks": {
                    "TeammateIdle": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo teammate-idle",
                                    "timeout": 1,
                                }
                            ]
                        }
                    ],
                    "SessionStart": [
                        {
                            "matcher": "startup|resume|clear",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(hooks_dir / "itb-session-start.sh"),
                                    "timeout": 30,
                                }
                            ],
                        }
                    ],
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(hooks_dir / "itb-session-end.sh"),
                                    "timeout": 30,
                                }
                            ]
                        }
                    ],
                }
            }
            settings_path.write_text(json.dumps(original_settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                root / "state",
                "hook-install",
                {
                    "session_id": "install-session",
                    "home_dir": str(home),
                    "hooks_dir": str(hooks_dir),
                    "dry_run": True,
                },
                runtime="claude",
            )

            self.assertEqual(output["hookInstall"]["result"], "dry_run")
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), original_settings)
            self.assertFalse((hooks_dir / "itb-final-response-guard.sh").exists())
            logical_changes = []
            for change in output["hookInstall"]["planned_changes"]:
                logical_changes.extend(change.get("logical_changes", []))
            added_events = {change.get("event") for change in logical_changes if change.get("action") == "add_hook_event"}
            self.assertIn("PreToolUse", added_events)
            self.assertIn("Stop", added_events)
            self.assertIn("SubagentStop", added_events)
            updated_events = {change.get("event") for change in logical_changes if change.get("action") == "update_command"}
            self.assertIn("SessionEnd", updated_events)

    def test_role_queue_non_dry_run_does_not_claim_nudge_without_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "tech-lead",
                    "from_role": "infra-director",
                    "task_id": "TSK-9999",
                    "message_id": "msg-non-dry",
                    "instruction": "queued without consumer",
                },
            )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "queue_consumer_unavailable")
            self.assertFalse(summary["nudge"]["sent"])

    def test_role_queue_nudge_uses_buffer_submit(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            with mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload, mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ) as wait_ack:
                output = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "from_role": "codex-user-prompt-submit",
                        "task_id": "ENTRY-test-session-submit",
                        "message_id": "msg-submit",
                        "report_id": "rep-submit",
                        "instruction": "Create a thin envelope.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "nudge_sent_ack_deferred")
            self.assertTrue(summary["nudge"]["ack_deferred"])
            self.assertEqual(summary["nudge"]["ack_owner"], "queue-watch-daemon")
            self.assertEqual(summary["nudge"]["submit_enter_count"], 2)
            target, prompt = send_payload.call_args.args[:2]
            self.assertTrue(target.endswith(":gate-prompt-formatter.0"))
            self.assertIn("role_id: gate-prompt-formatter", prompt)
            self.assertIn("role-report --runtime codex", prompt)
            self.assertIn("--role-id gate-prompt-formatter", prompt)
            self.assertIn("--message-id msg-submit", prompt)
            self.assertEqual(send_payload.call_args.kwargs["submit_enter_count"], 2)
            wait_ack.assert_not_called()

    def test_role_queue_activates_lazy_queue_consumer_before_nudge(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)
            launch_result = self.provider_ready_launch_result("gate-task-evaluator")

            with mock.patch.dict(os.environ, {"ITB_ROLE_QUEUE_ACTIVATE_PROVIDER": "1"}, clear=False), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ) as ensure_provider, mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload:
                output = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-task-evaluator",
                        "from_role": "teams-project-manager",
                        "task_id": "TSK-9999",
                        "message_id": "msg-activate",
                        "report_id": "rep-activate",
                        "instruction": "Evaluate the completed task.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["nudge"]["result"], "nudge_sent_ack_deferred")
            self.assertEqual(summary["nudge"]["activation"]["result"], "provider_ready")
            self.assertEqual(summary["nudge"]["activation"]["launch_result"]["agent_id"], "gate-task-evaluator")
            self.assertEqual(
                summary["nudge"]["activation"]["prompt_readiness"]["state_update"]["resident_agents_prompt_ready"],
                1,
            )
            self.assertEqual(
                summary["nudge"]["activation"]["prompt_readiness"]["state_update"]["prompt_readiness_scope"],
                "interactive_ready",
            )
            ensure_provider.assert_called_once()
            launch_row = ensure_provider.call_args.kwargs["row"]
            self.assertEqual(launch_row["organization_instance_id"], builder.organization_id("test-session"))
            self.assertEqual(launch_row["parent_session_id"], "test-session")
            self.assertEqual(launch_row["agent_instance_id"], "gate-task-evaluator@test-session")
            self.assertEqual(launch_row["activation_status"], "metadata_ready")
            self.assertEqual(launch_row["runtime"], "codex")
            self.assertEqual(launch_row["state_root"], str(state_root))
            self.assertIn("test-session/queue", launch_row["queue_root"])
            self.assertEqual(ensure_provider.call_args.kwargs["tools"], "Read,Grep,Glob,Bash")
            target, prompt = send_payload.call_args.args[:2]
            self.assertTrue(target.endswith(":gate-task-evaluator.0"))
            self.assertIn("role_id: gate-task-evaluator", prompt)
            self.assertIn("--role-id gate-task-evaluator", prompt)
            self.assertIn("--message-id msg-activate", prompt)
            self.assertIn("--report-json", prompt)
            self.assertIn("Do not use stdin, heredoc, echo, printf, cat, or a pipe", prompt)
            self.assertNotIn("printf '%s\\n'", prompt)
            self.assertNotIn("| python3", prompt)
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows = {row["agent_id"]: row for row in roster}
            self.assertEqual(rows["gate-task-evaluator"]["process_status"], "process_ready")
            self.assertEqual(rows["gate-task-evaluator"]["launch_status"], "already_running")
            self.assertTrue(rows["gate-task-evaluator"]["interactive_ready"])
            self.assertEqual(rows["gate-task-evaluator"]["interactive_status"], "interactive_ready")
            self.assertEqual(rows["gate-task-evaluator"]["interactive_evidence_source"], "queue_activation_prompt_probe")
            self.assertEqual(rows["gate-task-evaluator"]["last_queue_activation_message_id"], "msg-activate")
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["nudge"]["activation"]["state_update"]["resident_agents_provider_ready"], 1)
            self.assertEqual(state["resident_agents_provider_ready"], 1)
            self.assertEqual(state["resident_agents_process_ready"], 1)
            self.assertEqual(state["resident_agents_interactive_ready"], 1)
            self.assertEqual(state["resident_agents_prompt_ready"], 1)
            self.assertEqual(state["prompt_readiness_scope"], "interactive_ready")
            self.assertEqual(state["last_queue_activation_role"], "gate-task-evaluator")
            self.assertEqual(state["last_queue_activation_message_id"], "msg-activate")
            self.assertEqual(state["last_queue_activation_prompt_check_role"], "gate-task-evaluator")
            self.assertEqual(state["last_queue_activation_prompt_check_message_id"], "msg-activate")

    def test_role_queue_can_defer_nudge_for_daemon_only_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-daemon-only-drain",
                    "message_id": "msg-daemon-only-drain",
                    "report_id": "rep-daemon-only-drain",
                    "instruction": "Write a minimal role report.",
                    "skip_auto_queue_handoff": True,
                    "defer_nudge": True,
                },
            )

            role_queue = queue_output["roleQueue"]
            self.assertEqual(role_queue["result"], "queued_nudge_unconfirmed")
            self.assertEqual(role_queue["nudge"]["result"], "nudge_deferred_by_input")
            self.assertFalse(role_queue["nudge"]["sent"])
            self.assertIn("queue-watch-daemon", role_queue["nudge"]["reason"])

            watch = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "dry_run": True,
                },
            )
            self.assertEqual(watch["queueWatch"]["pending_count"], 1)
            self.assertEqual(watch["queueWatch"]["nudged_count"], 1)
            self.assertTrue(watch["queueWatch"]["dry_run"])
            self.assertEqual(watch["queueWatch"]["messages"][0]["nudge"]["result"], "dry_run")

    def test_queue_watch_respects_nudge_cooldown_before_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-cooldown-drain",
                    "message_id": "msg-cooldown-drain",
                    "report_id": "rep-cooldown-drain",
                    "instruction": "Write a minimal role report.",
                    "skip_auto_queue_handoff": True,
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            inbox["messages"][0]["last_nudge_result"] = "nudge_send_unconfirmed"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            watch = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "dry_run": True,
                    "nudge_cooldown_seconds": 60,
                },
            )

            self.assertEqual(watch["queueWatch"]["result"], "nudge_cooldown")
            self.assertEqual(watch["queueWatch"]["pending_count"], 1)
            self.assertEqual(watch["queueWatch"]["nudged_count"], 0)
            self.assertEqual(watch["queueWatch"]["dead_letter_count"], 0)
            self.assertEqual(watch["queueWatch"]["cooldown_skipped_count"], 1)
            cooldown = watch["queueWatch"]["cooldown_skipped_messages"][0]["nudge"]
            self.assertEqual(cooldown["result"], "nudge_cooldown")
            self.assertTrue(cooldown["cooldown_active"])
            self.assertFalse(cooldown["future_clock_skew"])

    def test_queue_watch_future_last_nudged_at_does_not_permanently_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-future-cooldown",
                    "message_id": "msg-future-cooldown",
                    "report_id": "rep-future-cooldown",
                    "instruction": "Write a minimal role report.",
                    "skip_auto_queue_handoff": True,
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queue_output["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudged_at"] = "2999-01-01T00:00:00+00:00"
            inbox["messages"][0]["last_nudge_result"] = "nudge_send_unconfirmed"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            watch = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "dry_run": True,
                    "nudge_cooldown_seconds": 60,
                },
            )

            self.assertEqual(watch["queueWatch"]["result"], "dead_lettered")
            self.assertEqual(watch["queueWatch"]["cooldown_skipped_count"], 0)
            self.assertEqual(watch["queueWatch"]["dead_letter_count"], 1)

    def test_role_queue_defers_nudge_when_queue_activation_fails(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            launch_result = {
                "agent_id": "gate-task-evaluator",
                "process_status": "launch_failed",
                "launch_status": "claude_unavailable",
                "provider_status": "not_started",
                "tmux_target": "itb-org-test:gate-task-evaluator.0",
                "launch_error": "claude command not found",
            }

            with mock.patch.dict(os.environ, {"ITB_ROLE_QUEUE_ACTIVATE_PROVIDER": "1"}, clear=False), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, "claude command not found"),
            ) as ensure_provider, mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload:
                output = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-task-evaluator",
                        "from_role": "teams-project-manager",
                        "task_id": "TSK-9999",
                        "message_id": "msg-activation-failed",
                        "report_id": "rep-activation-failed",
                        "instruction": "Evaluate the completed task.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["nudge"]["result"], "nudge_deferred_provider_activation_failed")
            self.assertFalse(summary["nudge"]["sent"])
            self.assertEqual(summary["nudge"]["activation"]["result"], "activation_failed")
            self.assertIn("claude command not found", summary["nudge"]["error"])
            ensure_provider.assert_called_once()
            send_payload.assert_not_called()
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows = {row["agent_id"]: row for row in roster}
            self.assertEqual(rows["gate-task-evaluator"]["launch_status"], "claude_unavailable")
            self.assertEqual(rows["gate-task-evaluator"]["last_queue_activation_message_id"], "msg-activation-failed")

    def test_role_queue_nudge_writer_uses_claude_runtime(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "claude-session", "cwd": "/tmp", "source": "SessionStart"},
                runtime="claude",
            )

            with mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload, mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ) as wait_ack:
                output = builder.role_queue(
                    runtime="claude",
                    state_root=state_root,
                    hook_input={
                        "session_id": "claude-session",
                        "role_id": "gate-prompt-formatter",
                        "from_role": "claude-user-prompt-submit",
                        "task_id": "ENTRY-claude-session-submit",
                        "message_id": "msg-claude-submit",
                        "report_id": "rep-claude-submit",
                        "instruction": "Create a thin envelope.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "nudge_sent_ack_deferred")
            prompt = send_payload.call_args.args[1]
            self.assertIn("role-report --runtime claude", prompt)
            wait_ack.assert_not_called()

    def test_role_queue_defers_nudge_when_pending_message_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            first = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-first",
                    "message_id": "msg-first",
                    "report_id": "rep-first",
                    "instruction": "Create the first envelope.",
                },
                extra_args=["--dry-run"],
            )
            self.assertEqual(first["roleQueue"]["nudge"]["result"], "dry_run")

            second = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-second",
                    "message_id": "msg-second",
                    "report_id": "rep-second",
                    "instruction": "Create the second envelope.",
                },
            )

            summary = second["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "nudge_deferred_pending_message")
            self.assertEqual(summary["nudge"]["pending_message_count"], 1)
            self.assertEqual(summary["nudge"]["oldest_pending_message_id"], "msg-first")

    def test_role_queue_recovers_reported_pending_message_before_deferring_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            first = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-first",
                    "message_id": "msg-first",
                    "report_id": "rep-first",
                    "instruction": "Create the first envelope.",
                },
                extra_args=["--dry-run"],
            )
            first_report_path = Path(first["roleQueue"]["report_path"])
            first_report_path.parent.mkdir(parents=True, exist_ok=True)
            first_report_path.write_text(
                json.dumps(
                    {
                        "report_version": "1",
                        "report_type": "role_queue_report",
                        "from_role": "gate-prompt-formatter",
                        "message_id": "msg-first",
                        "created_at": "2026-01-01T00:00:00+09:00",
                        "result": "gate_intake_ready",
                        "status": "done",
                        "summary": "Recovered from existing report.",
                        "provider_evidence": {
                            "usage_source": "claude_tmux_interactive",
                            "effective_model": "claude-sonnet-4-6",
                            "provider_session_id": "sess-gpf",
                            "request_id": "req-gpf",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            second = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-second",
                    "message_id": "msg-second",
                    "report_id": "rep-second",
                    "instruction": "Create the second envelope.",
                },
                extra_args=["--dry-run"],
            )

            summary = second["roleQueue"]
            self.assertEqual(summary["result"], "queued")
            self.assertEqual(summary["nudge"]["result"], "dry_run")
            self.assertEqual(summary["nudge"]["recovered_pending_count"], 1)
            inbox = json.loads(Path(summary["inbox_path"]).read_text(encoding="utf-8"))
            messages_by_id = {item["message_id"]: item for item in inbox["messages"]}
            self.assertEqual(messages_by_id["msg-first"]["status"], "done")
            self.assertEqual(messages_by_id["msg-second"]["status"], "pending")

    def test_role_queue_defers_nudge_when_prompt_not_ready(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            with mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(False, "provider is busy"),
            ) as wait_ready, mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload:
                output = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "from_role": "codex-user-prompt-submit",
                        "task_id": "ENTRY-test-session-busy",
                        "message_id": "msg-busy",
                        "report_id": "rep-busy",
                        "instruction": "Create a thin envelope.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "nudge_deferred_prompt_not_ready")
            self.assertEqual(summary["nudge"]["error"], "provider is busy")
            wait_ready.assert_called_once()
            send_payload.assert_not_called()
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertFalse(gpf_row["interactive_ready"])
            self.assertEqual(gpf_row["interactive_status"], "prompt_busy")
            self.assertEqual(gpf_row["interactive_error"], "provider is busy")
            self.assertEqual(gpf_row["interactive_evidence_source"], "queue_activation_prompt_probe")
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            self.assertEqual(state["resident_agents_prompt_ready"], 0)
            self.assertEqual(state["prompt_readiness_scope"], "interactive_blocked")
            self.assertEqual(state["last_queue_activation_prompt_check_role"], "gate-prompt-formatter")
            self.assertEqual(state["last_queue_activation_prompt_check_message_id"], "msg-busy")

    def test_role_queue_classifies_prompt_ready_approval_as_approval_wait(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            with mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(False, "provider approval prompt is waiting for human approval"),
            ) as wait_ready, mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload:
                output = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "from_role": "codex-user-prompt-submit",
                        "task_id": "ENTRY-test-session-approval-busy",
                        "message_id": "msg-approval-busy",
                        "report_id": "rep-approval-busy",
                        "instruction": "Create a thin envelope.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "nudge_deferred_provider_approval")
            self.assertEqual(summary["nudge"]["provider_status"], "approval_wait")
            self.assertEqual(summary["nudge"]["error"], "provider approval prompt is waiting for human approval")
            wait_ready.assert_called_once()
            send_payload.assert_not_called()
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertFalse(gpf_row["interactive_ready"])
            self.assertEqual(gpf_row["interactive_status"], "blocked_provider_approval")
            self.assertEqual(gpf_row["interactive_evidence_source"], "queue_activation_prompt_probe")

    def test_role_queue_marks_nudge_unconfirmed_when_ack_missing(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            with mock.patch.dict(os.environ, {"ITB_ROLE_QUEUE_ACK_MODE": "recover"}, clear=False), mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload, mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(False, "ack marker missing"),
            ) as wait_ack, mock.patch.object(
                builder,
                "recover_unconfirmed_tmux_payload_send",
                return_value=(False, "ack marker missing", [{"action": "extra_enter", "result": "unconfirmed"}]),
            ) as recover_send:
                output = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "from_role": "codex-user-prompt-submit",
                        "task_id": "ENTRY-test-session-unconfirmed",
                        "message_id": "msg-unconfirmed",
                        "report_id": "rep-unconfirmed",
                        "instruction": "Create a thin envelope.",
                    },
                )

            summary = output["roleQueue"]
            self.assertEqual(summary["result"], "queued_nudge_unconfirmed")
            self.assertEqual(summary["nudge"]["result"], "nudge_send_unconfirmed")
            self.assertTrue(summary["nudge"]["sent"])
            self.assertEqual(summary["nudge"]["error"], "ack marker missing")
            self.assertEqual(summary["nudge"]["send_recovery"][0]["action"], "extra_enter")
            send_payload.assert_called_once()
            wait_ack.assert_called_once()
            recover_send.assert_called_once()

    def test_role_queue_blocks_unknown_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "missing-role",
                    "task_id": "TSK-9999",
                },
                extra_args=["--dry-run"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("role-agent registry has no active resident role", output["reason"])

    def test_queue_watch_nudges_pending_message_and_records_retry_metrics(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-watch",
                    "message_id": "msg-watch",
                    "report_id": "rep-watch",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "nudge_sent", "sent": True, "tmux_target": "itb-test:gate"},
            ) as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                    },
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "nudged")
            self.assertEqual(summary["pending_count"], 1)
            self.assertEqual(summary["nudged_count"], 1)
            nudge.assert_called_once()
            self.assertFalse(nudge.call_args.kwargs["dry_run"])
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0]["retry_count"], 1)
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_sent")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "watch_nudge" for item in metrics))

    def test_queue_watch_prompt_not_ready_does_not_increment_retry_or_dead_letter(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-provider-busy",
                    "message_id": "msg-provider-busy",
                    "report_id": "rep-provider-busy",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudge_result"] = "nudge_deferred_prompt_not_ready"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "nudge_deferred_prompt_not_ready", "sent": False, "error": "provider is busy"},
            ):
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "provider_busy")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 0)
            self.assertEqual(summary["nudged_count"], 0)
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0]["retry_count"], 5)
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_deferred_prompt_not_ready")
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(gpf_row["response_status"], "busy")
            self.assertEqual(gpf_row["provider_status"], "prompt_not_ready")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "watch_provider_busy" for item in metrics))

    def test_queue_watch_prompt_not_ready_sla_surfaces_sla_breach_result(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-provider-busy-sla",
                    "message_id": "msg-provider-busy-sla",
                    "report_id": "rep-provider-busy-sla",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudge_result"] = "nudge_deferred_prompt_not_ready"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "nudge_deferred_prompt_not_ready", "sent": False, "error": "provider is busy"},
            ):
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "sla_breached")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["sla_breach_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 0)
            self.assertEqual(summary["nudged_count"], 0)
            self.assertEqual(summary["sla_breaches"][0]["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_messages"][0]["notification_class"], "flow_alert")
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0]["retry_count"], 5)
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_deferred_prompt_not_ready")

    def test_queue_watch_initial_nudge_provider_approval_wait_is_provider_busy(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-initial-approval-wait",
                    "message_id": "msg-initial-approval-wait",
                    "report_id": "rep-initial-approval-wait",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={
                    "result": "nudge_deferred_provider_approval",
                    "sent": False,
                    "provider_status": "approval_wait",
                    "error": "provider approval prompt is waiting for human approval",
                },
            ):
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "provider_busy")
            self.assertEqual(summary["notification_class"], "approval_wait")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["sla_breach_count"], 1)
            self.assertEqual(summary["nudged_count"], 0)
            self.assertEqual(summary["provider_busy_messages"][0]["nudge"]["provider_status"], "approval_wait")
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_deferred_provider_approval")
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            sla_events = [
                item
                for item in events
                if item["event_type"] == "queue_sla_breach"
                and item["message_id"] == "msg-initial-approval-wait"
            ]
            self.assertEqual(len(sla_events), 1)
            self.assertEqual(sla_events[0]["notification_class"], "approval_wait")
            self.assertEqual(sla_events[0]["notification_result"], "approval_wait")

    def test_queue_watch_all_prompt_not_ready_sla_surfaces_sla_breach_result(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-provider-busy-sla-all",
                    "message_id": "msg-provider-busy-sla-all",
                    "report_id": "rep-provider-busy-sla-all",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudge_result"] = "nudge_deferred_prompt_not_ready"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "nudge_deferred_prompt_not_ready", "sent": False, "error": "provider is busy"},
            ):
                output = builder.role_queue_watch_all(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_ids": ["gate-prompt-formatter"]},
                )

            summary = output["queueWatchAll"]
            self.assertEqual(summary["result"], "sla_breached")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["sla_breach_count"], 1)
            self.assertEqual(summary["roles"][0]["result"], "sla_breached")
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            watch_all_events = [item for item in events if item["event_type"] == "queue_watch_all"]
            self.assertEqual(watch_all_events[-1]["result"], "sla_breached")
            self.assertEqual(watch_all_events[-1]["notification_class"], "flow_alert")

    def test_queue_watch_all_mixed_approval_and_prompt_not_ready_sla_surfaces_sla_breach(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            role_message_ids = {
                "gate-prompt-formatter": "msg-mixed-approval",
                "gate-task-creator": "msg-mixed-prompt-not-ready",
            }
            for role_id, message_id in role_message_ids.items():
                queued = self.run_builder(
                    state_root,
                    "role-queue",
                    {
                        "session_id": "test-session",
                        "role_id": role_id,
                        "from_role": "codex-user-prompt-submit",
                        "task_id": f"ENTRY-test-session-{message_id}",
                        "message_id": message_id,
                        "report_id": f"rep-{message_id}",
                        "instruction": "Create a thin envelope.",
                        "defer_nudge": True,
                    },
                )
                inbox_path = Path(queued["roleQueue"]["inbox_path"])
                inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
                inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
                inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            def nudge_for_message(_role_row, **kwargs):
                message_id = kwargs["message"]["message_id"]
                if message_id == "msg-mixed-approval":
                    return {
                        "result": "nudge_deferred_provider_approval",
                        "sent": False,
                        "provider_status": "approval_wait",
                        "error": "provider approval prompt is waiting for human approval",
                    }
                return {"result": "nudge_deferred_prompt_not_ready", "sent": False, "error": "provider is busy"}

            with mock.patch.object(builder, "nudge_role_agent", side_effect=nudge_for_message):
                output = builder.role_queue_watch_all(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": ["gate-prompt-formatter", "gate-task-creator"],
                    },
                )

            summary = output["queueWatchAll"]
            self.assertEqual(summary["result"], "sla_breached")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 2)
            self.assertEqual(summary["sla_breach_count"], 2)
            roles = {item["role_id"]: item for item in summary["roles"]}
            self.assertEqual(roles["gate-prompt-formatter"]["result"], "provider_busy")
            self.assertEqual(roles["gate-prompt-formatter"]["notification_class"], "approval_wait")
            self.assertEqual(roles["gate-task-creator"]["result"], "sla_breached")
            self.assertEqual(roles["gate-task-creator"]["notification_class"], "flow_alert")

    def test_queue_watch_all_approval_wait_plus_child_error_surfaces_flow_alert(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            approval_summary = {
                "result": "provider_busy",
                "role_id": "gate-prompt-formatter",
                "pending_count": 1,
                "recovered_count": 0,
                "dead_letter_count": 0,
                "nudged_count": 0,
                "sla_breach_count": 1,
                "cooldown_skipped_count": 0,
                "provider_busy_count": 1,
                "provider_busy_messages": [{"notification_class": "approval_wait"}],
                "notification_class": "approval_wait",
            }
            with mock.patch.object(
                builder,
                "role_queue_watch_once",
                side_effect=[
                    {"queueWatch": approval_summary},
                    {"decision": "block", "reason": "child failed"},
                ],
            ):
                output = builder.role_queue_watch_all(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": ["gate-prompt-formatter", "missing-role"],
                    },
                )

            summary = output["queueWatchAll"]
            self.assertEqual(summary["result"], "partial_error")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["errors"][0]["role_id"], "missing-role")

    def test_queue_watch_all_approval_wait_plus_dead_letter_surfaces_flow_alert(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            role_message_ids = {
                "gate-prompt-formatter": "msg-approval-with-dead-letter",
                "gate-task-creator": "msg-dead-letter-with-approval",
            }
            for role_id, message_id in role_message_ids.items():
                queued = self.run_builder(
                    state_root,
                    "role-queue",
                    {
                        "session_id": "test-session",
                        "role_id": role_id,
                        "from_role": "codex-user-prompt-submit",
                        "task_id": f"ENTRY-test-session-{message_id}",
                        "message_id": message_id,
                        "report_id": f"rep-{message_id}",
                        "instruction": "Create a thin envelope.",
                        "defer_nudge": True,
                    },
                )
                inbox_path = Path(queued["roleQueue"]["inbox_path"])
                inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
                inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
                if role_id == "gate-task-creator":
                    inbox["messages"][0]["retry_count"] = 5
                    inbox["messages"][0]["last_nudge_result"] = "nudge_send_unconfirmed"
                inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            def nudge_for_message(_role_row, **kwargs):
                if kwargs["message"]["message_id"] == "msg-approval-with-dead-letter":
                    return {
                        "result": "nudge_deferred_provider_approval",
                        "sent": False,
                        "provider_status": "approval_wait",
                        "error": "provider approval prompt is waiting for human approval",
                    }
                return {"result": "nudge_send_unconfirmed", "sent": False, "error": "not called for dead letter"}

            with mock.patch.object(builder, "nudge_role_agent", side_effect=nudge_for_message):
                output = builder.role_queue_watch_all(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": ["gate-prompt-formatter", "gate-task-creator"],
                    },
                )

            summary = output["queueWatchAll"]
            self.assertEqual(summary["result"], "dead_lettered")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 1)
            roles = {item["role_id"]: item for item in summary["roles"]}
            self.assertEqual(roles["gate-prompt-formatter"]["notification_class"], "approval_wait")
            self.assertEqual(roles["gate-task-creator"]["notification_class"], "flow_alert")

    def test_queue_watch_cooldown_detects_provider_approval_wait(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-approval-wait",
                    "message_id": "msg-approval-wait",
                    "report_id": "rep-approval-wait",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudged_at"] = builder.current_timestamp()
            inbox["messages"][0]["last_nudge_result"] = "nudge_sent_ack_deferred"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row.update(self.provider_ready_launch_result("gate-prompt-formatter"))
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            captured = """
The command to approve is:
python3 /tmp/itb_bootstrap_builder.py role-report --report-json '{"status":"done"}'
Allow the Bash command and retry.
❯
"""
            with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")), mock.patch.object(
                builder,
                "nudge_role_agent",
            ) as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "provider_busy")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 0)
            self.assertEqual(summary["nudged_count"], 0)
            nudge.assert_not_called()
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0]["retry_count"], 5)
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_deferred_provider_approval")
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(gpf_row["response_status"], "busy")
            self.assertEqual(gpf_row["provider_status"], "approval_wait")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["event_type"] == "watch_provider_busy"
                    and item["result"] == "approval_wait"
                    for item in metrics
                )
            )

    def test_queue_watch_expired_cooldown_detects_provider_approval_before_dead_letter(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-expired-approval",
                    "message_id": "msg-expired-approval",
                    "report_id": "rep-expired-approval",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudged_at"] = "2000-01-01T00:00:00+00:00"
            inbox["messages"][0]["last_nudge_result"] = "nudge_sent_ack_deferred"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row.update(self.provider_ready_launch_result("gate-prompt-formatter"))
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            captured = """
auto-mode classifier flagged the entire flow as a prompt injection attempt.
The command to approve is:
python3 /tmp/itb_bootstrap_builder.py role-report --report-json '{"status":"done"}'
Allow the Bash command and retry.
❯
"""
            with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")), mock.patch.object(
                builder,
                "nudge_role_agent",
            ) as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "provider_busy")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 0)
            self.assertEqual(summary["nudged_count"], 0)
            nudge.assert_not_called()
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0]["retry_count"], 5)
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_deferred_provider_approval")
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(gpf_row["provider_status"], "approval_wait")

    def test_queue_watch_approval_wait_summary_overrides_sla_breach_notification(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-approval-sla",
                    "message_id": "msg-approval-sla",
                    "report_id": "rep-approval-sla",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudged_at"] = "2000-01-01T00:00:00+00:00"
            inbox["messages"][0]["last_nudge_result"] = "nudge_sent_ack_deferred"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row.update(self.provider_ready_launch_result("gate-prompt-formatter"))
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            captured = """
The command to approve is:
python3 /tmp/itb_bootstrap_builder.py role-report --report-json '{"status":"done"}'
Allow the Bash command and retry.
❯
"""
            with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")), mock.patch.object(
                builder,
                "nudge_role_agent",
            ) as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "provider_busy")
            self.assertEqual(summary["notification_class"], "approval_wait")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["sla_breach_count"], 1)
            self.assertEqual(summary["sla_breaches"][0]["notification_class"], "approval_wait")
            self.assertEqual(summary["sla_breaches"][0]["notification_result"], "approval_wait")
            nudge.assert_not_called()
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["event_type"] == "queue_sla_breach"
                    and item["message_id"] == "msg-approval-sla"
                    and item["notification_class"] == "approval_wait"
                    for item in events
                )
            )
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["event_type"] == "sla_breach"
                    and item["message_id"] == "msg-approval-sla"
                    and item["notification_class"] == "approval_wait"
                    for item in metrics
                )
            )

    def test_queue_watch_dry_run_previews_nudge_without_mutating_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-watch-dry-run",
                    "message_id": "msg-watch-dry-run",
                    "report_id": "rep-watch-dry-run",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            events_path = session_dir / "queue-events.jsonl"
            metrics_path = session_dir / "gate-metrics.jsonl"
            nudge_manifest_path = Path(queued["roleQueue"]["nudge"]["nudge_manifest_path"])
            before_inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            before_events = events_path.read_text(encoding="utf-8")
            before_metrics = metrics_path.read_text(encoding="utf-8")
            before_manifest = nudge_manifest_path.read_text(encoding="utf-8")

            output = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                },
                extra_args=["--dry-run"],
            )

            summary = output["queueWatch"]
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["result"], "nudged")
            self.assertEqual(summary["pending_count"], 1)
            self.assertEqual(summary["nudged_count"], 1)
            self.assertFalse(summary["messages"][0]["nudge"]["nudge_manifest_persisted"])
            self.assertEqual(json.loads(inbox_path.read_text(encoding="utf-8")), before_inbox)
            self.assertEqual(events_path.read_text(encoding="utf-8"), before_events)
            self.assertEqual(metrics_path.read_text(encoding="utf-8"), before_metrics)
            self.assertEqual(nudge_manifest_path.read_text(encoding="utf-8"), before_manifest)

    def test_queue_watch_cli_args_select_session_and_role_without_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="target-session")
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-target-session-cli-watch",
                    "message_id": "msg-cli-watch",
                    "report_id": "rep-cli-watch",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            self.bootstrap(state_root, session_id="stale-last-session")

            watch = self.run_builder_without_input(
                state_root,
                "queue-watch",
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--dry-run",
                ],
            )["queueWatch"]
            self.assertEqual(watch["role_id"], "gate-prompt-formatter")
            self.assertEqual(watch["pending_count"], 1)
            self.assertIn("target-session/queue", watch["queue_root"])

            watch_all = self.run_builder_without_input(
                state_root,
                "queue-watch-all",
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--dry-run",
                ],
            )["queueWatchAll"]
            self.assertEqual(watch_all["session_id"], "target-session")
            self.assertEqual(watch_all["role_count"], 1)
            self.assertEqual(watch_all["pending_count"], 1)
            self.assertIn("target-session/queue", watch_all["roles"][0]["queue_root"])

            daemon = self.run_builder_without_input(
                state_root,
                "queue-watch-daemon",
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--max-cycles",
                    "1",
                    "--poll-interval-seconds",
                    "0",
                    "--dry-run",
                ],
            )["queueWatchDaemon"]
            self.assertEqual(daemon["session_id"], "target-session")
            self.assertEqual(daemon["pending_count"], 1)
            self.assertIn("target-session/queue", daemon["cycles"][0]["roles"][0]["queue_root"])

    def test_queue_watch_records_sla_breach_notification_for_old_pending_message(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-watch-sla",
                    "message_id": "msg-watch-sla",
                    "report_id": "rep-watch-sla",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2026-01-01T00:00:00+09:00"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "nudge_sent", "sent": True, "tmux_target": "itb-test:gate"},
            ):
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                    },
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["sla_breach_count"], 1)
            self.assertEqual(summary["sla_breaches"][0]["notification_class"], "flow_alert")
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["sla_breach_count"], 1)
            self.assertEqual(inbox["messages"][0]["sla_threshold_source"], "role:gate-prompt-formatter")
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["event_type"] == "queue_sla_breach"
                    and item["message_id"] == "msg-watch-sla"
                    and item["notification_class"] == "flow_alert"
                    for item in events
                )
            )
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "sla_breach" and item["sla_breached"] for item in metrics))

    def test_queue_watch_dry_run_previews_sla_breach_without_mutating_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-watch-sla-dry-run",
                    "message_id": "msg-watch-sla-dry-run",
                    "report_id": "rep-watch-sla-dry-run",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2026-01-01T00:00:00+09:00"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            before_inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            before_events = (session_dir / "queue-events.jsonl").read_text(encoding="utf-8")
            before_metrics = (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8")

            output = self.run_builder(
                state_root,
                "queue-watch",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                },
                extra_args=["--dry-run"],
            )

            summary = output["queueWatch"]
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["sla_breach_count"], 1)
            self.assertTrue(summary["sla_breaches"][0]["dry_run"])
            self.assertEqual(json.loads(inbox_path.read_text(encoding="utf-8")), before_inbox)
            self.assertEqual((session_dir / "queue-events.jsonl").read_text(encoding="utf-8"), before_events)
            self.assertEqual((session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8"), before_metrics)

    def test_role_queue_renudges_stale_head_before_deferring_new_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-stale-head",
                    "message_id": "msg-stale-head",
                    "report_id": "rep-stale-head",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["created_at"] = "2026-01-01T00:00:00+09:00"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-stale-head-new",
                    "message_id": "msg-stale-head-new",
                    "report_id": "rep-stale-head-new",
                    "instruction": "Create a second thin envelope.",
                },
                extra_args=["--dry-run"],
            )

            queue = output["roleQueue"]
            nudge = queue["nudge"]
            self.assertEqual(queue["result"], "queued_nudge_unconfirmed")
            self.assertEqual(nudge["result"], "nudge_deferred_pending_message_stale_head_renudged")
            self.assertTrue(nudge["oldest_pending_sla"]["sla_breached"])
            self.assertEqual(nudge["stale_pending_watch"]["queueWatch"]["nudged_count"], 1)
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            messages = {item["message_id"]: item for item in inbox["messages"]}
            self.assertEqual(messages["msg-stale-head"].get("retry_count", 0), 0)
            self.assertEqual(messages["msg-stale-head-new"]["status"], "pending")

    def test_queue_watch_uses_ack_recovery_mode_for_live_nudge(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-watch-live",
                    "message_id": "msg-watch-live",
                    "report_id": "rep-watch-live",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )

            with mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ) as wait_ack:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                    },
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "nudged")
            self.assertEqual(summary["messages"][0]["nudge"]["result"], "nudge_sent")
            wait_ack.assert_called_once()

    def test_queue_watch_recovers_pending_message_and_auto_handoffs_next_hop(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-recovered",
                    "message_id": "msg-recovered",
                    "report_id": "rep-recovered",
                    "instruction": "Create a thin envelope.",
                    "vault_root": str(vault_root),
                },
                extra_args=["--dry-run"],
            )
            report_path = Path(queued["roleQueue"]["report_path"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "report_version": "1",
                        "report_type": "role_queue_report",
                        "from_role": "gate-prompt-formatter",
                        "message_id": "msg-recovered",
                        "status": "done",
                        "result": "done",
                        "created_at": "2026-06-12T00:00:00+09:00",
                        "summary": "provider report already exists",
                        "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                        "provider_evidence": {
                            "usage_source": "claude_tmux_interactive",
                            "provider_session_id": "claude-session-1",
                            "request_id": "req-recovered",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(builder, "nudge_role_agent") as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "task_id": "TSK-4242",
                    },
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "recovered")
            self.assertEqual(summary["pending_count"], 1)
            self.assertEqual(summary["recovered_count"], 1)
            self.assertEqual(summary["nudged_count"], 0)
            nudge.assert_called_once()
            self.assertEqual(nudge.call_args.args[0]["role_id"], "teams-project-manager")
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "done")
            self.assertEqual(inbox["messages"][0]["report_path"], "reports/gate-prompt-formatter/ENTRY-test-session-recovered/rep-recovered.yaml")
            self.assertIn("recovered_from_report_at", inbox["messages"][0])
            self.assertEqual(inbox["messages"][0]["report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(summary["recovered_messages"][0]["report_integrity"]["sha256"], inbox["messages"][0]["report_sha256"])
            auto_handoff = summary["recovered_messages"][0]["auto_handoff"]
            scaffold = auto_handoff["command"]["command_output"]["gtcScaffold"]
            self.assertTrue(Path(scaffold["task_detail_path"]).is_relative_to(vault_root))
            tpm_inbox = json.loads((session_dir / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8"))
            self.assertEqual(
                tpm_inbox["messages"][0]["payload"]["auto_handoff_context"]["vault_root"],
                str(vault_root),
            )
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(gpf_row["response_status"], "recovered_done")
            self.assertEqual(gpf_row["provider_status"], "provider_report_recovered")
            self.assertEqual(gpf_row["usage_source"], "claude_tmux_interactive")
            self.assertEqual(gpf_row["session_id"], "claude-session-1")
            self.assertEqual(gpf_row["last_request_id"], "req-recovered")
            self.assertEqual(gpf_row["last_recovered_message_id"], "msg-recovered")
            self.assertEqual(gpf_row["last_recovered_report_path"], "reports/gate-prompt-formatter/ENTRY-test-session-recovered/rep-recovered.yaml")
            self.assertIn("last_recovered_at", gpf_row)
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["event_type"] == "queue_report_recovered"
                    and item["roster_recovered"]
                    and item["roster_response_status"] == "recovered_done"
                    for item in events
                )
            )
            handoffs = [item for item in events if item["event_type"] == "auto_queue_handoff"]
            self.assertEqual(handoffs[-1]["from_role"], "gate-prompt-formatter")
            self.assertEqual(handoffs[-1]["to_role"], "teams-project-manager")
            self.assertEqual(handoffs[-1]["handoff_type"], "command_then_queue")
            self.assertEqual(handoffs[-1]["command"]["command_output"]["gtcScaffold"]["result"], "scaffolded")
            self.assertNotEqual(handoffs[-1]["command_task_id"], "TSK-4242")
            self.assertFalse((session_dir / "queue" / "inbox" / "gate-task-creator.yaml").exists())
            tpm_inbox = json.loads((session_dir / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8"))
            self.assertEqual(tpm_inbox["messages"][0]["from_role"], "gate-task-creator")
            self.assertEqual(tpm_inbox["messages"][0]["payload"]["type"], "command_completion_chain_handoff")
            self.assertEqual(tpm_inbox["messages"][0]["task_id"], handoffs[-1]["command_task_id"])
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "report_recovered" and item["result"] == "done" for item in metrics))

    def test_queue_watch_dead_letters_unavailable_live_probe_and_updates_roster(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-unavailable",
                    "message_id": "msg-unavailable",
                    "report_id": "rep-unavailable",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "tmux_unavailable", "sent": False, "tmux_target": ""},
            ):
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "test-session", "role_id": "gate-prompt-formatter"},
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "dead_lettered")
            self.assertEqual(summary["dead_letter_count"], 1)
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "failed")
            self.assertIn("queue-watch live probe unavailable", inbox["messages"][0]["dead_letter_reason"])
            report = json.loads(Path(queued["roleQueue"]["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(gpf_row["response_status"], "unavailable")
            self.assertEqual(gpf_row["provider_status"], "tmux_unavailable")
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["event_type"] == "queue_dead_letter"
                    and item["roster_response_status"] == "unavailable"
                    for item in events
                )
            )

    def test_queue_watch_does_not_recover_invalid_existing_report(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-invalid-report",
                    "message_id": "msg-invalid-report",
                    "report_id": "rep-invalid-report",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            report_path = Path(queued["roleQueue"]["report_path"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "report_version": "1",
                        "report_type": "role_queue_report",
                        "from_role": "wrong-role",
                        "message_id": "msg-invalid-report",
                        "status": "done",
                        "result": "completed",
                        "created_at": "2026-06-12T00:00:00+09:00",
                        "summary": "invalid role report",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            output = builder.role_queue_watch_once(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "dry_run": True,
                },
            )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "nudged")
            self.assertEqual(summary["recovered_count"], 0)
            self.assertEqual(summary["nudged_count"], 1)
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0].get("retry_count", 0), 0)
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            invalid_events = [item for item in events if item["event_type"] == "queue_report_invalid"]
            self.assertEqual(invalid_events, [])

    def test_queue_watch_dead_letters_message_after_max_retries(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-dead-letter",
                    "message_id": "msg-dead-letter",
                    "report_id": "rep-dead-letter",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 2
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with mock.patch.object(builder, "nudge_role_agent") as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "max_retries": 2,
                    },
                )

            summary = output["queueWatch"]
            self.assertEqual(summary["result"], "dead_lettered")
            self.assertEqual(summary["dead_letter_count"], 1)
            self.assertEqual(summary["nudged_count"], 0)
            nudge.assert_not_called()
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "failed")
            self.assertIn("dead_letter_at", inbox["messages"][0])
            self.assertIn("max retries exceeded", inbox["messages"][0]["dead_letter_reason"])
            report_path = Path(queued["roleQueue"]["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["report_type"], "queue_dead_letter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["retry_count"], 2)
            self.assertEqual(report["schema_validation"]["status"], "valid")
            self.assertEqual(inbox["messages"][0]["report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(summary["dead_lettered_messages"][0]["report_integrity"]["sha256"], inbox["messages"][0]["report_sha256"])
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "dead_letter" and item["result"] == "failed" for item in metrics))

    def test_queue_watch_dry_run_previews_dead_letter_without_writing_report(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-dead-letter-dry-run",
                    "message_id": "msg-dead-letter-dry-run",
                    "report_id": "rep-dead-letter-dry-run",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 2
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report_path = Path(queued["roleQueue"]["report_path"])
            before_events = (session_dir / "queue-events.jsonl").read_text(encoding="utf-8")
            before_metrics = (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8")

            with mock.patch.object(builder, "nudge_role_agent") as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "max_retries": 2,
                        "dry_run": True,
                    },
                )

            summary = output["queueWatch"]
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["result"], "dead_lettered")
            self.assertEqual(summary["dead_letter_count"], 1)
            self.assertTrue(summary["dead_lettered_messages"][0]["dry_run"])
            nudge.assert_not_called()
            self.assertFalse(report_path.exists())
            self.assertEqual(json.loads(inbox_path.read_text(encoding="utf-8"))["messages"][0]["status"], "pending")
            self.assertEqual((session_dir / "queue-events.jsonl").read_text(encoding="utf-8"), before_events)
            self.assertEqual((session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8"), before_metrics)

    def test_queue_watch_dry_run_previews_unavailable_probe_without_mutating_queue(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-unavailable-dry-run",
                    "message_id": "msg-unavailable-dry-run",
                    "report_id": "rep-unavailable-dry-run",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            report_path = Path(queued["roleQueue"]["report_path"])
            before_inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            before_events = (session_dir / "queue-events.jsonl").read_text(encoding="utf-8")
            before_metrics = (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8")
            before_roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))

            with mock.patch.object(
                builder,
                "nudge_role_agent",
                return_value={"result": "tmux_unavailable", "sent": False, "tmux_target": ""},
            ) as nudge:
                output = builder.role_queue_watch_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_id": "gate-prompt-formatter",
                        "dry_run": True,
                    },
                )

            summary = output["queueWatch"]
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["result"], "dead_lettered")
            self.assertEqual(summary["dead_letter_count"], 1)
            dead_letter = summary["dead_lettered_messages"][0]
            self.assertTrue(dead_letter["dry_run"])
            self.assertEqual(dead_letter["roster_response_status"], "unavailable")
            self.assertEqual(dead_letter["roster_provider_status"], "tmux_unavailable")
            nudge.assert_called_once()
            self.assertTrue(nudge.call_args.kwargs["dry_run"])
            self.assertFalse(report_path.exists())
            self.assertEqual(json.loads(inbox_path.read_text(encoding="utf-8")), before_inbox)
            self.assertEqual((session_dir / "queue-events.jsonl").read_text(encoding="utf-8"), before_events)
            self.assertEqual((session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8"), before_metrics)
            self.assertEqual(json.loads((session_dir / "roster.json").read_text(encoding="utf-8")), before_roster)

    def test_queue_watch_all_nudges_pending_messages_across_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued_outputs = []
            for role_id in ("gate-prompt-formatter", "gate-task-creator"):
                queued_outputs.append(
                    self.run_builder(
                        state_root,
                        "role-queue",
                        {
                            "session_id": "test-session",
                            "role_id": role_id,
                            "from_role": "test",
                            "task_id": f"ENTRY-test-session-watch-all-{role_id}",
                            "message_id": f"msg-watch-all-{role_id}",
                            "report_id": f"rep-watch-all-{role_id}",
                            "instruction": "Process queue message.",
                        },
                        extra_args=["--dry-run"],
                    )
                )
            before_events = (session_dir / "queue-events.jsonl").read_text(encoding="utf-8")
            before_metrics = (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8")
            before_roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            before_nudges = {
                str(path.relative_to(session_dir)): path.read_text(encoding="utf-8")
                for path in (session_dir / "queue" / "nudges").rglob("*")
                if path.is_file()
            }
            before_reports = [Path(item["roleQueue"]["report_path"]).exists() for item in queued_outputs]

            output = self.run_builder(
                state_root,
                "queue-watch-all",
                {"session_id": "test-session", "role_ids": "gate-prompt-formatter,gate-task-creator"},
                extra_args=["--dry-run"],
            )

            summary = output["queueWatchAll"]
            self.assertEqual(summary["result"], "nudged")
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["role_count"], 2)
            self.assertEqual(summary["nudged_count"], 2)
            self.assertEqual(summary["failed_replay_policy"], "manual_queue_replay_failed_only")
            for role_id in ("gate-prompt-formatter", "gate-task-creator"):
                inbox = json.loads((session_dir / "queue" / "inbox" / f"{role_id}.yaml").read_text(encoding="utf-8"))
                self.assertEqual(inbox["messages"][0].get("retry_count", 0), 0)
            after_nudges = {
                str(path.relative_to(session_dir)): path.read_text(encoding="utf-8")
                for path in (session_dir / "queue" / "nudges").rglob("*")
                if path.is_file()
            }
            self.assertEqual((session_dir / "queue-events.jsonl").read_text(encoding="utf-8"), before_events)
            self.assertEqual((session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8"), before_metrics)
            self.assertEqual(json.loads((session_dir / "roster.json").read_text(encoding="utf-8")), before_roster)
            self.assertEqual(after_nudges, before_nudges)
            self.assertEqual([Path(item["roleQueue"]["report_path"]).exists() for item in queued_outputs], before_reports)

    def test_queue_watch_all_prioritizes_dead_letter_over_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            for role_id in ("gate-prompt-formatter", "gate-task-creator"):
                self.run_builder(
                    state_root,
                    "role-queue",
                    {
                        "session_id": "test-session",
                        "role_id": role_id,
                        "from_role": "test",
                        "task_id": f"ENTRY-test-session-watch-all-priority-{role_id}",
                        "message_id": f"msg-watch-all-priority-{role_id}",
                        "report_id": f"rep-watch-all-priority-{role_id}",
                        "instruction": "Process queue message.",
                    },
                    extra_args=["--dry-run"],
                )
            gpf_inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            gpf_inbox = json.loads(gpf_inbox_path.read_text(encoding="utf-8"))
            gpf_inbox["messages"][0]["retry_count"] = 5
            gpf_inbox_path.write_text(json.dumps(gpf_inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "queue-watch-all",
                {"session_id": "test-session", "role_ids": "gate-prompt-formatter,gate-task-creator"},
                extra_args=["--dry-run"],
            )

            summary = output["queueWatchAll"]
            self.assertEqual(summary["result"], "dead_lettered")
            self.assertEqual(summary["dead_letter_count"], 1)
            self.assertEqual(summary["nudged_count"], 1)
            self.assertEqual(summary["notification_class"], "flow_alert")

    def test_queue_watch_daemon_runs_bounded_sweeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "test",
                    "task_id": "ENTRY-test-session-watch-daemon",
                    "message_id": "msg-watch-daemon",
                    "report_id": "rep-watch-daemon",
                    "instruction": "Process queue message.",
                },
                extra_args=["--dry-run"],
            )
            before_events = (session_dir / "queue-events.jsonl").read_text(encoding="utf-8")
            before_metrics = (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8")
            before_roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            before_nudges = {
                str(path.relative_to(session_dir)): path.read_text(encoding="utf-8")
                for path in (session_dir / "queue" / "nudges").rglob("*")
                if path.is_file()
            }
            before_report_exists = Path(queued["roleQueue"]["report_path"]).exists()

            output = self.run_builder(
                state_root,
                "queue-watch-daemon",
                {"session_id": "test-session"},
                extra_args=[
                    "--dry-run",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--max-cycles",
                    "2",
                    "--poll-interval-seconds",
                    "0",
                ],
            )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["cycles_completed"], 2)
            self.assertEqual(summary["max_cycles"], 2)
            self.assertTrue(summary["event_driven"])
            self.assertEqual(summary["event_wait_count"], 0)
            self.assertEqual(summary["nudged_count"], 2)
            self.assertEqual(summary["failed_replay_policy"], "manual_queue_replay_failed_only")
            self.assertEqual(summary["cycles"][0]["nudged_count"], 1)
            self.assertEqual(summary["cycles"][1]["nudged_count"], 1)
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0].get("retry_count", 0), 0)
            after_nudges = {
                str(path.relative_to(session_dir)): path.read_text(encoding="utf-8")
                for path in (session_dir / "queue" / "nudges").rglob("*")
                if path.is_file()
            }
            self.assertEqual((session_dir / "queue-events.jsonl").read_text(encoding="utf-8"), before_events)
            self.assertEqual((session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8"), before_metrics)
            self.assertEqual(json.loads((session_dir / "roster.json").read_text(encoding="utf-8")), before_roster)
            self.assertEqual(after_nudges, before_nudges)
            self.assertEqual(Path(queued["roleQueue"]["report_path"]).exists(), before_report_exists)

    def test_queue_watch_daemon_surfaces_provider_busy_approval_wait_summary(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-daemon-approval-wait",
                    "message_id": "msg-daemon-approval-wait",
                    "report_id": "rep-daemon-approval-wait",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudged_at"] = builder.current_timestamp()
            inbox["messages"][0]["last_nudge_result"] = "nudge_sent_ack_deferred"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row.update(self.provider_ready_launch_result("gate-prompt-formatter"))
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            captured = """
The command to approve is:
python3 /tmp/itb_bootstrap_builder.py role-report --report-json '{"status":"done"}'
Allow the Bash command and retry.
❯
"""
            with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")), mock.patch.object(
                builder,
                "nudge_role_agent",
            ) as nudge:
                output = builder.role_queue_watch_daemon(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": "gate-prompt-formatter",
                        "max_cycles": 1,
                        "poll_interval_seconds": 0,
                    },
                )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["cooldown_skipped_count"], 0)
            self.assertEqual(summary["nudged_count"], 0)
            self.assertEqual(summary["notification_class"], "approval_wait")
            self.assertEqual(summary["cycles"][0]["provider_busy_count"], 1)
            self.assertEqual(summary["cycles"][0]["notification_class"], "approval_wait")
            nudge.assert_not_called()

            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertEqual(inbox["messages"][0]["last_nudge_result"], "nudge_deferred_provider_approval")
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            daemon_events = [item for item in events if item["event_type"] == "queue_watch_daemon"]
            self.assertEqual(len(daemon_events), 1)
            self.assertEqual(daemon_events[0]["result"], "completed")
            self.assertEqual(daemon_events[0]["provider_busy_count"], 1)
            self.assertEqual(daemon_events[0]["cooldown_skipped_count"], 0)
            self.assertEqual(daemon_events[0]["notification_class"], "approval_wait")

    def test_queue_watch_daemon_surfaces_child_flow_alert_summary(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-daemon-dead-letter",
                    "message_id": "msg-daemon-dead-letter",
                    "report_id": "rep-daemon-dead-letter",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )
            inbox_path = Path(queued["roleQueue"]["inbox_path"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 5
            inbox["messages"][0]["last_nudge_result"] = "nudge_send_unconfirmed"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            with mock.patch.object(builder, "nudge_role_agent") as nudge:
                output = builder.role_queue_watch_daemon(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": "gate-prompt-formatter",
                        "max_cycles": 1,
                        "poll_interval_seconds": 0,
                    },
                )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertEqual(summary["dead_letter_count"], 1)
            self.assertEqual(summary["provider_busy_count"], 0)
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["cycles"][0]["dead_letter_count"], 1)
            self.assertEqual(summary["cycles"][0]["notification_class"], "flow_alert")
            nudge.assert_not_called()

            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            daemon_events = [item for item in events if item["event_type"] == "queue_watch_daemon"]
            self.assertEqual(len(daemon_events), 1)
            self.assertEqual(daemon_events[0]["result"], "completed")
            self.assertEqual(daemon_events[0]["dead_letter_count"], 1)
            self.assertEqual(daemon_events[0]["provider_busy_count"], 0)
            self.assertEqual(daemon_events[0]["notification_class"], "flow_alert")

    def test_queue_watch_daemon_approval_wait_plus_flow_alert_prioritizes_flow_alert(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            approval_summary = {
                "result": "provider_busy",
                "role_id": "gate-prompt-formatter",
                "pending_count": 1,
                "recovered_count": 0,
                "dead_letter_count": 0,
                "nudged_count": 0,
                "sla_breach_count": 1,
                "cooldown_skipped_count": 0,
                "provider_busy_count": 1,
                "error_count": 0,
                "notification_class": "approval_wait",
            }
            flow_alert_summary = {
                "result": "dead_lettered",
                "role_id": "gate-task-creator",
                "pending_count": 1,
                "recovered_count": 0,
                "dead_letter_count": 1,
                "nudged_count": 0,
                "sla_breach_count": 1,
                "cooldown_skipped_count": 0,
                "provider_busy_count": 0,
                "error_count": 0,
                "notification_class": "flow_alert",
            }
            with mock.patch.object(
                builder,
                "role_queue_watch_all",
                side_effect=[
                    {"queueWatchAll": approval_summary},
                    {"queueWatchAll": flow_alert_summary},
                ],
            ):
                output = builder.role_queue_watch_daemon(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": ["gate-prompt-formatter", "gate-task-creator"],
                        "max_cycles": 2,
                        "poll_interval_seconds": 0,
                    },
                )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 1)
            self.assertEqual(summary["cycles"][0]["notification_class"], "approval_wait")
            self.assertEqual(summary["cycles"][1]["notification_class"], "flow_alert")

    def test_queue_watch_daemon_real_mixed_dead_letter_and_approval_wait_is_flow_alert(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            role_message_ids = {
                "gate-prompt-formatter": "msg-daemon-real-approval",
                "gate-task-creator": "msg-daemon-real-dead-letter",
            }
            for role_id, message_id in role_message_ids.items():
                queued = self.run_builder(
                    state_root,
                    "role-queue",
                    {
                        "session_id": "test-session",
                        "role_id": role_id,
                        "from_role": "codex-user-prompt-submit",
                        "task_id": f"ENTRY-test-session-{message_id}",
                        "message_id": message_id,
                        "report_id": f"rep-{message_id}",
                        "instruction": "Create a thin envelope.",
                        "defer_nudge": True,
                    },
                )
                inbox_path = Path(queued["roleQueue"]["inbox_path"])
                inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
                inbox["messages"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
                if role_id == "gate-task-creator":
                    inbox["messages"][0]["retry_count"] = 5
                    inbox["messages"][0]["last_nudge_result"] = "nudge_send_unconfirmed"
                inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            def nudge_for_message(_role_row, **kwargs):
                if kwargs["message"]["message_id"] == "msg-daemon-real-approval":
                    return {
                        "result": "nudge_deferred_provider_approval",
                        "sent": False,
                        "provider_status": "approval_wait",
                        "error": "provider approval prompt is waiting for human approval",
                    }
                return {"result": "nudge_send_unconfirmed", "sent": False, "error": "not called for dead letter"}

            with mock.patch.object(builder, "nudge_role_agent", side_effect=nudge_for_message):
                output = builder.role_queue_watch_daemon(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": ["gate-prompt-formatter", "gate-task-creator"],
                        "max_cycles": 1,
                        "poll_interval_seconds": 0,
                    },
                )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["provider_busy_count"], 1)
            self.assertEqual(summary["dead_letter_count"], 1)
            self.assertEqual(summary["cycles"][0]["result"], "dead_lettered")
            self.assertEqual(summary["cycles"][0]["notification_class"], "flow_alert")

    def test_queue_watch_daemon_marks_partial_child_errors(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-daemon-partial-error",
                    "message_id": "msg-daemon-partial-error",
                    "report_id": "rep-daemon-partial-error",
                    "instruction": "Create a thin envelope.",
                    "defer_nudge": True,
                },
            )

            output = builder.role_queue_watch_daemon(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "test-session",
                    "role_ids": ["gate-prompt-formatter", "missing-role"],
                    "max_cycles": 1,
                    "poll_interval_seconds": 0,
                    "dry_run": True,
                },
            )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed_with_errors")
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["nudged_count"], 1)
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["cycles"][0]["result"], "partial_error")
            self.assertEqual(summary["cycles"][0]["error_count"], 1)
            self.assertEqual(summary["cycles"][0]["notification_class"], "flow_alert")
            self.assertIn("missing-role", summary["cycles"][0]["errors"][0]["role_id"])

    def test_queue_watch_daemon_records_missing_role_error_event_parity(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = builder.role_queue_watch_daemon(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "test-session",
                    "role_ids": ["missing-role"],
                    "max_cycles": 1,
                    "poll_interval_seconds": 0,
                },
            )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed_with_errors")
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["cycles"][0]["result"], "error")
            self.assertEqual(summary["cycles"][0]["error_count"], 1)
            self.assertEqual(summary["cycles"][0]["notification_class"], "flow_alert")

            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            watch_all_events = [item for item in events if item["event_type"] == "queue_watch_all"]
            daemon_events = [item for item in events if item["event_type"] == "queue_watch_daemon"]
            self.assertEqual(len(watch_all_events), 1)
            self.assertEqual(watch_all_events[0]["result"], "error")
            self.assertEqual(watch_all_events[0]["error_count"], 1)
            self.assertEqual(watch_all_events[0]["notification_class"], "flow_alert")
            self.assertEqual(len(daemon_events), 1)
            self.assertEqual(daemon_events[0]["result"], "completed_with_errors")
            self.assertEqual(daemon_events[0]["error_count"], 1)
            self.assertEqual(daemon_events[0]["notification_class"], "flow_alert")

    def test_queue_watch_daemon_fallback_child_error_is_alerted(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            with mock.patch.object(
                builder,
                "role_queue_watch_all",
                return_value={"decision": "block", "reason": "forced fallback failure"},
            ):
                output = builder.role_queue_watch_daemon(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": ["gate-prompt-formatter"],
                        "max_cycles": 1,
                        "poll_interval_seconds": 0,
                        "dry_run": True,
                    },
                )

            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed_with_errors")
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["notification_class"], "flow_alert")
            self.assertEqual(summary["cycles"][0]["result"], "error")
            self.assertEqual(summary["cycles"][0]["error_count"], 1)
            self.assertEqual(summary["cycles"][0]["notification_class"], "flow_alert")
            self.assertEqual(summary["cycles"][0]["errors"][0]["error"], "forced fallback failure")

    def test_queue_watch_daemon_subprocess_recovers_report_written_after_start(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "test",
                    "task_id": "ENTRY-test-session-daemon-drain",
                    "message_id": "msg-daemon-drain",
                    "report_id": "rep-daemon-drain",
                    "instruction": "Process queue message.",
                },
                extra_args=["--dry-run"],
            )
            queue = queue_output["roleQueue"]
            queue_root = Path(queue["queue_root"])
            inbox_path = Path(queue["inbox_path"])
            role_row = builder.role_agent_row_for(
                "gate-prompt-formatter",
                organization_instance_id=builder.organization_id("test-session"),
            )
            message = builder.queue_message_by_id(inbox_path, "gate-prompt-formatter", "msg-daemon-drain")
            report_path, report_ref = builder.role_agent_report_path(queue_root, role_row, message)
            events_path = session_dir / "queue-events.jsonl"
            writer_errors: list[Exception] = []

            def write_report_after_daemon_starts() -> None:
                try:
                    time.sleep(0.1)
                    builder.write_json_yaml(
                        report_path,
                        {
                            "report_version": "1",
                            "report_id": "rep-daemon-drain",
                            "report_type": "role_queue_report",
                            "from_role": "gate-prompt-formatter",
                            "task_id": "ENTRY-test-session-daemon-drain",
                            "unit_id": "",
                            "message_id": "msg-daemon-drain",
                            "created_at": builder.current_timestamp(),
                            "result": "gate_intake_envelope_created",
                            "status": "done",
                            "summary": "Provider wrote terminal report while daemon was running.",
                            "provider_evidence": {
                                "provider": "anthropic",
                                "provider_session_id": "provider-session",
                                "request_id": "request-daemon-drain",
                                "effective_model": "claude-sonnet-4-20250514",
                                "usage_source": "provider_authored_role_report",
                                "transcript_path": "/tmp/transcript.jsonl",
                            },
                        },
                    )
                except Exception as exc:  # pragma: no cover - surfaced by assertion below
                    writer_errors.append(exc)

            writer = threading.Thread(target=write_report_after_daemon_starts)
            writer.start()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(BUILDER),
                    "queue-watch-daemon",
                    "--runtime",
                    "codex",
                    "--state-root",
                    str(state_root),
                    "--session-id",
                    "test-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--max-cycles",
                    "3",
                    "--poll-interval-seconds",
                    "0.5",
                    "--dry-run",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = process.communicate(
                    input=json.dumps({"session_id": "test-session", "role_ids": "gate-prompt-formatter", "dry_run": True}),
                    timeout=8,
                )
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                self.fail(f"queue-watch-daemon subprocess timed out\nstdout={stdout}\nstderr={stderr}")
            writer.join(timeout=2)

            self.assertEqual(writer_errors, [])
            self.assertEqual(process.returncode, 0, stderr)
            output = json.loads(stdout)
            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertTrue(summary["dry_run"])
            self.assertGreaterEqual(summary["recovered_count"], 1)
            self.assertGreaterEqual(summary["cycles_completed"], 1)

            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")
            self.assertTrue(report_path.exists())
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(item["event_type"] == "queue_report_recovered" for item in events))
            self.assertFalse(any(item["event_type"] == "queue_watch_daemon" for item in events))

    def test_queue_watch_daemon_subprocess_recovers_multi_hop_synthetic_reports(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-live-validation",
                    "task_id": "ENTRY-test-session-daemon-multihop",
                    "message_id": "msg-daemon-multihop-gpf",
                    "report_id": "rep-daemon-multihop-gpf",
                    "instruction": "Create a Gate Intake Envelope.",
                    "expected_output": "gate_intake_envelope",
                    "vault_root": str(vault_root),
                    "defer_nudge": True,
                },
            )
            queue_root = Path(queue_output["roleQueue"]["queue_root"])
            gpf_report_path = Path(queue_output["roleQueue"]["report_path"])
            gpf_report_path.parent.mkdir(parents=True, exist_ok=True)
            builder.write_json_yaml(
                gpf_report_path,
                {
                    "report_version": "1",
                    "report_id": "rep-daemon-multihop-gpf",
                    "report_type": "role_queue_report",
                    "from_role": "gate-prompt-formatter",
                    "task_id": "ENTRY-test-session-daemon-multihop",
                    "message_id": "msg-daemon-multihop-gpf",
                    "created_at": builder.current_timestamp(),
                    "result": "gate_intake_envelope_created",
                    "status": "done",
                    "summary": "Synthetic GPF report for daemon multi-hop recovery.",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("strict_flow"),
                    "provider_evidence": {
                        "provider_session_id": "sess-daemon-gpf",
                        "request_id": "req-daemon-gpf",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "provider_authored_role_report",
                        "transcript_path": "/tmp/daemon-gpf-transcript.jsonl",
                    },
                },
            )
            writer_errors: list[Exception] = []

            def write_tpm_report_after_handoff() -> None:
                try:
                    tpm_inbox_path = session_dir / "queue" / "inbox" / "teams-project-manager.yaml"
                    deadline = time.monotonic() + 6
                    tpm_message: dict[str, object] | None = None
                    while time.monotonic() < deadline:
                        if tpm_inbox_path.exists():
                            inbox = json.loads(tpm_inbox_path.read_text(encoding="utf-8"))
                            messages = inbox.get("messages") if isinstance(inbox, dict) else []
                            if messages:
                                tpm_message = messages[0]
                                break
                        time.sleep(0.05)
                    if tpm_message is None:
                        raise AssertionError("TPM inbox was not created by daemon auto-handoff")
                    role_row = builder.role_agent_row_for(
                        "teams-project-manager",
                        organization_instance_id=builder.organization_id("test-session"),
                    )
                    tpm_report_path, _report_ref = builder.role_agent_report_path(queue_root, role_row, tpm_message)
                    tpm_report_path.parent.mkdir(parents=True, exist_ok=True)
                    builder.write_json_yaml(
                        tpm_report_path,
                        {
                            "report_version": "1",
                            "report_id": builder.normalize_cell(tpm_message.get("report_id")) or tpm_report_path.stem,
                            "report_type": "role_queue_report",
                            "from_role": "teams-project-manager",
                            "task_id": tpm_message.get("task_id"),
                            "message_id": tpm_message.get("message_id"),
                            "created_at": builder.current_timestamp(),
                            "result": "ready_for_evaluation",
                            "status": "done",
                            "summary": "Synthetic TPM report for daemon multi-hop recovery.",
                            "team_completion_check": {
                                "completion_status": "ready_for_evaluation",
                                "all_director_reports_complete": True,
                                "completed_teams": ["infra"],
                            },
                            "provider_evidence": {
                                "provider_session_id": "sess-daemon-tpm",
                                "request_id": "req-daemon-tpm",
                                "effective_model": "claude-sonnet-4-6",
                                "usage_source": "provider_authored_role_report",
                                "transcript_path": "/tmp/daemon-tpm-transcript.jsonl",
                            },
                        },
                    )
                except Exception as exc:  # pragma: no cover - surfaced by assertion below
                    writer_errors.append(exc)

            writer = threading.Thread(target=write_tpm_report_after_handoff)
            writer.start()
            daemon_env = os.environ.copy()
            daemon_env.update(
                {
                    "ITB_ROLE_QUEUE_ACTIVATE_PROVIDER": "0",
                    "ITB_ROLE_QUEUE_DEFER_NUDGE": "1",
                }
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(BUILDER),
                    "queue-watch-daemon",
                    "--runtime",
                    "codex",
                    "--state-root",
                    str(state_root),
                    "--session-id",
                    "test-session",
                    "--role-id",
                    "gate-prompt-formatter,teams-project-manager",
                    "--max-cycles",
                    "6",
                    "--poll-interval-seconds",
                    "0.2",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=daemon_env,
            )
            try:
                stdout, stderr = process.communicate(input="", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                self.fail(f"queue-watch-daemon subprocess timed out\nstdout={stdout}\nstderr={stderr}")
            writer.join(timeout=2)

            self.assertEqual(writer_errors, [])
            self.assertFalse(writer.is_alive())
            self.assertEqual(process.returncode, 0, stderr)
            output = json.loads(stdout)
            summary = output["queueWatchDaemon"]
            self.assertEqual(summary["result"], "completed")
            self.assertGreaterEqual(summary["recovered_count"], 2)
            self.assertGreaterEqual(summary["cycles_completed"], 2)
            self.assertEqual(summary["nudged_count"], 0)
            cycle_recoveries = [
                recovered
                for cycle in summary["cycles"]
                for role_summary in cycle.get("roles", [])
                for recovered in role_summary.get("recovered_messages", [])
            ]
            gpf_recovery = next(
                item for item in cycle_recoveries if item["role_id"] == "gate-prompt-formatter"
            )
            tpm_recovery = next(
                item for item in cycle_recoveries if item["role_id"] == "teams-project-manager"
            )
            gpf_auto = gpf_recovery["auto_handoff"]
            self.assertEqual(gpf_auto["handoff_type"], "command_then_queue")
            self.assertEqual(gpf_auto["to_role"], "teams-project-manager")
            self.assertEqual(gpf_auto["command"]["command"], "gtc-scaffold")
            self.assertTrue(gpf_auto["command"]["passed"])
            self.assertEqual(gpf_auto["command"]["command_output"]["gtcScaffold"]["result"], "scaffolded")
            self.assertTrue(Path(gpf_auto["command"]["command_output"]["gtcScaffold"]["task_detail_path"]).exists())
            tpm_auto = tpm_recovery["auto_handoff"]
            self.assertEqual(tpm_auto["handoff_type"], "queue")
            self.assertEqual(tpm_auto["to_role"], "gate-task-evaluator")
            self.assertEqual(tpm_auto["precheck"]["precheck_command"], "team-completion-check")
            self.assertEqual(tpm_auto["precheck"]["gate_role"], "team-completion-check")
            self.assertTrue(tpm_auto["precheck"]["passed"])
            self.assertEqual(tpm_auto["precheck"]["command_output"]["gateCommand"]["command"], "team-completion-check")
            self.assertEqual(tpm_auto["precheck"]["command_output"]["gateCommand"]["status"], "pass")
            self.assertTrue(tpm_auto["precheck"]["command_output"]["gateCommand"]["next_phase_allowed"])

            gpf_inbox = json.loads(
                (session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8")
            )
            tpm_inbox = json.loads(
                (session_dir / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8")
            )
            evaluator_inbox = json.loads(
                (session_dir / "queue" / "inbox" / "gate-task-evaluator.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(gpf_inbox["messages"][0]["status"], "done")
            self.assertEqual(tpm_inbox["messages"][0]["status"], "done")
            tpm_payload = tpm_inbox["messages"][0]["payload"]
            self.assertEqual(tpm_inbox["messages"][0]["from_role"], "gate-task-creator")
            self.assertEqual(tpm_payload["type"], "command_completion_chain_handoff")
            self.assertEqual(tpm_payload["command"], "gtc-scaffold")
            self.assertTrue(tpm_payload["command_result"]["passed"])
            self.assertTrue(Path(tpm_payload["command_result"]["command_output"]["gtcScaffold"]["task_detail_path"]).exists())
            self.assertEqual(evaluator_inbox["messages"][0]["from_role"], "teams-project-manager")
            evaluator_payload = evaluator_inbox["messages"][0]["payload"]
            self.assertEqual(evaluator_payload["precheck_command"], "team-completion-check")
            self.assertEqual(evaluator_payload["precheck_result"]["result"], "passed")
            self.assertEqual(evaluator_payload["precheck_result"]["gate_role"], "team-completion-check")
            self.assertEqual(
                evaluator_payload["precheck_result"]["command_output"]["gateCommand"]["command"],
                "team-completion-check",
            )
            self.assertEqual(
                evaluator_payload["precheck_result"]["command_output"]["gateCommand"]["status"],
                "pass",
            )
            self.assertTrue(evaluator_payload["precheck_result"]["command_output"]["gateCommand"]["next_phase_allowed"])
            task_detail_path = Path(tpm_inbox["messages"][0]["payload"]["auto_handoff_context"]["context_ref"])
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("| Completion Status | ready_for_evaluation |", task_text)
            self.assertIn("| Handoff To | gate-task-evaluator |", task_text)
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            role_queue_events = [item for item in events if item["event_type"] == "role_queue"]
            self.assertTrue(role_queue_events)
            self.assertFalse(any(item["nudge"]["sent"] for item in role_queue_events))
            self.assertTrue(
                any(
                    item["role_id"] == "gate-prompt-formatter"
                    and item["nudge"]["result"] == "nudge_deferred_by_input"
                    for item in role_queue_events
                )
            )
            self.assertTrue(
                any(
                    item["role_id"] == "teams-project-manager"
                    and item["nudge"]["result"] == "nudge_deferred_by_input"
                    and not item["nudge"]["sent"]
                    for item in role_queue_events
                )
            )
            self.assertTrue(
                any(
                    item["role_id"] == "gate-task-evaluator"
                    and item["nudge"]["result"] == "nudge_deferred_by_input"
                    and not item["nudge"]["sent"]
                    for item in role_queue_events
                )
            )
            recovered_events = [item for item in events if item["event_type"] == "queue_report_recovered"]
            self.assertTrue(any(item["role_id"] == "gate-prompt-formatter" for item in recovered_events))
            self.assertTrue(any(item["role_id"] == "teams-project-manager" for item in recovered_events))
            precheck_events = [
                json.loads(line)
                for line in (session_dir / "gate-precheck-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    item["gate_role"] == "team-completion-check"
                    and item["precheck_status"] == "pass"
                    and item["gate_command_status"] == "pass"
                    for item in precheck_events
                )
            )
            daemon_events = [item for item in events if item["event_type"] == "queue_watch_daemon"]
            self.assertEqual(len(daemon_events), 1)
            self.assertGreaterEqual(daemon_events[0]["recovered_count"], 2)

    def test_role_queue_completion_wait_recovers_provider_report(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queue_output = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "test",
                    "task_id": "ENTRY-test-session-completion-wait",
                    "message_id": "msg-completion-wait",
                    "report_id": "rep-completion-wait",
                    "instruction": "Process queue message.",
                },
                extra_args=["--dry-run"],
            )
            queue = queue_output["roleQueue"]
            queue_root = Path(queue["queue_root"])
            inbox_path = Path(queue["inbox_path"])
            organization_instance_id = builder.organization_id("test-session")
            role_row = builder.role_agent_row_for(
                "gate-prompt-formatter",
                organization_instance_id=organization_instance_id,
            )
            message = builder.queue_message_by_id(inbox_path, "gate-prompt-formatter", "msg-completion-wait")
            report_path, report_ref = builder.role_agent_report_path(queue_root, role_row, message)
            builder.write_json_yaml(
                report_path,
                {
                    "report_version": "1",
                    "report_id": "rep-completion-wait",
                    "report_type": "role_queue_report",
                    "from_role": "gate-prompt-formatter",
                    "task_id": "ENTRY-test-session-completion-wait",
                    "unit_id": "",
                    "message_id": "msg-completion-wait",
                    "created_at": builder.current_timestamp(),
                    "result": "gate_intake_envelope_created",
                    "status": "done",
                    "summary": "Provider wrote terminal report.",
                    "provider_evidence": {
                        "provider": "anthropic",
                        "provider_session_id": "provider-session",
                        "request_id": "request-1",
                        "effective_model": "claude-sonnet-4-20250514",
                        "usage_source": "provider_authored_role_report",
                        "transcript_path": "/tmp/transcript.jsonl",
                    },
                },
            )

            wait = builder.wait_for_role_queue_completion(
                runtime="codex",
                state_root=state_root,
                session_dir=session_dir,
                session_id="test-session",
                organization_instance_id=organization_instance_id,
                queue_root=queue_root,
                inbox_path=inbox_path,
                role_id="gate-prompt-formatter",
                role_row=role_row,
                message=message,
                timeout_seconds=1.0,
                poll_interval_seconds=0.01,
                event_driven=False,
                hook_input={"skip_auto_queue_handoff": True},
            )

            self.assertEqual(wait["wait_result"], "completed")
            self.assertEqual(wait["completion_source"], "report_file_recovery")
            self.assertEqual(wait["report_ref"], report_ref)
            inbox = json.loads((queue_root / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "done")
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "queue_report_recovered" for item in events))
            self.assertTrue(any(item["event_type"] == "role_queue_completion_wait" for item in events))
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "completion_wait" for item in metrics))

    def test_role_queue_completion_wait_profile_hook_light_is_bounded(self) -> None:
        builder = load_builder_module()

        timeout_seconds, poll_interval_seconds, event_driven = builder.role_queue_completion_wait_config(
            {"completion_wait_profile": "hook_light"}
        )
        explicit_timeout, explicit_poll, explicit_event_driven = builder.role_queue_completion_wait_config(
            {
                "completion_wait_profile": "hook_light",
                "completion_wait_seconds": "3.5",
                "completion_wait_poll_seconds": "0.4",
                "completion_wait_event_driven": False,
            }
        )
        unknown_timeout, unknown_poll, unknown_event_driven = builder.role_queue_completion_wait_config(
            {"completion_wait_profile": "unknown-profile"}
        )

        self.assertEqual(timeout_seconds, 0.75)
        self.assertEqual(poll_interval_seconds, 0.1)
        self.assertTrue(event_driven)
        self.assertEqual(explicit_timeout, 3.5)
        self.assertEqual(explicit_poll, 0.4)
        self.assertFalse(explicit_event_driven)
        self.assertEqual(unknown_timeout, 0.0)
        self.assertEqual(unknown_poll, 0.25)
        self.assertTrue(unknown_event_driven)

    def test_queue_watch_daemon_records_event_wait_between_cycles(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "test",
                    "task_id": "ENTRY-test-session-watch-daemon-event",
                    "message_id": "msg-watch-daemon-event",
                    "report_id": "rep-watch-daemon-event",
                    "instruction": "Process queue message.",
                },
                extra_args=["--dry-run"],
            )

            event_wait = {
                "result": "event",
                "mode": "kqueue",
                "wait_seconds": 5.0,
                "event_count": 1,
                "watch_dir_count": 2,
                "event_paths": [str(state_root / "test-session" / "queue" / "reports")],
            }
            with mock.patch.object(builder, "queue_watch_wait_for_event", return_value=event_wait) as wait:
                output = builder.role_queue_watch_daemon(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "role_ids": "gate-prompt-formatter",
                        "dry_run": True,
                        "max_cycles": "2",
                        "poll_interval_seconds": "5",
                    },
                )

            summary = output["queueWatchDaemon"]
            self.assertTrue(summary["event_driven"])
            self.assertEqual(summary["event_wait_count"], 1)
            self.assertEqual(summary["event_wakeup_count"], 1)
            self.assertEqual(summary["event_waits"], [event_wait])
            self.assertEqual(summary["cycles"][0]["event_wait"], event_wait)
            wait.assert_called_once()
            self.assertTrue(wait.call_args.kwargs["event_driven"])

    def test_queue_watch_event_wait_can_use_sleep_mode(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            queue_root.mkdir()
            with mock.patch.object(builder.time, "sleep") as sleep:
                result = builder.queue_watch_wait_for_event(
                    queue_root=queue_root,
                    timeout_seconds=0.25,
                    event_driven=False,
                )

            self.assertEqual(result["result"], "slept")
            self.assertEqual(result["mode"], "sleep")
            self.assertEqual(result["wait_seconds"], 0.25)
            sleep.assert_called_once_with(0.25)

    def test_queue_replay_failed_requeues_dead_letter_with_new_report_path(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-replay",
                    "message_id": "msg-replay",
                    "report_id": "rep-replay",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 2
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            builder.role_queue_watch_once(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "max_retries": 2,
                },
            )
            old_report_path = queued["roleQueue"]["report_path"]

            output = self.run_builder(
                state_root,
                "queue-replay-failed",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-replay",
                    "reason": "operator approved replay",
                },
            )

            summary = output["queueReplayFailed"]
            self.assertEqual(summary["result"], "replayed")
            self.assertEqual(summary["replayed_count"], 1)
            self.assertEqual(summary["failed_replay_policy"], "manual_explicit_replay_only")
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            message = inbox["messages"][0]
            self.assertEqual(message["status"], "pending")
            self.assertEqual(message["retry_count"], 0)
            self.assertEqual(message["replay_count"], 1)
            self.assertEqual(message["previous_report_path"], "reports/gate-prompt-formatter/ENTRY-test-session-replay/rep-replay.yaml")
            self.assertIn("replay-", message["payload"]["report_path"])
            self.assertNotEqual(str(session_dir / "queue" / message["payload"]["report_path"]), old_report_path)
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "queue_failed_replay" and item["result"] == "replayed" for item in events))

    def test_queue_replay_failed_cli_args_select_session_and_role_without_stdin(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="target-session")
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-target-session-replay-cli",
                    "message_id": "msg-replay-cli",
                    "report_id": "rep-replay-cli",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["retry_count"] = 2
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            builder.role_queue_watch_once(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": "target-session",
                    "role_id": "gate-prompt-formatter",
                    "max_retries": 2,
                },
            )
            self.bootstrap(state_root, session_id="stale-last-session")

            output = self.run_builder_without_input(
                state_root,
                "queue-replay-failed",
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--dry-run",
                ],
            )

            summary = output["queueReplayFailed"]
            self.assertEqual(summary["result"], "dry_run")
            self.assertEqual(summary["role_id"], "gate-prompt-formatter")
            self.assertEqual(summary["candidate_count"], 1)
            self.assertEqual(summary["replayed_count"], 1)
            self.assertIn("target-session/queue", summary["queue_root"])
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "failed")

    def test_queue_close_message_marks_pending_superseded_without_provider_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="target-session")
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "target-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-target-session-close",
                    "message_id": "msg-close",
                    "report_id": "rep-close",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            self.bootstrap(state_root, session_id="stale-last-session")

            output = self.run_builder_without_input(
                state_root,
                "queue-close-message",
                extra_args=[
                    "--session-id",
                    "target-session",
                    "--role-id",
                    "gate-prompt-formatter",
                    "--message-id",
                    "msg-close",
                    "--reason",
                    "stale entry prompt superseded",
                ],
            )

            summary = output["queueCloseMessage"]
            self.assertEqual(summary["result"], "superseded")
            self.assertEqual(summary["closed_count"], 1)
            self.assertEqual(summary["manual_close_policy"], "explicit_message_id_only")
            self.assertFalse(summary["dry_run"])
            self.assertFalse(summary["message"]["roster_updated"])
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            message = inbox["messages"][0]
            self.assertEqual(message["status"], "failed")
            self.assertEqual(message["manual_close_reason"], "stale entry prompt superseded")
            self.assertEqual(message["manual_close_result"], "superseded")
            self.assertEqual(message["report_path"], "reports/gate-prompt-formatter/ENTRY-target-session-close/rep-close.yaml")
            report_path = Path(queued["roleQueue"]["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["report_type"], "queue_manual_close")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["result"], "superseded")
            self.assertEqual(report["schema_validation"]["status"], "valid")
            self.assertEqual(message["report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "queue_manual_close" and item["result"] == "superseded" for item in events))
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "manual_close" and item["result"] == "superseded" for item in metrics))

    def test_queue_close_message_dry_run_does_not_mutate_pending_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-close-dry-run",
                    "message_id": "msg-close-dry-run",
                    "report_id": "rep-close-dry-run",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            before_inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            before_events = (session_dir / "queue-events.jsonl").read_text(encoding="utf-8")
            before_metrics = (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8")

            output = self.run_builder(
                state_root,
                "queue-close-message",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-close-dry-run",
                    "reason": "preview stale close",
                },
                extra_args=["--dry-run"],
            )

            summary = output["queueCloseMessage"]
            self.assertEqual(summary["result"], "dry_run")
            self.assertEqual(summary["closed_count"], 0)
            self.assertTrue(summary["dry_run"])
            self.assertTrue(summary["message"]["dry_run"])
            self.assertIn("would_write_report", summary["message"])
            self.assertIn("would_update_inbox", summary["message"])
            self.assertFalse(Path(queued["roleQueue"]["report_path"]).exists())
            self.assertEqual(json.loads(inbox_path.read_text(encoding="utf-8")), before_inbox)
            self.assertEqual((session_dir / "queue-events.jsonl").read_text(encoding="utf-8"), before_events)
            self.assertEqual((session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8"), before_metrics)

    def test_queue_close_message_requires_explicit_pending_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            output = self.run_builder(
                state_root,
                "queue-close-message",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("explicit message_id", output["reason"])

    def test_queue_close_message_blocks_when_terminal_report_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-close-existing-report",
                    "message_id": "msg-close-existing-report",
                    "report_id": "rep-close-existing-report",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            report_path = Path(queued["roleQueue"]["report_path"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            original_report = {
                "report_version": "1",
                "report_id": "rep-close-existing-report",
                "report_type": "role_queue_report",
                "from_role": "gate-prompt-formatter",
                "task_id": "ENTRY-test-session-close-existing-report",
                "unit_id": "",
                "message_id": "msg-close-existing-report",
                "created_at": "2026-06-14T00:00:00+09:00",
                "result": "gate_intake_envelope_created",
                "status": "done",
                "summary": "Provider report already exists.",
                "provider_evidence": {
                    "provider": "anthropic",
                    "provider_session_id": "provider-session",
                    "request_id": "request-existing",
                    "effective_model": "claude-sonnet-4-20250514",
                    "usage_source": "provider_authored_role_report",
                    "transcript_path": "/tmp/transcript.jsonl",
                },
            }
            report_path.write_text(json.dumps(original_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "queue-close-message",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-close-existing-report",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("run queue-watch recovery", output["reason"])
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), original_report)
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")

    def test_queue_close_message_treats_missing_status_as_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-close-missing-status",
                    "message_id": "msg-close-missing-status",
                    "report_id": "rep-close-missing-status",
                    "instruction": "Create a thin envelope.",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0].pop("status")
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "queue-close-message",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "message_id": "msg-close-missing-status",
                    "reason": "legacy pending close",
                },
            )

            summary = output["queueCloseMessage"]
            self.assertEqual(summary["result"], "superseded")
            self.assertEqual(summary["closed_count"], 1)
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "failed")
            self.assertEqual(inbox["messages"][0]["manual_close_reason"], "legacy pending close")
            self.assertEqual(summary["message"]["status"], "failed")

    def test_role_agent_worker_blocks_pending_message_without_provider_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-entry-1",
                    "message_id": "msg-entry",
                    "report_id": "rep-entry",
                    "instruction": "要件にして",
                    "payload_type": "human_prompt",
                    "expected_output": "gate_intake_envelope",
                },
                extra_args=["--dry-run"],
            )
            report_path = Path(queued["roleQueue"]["report_path"])

            output = self.run_builder(
                state_root,
                "role-agent-worker",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "max_messages": "1",
                },
            )

            summary = output["roleAgentWorker"]
            self.assertEqual(summary["result"], "worker_complete")
            self.assertEqual(summary["messages_processed"], 0)
            self.assertEqual(summary["messages_blocked"], 1)
            self.assertFalse(report_path.exists())
            self.assertIn("no longer claims pending messages without provider evidence", summary["steps"][0]["error"])
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "pending")

    def test_role_agent_worker_records_supplied_provider_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-creator",
                    "from_role": "gate-prompt-formatter",
                    "task_id": "TSK-9999",
                    "message_id": "msg-gtc",
                    "report_id": "rep-gtc",
                    "instruction": "Create Task Detail",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-agent-worker",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-creator",
                    "provider_evidence": {
                        "provider_session_id": "claude-session-1",
                        "effective_model": "claude-haiku-4-5",
                        "usage_source": "claude_tmux_interactive",
                        "request_id": "req-1",
                        "transcript_path": "reports/evidence/gtc.jsonl",
                        "input_tokens": 10,
                        "output_tokens": 20,
                    },
                },
            )

            report_path = Path(output["roleAgentWorker"]["steps"][0]["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["evidence"]["provider_session_id"], "claude-session-1")
            self.assertEqual(report["evidence"]["effective_model"], "claude-haiku-4-5")
            self.assertEqual(report["evidence"]["usage_source"], "claude_tmux_interactive")
            self.assertEqual(report["evidence"]["input_tokens"], 10)
            self.assertEqual(report["schema_validation"]["status"], "valid")
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-task-creator.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(output["roleAgentWorker"]["steps"][0]["report_integrity"]["sha256"], inbox["messages"][0]["report_sha256"])

    def test_gate_flow_e2e_fixture_processes_gpf_to_command_chain(self) -> None:
        fixture_path = SKILL_ROOT / "tests" / "fixtures" / "gate_flow_e2e_fixture.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)

            gate_entry = fixture["gate_entry"]
            queued = self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": gate_entry["role_id"],
                    "from_role": gate_entry["from_role"],
                    "task_id": fixture["task_id"],
                    "message_id": gate_entry["message_id"],
                    "report_id": gate_entry["report_id"],
                    "instruction": gate_entry["instruction"],
                    "expected_output": gate_entry["expected_output"],
                    "vault_root": str(vault_root),
                    "defer_nudge": True,
                },
            )
            self.assertIn(queued["roleQueue"]["result"], {"queued", "queued_nudge_unconfirmed"})

            gate_report = gate_entry["report"]
            gpf_output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": gate_entry["role_id"],
                    "message_id": gate_entry["message_id"],
                    "status": gate_report["status"],
                    "result": gate_report["result"],
                    "summary": gate_report["summary"],
                    "gate_intake_envelope": self.sample_gate_intake_envelope(gate_report["workflow_mode"]),
                    "provider_evidence": gate_report["provider_evidence"],
                },
            )
            gpf_summary = gpf_output["roleReport"]
            self.assertEqual(gpf_summary["result"], "done")

            expected_command = fixture["expected_command_handoff"]
            auto_handoff = gpf_summary["auto_handoff"]
            self.assertIn(auto_handoff["result"], {"queued", "queued_nudge_unconfirmed"})
            self.assertEqual(auto_handoff["handoff_type"], expected_command["handoff_type"])
            self.assertEqual(auto_handoff["from_role"], expected_command["from_role"])
            self.assertEqual(auto_handoff["to_role"], expected_command["to_role"])
            self.assertEqual(auto_handoff["command"]["command"], expected_command["command"])
            self.assertTrue(auto_handoff["command"]["passed"])
            scaffold = auto_handoff["command"]["command_output"]["gtcScaffold"]
            self.assertEqual(scaffold["result"], expected_command["expected_result"])
            self.assertTrue(Path(scaffold["task_detail_path"]).is_relative_to(vault_root))
            task_text = Path(scaffold["task_detail_path"]).read_text(encoding="utf-8")
            self.assertIn("## Team Routing Decision", task_text)
            self.assertIn("| gate-task-creator | builder | deterministic | deterministic | gtc-scaffold |", task_text)

            for role_id in fixture["forbidden_inboxes"]:
                self.assertFalse((session_dir / "queue" / "inbox" / f"{role_id}.yaml").exists())

            tpm_inbox = json.loads((session_dir / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8"))
            tpm_message = tpm_inbox["messages"][0]
            self.assertEqual(tpm_message["from_role"], expected_command["command_owner_role"])
            self.assertEqual(tpm_message["to_role"], "teams-project-manager")
            self.assertEqual(tpm_message["payload"]["type"], fixture["tpm_report"]["message_source"])
            self.assertEqual(tpm_message["task_id"], scaffold["task_id"])
            self.assertEqual(tpm_message["payload"]["context_ref"], scaffold["task_detail_path"])

            tpm_report = fixture["tpm_report"]
            tpm_output = self.run_builder(
                state_root,
                "role-report",
                {
                    "session_id": "test-session",
                    "role_id": tpm_report["role_id"],
                    "message_id": tpm_message["message_id"],
                    "status": tpm_report["status"],
                    "result": tpm_report["result"],
                    "summary": tpm_report["summary"],
                    "report_extra": tpm_report["report_extra"],
                    "provider_evidence": tpm_report["provider_evidence"],
                },
            )
            tpm_summary = tpm_output["roleReport"]
            self.assertEqual(tpm_summary["team_completion_update"]["result"], "updated")
            self.assertEqual(tpm_summary["auto_handoff"]["precheck"]["result"], "passed")
            gate_command = tpm_summary["auto_handoff"]["precheck"]["command_output"]["gateCommand"]
            self.assertEqual(gate_command["command"], fixture["expected_next_queue"]["precheck_command"])
            self.assertEqual(gate_command["status"], "pass")
            self.assertTrue(gate_command["next_phase_allowed"])

            evaluator_inbox = json.loads(
                (session_dir / "queue" / "inbox" / f"{fixture['expected_next_queue']['role_id']}.yaml").read_text(encoding="utf-8")
            )
            evaluator_message = evaluator_inbox["messages"][0]
            self.assertEqual(evaluator_message["from_role"], fixture["expected_next_queue"]["from_role"])
            self.assertEqual(evaluator_message["to_role"], fixture["expected_next_queue"]["role_id"])
            self.assertEqual(
                evaluator_message["payload"]["precheck_result"]["command_output"]["gateCommand"]["command"],
                fixture["expected_next_queue"]["precheck_command"],
            )
            for role_id in fixture["forbidden_inboxes"]:
                self.assertFalse((session_dir / "queue" / "inbox" / f"{role_id}.yaml").exists())

            queue_events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            forbidden_roles = set(fixture["forbidden_inboxes"])
            forbidden_role_queue_events = [
                item
                for item in queue_events
                if item.get("event_type") == "role_queue"
                and (item.get("role_id") in forbidden_roles or item.get("to_role") in forbidden_roles)
            ]
            self.assertEqual(forbidden_role_queue_events, [])

            metrics_path = session_dir / "gate-metrics.jsonl"
            metrics = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
            finalized = [item for item in metrics if item["event_type"] == "finalized"]
            finalized_roles = {item["role_id"] for item in finalized}
            self.assertIn("gate-prompt-formatter", finalized_roles)
            self.assertIn("teams-project-manager", finalized_roles)
            gtc_finalized = [item for item in finalized if item.get("role_id") == "gate-task-creator"]
            self.assertTrue(gtc_finalized)
            self.assertTrue(all(item.get("usage_source") == "builder_command" for item in gtc_finalized))
            self.assertTrue(all(item.get("completion_source") == "gtc-scaffold_command" for item in gtc_finalized))
            self.assertNotIn("gate-task-assessor", finalized_roles)
            self.assertNotIn("gate-task-guardian", finalized_roles)
            forbidden_metric_events = [
                item
                for item in metrics
                if (item.get("role_id") in forbidden_roles or item.get("agent_id") in forbidden_roles)
                and item.get("event_type") in {"queued", "finalized", "agent_dispatch", "provider_activation"}
                and not (
                    item.get("role_id") == "gate-task-creator"
                    and item.get("event_type") == "finalized"
                    and item.get("usage_source") == "builder_command"
                    and item.get("completion_source") == "gtc-scaffold_command"
                )
            ]
            self.assertEqual(forbidden_metric_events, [])

    def test_role_agent_worker_rejects_provider_evidence_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-creator",
                    "from_role": "gate-prompt-formatter",
                    "task_id": "TSK-9999",
                    "message_id": "msg-bad-provider",
                    "report_id": "rep-bad-provider",
                    "instruction": "Create Task Detail",
                },
                extra_args=["--dry-run"],
            )

            output = self.run_builder(
                state_root,
                "role-agent-worker",
                {
                    "session_id": "test-session",
                    "role_id": "gate-task-creator",
                    "provider_evidence": {
                        "provider_session_id": "codex-session-1",
                        "effective_model": "gpt-5.5",
                        "usage_source": "codex_exec",
                        "request_id": "req-1",
                        "transcript_path": "reports/evidence/gtc.jsonl",
                    },
                },
            )

            step = output["roleAgentWorker"]["steps"][0]
            self.assertEqual(step["result"], "message_failed")
            self.assertIn("provider mismatch", step["error"])
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-task-creator.yaml").read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["status"], "failed")
            report = json.loads(Path(step["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")

    def test_role_agent_worker_does_not_claim_processing_message_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            self.run_builder(
                state_root,
                "role-queue",
                {
                    "session_id": "test-session",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "task_id": "ENTRY-test-session-entry-1",
                    "message_id": "msg-processing",
                    "report_id": "rep-processing",
                    "instruction": "要件にして",
                },
                extra_args=["--dry-run"],
            )
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            inbox["messages"][0]["status"] = "processing"
            inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "role-agent-worker",
                {"session_id": "test-session", "role_id": "gate-prompt-formatter"},
            )

            summary = output["roleAgentWorker"]
            self.assertEqual(summary["messages_processed"], 0)
            self.assertEqual(summary["steps"][0]["result"], "idle")

    def test_role_agent_worker_is_idle_without_pending_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "role-agent-worker",
                {"session_id": "test-session", "role_id": "gate-prompt-formatter"},
            )

            summary = output["roleAgentWorker"]
            self.assertEqual(summary["result"], "worker_complete")
            self.assertEqual(summary["messages_processed"], 0)
            self.assertEqual(summary["steps"][0]["result"], "idle")

    def test_prompt_preflight_persists_event_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(state_root, "prompt-preflight", {"prompt": "hello"})
            context = output["hookSpecificOutput"]["additionalContext"]

            self.assertNotEqual(output.get("decision"), "block")
            self.assertIn("readiness_scope", context)
            self.assertIn("Act on this context silently", context)
            self.assertNotIn("Do not bypass Gate entry before task work", context)
            self.assertTrue((session_dir / "preflight-events.jsonl").exists())
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_ready")
            self.assertTrue(event["fallback_session_used"])
            self.assertTrue(
                (session_dir / "last_event").read_text(encoding="utf-8").startswith("UserPromptSubmit ")
            )

    def test_prompt_preflight_compacts_unchanged_ready_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            first = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "ok"},
            )
            first_context = first["hookSpecificOutput"]["additionalContext"]
            self.assertIn("resident_agents_registered", first_context)

            second = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "ok"},
            )
            second_context = second["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Readiness is unchanged", second_context)
            self.assertIn("Act on this context silently", second_context)
            self.assertIn("preflight_context_hash", second_context)
            self.assertNotIn("resident_agents_registered", second_context)
            self.assertNotIn("Do not bypass Gate entry before task work", second_context)

            events = [
                json.loads(line)
                for line in (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(events[-2]["context_compacted"])
            self.assertTrue(events[-1]["context_compacted"])
            self.assertEqual(events[-2]["preflight_context_hash"], events[-1]["preflight_context_hash"])
            self.assertTrue((session_dir / "preflight-context-hash.json").exists())

    def test_prompt_preflight_compaction_ignores_volatile_queue_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "整理して",
                    "entry_id": "entry-one",
                    "message_id": "msg-one",
                    "report_id": "rep-one",
                    "dry_run": True,
                    "force_nudge": True,
                },
            )
            second = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "整理して",
                    "entry_id": "entry-two",
                    "message_id": "msg-two",
                    "report_id": "rep-two",
                    "dry_run": True,
                    "force_nudge": True,
                },
            )

            second_context = second["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Readiness is unchanged", second_context)
            events = [
                json.loads(line)
                for line in (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(events[-1]["context_compacted"])
            self.assertEqual(events[-2]["preflight_context_hash"], events[-1]["preflight_context_hash"])
            self.assertNotEqual(events[-2]["gate_entry_queue"]["task_id"], events[-1]["gate_entry_queue"]["task_id"])
            self.assertNotEqual(events[-2]["gate_entry_queue"]["message_id"], events[-1]["gate_entry_queue"]["message_id"])

    def test_context_surface_report_measures_add_dirs_and_preflight_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            session_dir = self.bootstrap(state_root, session_id="surface-session")
            cwd = root / "cwd"
            cwd.mkdir()
            context_dir = root / "context"
            (context_dir / "nested").mkdir(parents=True)
            (context_dir / ".git").mkdir()
            (context_dir / "a.txt").write_text("abcd", encoding="utf-8")
            (context_dir / "nested" / "b.md").write_text("hello world", encoding="utf-8")
            (context_dir / ".git" / "ignored").write_text("ignored bytes", encoding="utf-8")
            (session_dir / "preflight-events.jsonl").write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"event_type": "prompt_preflight", "result": "preflight_ready", "context_compacted": False},
                        {"event_type": "prompt_preflight", "result": "preflight_ready", "context_compacted": True},
                        {"event_type": "prompt_preflight", "result": "preflight_degraded", "context_compacted": True},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ | {"ITB_PROVIDER_ADD_DIRS": str(context_dir)}

            output = self.run_builder(
                state_root,
                "context-surface-report",
                {
                    "session_id": "surface-session",
                    "role_id": "gate-prompt-formatter",
                    "cwd": str(cwd),
                    "include_queue_root": False,
                    "max_files": 10,
                    "max_bytes": 1000,
                    "max_depth": 4,
                    "max_events": 10,
                },
                env=env,
            )

            report = output["contextSurfaceReport"]
            self.assertEqual(report["role_count"], 1)
            self.assertEqual(report["roles_with_add_dirs"], 1)
            role = report["roles"][0]
            self.assertEqual(role["agent_id"], "gate-prompt-formatter")
            self.assertEqual(role["effective_add_dirs"], [str(context_dir.resolve())])
            self.assertEqual(role["sampled_bytes"], 15)
            self.assertEqual(role["rough_tokens"], 4)
            self.assertFalse(role["truncated"])
            self.assertEqual(role["paths"][0]["file_count"], 2)
            self.assertGreaterEqual(role["paths"][0]["skipped_dir_count"], 1)
            self.assertEqual(report["preflight_compaction"]["sampled_event_count"], 3)
            self.assertEqual(report["preflight_compaction"]["compacted_count"], 2)
            self.assertEqual(report["preflight_compaction"]["compaction_hit_rate"], 0.6667)
            self.assertTrue(Path(report["artifact_path"]).exists())

    def test_context_surface_report_caps_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            self.bootstrap(state_root, session_id="surface-cap-session")
            cwd = root / "cwd"
            cwd.mkdir()
            context_dir = root / "context"
            context_dir.mkdir()
            for index in range(5):
                (context_dir / f"file-{index}.txt").write_text("0123456789", encoding="utf-8")
            env = os.environ | {"ITB_PROVIDER_ADD_DIRS": str(context_dir)}

            output = self.run_builder(
                state_root,
                "context-surface-report",
                {
                    "session_id": "surface-cap-session",
                    "role_id": "gate-prompt-formatter",
                    "cwd": str(cwd),
                    "include_queue_root": False,
                    "max_files": 2,
                    "max_bytes": 1000,
                    "max_depth": 2,
                },
                env=env,
            )

            role = output["contextSurfaceReport"]["roles"][0]
            self.assertTrue(role["truncated"])
            self.assertEqual(role["paths"][0]["file_count"], 2)
            self.assertEqual(role["paths"][0]["truncation_reason"], "max_files")

    def test_pre_gpf_classifier_marks_simple_read_only_prompt_low_risk(self) -> None:
        builder = load_builder_module()

        verdict = builder.classify_gate_entry_prompt({"prompt": "今の状態を教えて？"})

        self.assertEqual(verdict["workflow_mode"], "controlled_micro_flow")
        self.assertEqual(verdict["risk_tier"], "low")
        self.assertTrue(verdict["read_only"])
        self.assertTrue(verdict["single_deliverable"])
        self.assertEqual(verdict["fast_path_candidate"], "read_only_no_diff_single_team")

    def test_pre_gpf_classifier_escalates_write_or_security_prompt(self) -> None:
        builder = load_builder_module()

        write_verdict = builder.classify_gate_entry_prompt({"prompt": "このファイルを修正して"})
        security_verdict = builder.classify_gate_entry_prompt({"prompt": "本番の認証トークンを更新して"})

        self.assertEqual(write_verdict["workflow_mode"], "strict_flow")
        self.assertEqual(write_verdict["risk_tier"], "normal")
        self.assertIn("修正", write_verdict["escalation_triggers"])
        self.assertEqual(security_verdict["workflow_mode"], "strict_flow")
        self.assertEqual(security_verdict["risk_tier"], "high")
        self.assertTrue(security_verdict["approval_required"])

    def test_pre_gpf_classifier_benchmark_fixture_cases(self) -> None:
        builder = load_builder_module()
        fixture_path = SKILL_ROOT / "tests" / "fixtures" / "pre_gpf_classifier_cases.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        for case in fixture["cases"]:
            with self.subTest(case_id=case["case_id"]):
                verdict = builder.classify_gate_entry_prompt({"prompt": case["prompt"]})
                expected = case["expected"]
                for key in (
                    "workflow_mode",
                    "risk_tier",
                    "read_only",
                    "inspection_required",
                    "fast_path_candidate",
                ):
                    self.assertEqual(verdict[key], expected[key], key)
                for key in (
                    "escalation_triggers_include",
                    "suppressed_escalation_triggers_include",
                    "inspection_signals_include",
                ):
                    for item in expected.get(key, []):
                        verdict_key = key.removesuffix("_include")
                        self.assertIn(item, verdict[verdict_key])

    def test_prompt_preflight_standard_read_only_research_uses_gate_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "このコードの責務を調査して要約して",
                    "entry_id": "entry-standard-research",
                    "message_id": "msg-standard-research",
                    "report_id": "rep-standard-research",
                },
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Gate Entry Queue", context)
            self.assertNotIn("Controlled Micro-Flow Fast Path", context)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["pre_gpf_classifier"]["workflow_mode"], "standard_flow")
            self.assertEqual(event["pre_gpf_classifier"]["risk_tier"], "normal")
            self.assertTrue(event["pre_gpf_classifier"]["inspection_required"])
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            classifier = inbox["messages"][0]["payload"]["pre_gpf_classifier"]
            self.assertEqual(classifier["workflow_mode"], "standard_flow")
            self.assertEqual(classifier["fast_path_candidate"], "not_eligible")

    def test_prompt_preflight_micro_fast_path_collapses_read_only_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            repo_root = Path(tmp) / "repo"
            self.init_git_repo(repo_root)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "今の状態を教えて？",
                    "cwd": str(repo_root),
                },
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Controlled Micro-Flow Fast Path", context)
            self.assertIn("main_transport_renderer", context)
            self.assertFalse((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").exists())
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_micro_fast_path")
            micro = event["micro_fast_path"]
            self.assertEqual(micro["status"], "pass")
            self.assertEqual(micro["result"], "micro_fast_path_complete")
            self.assertEqual(micro["role_provider_turns"], 0)
            self.assertEqual(micro["git_diff_status"], "no_diff")
            self.assertEqual(micro["notification_class"], "done")
            self.assertTrue(Path(micro["artifact_path"]).exists())
            self.assertTrue(Path(micro["tpm_completion_artifact_path"]).exists())
            self.assertTrue(Path(micro["evaluation_artifact_path"]).exists())
            self.assertTrue(Path(micro["finalization_artifact_path"]).exists())
            tpm_completion = json.loads(Path(micro["tpm_completion_artifact_path"]).read_text(encoding="utf-8"))
            self.assertEqual(tpm_completion["next_action"], "queue_gate_task_evaluator")
            self.assertEqual(tpm_completion["llm_dispatch_policy"], "skip_assessor_runtime_queue_evaluator")
            self.assertEqual(tpm_completion["missing_evidence"], [])
            self.assertEqual(tpm_completion["blockers"], [])
            self.assertTrue(tpm_completion["next_phase_allowed"])
            finalization = json.loads(Path(micro["finalization_artifact_path"]).read_text(encoding="utf-8"))
            self.assertEqual(finalization["notification_class"], "done")
            self.assertEqual(finalization["next_action"], "handoff_to_main_transport_renderer")
            self.assertEqual(finalization["llm_dispatch_policy"], "skip_guardian_runtime_render_final")
            self.assertEqual(finalization["missing_evidence"], [])
            self.assertEqual(finalization["blockers"], [])
            self.assertTrue(finalization["next_phase_allowed"])

    def test_prompt_preflight_micro_fast_path_falls_back_to_gate_queue_for_dirty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            repo_root = Path(tmp) / "repo"
            self.init_git_repo(repo_root)
            (repo_root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "今の状態を教えて？",
                    "cwd": str(repo_root),
                    "entry_id": "entry-dirty-fastpath",
                    "message_id": "msg-dirty-fastpath",
                    "report_id": "rep-dirty-fastpath",
                    "git_diff_status": "no_diff",
                },
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Gate Entry Queue", context)
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            self.assertTrue(inbox_path.exists())
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_degraded")
            self.assertEqual(event["micro_fast_path"]["status"], "block")
            self.assertEqual(event["micro_fast_path"]["git_diff_status"], "dirty")
            self.assertIn("git status is dirty", event["micro_fast_path"]["reason"])
            self.assertEqual(event["gate_entry_queue"]["message_id"], "msg-dirty-fastpath")

    def test_prompt_preflight_records_pre_gpf_classifier_in_queue_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "今の状態を教えて？",
                    "entry_id": "entry-classifier",
                    "message_id": "msg-classifier",
                    "report_id": "rep-classifier",
                    "force_gate_entry_queue": True,
                    "dry_run": True,
                },
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("classifier_workflow_mode", context)
            self.assertIn("controlled_micro_flow", context)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["pre_gpf_classifier"]["workflow_mode"], "controlled_micro_flow")
            self.assertEqual(event["pre_gpf_classifier"]["risk_tier"], "low")
            inbox = json.loads((session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8"))
            classifier = inbox["messages"][0]["payload"]["pre_gpf_classifier"]
            self.assertEqual(classifier["workflow_mode"], "controlled_micro_flow")
            self.assertEqual(classifier["fast_path_candidate"], "read_only_no_diff_single_team")

    def test_gate_entry_dispatch_prompt_uses_pre_gpf_classifier_defaults(self) -> None:
        builder = load_builder_module()

        prompt = builder.build_gate_entry_dispatch_prompt(
            user_prompt="今の状態を教えて？",
            task_detail_path="",
            flow_phase="pre_execution",
            task_context_source="",
            classifier={
                "workflow_mode": "controlled_micro_flow",
                "risk_tier": "low",
                "fast_path_candidate": "read_only_no_diff_single_team",
            },
        )

        self.assertIn("workflow_mode: controlled_micro_flow", prompt)
        self.assertIn("risk_tier: low", prompt)
        self.assertIn("classifier_fast_path_candidate: read_only_no_diff_single_team", prompt)

    def test_prompt_preflight_enqueues_gate_entry_by_default_for_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "整理して",
                    "entry_id": "entry-1",
                    "message_id": "msg-entry",
                    "report_id": "rep-entry",
                    "cwd": "/tmp/project",
                },
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Gate Entry Queue", context)
            self.assertIn("gate-prompt-formatter", context)
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            payload_path = session_dir / "queue" / "tasks" / "ENTRY-test-session-entry-1" / "msg-entry.yaml"
            self.assertTrue(inbox_path.exists())
            self.assertTrue(payload_path.exists())
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["role_id"], "gate-prompt-formatter")
            self.assertEqual(inbox["messages"][0]["payload"]["type"], "human_prompt")
            self.assertEqual(inbox["messages"][0]["payload"]["expected_output"], "gate_intake_envelope")
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["instruction"], "整理して")
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_degraded")
            self.assertTrue(event["preflight_degraded"])
            self.assertTrue(
                any(reason.startswith("gate_entry_queue:") for reason in event["preflight_degraded_reasons"])
            )
            self.assertEqual(event["gate_entry_queue"]["role_id"], "gate-prompt-formatter")
            self.assertEqual(event["gate_entry_queue"]["result"], "queued_nudge_unconfirmed")

    def test_prompt_preflight_enqueues_gate_entry_for_claude_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "test-session", "cwd": "/tmp", "source": "SessionStart"},
                runtime="claude",
            )
            session_dir = state_root / "test-session"

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "要件にして",
                    "entry_id": "entry-claude",
                    "message_id": "msg-claude-entry",
                    "report_id": "rep-claude-entry",
                },
                runtime="claude",
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Gate Entry Queue", context)
            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            self.assertEqual(inbox["messages"][0]["from_role"], "claude-user-prompt-submit")
            self.assertEqual(inbox["messages"][0]["task_id"], "ENTRY-test-session-entry-claude")
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["gate_entry_queue"]["message_id"], "msg-claude-entry")

    def test_prompt_preflight_auto_dispatches_gate_entry_when_enabled(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            dispatch_output = {
                "agentDispatch": {
                    "agent_id": "gate-prompt-formatter",
                    "request_id": "req-gpf",
                    "result": "provider_response_ready",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-sonnet-4-6",
                    "response": (
                        "# Normalized Request\n"
                        "## Original Request\nhello\n"
                        "## Intent\nroute through Gate\n"
                        "## Desired Outcome\nvalid envelope\n"
                        "## Scope\nno task work\n"
                        "## Task Units\nunit-1\n"
                        "## Handoff Notes\ngate-task-creator"
                    ),
                }
            }

            with mock.patch.dict(os.environ, {"ITB_GATE_ENTRY_DISPATCH": "1"}, clear=False), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value=dispatch_output,
            ) as agent_dispatch:
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "整理して",
                        "cwd": "/tmp",
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            context = output["hookSpecificOutput"]["additionalContext"]
            call_input = agent_dispatch.call_args.kwargs["hook_input"]
            self.assertIn("Gate Entry Dispatch", context)
            self.assertIn("provider_response_ready", context)
            self.assertIn("# Normalized Request", context)
            self.assertEqual(call_input["agent_id"], "gate-prompt-formatter")
            self.assertEqual(call_input["source_agent"], "codex-user-prompt-submit")
            self.assertEqual(call_input["permission_mode"], "auto")
            self.assertEqual(call_input["tools"], "Read,Grep,Glob,Bash")
            self.assertEqual(call_input["submit_enter_count"], 2)
            self.assertIn("Original Request:\n整理して", call_input["prompt"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_ready")
            self.assertEqual(event["gate_entry_queue"]["role_id"], "gate-prompt-formatter")
            self.assertEqual(event["gate_entry_queue"]["result"], "queued")
            self.assertEqual(event["gate_entry_dispatch"]["result"], "provider_response_ready")
            self.assertEqual(event["gate_entry_dispatch"]["queue_finalize"]["result"], "done")
            inbox = json.loads(
                (session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(inbox["messages"][0]["status"], "done")
            report_path = session_dir / "queue" / inbox["messages"][0]["report_path"]
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "gate_intake_envelope_created")
            self.assertIn("# Normalized Request", report["gate_intake_envelope"])
            self.assertTrue((session_dir / "gate-metrics.jsonl").exists())

    def test_prompt_preflight_uses_codex_exec_for_openai_gate_entry(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row["provider"] = "openai"
                    row["intended_model"] = "gpt-5.5"
                    row["execution_mode"] = "codex"
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            fake_bin = state_root / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            args_file = state_root / "codex-args.txt"
            fake_codex.write_text(
                r"""#!/bin/sh
printf '%s\n' "$@" > "$FAKE_CODEX_ARGS"
printf '%s\n' '{"type":"result","subtype":"success","model":"gpt-5.5","session_id":"codex-session-gpf","request_id":"req-codex-gpf","usage":{"input_tokens":31,"output_tokens":11},"duration_api_ms":789,"num_turns":1,"result":"envelope_version: \"2\"\nsource_type: human_prompt\noriginal_request: |\n  hello\nintent_summary: \"route through Gate\"\ndesired_outcome:\n  deliverables: [\"Gate Intake Envelope\"]\n  done_criteria: [\"ready for GTC\"]\nscope:\n  in: [\"normalize prompt\"]\n  out: [\"task work\"]\napproval_required: false\nworkflow_mode: strict_flow\ntask_units:\n  - unit_id: unit-1\n    title: \"normalize\"\nrouting_hint: \"teams-project-manager\"\nreview_requirements: [\"independent_review\"]\nvault_update_targets: [\"Agents-Vault\"]\nhandoff_notes:\n  gate-task-creator: \"create task\""}'
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            env_path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": env_path,
                    "FAKE_CODEX_ARGS": str(args_file),
                    "ITB_GATE_ENTRY_DISPATCH": "1",
                },
                clear=False,
            ), mock.patch.object(
                builder,
                "agent_dispatch",
                side_effect=AssertionError("OpenAI gate entry should use codex exec one-shot"),
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "entry_id": "entry-codex",
                        "message_id": "msg-codex",
                        "report_id": "rep-codex",
                        "cwd": "/tmp/project",
                        "force_gate_entry_dispatch": True,
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            args = args_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(args[:5], ["exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--json"])
            self.assertEqual(args[args.index("--model") + 1], "gpt-5.5")
            self.assertIn("Provider request:", "\n".join(args))
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["gate_entry_dispatch"]["usage_source"], "codex_exec_json")
            self.assertEqual(event["gate_entry_dispatch"]["dispatch_mode"], "codex_exec_json")
            self.assertEqual(event["gate_entry_dispatch"]["queue_finalize"]["result"], "done")
            report = json.loads(
                (
                    session_dir
                    / "queue"
                    / "reports"
                    / "gate-prompt-formatter"
                    / "ENTRY-test-session-entry-codex"
                    / "rep-codex.yaml"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(report["provider_evidence"]["provider"], "openai")
            self.assertEqual(report["provider_evidence"]["intended_model"], "gpt-5.5")
            self.assertEqual(report["provider_evidence"]["usage_source"], "codex_exec_json")
            self.assertIn('envelope_version: "2"', report["gate_intake_envelope"])
            self.assertTrue(list((session_dir / "provider-exec" / "gate-prompt-formatter").glob("*.jsonl")))

    def test_prompt_preflight_skips_gate_entry_for_notifications_and_short_replies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            cases = [
                "<task-notification>Background command completed</task-notification>",
                "A",
                "再開して",
            ]

            for index, prompt in enumerate(cases):
                with self.subTest(prompt=prompt):
                    output = self.run_builder(
                        state_root,
                        "prompt-preflight",
                        {
                            "session_id": "test-session",
                            "prompt": prompt,
                            "entry_id": f"skip-{index}",
                            "message_id": f"msg-skip-{index}",
                        },
                    )

                    context = output["hookSpecificOutput"]["additionalContext"]
                    self.assertNotIn("Gate Entry Queue", context)

            inbox_path = session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml"
            self.assertFalse(inbox_path.exists())

    def test_prompt_preflight_blocks_when_gate_entry_dispatch_fails(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            with mock.patch.dict(os.environ, {"ITB_GATE_ENTRY_DISPATCH": "1"}, clear=False), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "decision": "block",
                    "reason": "timed out waiting for marker",
                    "agentDispatch": {
                        "request_id": "req-timeout",
                        "result": "provider_response_timeout",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "force_gate_entry_dispatch": True,
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            self.assertIn("Gate entry dispatch failed", output["reason"])
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("provider_response_timeout", context)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")
            self.assertEqual(event["gate_entry_dispatch"]["request_id"], "req-timeout")

    def test_prompt_preflight_blocks_when_gate_entry_dispatch_response_is_invalid(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            with mock.patch.dict(os.environ, {"ITB_GATE_ENTRY_DISPATCH": "1"}, clear=False), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "agentDispatch": {
                        "request_id": "req-empty",
                        "result": "provider_response_ready",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                        "response": "# Normalized Request\n\nok",
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "force_gate_entry_dispatch": True,
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            self.assertIn("provider response is not repairable as Gate Intake Envelope", output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")
            self.assertEqual(
                event["gate_entry_dispatch"]["response_validation_errors"],
                ["provider response is not repairable as Gate Intake Envelope"],
            )
            inbox = json.loads(
                (session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(inbox["messages"][0]["status"], "failed")

    def test_prompt_preflight_accepts_compact_gate_entry_envelope(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            with mock.patch.dict(os.environ, {"ITB_GATE_ENTRY_DISPATCH": "1"}, clear=False), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "agentDispatch": {
                        "request_id": "req-compact",
                        "result": "provider_response_ready",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                        "response": (
                            "scope_in: format prompt\n"
                            "deliverables: Gate Intake Envelope\n"
                            "done_criteria: ready for GTC\n"
                            "requires_human_approval: false\n"
                            "Handoff Notes\n"
                            "- gate-task-creator: create task"
                        ),
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "force_gate_entry_dispatch": True,
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            self.assertNotIn("reason", output)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_ready")
            self.assertEqual(event["gate_entry_dispatch"]["request_id"], "req-compact")

    def test_prompt_preflight_accepts_thin_yaml_gate_entry_and_finalizes_queue(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)

            response = """envelope_version: "2"
source_type: human_prompt
original_request: |
  hello
intent_summary: "route through Gate"
desired_outcome:
  deliverables: ["Gate Intake Envelope"]
  done_criteria: ["ready for GTC"]
scope:
  in: ["normalize prompt"]
  out: ["task work"]
approval_required: false
workflow_mode: strict_flow
task_units:
  - unit_id: unit-1
    title: "normalize"
routing_hint: "teams-project-manager"
review_requirements: ["independent_review"]
vault_update_targets: ["Agents-Vault"]
handoff_notes:
  gate-task-creator: "create task"
"""
            with mock.patch.dict(os.environ, {"ITB_GATE_ENTRY_DISPATCH": "1"}, clear=False), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "agentDispatch": {
                        "request_id": "req-yaml",
                        "result": "provider_response_ready",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                        "target": "itb-org-test:gate-prompt-formatter.0",
                        "response": response,
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "force_gate_entry_dispatch": True,
                        "entry_id": "entry-yaml",
                        "message_id": "msg-yaml",
                        "report_id": "rep-yaml",
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            self.assertNotIn("reason", output)
            inbox = json.loads(
                (session_dir / "queue" / "inbox" / "gate-prompt-formatter.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(inbox["messages"][0]["status"], "done")
            report = json.loads(
                (
                    session_dir
                    / "queue"
                    / "reports"
                    / "gate-prompt-formatter"
                    / "ENTRY-test-session-entry-yaml"
                    / "rep-yaml.yaml"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(report["provider_evidence"]["request_id"], "req-yaml")
            self.assertEqual(report["validation"]["artifact_writer"], "itb_atomic_queue_writer")
            self.assertIn("improvement_log", report)

    def test_prompt_preflight_repairs_partial_gate_entry_fragment(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            partial_response = """main_team: gate
assignee: gate-task-creator
done_criteria: ["ready for GTC"]
routing_hint: "teams-project-manager"
handoff_notes:
  gate-task-creator: "create task"
"""
            with mock.patch.dict(os.environ, {"ITB_GATE_ENTRY_DISPATCH": "1"}, clear=False), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "agentDispatch": {
                        "request_id": "req-partial",
                        "result": "provider_response_ready",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                        "target": "itb-org-test:gate-prompt-formatter.0",
                        "response": partial_response,
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "force_gate_entry_dispatch": True,
                        "entry_id": "entry-partial",
                        "message_id": "msg-partial",
                        "report_id": "rep-partial",
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertTrue(event["gate_entry_dispatch"]["response_repaired"])
            report = json.loads(
                (
                    session_dir
                    / "queue"
                    / "reports"
                    / "gate-prompt-formatter"
                    / "ENTRY-test-session-entry-partial"
                    / "rep-partial.yaml"
                ).read_text(encoding="utf-8")
            )
            self.assertIn('envelope_version: "2"', report["gate_intake_envelope"])
            self.assertIn("provider_fragment", report["gate_intake_envelope"])

    def test_prompt_preflight_auto_enqueues_gate_task_creator_by_default(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)

            response = """envelope_version: "2"
source_type: human_prompt
original_request: |
  hello
intent_summary: "route through Gate"
desired_outcome:
  deliverables: ["Gate Intake Envelope"]
  done_criteria: ["ready for GTC"]
scope:
  in: ["normalize prompt"]
  out: ["task work"]
approval_required: false
workflow_mode: strict_flow
task_units:
  - unit_id: unit-1
    title: "normalize"
routing_hint: "teams-project-manager"
review_requirements: ["independent_review"]
vault_update_targets: ["Agents-Vault"]
handoff_notes:
  gate-task-creator: "create task"
"""
            with mock.patch.dict(
                os.environ,
                {"ITB_GATE_ENTRY_DISPATCH": "1"},
                clear=False,
            ), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "agentDispatch": {
                        "request_id": "req-auto-gtc",
                        "result": "provider_response_ready",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                        "target": "itb-org-test:gate-prompt-formatter.0",
                        "response": response,
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "force_gate_entry_dispatch": True,
                        "entry_id": "entry-auto-gtc",
                        "message_id": "msg-auto-gtc",
                        "report_id": "rep-auto-gtc",
                        "vault_root": str(vault_root),
                    },
                )

            self.assertNotIn("decision", output)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            auto_scaffold = event["gate_entry_dispatch"]["gate_entry_auto_scaffold"]
            self.assertEqual(auto_scaffold["handoff_type"], "command_then_queue")
            self.assertEqual(auto_scaffold["from_role"], "gate-prompt-formatter")
            self.assertEqual(auto_scaffold["to_role"], "teams-project-manager")
            self.assertEqual(auto_scaffold["command"]["command_output"]["gtcScaffold"]["result"], "scaffolded")
            self.assertTrue(auto_scaffold["command_task_id"].startswith("TSK-"))
            self.assertFalse((session_dir / "queue" / "inbox" / "gate-task-creator.yaml").exists())
            tpm_inbox = json.loads(
                (session_dir / "queue" / "inbox" / "teams-project-manager.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(tpm_inbox["messages"][0]["from_role"], "gate-task-creator")
            self.assertEqual(tpm_inbox["messages"][0]["payload"]["type"], "command_completion_chain_handoff")
            self.assertEqual(tpm_inbox["messages"][0]["task_id"], auto_scaffold["command_task_id"])
            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            tpm_queue_events = [
                item for item in events if item.get("event_type") == "role_queue" and item.get("role_id") == "teams-project-manager"
            ]
            self.assertEqual(tpm_queue_events[-1]["sla_threshold_source"], "hop:gate-task-creator->teams-project-manager")

    def test_gtc_scaffold_creates_task_artifacts_and_active_task(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "gtc-scaffold",
                {
                    "session_id": "test-session",
                    "vault_root": str(vault_root),
                    "project": "AI-Agent-Organization",
                    "task_id": "TSK-9999",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("controlled_micro_flow"),
                    "source_report_path": str(session_dir / "queue" / "reports" / "gate-prompt-formatter" / "ENTRY" / "rep.yaml"),
                },
            )

            summary = output["gtcScaffold"]
            task_detail_path = Path(summary["task_detail_path"])
            self.assertEqual(summary["result"], "scaffolded")
            self.assertTrue(summary["shared_lock"]["acquired"])
            self.assertFalse(Path(summary["shared_lock"]["lock_path"]).exists())
            self.assertTrue(task_detail_path.exists())
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("## Project Manager Handoff", task_text)
            self.assertIn("| Handoff To | teams-project-manager |", task_text)
            self.assertIn("## Team Routing Decision", task_text)
            self.assertIn("| Handoff To Director | infra-director |", task_text)
            self.assertIn("## Controlled Micro-Flow", task_text)
            self.assertIn("| gate_intake_envelope_created | true |", task_text)
            self.assertIn("team-completion-check -> gate-task-evaluator", task_text)
            index_text = (vault_root / "00-Inbox&Tasks" / "Task-Index.md").read_text(encoding="utf-8")
            self.assertIn("| TSK-9999 | Deterministic scaffold | infra | infra-team-bootstrap | ready |", index_text)
            kanban_text = (vault_root / "00-Inbox&Tasks" / "Kanban.md").read_text(encoding="utf-8")
            self.assertIn("[[01-Projects/AI-Agent-Organization/TSK-9999-deterministic-scaffold/task|TSK-9999 Deterministic scaffold]]", kanban_text)
            active_task = json.loads((session_dir / "active-task.json").read_text(encoding="utf-8"))
            self.assertEqual(active_task["task_id"], "TSK-9999")
            self.assertEqual(active_task["owner_role"], "gate-task-creator")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            scaffold_metric = next(
                item
                for item in metrics
                if item.get("role_id") == "gate-task-creator" and item.get("command") == "gtc-scaffold"
            )
            self.assertEqual(scaffold_metric["result"], "done")
            self.assertEqual(scaffold_metric["usage_source"], "builder_command")
            self.assertEqual(scaffold_metric["effective_model"], "deterministic")
            self.assertEqual(scaffold_metric["completion_source"], "gtc-scaffold_command")
            self.assertEqual(scaffold_metric["hop_key"], "gate-task-creator->teams-project-manager")
            self.assertEqual(scaffold_metric["sla_threshold_source"], "hop:gate-task-creator->teams-project-manager")
            self.assertGreater(scaffold_metric["duration_sec"], 0)
            errors, warnings = builder.validate_task_flow_artifact(task_detail_path, "pre_execution")
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])
            post_routing_errors, post_routing_warnings = builder.validate_task_flow_artifact(task_detail_path, "post_routing")
            self.assertEqual(post_routing_errors, [])
            self.assertEqual(post_routing_warnings, [])

    def test_gtc_scaffold_missing_required_envelope_fields_creates_triage_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "gtc-scaffold",
                {
                    "session_id": "test-session",
                    "vault_root": str(vault_root),
                    "project": "AI-Agent-Organization",
                    "task_id": "TSK-9998",
                    "gate_intake_envelope": "envelope_version: \"2\"\nsource_type: human_prompt\noriginal_request: |\n  Missing fields\n",
                },
            )

            summary = output["gtcScaffold"]
            self.assertEqual(summary["result"], "scaffolded_triage")
            self.assertIn("intent_summary", summary["missing_envelope_fields"])
            task_text = Path(summary["task_detail_path"]).read_text(encoding="utf-8")
            self.assertIn("| Status | triage |", task_text)
            self.assertIn("intent_summary", task_text)
            index_text = (vault_root / "00-Inbox&Tasks" / "Task-Index.md").read_text(encoding="utf-8")
            self.assertIn("| TSK-9998 | TSK-9998 | gate | teams-project-manager | triage |", index_text)
            kanban_text = (vault_root / "00-Inbox&Tasks" / "Kanban.md").read_text(encoding="utf-8")
            inbox_section = kanban_text.split("## Ready", 1)[0]
            self.assertIn("TSK-9998", inbox_section)

    def test_task_detail_append_writes_thin_report_reference_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)
            scaffold = self.run_builder(
                state_root,
                "gtc-scaffold",
                {
                    "session_id": "test-session",
                    "vault_root": str(vault_root),
                    "project": "AI-Agent-Organization",
                    "task_id": "TSK-9997",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("controlled_micro_flow"),
                    "source_report_path": str(session_dir / "queue" / "reports" / "gate-prompt-formatter" / "ENTRY" / "rep.yaml"),
                },
            )
            task_detail_path = Path(scaffold["gtcScaffold"]["task_detail_path"])
            report_path = session_dir / "queue" / "reports" / "gate-task-evaluator" / "TSK-9997" / "eval.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps({"status": "quality_ok", "summary": "ok"}, sort_keys=True), encoding="utf-8")
            expected_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()

            output = self.run_builder(
                state_root,
                "task-detail-append",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail_path),
                    "section": "Quality Evaluation",
                    "status": "quality_ok",
                    "summary": "Requirements satisfied; see report.",
                    "report_path": str(report_path),
                    "owner_role": "gate-task-evaluator",
                },
            )

            summary = output["taskDetailAppend"]
            self.assertEqual(summary["result"], "updated")
            self.assertEqual(summary["report_sha256"], expected_sha)
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertEqual(task_text.count("## Quality Evaluation"), 1)
            self.assertIn("| Status | quality_ok |", task_text)
            self.assertIn("| Summary | Requirements satisfied; see report. |", task_text)
            self.assertIn(f"| Report SHA256 | {expected_sha} |", task_text)
            self.assertIn("| Owner Role | gate-task-evaluator |", task_text)

            second = self.run_builder(
                state_root,
                "task-detail-append",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail_path),
                    "section": "Quality Evaluation",
                    "status": "needs_rework",
                    "summary": "Updated thin summary.",
                    "report_path": str(report_path),
                    "report_sha256": expected_sha,
                    "owner_role": "gate-task-evaluator",
                },
            )

            self.assertEqual(second["taskDetailAppend"]["result"], "updated")
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertEqual(task_text.count("## Quality Evaluation"), 1)
            self.assertIn("| Status | needs_rework |", task_text)
            self.assertNotIn("| Status | quality_ok |", task_text)

    def test_task_detail_append_auto_runs_vault_final_update_after_publication_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = Path(tmp) / "TSK-9995"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            task_detail_path.write_text(
                "# TSK-9995 Publication\n\n"
                "## Metadata\n\n"
                "| Field | Value |\n|---|---|\n| Task ID | TSK-9995 |\n\n"
                "## Git Publication Manifest\n\n"
                "| Field | Value |\n|---|---|\n"
                "| commit_required | true |\n"
                "| push_required | false |\n"
                "| pr_required | false |\n"
                "| publication_flow | commit_only |\n"
                "| handoff_to | git-publisher |\n",
                encoding="utf-8",
            )
            report_path = session_dir / "queue" / "reports" / "git-publisher" / "TSK-9995" / "publication.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps(
                    {
                        "git_publication_result": {
                            "git_publication_status": "complete",
                            "executor_role": "git-publisher",
                            "executor_session_id": "claude-session-git-publisher",
                            "usage_source": "claude_tmux_interactive",
                            "commit_status": "complete",
                            "commit_hashes": ["abc123"],
                            "committed_diff_matches_snapshot": True,
                            "push_status": "not_required",
                            "pr_status": "not_required",
                            "next_role": "vault_final_update",
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            gate_dir = session_dir / "gates" / "TSK-9995"
            gate_dir.mkdir(parents=True)
            for name, payload in {
                "tpm_completion": {"command": "team-completion-check", "status": "pass"},
                "evaluation": {"command": "evaluator-precheck", "status": "pass"},
            }.items():
                (gate_dir / f"{name}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

            output = self.run_builder(
                state_root,
                "task-detail-append",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail_path),
                    "section": "Git Publication Result",
                    "status": "complete",
                    "summary": "Publication complete; see report.",
                    "report_path": str(report_path),
                    "owner_role": "git-publisher",
                    "auto_vault_final_update": True,
                },
            )

            summary = output["taskDetailAppend"]
            self.assertEqual(summary["result"], "updated")
            self.assertEqual(summary["vault_final_update"]["vaultFinalUpdate"]["status"], "complete")
            self.assertTrue((gate_dir / "vault_final_update.json").exists())
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("## Git Publication Result", task_text)
            self.assertIn("## Vault Final Update", task_text)

    def test_task_detail_append_blocks_auto_vault_final_update_on_invalid_publication_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = Path(tmp) / "TSK-9994"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            task_detail_path.write_text(
                "# TSK-9994 Publication\n\n"
                "## Metadata\n\n"
                "| Field | Value |\n|---|---|\n| Task ID | TSK-9994 |\n\n"
                "## Git Publication Manifest\n\n"
                "| Field | Value |\n|---|---|\n"
                "| commit_required | true |\n"
                "| push_required | false |\n"
                "| pr_required | false |\n"
                "| publication_flow | commit_only |\n",
                encoding="utf-8",
            )
            report_path = session_dir / "queue" / "reports" / "git-publisher" / "TSK-9994" / "publication.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps({"git_publication_result": {"git_publication_status": "complete"}}, sort_keys=True),
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "task-detail-append",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail_path),
                    "section": "Git Publication Result",
                    "status": "complete",
                    "summary": "Publication incomplete.",
                    "report_path": str(report_path),
                    "owner_role": "git-publisher",
                    "auto_vault_final_update": True,
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("commit_required true but Git Publication Result commit_status is not complete", output["reason"])
            self.assertNotIn("## Git Publication Result", task_detail_path.read_text(encoding="utf-8"))

    def test_vault_final_update_rolls_up_compact_gate_artifacts_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = state_root / "TSK-9997"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            task_detail_path.write_text(
                "# TSK-9997 Rollup\n\n"
                "## Metadata\n\n"
                "| Field | Value |\n|---|---|\n| Task ID | TSK-9997 |\n\n"
                "## Finalization Check\n\n"
                "| Field | Value |\n|---|---|\n| Finalization Status | complete |\n",
                encoding="utf-8",
            )
            gate_dir = session_dir / "gates" / "TSK-9997"
            gate_dir.mkdir(parents=True)
            artifacts = {
                "tpm_completion": {"command": "team-completion-check", "status": "pass", "next_action": "queue_gate_task_evaluator"},
                "evaluation": {"command": "evaluator-precheck", "status": "pass", "next_action": "dispatch_thin_gate_task_evaluator_verdict"},
                "finalization": {"command": "finalization-check", "status": "pass", "notification_class": "done"},
            }
            for name, payload in artifacts.items():
                (gate_dir / f"{name}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

            output = self.run_builder(
                state_root,
                "vault-final-update",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9997",
                    "task_detail_path": str(task_detail_path),
                },
            )

            self.assertNotIn("decision", output)
            rollup = output["vaultFinalUpdate"]
            self.assertEqual(rollup["status"], "complete")
            self.assertEqual(rollup["handoff_to"], "finalization-check")
            artifact_path = Path(rollup["artifact_path"])
            self.assertTrue(artifact_path.exists())
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertEqual(task_text.count("## Vault Final Update"), 1)
            self.assertIn("| Status | complete |", task_text)
            self.assertIn("| Gate Artifacts | team-completion-check:pass, evaluator-precheck:pass, finalization-check:pass |", task_text)
            self.assertIn(rollup["artifact_sha256"], task_text)
            self.assertEqual(load_builder_module().file_sha256_if_exists(artifact_path), rollup["artifact_sha256"])

            second = self.run_builder(
                state_root,
                "vault-final-update",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9997",
                    "task_detail_path": str(task_detail_path),
                },
            )

            self.assertEqual(second["vaultFinalUpdate"]["status"], "complete")
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertEqual(task_text.count("## Vault Final Update"), 1)

    def test_vault_final_update_auto_runs_finalization_check_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = state_root / "TSK-9998"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            self.write_task_detail(task_detail_path, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            task_text = task_detail_path.read_text(encoding="utf-8").replace("TSK-9999", "TSK-9998")
            start = task_text.index("## Final Transport Render Check")
            end = task_text.index("## Invocation Evidence", start)
            task_detail_path.write_text(task_text[:start].rstrip() + "\n\n" + task_text[end:], encoding="utf-8")
            gate_dir = session_dir / "gates" / "TSK-9998"
            gate_dir.mkdir(parents=True)
            for name, payload in {
                "tpm_completion": {"command": "team-completion-check", "status": "pass"},
                "evaluation": {"command": "evaluator-precheck", "status": "pass"},
            }.items():
                (gate_dir / f"{name}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

            output = self.run_builder(
                state_root,
                "vault-final-update",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9998",
                    "task_detail_path": str(task_detail_path),
                    "auto_finalization_check": True,
                    "auto_final_transport_render_check": True,
                },
            )

            self.assertNotIn("decision", output)
            rollup = output["vaultFinalUpdate"]
            self.assertEqual(rollup["status"], "complete")
            self.assertEqual(rollup["result"], "updated_finalization_passed")
            self.assertTrue(rollup["next_phase_allowed"])
            self.assertEqual(rollup["llm_dispatch_policy"], "run_builder_finalization_check")
            self.assertEqual(rollup["missing_evidence"], [])
            self.assertEqual(rollup["blockers"], [])
            self.assertEqual(rollup["finalization_check_status"], "pass")
            self.assertEqual(rollup["finalization_check"]["gateCommand"]["status"], "pass")
            vault_update_artifact = json.loads((gate_dir / "vault_final_update.json").read_text(encoding="utf-8"))
            self.assertTrue(vault_update_artifact["next_phase_allowed"])
            self.assertEqual(vault_update_artifact["llm_dispatch_policy"], "run_builder_finalization_check")
            self.assertEqual(vault_update_artifact["missing_evidence"], [])
            self.assertEqual(vault_update_artifact["blockers"], [])
            self.assertEqual(
                rollup["finalization_check"]["gateCommand"]["final_transport_render_check"]["status"],
                "complete",
            )
            final_transport = rollup["finalization_check"]["gateCommand"]["final_transport_render_check"]
            self.assertTrue(final_transport["next_phase_allowed"])
            self.assertEqual(final_transport["llm_dispatch_policy"], "render_main_transport_response")
            task_text = task_detail_path.read_text(encoding="utf-8")
            self.assertIn("## Vault Final Update", task_text)
            self.assertIn("## Final Transport Render Check", task_text)
            self.assertIn(rollup["artifact_sha256"], task_text)
            self.assertEqual(
                load_builder_module().file_sha256_if_exists(Path(rollup["artifact_path"])),
                rollup["artifact_sha256"],
            )
            self.assertTrue((gate_dir / "vault_final_update.json").exists())
            self.assertTrue((gate_dir / "finalization.json").exists())

    def test_vault_final_update_auto_finalization_blocks_when_pre_final_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            session_dir = self.bootstrap(state_root)
            task_dir = state_root / "TSK-9996"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            task_detail_path.write_text(
                "# TSK-9996 Incomplete\n\n"
                "## Metadata\n\n"
                "| Field | Value |\n|---|---|\n| Task ID | TSK-9996 |\n",
                encoding="utf-8",
            )
            gate_dir = session_dir / "gates" / "TSK-9996"
            gate_dir.mkdir(parents=True)
            for name, payload in {
                "tpm_completion": {"command": "team-completion-check", "status": "pass"},
                "evaluation": {"command": "evaluator-precheck", "status": "pass"},
            }.items():
                (gate_dir / f"{name}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

            output = self.run_builder(
                state_root,
                "vault-final-update",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9996",
                    "task_detail_path": str(task_detail_path),
                    "auto_finalization_check": True,
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("Project Manager Handoff missing", output["reason"])
            rollup = output["vaultFinalUpdate"]
            self.assertEqual(rollup["status"], "complete")
            self.assertEqual(rollup["result"], "updated_finalization_blocked")
            self.assertEqual(rollup["finalization_check_status"], "block")
            self.assertEqual(rollup["finalization_check"]["gateCommand"]["status"], "block")
            self.assertIn("## Vault Final Update", task_detail_path.read_text(encoding="utf-8"))
            finalization_artifact = json.loads((gate_dir / "finalization.json").read_text(encoding="utf-8"))
            self.assertEqual(finalization_artifact["status"], "block")

    def test_vault_final_update_blocks_missing_compact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            self.bootstrap(state_root)
            task_dir = state_root / "TSK-9996"
            task_dir.mkdir()
            task_detail_path = task_dir / "task.md"
            task_detail_path.write_text("# TSK-9996 Missing\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "vault-final-update",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9996",
                    "task_detail_path": str(task_detail_path),
                    "artifact_paths": [str(state_root / "missing.json")],
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["vaultFinalUpdate"]["status"], "blocked")
            self.assertIn("gate artifact does not exist", output["reason"])
            self.assertNotIn("## Vault Final Update", task_detail_path.read_text(encoding="utf-8"))

    def test_task_detail_line_cap_warns_before_execution_and_blocks_pre_final(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            vault_root = Path(tmp) / "vault"
            self.init_test_vault(vault_root)
            session_dir = self.bootstrap(state_root)
            scaffold = self.run_builder(
                state_root,
                "gtc-scaffold",
                {
                    "session_id": "test-session",
                    "vault_root": str(vault_root),
                    "project": "AI-Agent-Organization",
                    "task_id": "TSK-9996",
                    "gate_intake_envelope": self.sample_gate_intake_envelope("controlled_micro_flow"),
                    "source_report_path": str(session_dir / "queue" / "reports" / "gate-prompt-formatter" / "ENTRY" / "rep.yaml"),
                },
            )
            task_detail_path = Path(scaffold["gtcScaffold"]["task_detail_path"])

            with mock.patch.dict(os.environ, {"ITB_TASK_DETAIL_LINE_CAP": "80"}, clear=False):
                pre_errors, pre_warnings = builder.validate_task_flow_artifact(task_detail_path, "pre_execution")
                final_errors, _ = builder.validate_task_flow_artifact(task_detail_path, "pre_final_response")

            self.assertEqual(pre_errors, [])
            self.assertTrue(any("line count" in item for item in pre_warnings))
            self.assertTrue(any("line count" in item for item in final_errors))

    def test_prompt_preflight_can_skip_auto_gate_task_creator(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            response = """envelope_version: "2"
source_type: human_prompt
original_request: |
  hello
intent_summary: "route through Gate"
desired_outcome:
  deliverables: ["Gate Intake Envelope"]
  done_criteria: ["ready for GTC"]
scope:
  in: ["normalize prompt"]
  out: ["task work"]
approval_required: false
workflow_mode: strict_flow
task_units:
  - unit_id: unit-1
    title: "normalize"
routing_hint: "teams-project-manager"
review_requirements: ["independent_review"]
vault_update_targets: ["Agents-Vault"]
handoff_notes:
  gate-task-creator: "create task"
"""
            with mock.patch.dict(
                os.environ,
                {"ITB_GATE_ENTRY_DISPATCH": "1"},
                clear=False,
            ), mock.patch.object(
                builder,
                "agent_dispatch",
                return_value={
                    "agentDispatch": {
                        "request_id": "req-skip-auto-gtc",
                        "result": "provider_response_ready",
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "claude_tmux_interactive",
                        "target": "itb-org-test:gate-prompt-formatter.0",
                        "response": response,
                    },
                },
            ):
                output = builder.preflight_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "test-session",
                        "prompt": "hello",
                        "entry_id": "entry-skip-auto-gtc",
                        "message_id": "msg-skip-auto-gtc",
                        "report_id": "rep-skip-auto-gtc",
                        "skip_gate_entry_auto_gtc": True,
                    },
                )

            self.assertNotIn("decision", output)
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertNotIn("gate_task_creator_queue", event["gate_entry_dispatch"])
            self.assertFalse((session_dir / "queue" / "inbox" / "gate-task-creator.yaml").exists())

    def test_active_task_registration_persists_and_preflight_uses_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, pm_handoff=False)

            active_output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )

            self.assertEqual(active_output["activeTask"]["result"], "active_task_set")
            self.assertTrue((session_dir / "active-task.json").exists())

            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )
            self.assertNotIn("decision", preflight_output)
            self.assertIn("Project Manager Handoff missing", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["task_context_source"], "active-task.json")
            self.assertEqual(event["task_detail_path"], str(task_detail))

    def test_active_task_registration_blocks_unsupported_flow_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)

            active_output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "final",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )

            self.assertEqual(active_output["decision"], "block")
            self.assertIn("unsupported flow_phase: final", active_output["reason"])
            self.assertFalse((session_dir / "active-task.json").exists())

    def test_pre_execution_blocks_codex_transport_for_claude_gate_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | codex-session | req-gpf | sess-gpf | current Codex turn | complete |
"""
                )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("gate-prompt-formatter: provider mismatch", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_pre_execution_blocks_self_claimed_claude_gate_evidence_without_provider_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_tmux_interactive | provider_response_ready |
"""
                )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("Gate role evidence has no matching provider transcript", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_pre_execution_blocks_natural_language_gate_success_without_provider_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_tmux_interactive | Gate Intake Envelope created. |
"""
                )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("Gate role evidence has no matching provider transcript", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_pre_execution_blocks_gate_timeout_without_successful_provider_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-timeout | sess-gpf | claude_tmux_interactive | provider_response_timeout |
"""
                )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("Gate role latest evidence is failed or incomplete", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_pre_execution_allows_historical_gate_timeout_with_successful_provider_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-timeout | sess-gpf | claude_tmux_interactive | provider_response_timeout |
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_tmux_interactive | provider_response_ready |
"""
                )
            self.write_provider_activation_evidence(
                session_dir,
                event_type="agent_dispatch",
                usage_source="claude_tmux_interactive",
            )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotEqual(preflight_output.get("decision"), "block")
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_ready")

    def test_pre_execution_blocks_gate_success_with_provider_request_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | forged-req-gpf | sess-gpf | claude_tmux_interactive | provider_response_ready |
"""
                )
            self.write_provider_activation_evidence(
                session_dir,
                event_type="agent_dispatch",
                usage_source="claude_tmux_interactive",
            )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("Gate role evidence has no matching provider transcript", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_pre_execution_allows_controlled_micro_flow_local_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            self.add_controlled_micro_flow_section(task_detail)
            self.replace_invocation_evidence_with_local_micro_flow(task_detail)

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotEqual(preflight_output.get("decision"), "block")
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_ready")

    def test_pre_execution_blocks_gate_timeout_after_successful_provider_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_tmux_interactive | provider_response_ready |
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-later-timeout | sess-gpf | claude_tmux_interactive | provider_response_timeout |
"""
                )
            self.write_provider_activation_evidence(
                session_dir,
                event_type="agent_dispatch",
                usage_source="claude_tmux_interactive",
            )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("Gate role latest evidence is failed or incomplete", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_pre_execution_blocks_latest_provider_activation_failed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)
            with task_detail.open("a", encoding="utf-8") as fh:
                fh.write(
                    """
## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_tmux_interactive | provider_response_ready |
| gate-prompt-formatter | registry | claude-sonnet-4-6 | unavailable | sess-gpf | claude_tmux_interactive | provider_activation_failed |
"""
                )
            self.write_provider_activation_evidence(
                session_dir,
                event_type="agent_dispatch",
                usage_source="claude_tmux_interactive",
            )

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                },
            )
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "execute active task"},
            )

            self.assertNotIn("decision", preflight_output)
            self.assertIn("Gate role latest evidence is failed or incomplete", preflight_output["reason"])
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_blocked")

    def test_active_task_clear_removes_preflight_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, pm_handoff=False)

            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                },
            )
            clear_output = self.run_builder(
                state_root,
                "active-task",
                {"session_id": "test-session", "action": "clear"},
            )

            self.assertEqual(clear_output["activeTask"]["result"], "active_task_cleared")
            self.assertFalse((session_dir / "active-task.json").exists())
            preflight_output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "no active task"},
            )
            self.assertNotEqual(preflight_output.get("decision"), "block")

    def test_assessor_precheck_allows_post_routing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, completion_envelope=True)

            output = self.run_builder(
                state_root,
                "assessor-precheck",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                },
            )

            self.assertNotIn("decision", output)
            self.assertEqual(output["gatePrecheck"]["gate_role"], "team-completion-check")
            self.assertEqual(output["gatePrecheck"]["flow_phase"], "post_routing")
            self.assertEqual(output["gatePrecheck"]["precheck_status"], "pass")
            self.assertEqual(output["gateCommand"]["command"], "team-completion-check")
            self.assertEqual(output["gateCommand"]["status"], "pass")
            self.assertTrue(output["gateCommand"]["next_phase_allowed"])
            self.assertEqual(output["gateCommand"]["next_action"], "queue_gate_task_evaluator")
            self.assertEqual(output["gateCommand"]["llm_dispatch_policy"], "skip_assessor_runtime_queue_evaluator")
            artifact_path = Path(output["gateCommand"]["artifact_path"])
            self.assertTrue(artifact_path.exists())
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["command"], "team-completion-check")
            self.assertEqual(artifact["status"], "pass")
            event = json.loads(
                (session_dir / "gate-precheck-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["llm_scope"], "verdict_reason_only")
            self.assertEqual(event["gate_command_status"], "pass")
            self.assertEqual(event["next_action"], "queue_gate_task_evaluator")
            self.assertEqual(event["llm_dispatch_policy"], "skip_assessor_runtime_queue_evaluator")

    def test_assessor_precheck_blocks_missing_routing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, routing=False)

            output = self.run_builder(
                state_root,
                "assessor-precheck",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["gatePrecheck"]["precheck_status"], "block")
            self.assertEqual(output["gateCommand"]["status"], "block")
            self.assertFalse(output["gateCommand"]["next_phase_allowed"])
            self.assertEqual(
                output["gateCommand"]["next_action"],
                "return_to_teams_project_manager_with_missing_evidence",
            )
            self.assertEqual(output["gateCommand"]["llm_dispatch_policy"], "skip_assessor_and_evaluator_dispatch")
            self.assertIn("Team Routing Decision missing", output["reason"])
            self.assertIn("Team Completion Check missing for team-completion-check", output["reason"])

    def test_gate_command_ambiguity_benchmark_fixture_cases(self) -> None:
        fixture_path = SKILL_ROOT / "tests" / "fixtures" / "gate_command_ambiguity_cases.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        ambiguous_cases: list[str] = []

        for case in fixture["cases"]:
            with self.subTest(case_id=case["case_id"]):
                with tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = self.bootstrap(state_root)
                    task_detail = state_root / f"{case['case_id']}.md"
                    task_config = case.get("task_detail", {})
                    self.write_task_detail(
                        task_detail,
                        routing=bool(task_config.get("routing", True)),
                        guardian_complete=bool(task_config.get("guardian_complete", False)),
                        completion_envelope=bool(task_config.get("completion_envelope", False)),
                    )
                    if case.get("provider_evidence"):
                        self.write_provider_activation_evidence(session_dir)

                    output = self.run_builder(
                        state_root,
                        case["command"],
                        {
                            "session_id": "test-session",
                            "task_detail_path": str(task_detail),
                        },
                    )

                    command = output["gateCommand"]
                    expected = case["expected"]
                    if command["status"] == "ambiguous":
                        ambiguous_cases.append(case["case_id"])
                    self.assertEqual(command["status"], expected["status"])
                    self.assertNotEqual(command["status"], "ambiguous")
                    self.assertEqual(command["next_action"], expected["next_action"])
                    self.assertEqual(command["llm_dispatch_policy"], expected["llm_dispatch_policy"])
                    self.assertEqual(command["next_phase_allowed"], expected["status"] == "pass")
                    if expected["status"] == "block":
                        self.assertEqual(output["decision"], "block")
                    else:
                        self.assertNotIn("decision", output)

        self.assertEqual(ambiguous_cases, [])

    def test_guardian_precheck_allows_verified_pre_final_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)

            output = self.run_builder(
                state_root,
                "guardian-precheck",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                },
            )

            self.assertNotIn("decision", output)
            self.assertEqual(output["gatePrecheck"]["gate_role"], "finalization-check")
            self.assertEqual(output["gatePrecheck"]["flow_phase"], "pre_final_response")
            self.assertEqual(output["gatePrecheck"]["precheck_status"], "pass")
            self.assertEqual(output["gateCommand"]["command"], "finalization-check")
            self.assertEqual(output["gateCommand"]["status"], "pass")
            self.assertTrue(output["gateCommand"]["next_phase_allowed"])
            self.assertEqual(output["gateCommand"]["next_action"], "handoff_to_main_transport_renderer")
            self.assertEqual(output["gateCommand"]["llm_dispatch_policy"], "skip_guardian_runtime_render_final")
            artifact_path = Path(output["gateCommand"]["artifact_path"])
            self.assertTrue(artifact_path.exists())
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["command"], "finalization-check")
            self.assertEqual(artifact["status"], "pass")
            self.assertEqual(artifact["notification_class"], "done")
            self.assertEqual(artifact["active_task"]["flow_phase"], "pre_final_response")
            active_task = json.loads((session_dir / "active-task.json").read_text(encoding="utf-8"))
            self.assertEqual(active_task["flow_phase"], "pre_final_response")
            event = json.loads(
                (session_dir / "gate-precheck-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["notification_class"], "done")
            self.assertEqual(event["active_task_result"], "active_task_set")
            self.assertEqual(event["next_action"], "handoff_to_main_transport_renderer")
            self.assertEqual(event["llm_dispatch_policy"], "skip_guardian_runtime_render_final")

    def test_finalization_check_auto_generates_final_transport_render_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            start = text.index("## Final Transport Render Check")
            end = text.index("## Invocation Evidence", start)
            task_detail.write_text(text[:start].rstrip() + "\n\n" + text[end:], encoding="utf-8")

            blocked = self.run_builder(
                state_root,
                "finalization-check",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                },
            )

            self.assertEqual(blocked["decision"], "block")
            self.assertIn("Final Transport Render Check missing for pre_final_response", blocked["reason"])

            output = self.run_builder(
                state_root,
                "finalization-check",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "auto_final_transport_render_check": True,
                    "style_profile": "main_transport_renderer_default",
                },
            )

            self.assertNotIn("decision", output)
            self.assertEqual(output["gateCommand"]["status"], "pass")
            final_transport = output["gateCommand"]["final_transport_render_check"]
            self.assertEqual(final_transport["status"], "complete")
            self.assertEqual(final_transport["result"], "updated")
            artifact_path = Path(final_transport["artifact_path"])
            self.assertTrue(artifact_path.exists())
            task_text = task_detail.read_text(encoding="utf-8")
            self.assertIn("## Final Transport Render Check", task_text)
            self.assertIn("| Renderer | main_transport_renderer |", task_text)
            self.assertIn("| Source Envelope | Completion Envelope |", task_text)
            self.assertIn("| Source Finalization Check | Finalization Check |", task_text)
            self.assertIn("| Facts Preserved | true |", task_text)
            finalization_artifact = json.loads(
                (session_dir / "gates" / "TSK-9999" / "finalization.json").read_text(encoding="utf-8")
            )
            self.assertIn("final_transport_render_check", finalization_artifact)
            second = self.run_builder(
                state_root,
                "final-transport-render-check",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "finalization_artifact_path": str(session_dir / "gates" / "TSK-9999" / "finalization.json"),
                },
            )
            self.assertEqual(second["finalTransportRenderCheck"]["result"], "updated")
            self.assertEqual(task_detail.read_text(encoding="utf-8").count("## Final Transport Render Check"), 1)

    def test_finalization_auto_transport_block_updates_gate_event_metadata(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            start = text.index("## Final Transport Render Check")
            end = text.index("## Invocation Evidence", start)
            text = text[:start].rstrip() + "\n\n" + text[end:]
            while len(text.splitlines()) < builder.TASK_DETAIL_LINE_CAP_DEFAULT - 2:
                text += "\nline-cap-filler"
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "finalization-check",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "auto_final_transport_render_check": True,
                    "style_profile": "main_transport_renderer_default",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("Task Detail line count", output["reason"])
            self.assertEqual(output["gateCommand"]["status"], "block")
            self.assertFalse(output["gateCommand"]["next_phase_allowed"])
            self.assertEqual(
                output["reason"],
                "; ".join(output["gateCommand"]["final_transport_render_check"]["validation_errors"]),
            )
            self.assertEqual(output["gateCommand"]["reason"], output["reason"])
            self.assertEqual(output["gateCommand"]["final_transport_render_check"]["status"], "blocked")
            event = json.loads(
                (session_dir / "gate-precheck-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["precheck_status"], "block")
            self.assertEqual(event["result"], "gate_precheck_blocked")
            self.assertEqual(event["machine_verdict"], "blocked_by_builder_precheck")
            self.assertEqual(event["llm_scope"], "blocked_until_artifacts_fixed")
            self.assertEqual(event["notification_class"], "flow_alert")
            self.assertEqual(event["gate_command_status"], "block")
            self.assertEqual(event["active_task_result"], "active_task_set")
            canonical_finalization_path = session_dir / "gates" / "TSK-9999" / "finalization.json"
            self.assertEqual(Path(output["gateCommand"]["artifact_path"]), canonical_finalization_path)
            persisted = json.loads(canonical_finalization_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(persisted["reason"], output["reason"])
            self.assertFalse(persisted["next_phase_allowed"])
            self.assertEqual(persisted["final_transport_render_check"]["status"], "blocked")
            self.assertEqual(persisted["final_transport_render_check"], output["gateCommand"]["final_transport_render_check"])

    def test_final_response_guard_skips_when_no_pre_final_task_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "final-response-guard",
                {"session_id": "test-session"},
            )

            self.assertEqual(output["permissionDecision"], "allow")
            self.assertNotIn("decision", output)
            self.assertEqual(output["finalResponseGuard"]["result"], "skipped_no_active_pre_final_task")
            event = json.loads(
                (session_dir / "final-response-guard-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["notification_class"], "silent")

    def test_final_response_guard_blocks_incomplete_pre_final_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=False, completion_envelope=False)

            output = self.run_builder(
                state_root,
                "final-response-guard",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertEqual(output["finalResponseGuard"]["result"], "blocked_finalization_gate")
            self.assertEqual(output["gateCommand"]["status"], "block")
            self.assertIn("Completion Envelope missing for pre_final_response", output["reason"])

    def test_final_response_guard_runs_finalization_and_transport_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            start = text.index("## Final Transport Render Check")
            end = text.index("## Invocation Evidence", start)
            task_detail.write_text(text[:start].rstrip() + "\n\n" + text[end:], encoding="utf-8")

            output = self.run_builder(
                state_root,
                "final-response-guard",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                    "style_profile": "main_transport_renderer_default",
                },
            )

            self.assertEqual(output["permissionDecision"], "allow")
            self.assertNotIn("decision", output)
            self.assertEqual(output["finalResponseGuard"]["result"], "final_response_allowed")
            self.assertEqual(output["gateCommand"]["status"], "pass")
            final_transport = output["gateCommand"]["final_transport_render_check"]
            self.assertEqual(final_transport["status"], "complete")
            self.assertEqual(final_transport["result"], "updated")
            self.assertIn("## Final Transport Render Check", task_detail.read_text(encoding="utf-8"))

    def test_guardian_precheck_blocks_provider_session_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace("sess-gte", "forged-gte-session")
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "guardian-precheck",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["gatePrecheck"]["precheck_status"], "block")
            self.assertEqual(output["gateCommand"]["status"], "block")
            self.assertFalse(output["gateCommand"]["next_phase_allowed"])
            self.assertEqual(
                output["gateCommand"]["next_action"],
                "return_to_gate_task_evaluator_or_vault_final_update_with_missing_evidence",
            )
            self.assertEqual(output["gateCommand"]["llm_dispatch_policy"], "skip_guardian_and_final_renderer")
            self.assertIn(
                "Invocation Evidence has no matching provider transcript: gate-task-evaluator",
                output["reason"],
            )

    def test_evaluator_precheck_returns_no_diff_shortcut_for_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            repo_root = state_root / "repo"
            self.init_git_repo(repo_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, completion_envelope=True)

            output = self.run_builder(
                state_root,
                "evaluator-precheck",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "repo_root": str(repo_root),
                },
            )

            self.assertNotIn("decision", output)
            precheck = output["gatePrecheck"]
            self.assertEqual(precheck["gate_role"], "gate-task-evaluator")
            self.assertEqual(precheck["git_diff_status"], "no_diff")
            self.assertEqual(precheck["shortcut"], "no_diff_publication_not_required")
            self.assertFalse(precheck["suggested_task_change_manifest"]["commit_required"])
            self.assertFalse(precheck["suggested_git_publication_manifest"]["publication_required"])
            self.assertEqual(precheck["llm_scope"], "quality_verdict_only")
            self.assertEqual(output["gateCommand"]["command"], "evaluator-precheck")
            self.assertEqual(output["gateCommand"]["status"], "pass")
            self.assertEqual(output["gateCommand"]["git_diff_status"], "no_diff")
            self.assertEqual(output["gateCommand"]["next_action"], "dispatch_thin_gate_task_evaluator_verdict")
            self.assertEqual(output["gateCommand"]["llm_dispatch_policy"], "allow_thin_evaluator_verdict")
            artifact_path = Path(output["gateCommand"]["artifact_path"])
            self.assertTrue(artifact_path.exists())
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["command"], "evaluator-precheck")
            self.assertEqual(artifact["task_id"], "TSK-9999")

    def test_evaluator_precheck_keeps_dirty_repo_for_diff_scope_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            repo_root = state_root / "repo"
            self.init_git_repo(repo_root)
            (repo_root / "changed.txt").write_text("dirty\n", encoding="utf-8")
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, completion_envelope=True)

            output = self.run_builder(
                state_root,
                "evaluator-precheck",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "repo_root": str(repo_root),
                },
            )

            self.assertNotIn("decision", output)
            precheck = output["gatePrecheck"]
            self.assertEqual(precheck["git_diff_status"], "dirty")
            self.assertEqual(precheck["shortcut"], "")
            self.assertEqual(precheck["llm_scope"], "diff_scope_and_manifest_required")
            self.assertIn("?? changed.txt", precheck["git_status_lines"])

    def test_evaluator_precheck_suggests_manifest_for_task_owned_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            repo_root = state_root / "repo"
            self.init_git_repo(repo_root)
            (repo_root / "src").mkdir()
            (repo_root / "src" / "app.py").write_text("print('owned')\n", encoding="utf-8")
            (repo_root / "notes.txt").write_text("unrelated\n", encoding="utf-8")
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, completion_envelope=True)

            output = self.run_builder(
                state_root,
                "evaluator-precheck",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "repo_root": str(repo_root),
                    "owned_paths": "src",
                },
            )

            self.assertNotIn("decision", output)
            precheck = output["gatePrecheck"]
            self.assertEqual(precheck["git_diff_status"], "dirty")
            self.assertEqual(precheck["shortcut"], "task_owned_dirty_manifest_suggested")
            self.assertEqual(precheck["llm_scope"], "quality_verdict_and_manifest_review")
            task_manifest = precheck["suggested_task_change_manifest"]
            publication_manifest = precheck["suggested_git_publication_manifest"]
            self.assertTrue(task_manifest["commit_required"])
            self.assertEqual(task_manifest["owned_paths"], ["src/app.py"])
            self.assertEqual(task_manifest["excluded_paths"], ["notes.txt"])
            self.assertEqual(task_manifest["unrelated_dirty_paths"], ["notes.txt"])
            self.assertEqual(task_manifest["approved_scope"], "task_owned_git_diff")
            self.assertEqual(task_manifest["approved_diff_snapshot"]["git_status"], ["?? src/app.py"])
            self.assertEqual(task_manifest["approved_diff_snapshot"]["untracked_files"][0]["path"], "src/app.py")
            self.assertTrue(publication_manifest["publication_required"])
            self.assertEqual(publication_manifest["handoff_to"], "git-publisher")

    def test_evaluator_precheck_blocks_without_ready_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            repo_root = state_root / "repo"
            self.init_git_repo(repo_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail)

            output = self.run_builder(
                state_root,
                "evaluator-precheck",
                {
                    "session_id": "test-session",
                    "task_detail_path": str(task_detail),
                    "repo_root": str(repo_root),
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["gatePrecheck"]["precheck_status"], "block")
            self.assertEqual(output["gateCommand"]["next_action"], "return_to_prior_role_with_validation_errors")
            self.assertEqual(output["gateCommand"]["llm_dispatch_policy"], "skip_evaluator_dispatch")
            self.assertIn("Team Completion Check is missing or not ready_for_evaluation", output["reason"])

    def test_prompt_preflight_clears_completed_active_task_with_legacy_provider_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "done-task.md"
            task_detail.write_text(
                """---
status: done
---

# Done Task

## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | codex session | unavailable | test-session | current chat | Gate Intake Envelope created. |
""",
                encoding="utf-8",
            )
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-DONE",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                },
            )

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "new prompt after done task"},
            )

            self.assertNotEqual(output.get("decision"), "block")
            self.assertFalse((session_dir / "active-task.json").exists())
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_recovered")
            self.assertEqual(event["task_context_source"], "")
            self.assertEqual(event["active_task_recovery"]["reason"], "task_detail_already_complete")

    def test_active_task_registration_blocks_invalid_pre_final_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=False, completion_envelope=False)

            output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                    "source": "guardian_complete",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("Finalization Status is not complete", output["reason"])
            self.assertFalse((session_dir / "active-task.json").exists())
            event = json.loads(
                (session_dir / "active-task-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "active_task_blocked")

    def test_pre_final_response_allows_controlled_micro_flow_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.add_controlled_micro_flow_section(task_detail)
            self.replace_invocation_evidence_with_local_micro_flow(task_detail)

            output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                    "source": "guardian_complete",
                },
            )

            self.assertNotIn("decision", output)
            self.assertEqual(output["activeTask"]["result"], "active_task_set")

    def test_pre_final_response_blocks_local_micro_flow_evidence_without_control_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.replace_invocation_evidence_with_local_micro_flow(task_detail)

            output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                    "source": "guardian_complete",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("local controlled micro-flow evidence is not allowed", output["reason"])
            self.assertIn("Controlled Micro-Flow section missing", output["reason"])

    def test_active_task_registration_blocks_deferred_commit_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace(
                "| Evaluation Status | quality_ok |",
                "| Evaluation Status | quality_ok |\n"
                "| Commit Required | true |\n"
                "| Git Publication Manifest | present |",
            )
            text = text.replace(
                "| task_id | TSK-9999 |",
                "| task_id | TSK-9999 |\n"
                "| commit_required | true |\n"
                "| committed_diff_matches_snapshot | false |",
            )
            text = text.replace(
                "## Finalization Check",
                "## Git Publication Result\n\n"
                "| Field | Value |\n"
                "|---|---|\n"
                "| Git Publication Status | deferred_not_requested |\n"
                "| Commit Status | deferred_not_requested |\n"
                "| Commit Hashes |  |\n"
                "| Committed Diff Matches Snapshot | false |\n\n"
                "## Finalization Check",
            )
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                    "source": "guardian_complete",
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("invalid terminal status: deferred_not_requested", output["reason"])
            self.assertIn("commit_status is not complete", output["reason"])
            self.assertFalse((session_dir / "active-task.json").exists())

    def test_git_publication_gate_requires_push_result_when_push_required(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | true |
| pr_required | false |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Push Status | missing |
| Remote Branch |  |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("push_required true but Git Publication Result push_status is not complete", errors)
        self.assertIn("push_required true but Git Publication Result remote_branch is missing", errors)

    def test_git_publication_gate_rejects_task_branch_for_merge_to_main(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | true |
| pr_required | false |
| publication_flow | merge_to_main_and_push |
| default_branch | main |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Push Status | complete |
| Remote Branch | origin/codex/task-work |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("merge_to_main_and_push publication remote_branch does not match default_branch", errors)

    def test_git_publication_gate_allows_default_branch_for_merge_to_main(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | true |
| pr_required | false |
| publication_flow | merge_to_main_and_push |
| default_branch | main |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| Push Status | complete |
| Remote Branch | origin/main |
"""

        self.assertEqual(builder.git_publication_gate_errors(text), [])

    def test_git_publication_gate_reads_thin_result_report_artifact(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "git-publication-result.json"
            report_path.write_text(
                json.dumps(
                    {
                        "git_publication_result": {
                            "git_publication_status": "complete",
                            "executor_role": "git-publisher",
                            "executor_session_id": "claude-session-git-publisher",
                            "usage_source": "claude_tmux_interactive",
                            "commit_status": "complete",
                            "commit_hashes": ["abc123"],
                            "committed_diff_matches_snapshot": True,
                            "push_status": "not_required",
                            "pr_status": "not_required",
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
            text = f"""
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | false |
| pr_required | false |
| publication_flow | commit_only |

## Git Publication Result

| Field | Value |
|---|---|
| Status | complete |
| Report Path | {report_path} |
| Report SHA256 | {report_sha} |
"""

            self.assertEqual(builder.git_publication_gate_errors(text), [])

    def test_git_publication_gate_blocks_missing_thin_result_report_artifact(self) -> None:
        builder = load_builder_module()
        missing_report_path = Path("/tmp/itb-missing-git-publication-result.json")
        text = f"""
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | false |
| pr_required | false |
| publication_flow | commit_only |

## Git Publication Result

| Field | Value |
|---|---|
| Status | complete |
| Report Path | {missing_report_path} |
| Report SHA256 | missing |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn(f"Git Publication Result report_path does not exist: {missing_report_path}", errors)
        self.assertIn("commit_required true but Git Publication Result commit_status is not complete", errors)

    def test_git_publication_gate_rejects_main_agent_executor(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | false |
| pr_required | false |
| publication_flow | commit_only |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | codex-main |
| Executor Session ID | local-main |
| Usage Source | main_agent_local |
| Commit Status | complete |
| Commit Hashes | abc123 |
| Committed Diff Matches Snapshot | true |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("Git Publication Result executor_role cannot be main agent", errors)
        self.assertIn("Git Publication Result usage_source is missing or not provider-backed", errors)

    def test_git_publication_gate_requires_finalization_after_guardian_artifacts(self) -> None:
        builder = load_builder_module()
        text = """
## Completion Gate

| Field | Value |
|---|---|
| Guardian Status Checked | true |
| Guardian Status | complete |
| Vault Final Update | complete |

## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | false |
| pr_required | false |
| publication_flow | commit_only |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| Commit Status | complete |
| Commit Hashes | abc123 |
| Committed Diff Matches Snapshot | true |

## Guardian Verdict

| Field | Value |
|---|---|
| Guardian Status | complete |
| Handoff To | main_transport_renderer |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn(
            "Git Publication Result finalization_status is missing or invalid after completion artifacts",
            errors,
        )

    def test_git_publication_gate_allows_finalization_commit_after_guardian_artifacts(self) -> None:
        builder = load_builder_module()
        text = """
## Completion Gate

| Field | Value |
|---|---|
| Guardian Status Checked | true |
| Guardian Status | complete |
| Vault Final Update | complete |

## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | false |
| pr_required | false |
| publication_flow | commit_only |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| Commit Status | complete |
| Commit Hashes | abc123 |
| Committed Diff Matches Snapshot | true |
| Finalization Status | complete |
| Finalization Commit Hashes | def456 |

## Guardian Verdict

| Field | Value |
|---|---|
| Guardian Status | complete |
| Handoff To | main_transport_renderer |
"""

        self.assertEqual(builder.git_publication_gate_errors(text), [])

    def test_git_publication_gate_requires_finalization_push_when_push_required(self) -> None:
        builder = load_builder_module()
        text = """
## Completion Gate

| Field | Value |
|---|---|
| Guardian Status Checked | true |
| Guardian Status | complete |
| Vault Final Update | complete |

## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | true |
| pr_required | false |
| publication_flow | commit_and_push |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| Commit Status | complete |
| Commit Hashes | abc123 |
| Committed Diff Matches Snapshot | true |
| Push Status | complete |
| Remote Branch | origin/codex/task-work |
| Finalization Status | complete |
| Finalization Commit Hashes | def456 |

## Guardian Verdict

| Field | Value |
|---|---|
| Guardian Status | complete |
| Handoff To | main_transport_renderer |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn(
            "push_required true but Git Publication Result finalization_push_status is not complete",
            errors,
        )
        self.assertIn(
            "push_required true but Git Publication Result finalization_remote_branch is missing",
            errors,
        )

    def test_git_publication_gate_rejects_unsupported_publication_flow(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | false |
| publication_flow | mystery_flow |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | not_required |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("Git Publication Manifest uses unsupported publication_flow: mystery_flow", errors)

    def test_git_publication_gate_rejects_branch_action_none_when_git_required(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | true |
| push_required | false |
| pr_required | false |
| publication_flow | commit_only |
| branch_action | none |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Commit Status | complete |
| Commit Hashes | abc123 |
| Committed Diff Matches Snapshot | true |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("branch_action none is invalid when git publication is required", errors)

    def test_git_publication_gate_requires_vault_direct_write_approval(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | false |
| publication_flow | vault_direct_write |
| branch_action | none |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | not_required |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("publication_flow vault_direct_write requires explicit vault_direct_write approval", errors)

    def test_git_publication_gate_allows_approved_vault_direct_write(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | false |
| publication_flow | vault_direct_write |
| branch_action | none |
| vault_direct_write_approved | true |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | not_required |
"""

        self.assertEqual(builder.git_publication_gate_errors(text), [])

    def test_git_publication_gate_requires_pr_url_when_pr_required(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | true |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| PR Status | deferred |
| PR URL |  |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("pr_required true but Git Publication Result pr_status is not created or complete", errors)
        self.assertIn("pr_required true but Git Publication Result pr_url is missing", errors)

    def test_git_publication_gate_rejects_invalid_pr_url(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | true |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| PR Status | created |
| PR URL | https://example.com/not-a-pr |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("pr_required true but Git Publication Result pr_url is not a GitHub pull request URL", errors)

    def test_git_publication_gate_requires_verified_pr_url_when_pr_required(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | true |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| PR Status | created |
| PR URL | https://github.com/example/repo/pull/12 |
"""

        errors = builder.git_publication_gate_errors(text)

        self.assertIn("pr_required true but Git Publication Result pr_verified is not true", errors)
        self.assertIn("pr_required true but Git Publication Result pr_verification_source is not gh_pr_view", errors)
        self.assertIn(
            "pr_required true but Git Publication Result pr_verified_url is not a GitHub pull request URL",
            errors,
        )

    def test_git_publication_gate_allows_gh_verified_pr_url(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | true |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | complete |
| Executor Role | git-publisher |
| Executor Session ID | claude-session-git-publisher |
| Usage Source | claude_tmux_interactive |
| PR Status | created |
| PR URL | https://github.com/example/repo/pull/12 |
| PR Verified | true |
| PR Verification Source | gh_pr_view |
| PR Verified URL | https://github.com/example/repo/pull/12 |
"""

        self.assertEqual(builder.git_publication_gate_errors(text), [])

    def test_git_publication_gate_allows_not_required_publication(self) -> None:
        builder = load_builder_module()
        text = """
## Git Publication Manifest

| Field | Value |
|---|---|
| commit_required | false |
| push_required | false |
| pr_required | false |
| publication_policy | not_required |

## Git Publication Result

| Field | Value |
|---|---|
| Git Publication Status | not_required |
| Commit Status | not_required |
| Push Status | not_required |
| PR Status | not_required |
| Reasons | no task-owned git diff |
"""

        self.assertEqual(builder.git_publication_gate_errors(text), [])

    def test_prompt_preflight_recovers_invalid_pre_final_active_task_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=False, completion_envelope=False)
            (session_dir / "active-task.json").write_text(
                json.dumps(
                    {
                        "status": "active",
                        "runtime": "codex",
                        "session_id": "test-session",
                        "task_id": "TSK-9999",
                        "task_detail_path": str(task_detail),
                        "flow_phase": "pre_final_response",
                        "owner_role": "finalization-check",
                        "last_gate": "finalization-check",
                        "source": "legacy_guardian_complete",
                    }
                ),
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "new prompt after failed final"},
            )

            self.assertNotEqual(output.get("decision"), "block")
            self.assertFalse((session_dir / "active-task.json").exists())
            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["result"], "preflight_recovered")
            self.assertEqual(event["active_task_recovery"]["reason"], "invalid_pre_final_response_fallback")
            self.assertIn("Finalization Status is not complete", event["active_task_recovery"]["validation_errors"])

    def test_prompt_preflight_blocks_completed_active_task_with_unpublished_required_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "done-task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            text = "---\nstatus: done\n---\n" + task_detail.read_text(encoding="utf-8")
            text = text.replace(
                "| Evaluation Status | quality_ok |",
                "| Evaluation Status | quality_ok |\n"
                "| Commit Required | true |\n"
                "| Git Publication Manifest | present |",
            )
            text = text.replace(
                "| task_id | TSK-9999 |",
                "| task_id | TSK-9999 |\n"
                "| commit_required | true |",
            )
            task_detail.write_text(text, encoding="utf-8")
            self.run_builder(
                state_root,
                "active-task",
                {
                    "session_id": "test-session",
                    "task_id": "TSK-9999",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                    "owner_role": "finalization-check",
                    "last_gate": "finalization-check",
                },
            )

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "new prompt after premature done"},
            )

            self.assertNotIn("decision", output)
            self.assertIn("completed active task has incomplete Git publication gate", output["reason"])
            self.assertIn("Git Publication Result missing while commit is required", output["reason"])
            self.assertTrue((session_dir / "active-task.json").exists())

    def test_prompt_preflight_blocks_missing_roster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            (session_dir / "roster.json").unlink()

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "hello"},
            )

            self.assertNotIn("decision", output)
            self.assertIn("roster.json missing", output["reason"])

    def test_prompt_preflight_blocks_task_without_gtc_to_tpm_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, pm_handoff=False)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "execute task",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_execution",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Project Manager Handoff missing", output["reason"])

    def test_prompt_preflight_blocks_tpm_routing_without_director_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, director="")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "route task",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "post_routing",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Team Routing Decision missing valid director handoff", output["reason"])

    def test_prompt_preflight_blocks_final_response_before_guardian_and_transport_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=False, completion_envelope=False)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Finalization Status is not complete", output["reason"])
            self.assertIn("Completion Envelope missing for pre_final_response", output["reason"])
            self.assertIn("Final Transport Render Check missing for pre_final_response", output["reason"])

    def test_prompt_preflight_allows_final_response_with_guardian_and_transport_render_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotEqual(output.get("decision"), "block")

    def test_prompt_preflight_allows_final_response_with_agent_dispatch_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(
                session_dir,
                event_type="agent_dispatch",
                usage_source="claude_tmux_interactive",
            )
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace("claude_print_json", "claude_tmux_interactive")
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotEqual(output.get("decision"), "block")

    def test_prompt_preflight_blocks_final_response_with_self_authored_invocation_table_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("no matching provider transcript", output["reason"])

    def test_prompt_preflight_blocks_main_transport_renderer_as_execution_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace(
                "| infra-director | complete | claude_tmux_interactive |",
                "| main_transport_renderer | complete | main_transport_render |",
            )
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn(
                "Role Execution Evidence cannot use main agent as executor: main_transport_renderer",
                output["reason"],
            )

    def test_prompt_preflight_blocks_invalid_final_transport_render_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace("| Facts Preserved | true |", "| Facts Preserved | false |")
            text = text.replace("| No New Task Judgment | true |", "| No New Task Judgment | false |")
            text = text.replace("| Worker Persona Leakage | false |", "| Worker Persona Leakage | true |")
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Final Transport Render Check facts_preserved is not true", output["reason"])
            self.assertIn("Final Transport Render Check no_new_task_judgment is not true", output["reason"])
            self.assertIn("Final Transport Render Check worker_persona_leakage is not false", output["reason"])

    def test_prompt_preflight_blocks_final_render_check_without_report_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace("| Source Envelope | Completion Envelope |\n", "")
            text = text.replace("| Source Finalization Check | Finalization Check |\n", "")
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Final Transport Render Check missing required field: Source Envelope", output["reason"])
            self.assertIn("Final Transport Render Check missing required field: Source Finalization Check", output["reason"])

    def test_prompt_preflight_blocks_self_certified_final_transport_render_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace(
                "| Safety Exception | false |",
                "| Safety Exception | false |\n| Self Certified | true |\n| Evidence Source | main-agent |",
            )
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Final Transport Render Check cannot be self-certified by main agent", output["reason"])

    def test_prompt_preflight_blocks_final_response_with_provider_session_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace("sess-gte", "forged-gte-session")
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn(
                "Invocation Evidence has no matching provider transcript: gate-task-evaluator",
                output["reason"],
            )

    def test_prompt_preflight_blocks_self_certified_final_without_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=False)

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Team Completion Check missing for pre_final_response", output["reason"])
            self.assertIn("Finalization Check missing for pre_final_response", output["reason"])

    def test_prompt_preflight_blocks_final_response_without_required_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace(
                "| gate-task-evaluator | registry | claude-sonnet-4-6 | req-gte | sess-gte | claude_print_json | complete |",
                "| gate-task-evaluator | registry |  |  |  |  | not_invoked |",
            )
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Invocation Evidence required agent not invoked: gate-task-evaluator", output["reason"])

    def test_prompt_preflight_blocks_final_response_with_main_agent_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            self.write_provider_activation_evidence(session_dir)
            text = task_detail.read_text(encoding="utf-8")
            text = text.replace(
                "| infra-director | complete | claude_tmux_interactive |",
                "| main-agent | complete | main_agent_local |",
            )
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("Role Execution Evidence cannot use main agent as executor", output["reason"])
            self.assertIn("Role Execution Evidence has no completed non-gate execution role", output["reason"])

    def test_response_active_requires_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            roster[0]["activation_status"] = "response_active"
            roster[0]["effective_model"] = ""
            roster[0]["usage_source"] = "bootstrap_metadata_only"
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "hello"},
            )

            self.assertNotIn("decision", output)
            self.assertIn("response_active without runtime evidence", output["reason"])

    def test_response_active_with_runtime_evidence_passes_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            roster[0]["activation_status"] = "response_active"
            roster[0]["effective_model"] = "claude-sonnet-4-6"
            roster[0]["usage_source"] = "claude_transcript_jsonl"
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "hello"},
            )

            self.assertNotEqual(output.get("decision"), "block")

    def test_response_active_blocks_claude_agent_with_codex_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            target = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            target["activation_status"] = "response_active"
            target["effective_model"] = "gpt-5.5"
            target["usage_source"] = "codex_session_log"
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {"session_id": "test-session", "prompt": "hello"},
            )

            self.assertNotIn("decision", output)
            self.assertIn("intended Claude/anthropic", output["reason"])

    def test_task_detail_invocation_evidence_blocks_claude_agent_with_codex_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root)
            task_detail = state_root / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            text = task_detail.read_text(encoding="utf-8").replace(
                "| gate-prompt-formatter | registry | claude-sonnet-4-6 | req-gpf | sess-gpf | claude_print_json | complete |",
                "| gate-prompt-formatter | registry | gpt-5.5 | req-1 | sess-1 | codex_session_log | complete |",
            )
            task_detail.write_text(text, encoding="utf-8")

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "final response",
                    "task_detail_path": str(task_detail),
                    "flow_phase": "pre_final_response",
                },
            )

            self.assertNotIn("decision", output)
            self.assertIn("task.md", output["reason"])
            self.assertIn("intended Claude/anthropic", output["reason"])

    def test_task_detail_invocation_evidence_warns_on_model_tier_mismatch(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            task_detail = Path(tmp) / "task.md"
            self.write_task_detail(task_detail, guardian_complete=True, completion_envelope=True)
            text = task_detail.read_text(encoding="utf-8").replace(
                "| gate-task-creator | registry | claude-haiku-4-5 | req-gtc | sess-gtc | claude_print_json | complete |",
                "| gate-task-creator | registry | claude-sonnet-4-6 | req-gtc | sess-gtc | claude_print_json | complete |",
            )
            task_detail.write_text(text, encoding="utf-8")

            errors, warnings = builder.validate_task_detail_provider_evidence(str(task_detail))

            self.assertEqual(errors, [])
            self.assertTrue(any("gate-task-creator: model tier mismatch" in warning for warning in warnings))

    def test_session_end_records_runtime_and_archive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root)

            output = self.run_builder(
                state_root,
                "session-end",
                {"session_id": "test-session", "reason": "unit-test"},
                runtime="claude",
            )
            evidence = (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            last_entry = json.loads(evidence[-1])

            self.assertTrue(output["suppressOutput"])
            self.assertEqual((session_dir / "status").read_text(encoding="utf-8"), "archived\n")
            self.assertTrue((session_dir / "shutdown.json").exists())
            self.assertEqual(last_entry["event_type"], "session_end")
            self.assertEqual(last_entry["runtime"], "claude")
            self.assertEqual(last_entry["result"], "session_archived_no_tmux_session_recorded")

    def test_session_start_and_end_cli_args_select_session_without_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)

            start = self.run_builder_without_input(
                state_root,
                "session-start",
                extra_args=["--session-id", "cli-session"],
            )
            self.assertEqual(start["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertTrue((state_root / "cli-session" / "bootstrap.json").exists())
            self.assertEqual((state_root / "last-session").read_text(encoding="utf-8").strip(), "cli-session")

            self.bootstrap(state_root, session_id="stale-last-session")
            end = self.run_builder_without_input(
                state_root,
                "session-end",
                extra_args=["--session-id", "cli-session"],
            )

            self.assertTrue(end["suppressOutput"])
            self.assertEqual((state_root / "cli-session" / "status").read_text(encoding="utf-8"), "archived\n")
            self.assertNotEqual((state_root / "stale-last-session" / "status").read_text(encoding="utf-8"), "archived\n")

    def test_session_end_dry_run_records_scoped_tmux_shutdown_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "shutdown-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            env["ITB_SESSION_END_DRY_RUN"] = "1"

            self.run_builder(
                state_root,
                "session-end",
                {"session_id": "shutdown-session", "reason": "unit-test"},
                runtime="codex",
                env=env,
            )
            session_dir = state_root / "shutdown-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            shutdown = json.loads((session_dir / "shutdown.json").read_text(encoding="utf-8"))
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(shutdown["tmux_shutdown_result"], "tmux_kill_dry_run")
            self.assertEqual(shutdown["tmux_session"], state["tmux_session"])
            self.assertEqual(shutdown["expected_tmux_session"], state["tmux_session"])
            self.assertFalse(shutdown["tmux_kill_attempted"])
            self.assertEqual(evidence[-1]["result"], "session_archived_tmux_kill_dry_run")

    def test_session_end_refuses_mismatched_tmux_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "unsafe-shutdown-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "unsafe-shutdown-session"
            state_path = session_dir / "bootstrap.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["tmux_session"] = "itb-org-not-this-session"
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            self.run_builder(
                state_root,
                "session-end",
                {"session_id": "unsafe-shutdown-session", "reason": "unit-test"},
                runtime="codex",
                env=env,
            )
            shutdown = json.loads((session_dir / "shutdown.json").read_text(encoding="utf-8"))
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(shutdown["tmux_shutdown_result"], "refused_unsafe_tmux_session")
            self.assertFalse(shutdown["tmux_kill_attempted"])
            self.assertIn("expected=", shutdown["tmux_shutdown_error"])
            self.assertEqual(evidence[-1]["result"], "session_archived_refused_unsafe_tmux_session")

    def test_session_end_stops_session_local_daemons_with_mocked_pids(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "daemon-session-end"
            session_dir = state_root / session_id
            session_dir.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "organization_instance_id": builder.organization_id(session_id),
                        "tmux_session": "",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (session_dir / "queue-watch-daemon.pid").write_text("111\n", encoding="utf-8")
            (session_dir / "interactive-readiness-followup.pid").write_text("222\n", encoding="utf-8")
            running = {111: True, 222: True}
            kill_calls: list[tuple[int, int]] = []

            def fake_running(pid: int) -> bool:
                return running.get(pid, False)

            def fake_kill(pid: int, sig: int) -> None:
                kill_calls.append((pid, sig))
                if sig == builder.signal.SIGTERM:
                    running[pid] = False

            def fake_command_line(pid: int) -> str:
                command = "queue-watch-daemon" if pid == 111 else "interactive-readiness-followup"
                return f"/usr/bin/python3 {builder.__file__} {command} --runtime codex"

            with mock.patch.object(builder, "process_is_running", side_effect=fake_running), mock.patch.object(
                builder, "process_command_line", side_effect=fake_command_line
            ), mock.patch.object(builder.os, "kill", side_effect=fake_kill):
                output = builder.session_end(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": session_id, "reason": "unit-test"},
                )

            shutdown = json.loads((session_dir / "shutdown.json").read_text(encoding="utf-8"))
            detached = shutdown["detached_process_shutdown"]

            self.assertTrue(output["suppressOutput"])
            self.assertEqual(detached["result"], "stopped")
            self.assertEqual(detached["stopped_count"], 2)
            self.assertEqual(detached["processes"]["queue_watch_daemon"]["result"], "terminated")
            self.assertEqual(detached["processes"]["interactive_readiness_followup"]["result"], "terminated")
            self.assertFalse((session_dir / "queue-watch-daemon.pid").exists())
            self.assertFalse((session_dir / "interactive-readiness-followup.pid").exists())
            self.assertEqual(
                kill_calls,
                [(111, builder.signal.SIGTERM), (222, builder.signal.SIGTERM)],
            )

    def test_archive_shutdown_dry_run_does_not_update_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "archive-dry-run-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "archive-dry-run-session"
            status_before = (session_dir / "status").read_text(encoding="utf-8")
            last_event_before = (session_dir / "last_event").read_text(encoding="utf-8")
            evidence_before = (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8")

            output = self.run_builder(
                state_root,
                "archive-shutdown",
                {"reason": "unit-test"},
                extra_args=["--session-id", "archive-dry-run-session", "--dry-run"],
                env=env,
            )

            self.assertEqual(output["archiveShutdown"]["tmux_shutdown_result"], "tmux_kill_dry_run")
            self.assertTrue(output["archiveShutdown"]["dry_run"])
            self.assertFalse(output["archiveShutdown"]["state_updated"])
            self.assertTrue(output["archiveShutdown"]["would_update_state"])
            self.assertEqual((session_dir / "status").read_text(encoding="utf-8"), status_before)
            self.assertEqual((session_dir / "last_event").read_text(encoding="utf-8"), last_event_before)
            self.assertFalse((session_dir / "shutdown.json").exists())
            self.assertFalse((session_dir / "archive-shutdown-input.json").exists())
            self.assertEqual(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8"),
                evidence_before,
            )

    def test_archive_shutdown_blocks_missing_state_without_creating_session_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)

            output = self.run_builder(
                state_root,
                "archive-shutdown",
                {"reason": "unit-test"},
                extra_args=["--session-id", "missing-session"],
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("bootstrap state not found", output["reason"])
            self.assertFalse((state_root / "missing-session").exists())

    def test_archive_shutdown_refuses_mismatched_tmux_session_and_records_archive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "archive-unsafe-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "archive-unsafe-session"
            state_path = session_dir / "bootstrap.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["tmux_session"] = "itb-org-not-this-session"
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            self.run_builder(
                state_root,
                "archive-shutdown",
                {"reason": "unit-test"},
                extra_args=["--session-id", "archive-unsafe-session"],
                env=env,
            )
            shutdown = json.loads((session_dir / "shutdown.json").read_text(encoding="utf-8"))
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual((session_dir / "status").read_text(encoding="utf-8"), "archived\n")
            self.assertTrue(
                (session_dir / "last_event").read_text(encoding="utf-8").startswith("ArchiveShutdown ")
            )
            self.assertTrue((session_dir / "archive-shutdown-input.json").exists())
            self.assertEqual(shutdown["tmux_shutdown_result"], "refused_unsafe_tmux_session")
            self.assertFalse(shutdown["tmux_kill_attempted"])
            self.assertEqual(evidence[-1]["event_type"], "archive_shutdown")
            self.assertEqual(evidence[-1]["result"], "archive_shutdown_refused_unsafe_tmux_session")

    def test_archive_shutdown_records_successful_tmux_kill_with_mocked_tmux(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "archive-success-session"
            session_dir = state_root / session_id
            session_dir.mkdir(parents=True)
            org_id = builder.organization_id(session_id)
            tmux_session = builder.safe_tmux_name(f"itb-{org_id}", max_length=60)
            (session_dir / "bootstrap.json").write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "organization_instance_id": org_id,
                        "tmux_session": tmux_session,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
                builder, "tmux_session_exists", side_effect=[True, False]
            ), mock.patch.object(
                builder, "tmux_window_count", return_value=(37, "")
            ), mock.patch.object(
                builder, "run_tmux", return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")
            ):
                output = builder.archive_shutdown(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"reason": "unit-test"},
                    session_id=session_id,
                )

            shutdown = json.loads((session_dir / "shutdown.json").read_text(encoding="utf-8"))
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(output["archiveShutdown"]["tmux_shutdown_result"], "tmux_killed")
            self.assertTrue(output["archiveShutdown"]["state_updated"])
            self.assertEqual(shutdown["tmux_shutdown_result"], "tmux_killed")
            self.assertTrue(shutdown["tmux_kill_attempted"])
            self.assertEqual(shutdown["tmux_windows_before"], 37)
            self.assertEqual(shutdown["tmux_windows_after"], 0)
            self.assertEqual(evidence[-1]["event_type"], "archive_shutdown")
            self.assertEqual(evidence[-1]["result"], "archive_shutdown_tmux_killed")

    def test_archive_shutdown_refuses_unsafe_detached_process_pid(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "archive-unsafe-daemon-session"
            session_dir = state_root / session_id
            session_dir.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "organization_instance_id": builder.organization_id(session_id),
                        "tmux_session": "",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (session_dir / "queue-watch-daemon.pid").write_text("333\n", encoding="utf-8")

            with mock.patch.object(builder, "process_is_running", return_value=True), mock.patch.object(
                builder, "process_command_line", return_value="/usr/bin/python3 unrelated.py queue-watch-daemon"
            ), mock.patch.object(builder.os, "kill") as kill:
                output = builder.archive_shutdown(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"reason": "unit-test"},
                    session_id=session_id,
                )

            shutdown = json.loads((session_dir / "shutdown.json").read_text(encoding="utf-8"))
            queue_shutdown = shutdown["detached_process_shutdown"]["processes"]["queue_watch_daemon"]

            self.assertEqual(output["archiveShutdown"]["detached_process_shutdown"]["result"], "refused")
            self.assertEqual(queue_shutdown["result"], "refused_unsafe_process")
            self.assertTrue((session_dir / "queue-watch-daemon.pid").exists())
            kill.assert_not_called()

    def test_archive_shutdown_current_uses_last_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "archive-current-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )

            output = self.run_builder(
                state_root,
                "archive-shutdown",
                {"reason": "unit-test"},
                extra_args=["--current", "--dry-run"],
                env=env,
            )

            self.assertEqual(output["archiveShutdown"]["session_id"], "archive-current-session")
            self.assertEqual(output["archiveShutdown"]["tmux_shutdown_result"], "tmux_kill_dry_run")

    def test_stale_tmux_cleanup_targets_only_old_itb_sessions(self) -> None:
        builder = load_builder_module()
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            calls.append(args)
            if args[:2] == ["list-sessions", "-F"]:
                return subprocess.CompletedProcess(
                    ["tmux", *args],
                    0,
                    stdout=(
                        "itb-org-current\t100000\t37\n"
                        "itb-org-newer\t99980\t37\n"
                        "itb-org-old\t1000\t37\n"
                        "work\t1000\t4\n"
                    ),
                    stderr="",
                )
            if args[:2] == ["kill-session", "-t"]:
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")
            return subprocess.CompletedProcess(["tmux", *args], 1, stdout="", stderr="unexpected tmux call")

        with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/tmux"), mock.patch.object(
            builder, "run_tmux", side_effect=fake_run_tmux
        ), mock.patch.dict(
            os.environ,
            {
                "ITB_STALE_TMUX_KEEP_RECENT": "1",
                "ITB_STALE_TMUX_MIN_AGE_SECONDS": "3600",
            },
            clear=False,
        ):
            summary = builder.cleanup_stale_tmux_sessions(
                current_session_name="itb-org-current",
                now_epoch=100000,
            )

        self.assertEqual(summary["total_itb_sessions"], 3)
        self.assertEqual(summary["skipped_count"], 1)
        self.assertEqual(summary["killed_count"], 1)
        self.assertEqual(summary["killed_sessions"][0]["session_name"], "itb-org-old")
        self.assertIn(["kill-session", "-t", "itb-org-old"], calls)
        self.assertNotIn(["kill-session", "-t", "work"], calls)
        self.assertNotIn(["kill-session", "-t", "itb-org-current"], calls)

    def test_configure_tmux_capture_settings_sets_history_and_default_size(self) -> None:
        builder = load_builder_module()
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(builder, "run_tmux", side_effect=fake_run_tmux), mock.patch.dict(
            os.environ,
            {
                "ITB_TMUX_HISTORY_LIMIT": "90000",
                "ITB_TMUX_DEFAULT_COLS": "180",
                "ITB_TMUX_DEFAULT_ROWS": "56",
            },
            clear=False,
        ):
            summary = builder.configure_tmux_capture_settings("itb-org-test")

        self.assertEqual(summary["tmux_config_status"], "configured")
        self.assertEqual(summary["tmux_history_limit"], 90000)
        self.assertEqual(summary["tmux_default_size"], "180x56")
        self.assertIn(["set-option", "-t", "itb-org-test", "history-limit", "90000"], calls)
        self.assertIn(["set-option", "-t", "itb-org-test", "default-size", "180x56"], calls)

    def test_ensure_agent_processes_uses_session_pane_cache_and_single_tmux_config(self) -> None:
        builder = load_builder_module()
        rows = [
            {
                "agent_id": "gate-prompt-formatter",
                "agent_instance_id": "gate-prompt-formatter@test-session",
                "organization_instance_id": "org-test",
                "parent_session_id": "test-session",
                "activation_status": "metadata_ready",
                "provider": "anthropic",
                "intended_model": "claude-haiku-4-5",
                "execution_mode": "agent",
                "startup_profile": "provider_cli",
            },
            {
                "agent_id": "gate-task-creator",
                "agent_instance_id": "gate-task-creator@test-session",
                "organization_instance_id": "org-test",
                "parent_session_id": "test-session",
                "activation_status": "metadata_ready",
                "provider": "anthropic",
                "intended_model": "claude-haiku-4-5",
                "execution_mode": "agent",
                "startup_profile": "provider_cli",
            },
        ]
        start_command = (
            "ITB_AGENT_PROCESS_MODE=provider_cli "
            "ITB_PROVIDER_ADD_DIRS_EFFECTIVE= "
            "ITB_PROVIDER_STATE_DIR= "
            "ITB_PROVIDER_CONFIG_POLICY= "
            "ITB_PROVIDER_AUTH_POLICY= "
            "ITB_PROVIDER_PERMISSION_MODE_EFFECTIVE= "
            "ITB_PROVIDER_TOOLS_EFFECTIVE= "
            "ITB_CODEX_APPROVAL_POLICY_EFFECTIVE= "
            "ITB_CLAUDE_EFFORT_EFFECTIVE= "
            "ITB_CLAUDE_FAST_MODE_EFFECTIVE= "
            "ITB_CODEX_MODEL_EFFECTIVE= "
            "ITB_CODEX_REASONING_EFFORT_EFFECTIVE= "
            "ITB_CODEX_SERVICE_TIER_EFFECTIVE= "
            "claude"
        )
        pane_lines = "\n".join(
            [
                f"itb-org-test\tgate-prompt-formatter\t0\tclaude\t0\t111\t/tmp\tITB_AGENT_ID=gate-prompt-formatter {start_command}",
                f"itb-org-test\tgate-task-creator\t0\tclaude\t0\t222\t/tmp\tITB_AGENT_ID=gate-task-creator {start_command}",
            ]
        )
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            calls.append(args)
            if args[:2] == ["list-panes", "-a"]:
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout=pane_lines, stderr="")
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/tool"), mock.patch.object(
            builder,
            "cleanup_stale_tmux_sessions",
            return_value={"enabled": True, "killed_count": 0, "failed_count": 0},
        ), mock.patch.object(
            builder,
            "tmux_session_exists",
            return_value=True,
        ), mock.patch.object(
            builder,
            "tmux_window_exists",
            return_value=True,
        ), mock.patch.object(
            builder,
            "tmux_pane_info",
            side_effect=AssertionError("pane info should come from session cache"),
        ), mock.patch.object(
            builder,
            "build_agent_command",
            return_value=("launch command", "claude_tmux"),
        ), mock.patch.object(
            builder,
            "wait_for_interactive_prompt",
            return_value=(True, ""),
        ), mock.patch.object(
            builder,
            "run_tmux",
            side_effect=fake_run_tmux,
        ):
            summary = builder.ensure_agent_processes(
                roster=rows,
                cwd="/tmp",
                session_id="test-session",
                organization_instance_id="org-test",
                queue_root=Path("/tmp/queue"),
                now="2026-01-01T00:00:00+09:00",
            )

        self.assertEqual(summary["process_ready_count"], 2)
        self.assertEqual(summary["tmux_config_scope"], "session")
        self.assertEqual(summary["tmux_config_status"], "configured")
        self.assertEqual(summary["pane_info_cache_status"], "ready")
        self.assertEqual(sum(1 for call in calls if call[:2] == ["list-panes", "-a"]), 1)
        self.assertEqual(sum(1 for call in calls if call[:2] == ["set-option", "-t"]), 2)
        self.assertTrue(all(row["launch_status"] == "already_running" for row in rows))
        by_agent = {result["agent_id"]: result for result in summary["results"]}
        self.assertEqual(by_agent["gate-prompt-formatter"]["pane_pid"], "111")
        self.assertEqual(by_agent["gate-task-creator"]["pane_pid"], "222")

    def test_archived_state_dir_cleanup_moves_only_old_archived_sessions(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            old_archived = state_root / "old-archived"
            recent_archived = state_root / "recent-archived"
            active_old = state_root / "active-old"
            current = state_root / "current-session"
            for path in (old_archived, recent_archived, active_old, current):
                path.mkdir(parents=True)
            (old_archived / "status").write_text("archived\n", encoding="utf-8")
            (recent_archived / "shutdown.json").write_text("{}\n", encoding="utf-8")
            (current / "status").write_text("archived\n", encoding="utf-8")
            os.utime(old_archived, (1000, 1000))
            os.utime(recent_archived, (99980, 99980))
            os.utime(active_old, (1000, 1000))
            os.utime(current, (1000, 1000))

            with mock.patch.dict(
                os.environ,
                {
                    "ITB_STATE_DIR_KEEP_RECENT": "1",
                    "ITB_STATE_DIR_MIN_AGE_SECONDS": "3600",
                },
                clear=False,
            ):
                summary = builder.cleanup_archived_state_dirs(
                    state_root=state_root,
                    current_session_id="current-session",
                    now_epoch=100000,
                )

            self.assertEqual(summary["archived_candidate_count"], 2)
            self.assertEqual(summary["moved_count"], 1)
            self.assertFalse(old_archived.exists())
            self.assertTrue((state_root / "archive" / "sessions" / "old-archived").exists())
            self.assertTrue(recent_archived.exists())
            self.assertTrue(active_old.exists())
            self.assertTrue(current.exists())

    def test_state_dir_cleanup_archives_old_orphan_without_live_tmux(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            orphan = state_root / "orphan-session"
            current = state_root / "current-session"
            orphan.mkdir(parents=True)
            current.mkdir(parents=True)
            (orphan / "status").write_text("ready\n", encoding="utf-8")
            (orphan / "bootstrap.json").write_text(
                json.dumps({"tmux_session": "itb-org-orphan"}) + "\n",
                encoding="utf-8",
            )
            os.utime(orphan, (1000, 1000))

            with mock.patch.dict(
                os.environ,
                {
                    "ITB_STATE_DIR_KEEP_RECENT": "0",
                    "ITB_STATE_DIR_MIN_AGE_SECONDS": "999999",
                    "ITB_STATE_DIR_ORPHAN_MIN_AGE_SECONDS": "3600",
                },
                clear=False,
            ), mock.patch.object(
                builder.shutil, "which", return_value="/usr/bin/tmux"
            ), mock.patch.object(
                builder,
                "tmux_session_exists",
                return_value=False,
            ) as tmux_exists:
                summary = builder.cleanup_archived_state_dirs(
                    state_root=state_root,
                    current_session_id="current-session",
                    now_epoch=100000,
                )

            self.assertEqual(summary["archived_candidate_count"], 0)
            self.assertEqual(summary["orphan_candidate_count"], 1)
            self.assertEqual(summary["moved_count"], 1)
            self.assertFalse(orphan.exists())
            self.assertTrue((state_root / "archive" / "sessions" / "orphan-session").exists())
            self.assertEqual(summary["moved_sessions"][0]["reason"], "orphan_tmux_missing")
            tmux_exists.assert_called_once_with("itb-org-orphan")

    def test_model_registry_keeps_active_gate_agents_resident_with_expected_startup_profiles(self) -> None:
        builder = load_builder_module()
        rows = builder.parse_registry()
        gate_rows = [
            row
            for row in rows
            if row.get("status") == "active"
            and (row.get("team") == "gate" or row.get("agent_id") == "teams-project-manager")
        ]
        grh_rows = [row for row in rows if row.get("agent_id") == "gate-response-humanizer"]
        reference_gate_rows = {
            row["agent_id"]: row
            for row in rows
            if row.get("agent_id") in {"gate-task-assessor", "gate-task-guardian"}
        }
        gate_rows_by_id = {row["agent_id"]: row for row in gate_rows}

        self.assertGreaterEqual(len(gate_rows), 4)
        self.assertTrue(all(row["resident_target"] == "true" for row in gate_rows))
        self.assertEqual(gate_rows_by_id["gate-prompt-formatter"]["startup_profile"], "provider_cli")
        self.assertEqual(gate_rows_by_id["gate-task-creator"]["startup_profile"], "lazy_activation")
        self.assertEqual(gate_rows_by_id["gate-task-creator"]["always_active"], "false")
        self.assertEqual(gate_rows_by_id["teams-project-manager"]["startup_profile"], "provider_cli")
        self.assertEqual(gate_rows_by_id["gate-task-evaluator"]["startup_profile"], "lazy_activation")
        self.assertEqual(reference_gate_rows["gate-task-assessor"]["status"], "reference")
        self.assertEqual(reference_gate_rows["gate-task-assessor"]["resident_target"], "false")
        self.assertEqual(reference_gate_rows["gate-task-guardian"]["status"], "reference")
        self.assertEqual(reference_gate_rows["gate-task-guardian"]["resident_target"], "false")
        self.assertEqual(len(grh_rows), 1)
        self.assertEqual(grh_rows[0]["status"], "reference")
        self.assertEqual(grh_rows[0]["resident_target"], "false")
        self.assertEqual(grh_rows[0]["startup_profile"], "compatibility_only")

    def test_reference_gate_skills_do_not_define_runtime_procedures(self) -> None:
        builder = load_builder_module()
        migrated_role_ids = {
            "gate-task-assessor",
            "gate-task-guardian",
            "gate-task-evaluator",
            "gate-response-humanizer",
            "tech-tester",
            "git-publisher",
            "teams-project-manager",
            "tech-director",
            "contents-director",
            "business-director",
        }
        for role_id in migrated_role_ids:
            self.assertFalse((SKILL_ROOT.parent / role_id / "SKILL.md").exists())
            self.assertTrue((builder.ATV_ROLE_ROOT / f"{role_id}.md").exists())

        infra_qa_text = builder.role_definition_path("infra-local-qa").read_text(encoding="utf-8")
        registry_text = (SKILL_ROOT / "config" / "role-agent-registry.yaml").read_text(encoding="utf-8")
        infra_director_text = builder.role_definition_path("infra-director").read_text(encoding="utf-8")
        team_config_text = (SKILL_ROOT / "references" / "team-config.md").read_text(encoding="utf-8")
        comprehensive_plan_text = (
            SKILL_ROOT / "references" / "resident-organization-comprehensive-plan.md"
        ).read_text(encoding="utf-8")

        self.assertIn('"gate-task-evaluator": {\n      "model_registry_ref": "gate-task-evaluator",\n      "queue_consumer": true,\n      "allowed_tools": ["Read", "Grep", "Glob"]', registry_text)
        self.assertIn('"gate-task-guardian": {\n      "model_registry_ref": "gate-task-guardian",\n      "queue_consumer": false,\n      "allowed_tools": ["Read", "Grep", "Glob"]', registry_text)
        self.assertIn("`vault-final-update` / `finalization-check`", infra_qa_text)
        self.assertNotIn("`gate-task-guardian` に Vault 更新証跡を渡す", infra_qa_text)
        self.assertIn("`teams-project-manager` via structured Completion Report", infra_director_text)
        self.assertIn("`team-completion-check` command evidence", infra_director_text)
        self.assertNotIn("then `gate-task-assessor` after Completion Report", infra_director_text)
        self.assertNotIn("Completion Report を `gate-task-assessor` へ渡す", infra_director_text)
        self.assertNotIn("| Handoff To | gate-task-assessor |", infra_director_text)
        self.assertNotIn("| Completion Handoff | `gate-task-assessor` |", infra_director_text)
        self.assertNotIn("handoff 先を `gate-task-assessor`", infra_director_text)
        self.assertIn("`status: reference` / `startup_profile: compatibility_only`", team_config_text)
        self.assertNotIn("Gate 系 role と `teams-project-manager` は必ず", team_config_text)
        self.assertIn("| Gate reference | `gate-task-assessor`, `gate-task-guardian`", comprehensive_plan_text)
        self.assertIn("-> team-completion-check", comprehensive_plan_text)
        self.assertIn("-> finalization-check", comprehensive_plan_text)
        self.assertIn("-> final-transport-render-check", comprehensive_plan_text)
        self.assertNotIn("-> assessor", comprehensive_plan_text)
        self.assertNotIn("-> guardian", comprehensive_plan_text)
        self.assertNotIn("guardian complete", comprehensive_plan_text)
        self.assertNotIn("Guardian 必須化", comprehensive_plan_text)
        self.assertNotIn("assessor / evaluator / guardian", comprehensive_plan_text)

        lint = builder.gate_skill_contract_lint_output(
            runtime="codex",
            state_root=Path(tempfile.gettempdir()),
            hook_input={},
        )["gateSkillContractLint"]
        self.assertEqual(lint["status"], "pass")

    def test_gate_skill_contract_lint_passes_current_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            output = self.run_builder(
                state_root,
                "gate-skill-contract-lint",
                {},
            )

        lint = output["gateSkillContractLint"]
        self.assertEqual(lint["status"], "pass")
        self.assertEqual(lint["finding_count"], 0)
        self.assertGreater(lint["checked_file_count"], 10)

    def test_gate_skill_contract_lint_blocks_retired_runtime_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            skills_root = tmp_root / "skills"
            self.copy_gate_contract_lint_fixture(skills_root)
            assessor_path = skills_root / "gate-task-assessor" / "SKILL.md"
            assessor_path.write_text(
                assessor_path.read_text(encoding="utf-8")
                + "\n## 実行手順\n\n| Handoff To | gate-task-assessor |\n\n"
                + "Completion Report を作成して gate-task-assessor へ渡す。\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "gate-skill-contract-lint",
                    "--runtime",
                    "codex",
                    "--state-root",
                    str(tmp_root / "state"),
                ],
                input=json.dumps({"skills_root": str(skills_root)}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            output = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 2)
        lint = output["gateSkillContractLint"]
        self.assertEqual(output["decision"], "block")
        self.assertEqual(lint["status"], "block")
        self.assertTrue(any(item["rule_id"] == "assessor_forbid_runtime_steps" for item in lint["findings"]))
        self.assertTrue(any(item["rule_id"] == "assessor_forbid_self_handoff_table" for item in lint["findings"]))
        self.assertTrue(any(item.get("forbidden") == "## 実行手順" for item in lint["findings"]))
        self.assertIn("assessor_forbid_runtime_steps", output["reason"])

    def test_gate_skill_contract_lint_blocks_reference_gate_model_registry_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            skills_root = tmp_root / "skills"
            self.copy_gate_contract_lint_fixture(skills_root)
            model_registry_path = skills_root / "infra-team-bootstrap" / "references" / "model-registry.md"
            model_registry_text = model_registry_path.read_text(encoding="utf-8")
            model_registry_path.write_text(
                model_registry_text.replace(
                    "| gate-task-assessor | gate | reference | false | false |",
                    "| gate-task-assessor | gate | active | true | true |",
                    1,
                ).replace(
                    "| agent | low | low | compatibility_only |  | 2026-06-13: runtime agent から外す。",
                    "| agent | low | low | provider_cli |  | 2026-06-13: runtime agent から外す。",
                    1,
                ),
                encoding="utf-8",
            )

            builder = load_builder_module()
            output = builder.gate_skill_contract_lint_output(
                runtime="codex",
                state_root=tmp_root / "state",
                hook_input={"skills_root": str(skills_root)},
            )

        lint = output["gateSkillContractLint"]
        self.assertEqual(output["decision"], "block")
        self.assertEqual(lint["status"], "block")
        model_findings = [
            item
            for item in lint["findings"]
            if item["rule_id"] == "reference_gate_model_registry_runtime_enabled"
        ]
        self.assertGreaterEqual(len(model_findings), 3)
        self.assertTrue(any(item["field"] == "status" and item["actual"] == "active" for item in model_findings))
        self.assertTrue(any(item["field"] == "resident_target" and item["actual"] == "true" for item in model_findings))
        self.assertTrue(any(item["field"] == "startup_profile" and item["actual"] == "provider_cli" for item in model_findings))

    def test_prompt_preflight_blocks_gate_skill_contract_lint_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            state_root = tmp_root / "state"
            session_dir = self.bootstrap(state_root)
            skills_root = tmp_root / "skills"
            self.copy_gate_contract_lint_fixture(skills_root)
            guardian_path = skills_root / "gate-task-guardian" / "SKILL.md"
            guardian_path.write_text(
                guardian_path.read_text(encoding="utf-8")
                + "\n| Guardian | gate-task-guardian |\n",
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "hello",
                    "skills_root": str(skills_root),
                },
            )

            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )

        self.assertIn("Gate skill contract lint failed", output["reason"])
        self.assertEqual(event["result"], "preflight_blocked")
        self.assertEqual(event["gate_skill_contract_lint"]["status"], "block")
        self.assertTrue(
            any(
                item["rule_id"] == "guardian_forbid_self_handoff_table"
                for item in event["gate_skill_contract_lint"]["findings"]
            )
        )

    def test_prompt_preflight_can_skip_gate_skill_contract_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            state_root = tmp_root / "state"
            session_dir = self.bootstrap(state_root)
            skills_root = tmp_root / "skills"
            self.copy_gate_contract_lint_fixture(skills_root)
            guardian_path = skills_root / "gate-task-guardian" / "SKILL.md"
            guardian_path.write_text(
                guardian_path.read_text(encoding="utf-8")
                + "\n| Guardian | gate-task-guardian |\n",
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "prompt-preflight",
                {
                    "session_id": "test-session",
                    "prompt": "hello",
                    "skills_root": str(skills_root),
                    "skip_gate_skill_contract_lint": True,
                },
            )

            event = json.loads(
                (session_dir / "preflight-events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )

        self.assertNotIn("decision", output)
        self.assertEqual(event["result"], "preflight_ready")
        self.assertEqual(event["gate_skill_contract_lint"]["status"], "skipped")

    def test_role_evals_do_not_expect_retired_assessor_guardian_runtime_flow(self) -> None:
        retired_patterns = [
            "gate-task-assessor へ渡す",
            "gate-task-assessor への Completion Report handoff",
            "handoff 先を常に gate-task-assessor",
            "Completion Report を作成して gate-task-assessor",
            "gate-task-assessor ->",
            "assessor / evaluator / guardian",
            "assessor は ready_for_evaluation",
            "GTG",
            "Guardian が complete",
            "Guardian Status Checked",
            "Guardian Verdict complete",
            "Guardian Verdict が missing",
            "Guardian Verdict を作って",
            "guardian_status",
            "guardian -> main transport renderer",
            "guardian complete",
            "guardian OK",
            "next_role: gate-task-guardian",
            "gate-task-guardian へ戻す",
        ]
        eval_paths = sorted(SKILL_ROOT.parent.glob("*/evals/evals.json"))

        for eval_path in eval_paths:
            payload = json.loads(eval_path.read_text(encoding="utf-8"))
            for eval_case in payload.get("evals", []):
                eval_text = json.dumps(eval_case, ensure_ascii=False, sort_keys=True)
                for pattern in retired_patterns:
                    with self.subTest(path=str(eval_path), eval_id=eval_case.get("id"), pattern=pattern):
                        self.assertNotIn(pattern, eval_text)

    def test_model_registry_rejects_malformed_markdown_rows(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_registry = Path(tmp) / "model-registry.md"
            bad_registry.write_text(
                "# Bad Registry\n\n"
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| gate-prompt-formatter | gate | active | true | true | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | note | extra-cell |\n",
                encoding="utf-8",
            )
            original_registry = builder.MODEL_REGISTRY
            builder.MODEL_REGISTRY = bad_registry
            try:
                with self.assertRaisesRegex(ValueError, "model registry row has 15 cells but expected 14"):
                    builder.parse_registry()
            finally:
                builder.MODEL_REGISTRY = original_registry

    def test_model_registry_rejects_missing_required_columns(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_registry = Path(tmp) / "model-registry.md"
            bad_registry.write_text(
                "# Bad Registry\n\n"
                "| agent_id | team | status |\n"
                "|---|---|---|\n"
                "| gate-prompt-formatter | gate | active |\n",
                encoding="utf-8",
            )
            original_registry = builder.MODEL_REGISTRY
            builder.MODEL_REGISTRY = bad_registry
            try:
                with self.assertRaisesRegex(ValueError, "missing required columns"):
                    builder.parse_registry()
            finally:
                builder.MODEL_REGISTRY = original_registry

    def test_model_registry_rejects_duplicate_agent_id(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad_registry = Path(tmp) / "model-registry.md"
            bad_registry.write_text(
                "# Bad Registry\n\n"
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| gate-prompt-formatter | gate | active | true | true | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | first |\n"
                "| gate-prompt-formatter | gate | active | true | true | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | duplicate |\n",
                encoding="utf-8",
            )
            original_registry = builder.MODEL_REGISTRY
            builder.MODEL_REGISTRY = bad_registry
            try:
                with self.assertRaisesRegex(ValueError, "duplicate agent_id"):
                    builder.parse_registry()
            finally:
                builder.MODEL_REGISTRY = original_registry

    def test_read_json_yaml_accepts_standard_yaml(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "report.yaml"
            payload_path.write_text(
                """
status: done
summary: queue report
files_changed:
  - skills/infra-team-bootstrap/scripts/itb_bootstrap_builder.py
provider_evidence:
  usage_source: claude_transcript_jsonl
""",
                encoding="utf-8",
            )

            payload = builder.read_json_yaml(payload_path)

        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["files_changed"], ["skills/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"])
        self.assertEqual(payload["provider_evidence"]["usage_source"], "claude_transcript_jsonl")

    def test_policy_digest_records_hashes_and_missing_sources(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = root / "ready.md"
            missing = root / "missing.md"
            ready.write_text("# Policy\n\nversion: test\n", encoding="utf-8")

            entries = builder.policy_digest_entries({"Ready Policy": ready, "Missing Policy": missing})
            by_policy = {entry["policy_id"]: entry for entry in entries}

            self.assertEqual(by_policy["Ready Policy"]["status"], "ready")
            self.assertEqual(len(by_policy["Ready Policy"]["sha1"]), 40)
            self.assertGreater(by_policy["Ready Policy"]["byte_count"], 0)
            self.assertEqual(by_policy["Missing Policy"]["status"], "missing")
            self.assertEqual(by_policy["Missing Policy"]["sha1"], "")
            self.assertEqual(len(builder.policy_digest_sha1(entries)), 40)

    def test_policy_digest_skill_block_replaces_existing_block(self) -> None:
        builder = load_builder_module()
        first_entries = [
            {
                "policy_id": "Ready Policy",
                "path": "/tmp/ready.md",
                "status": "ready",
                "sha1": "a" * 40,
                "byte_count": 12,
            }
        ]
        second_entries = [
            {
                "policy_id": "Ready Policy",
                "path": "/tmp/ready.md",
                "status": "ready",
                "sha1": "b" * 40,
                "byte_count": 34,
            }
        ]

        first_block = builder.render_policy_digest_skill_block(first_entries)
        second_block = builder.render_policy_digest_skill_block(second_entries)
        updated = builder.replace_policy_digest_skill_block("# Role\n\nbody\n", first_block)
        replaced = builder.replace_policy_digest_skill_block(updated, second_block)

        self.assertIn(builder.POLICY_DIGEST_SKILL_BLOCK_BEGIN, replaced)
        self.assertEqual(replaced.count(builder.POLICY_DIGEST_SKILL_BLOCK_BEGIN), 1)
        self.assertIn("`" + builder.policy_digest_sha1(second_entries) + "`", replaced)
        self.assertNotIn("`" + builder.policy_digest_sha1(first_entries) + "`", replaced)
        self.assertIn("body", replaced)

    def test_sync_policy_digest_skills_updates_active_resident_roles_only(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills_root = root / "skills"
            state_root = root / "state"
            registry = root / "model-registry.md"
            policy = root / "policy.md"
            policy.write_text("# Policy\n\nversion: test\n", encoding="utf-8")
            registry.write_text(
                "# Test Registry\n\n"
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| active-role | gate | active | true | true | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | active |\n"
                "| inactive-role | gate | active | false | false | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | not resident |\n",
                encoding="utf-8",
            )
            active_skill = skills_root / "active-role" / "SKILL.md"
            inactive_skill = skills_root / "inactive-role" / "SKILL.md"
            active_skill.parent.mkdir(parents=True)
            inactive_skill.parent.mkdir(parents=True)
            active_skill.write_text("# Active Role\n", encoding="utf-8")
            inactive_skill.write_text("# Inactive Role\n", encoding="utf-8")

            original_registry = builder.MODEL_REGISTRY
            original_skills_root = builder.SKILLS_ROOT
            original_policy_sources = builder.POLICY_DIGEST_SOURCES
            builder.MODEL_REGISTRY = registry
            builder.SKILLS_ROOT = skills_root
            builder.POLICY_DIGEST_SOURCES = {"Ready Policy": policy}
            try:
                output = builder.sync_policy_digest_skills(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "sync-session"},
                )
                second_output = builder.sync_policy_digest_skills(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "sync-session"},
                )
            finally:
                builder.MODEL_REGISTRY = original_registry
                builder.SKILLS_ROOT = original_skills_root
                builder.POLICY_DIGEST_SOURCES = original_policy_sources

            summary = output["policyDigestSkillSync"]
            second_summary = second_output["policyDigestSkillSync"]
            active_text = active_skill.read_text(encoding="utf-8")
            inactive_text = inactive_skill.read_text(encoding="utf-8")
            event_path = state_root / "sync-session" / "policy-digest-sync-events.jsonl"
            event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])

            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["updated_roles"], ["active-role"])
            self.assertEqual(second_summary["updated_count"], 0)
            self.assertEqual(second_summary["unchanged_roles"], ["active-role"])
            self.assertIn(builder.POLICY_DIGEST_SKILL_BLOCK_BEGIN, active_text)
            self.assertIn(summary["policy_digest_sha1"], active_text)
            self.assertNotIn(builder.POLICY_DIGEST_SKILL_BLOCK_BEGIN, inactive_text)
            self.assertEqual(event["event_type"], "policy_digest_skill_sync")
            self.assertEqual(event["policy_digest_sha1"], summary["policy_digest_sha1"])

    def test_sync_policy_digest_skills_skips_atv_migrated_roles(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills_root = root / "skills"
            state_root = root / "state"
            registry = root / "model-registry.md"
            policy = root / "policy.md"
            policy.write_text("# Policy\n\nversion: test\n", encoding="utf-8")
            registry.write_text(
                "# Test Registry\n\n"
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| gate-prompt-formatter | gate | active | true | true | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | migrated |\n",
                encoding="utf-8",
            )

            original_registry = builder.MODEL_REGISTRY
            original_skills_root = builder.SKILLS_ROOT
            original_policy_sources = builder.POLICY_DIGEST_SOURCES
            builder.MODEL_REGISTRY = registry
            builder.SKILLS_ROOT = skills_root
            builder.POLICY_DIGEST_SOURCES = {"Ready Policy": policy}
            try:
                output = builder.sync_policy_digest_skills(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "sync-session", "role_id": "gate-prompt-formatter"},
                )
            finally:
                builder.MODEL_REGISTRY = original_registry
                builder.SKILLS_ROOT = original_skills_root
                builder.POLICY_DIGEST_SOURCES = original_policy_sources

            summary = output["policyDigestSkillSync"]
            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["updated_roles"], [])
            self.assertEqual(summary["missing_roles"], [])
            self.assertEqual(summary["skipped_migrated_roles"], ["gate-prompt-formatter"])

    def test_sync_policy_digest_skills_can_opt_in_reference_roles(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills_root = root / "skills"
            state_root = root / "state"
            registry = root / "model-registry.md"
            policy = root / "policy.md"
            policy.write_text("# Policy\n\nversion: test\n", encoding="utf-8")
            registry.write_text(
                "# Test Registry\n\n"
                "| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| active-role | gate | active | true | true | anthropic | claude-haiku-4-5 |  | agent | low | low | provider_cli |  | active |\n"
                "| reference-role | gate | reference | false | false | anthropic | claude-haiku-4-5 |  | agent | low | low | compatibility_only |  | legacy reference |\n",
                encoding="utf-8",
            )
            active_skill = skills_root / "active-role" / "SKILL.md"
            reference_skill = skills_root / "reference-role" / "SKILL.md"
            active_skill.parent.mkdir(parents=True)
            reference_skill.parent.mkdir(parents=True)
            active_skill.write_text("# Active Role\n", encoding="utf-8")
            reference_skill.write_text("# Reference Role\n", encoding="utf-8")

            original_registry = builder.MODEL_REGISTRY
            original_skills_root = builder.SKILLS_ROOT
            original_policy_sources = builder.POLICY_DIGEST_SOURCES
            builder.MODEL_REGISTRY = registry
            builder.SKILLS_ROOT = skills_root
            builder.POLICY_DIGEST_SOURCES = {"Ready Policy": policy}
            try:
                rejected_output = builder.sync_policy_digest_skills(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "sync-session", "role_id": "reference-role"},
                )
                accepted_output = builder.sync_policy_digest_skills(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "sync-session",
                        "role_id": "reference-role",
                        "include_reference_roles": True,
                    },
                )
            finally:
                builder.MODEL_REGISTRY = original_registry
                builder.SKILLS_ROOT = original_skills_root
                builder.POLICY_DIGEST_SOURCES = original_policy_sources

            rejected_summary = rejected_output["policyDigestSkillSync"]
            accepted_summary = accepted_output["policyDigestSkillSync"]
            reference_text = reference_skill.read_text(encoding="utf-8")

            self.assertEqual(rejected_summary["status"], "failed")
            self.assertFalse(rejected_summary["include_reference_roles"])
            self.assertEqual(
                rejected_summary["errors"],
                [{"agent_id": "reference-role", "error": "role_id is not an active resident Team Role"}],
            )
            self.assertEqual(accepted_summary["status"], "ready")
            self.assertTrue(accepted_summary["include_reference_roles"])
            self.assertEqual(accepted_summary["updated_roles"], ["reference-role"])
            self.assertIn(builder.POLICY_DIGEST_SKILL_BLOCK_BEGIN, reference_text)
            self.assertIn(accepted_summary["policy_digest_sha1"], reference_text)

    def test_session_start_report_includes_policy_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="policy-digest-session")
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            report = (session_dir / "bootstrap-report.md").read_text(encoding="utf-8")

            self.assertIn("policy_digest_status", state)
            self.assertIn("policy_digest_sha1", state)
            self.assertIn("policy_digest", state)
            self.assertIn("## Policy Digest", report)
            self.assertIn("| Policy | Status | SHA1 | Bytes | Source |", report)

    def test_session_start_launch_agents_dry_run_marks_process_ready_without_response_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"

            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "launch-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "launch-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            report = (session_dir / "bootstrap-report.md").read_text(encoding="utf-8")
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(state["bootstrap_status"], "ready")
            self.assertEqual(state["readiness_scope"], "process_ready")
            self.assertEqual(state["spawn_mode"], "tmux_process")
            self.assertEqual(state["resident_process_mode"], "provider_cli")
            self.assertEqual(state["resident_agents_process_ready"], state["process_launch_target_count"])
            self.assertLess(state["resident_agents_process_ready"], state["resident_agents_total"])
            self.assertGreater(state["lazy_activation_agents"], 0)
            self.assertEqual(state["resident_agents_provider_ready"], state["process_launch_target_count"])
            self.assertEqual(state["resident_agents_tool_sidecar_ready"], 0)
            self.assertEqual(state["resident_agents_interactive_ready"], 0)
            self.assertEqual(state["interactive_readiness_scope"], "not_checked_dry_run")
            self.assertEqual(state["interactive_checked_count"], 0)
            self.assertEqual(state["interactive_blocker_count"], 0)
            self.assertEqual(state["resident_agents_response_ready"], 0)
            self.assertEqual(state["resident_agents_prompt_ready"], 0)
            self.assertEqual(state["prompt_readiness_scope"], "not_checked_dry_run")
            self.assertEqual(state["resident_agents_provider_response_ready"], 0)
            self.assertEqual(state["provider_response_readiness_scope"], "not_invoked")
            self.assertIn("resident_agents_prompt_ready", report)
            self.assertIn("resident_agents_provider_response_ready", report)
            self.assertTrue(state["launch_dry_run"])
            eager_rows = [row for row in roster if row["launch_status"] != "skipped_by_startup_profile"]
            lazy_rows = [row for row in roster if row["launch_status"] == "skipped_by_startup_profile"]
            self.assertEqual(len(eager_rows), state["process_launch_target_count"])
            self.assertEqual(len(lazy_rows), state["lazy_activation_agents"])
            self.assertTrue(all(row["process_status"] == "process_ready" for row in eager_rows))
            self.assertTrue(all(row["process_mode"] == "provider_cli" for row in eager_rows))
            self.assertTrue(all(row["runtime_kind"] in {"claude_tmux", "codex_tmux"} for row in eager_rows))
            self.assertTrue(all(row["provider_status"] == "provider_process_ready" for row in eager_rows))
            process_ready_rows = [row for row in eager_rows if row["process_status"] == "process_ready"]
            self.assertTrue(all(row["interactive_status"] == "not_checked_dry_run" for row in process_ready_rows))
            self.assertTrue(all(row["interactive_ready"] is False for row in process_ready_rows))
            self.assertTrue(all(row["tool_sidecar_status"] == "not_verified" for row in eager_rows))
            self.assertTrue(all(row["process_status"] == "not_launched" for row in lazy_rows))
            self.assertTrue(all(row["effective_model"] == "" for row in roster))
            self.assertTrue(all(row["usage_source"] == "bootstrap_metadata_only" for row in roster))
            self.assertTrue(all(row["runtime"] == "codex" for row in roster))
            self.assertTrue(all(row["state_root"] == str(state_root) for row in roster))
            self.assertTrue(any(entry["result"] == "process_ready" for entry in evidence))
            self.assertEqual(
                sum(1 for entry in evidence if entry["event_type"] == "agent_process_launch"),
                state["process_launch_target_count"],
            )

    def test_session_start_compacts_unchanged_compact_source(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            with mock.patch.dict(os.environ, {"ITB_AGENT_LAUNCH_DRY_RUN": "1"}, clear=False):
                first = builder.build_roster(
                    runtime="codex",
                    event="SessionStart",
                    state_root=state_root,
                    hook_input={"session_id": "compact-session", "cwd": "/tmp", "source": "SessionStart"},
                    launch_agents=True,
                )
                with mock.patch.object(
                    builder,
                    "ensure_agent_processes",
                    side_effect=AssertionError("unchanged compact SessionStart should not launch agents"),
                ), mock.patch.object(
                    builder,
                    "cleanup_archived_state_dirs",
                    side_effect=AssertionError("unchanged compact SessionStart should not run state dir GC"),
                ):
                    second = builder.build_roster(
                        runtime="codex",
                        event="SessionStart",
                        state_root=state_root,
                        hook_input={"session_id": "compact-session", "cwd": "/tmp", "source": "compact"},
                        launch_agents=True,
                    )

            session_dir = state_root / "compact-session"
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))

            self.assertFalse(first["session_start_compacted"])
            self.assertTrue(second["session_start_compacted"])
            self.assertEqual(second["session_start_compact_reason"], "unchanged_session_start_guard")
            self.assertEqual(second["state_dir_gc"]["result"], "skipped_session_start_compacted")
            self.assertEqual(state["session_start_compact_reason"], "unchanged_session_start_guard")
            self.assertTrue(any(entry["event_type"] == "session_start_compacted" for entry in evidence))

    def test_session_start_does_not_compact_startup_source(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            with mock.patch.dict(os.environ, {"ITB_AGENT_LAUNCH_DRY_RUN": "1"}, clear=False):
                builder.build_roster(
                    runtime="codex",
                    event="SessionStart",
                    state_root=state_root,
                    hook_input={"session_id": "startup-session", "cwd": "/tmp", "source": "SessionStart"},
                    launch_agents=True,
                )
                second = builder.build_roster(
                    runtime="codex",
                    event="SessionStart",
                    state_root=state_root,
                    hook_input={"session_id": "startup-session", "cwd": "/tmp", "source": "startup"},
                    launch_agents=True,
                )

            self.assertFalse(second["session_start_compacted"])
            self.assertEqual(second["session_start_guard_check"]["reason"], "source_not_compactable")

    def test_session_start_launch_agents_filter_marks_partial_process_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            env["ITB_AGENT_LAUNCH_FILTER"] = "gate-task-evaluator,tech-reviewer"

            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "partial-launch-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "partial-launch-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows_by_id = {row["agent_id"]: row for row in roster}

            self.assertEqual(state["bootstrap_status"], "ready")
            self.assertEqual(state["readiness_scope"], "process_partial")
            self.assertEqual(state["process_launch_target_count"], 2)
            self.assertEqual(state["resident_agents_process_ready"], 1)
            self.assertEqual(rows_by_id["gate-task-evaluator"]["process_status"], "process_ready")
            self.assertEqual(rows_by_id["tech-reviewer"]["runtime_kind"], "codex_tmux")
            self.assertEqual(rows_by_id["tech-reviewer"]["provider_runtime_kind"], "codex_tmux")
            self.assertEqual(rows_by_id["tech-reviewer"]["process_status"], "not_launched")
            self.assertEqual(rows_by_id["tech-reviewer"]["launch_status"], "interactive_codex_resident_disabled")
            self.assertEqual(rows_by_id["tech-reviewer"]["provider_status"], "not_started")
            self.assertEqual(rows_by_id["tech-backend"]["launch_status"], "skipped_by_filter")

    def test_session_start_launch_agents_full_filter_marks_process_ready(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            env["ITB_ALLOW_INTERACTIVE_CODEX_RESIDENT"] = "1"
            env["ITB_AGENT_LAUNCH_FILTER"] = ",".join(
                row["agent_id"] for row in builder.role_agent_rows(organization_instance_id="org-test")
            )

            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "full-filter-launch-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "full-filter-launch-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))

            self.assertEqual(state["bootstrap_status"], "ready")
            self.assertEqual(state["readiness_scope"], "process_ready")
            self.assertEqual(state["process_launch_target_count"], state["resident_agents_registered"])
            self.assertEqual(state["resident_agents_process_ready"], state["resident_agents_registered"])
            self.assertEqual(state["lazy_activation_agents"], 0)
            self.assertTrue(all(row["process_status"] == "process_ready" for row in roster))

    def test_session_start_launch_agents_provider_mode_records_provider_without_sidecar_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            env["ITB_ALLOW_INTERACTIVE_CODEX_RESIDENT"] = "1"
            env["ITB_AGENT_PROCESS_MODE"] = "provider"
            env["ITB_AGENT_LAUNCH_FILTER"] = "tech-reviewer"

            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "provider-launch-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "provider-launch-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows_by_id = {row["agent_id"]: row for row in roster}

            self.assertEqual(state["readiness_scope"], "process_partial")
            self.assertEqual(state["resident_process_mode"], "provider_cli")
            self.assertEqual(state["resident_agents_provider_ready"], 1)
            self.assertEqual(state["resident_agents_tool_sidecar_ready"], 0)
            self.assertEqual(rows_by_id["tech-reviewer"]["process_mode"], "provider_cli")
            self.assertEqual(rows_by_id["tech-reviewer"]["runtime_kind"], "codex_tmux")
            self.assertEqual(rows_by_id["tech-reviewer"]["provider_status"], "provider_process_ready")
            self.assertEqual(rows_by_id["tech-reviewer"]["tool_sidecar_status"], "not_verified")

    def test_session_start_launch_agents_provider_mode_routes_gate_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"
            env["ITB_AGENT_PROCESS_MODE"] = "provider"
            env["ITB_AGENT_LAUNCH_FILTER"] = "gate-prompt-formatter,teams-project-manager"

            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "gate-provider-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "gate-provider-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows_by_id = {row["agent_id"]: row for row in roster}

            self.assertEqual(state["resident_process_mode"], "provider_cli")
            self.assertEqual(state["resident_agents_provider_ready"], 2)
            self.assertEqual(rows_by_id["gate-task-creator"]["launch_status"], "skipped_by_filter")
            self.assertEqual(rows_by_id["gate-task-creator"]["startup_profile"], "lazy_activation")
            for agent_id in ("gate-prompt-formatter", "teams-project-manager"):
                self.assertEqual(rows_by_id[agent_id]["provider"], "anthropic")
                self.assertTrue(rows_by_id[agent_id]["intended_model"].startswith("claude-"))
                self.assertEqual(rows_by_id[agent_id]["runtime_kind"], "claude_tmux")
                self.assertEqual(rows_by_id[agent_id]["provider_runtime_kind"], "claude_tmux")
                self.assertEqual(rows_by_id[agent_id]["provider_status"], "provider_process_ready")

    def test_provider_activate_records_claude_response_usage_for_gate_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            fake_bin = state_root / "bin"
            fake_bin.mkdir()
            fake_claude = fake_bin / "claude"
            args_file = state_root / "claude-args.txt"
            fake_claude.write_text(
                """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_CLAUDE_ARGS"
printf '%s\n' '{"result":"ok","model":"claude-sonnet-4-6","session_id":"claude-session-1","request_id":"req-123","usage":{"input_tokens":17,"output_tokens":5},"duration_api_ms":321,"num_turns":1,"total_cost_usd":0.001}'
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            env["FAKE_CLAUDE_ARGS"] = str(args_file)

            self.bootstrap(state_root, session_id="activation-session")
            session_dir = state_root / "activation-session"
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row.pop("fallback_models", None)
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output = self.run_builder(
                state_root,
                "provider-activate",
                {
                    "session_id": "activation-session",
                    "agent_id": "gate-prompt-formatter",
                    "prompt": "Summarize this test task in one sentence.",
                    "max_budget_usd": "0.01",
                },
                env=env,
            )
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows_by_id = {row["agent_id"]: row for row in roster}
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(output["activation"]["agent_id"], "gate-prompt-formatter")
            self.assertEqual(output["activation"]["effective_model"], "claude-sonnet-4-6")
            self.assertEqual(output["activation"]["input_tokens"], 17)
            self.assertEqual(output["activation"]["output_tokens"], 5)
            self.assertEqual(state["readiness_scope"], "response_evidence")
            self.assertEqual(state["resident_agents_response_ready"], 1)
            self.assertEqual(state["resident_agents_provider_response_ready"], 1)
            self.assertEqual(state["provider_response_readiness_scope"], "response_evidence")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["activation_status"], "response_active")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["usage_source"], "claude_print_json")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["session_id"], "claude-session-1")
            args = args_file.read_text(encoding="utf-8").splitlines()
            self.assertIn("--fallback-model", args)
            self.assertIn("claude-haiku-4-5", args)
            self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
            self.assertEqual(args[args.index("--effort") + 1], "medium")
            self.assertTrue(
                any(
                    entry["event_type"] == "provider_activation"
                    and entry["agent_id"] == "gate-prompt-formatter"
                    and entry["input_tokens"] == 17
                    and entry["output_tokens"] == 5
                    for entry in evidence
                )
            )

    def test_provider_activate_uses_model_tier_budget_default_for_opus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            fake_bin = state_root / "bin"
            fake_bin.mkdir()
            fake_claude = fake_bin / "claude"
            args_file = state_root / "claude-args.txt"
            fake_claude.write_text(
                """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_CLAUDE_ARGS"
printf '%s\n' '{"result":"ok","model":"claude-opus-4-8","session_id":"opus-session-1","usage":{"input_tokens":11,"output_tokens":7},"duration_api_ms":123,"num_turns":1,"total_cost_usd":0.25}'
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            env["FAKE_CLAUDE_ARGS"] = str(args_file)

            self.bootstrap(state_root, session_id="opus-budget-session")
            output = self.run_builder(
                state_root,
                "provider-activate",
                {
                    "session_id": "opus-budget-session",
                    "agent_id": "tech-lead",
                    "prompt": "Review this architecture decision.",
                },
                env=env,
            )
            args = args_file.read_text(encoding="utf-8").splitlines()
            budget_index = args.index("--max-budget-usd") + 1

            self.assertEqual(args[budget_index], "30.00")
            self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
            self.assertEqual(args[args.index("--effort") + 1], "max")
            self.assertEqual(output["activation"]["effective_model"], "claude-opus-4-8")
            self.assertEqual(output["activation"]["max_budget_usd"], "30.00")
            self.assertEqual(output["activation"]["budget_source"], "model_tier:opus")

    def test_claude_activation_budget_respects_explicit_hook_value(self) -> None:
        builder = load_builder_module()

        budget, source = builder.claude_activation_budget(
            {"intended_model": "claude-opus-4-8"},
            {"max_budget_usd": "3.50"},
        )

        self.assertEqual(budget, "3.50")
        self.assertEqual(source, "hook_input")

    def test_provider_activate_blocks_empty_claude_response_without_inference_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            fake_bin = state_root / "bin"
            fake_bin.mkdir()
            fake_claude = fake_bin / "claude"
            fake_claude.write_text(
                """#!/bin/sh
printf '%s\n' '{"type":"result","subtype":"success","duration_api_ms":0,"num_turns":0,"result":"","session_id":"empty-session","total_cost_usd":0,"usage":{"input_tokens":0,"output_tokens":0}}'
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

            self.bootstrap(state_root, session_id="empty-activation-session")
            session_dir = state_root / "empty-activation-session"
            state_path = session_dir / "bootstrap.json"
            roster_path = session_dir / "roster.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for row in roster:
                if row["agent_id"] == "gate-prompt-formatter":
                    row["activation_status"] = "response_active"
                    row["response_status"] = "invoked"
                    row["provider_status"] = "provider_response_ready"
                    row["effective_model"] = "claude-sonnet-4-6"
                    row["session_id"] = "stale-session"
                    row["last_request_id"] = "stale-request"
                    row["usage_source"] = "claude_print_json"
            state["readiness_scope"] = "response_evidence"
            state["resident_agents_response_ready"] = 1
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output = self.run_builder(
                state_root,
                "provider-activate",
                {
                    "session_id": "empty-activation-session",
                    "agent_id": "gate-prompt-formatter",
                    "prompt": "This fake Claude response is empty.",
                    "max_budget_usd": "0.01",
                },
                env=env,
            )
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows_by_id = {row["agent_id"]: row for row in roster}
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(output["decision"], "block")
            self.assertIn("no inference evidence", output["reason"])
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["activation_status"], "metadata_ready")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["response_status"], "not_invoked")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["provider_status"], "provider_no_inference")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["usage_source"], "claude_print_json_no_inference")
            self.assertEqual(rows_by_id["gate-prompt-formatter"]["session_id"], "")
            self.assertEqual(state["resident_agents_response_ready"], 0)
            self.assertEqual(state["resident_agents_provider_response_ready"], 0)
            self.assertEqual(state["provider_response_readiness_scope"], "not_invoked")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertTrue(
                any(
                    entry["event_type"] == "provider_activation"
                    and entry["agent_id"] == "gate-prompt-formatter"
                    and entry["result"] == "provider_activation_no_inference"
                    for entry in evidence
                )
            )

    def test_provider_activate_records_codex_exec_response_for_openai_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            fake_bin = state_root / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            args_file = state_root / "codex-args.txt"
            fake_codex.write_text(
                """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_CODEX_ARGS"
printf '%s\n' '{"type":"agent_message","message":"ok"}'
printf '%s\n' '{"type":"result","subtype":"success","model":"gpt-5.5","session_id":"codex-session-1","request_id":"req-codex-1","usage":{"input_tokens":21,"output_tokens":9},"duration_api_ms":456,"num_turns":1}'
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            env["FAKE_CODEX_ARGS"] = str(args_file)

            self.bootstrap(state_root, session_id="codex-activation-session")
            session_dir = state_root / "codex-activation-session"

            output = self.run_builder(
                state_root,
                "provider-activate",
                {
                    "session_id": "codex-activation-session",
                    "agent_id": "tech-backend",
                    "prompt": "Summarize this implementation task.",
                    "cwd": "/tmp/project",
                },
                env=env,
            )
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            rows_by_id = {row["agent_id"]: row for row in roster}
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            args = args_file.read_text(encoding="utf-8").splitlines()

            self.assertEqual(output["activation"]["agent_id"], "tech-backend")
            self.assertEqual(output["activation"]["effective_model"], "gpt-5.5")
            self.assertEqual(output["activation"]["usage_source"], "codex_exec_json")
            self.assertEqual(output["activation"]["input_tokens"], 21)
            self.assertEqual(state["readiness_scope"], "response_evidence")
            self.assertEqual(rows_by_id["tech-backend"]["activation_status"], "response_active")
            self.assertEqual(rows_by_id["tech-backend"]["usage_source"], "codex_exec_json")
            self.assertEqual(rows_by_id["tech-backend"]["session_id"], "codex-session-1")
            self.assertEqual(args[:4], ["exec", "--ephemeral", "--ignore-user-config", "--ignore-rules"])
            self.assertIn("--json", args)
            self.assertIn("--model", args)
            self.assertEqual(args[args.index("--model") + 1], "gpt-5.5")
            self.assertIn("--sandbox", args)
            self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
            self.assertNotIn("claude", " ".join(args))
            self.assertTrue(
                any(
                    entry["event_type"] == "provider_activation"
                    and entry["agent_id"] == "tech-backend"
                    and entry["usage_source"] == "codex_exec_json"
                    and entry["provider_session_id"] == "codex-session-1"
                    for entry in evidence
                )
            )

    def test_provider_mode_does_not_treat_resident_shell_pane_as_provider_cli(self) -> None:
        builder = load_builder_module()
        row = {"agent_id": "gate-prompt-formatter"}
        info = {
            "pane_start_command": "ITB_AGENT_ID=gate-prompt-formatter ITB_AGENT_PROCESS_MODE=resident_shell exec /bin/sh",
            "pane_current_command": "bash",
        }

        self.assertFalse(builder.pane_matches_agent(info, row, "claude", "provider_cli"))
        self.assertTrue(builder.pane_matches_agent(info, row, "/bin/sh", "resident_shell"))
        self.assertTrue(builder.pane_is_resident_shell_for_agent(info, row))

    def test_resident_shell_pane_does_not_require_worker_marker(self) -> None:
        builder = load_builder_module()
        row = {"agent_id": "gate-prompt-formatter"}
        info = {
            "pane_start_command": (
                "ITB_AGENT_ID=gate-prompt-formatter ITB_AGENT_PROCESS_MODE=resident_shell "
                "exec /bin/sh -lc 'while :; do sleep 3600; done'"
            ),
            "pane_current_command": "sh",
        }

        self.assertTrue(builder.pane_matches_agent(info, row, "/bin/sh", "resident_shell"))

    def test_resident_shell_upgrade_check_rejects_other_agents(self) -> None:
        builder = load_builder_module()
        row = {"agent_id": "gate-task-creator"}
        info = {
            "pane_start_command": "ITB_AGENT_ID=gate-prompt-formatter ITB_AGENT_PROCESS_MODE=resident_shell exec /bin/sh",
            "pane_current_command": "bash",
        }

        self.assertFalse(builder.pane_is_resident_shell_for_agent(info, row))

    def test_build_agent_command_allows_claude_permission_mode_override(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "runtime": "codex",
            "state_root": "/tmp/itb-state",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
        }

        command, runtime_kind = builder.build_agent_command(
            row,
            "/Users/takagiyasushi/skills-repo",
            "provider_cli",
            permission_mode="plan",
            tools="Read",
        )

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn("--permission-mode plan", command)
        self.assertIn("--effort medium", command)
        self.assertIn("--tools Read", command)

    def test_build_agent_command_defaults_haiku_permission_mode_to_accept_edits(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-task-creator",
            "agent_instance_id": "gate-task-creator@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-haiku-4-5",
            "execution_mode": "agent",
        }

        with mock.patch.dict(os.environ, {"ITB_PROVIDER_PERMISSION_MODE": ""}, clear=False):
            command, runtime_kind = builder.build_agent_command(
                row,
                "/Users/takagiyasushi/skills-repo",
                "provider_cli",
            )

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn("--permission-mode acceptEdits", command)
        self.assertIn("--effort medium", command)
        self.assertIn("ITB_CLAUDE_EFFORT_EFFECTIVE=medium", command)
        self.assertIn("ITB_CLAUDE_FAST_MODE_EFFECTIVE=disabled", command)
        self.assertIn("CLAUDE_CODE_DISABLE_FAST_MODE=1", command)
        self.assertNotIn("--fast", command)

    def test_build_agent_command_uses_role_allowed_tools_by_default(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "runtime": "codex",
            "state_root": "/tmp/itb-state",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
            "allowed_tools": ["Read", "Grep", "Glob"],
        }

        command, runtime_kind = builder.build_agent_command(
            row,
            "/Users/takagiyasushi/skills-repo",
            "provider_cli",
        )

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn("--tools Read,Grep,Glob", command)
        self.assertIn("ITB_PROVIDER_TOOLS_EFFECTIVE=Read,Grep,Glob", command)

    def test_build_agent_command_adds_transport_bash_for_queue_finalizer(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
            "allowed_tools": ["Read", "Grep", "Glob"],
            "runtime": "codex",
            "state_root": "/tmp/itb-state",
            "queue_consumer": True,
            "queue_finalizer": "role-report",
        }

        command, runtime_kind = builder.build_agent_command(
            row,
            "/Users/takagiyasushi/skills-repo",
            "provider_cli",
        )

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertEqual(builder.tools_argument_for_role(row), "Read,Grep,Glob")
        self.assertEqual(builder.transport_tools_argument_for_role(row), "Read,Grep,Glob,Bash")
        self.assertIn("--tools Read,Grep,Glob,Bash", command)
        self.assertIn("--allowedTools", command)
        self.assertIn("Bash(python3", command)
        self.assertIn("role-report", command)
        self.assertIn("--role-id gate-prompt-formatter", command)
        self.assertIn("--message-id * --report-json *", command)
        self.assertIn("ITB_PROVIDER_TOOLS_EFFECTIVE=Read,Grep,Glob,Bash", command)
        self.assertIn("ITB_CLAUDE_ALLOWED_TOOLS_EFFECTIVE=", command)

    def test_validate_tools_allows_transport_bash_for_queue_finalizer(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "allowed_tools": ["Read", "Grep", "Glob"],
            "queue_consumer": True,
            "queue_finalizer": "role-report",
        }

        self.assertEqual(builder.validate_tools_argument_for_role(row, "Read,Grep,Glob,Bash"), "")
        error = builder.validate_tools_argument_for_role(row, "Read,Grep,Glob,Bash,Write")
        self.assertIn("outside role profile", error)
        self.assertIn("Write", error)

    def test_build_agent_command_uses_max_effort_with_fast_mode_disabled_for_opus(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "tech-lead",
            "agent_instance_id": "tech-lead@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-opus-4-8",
            "execution_mode": "agent",
        }

        command, runtime_kind = builder.build_agent_command(
            row,
            "/Users/takagiyasushi/skills-repo",
            "provider_cli",
        )

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn("--permission-mode auto", command)
        self.assertIn("--effort max", command)
        self.assertIn("ITB_CLAUDE_EFFORT_EFFECTIVE=max", command)
        self.assertIn("ITB_CLAUDE_FAST_MODE_EFFECTIVE=disabled", command)
        self.assertIn("CLAUDE_CODE_DISABLE_FAST_MODE=1", command)
        self.assertNotIn("--fast", command)

    def test_build_agent_command_adds_provider_workspace_dirs(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-task-creator",
            "agent_instance_id": "gate-task-creator@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-haiku-4-5",
            "execution_mode": "agent",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            vault = home / "Agents-Vault"
            yasu = home / "Yasu Vault"
            queue = home / "itb-queue"
            workspace = home / "skills-repo"
            home.mkdir()
            vault.mkdir()
            yasu.mkdir()
            queue.mkdir()
            workspace.mkdir()
            row["state_root"] = str(home / "state")
            with mock.patch.object(builder.Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {
                    "ITB_PROVIDER_ADD_DIRS": os.pathsep.join([str(vault), str(yasu)]),
                    "ITB_PROVIDER_MEMORY_ISOLATION": "1",
                },
                clear=False,
            ):
                command, runtime_kind = builder.build_agent_command(
                    row,
                    str(workspace),
                    "provider_cli",
                    extra_add_dirs=[queue],
            )
            claude_state_dir = home / "state" / "test-session" / "provider-state" / "gate-task-creator" / "claude"
            self.assertTrue(claude_state_dir.exists())
            memory_policy = json.loads((claude_state_dir / "memory-policy.json").read_text(encoding="utf-8"))
            claude_memory_files_exist = (claude_state_dir / "CLAUDE.md").exists() and (claude_state_dir / "AGENTS.md").exists()

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn(f"cd {shlex.quote(str(claude_state_dir))}", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--add-dir", command)
        self.assertIn(str(workspace), command)
        self.assertIn(str(vault.resolve()), command)
        self.assertIn(shlex.quote(str(yasu.resolve())), command)
        self.assertIn(str(queue.resolve()), command)
        self.assertIn(f"ITB_PROVIDER_STATE_DIR={claude_state_dir}", command)
        self.assertIn("ITB_PROVIDER_CONFIG_DIR=''", command)
        self.assertIn("ITB_PROVIDER_CONFIG_POLICY=global_provider_config", command)
        self.assertIn("ITB_PROVIDER_AUTH_POLICY=provider_default_auth", command)
        self.assertNotIn("CLAUDE_CONFIG_DIR=", command)
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", command)
        self.assertIn("ITB_PROVIDER_MEMORY_POLICY=isolated_session_config", command)
        self.assertIn(f"ITB_PROVIDER_MEMORY_POLICY_FILE={claude_state_dir / 'memory-policy.json'}", command)
        self.assertEqual(memory_policy["policy"], "isolated_session_config")
        self.assertEqual(memory_policy["state_dir"], str(claude_state_dir))
        self.assertEqual(memory_policy["config_dir"], "")
        self.assertEqual(memory_policy["config_policy"], "global_provider_config")
        self.assertEqual(memory_policy["auth_policy"], "provider_default_auth")
        self.assertEqual(memory_policy["auth_seed"]["status"], "seeded")
        self.assertEqual(memory_policy["auth_seed"]["trust_seed_policy"], "global_claude_json")
        self.assertIn(str(vault.resolve()), memory_policy["auth_seed"]["trust_seeded_paths"])
        self.assertIn(str(yasu.resolve()), memory_policy["auth_seed"]["trust_seeded_paths"])
        self.assertEqual(memory_policy["workspace_cwd"], str(workspace))
        self.assertEqual(memory_policy["launch_cwd"], str(claude_state_dir))
        self.assertTrue(claude_memory_files_exist)

    def test_trust_seed_path_allowed_is_limited_to_home_and_tmp_roots(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with mock.patch.object(builder.Path, "home", return_value=home):
                self.assertTrue(builder.trust_seed_path_allowed(str(home / "dev" / "repo")))
                self.assertTrue(builder.trust_seed_path_allowed("/private/tmp/itb-worktrees/repo"))
                self.assertFalse(builder.trust_seed_path_allowed("/etc"))

    def test_backup_json_file_once_uses_unique_paths(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".claude.json"
            source.write_text('{"projects": {}}\n', encoding="utf-8")
            first = builder.backup_json_file_once(source, label="itb-trust-seed")
            second = builder.backup_json_file_once(source, label="itb-trust-seed")

        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith(".bak"))
        self.assertTrue(second.endswith(".bak"))

    def test_build_agent_command_can_opt_into_claude_provider_config_isolation(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-task-creator",
            "agent_instance_id": "gate-task-creator@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-haiku-4-5",
            "execution_mode": "agent",
            "state_root": "/tmp/itb-state",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace = home / "skills-repo"
            home.mkdir()
            workspace.mkdir()
            row["state_root"] = str(home / "state")
            with mock.patch.object(builder.Path, "home", return_value=home), mock.patch.dict(
                os.environ,
                {
                    "ITB_PROVIDER_CONFIG_ISOLATION": "1",
                    "ITB_PROVIDER_MEMORY_ISOLATION": "1",
                },
                clear=False,
            ):
                command, runtime_kind = builder.build_agent_command(
                    row,
                    str(workspace),
                    "provider_cli",
                )
            claude_config = home / "state" / "test-session" / "provider-config" / "gate-task-creator" / "claude"
            claude_state_dir = home / "state" / "test-session" / "provider-state" / "gate-task-creator" / "claude"
            memory_policy = json.loads((claude_state_dir / "memory-policy.json").read_text(encoding="utf-8"))
            claude_state = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            isolated_config_state_exists = (claude_config / ".claude.json").exists()

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn(f"cd {claude_state_dir}", command)
        self.assertIn(f"ITB_PROVIDER_STATE_DIR={claude_state_dir}", command)
        self.assertIn(f"ITB_PROVIDER_CONFIG_DIR={claude_config}", command)
        self.assertIn("ITB_PROVIDER_CONFIG_POLICY=isolated_provider_config", command)
        self.assertIn("ITB_PROVIDER_AUTH_POLICY=shared_secure_storage", command)
        self.assertIn(f"CLAUDE_CONFIG_DIR={claude_config}", command)
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR=''", command)
        self.assertEqual(memory_policy["config_dir"], str(claude_config))
        self.assertEqual(memory_policy["config_policy"], "isolated_provider_config")
        self.assertEqual(memory_policy["auth_policy"], "shared_secure_storage")
        self.assertEqual(memory_policy["auth_seed"]["status"], "seeded")
        self.assertEqual(memory_policy["auth_seed"]["trust_seed_policy"], "global_claude_json")
        self.assertFalse(isolated_config_state_exists)
        self.assertTrue(claude_state["hasCompletedOnboarding"])
        self.assertTrue(claude_state["projects"][str(claude_state_dir)]["hasTrustDialogAccepted"])
        self.assertTrue(claude_state["projects"][str(workspace)]["hasTrustDialogAccepted"])

    def test_build_agent_command_records_workspace_memory_policy_when_memory_isolation_disabled(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-task-creator",
            "agent_instance_id": "gate-task-creator@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-haiku-4-5",
            "execution_mode": "agent",
            "state_root": "/tmp/itb-state",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row["state_root"] = str(root / "state")
            with mock.patch.dict(os.environ, {}, clear=True):
                command, runtime_kind = builder.build_agent_command(
                    row,
                    "/Users/takagiyasushi/skills-repo",
                    "provider_cli",
                )
            claude_state_dir = root / "state" / "test-session" / "provider-state" / "gate-task-creator" / "claude"

        self.assertEqual(runtime_kind, "claude_tmux")
        self.assertIn("cd /Users/takagiyasushi/skills-repo", command)
        self.assertIn("ITB_PROVIDER_STATE_DIR=''", command)
        self.assertIn("ITB_PROVIDER_CONFIG_DIR=''", command)
        self.assertNotIn("CLAUDE_CONFIG_DIR=", command)
        self.assertIn("ITB_PROVIDER_MEMORY_POLICY=workspace_cwd", command)
        self.assertIn("ITB_PROVIDER_MEMORY_POLICY_FILE=''", command)
        self.assertFalse(claude_state_dir.exists())

    def test_build_agent_command_configures_codex_approval_and_add_dirs(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "tech-backend",
            "agent_instance_id": "tech-backend@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "openai",
            "intended_model": "gpt-5.5",
            "execution_mode": "codex",
            "state_root": "",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "Agents-Vault"
            vault.mkdir()
            row["state_root"] = str(root / "state")
            with mock.patch.dict(
                os.environ,
                {
                    "ITB_ALLOW_INTERACTIVE_CODEX_RESIDENT": "1",
                    "ITB_PROVIDER_ADD_DIRS": str(vault),
                    "ITB_PROVIDER_MEMORY_ISOLATION": "1",
                    "ITB_CODEX_APPROVAL_POLICY": "",
                },
                clear=False,
            ):
                command, runtime_kind = builder.build_agent_command(
                    row,
                    "/Users/takagiyasushi/skills-repo",
                    "provider_cli",
            )
            codex_state_dir = root / "state" / "test-session" / "provider-state" / "tech-backend" / "codex"
            codex_config_dir = root / "state" / "test-session" / "provider-config" / "tech-backend" / "codex"
            self.assertTrue(codex_state_dir.exists())
            memory_policy = json.loads((codex_state_dir / "memory-policy.json").read_text(encoding="utf-8"))
            codex_memory_files_exist = (codex_state_dir / "CLAUDE.md").exists() and (codex_state_dir / "AGENTS.md").exists()

        self.assertEqual(runtime_kind, "codex_tmux")
        self.assertIn("--model gpt-5.5", command)
        self.assertIn(f"--cd {codex_state_dir}", command)
        self.assertIn("--ask-for-approval never", command)
        self.assertIn("--sandbox workspace-write", command)
        self.assertIn("model_reasoning_effort=", command)
        self.assertIn("xhigh", command)
        self.assertIn("service_tier=", command)
        self.assertIn("fast", command)
        self.assertIn("--add-dir /Users/takagiyasushi/skills-repo", command)
        self.assertIn(f"--add-dir {vault.resolve()}", command)
        self.assertIn("ITB_CODEX_APPROVAL_POLICY_EFFECTIVE=never", command)
        self.assertIn("ITB_CODEX_MODEL_EFFECTIVE=gpt-5.5", command)
        self.assertIn("ITB_CODEX_REASONING_EFFORT_EFFECTIVE=xhigh", command)
        self.assertIn("ITB_CODEX_SERVICE_TIER_EFFECTIVE=fast", command)
        self.assertIn(f"ITB_PROVIDER_STATE_DIR={codex_state_dir}", command)
        self.assertIn("ITB_PROVIDER_CONFIG_DIR=''", command)
        self.assertNotIn("CODEX_HOME=", command)
        self.assertFalse(codex_config_dir.exists())
        self.assertIn("ITB_PROVIDER_AUTH_POLICY=provider_default_auth", command)
        self.assertIn("ITB_PROVIDER_MEMORY_POLICY=isolated_session_config", command)
        self.assertEqual(memory_policy["policy"], "isolated_session_config")
        self.assertEqual(memory_policy["auth_policy"], "provider_default_auth")
        self.assertEqual(memory_policy["auth_seed"]["status"], "not_applicable")
        self.assertEqual(memory_policy["workspace_cwd"], "/Users/takagiyasushi/skills-repo")
        self.assertEqual(memory_policy["launch_cwd"], str(codex_state_dir))
        self.assertTrue(codex_memory_files_exist)

    def test_build_agent_command_blocks_interactive_codex_resident_by_default(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "tech-backend",
            "agent_instance_id": "tech-backend@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "openai",
            "intended_model": "gpt-5.5",
            "execution_mode": "codex",
        }

        with mock.patch.dict(os.environ, {"ITB_ALLOW_INTERACTIVE_CODEX_RESIDENT": ""}, clear=False):
            with self.assertRaisesRegex(ValueError, "codex exec provider adapter"):
                builder.build_agent_command(
                    row,
                    "/Users/takagiyasushi/skills-repo",
                    "provider_cli",
                )

    def test_build_agent_command_does_not_start_role_agent_worker_for_resident_shell(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
            "runtime": "codex",
            "state_root": "/tmp/itb-state",
        }

        command, runtime_kind = builder.build_agent_command(row, "/tmp/project", "resident_shell")

        self.assertEqual(runtime_kind, "resident_shell_tmux")
        self.assertNotIn("role-agent-worker", command)
        self.assertNotIn("--max-messages 0", command)
        self.assertIn("ITB_AGENT_ID=gate-prompt-formatter", command)
        self.assertIn("ITB_STATE_ROOT=/tmp/itb-state", command)
        self.assertIn("sleep 3600", command)

    def test_agent_dispatch_payload_does_not_contain_exact_done_marker(self) -> None:
        builder = load_builder_module()
        payload = builder.build_agent_dispatch_payload(
            runtime="codex",
            request_id="req123",
            agent_id="gate-prompt-formatter",
            source_agent="codex-main",
            cwd="/Users/takagiyasushi/skills-repo",
            dispatch_manifest_ref="dispatch/agent-dispatch/gate-prompt-formatter/req123/manifest.yaml",
            dispatch_manifest_path="/tmp/queue/dispatch/agent-dispatch/gate-prompt-formatter/req123/manifest.yaml",
            instruction_ref="dispatch/agent-dispatch/gate-prompt-formatter/req123/instruction.yaml",
            instruction_path="/tmp/queue/dispatch/agent-dispatch/gate-prompt-formatter/req123/instruction.yaml",
            report_ref="reports/agent-dispatch/gate-prompt-formatter/req123.yaml",
            atomic_report_writer="python3 /tmp/itb_bootstrap_builder.py agent-dispatch-report --runtime codex",
        )

        self.assertIn("Marker name: ITB_AGENT_RESPONSE_DONE", payload)
        self.assertIn("Marker id: req123", payload)
        self.assertIn("routed from Codex", payload)
        self.assertIn("dispatch_manifest_ref:", payload)
        self.assertIn("instruction_ref:", payload)
        self.assertIn("report_ref:", payload)
        self.assertIn("atomic_report_writer:", payload)
        self.assertIn("Use Bash only as the transport finalizer tool", payload)
        self.assertIn("Finalize by running atomic_report_writer as the first command token with --report-json", payload)
        self.assertIn("--report-json", payload)
        self.assertIn("result: provider_response_ready", payload)
        self.assertNotIn("[ITB_AGENT_RESPONSE_DONE id=req123]", payload)
        self.assertNotIn("Return ok.", payload)

    def test_agent_dispatch_payload_uses_runtime_label(self) -> None:
        builder = load_builder_module()
        payload = builder.build_agent_dispatch_payload(
            runtime="claude",
            request_id="req123",
            agent_id="gate-prompt-formatter",
            source_agent="claude-main",
            cwd="/Users/takagiyasushi/skills-repo",
            dispatch_manifest_ref="dispatch/agent-dispatch/gate-prompt-formatter/req123/manifest.yaml",
            instruction_ref="dispatch/agent-dispatch/gate-prompt-formatter/req123/instruction.yaml",
            report_ref="reports/agent-dispatch/gate-prompt-formatter/req123.yaml",
        )

        self.assertIn("routed from Claude", payload)
        self.assertIn("do not treat Claude's main model", payload)
        self.assertNotIn("routed from Codex", payload)

    def test_extract_agent_dispatch_response_strips_echoed_prompt_payload(self) -> None:
        builder = load_builder_module()
        captured = """
[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]
Default submit-count verification only.
Stay inside your role boundary.
When finished, end your response with one bracketed marker line.
Marker name: ITB_AGENT_RESPONSE_DONE
Marker id: req123
Marker format: [MARKER_NAME id=MARKER_ID]

⏺ Gate dispatch submit default works.
[ITB_AGENT_RESPONSE_DONE id=req123]
"""

        response = builder.extract_agent_dispatch_response(captured, "req123")

        self.assertEqual(response, "Gate dispatch submit default works.")

    def test_wait_for_interactive_prompt_detects_claude_prompt(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(builder, "tmux_capture_pane", return_value=("ready ❯", "")):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(ready)
        self.assertEqual(error, "")

    def test_wait_for_interactive_prompt_detects_codex_prompt(self) -> None:
        builder = load_builder_module()
        codex_tail = "  gpt-5.5 xhigh fast · ~/skills-repo\n› "
        with mock.patch.object(builder, "tmux_capture_pane", return_value=(codex_tail, "")):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:tech-performance.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(ready)
        self.assertEqual(error, "")

    def test_wait_for_interactive_prompt_blocks_codex_approval(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=("Allow Codex to run `rm -rf`?", ""),
        ):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:tech-performance.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("approval", error)

    def test_wait_for_interactive_prompt_blocks_claude_bash_permission_prompt(self) -> None:
        builder = load_builder_module()
        captured = """
  The command to approve is:
  python3 /tmp/itb_bootstrap_builder.py role-report --runtime codex
────────────────────────────────────────────────────── gate-prompt-formatter ──
❯ allow the bash command and retry
"""
        with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("approval", error)
        self.assertFalse(builder.captured_tail_has_idle_composer(captured))

    def test_extract_agent_dispatch_response_handles_codex_bullet(self) -> None:
        builder = load_builder_module()
        captured = (
            "[ITB_AGENT_REQUEST id=abc123 agent=tech-performance from=tech-director]\n"
            "Marker format: [MARKER_NAME id=MARKER_ID]\n"
            "› (echoed request)\n"
            "• role output line one\n"
            "• [ITB_AGENT_RESPONSE_DONE id=abc123]\n"
            "[ITB_AGENT_RESPONSE_DONE id=abc123]\n"
        )
        response = builder.extract_agent_dispatch_response(captured, "abc123")
        self.assertIn("role output line one", response)
        self.assertNotIn("echoed request", response)

    def test_wait_for_interactive_prompt_blocks_trust_prompt(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=("Quick safety check: Is this a project you trust?", ""),
        ):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("workspace trust", error)

    def test_startup_interactive_readiness_marks_gate_trust_blocker(self) -> None:
        builder = load_builder_module()
        row = {"agent_id": "gate-prompt-formatter"}
        result = {
            "agent_id": "gate-prompt-formatter",
            "process_status": "process_ready",
            "launch_status": "created_session",
            "process_mode": "provider_cli",
            "provider_status": "provider_process_ready",
            "tmux_target": "itb-org-test:gate-prompt-formatter.0",
        }

        with mock.patch.object(
            builder,
            "wait_for_interactive_prompt",
            return_value=(False, "Claude workspace trust prompt is waiting for human approval"),
        ) as wait_ready:
            annotated = builder.annotate_startup_interactive_readiness(
                row,
                result,
                dry_run=False,
                now="2026-01-01T00:00:00+09:00",
            )

        self.assertIs(annotated, result)
        self.assertEqual(result["interactive_status"], "blocked_trust_prompt")
        self.assertFalse(result["interactive_ready"])
        self.assertTrue(result["interactive_startup_target"])
        self.assertEqual(result["interactive_evidence_source"], "tmux_capture")
        wait_ready.assert_called_once()

    def test_interactive_readiness_counts_include_queue_activation_prompt_probe(self) -> None:
        builder = load_builder_module()
        roster = [
            {
                "agent_id": "gate-task-evaluator",
                "process_status": "process_ready",
                "process_mode": "provider_cli",
                "provider_status": "provider_process_ready",
                "interactive_ready": False,
                "interactive_status": "prompt_busy",
                "interactive_error": "provider is busy",
                "interactive_evidence_source": "queue_activation_prompt_probe",
                "tmux_target": "itb-org-test:gate-task-evaluator.0",
            }
        ]

        counts = builder.interactive_readiness_counts(roster)

        self.assertEqual(counts["interactive_readiness_scope"], "interactive_blocked")
        self.assertEqual(counts["prompt_readiness_scope"], "interactive_blocked")
        self.assertEqual(counts["resident_agents_prompt_ready"], 0)
        self.assertEqual(counts["interactive_checked_count"], 1)
        self.assertEqual(counts["interactive_blocker_count"], 1)
        self.assertEqual(counts["startup_interactive_blockers"][0]["agent_id"], "gate-task-evaluator")

    def test_interactive_readiness_followup_updates_non_startup_residents(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "follow-session"
            session_dir.mkdir()
            state = {
                "session_id": "follow-session",
                "organization_instance_id": "org-follow",
                "interactive_readiness_scope": "interactive_partial",
                "resident_agents_interactive_ready": 1,
            }
            roster = [
                {
                    "agent_id": "gate-prompt-formatter",
                    "process_status": "process_ready",
                    "process_mode": "provider_cli",
                    "provider_status": "provider_process_ready",
                    "interactive_ready": True,
                    "interactive_status": "interactive_ready",
                    "interactive_evidence_source": "tmux_capture",
                    "tmux_target": "itb-org-follow:gate-prompt-formatter.0",
                },
                {
                    "agent_id": "gate-task-creator",
                    "process_status": "process_ready",
                    "process_mode": "provider_cli",
                    "provider_status": "provider_process_ready",
                    "interactive_ready": False,
                    "interactive_status": "not_checked_startup_scope",
                    "interactive_evidence_source": "startup_scope",
                    "tmux_target": "itb-org-follow:gate-task-creator.0",
                },
                {
                    "agent_id": "teams-project-manager",
                    "process_status": "process_ready",
                    "process_mode": "provider_cli",
                    "provider_status": "provider_process_ready",
                    "interactive_ready": False,
                    "interactive_status": "not_checked_startup_scope",
                    "interactive_evidence_source": "startup_scope",
                    "tmux_target": "itb-org-follow:teams-project-manager.0",
                },
            ]
            (session_dir / "bootstrap.json").write_text(json.dumps(state), encoding="utf-8")
            (session_dir / "roster.json").write_text(json.dumps(roster), encoding="utf-8")

            wait_calls = 0

            def fake_wait_for_interactive_prompt(**_: object) -> tuple[bool, str]:
                nonlocal wait_calls
                wait_calls += 1
                if wait_calls == 1:
                    latest_roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                    for latest_row in latest_roster:
                        if latest_row["agent_id"] == "gate-prompt-formatter":
                            latest_row["last_request_id"] = "concurrent-req"
                    (session_dir / "roster.json").write_text(json.dumps(latest_roster), encoding="utf-8")
                    return True, ""
                return False, "Claude workspace trust prompt is waiting for human approval"

            with mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                side_effect=fake_wait_for_interactive_prompt,
            ) as wait_ready, mock.patch.object(builder, "render_report", return_value="# report\n"):
                output = builder.interactive_readiness_followup(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "follow-session"},
                )

            updated_state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            updated_roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))

        self.assertTrue(output["interactiveReadinessFollowup"]["state_updated"])
        self.assertEqual(output["interactiveReadinessFollowup"]["target_count"], 2)
        self.assertEqual(updated_state["interactive_readiness_scope"], "interactive_blocked")
        self.assertEqual(updated_state["resident_agents_interactive_ready"], 2)
        self.assertEqual(updated_state["prompt_readiness_scope"], "interactive_blocked")
        self.assertEqual(updated_state["resident_agents_prompt_ready"], 2)
        self.assertEqual(updated_state["interactive_readiness_target_count"], 3)
        self.assertEqual(updated_state["interactive_checked_count"], 3)
        self.assertEqual(updated_state["interactive_blocker_count"], 1)
        self.assertEqual(updated_state["interactive_followup_target_count"], 2)
        rows = {row["agent_id"]: row for row in updated_roster}
        self.assertEqual(rows["gate-task-creator"]["interactive_status"], "interactive_ready")
        self.assertEqual(rows["gate-task-creator"]["interactive_evidence_source"], "tmux_capture_followup")
        self.assertEqual(rows["teams-project-manager"]["interactive_status"], "blocked_trust_prompt")
        self.assertEqual(rows["gate-prompt-formatter"]["last_request_id"], "concurrent-req")
        self.assertEqual(wait_ready.call_count, 2)

    def test_start_interactive_readiness_followup_autostarts_detached_command(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "follow-session"
            session_dir.mkdir()
            launch_summary = {
                "dry_run": False,
                "child_context": False,
                "process_mode": "provider_cli",
                "results": [
                    {
                        "agent_id": "gate-prompt-formatter",
                        "process_status": "process_ready",
                        "process_mode": "provider_cli",
                        "provider_status": "provider_process_ready",
                        "interactive_evidence_source": "tmux_capture",
                        "tmux_target": "itb-org-follow:gate-prompt-formatter.0",
                    },
                    {
                        "agent_id": "gate-task-creator",
                        "process_status": "process_ready",
                        "process_mode": "provider_cli",
                        "provider_status": "provider_process_ready",
                        "interactive_evidence_source": "startup_scope",
                        "tmux_target": "itb-org-follow:gate-task-creator.0",
                    },
                ],
            }
            proc = mock.Mock(pid=2468)
            with mock.patch.object(builder, "process_is_running", return_value=False), mock.patch.object(
                builder.subprocess,
                "Popen",
                return_value=proc,
            ) as popen:
                result = builder.start_interactive_readiness_followup_if_needed(
                    runtime="codex",
                    state_root=state_root,
                    session_dir=session_dir,
                    session_id="follow-session",
                    launch_summary=launch_summary,
                )

            pid_text = (session_dir / "interactive-readiness-followup.pid").read_text(encoding="utf-8")

        self.assertEqual(result["result"], "started")
        self.assertEqual(result["pid"], 2468)
        self.assertEqual(result["target_count"], 1)
        self.assertEqual(pid_text.strip(), "2468")
        command = popen.call_args.args[0]
        self.assertIn("interactive-readiness-followup", command)
        self.assertIn("--session-id", command)
        self.assertIn("follow-session", command)

    def test_wait_for_interactive_prompt_blocks_claude_onboarding(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=("Choose the text style that looks best with your terminal\n❯ 2. Dark mode", ""),
        ):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("provider_onboarding_blocked", error)

    def test_wait_for_interactive_prompt_blocks_claude_login(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=("Select login method:\n❯ 1. Claude account with subscription", ""),
        ):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("Claude login prompt", error)

    def test_wait_for_interactive_prompt_ignores_historical_claude_onboarding(self) -> None:
        builder = load_builder_module()
        old_history = "Choose the text style that looks best with your terminal\n" + "\n".join(
            f"history line {index}" for index in range(20)
        )
        current_prompt = old_history + "\nWelcome back\n❯ "
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=(current_prompt, ""),
        ):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(ready)
        self.assertEqual(error, "")

    def test_wait_for_interactive_prompt_ignores_historical_claude_trust_prompt(self) -> None:
        builder = load_builder_module()
        old_history = "Quick safety check: Is this a project you trust?\n" + "\n".join(
            f"history line {index}" for index in range(20)
        )
        current_prompt = old_history + "\nWelcome back\n❯ "
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=(current_prompt, ""),
        ):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(ready)
        self.assertEqual(error, "")

    def test_wait_for_interactive_prompt_ignores_historical_prompt(self) -> None:
        builder = load_builder_module()
        old_history = "old prompt ❯\n" + "\n".join(f"line {index}" for index in range(20))
        with mock.patch.object(builder, "tmux_capture_pane", return_value=(old_history, "")):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=0.02,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("timed out", error)

    def test_wait_for_interactive_prompt_does_not_treat_busy_tail_as_ready(self) -> None:
        builder = load_builder_module()
        busy_tail = "Working\nEsc to interrupt\n› "
        with mock.patch.object(builder, "tmux_capture_pane", return_value=(busy_tail, "")):
            ready, error = builder.wait_for_interactive_prompt(
                target="itb-org-test:gate-prompt-formatter.0",
                timeout_seconds=0.02,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ready)
        self.assertIn("busy", error)

    def test_tmux_send_payload_loads_stdin_buffer_and_uses_bracketed_paste(self) -> None:
        builder = load_builder_module()
        run_tmux_calls = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            run_tmux_calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        payload = "first line\nsecond line with spaces"
        with mock.patch.object(
            builder.uuid,
            "uuid4",
            return_value=mock.Mock(hex="abc123def4567890"),
        ), mock.patch.object(
            builder,
            "run_tmux_with_input",
            return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
        ) as load_buffer, mock.patch.object(
            builder,
            "run_tmux",
            side_effect=fake_run_tmux,
        ):
            ok, error = builder.tmux_send_payload(
                "itb-org-test:gate-prompt-formatter.0",
                payload,
                submit_enter_count=1,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        load_buffer.assert_called_once_with(
            ["load-buffer", "-b", "itb-agent-abc123def456", "-"],
            payload,
            timeout=5.0,
        )
        self.assertIn(
            [
                "paste-buffer",
                "-p",
                "-b",
                "itb-agent-abc123def456",
                "-t",
                "itb-org-test:gate-prompt-formatter.0",
            ],
            run_tmux_calls,
        )
        self.assertIn(["delete-buffer", "-b", "itb-agent-abc123def456"], run_tmux_calls)
        self.assertIn(["send-keys", "-t", "itb-org-test:gate-prompt-formatter.0", "Escape"], run_tmux_calls)
        self.assertIn(["send-keys", "-t", "itb-org-test:gate-prompt-formatter.0", "C-u"], run_tmux_calls)
        self.assertTrue(all(payload not in arg for call in run_tmux_calls for arg in call))

    def test_tmux_send_payload_refuses_copy_mode_before_loading_payload(self) -> None:
        builder = load_builder_module()

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            if args[:3] == ["display-message", "-p", "-t"]:
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="1\n", stderr="")
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(
            builder,
            "run_tmux",
            side_effect=fake_run_tmux,
        ), mock.patch.object(builder, "run_tmux_with_input") as load_buffer:
            ok, error = builder.tmux_send_payload(
                "itb-org-test:gate-prompt-formatter.0",
                "payload that must not be loaded",
                submit_enter_count=1,
            )

        self.assertFalse(ok)
        self.assertIn("copy-mode", error)
        load_buffer.assert_not_called()

    def test_wait_for_tmux_payload_ack_ignores_composer_marker(self) -> None:
        builder = load_builder_module()
        captured = "› [ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\n"

        with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")):
            ok, error = builder.wait_for_tmux_payload_ack(
                target="itb-org-test:gate-prompt-formatter.0",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=0.02,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(ok)
        self.assertIn("timed out waiting for submitted payload marker", error)
        self.assertTrue(builder.captured_tail_has_unsubmitted_payload(captured, ["[ITB_AGENT_REQUEST id=req123"]))

    def test_wait_for_tmux_payload_ack_accepts_transcript_marker(self) -> None:
        builder = load_builder_module()
        captured = "[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\nWorking\n"

        with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")):
            ok, error = builder.wait_for_tmux_payload_ack(
                target="itb-org-test:gate-prompt-formatter.0",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertFalse(builder.captured_tail_has_unsubmitted_payload(captured, ["[ITB_AGENT_REQUEST id=req123"]))

    def test_wait_for_tmux_payload_ack_accepts_claude_pasted_placeholder(self) -> None:
        builder = load_builder_module()
        captured = "[Pasted text #1 +42 lines]\nThinking\nEsc to interrupt\n"

        with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")):
            ok, error = builder.wait_for_tmux_payload_ack(
                target="itb-org-test:gate-prompt-formatter.0",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")

    def test_wait_for_agent_marker_fast_fails_when_provider_returns_idle(self) -> None:
        builder = load_builder_module()
        captured = (
            "[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\n"
            "partial work without done marker\n"
            "Welcome back\n"
            "❯ "
        )

        with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")):
            completed, seen, error = builder.wait_for_agent_marker(
                target="itb-org-test:gate-prompt-formatter.0",
                request_id="req123",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
                history_lines=240,
                idle_fast_fail_seconds=0,
                idle_fast_fail_polls=2,
            )

        self.assertFalse(completed)
        self.assertEqual(seen, captured)
        self.assertIn("provider returned to idle prompt", error)

    def test_wait_for_agent_marker_does_not_fast_fail_while_provider_busy(self) -> None:
        builder = load_builder_module()
        captures = [
            "Working\nEsc to interrupt\n› ",
            "role output\n[ITB_AGENT_RESPONSE_DONE id=req123]\n",
        ]

        with mock.patch.object(builder, "tmux_capture_pane", side_effect=[(item, "") for item in captures]):
            completed, seen, error = builder.wait_for_agent_marker(
                target="itb-org-test:gate-prompt-formatter.0",
                request_id="req123",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
                history_lines=240,
                idle_fast_fail_seconds=0,
                idle_fast_fail_polls=1,
            )

        self.assertTrue(completed)
        self.assertEqual(seen, captures[-1])
        self.assertEqual(error, "")

    def test_wait_for_agent_completion_fast_fails_when_provider_returns_idle(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "missing-report.yaml"
            captured = (
                "[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\n"
                "partial work without done marker\n"
                "Welcome back\n"
                "❯ "
            )
            with mock.patch.object(builder, "tmux_capture_pane", return_value=(captured, "")), mock.patch.dict(
                os.environ,
                {
                    "ITB_AGENT_DISPATCH_IDLE_FAST_FAIL_SECONDS": "0",
                    "ITB_AGENT_DISPATCH_IDLE_FAST_FAIL_POLLS": "2",
                },
                clear=False,
            ):
                completed, seen, error, source = builder.wait_for_agent_completion(
                    target="itb-org-test:gate-prompt-formatter.0",
                    request_id="req123",
                    report_path=report_path,
                    timeout_seconds=10,
                    poll_interval_seconds=0.01,
                    history_lines=240,
                )

        self.assertFalse(completed)
        self.assertEqual(seen, captured)
        self.assertIn("provider returned to idle prompt", error)
        self.assertEqual(source, "idle_fast_fail")

    def test_agent_dispatch_default_timeout_uses_micro_low_risk_cap(self) -> None:
        builder = load_builder_module()

        self.assertEqual(builder.agent_dispatch_default_timeout_seconds({"workflow_mode": "micro"}), 120.0)
        self.assertEqual(builder.agent_dispatch_default_timeout_seconds({"risk_tier": "low"}), 120.0)
        self.assertEqual(builder.agent_dispatch_default_timeout_seconds({"risk_tier": "normal"}), 600.0)

    def test_recover_unconfirmed_tmux_payload_send_succeeds_after_extra_enter(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(
            builder,
            "run_tmux",
            return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
        ) as run_tmux, mock.patch.object(
            builder,
            "wait_for_tmux_payload_ack",
            return_value=(True, ""),
        ) as wait_for_ack, mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=("", ""),
        ):
            ok, error, attempts = builder.recover_unconfirmed_tmux_payload_send(
                target="itb-org-test:gate-prompt-formatter.0",
                payload="payload",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                history_lines=240,
                submit_enter_count=1,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(attempts[0]["action"], "extra_enter")
        self.assertEqual(attempts[1]["result"], "acknowledged")
        run_tmux.assert_called_once_with(["send-keys", "-t", "itb-org-test:gate-prompt-formatter.0", "Enter"], timeout=5.0)
        wait_for_ack.assert_called_once()

    def test_recover_unconfirmed_tmux_payload_send_repastes_when_payload_remains(self) -> None:
        builder = load_builder_module()
        captured = "› [ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\n"
        with mock.patch.object(
            builder,
            "run_tmux",
            return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
        ), mock.patch.object(
            builder,
            "wait_for_tmux_payload_ack",
            side_effect=[(False, "missing marker"), (True, "")],
        ), mock.patch.object(
            builder,
            "tmux_capture_pane",
            side_effect=[("", ""), (captured, "")],
        ), mock.patch.object(
            builder,
            "tmux_send_payload",
            return_value=(True, ""),
        ) as send_payload:
            ok, error, attempts = builder.recover_unconfirmed_tmux_payload_send(
                target="itb-org-test:gate-prompt-formatter.0",
                payload="payload",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                history_lines=240,
                submit_enter_count=2,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual([item["action"] for item in attempts], [
            "extra_enter",
            "ack_after_extra_enter",
            "residual_payload_check",
            "clear_and_repaste",
            "ack_after_repaste",
        ])
        send_payload.assert_called_once_with(
            "itb-org-test:gate-prompt-formatter.0",
            "payload",
            submit_enter_count=2,
        )

    def test_recover_unconfirmed_tmux_payload_send_skips_when_marker_already_submitted(self) -> None:
        builder = load_builder_module()
        captured = "[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\nWorking\n"
        with mock.patch.object(
            builder,
            "tmux_capture_pane",
            return_value=(captured, ""),
        ), mock.patch.object(builder, "run_tmux") as run_tmux, mock.patch.object(
            builder,
            "tmux_send_payload",
        ) as send_payload:
            ok, error, attempts = builder.recover_unconfirmed_tmux_payload_send(
                target="itb-org-test:gate-prompt-formatter.0",
                payload="payload",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                history_lines=240,
                submit_enter_count=1,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(attempts, [{"action": "submitted_marker_pre_recovery_check", "result": "already_submitted", "error": ""}])
        run_tmux.assert_not_called()
        send_payload.assert_not_called()

    def test_recover_unconfirmed_tmux_payload_send_skips_repaste_when_marker_submitted_after_enter(self) -> None:
        builder = load_builder_module()
        captured = "[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\nWorking\n"
        with mock.patch.object(
            builder,
            "run_tmux",
            return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
        ), mock.patch.object(
            builder,
            "wait_for_tmux_payload_ack",
            return_value=(False, "missing marker"),
        ), mock.patch.object(
            builder,
            "tmux_capture_pane",
            side_effect=[("", ""), (captured, "")],
        ), mock.patch.object(
            builder,
            "tmux_send_payload",
        ) as send_payload:
            ok, error, attempts = builder.recover_unconfirmed_tmux_payload_send(
                target="itb-org-test:gate-prompt-formatter.0",
                payload="payload",
                markers=["[ITB_AGENT_REQUEST id=req123"],
                timeout_seconds=1,
                poll_interval_seconds=0.1,
                history_lines=240,
                submit_enter_count=1,
            )

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(attempts[-1]["action"], "submitted_marker_before_repaste_check")
        send_payload.assert_not_called()

    def test_ensure_provider_cli_blocks_interactive_codex_resident_by_default(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "tech-backend",
            "agent_instance_id": "tech-backend@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "openai",
            "intended_model": "gpt-5.5",
            "execution_mode": "codex",
        }

        result, error = builder.ensure_provider_cli_for_agent(
            row=row,
            state={"organization_instance_id": "org-test", "tmux_session": "itb-org-test"},
            cwd="/Users/takagiyasushi/skills-repo",
            now="2026-01-01T00:00:00+09:00",
        )

        self.assertIsNone(result)
        self.assertIn("codex exec provider adapter", error)

    def test_agent_dispatch_records_tmux_response_evidence(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "abc123def456"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "force_respawned",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }
            captured = (
                f"[ITB_AGENT_REQUEST id={request_id} agent=gate-prompt-formatter]\n"
                "role output\n"
                f"[ITB_AGENT_RESPONSE_DONE id={request_id}]\n"
            )

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ) as wait_for_prompt, mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload, mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ) as wait_for_ack, mock.patch.object(
                builder,
                "wait_for_agent_completion",
                return_value=(True, captured, "", "pane_marker"),
            ) as wait_for_completion, mock.patch.object(
                builder,
                "run_tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
            ) as run_tmux:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                        "submit_enter_count": 99,
                        "prompt_ready_timeout_seconds": 999,
                        "timeout_seconds": 99999,
                        "poll_interval_seconds": -5,
                        "history_lines": 999999,
                    },
                )

            dispatch = output["agentDispatch"]
            self.assertEqual(dispatch["request_id"], request_id)
            self.assertEqual(dispatch["result"], "provider_response_ready")
            self.assertEqual(dispatch["target"], target)
            self.assertIn("started_at", dispatch)
            self.assertIn("completed_at", dispatch)
            self.assertIsInstance(dispatch["duration_sec"], float)
            self.assertEqual(dispatch["completion_source"], "pane_marker")
            self.assertEqual(dispatch["report_schema_status"], "valid")
            dispatch_report = json.loads(Path(dispatch["dispatch_report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(dispatch_report["report_type"], "agent_dispatch_transport_result")
            self.assertEqual(dispatch_report["result"], "provider_response_ready")
            self.assertEqual(dispatch_report["status"], "done")
            self.assertEqual(dispatch_report["request_id"], request_id)
            self.assertEqual(dispatch_report["response"], "role output")
            self.assertEqual(dispatch["response"], dispatch_report["response"])
            self.assertEqual(dispatch["response_source"], "dispatch_report_file")
            self.assertEqual(dispatch["dispatch_report_integrity"]["sha256"], hashlib.sha256(Path(dispatch["dispatch_report_path"]).read_bytes()).hexdigest())
            self.assertEqual(dispatch_report["queue"]["report_path"], dispatch["dispatch_report_ref"])
            self.assertEqual(dispatch_report["queue"]["instruction_ref"], dispatch["instruction_ref"])
            self.assertEqual(dispatch_report["queue"]["dispatch_manifest_ref"], dispatch["dispatch_manifest_ref"])
            instruction = json.loads(Path(dispatch["instruction_path"]).read_text(encoding="utf-8"))
            manifest = json.loads(Path(dispatch["dispatch_manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(instruction["instruction"], "Return a Gate Intake Envelope.")
            self.assertEqual(manifest["instruction_ref"], dispatch["instruction_ref"])
            self.assertEqual(manifest["report_ref"], dispatch["dispatch_report_ref"])
            self.assertIn("agent-dispatch-report --runtime codex", manifest["atomic_report_writer"])
            self.assertEqual(manifest["transport_finalizer"], "agent-dispatch-report")
            self.assertIn("agent_id", manifest["required_report_fields"])
            self.assertIn("request_id", manifest["required_report_fields"])
            self.assertIn("provider_evidence", manifest["required_report_fields"])
            self.assertIn("provider_session_id", manifest["provider_evidence_fields"])
            self.assertIn("provider_response_ready", manifest["allowed_results"])
            self.assertTrue(any("atomic_report_writer" in note for note in manifest["notes"]))
            self.assertTrue(any("--report-json" in note for note in manifest["notes"]))
            self.assertTrue(any("Do not use stdin" in note for note in manifest["notes"]))
            self.assertFalse(any("Pass one JSON object on stdin" in note for note in manifest["notes"]))
            sent_payload = send_payload.call_args.args[1]
            self.assertIn("Marker name: ITB_AGENT_RESPONSE_DONE", sent_payload)
            self.assertIn("dispatch_manifest_ref:", sent_payload)
            self.assertIn("instruction_ref:", sent_payload)
            self.assertIn("report_ref:", sent_payload)
            self.assertIn("atomic_report_writer:", sent_payload)
            self.assertIn("Finalize by running atomic_report_writer as the first command token with --report-json", sent_payload)
            self.assertIn("--report-json", sent_payload)
            self.assertIn("result: provider_response_ready", sent_payload)
            self.assertNotIn("Return a Gate Intake Envelope.", sent_payload)
            self.assertNotIn(f"[ITB_AGENT_RESPONSE_DONE id={request_id}]", sent_payload)
            self.assertEqual(send_payload.call_args.kwargs["submit_enter_count"], 3)
            self.assertIn(f"[ITB_AGENT_REQUEST id={request_id}", wait_for_ack.call_args.kwargs["markers"])
            self.assertEqual(wait_for_ack.call_args.kwargs["timeout_seconds"], 3)
            self.assertEqual(wait_for_prompt.call_args.kwargs["timeout_seconds"], 120)
            self.assertEqual(wait_for_completion.call_args.kwargs["timeout_seconds"], 900)
            self.assertEqual(wait_for_completion.call_args.kwargs["poll_interval_seconds"], 0.25)
            self.assertEqual(wait_for_completion.call_args.kwargs["history_lines"], 12000)
            run_tmux.assert_any_call(["clear-history", "-t", target], timeout=2.0)

            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            row = next(item for item in roster if item["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(row["response_status"], "invoked")
            self.assertEqual(row["provider_status"], "provider_response_ready")
            self.assertEqual(row["session_id"], target)
            self.assertEqual(row["last_request_id"], request_id)

            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    entry["event_type"] == "agent_dispatch"
                    and entry["agent_id"] == "gate-prompt-formatter"
                    and entry["result"] == "provider_response_ready"
                    and entry["provider_session_id"] == target
                    and "duration_sec" in entry
                    and "started_at" in entry
                    and "completed_at" in entry
                    and entry["dispatch_report_path"] == dispatch["dispatch_report_path"]
                    and entry["dispatch_report_integrity"]["sha256"] == dispatch["dispatch_report_integrity"]["sha256"]
                    for entry in evidence
                )
            )
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            dispatch_metrics = [
                item
                for item in metrics
                if item["event_type"] == "agent_dispatch" and item["request_id"] == request_id
            ]
            self.assertEqual(len(dispatch_metrics), 1)
            self.assertEqual(dispatch_metrics[0]["result"], "provider_response_ready")
            self.assertEqual(dispatch_metrics[0]["agent_id"], "gate-prompt-formatter")
            self.assertIn("started_at", dispatch_metrics[0])
            self.assertIn("completed_at", dispatch_metrics[0])
            self.assertIsInstance(dispatch_metrics[0]["duration_sec"], float)

    def test_agent_dispatch_prefers_provider_written_report_file(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-file-report-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "file123abc45"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
            }

            def write_provider_report(**kwargs):
                report_path = kwargs["report_path"]
                queue_root = report_path.parents[3]
                builder.write_agent_dispatch_report(
                    queue_root=queue_root,
                    agent_id="gate-prompt-formatter",
                    request_id=request_id,
                    runtime="codex",
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent="codex-main",
                    target=target,
                    result="provider_response_ready",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at="2026-01-01T00:00:00+09:00",
                    completed_at="2026-01-01T00:00:03+09:00",
                    duration_seconds=3.0,
                    response="file-backed role output",
                    wait_error="",
                    captured="",
                    launch_result=launch_result,
                    wait_enabled=True,
                    instruction_ref="dispatch/agent-dispatch/gate-prompt-formatter/file123abc45/instruction.yaml",
                    dispatch_manifest_ref="dispatch/agent-dispatch/gate-prompt-formatter/file123abc45/manifest.yaml",
                    input_tokens=31,
                    output_tokens=12,
                    duration_api_ms=456,
                    turn_duration_ms=7890,
                    num_turns=1,
                )
                self.assertTrue(report_path.exists())
                return True, "", "", "dispatch_report_file"

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_agent_completion",
                side_effect=write_provider_report,
            ) as wait_for_completion:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                    },
                )

            dispatch = output["agentDispatch"]
            self.assertEqual(dispatch["result"], "provider_response_ready")
            self.assertEqual(dispatch["completion_source"], "dispatch_report_file")
            self.assertEqual(dispatch["response"], "file-backed role output")
            self.assertEqual(dispatch["response_source"], "dispatch_report_file")
            self.assertEqual(dispatch["input_tokens"], 31)
            self.assertEqual(dispatch["output_tokens"], 12)
            self.assertEqual(dispatch["total_tokens"], 43)
            self.assertEqual(dispatch["duration_api_ms"], 456)
            self.assertEqual(dispatch["turn_duration_ms"], 7890)
            self.assertEqual(dispatch["num_turns"], 1)
            self.assertEqual(wait_for_completion.call_args.kwargs["report_path"], Path(dispatch["dispatch_report_path"]))
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            dispatch_metric = [
                item
                for item in metrics
                if item["event_type"] == "agent_dispatch" and item["request_id"] == request_id
            ][-1]
            self.assertEqual(dispatch_metric["input_tokens"], 31)
            self.assertEqual(dispatch_metric["output_tokens"], 12)
            self.assertEqual(dispatch_metric["total_tokens"], 43)
            self.assertEqual(dispatch_metric["duration_api_ms"], 456)
            self.assertEqual(dispatch_metric["turn_duration_ms"], 7890)
            self.assertEqual(dispatch_metric["completion_source"], "dispatch_report_file")

    def test_agent_dispatch_report_command_writes_valid_transport_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="dispatch-report-command-session")

            output = self.run_builder(
                state_root,
                "agent-dispatch-report",
                {
                    "session_id": "dispatch-report-command-session",
                    "agent_id": "gate-prompt-formatter",
                    "request_id": "writer123abc",
                    "source_agent": "codex-main",
                    "target": "itb-org-test:gate-prompt-formatter.0",
                    "result": "provider_response_ready",
                    "response": "writer command response",
                    "started_at": "2026-01-01T00:00:00+09:00",
                    "completed_at": "2026-01-01T00:00:05+09:00",
                    "provider_evidence": {
                        "usage_source": "claude_tmux_interactive",
                        "effective_model": "claude-sonnet-4-6",
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "input_tokens": 22,
                        "output_tokens": 8,
                        "duration_api_ms": 333,
                        "turn_duration_ms": 4444,
                        "num_turns": 1,
                    },
                    "instruction_ref": "dispatch/agent-dispatch/gate-prompt-formatter/writer123abc/instruction.yaml",
                    "dispatch_manifest_ref": "dispatch/agent-dispatch/gate-prompt-formatter/writer123abc/manifest.yaml",
                },
            )

            summary = output["agentDispatchReport"]
            self.assertEqual(summary["result"], "written")
            report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "provider_response_ready")
            self.assertEqual(report["response"], "writer command response")
            self.assertEqual(report["provider_evidence"]["input_tokens"], 22)
            self.assertEqual(report["provider_evidence"]["output_tokens"], 8)
            self.assertEqual(report["provider_evidence"]["duration_api_ms"], 333)
            self.assertEqual(report["provider_evidence"]["turn_duration_ms"], 4444)
            self.assertEqual(report["provider_evidence"]["num_turns"], 1)
            self.assertEqual(summary["schema_errors"], [])

    def test_agent_dispatch_report_command_accepts_zero_duration_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="dispatch-report-zero-duration-session")
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            tmux_session = state.get("tmux_session") or f"itb-{state['organization_instance_id']}"
            expected_target = f"{tmux_session}:gate-prompt-formatter.0"

            output = self.run_builder(
                state_root,
                "agent-dispatch-report",
                {
                    "session_id": "dispatch-report-zero-duration-session",
                    "agent_id": "gate-prompt-formatter",
                    "request_id": "writerzero123",
                    "source_agent": "codex-main",
                    "result": "provider_response_ready",
                    "response": "writer command response",
                    "started_at": "2026-01-01T00:00:00+09:00",
                    "completed_at": "2026-01-01T00:00:00+09:00",
                    "provider_evidence": {
                        "usage_source": "claude_tmux_interactive",
                        "effective_model": "claude-sonnet-4-6",
                        "duration_sec": 0.0,
                    },
                },
            )

            summary = output["agentDispatchReport"]
            self.assertEqual(summary["result"], "written")
            self.assertEqual(summary["schema_errors"], [])
            report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["provider_evidence"]["duration_sec"], 0.0)
            self.assertEqual(report["provider_evidence"]["provider_session_id"], expected_target)

    def test_builder_reads_hook_input_from_input_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            input_path = Path(tmp) / "hook-input.json"
            input_path.write_text(
                json.dumps({"session_id": "input-file-session", "cwd": "/tmp"}),
                encoding="utf-8",
            )

            output = self.run_builder_without_input(
                state_root,
                "session-start",
                extra_args=["--input-json-file", str(input_path)],
            )

            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn("session_id | `input-file-session`", context)

    def test_agent_dispatch_report_cli_args_select_session_and_role_without_session_in_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="target-session")
            self.bootstrap(state_root, session_id="stale-last-session")

            output = self.run_builder(
                state_root,
                "agent-dispatch-report",
                {
                    "request_id": "writercli123",
                    "source_agent": "codex-main",
                    "target": "itb-org-test:gate-prompt-formatter.0",
                    "result": "provider_response_ready",
                    "response": "writer command response",
                    "started_at": "2026-01-01T00:00:00+09:00",
                    "completed_at": "2026-01-01T00:00:05+09:00",
                    "provider_evidence": {
                        "usage_source": "claude_tmux_interactive",
                        "effective_model": "claude-sonnet-4-6",
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "duration_sec": 5,
                    },
                },
                extra_args=["--session-id", "target-session", "--role-id", "gate-prompt-formatter"],
            )

            summary = output["agentDispatchReport"]
            self.assertEqual(summary["result"], "written")
            self.assertIn("target-session/queue", summary["report_path"])
            report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["session_id"], "target-session")
            self.assertEqual(report["from_role"], "gate-prompt-formatter")
            self.assertFalse(
                (
                    state_root
                    / "stale-last-session"
                    / "queue"
                    / "reports"
                    / "agent-dispatch"
                    / "gate-prompt-formatter"
                    / "writercli123.yaml"
                ).exists()
            )

    def test_agent_dispatch_report_command_reads_usage_from_transcript_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = self.bootstrap(state_root, session_id="dispatch-report-transcript-session")
            transcript_path = session_dir / "provider-transcripts" / "writer123abc.jsonl"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "result",
                                "usage": {"input_tokens": 77, "output_tokens": 21},
                                "duration_api_ms": 654,
                                "num_turns": 4,
                            }
                        ),
                        json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 9876}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            output = self.run_builder(
                state_root,
                "agent-dispatch-report",
                {
                    "session_id": "dispatch-report-transcript-session",
                    "agent_id": "gate-prompt-formatter",
                    "request_id": "writer123abc",
                    "source_agent": "codex-main",
                    "target": "itb-org-test:gate-prompt-formatter.0",
                    "result": "provider_response_ready",
                    "response": "writer command response",
                    "started_at": "2026-01-01T00:00:00+09:00",
                    "completed_at": "2026-01-01T00:00:05+09:00",
                    "provider_evidence": {
                        "usage_source": "claude_tmux_interactive",
                        "effective_model": "claude-sonnet-4-6",
                        "provider_session_id": "itb-org-test:gate-prompt-formatter.0",
                        "transcript_path": str(transcript_path),
                    },
                    "instruction_ref": "dispatch/agent-dispatch/gate-prompt-formatter/writer123abc/instruction.yaml",
                    "dispatch_manifest_ref": "dispatch/agent-dispatch/gate-prompt-formatter/writer123abc/manifest.yaml",
                },
            )

            summary = output["agentDispatchReport"]
            self.assertEqual(summary["result"], "written")
            report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
            evidence = report["provider_evidence"]
            self.assertEqual(evidence["input_tokens"], 77)
            self.assertEqual(evidence["output_tokens"], 21)
            self.assertEqual(evidence["duration_api_ms"], 654)
            self.assertEqual(evidence["turn_duration_ms"], 9876)
            self.assertEqual(evidence["num_turns"], 4)
            self.assertEqual(evidence["transcript_path"], str(transcript_path))

    def test_agent_dispatch_timeout_updates_roster_state(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-timeout-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "timeout12345"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }
            captured = f"[ITB_AGENT_REQUEST id={request_id} agent=gate-prompt-formatter]\nThinking\n"

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_agent_completion",
                return_value=(False, captured, "timed out waiting for response marker", "timeout"),
            ), mock.patch.object(
                builder,
                "run_tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
            ):
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                        "timeout_seconds": 1,
                    },
                )

            dispatch = output["agentDispatch"]
            self.assertEqual(dispatch["result"], "provider_response_timeout")
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(row for row in roster if row["agent_id"] == "gate-prompt-formatter")
            self.assertEqual(gpf_row["response_status"], "timeout")
            self.assertEqual(gpf_row["provider_status"], "provider_response_timeout")
            self.assertEqual(gpf_row["last_request_id"], request_id)
            self.assertIn("last_timeout_at", gpf_row)

    def test_gate_latency_report_compares_haiku_sonnet_and_codex_exec(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "latency-session"
            session_dir.mkdir(parents=True)
            metrics_path = session_dir / "gate-metrics.jsonl"
            metrics = [
                {
                    "event_type": "agent_dispatch",
                    "role_id": "gate-task-creator",
                    "result": "provider_response_ready",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-haiku-4-5",
                    "permission_mode": "acceptEdits",
                    "duration_sec": 8.2,
                    "pending_latency_sec": 0,
                    "input_tokens": 40,
                    "output_tokens": 12,
                    "total_tokens": 52,
                    "duration_api_ms": 800,
                },
                {
                    "event_type": "agent_dispatch",
                    "role_id": "gate-prompt-formatter",
                    "result": "provider_response_ready",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-sonnet-4-6",
                    "duration_sec": 14.4,
                    "pending_latency_sec": 0,
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                    "duration_api_ms": 1200,
                },
                {
                    "event_type": "agent_dispatch",
                    "role_id": "gate-prompt-formatter",
                    "result": "provider_response_ready",
                    "usage_source": "codex_exec_json",
                    "effective_model": "gpt-5.5",
                    "duration_sec": 3.1,
                    "pending_latency_sec": 0,
                    "input_tokens": 18,
                    "output_tokens": 6,
                    "total_tokens": 24,
                    "duration_api_ms": 300,
                },
                {
                    "ts": "2026-06-13T10:00:00+09:00",
                    "event_type": "queued",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "to_role": "gate-prompt-formatter",
                    "task_id": "TSK-LAT-1",
                    "message_id": "msg-gpf",
                    "result": "queued",
                    "duration_sec": 0,
                    "pending_latency_sec": 0,
                },
                {
                    "ts": "2026-06-13T10:00:08+09:00",
                    "event_type": "finalized",
                    "role_id": "gate-prompt-formatter",
                    "from_role": "codex-user-prompt-submit",
                    "to_role": "gate-prompt-formatter",
                    "task_id": "TSK-LAT-1",
                    "message_id": "msg-gpf",
                    "result": "done",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-sonnet-4-6",
                    "duration_sec": 6.5,
                    "pending_latency_sec": 8,
                    "input_tokens": 90,
                    "output_tokens": 20,
                    "total_tokens": 110,
                    "duration_api_ms": 1000,
                },
                {
                    "ts": "2026-06-13T10:00:10+09:00",
                    "event_type": "queued",
                    "role_id": "gate-task-creator",
                    "from_role": "gate-prompt-formatter",
                    "to_role": "gate-task-creator",
                    "task_id": "TSK-LAT-1",
                    "message_id": "msg-gtc",
                    "result": "queued",
                    "duration_sec": 0,
                    "pending_latency_sec": 0,
                },
                {
                    "ts": "2026-06-13T10:00:23+09:00",
                    "event_type": "finalized",
                    "role_id": "gate-task-creator",
                    "from_role": "gate-prompt-formatter",
                    "to_role": "gate-task-creator",
                    "task_id": "TSK-LAT-1",
                    "message_id": "msg-gtc",
                    "result": "done",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-haiku-4-5",
                    "duration_sec": 10,
                    "pending_latency_sec": 13,
                    "input_tokens": 44,
                    "output_tokens": 16,
                    "total_tokens": 60,
                    "duration_api_ms": 900,
                },
                {
                    "ts": "2026-06-13T10:00:09+09:00",
                    "event_type": "finalized",
                    "role_id": "gate-task-creator",
                    "from_role": "gate-task-creator",
                    "to_role": "teams-project-manager",
                    "task_id": "TSK-LAT-1",
                    "message_id": "gtc-scaffold-TSK-LAT-1",
                    "result": "done",
                    "usage_source": "builder_command",
                    "effective_model": "deterministic",
                    "completion_source": "gtc-scaffold_command",
                    "duration_sec": 0.7,
                    "pending_latency_sec": 0,
                },
            ]
            for metric in metrics:
                builder.append_jsonl(metrics_path, metric)

            output = builder.gate_latency_report_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "latency-session", "metrics_path": str(metrics_path)},
            )

            summary = output["gateLatencyComparison"]
            variants = {row["variant"] for row in summary["rows"]}
            report_path = Path(summary["report_path"])
            json_path = Path(summary["json_path"])

            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["compared_sample_count"], 6)
            self.assertEqual(summary["missing_required_variants"], [])
            self.assertEqual(summary["task_timelines"][0]["task_id"], "TSK-LAT-1")
            self.assertEqual(summary["task_timelines"][0]["hop_count"], 3)
            self.assertEqual(summary["task_timelines"][0]["total_wall_sec"], 23.0)
            self.assertEqual(summary["task_timelines"][0]["slowest_hop_role"], "gate-task-creator")
            self.assertEqual(
                variants,
                {"builder_command", "claude_haiku_acceptEdits", "claude_sonnet_interactive", "codex_exec_json"},
            )
            rows_by_variant = {row["variant"]: row for row in summary["rows"]}
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["total_tokens_total"], 112)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["input_tokens_total"], 84)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["output_tokens_total"], 28)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["duration_api_ms_avg"], 850)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["token_sample_count"], 2)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["missing_token_sample_count"], 0)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["api_duration_sample_count"], 2)
            self.assertEqual(rows_by_variant["claude_haiku_acceptEdits"]["missing_api_duration_sample_count"], 0)
            self.assertEqual(rows_by_variant["claude_sonnet_interactive"]["total_tokens_total"], 235)
            self.assertEqual(rows_by_variant["codex_exec_json"]["total_tokens_total"], 24)
            self.assertEqual(rows_by_variant["builder_command"]["sample_count"], 1)
            self.assertEqual(rows_by_variant["builder_command"]["duration_p50_sec"], 0.7)
            prompt_submit = summary["prompt_submit_comparison"]
            self.assertEqual(prompt_submit["status"], "ready")
            self.assertEqual(prompt_submit["target_seconds"], 10.0)
            self.assertEqual(prompt_submit["target_verdict"], "pass")
            self.assertEqual(prompt_submit["combined_sla_seconds"], 240.0)
            self.assertEqual(prompt_submit["sla_verdict"], "pass")
            self.assertEqual(prompt_submit["deterministic_total_p50_sec"], 7.2)
            self.assertEqual(prompt_submit["llm_baseline_total_p50_sec"], 16.5)
            self.assertEqual(prompt_submit["baseline_speedup_ratio"], 2.292)
            self.assertEqual(prompt_submit["speedup_verdict"], "faster_than_llm_baseline")
            self.assertEqual(prompt_submit["chain_count"], 1)
            self.assertEqual(prompt_submit["deterministic_total_sample_count"], 1)
            self.assertEqual(prompt_submit["llm_baseline_total_sample_count"], 1)
            self.assertEqual(prompt_submit["gpf"]["sample_count"], 1)
            self.assertEqual(prompt_submit["gtc_scaffold"]["sample_count"], 1)
            self.assertEqual(prompt_submit["gtc_llm_baseline"]["sample_count"], 1)
            self.assertTrue(report_path.exists())
            self.assertTrue(json_path.exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Gate Latency Comparison", report_text)
            self.assertIn("PromptSubmit Comparison", report_text)
            self.assertIn("baseline_speedup_ratio", report_text)
            self.assertIn("token samples", report_text)
            self.assertIn("API samples", report_text)
            self.assertIn("Task Timeline", report_text)
            self.assertIn("total tokens", report_text)
            self.assertIn("| gate-task-creator | `claude_haiku_acceptEdits` | 2 | 2 |", report_text)
            self.assertIn("| gate-task-creator | `builder_command` | 1 | 1 | 0.7 | 0.7 | 0.7 |", report_text)
            self.assertIn("| gate-prompt-formatter | `codex_exec_json` | 1 | 1 |", report_text)
            self.assertIn("TSK-LAT-1", report_text)

    def test_gate_latency_prompt_submit_comparison_uses_chain_totals(self) -> None:
        builder = load_builder_module()
        metrics = [
            {
                "event_type": "finalized",
                "role_id": "gate-prompt-formatter",
                "from_role": "codex-user-prompt-submit",
                "task_id": "CHAIN-A",
                "result": "done",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-sonnet-4-6",
                "duration_sec": 1,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "CHAIN-A",
                "result": "done",
                "usage_source": "builder_command",
                "effective_model": "deterministic",
                "completion_source": "gtc-scaffold_command",
                "duration_sec": 100,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "CHAIN-A",
                "result": "done",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-haiku-4-5",
                "duration_sec": 120,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-prompt-formatter",
                "from_role": "codex-user-prompt-submit",
                "task_id": "CHAIN-B",
                "result": "done",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-sonnet-4-6",
                "duration_sec": 50,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "CHAIN-B",
                "result": "done",
                "usage_source": "builder_command",
                "effective_model": "deterministic",
                "completion_source": "gtc-scaffold_command",
                "duration_sec": 1,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "CHAIN-B",
                "result": "done",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-haiku-4-5",
                "duration_sec": 80,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "UNRELATED",
                "result": "done",
                "usage_source": "builder_command",
                "effective_model": "deterministic",
                "completion_source": "gtc-scaffold_command",
                "duration_sec": 999,
            },
        ]

        comparison = builder.gate_latency_prompt_submit_comparison(metrics, {})

        self.assertEqual(comparison["chain_count"], 2)
        self.assertEqual(comparison["deterministic_total_sample_count"], 2)
        self.assertEqual(comparison["llm_baseline_total_sample_count"], 2)
        self.assertEqual(comparison["deterministic_total_p50_sec"], 51.0)
        self.assertEqual(comparison["llm_baseline_total_p50_sec"], 121.0)
        self.assertEqual(comparison["gtc_scaffold"]["sample_count"], 2)
        self.assertEqual(comparison["gtc_scaffold"]["duration_max_sec"], 100.0)
        self.assertEqual(comparison["baseline_speedup_ratio"], 2.373)

    def test_gate_latency_prompt_submit_comparison_merges_legacy_report_path_aliases(self) -> None:
        builder = load_builder_module()
        metrics = [
            {
                "event_type": "finalized",
                "role_id": "gate-prompt-formatter",
                "from_role": "codex-user-prompt-submit",
                "task_id": "TSK-LEGACY",
                "message_id": "msg-gpf-legacy",
                "result": "done",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-sonnet-4-6",
                "duration_sec": 2,
                "report_path": "/tmp/queue/reports/gpf/TSK-LEGACY/rep-gpf.yaml",
                "report_ref": "reports/gpf/TSK-LEGACY/rep-gpf.yaml",
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "TSK-LEGACY",
                "message_id": "gtc-scaffold-TSK-LEGACY",
                "result": "done",
                "usage_source": "builder_command",
                "effective_model": "deterministic",
                "completion_source": "gtc-scaffold_command",
                "duration_sec": 1,
                "source_ref": "/tmp/queue/reports/gpf/TSK-LEGACY/rep-gpf.yaml",
                "report_path": "/tmp/queue/reports/gtc/TSK-LEGACY/rep-builder.yaml",
            },
            {
                "event_type": "finalized",
                "role_id": "gate-task-creator",
                "task_id": "TSK-LEGACY",
                "message_id": "msg-gtc-legacy",
                "result": "done",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-haiku-4-5",
                "duration_sec": 8,
                "report_path": "/tmp/queue/reports/gtc/TSK-LEGACY/rep-llm.yaml",
                "report_ref": "reports/gtc/TSK-LEGACY/rep-llm.yaml",
            },
        ]

        comparison = builder.gate_latency_prompt_submit_comparison(metrics, {})

        self.assertEqual(comparison["chain_count"], 1)
        self.assertEqual(comparison["deterministic_total_sample_count"], 1)
        self.assertEqual(comparison["llm_baseline_total_sample_count"], 1)
        self.assertEqual(comparison["deterministic_samples"][0]["chain_id"], "TSK-LEGACY")
        self.assertEqual(comparison["deterministic_total_p50_sec"], 3.0)
        self.assertEqual(comparison["llm_baseline_total_p50_sec"], 10.0)
        self.assertEqual(comparison["baseline_speedup_ratio"], 3.333)
        self.assertEqual(comparison["speedup_verdict"], "faster_than_llm_baseline")

    def test_gate_latency_report_requires_builder_command_by_default(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "latency-missing-builder-session"
            session_dir.mkdir(parents=True)
            metrics_path = session_dir / "gate-metrics.jsonl"
            for metric in [
                {
                    "event_type": "agent_dispatch",
                    "role_id": "gate-task-creator",
                    "result": "provider_response_ready",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-haiku-4-5",
                    "permission_mode": "acceptEdits",
                    "duration_sec": 7,
                },
                {
                    "event_type": "agent_dispatch",
                    "role_id": "gate-prompt-formatter",
                    "result": "provider_response_ready",
                    "usage_source": "claude_tmux_interactive",
                    "effective_model": "claude-sonnet-4-6",
                    "duration_sec": 9,
                },
                {
                    "event_type": "agent_dispatch",
                    "role_id": "gate-prompt-formatter",
                    "result": "provider_response_ready",
                    "usage_source": "codex_exec_json",
                    "effective_model": "gpt-5.5",
                    "duration_sec": 2,
                },
            ]:
                builder.append_jsonl(metrics_path, metric)

            output = builder.gate_latency_report_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "latency-missing-builder-session", "metrics_path": str(metrics_path)},
            )

            summary = output["gateLatencyComparison"]
            self.assertEqual(summary["status"], "insufficient_samples")
            self.assertIn("builder_command", summary["missing_required_variants"])
            self.assertEqual(summary["prompt_submit_comparison"]["status"], "insufficient_samples")

    def test_gate_latency_report_enriches_missing_usage_from_queue_report_transcript(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            state_root = root / "state"
            session_id = "latency-enrichment-session"
            session_dir = state_root / session_id
            queue_root = session_dir / "queue"
            metrics_path = session_dir / "gate-metrics.jsonl"
            report_path = (
                queue_root
                / "reports"
                / "gate-prompt-formatter"
                / "ENTRY-latency-transcript"
                / "rep-latency-transcript.yaml"
            )
            provider_cwd = state_root / session_id / "provider-state" / "gate-prompt-formatter" / "claude"
            project_dir = home / ".claude" / "projects" / builder.claude_project_dir_name_for_cwd(provider_cwd)
            home.mkdir()
            project_dir.mkdir(parents=True)
            transcript_path = project_dir / "claude-session.jsonl"
            transcript_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "message": {"role": "user", "content": "message_id: msg-latency-transcript"},
                                "cwd": str(provider_cwd),
                                "timestamp": "2026-06-14T12:00:01+09:00",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "model": "claude-sonnet-4-6",
                                    "usage": {"input_tokens": 55, "output_tokens": 13},
                                },
                                "duration_api_ms": 432,
                                "num_turns": 1,
                                "requestId": "req-latency-transcript",
                                "cwd": str(provider_cwd),
                                "timestamp": "2026-06-14T12:00:08+09:00",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "turn_duration",
                                "durationMs": 12000,
                                "cwd": str(provider_cwd),
                                "timestamp": "2026-06-14T12:00:09+09:00",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            builder.write_json_yaml(
                report_path,
                {
                    "report_type": "role_queue_report",
                    "from_role": "gate-prompt-formatter",
                    "task_id": "ENTRY-latency-transcript",
                    "message_id": "msg-latency-transcript",
                    "created_at": "2026-06-14T12:12:20+09:00",
                    "result": "done",
                    "status": "done",
                    "provider_evidence": {
                        "provider": "anthropic",
                        "request_id": "interactive-role-report",
                        "session_id": session_id,
                        "effective_model": "claude-sonnet-4-6",
                        "usage_source": "provider_turn",
                        "transcript_path": "",
                    },
                },
            )
            builder.append_jsonl(
                metrics_path,
                {
                    "ts": "2026-06-14T12:00:00+09:00",
                    "event_type": "queued",
                    "role_id": "gate-prompt-formatter",
                    "task_id": "ENTRY-latency-transcript",
                    "message_id": "msg-latency-transcript",
                    "result": "queued",
                    "duration_sec": 0,
                    "pending_latency_sec": 0,
                },
            )
            builder.append_jsonl(
                metrics_path,
                {
                    "ts": "2026-06-14T12:12:22+09:00",
                    "event_type": "finalized",
                    "role_id": "gate-prompt-formatter",
                    "task_id": "ENTRY-latency-transcript",
                    "message_id": "msg-latency-transcript",
                    "result": "done",
                    "usage_source": "provider_turn",
                    "effective_model": "claude-sonnet-4-6",
                    "duration_sec": 0,
                    "pending_latency_sec": 22,
                    "transcript_path": "",
                },
            )

            with mock.patch.object(builder.Path, "home", return_value=home):
                output = builder.gate_latency_report_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "metrics_path": str(metrics_path),
                        "required_variants": ["claude_sonnet_interactive"],
                    },
                )

            summary = output["gateLatencyComparison"]
            row = summary["rows"][0]
            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["report_enrichment"]["matched_report_count"], 1)
            self.assertEqual(summary["report_enrichment"]["enriched_metric_count"], 1)
            self.assertEqual(summary["report_enrichment"]["token_enriched_count"], 1)
            self.assertEqual(summary["report_enrichment"]["api_duration_enriched_count"], 1)
            self.assertEqual(summary["report_enrichment"]["turn_duration_enriched_count"], 1)
            self.assertEqual(summary["report_enrichment"]["transcript_found_count"], 1)
            self.assertEqual(row["variant"], "claude_sonnet_interactive")
            self.assertEqual(row["token_sample_count"], 1)
            self.assertEqual(row["missing_token_sample_count"], 0)
            self.assertEqual(row["total_tokens_total"], 68)
            self.assertEqual(row["duration_api_ms_avg"], 432)
            self.assertEqual(row["turn_duration_sample_count"], 1)
            self.assertEqual(row["turn_duration_ms_avg"], 12000)
            self.assertEqual(row["usage_sources"], ["claude_transcript_jsonl"])
            self.assertEqual(row["effective_models"], ["claude-sonnet-4-6"])
            report_text = Path(summary["report_path"]).read_text(encoding="utf-8")
            self.assertIn("report_enrichment", report_text)
            self.assertIn("Turn samples", report_text)

    def test_gate_latency_report_enriches_legacy_evidence_report(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "latency-legacy-evidence-session"
            session_dir = state_root / session_id
            queue_root = session_dir / "queue"
            metrics_path = session_dir / "gate-metrics.jsonl"
            report_path = (
                queue_root
                / "reports"
                / "gate-prompt-formatter"
                / "ENTRY-latency-legacy"
                / "rep-latency-legacy.yaml"
            )
            builder.write_json_yaml(
                report_path,
                {
                    "report_type": "role_agent_worker_report",
                    "from_role": "gate-prompt-formatter",
                    "task_id": "ENTRY-latency-legacy",
                    "message_id": "msg-latency-legacy",
                    "created_at": "2026-06-14T12:00:05+09:00",
                    "result": "done",
                    "status": "done",
                    "evidence": {
                        "request_id": "req-latency-legacy",
                        "usage_source": "claude_tmux_interactive",
                        "effective_model": "claude-sonnet-4-6",
                        "input_tokens": 21,
                        "output_tokens": 9,
                        "duration_api_ms": 321,
                    },
                },
            )
            builder.append_jsonl(
                metrics_path,
                {
                    "ts": "2026-06-14T12:00:07+09:00",
                    "event_type": "finalized",
                    "role_id": "gate-prompt-formatter",
                    "task_id": "ENTRY-latency-legacy",
                    "message_id": "msg-latency-legacy",
                    "result": "done",
                    "usage_source": "provider_turn",
                    "effective_model": "claude-sonnet-4-6",
                    "duration_sec": 0,
                    "pending_latency_sec": 7,
                },
            )

            output = builder.gate_latency_report_output(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": session_id,
                    "metrics_path": str(metrics_path),
                    "required_variants": ["claude_sonnet_interactive"],
                },
            )

            summary = output["gateLatencyComparison"]
            row = summary["rows"][0]
            self.assertEqual(summary["report_enrichment"]["matched_report_count"], 1)
            self.assertEqual(summary["report_enrichment"]["enriched_metric_count"], 1)
            self.assertEqual(row["token_sample_count"], 1)
            self.assertEqual(row["missing_token_sample_count"], 0)
            self.assertEqual(row["total_tokens_total"], 30)
            self.assertEqual(row["duration_api_ms_avg"], 321)
            self.assertEqual(row["usage_sources"], ["claude_tmux_interactive"])

    def test_agent_dispatch_defaults_submit_enter_count_to_two(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-default-submit-session"
            self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "def123abc456"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "force_respawned",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }
            captured = (
                f"[ITB_AGENT_REQUEST id={request_id} agent=gate-prompt-formatter]\n"
                "role output\n"
                f"[ITB_AGENT_RESPONSE_DONE id={request_id}]\n"
            )

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ) as send_payload, mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_agent_completion",
                return_value=(True, captured, "", "pane_marker"),
            ), mock.patch.object(
                builder,
                "run_tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr=""),
            ):
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                    },
                )

            self.assertEqual(output["agentDispatch"]["result"], "provider_response_ready")
            self.assertEqual(send_payload.call_args.kwargs["submit_enter_count"], 2)

    def test_agent_dispatch_force_respawns_at_context_turn_limit(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-context-limit-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for item in roster:
                if item.get("agent_id") == "gate-prompt-formatter":
                    item["dispatch_context_turns"] = 8
                    item["last_dispatch_task_id"] = "TSK-1000"
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8")
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": "itb-org-test:gate-prompt-formatter.0",
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }

            with mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ) as ensure_provider, mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ):
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return ok.",
                        "tools": "Read",
                        "task_id": "TSK-1000",
                        "context_reset_every": 8,
                        "wait": False,
                    },
                )

            self.assertTrue(ensure_provider.call_args.kwargs["force_respawn"])
            dispatch = output["agentDispatch"]
            self.assertEqual(dispatch["context_reset"]["reasons"], ["dispatch_context_turn_limit"])
            self.assertEqual(dispatch["dispatch_context_turns"], 1)
            updated = {
                item["agent_id"]: item
                for item in json.loads(roster_path.read_text(encoding="utf-8"))
            }
            self.assertEqual(updated["gate-prompt-formatter"]["dispatch_context_turns"], 1)
            self.assertEqual(updated["gate-prompt-formatter"]["last_context_reset_reason"], "dispatch_context_turn_limit")

    def test_agent_dispatch_force_respawns_on_task_boundary(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-task-boundary-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            roster_path = session_dir / "roster.json"
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            for item in roster:
                if item.get("agent_id") == "gate-prompt-formatter":
                    item["dispatch_context_turns"] = 1
                    item["last_dispatch_task_id"] = "TSK-1000"
            roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8")
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": "itb-org-test:gate-prompt-formatter.0",
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }

            with mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ) as ensure_provider, mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ):
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return ok.",
                        "tools": "Read",
                        "task_id": "TSK-2000",
                        "context_reset_every": 99,
                        "wait": False,
                    },
                )

            self.assertTrue(ensure_provider.call_args.kwargs["force_respawn"])
            self.assertEqual(output["agentDispatch"]["context_reset"]["reasons"], ["task_boundary"])
            updated = {
                item["agent_id"]: item
                for item in json.loads(roster_path.read_text(encoding="utf-8"))
            }
            self.assertEqual(updated["gate-prompt-formatter"]["last_dispatch_task_id"], "TSK-2000")
            self.assertEqual(updated["gate-prompt-formatter"]["dispatch_context_turns"], 1)

    def test_agent_dispatch_records_duration_when_wait_disabled(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-no-wait-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "fed123abc456"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_agent_completion",
                return_value=(True, "", "", "pane_marker"),
            ) as wait_for_completion:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                        "wait": False,
                    },
                )

            dispatch = output["agentDispatch"]
            self.assertEqual(dispatch["result"], "provider_request_sent")
            self.assertIsInstance(dispatch["duration_sec"], float)
            dispatch_report = json.loads(Path(dispatch["dispatch_report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(dispatch_report["result"], "provider_request_sent")
            self.assertEqual(dispatch_report["status"], "pending")
            self.assertFalse(dispatch_report["wait_enabled"])
            wait_for_completion.assert_not_called()
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            dispatch_metric = next(
                item
                for item in metrics
                if item["event_type"] == "agent_dispatch" and item["request_id"] == request_id
            )
            self.assertEqual(dispatch_metric["result"], "provider_request_sent")
            self.assertIsInstance(dispatch_metric["duration_sec"], float)

    def test_agent_dispatch_batch_fans_out_wait_false_and_records_barrier_reports(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-batch-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            calls: list[dict[str, object]] = []

            def fake_agent_dispatch(*, runtime, state_root, hook_input):
                calls.append(dict(hook_input))
                agent_id = hook_input["agent_id"]
                request_id = {
                    "gate-prompt-formatter": "batchgpf1234",
                    "teams-project-manager": "batchtpm1234",
                }[agent_id]
                started_at = "2026-01-01T00:00:00+09:00"
                completed_at = "2026-01-01T00:00:02+09:00"
                queue_root = builder.queue_root_for(session_dir, hook_input)
                report_path, report_ref = builder.write_agent_dispatch_report(
                    queue_root=queue_root,
                    agent_id=agent_id,
                    request_id=request_id,
                    runtime=runtime,
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent=hook_input["source_agent"],
                    target=f"itb-org-test:{agent_id}.0",
                    result="provider_response_ready",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_seconds=2.0,
                    response=f"{agent_id} done",
                    wait_error="",
                    captured="",
                    launch_result={"pane_current_command": "claude", "launch_status": "already_running"},
                    wait_enabled=False,
                    instruction_ref=f"dispatch/agent-dispatch/{agent_id}/{request_id}/instruction.yaml",
                    dispatch_manifest_ref=f"dispatch/agent-dispatch/{agent_id}/{request_id}/manifest.yaml",
                    input_tokens=10,
                    output_tokens=5,
                    duration_api_ms=321,
                    num_turns=1,
                )
                return {
                    "agentDispatch": {
                        "agent_id": agent_id,
                        "request_id": request_id,
                        "result": "provider_request_sent",
                        "started_at": started_at,
                        "dispatch_report_path": str(report_path),
                        "dispatch_report_ref": report_ref,
                        "source_agent": hook_input["source_agent"],
                    }
                }

            with mock.patch.object(builder, "agent_dispatch", side_effect=fake_agent_dispatch):
                output = builder.agent_dispatch_batch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "source_agent": "teams-project-manager",
                        "timeout_seconds": 1,
                        "poll_interval_seconds": 0.1,
                        "dispatches": [
                            {
                                "agent_id": "gate-prompt-formatter",
                                "prompt": "Review only this independent input.",
                                "tools": "Read",
                                "independent": True,
                            },
                            {
                                "agent_id": "teams-project-manager",
                                "prompt": "Review only this independent input.",
                                "tools": "Read",
                                "independent": True,
                            },
                        ],
                    },
                )

            batch = output["agentDispatchBatch"]
            self.assertEqual(batch["result"], "provider_responses_ready")
            self.assertEqual(batch["dispatch_count"], 2)
            self.assertEqual(batch["completed_count"], 2)
            self.assertEqual(batch["failed_count"], 0)
            self.assertTrue(all(call["wait"] is False for call in calls))
            self.assertTrue(all(call["batch_id"] == batch["batch_id"] for call in calls))
            self.assertEqual({item["result"] for item in batch["dispatches"]}, {"provider_response_ready"})
            self.assertEqual({item["completion_source"] for item in batch["dispatches"]}, {"dispatch_report_file_batch_barrier"})

            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            by_role = {item["agent_id"]: item for item in roster}
            self.assertEqual(by_role["gate-prompt-formatter"]["response_status"], "invoked")
            self.assertEqual(by_role["teams-project-manager"]["response_status"], "invoked")
            self.assertEqual(by_role["gate-prompt-formatter"]["last_dispatch_batch_id"], batch["batch_id"])
            bootstrap = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            self.assertEqual(bootstrap["last_agent_dispatch_batch_id"], batch["batch_id"])
            self.assertEqual(
                bootstrap["resident_agents_provider_ready"],
                sum(1 for item in roster if item.get("provider_status") in {"provider_process_ready", "provider_response_ready"}),
            )
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "agent_dispatch_batch" and item["result"] == "provider_responses_ready" for item in evidence))
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                len([item for item in metrics if item.get("completion_source") == "dispatch_report_file_batch_barrier"]),
                2,
            )

    def test_agent_dispatch_batch_requires_independence_and_unique_roles(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-batch-policy-session"
            self.bootstrap(state_root, session_id=session_id)

            output = builder.agent_dispatch_batch(
                runtime="codex",
                state_root=state_root,
                hook_input={
                    "session_id": session_id,
                    "dispatches": [
                        {"agent_id": "gate-prompt-formatter", "prompt": "first"},
                        {"agent_id": "gate-prompt-formatter", "prompt": "second", "independent": True},
                    ],
                },
            )

            self.assertEqual(output["decision"], "block")
            self.assertIn("must declare independent", output["reason"])
            self.assertIn("duplicates agent_id gate-prompt-formatter", output["reason"])

    def test_agent_dispatch_wait_false_preserves_fast_provider_report(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-fast-report-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "fast123abc45"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
            }

            def fast_provider_report(_target, _payload, *, submit_enter_count):
                queue_root = session_dir / "queue"
                builder.write_agent_dispatch_report(
                    queue_root=queue_root,
                    agent_id="gate-prompt-formatter",
                    request_id=request_id,
                    runtime="codex",
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent="codex-main",
                    target=target,
                    result="provider_response_ready",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at="2026-01-01T00:00:00+09:00",
                    completed_at="2026-01-01T00:00:01+09:00",
                    duration_seconds=1.0,
                    response="fast done",
                    wait_error="",
                    captured="",
                    launch_result=launch_result,
                    wait_enabled=False,
                    instruction_ref=f"dispatch/agent-dispatch/gate-prompt-formatter/{request_id}/instruction.yaml",
                    dispatch_manifest_ref=f"dispatch/agent-dispatch/gate-prompt-formatter/{request_id}/manifest.yaml",
                    input_tokens=7,
                    output_tokens=3,
                    duration_api_ms=111,
                    num_turns=1,
                )
                return True, ""

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                side_effect=fast_provider_report,
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_agent_completion",
            ) as wait_for_completion:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                        "wait": False,
                    },
                )

            dispatch = output["agentDispatch"]
            self.assertEqual(dispatch["result"], "provider_response_ready")
            self.assertEqual(dispatch["response"], "fast done")
            self.assertEqual(dispatch["completion_source"], "dispatch_report_file")
            wait_for_completion.assert_not_called()
            report = json.loads(Path(dispatch["dispatch_report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "provider_response_ready")
            self.assertEqual(report["status"], "done")
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(any(item["request_id"] == request_id and item["result"] == "provider_request_sent" for item in metrics))

    def test_agent_dispatch_batch_drains_sent_reports_after_later_send_failure(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-batch-partial-failure-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)

            def fake_agent_dispatch(*, runtime, state_root, hook_input):
                agent_id = hook_input["agent_id"]
                if agent_id == "teams-project-manager":
                    return {"decision": "block", "reason": "provider send failed"}
                request_id = "partialgpf12"
                report_path, report_ref = builder.write_agent_dispatch_report(
                    queue_root=session_dir / "queue",
                    agent_id=agent_id,
                    request_id=request_id,
                    runtime=runtime,
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent=hook_input["source_agent"],
                    target=f"itb-org-test:{agent_id}.0",
                    result="provider_response_ready",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at="2026-01-01T00:00:00+09:00",
                    completed_at="2026-01-01T00:00:02+09:00",
                    duration_seconds=2.0,
                    response="first done",
                    wait_error="",
                    captured="",
                    launch_result={"pane_current_command": "claude", "launch_status": "already_running"},
                    wait_enabled=False,
                )
                return {
                    "agentDispatch": {
                        "agent_id": agent_id,
                        "request_id": request_id,
                        "result": "provider_request_sent",
                        "started_at": "2026-01-01T00:00:00+09:00",
                        "dispatch_report_path": str(report_path),
                        "dispatch_report_ref": report_ref,
                        "source_agent": hook_input["source_agent"],
                    }
                }

            with mock.patch.object(builder, "agent_dispatch", side_effect=fake_agent_dispatch):
                output = builder.agent_dispatch_batch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "source_agent": "tech-director",
                        "timeout_seconds": 1,
                        "poll_interval_seconds": 0.1,
                        "dispatches": [
                            {"agent_id": "gate-prompt-formatter", "prompt": "first", "independent": True},
                            {"agent_id": "teams-project-manager", "prompt": "second", "independent": True},
                        ],
                    },
                )

            batch = output["agentDispatchBatch"]
            self.assertEqual(output["decision"], "block")
            self.assertEqual(batch["result"], "dispatch_failed")
            self.assertEqual(batch["completed_count"], 1)
            self.assertTrue(any(item["agent_id"] == "gate-prompt-formatter" and item["result"] == "provider_response_ready" for item in batch["dispatches"]))
            self.assertTrue(any(item["agent_id"] == "teams-project-manager" and item["result"] == "missing_report" for item in batch["dispatches"]))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            self.assertEqual(
                next(item for item in roster if item["agent_id"] == "gate-prompt-formatter")["response_status"],
                "invoked",
            )

    def test_agent_dispatch_batch_timeout_rewrites_pending_report_as_timeout(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-batch-timeout-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)

            def fake_agent_dispatch(*, runtime, state_root, hook_input):
                agent_id = hook_input["agent_id"]
                request_id = "timeoutgpf1"
                report_path, report_ref = builder.write_agent_dispatch_report(
                    queue_root=session_dir / "queue",
                    agent_id=agent_id,
                    request_id=request_id,
                    runtime=runtime,
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent=hook_input["source_agent"],
                    target=f"itb-org-test:{agent_id}.0",
                    result="provider_request_sent",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at="2026-01-01T00:00:00+09:00",
                    completed_at="2026-01-01T00:00:00+09:00",
                    duration_seconds=0.0,
                    response="",
                    wait_error="",
                    captured="",
                    launch_result={"pane_current_command": "claude", "launch_status": "already_running"},
                    wait_enabled=False,
                )
                return {
                    "agentDispatch": {
                        "agent_id": agent_id,
                        "target": f"itb-org-test:{agent_id}.0",
                        "request_id": request_id,
                        "result": "provider_request_sent",
                        "started_at": "2026-01-01T00:00:00+09:00",
                        "duration_sec": 0,
                        "dispatch_report_path": str(report_path),
                        "dispatch_report_ref": report_ref,
                        "source_agent": hook_input["source_agent"],
                        "session_id": session_id,
                        "organization_instance_id": "org-test",
                        "effective_model": "claude-sonnet-4-6",
                    }
                }

            with mock.patch.object(builder, "agent_dispatch", side_effect=fake_agent_dispatch):
                output = builder.agent_dispatch_batch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "source_agent": "tech-director",
                        "timeout_seconds": 0.01,
                        "poll_interval_seconds": 0.1,
                        "dispatches": [
                            {"agent_id": "gate-prompt-formatter", "prompt": "first", "independent": True},
                        ],
                    },
                )

            batch = output["agentDispatchBatch"]
            self.assertEqual(output["decision"], "block")
            self.assertEqual(batch["result"], "provider_batch_incomplete")
            self.assertEqual(batch["dispatches"][0]["result"], "provider_response_timeout")
            self.assertEqual(batch["dispatches"][0]["completion_source"], "batch_barrier_timeout")
            report = json.loads(Path(batch["dispatches"][0]["dispatch_report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["result"], "provider_response_timeout")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["completion_signal"], "batch_barrier_timeout")

    def test_agent_dispatch_batch_surfaces_invalid_report_schema_errors(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-batch-invalid-report-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)

            def fake_agent_dispatch(*, runtime, state_root, hook_input):
                agent_id = hook_input["agent_id"]
                request_id = "invalidgpf1"
                report_path, report_ref = builder.write_agent_dispatch_report(
                    queue_root=session_dir / "queue",
                    agent_id=agent_id,
                    request_id=request_id,
                    runtime=runtime,
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent=hook_input["source_agent"],
                    target=f"itb-org-test:{agent_id}.0",
                    result="provider_response_ready",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at="2026-01-01T00:00:00+09:00",
                    completed_at="2026-01-01T00:00:02+09:00",
                    duration_seconds=2.0,
                    response="bad report should not count as invoked",
                    wait_error="",
                    captured="",
                    launch_result={"pane_current_command": "claude", "launch_status": "already_running"},
                    wait_enabled=False,
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["from_role"] = "wrong-role"
                report["provider_evidence"].pop("provider_session_id", None)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return {
                    "agentDispatch": {
                        "agent_id": agent_id,
                        "request_id": request_id,
                        "result": "provider_request_sent",
                        "started_at": "2026-01-01T00:00:00+09:00",
                        "dispatch_report_path": str(report_path),
                        "dispatch_report_ref": report_ref,
                        "source_agent": hook_input["source_agent"],
                    }
                }

            with mock.patch.object(builder, "agent_dispatch", side_effect=fake_agent_dispatch):
                output = builder.agent_dispatch_batch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "source_agent": "tech-director",
                        "timeout_seconds": 1,
                        "poll_interval_seconds": 0.1,
                        "dispatches": [
                            {"agent_id": "gate-prompt-formatter", "prompt": "first", "independent": True},
                        ],
                    },
                )

            batch = output["agentDispatchBatch"]
            self.assertEqual(output["decision"], "block")
            self.assertEqual(batch["result"], "provider_batch_incomplete")
            self.assertEqual(batch["completed_count"], 0)
            self.assertEqual(batch["failed_count"], 1)
            self.assertEqual(batch["dispatches"][0]["result"], "invalid_report")
            self.assertEqual(batch["dispatches"][0]["report_schema_status"], "invalid")
            self.assertTrue(any("from_role mismatch" in error for error in batch["errors"]))
            self.assertTrue(any("provider_session_id" in error for error in batch["errors"]))
            self.assertIn("from_role mismatch", output["reason"])

            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            gpf_row = next(item for item in roster if item["agent_id"] == "gate-prompt-formatter")
            self.assertNotEqual(gpf_row.get("response_status"), "invoked")

            events = [
                json.loads(line)
                for line in (session_dir / "queue-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(item["event_type"] == "agent_dispatch_report_invalid" for item in events))
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            invalid_metrics = [
                item
                for item in metrics
                if item.get("event_type") == "agent_dispatch" and item.get("result") == "invalid_report"
            ]
            self.assertEqual(len(invalid_metrics), 1)

    def test_agent_dispatch_batch_preserves_send_failure_and_invalid_report_errors(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-batch-mixed-error-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)

            def fake_agent_dispatch(*, runtime, state_root, hook_input):
                agent_id = hook_input["agent_id"]
                if agent_id == "teams-project-manager":
                    return {"decision": "block", "reason": "provider send failed"}
                request_id = "mixedgpf123"
                report_path, report_ref = builder.write_agent_dispatch_report(
                    queue_root=session_dir / "queue",
                    agent_id=agent_id,
                    request_id=request_id,
                    runtime=runtime,
                    session_id=session_id,
                    organization_instance_id="org-test",
                    source_agent=hook_input["source_agent"],
                    target=f"itb-org-test:{agent_id}.0",
                    result="provider_response_ready",
                    usage_source="claude_tmux_interactive",
                    effective_model="claude-sonnet-4-6",
                    started_at="2026-01-01T00:00:00+09:00",
                    completed_at="2026-01-01T00:00:02+09:00",
                    duration_seconds=2.0,
                    response="invalid but terminal",
                    wait_error="",
                    captured="",
                    launch_result={"pane_current_command": "claude", "launch_status": "already_running"},
                    wait_enabled=False,
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["request_id"] = "wrong-request"
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return {
                    "agentDispatch": {
                        "agent_id": agent_id,
                        "request_id": request_id,
                        "result": "provider_request_sent",
                        "started_at": "2026-01-01T00:00:00+09:00",
                        "dispatch_report_path": str(report_path),
                        "dispatch_report_ref": report_ref,
                        "source_agent": hook_input["source_agent"],
                    }
                }

            with mock.patch.object(builder, "agent_dispatch", side_effect=fake_agent_dispatch):
                output = builder.agent_dispatch_batch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "source_agent": "tech-director",
                        "timeout_seconds": 1,
                        "poll_interval_seconds": 0.1,
                        "dispatches": [
                            {"agent_id": "gate-prompt-formatter", "prompt": "first", "independent": True},
                            {"agent_id": "teams-project-manager", "prompt": "second", "independent": True},
                        ],
                    },
                )

            batch = output["agentDispatchBatch"]
            self.assertEqual(output["decision"], "block")
            self.assertEqual(batch["result"], "dispatch_failed")
            self.assertTrue(any("provider send failed" in error for error in batch["errors"]))
            self.assertTrue(any("request_id mismatch" in error for error in batch["errors"]))
            self.assertIn("provider send failed", output["reason"])
            self.assertIn("request_id mismatch", output["reason"])

    def test_gate_latency_summary_ignores_agent_dispatch_request_sent_samples(self) -> None:
        builder = load_builder_module()
        metrics = [
            {
                "event_type": "agent_dispatch",
                "agent_id": "gate-prompt-formatter",
                "result": "provider_request_sent",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-sonnet-4-6",
                "duration_sec": 1.0,
            },
            {
                "event_type": "agent_dispatch",
                "agent_id": "gate-prompt-formatter",
                "result": "provider_response_ready",
                "usage_source": "claude_tmux_interactive",
                "effective_model": "claude-sonnet-4-6",
                "duration_sec": 3.0,
            },
        ]

        rows = builder.gate_latency_summary_rows(metrics)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_count"], 1)
        self.assertEqual(rows[0]["success_count"], 1)
        self.assertEqual(rows[0]["duration_avg_sec"], 3.0)

    def test_gate_latency_summary_keeps_pending_latency_only_terminal_samples(self) -> None:
        builder = load_builder_module()
        metrics = [
            {
                "event_type": "queued",
                "role_id": "gate-prompt-formatter",
                "result": "queued",
                "duration_sec": 0,
                "pending_latency_sec": 0,
            },
            {
                "event_type": "watch_nudge",
                "role_id": "gate-prompt-formatter",
                "result": "nudged",
                "duration_sec": 0,
                "pending_latency_sec": 5,
            },
            {
                "event_type": "finalized",
                "role_id": "gate-prompt-formatter",
                "result": "done",
                "usage_source": "provider_turn",
                "effective_model": "claude-sonnet-4-6",
                "duration_sec": 0,
                "pending_latency_sec": 22,
            },
        ]

        rows = builder.gate_latency_summary_rows(metrics)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["variant"], "claude_sonnet_interactive")
        self.assertEqual(rows[0]["sample_count"], 1)
        self.assertEqual(rows[0]["success_count"], 1)
        self.assertEqual(rows[0]["duration_p50_sec"], 22.0)
        self.assertEqual(rows[0]["duration_p90_sec"], 22.0)
        self.assertEqual(rows[0]["pending_p50_sec"], 22.0)
        self.assertEqual(rows[0]["token_sample_count"], 0)
        self.assertEqual(rows[0]["missing_token_sample_count"], 1)
        self.assertEqual(rows[0]["api_duration_sample_count"], 0)
        self.assertEqual(rows[0]["missing_api_duration_sample_count"], 1)
        self.assertEqual(rows[0]["turn_duration_sample_count"], 0)
        self.assertEqual(rows[0]["missing_turn_duration_sample_count"], 1)

    def test_agent_dispatch_blocks_when_send_ack_is_unconfirmed(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-send-unconfirmed-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "bad123def456"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(False, "request marker not visible"),
            ), mock.patch.object(
                builder,
                "recover_unconfirmed_tmux_payload_send",
                return_value=(
                    False,
                    "request marker not visible",
                    [{"action": "extra_enter", "result": "unconfirmed"}],
                ),
            ) as recover_send, mock.patch.object(
                builder,
                "wait_for_agent_completion",
                return_value=(True, "", "", "pane_marker"),
            ) as wait_for_completion:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                        "send_ack_timeout_seconds": 9,
                    },
                )

            self.assertEqual(output["decision"], "block")
            self.assertIn("request marker not visible", output["reason"])
            self.assertEqual(output["agentDispatch"]["result"], "provider_send_unconfirmed")
            self.assertEqual(recover_send.call_args.kwargs["timeout_seconds"], 9)
            self.assertEqual(output["agentDispatch"]["send_recovery"][0]["action"], "extra_enter")
            wait_for_completion.assert_not_called()
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                any(
                    entry["event_type"] == "agent_dispatch"
                    and entry["result"] == "provider_send_unconfirmed"
                    and entry["request_id"] == request_id
                    for entry in evidence
                )
            )
            metrics = [
                json.loads(line)
                for line in (session_dir / "gate-metrics.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            dispatch_metric = next(
                item
                for item in metrics
                if item["event_type"] == "agent_dispatch" and item["request_id"] == request_id
            )
            self.assertEqual(dispatch_metric["result"], "provider_send_unconfirmed")

    def test_agent_dispatch_merges_roster_updates_after_wait(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_id = "dispatch-merge-roster-session"
            session_dir = self.bootstrap(state_root, session_id=session_id)
            roster_path = session_dir / "roster.json"
            state_path = session_dir / "bootstrap.json"
            target = "itb-org-test:gate-prompt-formatter.0"
            request_id = "cab123def456"
            launch_result = {
                "agent_id": "gate-prompt-formatter",
                "process_status": "process_ready",
                "launch_status": "already_running",
                "runtime_kind": "claude_tmux",
                "process_mode": "provider_cli",
                "provider_runtime_kind": "claude_tmux",
                "provider_status": "provider_process_ready",
                "tool_sidecar_status": "not_verified",
                "tmux_session": "itb-org-test",
                "tmux_window": "gate-prompt-formatter",
                "tmux_target": target,
                "process_evidence_source": "tmux",
                "process_started_at": "2026-01-01T00:00:00+09:00",
                "launch_error": "",
                "tools": "Read",
                "pane_current_command": "claude",
                "pane_pid": "12345",
            }
            captured = (
                f"[ITB_AGENT_REQUEST id={request_id} agent=gate-prompt-formatter]\n"
                "role output\n"
                f"[ITB_AGENT_RESPONSE_DONE id={request_id}]\n"
            )

            def mark_other_agent_ready(**_kwargs):
                roster = json.loads(roster_path.read_text(encoding="utf-8"))
                for item in roster:
                    if item["agent_id"] == "gate-task-creator":
                        item["response_status"] = "invoked"
                        item["provider_status"] = "provider_response_ready"
                        item["last_request_id"] = "concurrent-req"
                        item["usage_source"] = "claude_tmux_interactive"
                roster_path.write_text(json.dumps(roster, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["concurrent_state_marker"] = "preserve-me"
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return True, captured, "", "pane_marker"

            with mock.patch.object(
                builder.uuid, "uuid4", return_value=mock.Mock(hex=f"{request_id}ffff")
            ), mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(launch_result, ""),
            ), mock.patch.object(
                builder,
                "wait_for_interactive_prompt",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "tmux_send_payload",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_tmux_payload_ack",
                return_value=(True, ""),
            ), mock.patch.object(
                builder,
                "wait_for_agent_completion",
                side_effect=mark_other_agent_ready,
            ):
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": session_id,
                        "agent_id": "gate-prompt-formatter",
                        "source_agent": "codex-main",
                        "cwd": "/tmp",
                        "prompt": "Return a Gate Intake Envelope.",
                        "tools": "Read",
                    },
                )

            self.assertEqual(output["agentDispatch"]["result"], "provider_response_ready")
            roster = json.loads(roster_path.read_text(encoding="utf-8"))
            rows = {item["agent_id"]: item for item in roster}
            self.assertEqual(rows["gate-prompt-formatter"]["last_request_id"], request_id)
            self.assertEqual(rows["gate-task-creator"]["last_request_id"], "concurrent-req")
            self.assertEqual(rows["gate-task-creator"]["response_status"], "invoked")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["concurrent_state_marker"], "preserve-me")
            self.assertEqual(state["resident_agents_response_ready"], 2)
            self.assertEqual(state["resident_agents_provider_response_ready"], 2)
            self.assertEqual(state["provider_response_readiness_scope"], "response_evidence")

    def test_agent_dispatch_timeout_diagnosis_detects_pending_claude_prompt(self) -> None:
        builder = load_builder_module()
        diagnosis = builder.diagnose_agent_dispatch_timeout(
            "[ITB_AGENT_REQUEST id=req123 agent=gate-prompt-formatter]\n"
            "Do you want to proceed?\n"
            "❯ ",
            "req123",
        )

        self.assertIn("request marker", diagnosis)
        self.assertIn("approval prompt", diagnosis)
        self.assertIn("input prompt", diagnosis)

    def test_agent_dispatch_blocks_unapproved_permission_mode_and_tools(self) -> None:
        builder = load_builder_module()
        cases = [
            ("permission_mode", "bypassPermissions", "permission_mode"),
            ("tools", "Bash", "tools"),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                self.bootstrap(state_root, session_id="dispatch-block-session")
                hook_input = {
                    "session_id": "dispatch-block-session",
                    "agent_id": "gate-prompt-formatter",
                    "prompt": "Return ok.",
                }
                hook_input[field] = value

                with mock.patch.object(builder, "ensure_provider_cli_for_agent") as ensure_provider:
                    output = builder.agent_dispatch(
                        runtime="codex",
                        state_root=state_root,
                        hook_input=hook_input,
                    )

                self.assertEqual(output["decision"], "block")
                self.assertIn(reason, output["reason"])
                ensure_provider.assert_not_called()

    def test_agent_dispatch_blocks_git_operation_for_non_git_role(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="dispatch-git-block-session")
            hook_input = {
                "session_id": "dispatch-git-block-session",
                "agent_id": "infra-director",
                "prompt": "Run git commit -m 'publish task artifacts'.",
                "tools": "Read,Grep,Glob,Bash,Write,Edit,Agent",
            }

            with mock.patch.object(builder, "ensure_provider_cli_for_agent") as ensure_provider:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input=hook_input,
                )

            self.assertEqual(output["decision"], "block")
            self.assertIn("git operation forbidden", output["reason"])
            self.assertIn("infra-director", output["reason"])
            ensure_provider.assert_not_called()

    def test_git_operation_validation_allows_git_tool_roles(self) -> None:
        builder = load_builder_module()

        self.assertEqual(
            builder.validate_git_operation_for_role(
                {"agent_id": "git-publisher"},
                "Run git push origin feature-branch.",
            ),
            "",
        )
        self.assertIn(
            "git operation forbidden",
            builder.validate_git_operation_for_role(
                {"agent_id": "tech-backend", "git_operations_allowed": False},
                "Run git reset --hard HEAD.",
            ),
        )

    def test_agent_dispatch_allows_accept_edits_permission_mode(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            self.bootstrap(state_root, session_id="dispatch-accept-edits-session")
            hook_input = {
                "session_id": "dispatch-accept-edits-session",
                "agent_id": "gate-prompt-formatter",
                "prompt": "Return ok.",
                "permission_mode": "acceptEdits",
                "tools": "Read",
            }

            with mock.patch.object(
                builder,
                "ensure_provider_cli_for_agent",
                return_value=(None, "intentional test stop"),
            ) as ensure_provider:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input=hook_input,
                )

            self.assertEqual(output["decision"], "block")
            self.assertIn("intentional test stop", output["reason"])
            ensure_provider.assert_called_once()
            self.assertEqual(ensure_provider.call_args.kwargs["permission_mode"], "acceptEdits")

    def test_ensure_provider_cli_force_respawns_existing_provider_pane(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
        }
        state = {
            "organization_instance_id": "org-test",
            "tmux_session": "itb-org-test",
        }
        pane_info = {
            "pane_current_command": "claude",
            "pane_dead": "0",
            "pane_pid": "999",
            "pane_current_path": "/tmp",
            "pane_start_command": (
                "ITB_AGENT_ID=gate-prompt-formatter "
                "ITB_AGENT_PROCESS_MODE=provider_cli claude --name gate-prompt-formatter"
            ),
        }
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/claude"), mock.patch.object(
            builder, "tmux_session_exists", return_value=True
        ), mock.patch.object(
            builder, "tmux_window_exists", return_value=True
        ), mock.patch.object(
            builder, "tmux_pane_info", return_value=pane_info
        ), mock.patch.object(
            builder, "run_tmux", side_effect=fake_run_tmux
        ):
            result, error = builder.ensure_provider_cli_for_agent(
                row=row,
                state=state,
                cwd="/tmp",
                now="2026-01-01T00:00:00+09:00",
                force_respawn=True,
                permission_mode="plan",
                tools="Read",
            )

        self.assertEqual(error, "")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["launch_status"], "force_respawned")
        self.assertEqual(result["tmux_target"], "itb-org-test:gate-prompt-formatter.0")
        self.assertEqual(result["tmux_config_status"], "configured")
        self.assertEqual(result["tmux_history_limit"], 50000)
        self.assertTrue(
            any(call[:4] == ["respawn-pane", "-k", "-t", "itb-org-test:gate-prompt-formatter.0"] for call in calls)
        )
        self.assertIn(["set-option", "-t", "itb-org-test", "history-limit", "50000"], calls)

    def test_ensure_provider_cli_respawns_conflicting_itb_pane(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
        }
        state = {
            "organization_instance_id": "org-test",
            "tmux_session": "itb-org-test",
        }
        pane_info = {
            "pane_current_command": "claude.exe",
            "pane_dead": "0",
            "pane_pid": "999",
            "pane_current_path": "/tmp",
            "pane_start_command": "claude.exe",
        }
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/claude"), mock.patch.object(
            builder, "tmux_session_exists", return_value=True
        ), mock.patch.object(
            builder, "tmux_window_exists", return_value=True
        ), mock.patch.object(
            builder, "tmux_pane_info", return_value=pane_info
        ), mock.patch.object(
            builder, "run_tmux", side_effect=fake_run_tmux
        ):
            result, error = builder.ensure_provider_cli_for_agent(
                row=row,
                state=state,
                cwd="/tmp",
                now="2026-01-01T00:00:00+09:00",
                permission_mode="plan",
                tools="Read",
            )

        self.assertEqual(error, "")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["launch_status"], "replaced_conflicting_itb_pane")
        self.assertTrue(
            any(call[:4] == ["respawn-pane", "-k", "-t", "itb-org-test:gate-prompt-formatter.0"] for call in calls)
        )

    def test_ensure_provider_cli_does_not_respawn_non_itb_conflicting_pane(self) -> None:
        builder = load_builder_module()
        row = {
            "agent_id": "gate-prompt-formatter",
            "agent_instance_id": "gate-prompt-formatter@test-session",
            "organization_instance_id": "org-test",
            "parent_session_id": "test-session",
            "activation_status": "metadata_ready",
            "provider": "anthropic",
            "intended_model": "claude-sonnet-4-6",
            "execution_mode": "agent",
        }
        state = {
            "organization_instance_id": "org-test",
            "tmux_session": "shared-work",
        }
        pane_info = {
            "pane_current_command": "claude.exe",
            "pane_dead": "0",
            "pane_pid": "999",
            "pane_current_path": "/tmp",
            "pane_start_command": "claude.exe",
        }
        calls: list[list[str]] = []

        def fake_run_tmux(args: list[str], timeout: float = 5.0):
            calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/claude"), mock.patch.object(
            builder, "tmux_session_exists", return_value=True
        ), mock.patch.object(
            builder, "tmux_window_exists", return_value=True
        ), mock.patch.object(
            builder, "tmux_pane_info", return_value=pane_info
        ), mock.patch.object(
            builder, "run_tmux", side_effect=fake_run_tmux
        ):
            result, error = builder.ensure_provider_cli_for_agent(
                row=row,
                state=state,
                cwd="/tmp",
                now="2026-01-01T00:00:00+09:00",
                permission_mode="plan",
                tools="Read",
            )

        self.assertIn("existing pane command=claude.exe", error)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["process_status"], "launch_failed")
        self.assertEqual(result["launch_status"], "window_conflict")
        self.assertFalse(any(call and call[0] == "respawn-pane" for call in calls))

    def test_session_start_launch_agents_child_env_skips_recursive_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            env = os.environ.copy()
            env["ITB_AGENT_CHILD"] = "1"
            env["ITB_AGENT_LAUNCH_DRY_RUN"] = "1"

            self.run_builder(
                state_root,
                "session-start",
                {"session_id": "child-session", "cwd": "/tmp", "source": "SessionStart"},
                extra_args=["--launch-agents"],
                env=env,
            )
            session_dir = state_root / "child-session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))

            self.assertEqual(state["bootstrap_status"], "ready")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["spawn_mode"], "child_hook_skipped")
            self.assertEqual(state["resident_agents_process_ready"], 0)
            self.assertTrue(all(row["launch_status"] == "skipped_child_agent_context" for row in roster))


if __name__ == "__main__":
    unittest.main()
