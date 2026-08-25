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


class ScandirStub:
    def __init__(self, entries=(), *, error: OSError | None = None):
        self._entries = iter(entries)
        self._error = error
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        return next(self._entries)

    def close(self) -> None:
        self.closed = True


def current_codex_jsonl(*, include_message: bool = True) -> str:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "provider-thread"},
        {"type": "turn.started"},
        {"type": "error", "message": "top-level diagnostic only"},
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

    def test_parse_codex_whitespace_content_does_not_replace_last_nonempty_message(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final review result"},
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "content": "   "},
                },
            )
        )

        parsed = builder.parse_codex_json_output(stdout)

        self.assertEqual(parsed["result"], "final review result")

    def test_parse_codex_top_level_diagnostic_is_not_response_evidence(self) -> None:
        builder = load_builder_module()

        parsed = builder.parse_codex_json_output(current_codex_jsonl(include_message=False))

        self.assertNotIn("result", parsed)
        self.assertEqual(parsed["session_id"], "provider-thread")
        self.assertEqual(parsed["usage"]["output_tokens"], 7)

    def test_parse_codex_subtype_only_error_cannot_spoof_response_metadata(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "error",
                    "subtype": "error",
                    "message": "diagnostic only",
                    "session_id": "spoofed-session",
                    "request_id": "spoofed-request",
                    "model": "spoofed-model",
                    "usage": {"input_tokens": 999, "output_tokens": 999},
                },
                {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 7}},
            )
        )

        parsed = builder.parse_codex_json_output(stdout)

        self.assertNotIn("result", parsed)
        self.assertEqual(parsed["session_id"], "provider-thread")
        self.assertNotIn("request_id", parsed)
        self.assertNotIn("model", parsed)
        self.assertEqual(parsed["usage"], {"input_tokens": 11, "output_tokens": 7})

    def test_parse_codex_legacy_error_terminal_remains_fail_closed(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "result",
                    "subtype": "error",
                    "result": "diagnostic only",
                    "session_id": "spoofed-session",
                    "request_id": "spoofed-request",
                    "model": "spoofed-model",
                },
            )
        )

        parsed = builder.parse_codex_json_output(stdout)

        self.assertNotIn("result", parsed)
        self.assertEqual(parsed["session_id"], "provider-thread")
        self.assertNotIn("request_id", parsed)
        self.assertNotIn("model", parsed)

    def test_parse_codex_legacy_result_event_remains_supported(self) -> None:
        builder = load_builder_module()
        legacy_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "legacy final response",
                "session_id": "legacy-session",
                "usage": {"input_tokens": 3, "output_tokens": 2},
                "unexpected": "ignored",
            }
        )

        parsed = builder.parse_codex_json_output(legacy_stdout)

        self.assertEqual(parsed["result"], "legacy final response")
        self.assertEqual(parsed["session_id"], "legacy-session")
        self.assertEqual(parsed["usage"]["output_tokens"], 2)
        self.assertNotIn("unexpected", parsed)

    def test_parse_codex_legacy_string_metadata_requires_nonempty_strings(self) -> None:
        builder = load_builder_module()
        legacy_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "legacy final response",
                "session_id": {"nested": "bad"},
                "request_id": ["bad"],
                "model": True,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        )

        parsed = builder.parse_codex_json_output(legacy_stdout)

        self.assertEqual(parsed["result"], "legacy final response")
        self.assertNotIn("session_id", parsed)
        self.assertNotIn("request_id", parsed)
        self.assertNotIn("model", parsed)

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

    def test_provider_activate_without_agent_message_remains_fail_closed(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            builder.session_start_metadata_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "session", "cwd": "/tmp/project", "source": "startup"},
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
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "cwd": "/tmp/project",
                    },
                )

            state = json.loads((state_root / "session" / "bootstrap.json").read_text(encoding="utf-8"))

        self.assertEqual(output["decision"], "block")
        self.assertEqual(output["reason"], "codex provider activation produced no inference evidence")
        self.assertNotEqual(state["readiness_scope"], "response_evidence")

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

    def test_queue_snapshot_does_not_start_traversal_after_deadline(self) -> None:
        builder = load_builder_module()
        queue_root = Path("/tmp/queue")

        with mock.patch.object(builder.time, "monotonic", return_value=1.0), mock.patch.object(
            builder.os,
            "scandir",
        ) as scandir_mock:
            snapshot = builder.queue_watch_snapshot(queue_root, deadline=0.5)

        self.assertIsNone(snapshot)
        scandir_mock.assert_not_called()

    def test_queue_snapshot_stops_when_metadata_lookup_exhausts_deadline(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            queue_root.mkdir()
            marker = queue_root / "marker.json"
            marker.write_text("{}", encoding="utf-8")

            with mock.patch.object(
                builder.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.0, 0.0, 1.0],
            ):
                snapshot = builder.queue_watch_snapshot(queue_root, deadline=0.5)

        self.assertIsNone(snapshot)

    def test_queue_snapshot_returns_incomplete_on_iteration_or_metadata_error(self) -> None:
        builder = load_builder_module()
        queue_root = Path("/tmp/queue")

        class FailingEntry:
            path = "/tmp/queue/failing"

            def stat(self, *, follow_symlinks: bool = True):
                raise OSError("metadata failed")

        with self.subTest(error="scan-open"), mock.patch.object(
            builder.os,
            "scandir",
            side_effect=OSError("scan failed"),
        ):
            self.assertIsNone(builder.queue_watch_snapshot(queue_root))

        with self.subTest(error="iteration"), mock.patch.object(
            builder.os,
            "scandir",
            return_value=ScandirStub(error=OSError("iteration failed")),
        ):
            self.assertIsNone(builder.queue_watch_snapshot(queue_root))

        with self.subTest(error="metadata"), mock.patch.object(
            builder.os,
            "scandir",
            return_value=ScandirStub([FailingEntry()]),
        ):
            self.assertIsNone(builder.queue_watch_snapshot(queue_root))

    def test_queue_snapshot_returns_incomplete_when_missing_root_scan_exhausts_deadline(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(
            builder.time,
            "monotonic",
            side_effect=[0.0, 0.6],
        ), mock.patch.object(
            builder.os,
            "scandir",
            side_effect=FileNotFoundError("queue disappeared"),
        ):
            snapshot = builder.queue_watch_snapshot(Path("/tmp/queue"), deadline=0.5)

        self.assertIsNone(snapshot)

    def test_queue_snapshot_returns_incomplete_on_nested_scan_race(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            nested = queue_root / "nested"
            nested.mkdir(parents=True)
            (nested / "marker.json").write_text("{}", encoding="utf-8")
            real_scandir = builder.os.scandir

            def flaky_scandir(path):
                if Path(path) == nested:
                    raise OSError("nested scan failed")
                return real_scandir(path)

            with mock.patch.object(builder.os, "scandir", side_effect=flaky_scandir):
                snapshot = builder.queue_watch_snapshot(queue_root)

        self.assertIsNone(snapshot)

    def test_queue_snapshot_is_order_independent_and_skips_nonfiles(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            queue_root.mkdir()
            first = queue_root / "first.json"
            second = queue_root / "second.json"
            directory = queue_root / "directory"
            symlink = queue_root / "external-link"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            directory.mkdir()
            symlink.symlink_to(Path(tmp) / "external-target")

            with builder.os.scandir(queue_root) as scanner:
                entries = {entry.name: entry for entry in scanner}
            with mock.patch.object(
                builder.os,
                "scandir",
                side_effect=[
                    ScandirStub(
                        [
                            entries["first.json"],
                            entries["directory"],
                            entries["second.json"],
                            entries["external-link"],
                        ]
                    ),
                    ScandirStub(),
                    ScandirStub(
                        [
                            entries["external-link"],
                            entries["second.json"],
                            entries["directory"],
                            entries["first.json"],
                        ]
                    ),
                    ScandirStub(),
                    ScandirStub([entries["first.json"], entries["second.json"]]),
                ],
            ):
                forward = builder.queue_watch_snapshot(queue_root)
                reverse = builder.queue_watch_snapshot(queue_root)
                files_only = builder.queue_watch_snapshot(queue_root)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward, files_only)

    def test_event_wait_returns_typed_timeout_when_initial_snapshot_exhausts_deadline(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "queue_watch_snapshot",
            return_value=None,
        ) as snapshot_mock, mock.patch.object(
            builder.time,
            "monotonic",
            side_effect=[0.0, 0.0, 0.5],
        ), mock.patch.object(builder.time, "sleep") as sleep_mock:
            output = builder.queue_watch_wait_for_event(
                queue_root=Path(tmp) / "queue",
                timeout_seconds=0.5,
                event_driven=True,
            )

        self.assertEqual(output["result"], "timeout")
        self.assertEqual(output["wait_result"], "timeout")
        self.assertTrue(output["event_driven"])
        self.assertIsNotNone(snapshot_mock.call_args.kwargs["deadline"])
        sleep_mock.assert_called_once_with(0.5)

    def test_event_wait_returns_typed_timeout_when_later_snapshot_is_incomplete(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "queue_watch_snapshot",
            side_effect=["initial", None],
        ) as snapshot_mock, mock.patch.object(
            builder.time,
            "monotonic",
            side_effect=[0.0, 0.0, 0.05, 0.5],
        ), mock.patch.object(builder.time, "sleep") as sleep_mock:
            output = builder.queue_watch_wait_for_event(
                queue_root=Path(tmp) / "queue",
                timeout_seconds=0.5,
                event_driven=True,
            )

        self.assertEqual(output["result"], "timeout")
        self.assertEqual(output["wait_result"], "timeout")
        self.assertEqual(snapshot_mock.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertAlmostEqual(sleep_mock.call_args_list[0].args[0], 0.05)
        self.assertAlmostEqual(sleep_mock.call_args_list[1].args[0], 0.45)

    def test_event_wait_nested_scan_race_times_out_without_false_change(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            nested = queue_root / "nested"
            nested.mkdir(parents=True)
            (nested / "marker.json").write_text("{}", encoding="utf-8")
            real_scandir = builder.os.scandir
            root_scans = 0

            def flaky_second_snapshot(path):
                nonlocal root_scans
                if Path(path) == queue_root:
                    root_scans += 1
                if Path(path) == nested and root_scans >= 2:
                    raise OSError("nested scan failed")
                return real_scandir(path)

            with mock.patch.object(
                builder.os,
                "scandir",
                side_effect=flaky_second_snapshot,
            ), mock.patch.object(builder.time, "sleep"):
                output = builder.queue_watch_wait_for_event(
                    queue_root=queue_root,
                    timeout_seconds=0.5,
                    event_driven=True,
                )

        self.assertEqual(output["result"], "timeout")
        self.assertEqual(output["wait_result"], "timeout")
        self.assertGreaterEqual(root_scans, 2)

    def test_event_wait_root_disappearance_after_deadline_is_typed_timeout(self) -> None:
        builder = load_builder_module()
        clock = {"now": 0.0}
        scan_count = 0

        def disappearing_root(path):
            nonlocal scan_count
            scan_count += 1
            if scan_count == 1:
                return ScandirStub()
            clock["now"] = 0.6
            raise FileNotFoundError(path)

        with mock.patch.object(
            builder.time,
            "monotonic",
            side_effect=lambda: clock["now"],
        ), mock.patch.object(builder.time, "sleep"), mock.patch.object(
            builder.os,
            "scandir",
            side_effect=disappearing_root,
        ):
            output = builder.queue_watch_wait_for_event(
                queue_root=Path("/tmp/queue"),
                timeout_seconds=0.5,
                event_driven=True,
            )

        self.assertEqual(output["result"], "timeout")
        self.assertEqual(output["wait_result"], "timeout")
        self.assertEqual(scan_count, 2)

    def test_event_wait_rechecks_deadline_before_snapshot_comparison(self) -> None:
        builder = load_builder_module()
        clock = {"now": 0.0}
        snapshots = iter(["initial", "changed"])

        def snapshot_after_deadline(*args, **kwargs):
            snapshot = next(snapshots)
            if snapshot == "changed":
                clock["now"] = 0.6
            return snapshot

        with mock.patch.object(
            builder.time,
            "monotonic",
            side_effect=lambda: clock["now"],
        ), mock.patch.object(builder.time, "sleep"), mock.patch.object(
            builder,
            "queue_watch_snapshot",
            side_effect=snapshot_after_deadline,
        ):
            output = builder.queue_watch_wait_for_event(
                queue_root=Path("/tmp/queue"),
                timeout_seconds=0.5,
                event_driven=True,
            )

        self.assertEqual(output["result"], "timeout")
        self.assertEqual(output["wait_result"], "timeout")

    def test_zero_timeout_does_not_traverse_queue(self) -> None:
        builder = load_builder_module()
        with mock.patch.object(builder.os, "scandir") as scandir_mock:
            output = builder.queue_watch_wait_for_event(
                queue_root=Path("/tmp/queue"),
                timeout_seconds=0.0,
                event_driven=True,
            )

        self.assertEqual(output["result"], "timeout")
        self.assertEqual(output["wait_result"], "timeout")
        scandir_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
