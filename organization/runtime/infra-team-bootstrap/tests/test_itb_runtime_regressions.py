from __future__ import annotations

import importlib.util
import json
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


def current_codex_jsonl(
    *,
    include_message: bool = True,
    include_thread_started: bool = True,
    include_turn_completed: bool = True,
) -> str:
    events: list[dict[str, object]] = []
    if include_thread_started:
        events.append(
            {"type": "thread.started", "thread_id": "provider-thread", "model": "gpt-5.6-sol"}
        )
    events.extend(
        [
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
    )
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
    if include_turn_completed:
        events.append(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 11,
                    "cached_input_tokens": 5,
                    "cache_write_input_tokens": 2,
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
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["usage"]["input_tokens"], 11)
        self.assertEqual(parsed["usage"]["cached_input_tokens"], 5)
        self.assertEqual(parsed["usage"]["cache_write_input_tokens"], 2)
        self.assertEqual(parsed["usage"]["output_tokens"], 7)
        self.assertEqual(parsed["usage"]["reasoning_output_tokens"], 3)
        self.assertEqual(parsed["num_turns"], 1)

    def test_parse_codex_requires_current_terminal_metadata(self) -> None:
        builder = load_builder_module()
        for label, kwargs in (
            ("thread_started", {"include_thread_started": False}),
            ("turn_completed", {"include_turn_completed": False}),
        ):
            with self.subTest(missing=label):
                parsed = builder.parse_codex_json_output(current_codex_jsonl(**kwargs))

                self.assertNotIn("result", parsed)

    def test_parse_codex_last_completed_whitespace_message_clears_prior_text(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "item.completed",
                    "item": {"id": "item-first", "type": "agent_message", "text": "final review result"},
                },
                {
                    "type": "item.completed",
                    "item": {"id": "item-last", "type": "agent_message", "content": "   "},
                },
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )

        parsed = builder.parse_codex_json_output(stdout)

        self.assertNotIn("result", parsed)
        self.assertEqual(parsed["session_id"], "provider-thread")
        self.assertEqual(parsed["usage"], {"output_tokens": 1})

    def test_parse_codex_rejects_malformed_or_nonterminal_current_streams(self) -> None:
        builder = load_builder_module()
        malformed_usage = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": {"output_tokens": "1"}},
            )
        )
        self.assertEqual(builder.parse_codex_json_output(malformed_usage), {})
        for label, event in (
            (
                "agent_message",
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "late response"}},
            ),
            ("error", {"type": "error", "message": "late diagnostic"}),
            ("unknown", {"type": "future.event", "message": "late unknown"}),
        ):
            with self.subTest(post_terminal=label):
                stdout = current_codex_jsonl() + json.dumps(event)

                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        for label, event in (
            ("error", {"type": "error", "message": "diagnostic"}),
            ("unknown", {"type": "future.event", "message": "unknown"}),
            ("padded_error", {"type": " error ", "message": "diagnostic"}),
        ):
            with self.subTest(pre_terminal=label):
                stdout = "\n".join(
                    json.dumps(item)
                    for item in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        event,
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                )

                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        current_error_and_legacy_success = "\n".join(
            (
                json.dumps({"type": "error", "message": "current diagnostic"}),
                json.dumps({"type": "result", "subtype": "success", "result": "legacy response"}),
            )
        )
        self.assertEqual(builder.parse_codex_json_output(current_error_and_legacy_success), {})

    def test_parse_codex_rejects_ambiguous_json_discriminators_and_status(self) -> None:
        builder = load_builder_module()
        duplicate_top_level_type = (
            '{"type":"error","type":"result","subtype":"success","result":"legacy promoted"}'
        )
        duplicate_nested_thread_id = "\n".join(
            (
                '{"type":"thread.started","thread_id":"one","thread_id":"two"}',
                json.dumps({"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}}),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
            )
        )
        duplicate_nested_usage = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "provider-thread"}),
                json.dumps({"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}}),
                '{"type":"turn.completed","usage":{"output_tokens":1,"output_tokens":1}}',
            )
        )
        for label, stdout in (
            ("duplicate_top_level_type", duplicate_top_level_type),
            ("duplicate_nested_thread_id", duplicate_nested_thread_id),
            ("duplicate_nested_usage", duplicate_nested_usage),
        ):
            with self.subTest(duplicate_key=label):
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        for label, is_error, subtype in (
            ("non_boolean", "true", "success"),
            ("success_contradiction", True, "success"),
            ("error_contradiction", False, "error"),
            ("null_subtype", False, None),
            ("empty_subtype", False, ""),
        ):
            with self.subTest(legacy_status=label):
                stdout = json.dumps(
                    {
                        "type": "result",
                        "subtype": subtype,
                        "is_error": is_error,
                        "result": "legacy promoted",
                    }
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        padded_thread = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "`thread.started`", "thread_id": "provider-thread"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )
        padded_item = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "item.completed",
                    "item": {"id": "item-malformed", "type": "`agent_message`", "text": "response"},
                },
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )
        padded_legacy = json.dumps(
            {"type": " result ", "subtype": "success", "result": "legacy promoted"}
        )
        padded_subtype = json.dumps(
            {"type": "result", "subtype": " success ", "result": "legacy promoted"}
        )

        self.assertEqual(builder.parse_codex_json_output(padded_thread), {})
        self.assertNotIn("result", builder.parse_codex_json_output(padded_item))
        self.assertEqual(builder.parse_codex_json_output(padded_legacy), {})
        self.assertEqual(builder.parse_codex_json_output(padded_subtype), {})

    def test_parse_codex_rejects_cross_event_identity_and_text_alias_conflicts(self) -> None:
        builder = load_builder_module()
        for label, terminal_event in (
            (
                "thread_id",
                {
                    "type": "turn.completed",
                    "thread_id": "other-thread",
                    "usage": {"output_tokens": 1},
                },
            ),
            (
                "model",
                {
                    "type": "turn.completed",
                    "model": "gpt-5.5",
                    "usage": {"output_tokens": 1},
                },
            ),
        ):
            with self.subTest(terminal_identity=label):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "thread.started",
                            "thread_id": "provider-thread",
                            "model": "gpt-5.6-sol",
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        terminal_event,
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        current_text_conflict = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-agent",
                        "type": "agent_message",
                        "result": "first response",
                        "text": "second response",
                    },
                },
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )
        legacy_text_conflict = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "first response",
                "message": "second response",
            }
        )

        self.assertEqual(builder.parse_codex_json_output(current_text_conflict), {})
        self.assertEqual(builder.parse_codex_json_output(legacy_text_conflict), {})

    def test_parse_codex_bounds_adversarial_jsonl_resources(self) -> None:
        builder = load_builder_module()
        huge_usage = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "provider-thread"}),
                json.dumps({"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}}),
                (
                    '{"type":"turn.completed","usage":{"output_tokens":'
                    + ("9" * 1000)
                    + "}}"
                ),
            )
        )
        integer_over_interpreter_limit = (
            '{"type":"turn.completed","usage":{"output_tokens":'
            + ("9" * 5000)
            + "}}"
        )
        legacy_huge_usage = (
            '{"type":"result","subtype":"success","result":"legacy response",'
            '"usage":{"output_tokens":'
            + ("9" * 1000)
            + "}}"
        )
        legacy_huge_metric = (
            '{"type":"result","subtype":"success","result":"legacy response","num_turns":'
            + ("9" * 1000)
            + "}"
        )
        legacy_conflicting_metric_aliases = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "legacy response",
                "num_turns": 1,
                "numTurns": 2,
            }
        )

        self.assertEqual(builder.parse_codex_json_output(huge_usage), {})
        self.assertEqual(builder.parse_codex_json_output(integer_over_interpreter_limit), {})
        self.assertEqual(builder.parse_codex_json_output(legacy_huge_usage), {})
        self.assertEqual(builder.parse_codex_json_output(legacy_huge_metric), {})
        self.assertEqual(builder.parse_codex_json_output(legacy_conflicting_metric_aliases), {})
        for constant in ("NaN", "Infinity", "-Infinity", "1e9999"):
            with self.subTest(nonfinite=constant):
                stdout = "\n".join(
                    (
                        (
                            '{"type":"thread.started","thread_id":"provider-thread",'
                            f'"unused":{constant}'
                            "}"
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                            }
                        ),
                        json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})
        legacy_nonfinite = (
            '{"type":"result","subtype":"success","result":"legacy response","unused":NaN}'
        )
        self.assertEqual(builder.parse_codex_json_output(legacy_nonfinite), {})
        with mock.patch.object(builder, "CODEX_JSONL_MAX_CHARS", 10):
            self.assertEqual(builder.parse_codex_json_output(" " * 11), {})
        with mock.patch.object(builder, "CODEX_JSONL_MAX_LINE_CHARS", 10):
            self.assertEqual(builder.parse_codex_json_output('{"type":"error"}'), {})
        with mock.patch.object(builder, "CODEX_JSONL_MAX_EVENTS", 1):
            self.assertEqual(builder.parse_codex_json_output("{}\n{}"), {})
        nested_value: object = 0
        for _ in range(5):
            nested_value = [nested_value]
        nested_event = json.dumps(
            {"type": "thread.started", "thread_id": "provider-thread", "unused": nested_value}
        )
        with mock.patch.object(builder, "CODEX_JSON_MAX_DEPTH", 3):
            self.assertEqual(builder.parse_codex_json_output(nested_event), {})
        wide_event = json.dumps(
            {"type": "thread.started", "thread_id": "provider-thread", "unused": [0, 1, 2, 3]}
        )
        with mock.patch.object(builder, "CODEX_JSON_MAX_NODES", 4):
            self.assertEqual(builder.parse_codex_json_output(wide_event), {})

    def test_bounded_output_runner_caps_provider_streams_and_rejects_invalid_utf8(self) -> None:
        builder = load_builder_module()
        for stream_name, fd, limit in (("stdout", 1, 257), ("stderr", 2, 129)):
            with self.subTest(stream=stream_name):
                completed = builder.run_command_with_bounded_output(
                    [
                        sys.executable,
                        "-c",
                        f"import os; os.write({fd}, b'x' * 65536)",
                    ],
                    timeout=5,
                    stdout_limit_bytes=limit if stream_name == "stdout" else 1024,
                    stderr_limit_bytes=limit if stream_name == "stderr" else 1024,
                )
                rejection_type, _ = builder.codex_bounded_output_rejection(completed)
                self.assertEqual(rejection_type, "provider_output_limit_exceeded")
                self.assertIn(stream_name, completed.output_limit_exceeded)
                captured = completed.stdout if stream_name == "stdout" else completed.stderr
                self.assertLessEqual(len(captured.encode("utf-8")), limit)

        invalid_utf8 = builder.run_command_with_bounded_output(
            [sys.executable, "-c", "import os; os.write(1, bytes([255]))"],
            timeout=5,
            stdout_limit_bytes=16,
            stderr_limit_bytes=16,
        )
        rejection_type, rejection_reason = builder.codex_bounded_output_rejection(invalid_utf8)
        self.assertEqual(rejection_type, "provider_output_decode_failed")
        self.assertIn("UTF-8", rejection_reason)

        started = time.monotonic()
        timed_out = builder.run_command_with_bounded_output(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout=0.05,
            stdout_limit_bytes=16,
            stderr_limit_bytes=16,
        )
        rejection_type, rejection_reason = builder.codex_bounded_output_rejection(timed_out)
        self.assertEqual(rejection_type, "provider_response_timeout")
        self.assertIn("timeout", rejection_reason)
        self.assertLess(time.monotonic() - started, 1.0)

        for select_error in (OSError("fd pressure"), ValueError("invalid descriptor")):
            with self.subTest(multiplexer_error=type(select_error).__name__), mock.patch.object(
                builder._select,
                "select",
                side_effect=select_error,
            ):
                started = time.monotonic()
                completed = builder.run_command_with_bounded_output(
                    [sys.executable, "-c", "import time; time.sleep(2)"],
                    timeout=5,
                    stdout_limit_bytes=16,
                    stderr_limit_bytes=16,
                )
                rejection_type, rejection_reason = builder.codex_bounded_output_rejection(completed)
                self.assertEqual(rejection_type, "provider_output_read_failed")
                self.assertIn(type(select_error).__name__, rejection_reason)
                self.assertEqual(completed.returncode, -9)
                self.assertLess(time.monotonic() - started, 1.0)

        select_calls = 0

        def invalid_utf8_then_select_error(readers, _writers, _errors, _timeout):
            nonlocal select_calls
            select_calls += 1
            if select_calls == 1:
                return readers[:1], [], []
            raise OSError("fd pressure after partial read")

        with mock.patch.object(
            builder._select,
            "select",
            side_effect=invalid_utf8_then_select_error,
        ):
            combined_failure = builder.run_command_with_bounded_output(
                [
                    sys.executable,
                    "-c",
                    "import os,time; os.write(1, bytes([255])); time.sleep(2)",
                ],
                timeout=5,
                stdout_limit_bytes=16,
                stderr_limit_bytes=16,
            )
        rejection_type, rejection_reason = builder.codex_bounded_output_rejection(
            combined_failure
        )
        self.assertTrue(combined_failure.output_decode_error)
        self.assertEqual(combined_failure.output_read_error, "multiplexer: OSError")
        self.assertEqual(rejection_type, "provider_output_read_failed")
        self.assertIn("multiplexer: OSError", rejection_reason)

        long_process_output = subprocess.CompletedProcess(
            args=["codex"],
            returncode=1,
            stdout="x" * (builder.CODEX_EVIDENCE_NOTE_MAX_CHARS + 100),
            stderr="",
        )
        note = builder.bounded_provider_process_note(long_process_output)
        self.assertEqual(len(note), builder.CODEX_EVIDENCE_NOTE_MAX_CHARS)
        self.assertTrue(note.endswith("... [truncated]"))
        exact_process_output = subprocess.CompletedProcess(
            args=["codex"],
            returncode=1,
            stdout="x" * builder.CODEX_EVIDENCE_NOTE_MAX_CHARS,
            stderr="",
        )
        self.assertEqual(
            builder.bounded_provider_process_note(exact_process_output),
            exact_process_output.stdout,
        )

    def test_bounded_output_runner_returns_typed_timeout_when_final_reap_times_out(self) -> None:
        builder = load_builder_module()

        class UnreapableProcess:
            pid = 42424242
            returncode = None

            def __init__(self, stdout, stderr) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.wait_timeouts: list[float | None] = []

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(cmd=["codex"], timeout=timeout)

        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = UnreapableProcess(stdout, stderr)
            with mock.patch.object(
                builder.subprocess,
                "Popen",
                return_value=process,
            ), mock.patch.object(builder, "terminate_process_group") as terminate_mock:
                completed = builder.run_command_with_bounded_output(
                    ["codex", "exec"],
                    timeout=0.01,
                    stdout_limit_bytes=16,
                    stderr_limit_bytes=16,
                )

        rejection_type, rejection_reason = builder.codex_bounded_output_rejection(completed)
        self.assertEqual(len(process.wait_timeouts), 2)
        self.assertGreaterEqual(process.wait_timeouts[0], 0.0)
        self.assertLessEqual(process.wait_timeouts[0], 0.01)
        self.assertEqual(process.wait_timeouts[1], 1.0)
        terminate_mock.assert_called_once_with(process)
        self.assertEqual(completed.returncode, -builder.signal.SIGKILL)
        self.assertTrue(completed.output_timed_out)
        self.assertEqual(rejection_type, "provider_response_timeout")
        self.assertIn("timeout", rejection_reason)

    def test_bounded_output_runner_preserves_prior_rejection_when_reap_recovers(self) -> None:
        builder = load_builder_module()

        class ReapedAfterKillProcess:
            pid = 42424242
            returncode = None

            def __init__(self, stdout, stderr) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.wait_timeouts: list[float | None] = []

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                if len(self.wait_timeouts) == 1:
                    raise subprocess.TimeoutExpired(cmd=["codex"], timeout=timeout)
                self.returncode = -builder.signal.SIGKILL
                return self.returncode

        with self.subTest(rejection="output-limit"), tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            stdout.write(b"x" * 32)
            stdout.seek(0)
            process = ReapedAfterKillProcess(stdout, stderr)
            with mock.patch.object(
                builder.subprocess,
                "Popen",
                return_value=process,
            ), mock.patch.object(builder, "terminate_process_group"):
                completed = builder.run_command_with_bounded_output(
                    ["codex", "exec"],
                    timeout=1.0,
                    stdout_limit_bytes=4,
                    stderr_limit_bytes=4,
                )

            rejection_type, _ = builder.codex_bounded_output_rejection(completed)
            self.assertEqual(rejection_type, "provider_output_limit_exceeded")
            self.assertFalse(completed.output_timed_out)
            self.assertEqual(len(process.wait_timeouts), 2)
            self.assertEqual(process.wait_timeouts[1], 1.0)

        with self.subTest(rejection="read-error"), tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = ReapedAfterKillProcess(stdout, stderr)
            with mock.patch.object(
                builder.subprocess,
                "Popen",
                return_value=process,
            ), mock.patch.object(
                builder._select,
                "select",
                side_effect=OSError("forced read failure"),
            ), mock.patch.object(builder, "terminate_process_group"):
                completed = builder.run_command_with_bounded_output(
                    ["codex", "exec"],
                    timeout=1.0,
                    stdout_limit_bytes=4,
                    stderr_limit_bytes=4,
                )

            rejection_type, _ = builder.codex_bounded_output_rejection(completed)
            self.assertEqual(rejection_type, "provider_output_read_failed")
            self.assertFalse(completed.output_timed_out)
            self.assertEqual(len(process.wait_timeouts), 2)
            self.assertEqual(process.wait_timeouts[1], 1.0)

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

        self.assertEqual(parsed, {})

    def test_parse_codex_legacy_error_terminal_remains_fail_closed(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
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
        self.assertNotIn("session_id", parsed)
        self.assertNotIn("request_id", parsed)
        self.assertNotIn("model", parsed)

    def test_parse_codex_rejects_conflicting_thread_started_metadata(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "thread-one", "model": "gpt-5.6-sol"},
                {"type": "thread.started", "thread_id": "thread-two", "model": "gpt-5.5"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )

        self.assertEqual(builder.parse_codex_json_output(stdout), {})

    def test_parse_codex_rejects_duplicate_thread_started_with_matching_identity(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread", "model": "gpt-5.6-sol"},
                {"type": "thread.started", "thread_id": "provider-thread", "model": "gpt-5.6-sol"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )

        self.assertEqual(builder.parse_codex_json_output(stdout), {})

    def test_parse_codex_validates_nested_item_discriminators(self) -> None:
        builder = load_builder_module()
        malformed_events = (
            ("missing_item", {"type": "item.completed"}),
            ("non_object_item", {"type": "item.completed", "item": []}),
            ("missing_type", {"type": "item.completed", "item": {"id": "item-malformed"}}),
            ("null_type", {"type": "item.completed", "item": {"id": "item-malformed", "type": None}}),
            ("empty_type", {"type": "item.completed", "item": {"id": "item-malformed", "type": ""}}),
            (
                "padded_type",
                {"type": "item.completed", "item": {"id": "item-malformed", "type": " agent_message "}},
            ),
            (
                "unknown_type",
                {"type": "item.completed", "item": {"id": "item-malformed", "type": "future_item"}},
            ),
            ("missing_id", {"type": "item.completed", "item": {"type": "reasoning"}}),
            ("null_id", {"type": "item.completed", "item": {"id": None, "type": "reasoning"}}),
            ("empty_id", {"type": "item.completed", "item": {"id": "", "type": "reasoning"}}),
            ("padded_id", {"type": "item.completed", "item": {"id": " item ", "type": "reasoning"}}),
            ("non_string_id", {"type": "item.completed", "item": {"id": 1, "type": "reasoning"}}),
            (
                "missing_agent_message_text",
                {"type": "item.completed", "item": {"id": "item-no-text", "type": "agent_message"}},
            ),
            (
                "unknown_started",
                {"type": "item.started", "item": {"id": "item-malformed", "type": "future_item"}},
            ),
            (
                "unknown_updated",
                {"type": "item.updated", "item": {"id": "item-malformed", "type": "future_item"}},
            ),
        )
        for label, malformed_event in malformed_events:
            with self.subTest(malformed_item=label):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        malformed_event,
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        for item_type in sorted(builder.CODEX_CURRENT_ITEM_TYPES - {"agent_message"}):
            with self.subTest(valid_non_message_item=item_type):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": f"item-{item_type}", "type": item_type},
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout)["result"], "response")

    def test_parse_codex_enforces_current_item_lifecycle(self) -> None:
        builder = load_builder_module()
        invalid_sequences = (
            (
                "started_agent_message",
                [
                    {
                        "type": "item.started",
                        "item": {"id": "item-message", "type": "agent_message", "text": "hidden"},
                    }
                ],
            ),
            (
                "updated_agent_message",
                [
                    {
                        "type": "item.updated",
                        "item": {"id": "item-message", "type": "agent_message", "text": "hidden"},
                    }
                ],
            ),
            (
                "duplicate_start",
                [
                    {"type": "item.started", "item": {"id": "item-command", "type": "command_execution"}},
                    {"type": "item.started", "item": {"id": "item-command", "type": "command_execution"}},
                    {"type": "item.completed", "item": {"id": "item-command", "type": "command_execution"}},
                ],
            ),
            (
                "orphan_update",
                [{"type": "item.updated", "item": {"id": "item-todo", "type": "todo_list"}}],
            ),
            (
                "type_change",
                [
                    {"type": "item.started", "item": {"id": "item-tool", "type": "command_execution"}},
                    {"type": "item.completed", "item": {"id": "item-tool", "type": "mcp_tool_call"}},
                ],
            ),
            (
                "duplicate_completion",
                [
                    {"type": "item.completed", "item": {"id": "item-reasoning", "type": "reasoning"}},
                    {"type": "item.completed", "item": {"id": "item-reasoning", "type": "reasoning"}},
                ],
            ),
            (
                "post_completion_update",
                [
                    {"type": "item.completed", "item": {"id": "item-todo", "type": "todo_list"}},
                    {"type": "item.updated", "item": {"id": "item-todo", "type": "todo_list"}},
                ],
            ),
            (
                "unfinished_at_turn_completion",
                [{"type": "item.started", "item": {"id": "item-command", "type": "command_execution"}}],
            ),
        )
        for label, lifecycle_events in invalid_sequences:
            with self.subTest(lifecycle=label):
                events = [
                    {"type": "thread.started", "thread_id": "provider-thread"},
                    *lifecycle_events,
                    {
                        "type": "item.completed",
                        "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                    },
                    {"type": "turn.completed", "usage": {"output_tokens": 1}},
                ]
                self.assertEqual(
                    builder.parse_codex_json_output(
                        "\n".join(json.dumps(event) for event in events)
                    ),
                    {},
                )

        valid_events = (
            {"type": "thread.started", "thread_id": "provider-thread"},
            {"type": "item.started", "item": {"id": "item-command", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": "item-command", "type": "command_execution"}},
            {"type": "item.started", "item": {"id": "item-todo", "type": "todo_list"}},
            {"type": "item.updated", "item": {"id": "item-todo", "type": "todo_list"}},
            {"type": "item.completed", "item": {"id": "item-todo", "type": "todo_list"}},
            {
                "type": "item.completed",
                "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
            },
            {"type": "turn.completed", "usage": {"output_tokens": 1}},
        )
        parsed = builder.parse_codex_json_output(
            "\n".join(json.dumps(event) for event in valid_events)
        )
        self.assertEqual(parsed["result"], "response")
        self.assertEqual(
            builder.CODEX_CURRENT_STARTED_ITEM_TYPES,
            {"command_execution", "mcp_tool_call", "collab_tool_call", "web_search", "todo_list"},
        )
        self.assertEqual(builder.CODEX_CURRENT_UPDATED_ITEM_TYPES, {"todo_list"})

    def test_parse_codex_rejects_missing_text_after_valid_agent_message(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "item.completed",
                    "item": {"id": "item-first", "type": "agent_message", "text": "intermediate"},
                },
                {
                    "type": "item.completed",
                    "item": {"id": "item-last", "type": "agent_message"},
                },
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )

        self.assertEqual(builder.parse_codex_json_output(stdout), {})

    def test_parse_codex_rejects_mixed_current_and_legacy_streams(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread", "model": "gpt-5.6-sol"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "current response"}},
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
                {"type": "result", "subtype": "success", "result": "legacy response", "model": "gpt-5.5"},
            )
        )

        self.assertEqual(builder.parse_codex_json_output(stdout), {})

    def test_parse_codex_rejects_unsafe_model_identifier(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread", "model": "gpt model with spaces"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )

        parsed = builder.parse_codex_json_output(stdout)

        self.assertEqual(parsed, {})

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

        self.assertEqual(parsed, {})

    def test_parse_codex_legacy_model_requires_bounded_safe_identifier(self) -> None:
        builder = load_builder_module()
        for model in ("bad model", "m" * 129):
            with self.subTest(model=model):
                parsed = builder.parse_codex_json_output(
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "result": "legacy final response",
                            "session_id": "legacy-session",
                            "model": model,
                        }
                    )
                )

                self.assertEqual(parsed, {})

    def test_parse_codex_rejects_duplicate_legacy_terminals(self) -> None:
        builder = load_builder_module()
        stdout = "\n".join(
            json.dumps(event)
            for event in (
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "first response",
                    "session_id": "legacy-session",
                    "model": "gpt-5.6-sol",
                },
                {"type": "result", "subtype": "success", "result": "second response"},
            )
        )

        self.assertEqual(builder.parse_codex_json_output(stdout), {})

    def test_parse_codex_rejects_conflicting_thread_aliases(self) -> None:
        builder = load_builder_module()
        events = (
            {
                "type": "thread.started",
                "thread_id": "thread-one",
                "threadId": "thread-two",
                "model": "gpt-5.6-sol",
            },
            {
                "type": "thread.started",
                "thread_id": "thread-one",
                "model": "gpt-5.6-sol",
                "effectiveModel": "gpt-5.5",
            },
        )
        for event in events:
            with self.subTest(event=event):
                stdout = "\n".join(
                    json.dumps(item)
                    for item in (
                        event,
                        {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                )

                self.assertEqual(builder.parse_codex_json_output(stdout), {})

    def test_parse_codex_rejects_non_string_identity_aliases(self) -> None:
        builder = load_builder_module()
        current_stdout = "\n".join(
            json.dumps(event)
            for event in (
                {
                    "type": "thread.started",
                    "thread_id": "provider-thread",
                    "threadId": {},
                    "model": "gpt-5.6-sol",
                },
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": {"output_tokens": 1}},
            )
        )
        legacy_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "legacy response",
                "session_id": {},
                "request_id": [],
                "model": "gpt-5.6-sol",
            }
        )

        self.assertEqual(builder.parse_codex_json_output(current_stdout), {})
        self.assertEqual(builder.parse_codex_json_output(legacy_stdout), {})

    def test_parse_codex_rejects_present_null_empty_or_conflicting_aliases(self) -> None:
        builder = load_builder_module()
        start_events = (
            {"type": "thread.started", "thread_id": "provider-thread", "threadId": None},
            {"type": "thread.started", "thread_id": "provider-thread", "threadId": ""},
            {"type": "thread.started", "thread_id": "provider-thread", "model": None},
            {"type": "thread.started", "thread_id": "provider-thread", "model": ""},
            {"type": "thread.started", "thread_id": " provider-thread"},
            {"type": "thread.started", "thread_id": "provider-thread", "model": "gpt-5.6-sol "},
            {
                "type": "thread.started",
                "thread_id": "provider-thread",
                "model": "gpt-5.6-sol",
                "effectiveModel": None,
            },
        )
        for start_event in start_events:
            with self.subTest(start_alias=start_event):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        start_event,
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        terminal_events = (
            {"type": "turn.completed", "threadId": None, "usage": {"output_tokens": 1}},
            {"type": "turn.completed", "model": None, "usage": {"output_tokens": 1}},
            {"type": "turn.completed", "model": "", "usage": {"output_tokens": 1}},
        )
        for terminal_event in terminal_events:
            with self.subTest(terminal_alias=terminal_event):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        terminal_event,
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        current_alias_cases = (
            {
                "type": "item.completed",
                "item": {"id": "item-agent", "type": "agent_message", "text": "response", "result": None},
            },
            {
                "type": "item.completed",
                "item": {"id": "item-agent", "type": "agent_message", "text": "response", "content": None},
            },
            {
                "type": "item.completed",
                "item": {"id": "item-agent", "type": "agent_message", "text": "response", "result": ""},
            },
        )
        for message_event in current_alias_cases:
            with self.subTest(current_text_alias=message_event):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        message_event,
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        legacy_cases = (
            {"session_id": "legacy-session", "sessionId": None},
            {"request_id": "legacy-request", "requestId": None},
            {"model": None},
            {"session_id": " legacy-session"},
            {"request_id": "legacy-request "},
            {"model": " gpt-5.6-sol"},
            {"result": "legacy response", "message": None},
            {"result": "legacy response", "content": None},
            {"num_turns": 1, "numTurns": None},
            {"duration_api_ms": None},
        )
        for aliases in legacy_cases:
            with self.subTest(legacy_alias=aliases):
                event = {"type": "result", "subtype": "success", "result": "legacy response"}
                event.update(aliases)
                self.assertEqual(builder.parse_codex_json_output(json.dumps(event)), {})

    def test_parse_codex_rejects_internally_inconsistent_usage(self) -> None:
        builder = load_builder_module()
        invalid_usage_values = (
            {"input_tokens": 10, "output_tokens": 10, "total_tokens": 1},
            {"input_tokens": 10, "output_tokens": 10, "total_tokens": 999},
            {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 1},
            {"input_tokens": 10, "cache_write_input_tokens": 11, "output_tokens": 1},
            {
                "input_tokens": 10,
                "cached_input_tokens": 8,
                "cache_write_input_tokens": 8,
                "output_tokens": 1,
            },
            {"input_tokens": 1, "output_tokens": 10, "reasoning_output_tokens": 11},
            {"cached_input_tokens": 1},
            {"cache_write_input_tokens": 1},
            {"reasoning_output_tokens": 1},
            {"input_tokens": 10, "total_tokens": 9},
            {"output_tokens": 10, "total_tokens": 9},
        )
        for usage in invalid_usage_values:
            with self.subTest(usage=usage):
                stdout = "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": usage},
                    )
                )
                self.assertEqual(builder.parse_codex_json_output(stdout), {})

        valid_usage = {
            "input_tokens": 10,
            "cached_input_tokens": 5,
            "cache_write_input_tokens": 2,
            "output_tokens": 4,
            "reasoning_output_tokens": 3,
            "total_tokens": 14,
        }
        valid_stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}},
                {"type": "turn.completed", "usage": valid_usage},
            )
        )

        self.assertEqual(builder.parse_codex_json_output(valid_stdout)["usage"], valid_usage)

        boundary_usage = {
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "cache_write_input_tokens": 6,
            "output_tokens": 1,
            "total_tokens": 11,
        }
        boundary_stdout = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "provider-thread"},
                {
                    "type": "item.completed",
                    "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                },
                {"type": "turn.completed", "usage": boundary_usage},
            )
        )
        self.assertEqual(
            builder.parse_codex_json_output(boundary_stdout)["usage"],
            boundary_usage,
        )

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
                builder,
                "run_command_with_bounded_output",
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
        self.assertEqual(dispatch["effective_model"], "gpt-5.6-sol")
        self.assertEqual(dispatch["input_tokens"], 11)
        self.assertEqual(dispatch["output_tokens"], 7)

    def test_provider_activate_uses_thread_started_effective_model(self) -> None:
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
                stdout=current_codex_jsonl(),
                stderr="",
            )

            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
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

        self.assertEqual(output["activation"]["effective_model"], "gpt-5.6-sol")
        self.assertEqual(output["activation"]["session_id"], "provider-thread")

    def test_codex_consumers_require_current_terminal_metadata(self) -> None:
        builder = load_builder_module()
        for rejected_case, stdout in (
            ("missing_thread_started", current_codex_jsonl(include_thread_started=False)),
            ("missing_turn_completed", current_codex_jsonl(include_turn_completed=False)),
            (
                "post_terminal_error",
                current_codex_jsonl() + json.dumps({"type": "error", "message": "late diagnostic"}),
            ),
            (
                "post_terminal_unknown",
                current_codex_jsonl() + json.dumps({"type": "future.event", "message": "late unknown"}),
            ),
            (
                "pre_terminal_error",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": "error", "message": "diagnostic"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "pre_terminal_unknown",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": "future.event", "message": "unknown"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "pre_terminal_padded_error",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": " error ", "message": "diagnostic"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "nonfinite_current_json",
                "\n".join(
                    (
                        '{"type":"thread.started","thread_id":"provider-thread","unused":NaN}',
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                            }
                        ),
                        json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
                    )
                ),
            ),
            (
                "legacy_null_subtype",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": None,
                        "is_error": False,
                        "result": "legacy promoted",
                    }
                ),
            ),
            (
                "terminal_thread_identity_mismatch",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "thread.started",
                            "thread_id": "provider-thread",
                            "model": "gpt-5.6-sol",
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {
                            "type": "turn.completed",
                            "thread_id": "other-thread",
                            "usage": {"output_tokens": 1},
                        },
                    )
                ),
            ),
            (
                "terminal_model_identity_mismatch",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "thread.started",
                            "thread_id": "provider-thread",
                            "model": "gpt-5.6-sol",
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {
                            "type": "turn.completed",
                            "model": "gpt-5.5",
                            "usage": {"output_tokens": 1},
                        },
                    )
                ),
            ),
            (
                "current_text_alias_conflict",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item-agent",
                                "type": "agent_message",
                                "result": "first response",
                                "text": "second response",
                            },
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "duplicate_thread_started",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "malformed_nested_item",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-malformed", "type": " future_item "},
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "missing_item_id",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "missing_text_after_valid_agent_message",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item-first",
                                "type": "agent_message",
                                "text": "intermediate",
                            },
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-last", "type": "agent_message"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "started_agent_message",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.started",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "hidden"},
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-final", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "duplicate_item_completion",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": "item.completed", "item": {"id": "item-reused", "type": "reasoning"}},
                        {"type": "item.completed", "item": {"id": "item-reused", "type": "reasoning"}},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "post_completion_update",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": "item.completed", "item": {"id": "item-todo", "type": "todo_list"}},
                        {"type": "item.updated", "item": {"id": "item-todo", "type": "todo_list"}},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "item_type_change",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {"type": "item.started", "item": {"id": "item-tool", "type": "command_execution"}},
                        {"type": "item.completed", "item": {"id": "item-tool", "type": "mcp_tool_call"}},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "null_identity_alias",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "thread.started",
                            "thread_id": "provider-thread",
                            "threadId": None,
                        },
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "null_text_alias",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response", "result": None},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
            (
                "null_metric_alias",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": "legacy promoted",
                        "num_turns": 1,
                        "numTurns": None,
                    }
                ),
            ),
            (
                "inconsistent_total_tokens",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 1},
                        },
                    )
                ),
            ),
            (
                "combined_cache_input_exceeds_input",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "thread.started", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 8,
                                "cache_write_input_tokens": 8,
                                "output_tokens": 1,
                            },
                        },
                    )
                ),
            ),
            (
                "duplicate_top_level_type",
                '{"type":"error","type":"result","subtype":"success","result":"legacy promoted"}',
            ),
            (
                "malformed_legacy_is_error",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": "true",
                        "result": "legacy promoted",
                    }
                ),
            ),
            (
                "padded_thread_discriminator",
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {"type": "`thread.started`", "thread_id": "provider-thread"},
                        {
                            "type": "item.completed",
                            "item": {"id": "item-agent", "type": "agent_message", "text": "response"},
                        },
                        {"type": "turn.completed", "usage": {"output_tokens": 1}},
                    )
                ),
            ),
        ):
            with self.subTest(consumer="agent_dispatch", rejected_case=rejected_case), tempfile.TemporaryDirectory() as tmp:
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
                    stdout=stdout,
                    stderr="",
                )
                with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    return_value=completed,
                ):
                    dispatch_output = builder.codex_exec_agent_dispatch(
                        runtime="codex",
                        state_root=state_root,
                        hook_input={
                            "session_id": "session",
                            "agent_id": "tech-backend",
                            "request_id": f"req-rejected-{rejected_case}",
                            "cwd": "/tmp/project",
                            "prompt": "Review only.",
                        },
                    )

                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                row = next(item for item in roster if item["agent_id"] == "tech-backend")
                self.assertEqual(dispatch_output["decision"], "block")
                self.assertEqual(
                    dispatch_output["agentDispatch"]["result"],
                    "provider_response_no_inference",
                )
                self.assertEqual(row["activation_status"], "idle")
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["effective_model"], "")
                self.assertEqual(row["session_id"], "")
                self.assertNotEqual(state["readiness_scope"], "response_evidence")

            with self.subTest(consumer="provider_activate", rejected_case=rejected_case), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                builder.session_start_metadata_output(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "cwd": "/tmp/project", "source": "startup"},
                )
                completed = subprocess.CompletedProcess(
                    args=["codex"],
                    returncode=0,
                    stdout=stdout,
                    stderr="",
                )
                with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    return_value=completed,
                ):
                    activation_output = builder.provider_activate(
                        runtime="codex",
                        state_root=state_root,
                        hook_input={
                            "session_id": "session",
                            "agent_id": "tech-backend",
                            "cwd": "/tmp/project",
                        },
                    )
                session_dir = state_root / "session"
                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                row = next(item for item in roster if item["agent_id"] == "tech-backend")

                self.assertEqual(activation_output["decision"], "block")
                self.assertEqual(
                    activation_output["reason"],
                    "codex provider activation produced no inference evidence",
                )
                self.assertEqual(row["activation_status"], "idle")
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["effective_model"], "")
                self.assertEqual(row["session_id"], "")
                self.assertNotEqual(state["readiness_scope"], "response_evidence")

    def test_codex_consumers_clear_prior_response_after_resource_rejection(self) -> None:
        builder = load_builder_module()
        rejected_stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "thread.started",
                        "thread_id": "provider-thread",
                        "model": "gpt-5.6-sol",
                    }
                ),
                json.dumps({"type": "item.completed", "item": {"id": "item-agent", "type": "agent_message", "text": "response"}}),
                (
                    '{"type":"turn.completed","usage":{"output_tokens":'
                    + ("9" * 5000)
                    + "}}"
                ),
            )
        )
        valid_completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(),
            stderr="",
        )
        rejected_completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=rejected_stdout,
            stderr="",
        )

        with self.subTest(consumer="agent_dispatch"), tempfile.TemporaryDirectory() as tmp:
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
            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=[valid_completed, rejected_completed],
            ):
                ready_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-ready",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
                rejected_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-rejected",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            row = next(item for item in roster if item["agent_id"] == "tech-backend")

            self.assertEqual(ready_output["agentDispatch"]["result"], "provider_response_ready")
            self.assertEqual(rejected_output["decision"], "block")
            self.assertEqual(row["activation_status"], "idle")
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(row["effective_model"], "")
            self.assertEqual(row["session_id"], "")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["provider_response_ready_count"], 0)

        with self.subTest(consumer="provider_activate"), tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            builder.session_start_metadata_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "session", "cwd": "/tmp/project", "source": "startup"},
            )
            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=[valid_completed, rejected_completed],
            ):
                ready_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "cwd": "/tmp/project",
                    },
                )
                rejected_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "cwd": "/tmp/project",
                    },
                )
            session_dir = state_root / "session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            row = next(item for item in roster if item["agent_id"] == "tech-backend")

            self.assertEqual(ready_output["activation"]["effective_model"], "gpt-5.6-sol")
            self.assertEqual(rejected_output["decision"], "block")
            self.assertEqual(row["activation_status"], "idle")
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(row["effective_model"], "")
            self.assertEqual(row["session_id"], "")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["provider_response_ready_count"], 0)

    def test_codex_consumers_clear_prior_response_after_process_output_rejection(self) -> None:
        builder = load_builder_module()

        def valid_completed() -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["codex"],
                returncode=0,
                stdout=current_codex_jsonl(),
                stderr="",
            )

        def limited_completed() -> subprocess.CompletedProcess[str]:
            completed = subprocess.CompletedProcess(
                args=["codex"],
                returncode=-9,
                stdout="x" * 64,
                stderr="",
            )
            setattr(completed, "output_limit_exceeded", "stdout")
            setattr(completed, "output_decode_error", False)
            setattr(completed, "output_read_error", "")
            return completed

        def read_failed_completed() -> subprocess.CompletedProcess[str]:
            completed = subprocess.CompletedProcess(
                args=["codex"],
                returncode=-9,
                stdout="",
                stderr="",
            )
            setattr(completed, "output_limit_exceeded", "")
            setattr(completed, "output_decode_error", True)
            setattr(completed, "output_read_error", "multiplexer: OSError")
            return completed

        with self.subTest(consumer="agent_dispatch"), tempfile.TemporaryDirectory() as tmp:
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
            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=[
                    valid_completed(),
                    limited_completed(),
                    valid_completed(),
                    read_failed_completed(),
                ],
            ):
                ready_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-ready",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
                rejected_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-limited",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
                read_ready_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-read-ready",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
                read_rejected_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-read-failed",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(ready_output["agentDispatch"]["result"], "provider_response_ready")
            self.assertEqual(rejected_output["decision"], "block")
            self.assertEqual(
                rejected_output["agentDispatch"]["result"],
                "provider_output_limit_exceeded",
            )
            self.assertEqual(
                read_ready_output["agentDispatch"]["result"],
                "provider_response_ready",
            )
            self.assertEqual(read_rejected_output["decision"], "block")
            self.assertEqual(
                read_rejected_output["agentDispatch"]["result"],
                "provider_output_read_failed",
            )
            self.assertEqual(row["activation_status"], "idle")
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(row["effective_model"], "")
            self.assertEqual(row["session_id"], "")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["provider_response_ready_count"], 0)
            evidence_results = [entry["result"] for entry in evidence]
            self.assertIn("provider_output_limit_exceeded", evidence_results)
            self.assertEqual(evidence[-1]["result"], "provider_output_read_failed")

        with self.subTest(consumer="provider_activate"), tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            builder.session_start_metadata_output(
                runtime="codex",
                state_root=state_root,
                hook_input={"session_id": "session", "cwd": "/tmp/project", "source": "startup"},
            )
            with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=[
                    valid_completed(),
                    limited_completed(),
                    valid_completed(),
                    read_failed_completed(),
                ],
            ):
                ready_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )
                rejected_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )
                read_ready_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )
                read_rejected_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )
            session_dir = state_root / "session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(ready_output["activation"]["effective_model"], "gpt-5.6-sol")
            self.assertEqual(rejected_output["decision"], "block")
            self.assertIn("bounded byte limit", rejected_output["reason"])
            self.assertEqual(read_ready_output["activation"]["effective_model"], "gpt-5.6-sol")
            self.assertEqual(read_rejected_output["decision"], "block")
            self.assertIn("multiplexer: OSError", read_rejected_output["reason"])
            self.assertEqual(row["activation_status"], "idle")
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(row["effective_model"], "")
            self.assertEqual(row["session_id"], "")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["provider_response_ready_count"], 0)
            evidence_results = [entry["result"] for entry in evidence]
            self.assertIn("provider_output_limit_exceeded", evidence_results)
            self.assertEqual(evidence[-1]["result"], "provider_output_read_failed")

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
                builder,
                "run_command_with_bounded_output",
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
                builder,
                "run_command_with_bounded_output",
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

    def test_queue_snapshot_skips_entries_that_vanish_during_scan(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            queue_root.mkdir()
            stable = queue_root / "stable.json"
            nested = queue_root / "nested"
            stable.write_text("stable", encoding="utf-8")
            nested.mkdir()
            expected = builder.queue_watch_snapshot(queue_root)
            with builder.os.scandir(queue_root) as scanner:
                entries = {entry.name: entry for entry in scanner}

            class VanishedEntry:
                path = str(queue_root / "vanished.json")

                def stat(self, *, follow_symlinks: bool = True):
                    raise FileNotFoundError(self.path)

            with self.subTest(race="metadata"), mock.patch.object(
                builder.os,
                "scandir",
                return_value=ScandirStub([VanishedEntry(), entries["stable.json"]]),
            ):
                self.assertEqual(builder.queue_watch_snapshot(queue_root), expected)

            with self.subTest(race="nested-scan"), mock.patch.object(
                builder.os,
                "scandir",
                side_effect=[
                    ScandirStub([entries["nested"], entries["stable.json"]]),
                    FileNotFoundError(str(nested)),
                ],
            ):
                self.assertEqual(builder.queue_watch_snapshot(queue_root), expected)

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
