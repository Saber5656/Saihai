from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
BUILDER = SKILL_ROOT / "scripts" / "itb_bootstrap_builder.py"


def load_builder_module():
    spec = importlib.util.spec_from_file_location("itb_runtime_regressions_for_test", BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load ITB builder module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_codex_jsonl(*, include_message: bool = True) -> str:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "provider-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-error",
                "type": "error",
                "message": "diagnostic only",
            },
        },
    ]
    if include_message:
        events.extend(
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-progress",
                        "type": "agent_message",
                        "text": "review in progress",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-final",
                        "type": "agent_message",
                        "text": "final review result",
                    },
                },
            ]
        )
    events.append(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 11,
                "cached_input_tokens": 5,
                "output_tokens": 7,
                "reasoning_output_tokens": 3,
            },
        }
    )
    return "\n".join(json.dumps(event) for event in events) + "\n"


class ItbRuntimeRegressionTest(unittest.TestCase):
    def test_parse_codex_current_jsonl_extracts_final_message_session_and_usage(self) -> None:
        builder = load_builder_module()

        parsed = builder.parse_codex_json_output(current_codex_jsonl())

        self.assertEqual(parsed["result"], "final review result")
        self.assertEqual(parsed["session_id"], "provider-thread")
        self.assertEqual(parsed["usage"]["input_tokens"], 11)
        self.assertEqual(parsed["usage"]["cached_input_tokens"], 5)
        self.assertEqual(parsed["usage"]["output_tokens"], 7)
        self.assertEqual(parsed["usage"]["reasoning_output_tokens"], 3)
        self.assertEqual(parsed["num_turns"], 1)

    def test_codex_dispatch_accepts_current_jsonl_as_response_evidence(self) -> None:
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
            completed = subprocess.CompletedProcess(
                args=["codex"],
                returncode=0,
                stdout=current_codex_jsonl(),
                stderr="",
            )

            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder.subprocess,
                "run",
                return_value=completed,
            ):
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-current-jsonl",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

        dispatch = output["agentDispatch"]
        self.assertEqual(dispatch["result"], "provider_response_ready")
        self.assertEqual(dispatch["response"], "final review result")
        self.assertEqual(dispatch["provider_session_id"], "provider-thread")
        self.assertEqual(dispatch["input_tokens"], 11)
        self.assertEqual(dispatch["output_tokens"], 7)

    def test_codex_dispatch_without_agent_message_remains_fail_closed(self) -> None:
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
            completed = subprocess.CompletedProcess(
                args=["codex"],
                returncode=0,
                stdout=current_codex_jsonl(include_message=False),
                stderr="",
            )

            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder.subprocess,
                "run",
                return_value=completed,
            ):
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-no-message",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

        self.assertEqual(output["decision"], "block")
        self.assertEqual(output["agentDispatch"]["result"], "provider_response_no_inference")

    def test_parse_codex_malformed_jsonl_remains_fail_closed(self) -> None:
        builder = load_builder_module()

        with self.assertRaises(json.JSONDecodeError):
            builder.parse_codex_json_output("{not-json}\n")

    def test_agent_call_wait_returns_typed_timeout_without_name_error(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            output = builder.agent_call(
                runtime="codex",
                state_root=Path(tmp),
                hook_input={
                    "session_id": "session",
                    "completion_wait_seconds": 0.02,
                    "completion_wait_poll_seconds": 0.005,
                    "completion_wait_event_driven": False,
                    "manifest": {
                        "agent_call_manifest_version": "1",
                        "task_id": "TSK-test",
                        "from_role": "tech-director",
                        "to_role": "tech-reviewer",
                        "assignment_role": "reviewer",
                        "instruction": "Review only.",
                        "expected_output": "review_report",
                        "wait": True,
                    },
                },
            )

        self.assertEqual(output["decision"], "ok")
        completion = output["agentCall"]["completion_wait"]
        self.assertEqual(completion["result"], "timeout")
        self.assertEqual(completion["wait_result"], "timeout")
        self.assertEqual(completion["completion_source"], "bounded_wait")

    def test_wait_returns_typed_done_and_failed_terminal_results(self) -> None:
        builder = load_builder_module()
        for status in ("done", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                queue_root = session_dir / "queue"
                inbox_path = queue_root / "inbox" / "tech-reviewer.yaml"
                message = {
                    "message_id": f"msg-{status}",
                    "task_id": "TSK-test",
                    "status": status,
                    "report_path": f"reports/tech-reviewer/TSK-test/{status}.yaml",
                }
                builder.write_json_yaml(
                    inbox_path,
                    {
                        "envelope_version": "1",
                        "role_id": "tech-reviewer",
                        "messages": [message],
                    },
                )

                output = builder.wait_for_role_queue_completion(
                    runtime="codex",
                    state_root=state_root,
                    session_dir=session_dir,
                    session_id="session",
                    organization_instance_id="org-test",
                    queue_root=queue_root,
                    inbox_path=inbox_path,
                    role_id="tech-reviewer",
                    role_row={},
                    message=message,
                    timeout_seconds=0.02,
                    poll_interval_seconds=0.005,
                    event_driven=False,
                    hook_input={},
                )

                self.assertEqual(output["result"], status)
                self.assertEqual(output["wait_result"], "completed")
                self.assertEqual(output["completion_source"], "inbox_status")

    def test_event_wait_reports_queue_change(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            queue_root.mkdir()
            marker = queue_root / "inbox" / "marker.json"

            def write_marker() -> None:
                time.sleep(0.02)
                marker.parent.mkdir(parents=True)
                marker.write_text("{}", encoding="utf-8")

            writer = threading.Thread(target=write_marker)
            writer.start()
            output = builder.queue_watch_wait_for_event(
                queue_root=queue_root,
                timeout_seconds=0.5,
                event_driven=True,
            )
            writer.join()

        self.assertEqual(output["result"], "queue_changed")
        self.assertEqual(output["wait_result"], "event")


if __name__ == "__main__":
    unittest.main()
