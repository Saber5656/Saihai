from __future__ import annotations

import importlib.util
import io
import json
import os
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
BUILDER_CODE = compile(BUILDER.read_bytes(), str(BUILDER), "exec")


def _load_builder_module_once():
    spec = importlib.util.spec_from_file_location("itb_runtime_regressions_for_test", BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load ITB builder module")
    module = importlib.util.module_from_spec(spec)
    exec(BUILDER_CODE, module.__dict__)
    return module


BUILDER_MODULE = _load_builder_module_once()


def load_builder_module():
    return BUILDER_MODULE


def policy_load_exception(exception_type, sensitive_detail: str) -> BaseException:
    if exception_type is UnicodeDecodeError:
        return UnicodeDecodeError("utf-8", b"\xff", 0, 1, sensitive_detail)
    return exception_type(sensitive_detail)


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
    include_model: bool = True,
    include_thread_started: bool = True,
    include_turn_completed: bool = True,
    model: str = "gpt-5.6-luna",
) -> str:
    events: list[dict[str, object]] = []
    if include_thread_started:
        thread_started: dict[str, object] = {
            "type": "thread.started",
            "thread_id": "provider-thread",
        }
        if include_model:
            thread_started["model"] = model
        events.append(thread_started)
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
    def test_cached_builder_module_reuses_process_local_import(self) -> None:
        first = load_builder_module()
        with mock.patch.object(first, "test_only_marker", True, create=True):
            second = load_builder_module()

            self.assertIs(first, second)
            self.assertTrue(second.test_only_marker)
            self.assertEqual(Path(second.__file__), BUILDER)

        self.assertFalse(hasattr(first, "test_only_marker"))

    def test_parse_codex_current_jsonl_extracts_final_message_session_and_usage(self) -> None:
        builder = load_builder_module()

        parsed = builder.parse_codex_json_output(current_codex_jsonl())

        self.assertEqual(parsed["result"], "final review result")
        self.assertEqual(parsed["session_id"], "provider-thread")
        self.assertEqual(parsed["model"], "gpt-5.6-luna")
        self.assertEqual(parsed["usage"]["input_tokens"], 11)
        self.assertEqual(parsed["usage"]["cached_input_tokens"], 5)
        self.assertEqual(parsed["usage"]["cache_write_input_tokens"], 2)
        self.assertEqual(parsed["usage"]["output_tokens"], 7)
        self.assertEqual(parsed["usage"]["reasoning_output_tokens"], 3)
        self.assertEqual(parsed["num_turns"], 1)
        self.assertEqual(
            builder.codex_event_model({"reported_effective_model": "gpt-5.6-luna"}),
            ("gpt-5.6-luna", True),
        )
        self.assertEqual(
            builder.codex_event_model(
                {
                    "model": "gpt-5.6-luna",
                    "reported_effective_model": "gpt-5.6-sol",
                }
            ),
            ("", False),
        )
        self.assertEqual(
            builder.codex_event_model({"reported_effective_model": 123}),
            ("", False),
        )
        for invalid_marker in (
            {"reported_model_metadata_valid": False},
            {"reported_model_metadata_valid": "true"},
            {"provider_identity_status": "invalid"},
        ):
            with self.subTest(invalid_model_marker=invalid_marker):
                marked_model = {"model": "gpt-5.6-luna", **invalid_marker}
                self.assertEqual(builder.codex_event_model(marked_model), ("", False))
                self.assertEqual(builder.claude_reported_model(marked_model), ("", False))
        marked_codex_events = [
            json.loads(line)
            for line in current_codex_jsonl().splitlines()
            if line.strip()
        ]
        marked_codex_events[0]["reported_model_metadata_valid"] = False
        self.assertEqual(
            builder.parse_codex_json_output(
                "\n".join(json.dumps(event) for event in marked_codex_events)
            ),
            {},
        )
        with self.assertRaisesRegex(ValueError, "provider JSON contains duplicate object keys"):
            builder.parse_claude_json_output(
                '{"model":"claude-sonnet-4-6","model":"claude-opus-4-6"}'
            )

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

        self.assertEqual(
            builder.provider_returncode_evidence(
                subprocess.CompletedProcess(args=["codex"], returncode=1)
            ),
            {"provider_returncode": 1},
        )
        self.assertEqual(
            builder.provider_returncode_evidence(
                subprocess.CompletedProcess(args=["codex"], returncode=999)
            ),
            {"provider_returncode": None},
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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
        self.assertEqual(dispatch["effective_model"], "gpt-5.6-luna")
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

        self.assertEqual(output["activation"]["effective_model"], "gpt-5.6-luna")
        self.assertEqual(output["activation"]["session_id"], "provider-thread")

    def test_codex_consumers_reject_reported_model_mismatch(self) -> None:
        """Reject a provider-reported model that differs from intended Luna."""
        builder = load_builder_module()
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(model="gpt-5.6-sol"),
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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
                return_value=completed,
            ):
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-model-mismatch",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(output["decision"], "block")
            self.assertIn("provider-reported effective model", output["reason"])
            self.assertNotIn("gpt-5.6-sol", json.dumps(output))
            self.assertEqual(output["agentDispatch"]["effective_model"], "")
            self.assertEqual(row["provider_status"], "provider_model_mismatch")
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(row["effective_model"], "")
            self.assertNotEqual(state["readiness_scope"], "response_evidence")
            self.assertEqual(evidence["result"], "provider_model_mismatch")
            self.assertEqual(evidence["effective_model"], "gpt-5.6-sol")

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

            session_dir = state_root / "session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(output["decision"], "block")
            self.assertIn("provider-reported effective model", output["reason"])
            self.assertNotIn("gpt-5.6-sol", json.dumps(output))
            self.assertEqual(row["provider_status"], "provider_model_mismatch")
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(row["effective_model"], "")
            self.assertNotEqual(state["readiness_scope"], "response_evidence")
            self.assertEqual(evidence["result"], "provider_model_mismatch")
            self.assertEqual(evidence["effective_model"], "gpt-5.6-sol")

    def test_codex_consumers_redact_cross_provider_mismatch_and_clear_stale_readiness(self) -> None:
        """Redact raw model data and invalidate readiness from a prior successful response."""
        builder = load_builder_module()
        reported_model = "claude-opus-4-6"
        successful = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(),
            stderr="",
        )
        mismatched = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(model=reported_model),
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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
                side_effect=[successful, mismatched],
            ):
                first_output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-before-cross-provider-mismatch",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-cross-provider-mismatch",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(first_output["agentDispatch"]["result"], "provider_response_ready")
            self.assertEqual(output["decision"], "block")
            self.assertNotIn(reported_model, json.dumps(output))
            self.assertNotIn(reported_model, row["notes"])
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["provider_response_ready_count"], 0)
            self.assertEqual(evidence["effective_model"], reported_model)

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
                side_effect=[successful, mismatched],
            ):
                first_output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )

            session_dir = state_root / "session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertNotIn("decision", first_output)
            self.assertEqual(output["decision"], "block")
            self.assertNotIn(reported_model, json.dumps(output))
            self.assertNotIn(reported_model, row["notes"])
            self.assertEqual(row["response_status"], "not_invoked")
            self.assertEqual(state["readiness_scope"], "metadata_only")
            self.assertEqual(state["provider_response_ready_count"], 0)
            self.assertEqual(evidence["effective_model"], reported_model)



    def test_agent_dispatch_requires_canonical_role_policy(self) -> None:
        """Ignore persisted unknown routing and reject malformed canonical policy."""
        builder = load_builder_module()

        for exception_type in (OSError, ValueError, UnicodeDecodeError):
            case = f"canonical_lookup_{exception_type.__name__}"
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                sensitive_detail = f"{case}-private-registry-detail"
                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    side_effect=policy_load_exception(exception_type, sensitive_detail),
                ), mock.patch.object(
                    builder,
                    "codex_exec_agent_dispatch",
                ) as codex_mock, mock.patch.object(
                    builder,
                    "claude_cli_agent_dispatch",
                ) as claude_mock, mock.patch.object(
                    builder.shutil,
                    "which",
                ) as which_mock, mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                ) as codex_runner, mock.patch.object(
                    builder,
                    "run_claude_command_with_bounded_output",
                ) as claude_runner:
                    output = builder.agent_dispatch(
                        runtime="codex",
                        state_root=Path(tmp),
                        hook_input={
                            "session_id": "session",
                            "organization_instance_id": "org-generic-policy-load",
                            "agent_id": "tech-backend",
                            "request_id": f"req-{case}",
                            "prompt": "Review only.",
                        },
                    )

                self.assertEqual(
                    output,
                    {"decision": "block", "reason": "canonical role policy is unavailable"},
                )
                self.assertNotIn(sensitive_detail, json.dumps(output, sort_keys=True))
                codex_mock.assert_not_called()
                claude_mock.assert_not_called()
                which_mock.assert_not_called()
                codex_runner.assert_not_called()
                claude_runner.assert_not_called()

        with self.subTest(case="unknown_persisted_role"), tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(
                json.dumps(
                    [
                        {
                            "agent_id": "unknown-role",
                            "provider": "anthropic",
                            "execution_mode": "claude",
                            "intended_model": "claude-opus-4-6",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(builder, "codex_exec_agent_dispatch") as codex_mock, mock.patch.object(
                builder,
                "claude_cli_agent_dispatch",
            ) as claude_mock:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "unknown-role"},
                )

            self.assertEqual(output, {"decision": "block", "reason": "canonical role policy is unavailable"})
            codex_mock.assert_not_called()
            claude_mock.assert_not_called()

        with self.subTest(case="malformed_canonical_policy"), tempfile.TemporaryDirectory() as tmp:
            malformed = {
                "agent_id": "tech-backend",
                "provider": "",
                "execution_mode": "codex",
                "intended_model": "gpt-5.6-luna",
            }
            with mock.patch.object(builder, "role_agent_row_for", return_value=malformed), mock.patch.object(
                builder,
                "codex_exec_agent_dispatch",
            ) as codex_mock, mock.patch.object(
                builder,
                "claude_cli_agent_dispatch",
            ) as claude_mock:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=Path(tmp),
                    hook_input={"session_id": "session", "agent_id": "tech-backend"},
                )

            self.assertEqual(output, {"decision": "block", "reason": "canonical provider policy is unsupported"})
            codex_mock.assert_not_called()
            claude_mock.assert_not_called()

        with self.subTest(case="provider_activate_malformed_canonical_policy"), tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            persisted = {
                "agent_id": "tech-backend",
                "provider": "anthropic",
                "execution_mode": "claude",
                "intended_model": "claude-opus-4-6",
            }
            malformed = {
                "agent_id": "tech-backend",
                "provider": "anthropic",
                "execution_mode": "codex",
                "intended_model": "gpt-5.6-luna",
            }
            (session_dir / "roster.json").write_text(json.dumps([persisted]), encoding="utf-8")
            with mock.patch.object(builder, "role_agent_row_for", return_value=malformed), mock.patch.object(
                builder.shutil,
                "which",
            ) as which_mock, mock.patch.object(
                builder,
                "run_command_with_bounded_output",
            ) as codex_mock, mock.patch.object(
                builder,
                "run_claude_command_with_bounded_output",
            ) as claude_mock:
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-malformed-provider-policy",
                    },
                )

            self.assertEqual(output, {"decision": "block", "reason": "canonical provider policy is unsupported"})
            which_mock.assert_not_called()
            codex_mock.assert_not_called()
            claude_mock.assert_not_called()
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(roster[0]["provider_status"], "provider_model_policy_invalid")
            self.assertEqual(roster[0]["response_status"], "not_invoked")
            self.assertEqual(evidence["result"], "provider_model_policy_invalid")
            self.assertFalse(evidence["provider_invoked"])

        canonical_claude = {
            "agent_id": "legacy-claude-role",
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "claude-sonnet-4-6",
            "allowed_tools": ["Read"],
        }
        claude_variants = (
            (
                "missing_execution",
                {"execution_mode": ""},
                "persisted execution policy does not match canonical role",
            ),
            (
                "tampered_model",
                {"intended_model": "claude-sonnet-4-6"},
                "persisted intended model policy does not match canonical role",
            ),
        )
        for entrypoint in ("generic", "direct"):
            for variant, override, expected_reason in claude_variants:
                with self.subTest(entrypoint=entrypoint, case=f"claude_{variant}"), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    persisted = dict(canonical_claude)
                    persisted.update(override)
                    (session_dir / "roster.json").write_text(json.dumps([persisted]), encoding="utf-8")
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        return_value=canonical_claude,
                    ), mock.patch.object(
                        builder.shutil,
                        "which",
                    ) as which_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                    ) as claude_mock:
                        hook_input = {
                            "session_id": "session",
                            "agent_id": "legacy-claude-role",
                            "request_id": f"req-claude-{entrypoint}-{variant}",
                            "prompt": "Review only.",
                        }
                        if entrypoint == "generic":
                            output = builder.agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        else:
                            output = builder.claude_cli_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )

                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], expected_reason)
                    which_mock.assert_not_called()
                    claude_mock.assert_not_called()
                    roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    self.assertEqual(roster[0]["provider_status"], "provider_model_policy_invalid")
                    self.assertEqual(roster[0]["response_status"], "not_invoked")
                    self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                    self.assertFalse(evidence["provider_invoked"])

        raw_model = " `claude-opus-4-6` "
        claude_success = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "review complete",
                    "model": canonical_claude["intended_model"],
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "duration_api_ms": 3,
                    "session_id": "provider-session",
                    "request_id": "provider-request",
                    "num_turns": 1,
                }
            ),
            stderr="",
        )
        for entrypoint in ("generic", "direct", "activation"):
            with self.subTest(entrypoint=entrypoint, case="claude_canonical_command_binding"), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                persisted = dict(canonical_claude)
                persisted["provider"] = " `anthropic` "
                persisted["execution_mode"] = " `claude` "
                persisted["intended_model"] = raw_model
                persisted["fallback_models"] = "claude-haiku-4-5"
                persisted["allowed_tools"] = ["Bash", "Write"]
                (session_dir / "roster.json").write_text(json.dumps([persisted]), encoding="utf-8")
                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical_claude,
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value="/usr/bin/claude",
                ), mock.patch.object(
                    builder,
                    "run_claude_command_with_bounded_output",
                    return_value=claude_success,
                ) as claude_mock:
                    hook_input = {
                        "session_id": "session",
                        "agent_id": "legacy-claude-role",
                        "request_id": f"req-claude-binding-{entrypoint}",
                        "prompt": "Review only.",
                    }
                    if entrypoint == "generic":
                        output = builder.agent_dispatch(runtime="codex", state_root=state_root, hook_input=hook_input)
                    elif entrypoint == "direct":
                        output = builder.claude_cli_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.provider_activate(runtime="codex", state_root=state_root, hook_input=hook_input)

                command = claude_mock.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], canonical_claude["intended_model"])
                self.assertEqual(
                    command[command.index("--fallback-model") + 1],
                    canonical_claude["fallback_models"],
                )
                self.assertEqual(command[command.index("--tools") + 1], "Read")
                serialized_command = "\n".join(command)
                self.assertNotIn(raw_model, serialized_command)
                self.assertNotIn(" `anthropic` ", serialized_command)
                self.assertNotIn(" `claude` ", serialized_command)
                self.assertNotIn("claude-haiku-4-5", serialized_command)
                self.assertNotIn("Bash", serialized_command)
                self.assertNotIn("Write", serialized_command)
                if entrypoint == "activation":
                    self.assertEqual(output["activation"]["effective_model"], canonical_claude["intended_model"])
                else:
                    self.assertEqual(output["agentDispatch"]["intended_model"], canonical_claude["intended_model"])
                    self.assertEqual(output["agentDispatch"]["effective_model"], canonical_claude["intended_model"])
                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                self.assertEqual(roster[0]["effective_model"], canonical_claude["intended_model"])
                self.assertEqual(evidence["effective_model"], canonical_claude["intended_model"])
                self.assertTrue(roster[0]["canonical_execution_policy_digest"])
                self.assertEqual(
                    evidence["canonical_execution_policy_digest"],
                    roster[0]["canonical_execution_policy_digest"],
                )
                self.assertEqual(evidence["canonical_provider"], "anthropic")
                self.assertEqual(evidence["canonical_execution_mode"], "claude")
                self.assertEqual(evidence["intended_model"], canonical_claude["intended_model"])

    def test_malformed_provider_rosters_persist_fail_closed_evidence(self) -> None:
        """Reject non-list and non-UTF-8 rosters without retaining stale readiness."""
        builder = load_builder_module()
        consumers = (
            (
                "codex",
                builder.codex_exec_agent_dispatch,
                "canonical codex role policy is unavailable",
                True,
            ),
            (
                "claude",
                builder.claude_cli_agent_dispatch,
                "canonical claude role policy is unavailable",
                True,
            ),
            (
                "activation",
                builder.provider_activate,
                "canonical provider role policy is unavailable",
                False,
            ),
        )
        for consumer_label, consumer, decode_reason, has_dispatch_result in consumers:
            for payload_kind in ("non_list", "invalid_utf8"):
                case = f"{consumer_label}_{payload_kind}"
                with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    (session_dir / "bootstrap.json").write_text(
                        json.dumps(
                            {
                                "organization_instance_id": "org-malformed-roster",
                                "provider_response_ready_count": 1,
                                "provider_response_scope": "response_evidence",
                                "readiness_scope": "response_evidence",
                            }
                        ),
                        encoding="utf-8",
                    )
                    sensitive_detail = f"{case}-private-roster-detail"
                    if payload_kind == "non_list":
                        roster_bytes = json.dumps(
                            {
                                "agent_id": "tech-backend",
                                "response_status": "invoked",
                                "private": sensitive_detail,
                            }
                        ).encode("utf-8")
                        expected_reason = "roster.json is not a list"
                    else:
                        roster_bytes = b"\xff" + sensitive_detail.encode("utf-8")
                        expected_reason = decode_reason
                    roster_path = session_dir / "roster.json"
                    roster_path.write_bytes(roster_bytes)

                    with mock.patch.object(builder.shutil, "which") as which_mock, mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                    ) as codex_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                    ) as claude_mock:
                        output = consumer(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "organization_instance_id": "org-malformed-roster",
                                "agent_id": "tech-backend",
                                "request_id": f"req-{case}",
                                "cwd": "/tmp/project",
                                "prompt": "Review only.",
                            },
                        )

                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    serialized = json.dumps(
                        {"output": output, "state": state, "evidence": evidence},
                        sort_keys=True,
                    )
                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], expected_reason)
                    if has_dispatch_result:
                        self.assertEqual(output["agentDispatch"]["result"], "provider_model_policy_invalid")
                    else:
                        self.assertNotIn("agentDispatch", output)
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["provider_response_scope"], "not_invoked")
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                    self.assertFalse(evidence["provider_invoked"])
                    self.assertEqual(roster_path.read_bytes(), roster_bytes)
                    self.assertNotIn(sensitive_detail, serialized)
                    which_mock.assert_not_called()
                    codex_mock.assert_not_called()
                    claude_mock.assert_not_called()

    def test_provider_policy_load_errors_persist_fail_closed_evidence(self) -> None:
        """Convert missing or malformed canonical policy loads into bounded rejections."""
        builder = load_builder_module()

        def write_stale_state(session_dir: Path, organization_instance_id: str) -> None:
            (session_dir / "bootstrap.json").write_text(
                json.dumps(
                    {
                        "organization_instance_id": organization_instance_id,
                        "provider_response_ready_count": 1,
                        "provider_response_scope": "response_evidence",
                        "readiness_scope": "response_evidence",
                    }
                ),
                encoding="utf-8",
            )

        direct_consumers = (
            (
                "codex",
                builder.codex_exec_agent_dispatch,
                "canonical codex role policy is unavailable",
            ),
            (
                "claude",
                builder.claude_cli_agent_dispatch,
                "canonical claude role policy is unavailable",
            ),
        )
        for provider_label, consumer, expected_reason in direct_consumers:
            for exception_type in (OSError, ValueError, UnicodeDecodeError):
                case = f"{provider_label}_{exception_type.__name__}_missing_roster"
                with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    write_stale_state(session_dir, "org-policy-load-direct")
                    sensitive_detail = f"{case}-private-registry-detail"
                    with mock.patch.object(
                        builder,
                        "role_agent_rows",
                        side_effect=policy_load_exception(exception_type, sensitive_detail),
                    ), mock.patch.object(builder.shutil, "which") as which_mock, mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                    ) as codex_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                    ) as claude_mock:
                        output = consumer(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "organization_instance_id": "org-policy-load-direct",
                                "agent_id": "tech-backend",
                                "request_id": f"req-{case}",
                                "cwd": "/tmp/project",
                                "prompt": "Review only.",
                            },
                        )

                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    serialized = json.dumps(
                        {"output": output, "state": state, "evidence": evidence},
                        sort_keys=True,
                    )
                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], expected_reason)
                    self.assertEqual(output["agentDispatch"]["result"], "provider_model_policy_invalid")
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["provider_response_scope"], "not_invoked")
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                    self.assertFalse(evidence["provider_invoked"])
                    self.assertFalse((session_dir / "roster.json").exists())
                    self.assertNotIn(sensitive_detail, serialized)
                    which_mock.assert_not_called()
                    codex_mock.assert_not_called()
                    claude_mock.assert_not_called()

        for exception_type in (OSError, ValueError, UnicodeDecodeError):
            case = f"activation_{exception_type.__name__}_missing_roster"
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                write_stale_state(session_dir, "org-policy-load-activation")
                sensitive_detail = f"{case}-private-registry-detail"
                with mock.patch.object(
                    builder,
                    "role_agent_rows",
                    side_effect=policy_load_exception(exception_type, sensitive_detail),
                ), mock.patch.object(builder.shutil, "which") as which_mock, mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                ) as codex_mock, mock.patch.object(
                    builder,
                    "run_claude_command_with_bounded_output",
                ) as claude_mock:
                    output = builder.provider_activate(
                        runtime="codex",
                        state_root=state_root,
                        hook_input={
                            "session_id": "session",
                            "organization_instance_id": "org-policy-load-activation",
                            "agent_id": "tech-backend",
                            "request_id": f"req-{case}",
                            "prompt": "Review only.",
                        },
                    )

                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                serialized = json.dumps(
                    {"output": output, "state": state, "evidence": evidence},
                    sort_keys=True,
                )
                self.assertEqual(output["decision"], "block")
                self.assertEqual(output["reason"], "canonical provider role policy is unavailable")
                self.assertEqual(state["provider_response_ready_count"], 0)
                self.assertEqual(state["provider_response_scope"], "not_invoked")
                self.assertEqual(state["readiness_scope"], "metadata_only")
                self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                self.assertFalse(evidence["provider_invoked"])
                self.assertFalse((session_dir / "roster.json").exists())
                self.assertNotIn(sensitive_detail, serialized)
                which_mock.assert_not_called()
                codex_mock.assert_not_called()
                claude_mock.assert_not_called()

        canonical_codex = builder.role_agent_row_for(
            "tech-backend",
            organization_instance_id="org-policy-load-existing",
        )
        canonical_claude = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-policy-load-existing",
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "claude-sonnet-4-6",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "always_active": False,
        }
        for provider_label, canonical in (
            ("codex", canonical_codex),
            ("claude", canonical_claude),
        ):
            for exception_type in (OSError, ValueError, UnicodeDecodeError):
                case = f"activation_{provider_label}_{exception_type.__name__}_existing_roster"
                with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    write_stale_state(session_dir, "org-policy-load-existing")
                    stale = dict(canonical)
                    stale.update(
                        {
                            "activation_status": "response_active",
                            "response_status": "invoked",
                            "provider_status": "provider_response_ready",
                            "effective_model": canonical["intended_model"],
                            "session_id": "stale-provider-session",
                            "last_request_id": "stale-request",
                            "usage_source": "provider-response",
                        }
                    )
                    (session_dir / "roster.json").write_text(json.dumps([stale]), encoding="utf-8")
                    sensitive_detail = f"{case}-private-policy-path"
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        side_effect=policy_load_exception(exception_type, sensitive_detail),
                    ), mock.patch.object(
                        builder,
                        "registry_row_for",
                        side_effect=AssertionError("obsolete pre-policy lookup must not run"),
                    ) as registry_mock, mock.patch.object(
                        builder.shutil,
                        "which",
                    ) as which_mock, mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                    ) as codex_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                    ) as claude_mock:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "organization_instance_id": "org-policy-load-existing",
                                "agent_id": canonical["agent_id"],
                                "request_id": f"req-{case}",
                                "prompt": "Review only.",
                            },
                        )

                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    serialized = json.dumps(
                        {"output": output, "state": state, "roster": roster, "evidence": evidence},
                        sort_keys=True,
                    )
                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], "canonical provider role policy is unavailable")
                    self.assertEqual(roster[0]["provider_status"], "provider_model_policy_invalid")
                    self.assertEqual(roster[0]["response_status"], "not_invoked")
                    self.assertEqual(roster[0]["effective_model"], "")
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["provider_response_scope"], "not_invoked")
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                    self.assertFalse(evidence["provider_invoked"])
                    self.assertNotIn(sensitive_detail, serialized)
                    registry_mock.assert_not_called()
                    which_mock.assert_not_called()
                    codex_mock.assert_not_called()
                    claude_mock.assert_not_called()

    def test_provider_activation_normalizes_second_policy_lookup_errors(self) -> None:
        """Keep activation policy-load evidence provider-neutral across both reads."""
        builder = load_builder_module()
        canonical_codex = builder.role_agent_row_for(
            "tech-backend",
            organization_instance_id="org-activation-second-read",
        )
        canonical_claude = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-activation-second-read",
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "claude-sonnet-4-6",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "always_active": False,
        }
        for provider_label, canonical in (
            ("codex", canonical_codex),
            ("claude", canonical_claude),
        ):
            for exception_type in (OSError, ValueError, UnicodeDecodeError):
                case = f"activation_{provider_label}_{exception_type.__name__}_second_policy_read"
                with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    (session_dir / "bootstrap.json").write_text(
                        json.dumps(
                            {
                                "organization_instance_id": "org-activation-second-read",
                                "provider_response_ready_count": 1,
                                "provider_response_scope": "response_evidence",
                                "readiness_scope": "response_evidence",
                            }
                        ),
                        encoding="utf-8",
                    )
                    stale = dict(canonical)
                    stale.update(
                        {
                            "activation_status": "response_active",
                            "response_status": "invoked",
                            "provider_status": "provider_response_ready",
                            "effective_model": canonical["intended_model"],
                            "session_id": "stale-provider-session",
                            "last_request_id": "stale-request",
                            "usage_source": "provider-response",
                        }
                    )
                    (session_dir / "roster.json").write_text(json.dumps([stale]), encoding="utf-8")
                    sensitive_detail = f"{case}-private-policy-path"
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        side_effect=[dict(canonical), policy_load_exception(exception_type, sensitive_detail)],
                    ) as policy_lookup, mock.patch.object(
                        builder,
                        "registry_row_for",
                        side_effect=AssertionError("obsolete pre-policy lookup must not run"),
                    ) as registry_mock, mock.patch.object(
                        builder.shutil,
                        "which",
                    ) as which_mock, mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                    ) as codex_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                    ) as claude_mock:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "organization_instance_id": "org-activation-second-read",
                                "agent_id": canonical["agent_id"],
                                "request_id": f"req-{case}",
                                "prompt": "Review only.",
                            },
                        )

                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    serialized = json.dumps(
                        {"output": output, "state": state, "roster": roster, "evidence": evidence},
                        sort_keys=True,
                    )
                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], "canonical provider role policy is unavailable")
                    self.assertEqual(policy_lookup.call_count, 2)
                    self.assertEqual(roster[0]["provider_status"], "provider_model_policy_invalid")
                    self.assertEqual(roster[0]["response_status"], "not_invoked")
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                    self.assertFalse(evidence["provider_invoked"])
                    self.assertNotIn(sensitive_detail, serialized)
                    registry_mock.assert_not_called()
                    which_mock.assert_not_called()
                    codex_mock.assert_not_called()
                    claude_mock.assert_not_called()

    def test_direct_policy_lookup_error_matrix_with_existing_roster(self) -> None:
        """Cover both direct adapters and both bounded policy-load exception classes."""
        builder = load_builder_module()
        canonical_codex = builder.role_agent_row_for(
            "tech-backend",
            organization_instance_id="org-direct-existing",
        )
        canonical_claude = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-direct-existing",
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "claude-sonnet-4-6",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "always_active": False,
        }
        direct_consumers = (
            (
                "codex",
                builder.codex_exec_agent_dispatch,
                canonical_codex,
                "canonical codex role policy is unavailable",
            ),
            (
                "claude",
                builder.claude_cli_agent_dispatch,
                canonical_claude,
                "canonical claude role policy is unavailable",
            ),
        )
        for provider_label, consumer, canonical, expected_reason in direct_consumers:
            for exception_type in (OSError, ValueError, UnicodeDecodeError):
                case = f"direct_{provider_label}_{exception_type.__name__}_existing_roster"
                with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    (session_dir / "bootstrap.json").write_text(
                        json.dumps(
                            {
                                "organization_instance_id": "org-direct-existing",
                                "provider_response_ready_count": 1,
                                "provider_response_scope": "response_evidence",
                                "readiness_scope": "response_evidence",
                            }
                        ),
                        encoding="utf-8",
                    )
                    stale = dict(canonical)
                    stale.update(
                        {
                            "activation_status": "response_active",
                            "response_status": "invoked",
                            "provider_status": "provider_response_ready",
                            "effective_model": canonical["intended_model"],
                            "session_id": "stale-provider-session",
                            "last_request_id": "stale-request",
                            "usage_source": "provider-response",
                        }
                    )
                    (session_dir / "roster.json").write_text(json.dumps([stale]), encoding="utf-8")
                    sensitive_detail = f"{case}-private-policy-path"
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        side_effect=policy_load_exception(exception_type, sensitive_detail),
                    ), mock.patch.object(builder.shutil, "which") as which_mock, mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                    ) as codex_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                    ) as claude_mock:
                        output = consumer(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "organization_instance_id": "org-direct-existing",
                                "agent_id": canonical["agent_id"],
                                "request_id": f"req-{case}",
                                "cwd": "/tmp/project",
                                "prompt": "Review only.",
                            },
                        )

                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    serialized = json.dumps(
                        {"output": output, "state": state, "roster": roster, "evidence": evidence},
                        sort_keys=True,
                    )
                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], expected_reason)
                    self.assertEqual(output["agentDispatch"]["result"], "provider_model_policy_invalid")
                    self.assertEqual(roster[0]["provider_status"], "provider_model_policy_invalid")
                    self.assertEqual(roster[0]["response_status"], "not_invoked")
                    self.assertEqual(roster[0]["effective_model"], "")
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["provider_response_scope"], "not_invoked")
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                    self.assertFalse(evidence["provider_invoked"])
                    self.assertNotIn(sensitive_detail, serialized)
                    which_mock.assert_not_called()
                    codex_mock.assert_not_called()
                    claude_mock.assert_not_called()

    def test_provider_policy_load_failure_recovers_after_policy_repair(self) -> None:
        """Do not persist an incomplete roster that blocks a repaired activation."""
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps(
                    {
                        "organization_instance_id": "org-policy-repair",
                        "provider_response_ready_count": 1,
                        "provider_response_scope": "response_evidence",
                        "readiness_scope": "response_evidence",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                builder,
                "role_agent_rows",
                side_effect=policy_load_exception(
                    UnicodeDecodeError,
                    "private malformed registry detail",
                ),
            ), mock.patch.object(builder.shutil, "which") as first_which, mock.patch.object(
                builder,
                "run_command_with_bounded_output",
            ) as first_runner:
                rejected = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "organization_instance_id": "org-policy-repair",
                        "agent_id": "tech-backend",
                        "request_id": "req-policy-load-failed",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            self.assertEqual(rejected["decision"], "block")
            self.assertEqual(rejected["reason"], "canonical provider role policy is unavailable")
            self.assertFalse((session_dir / "roster.json").exists())
            first_which.assert_not_called()
            first_runner.assert_not_called()

            def repaired_provider_response(*_args, **kwargs):
                kwargs["process_started"](mock.Mock())
                return subprocess.CompletedProcess(
                    args=["codex"],
                    returncode=0,
                    stdout=current_codex_jsonl(),
                    stderr="",
                )

            with mock.patch.object(
                builder.shutil,
                "which",
                return_value="/usr/bin/codex",
            ), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=repaired_provider_response,
            ) as repaired_runner:
                recovered = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "organization_instance_id": "org-policy-repair",
                        "agent_id": "tech-backend",
                        "request_id": "req-policy-load-repaired",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(recovered["activation"]["provider"], "openai")
            self.assertEqual(recovered["activation"]["effective_model"], "gpt-5.6-luna")
            self.assertEqual(row["response_status"], "invoked")
            self.assertEqual(row["provider_status"], "provider_response_ready")
            self.assertEqual(state["provider_response_ready_count"], 1)
            self.assertEqual(state["provider_response_scope"], "response_evidence")
            self.assertEqual(state["readiness_scope"], "response_evidence")
            self.assertEqual(evidence[0]["result"], "provider_model_policy_invalid")
            self.assertFalse(evidence[0]["provider_invoked"])
            self.assertEqual(evidence[-1]["result"], "provider_response_ready")
            self.assertTrue(evidence[-1]["provider_invoked"])
            self.assertEqual(evidence[-1]["launch_lock"], "released_after_process_start")
            repaired_runner.assert_called_once()

    def test_claude_consumers_require_exact_reported_model_without_synthesis(self) -> None:
        """Keep omitted Claude identity unknown and reject a non-primary report."""
        builder = load_builder_module()
        canonical_claude = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-claude-model",
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "claude-sonnet-4-6",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        for entrypoint in ("generic", "direct", "activation"):
            for case, model_fields, expected_reported_model, metadata_valid in (
                ("omitted", {}, "", True),
                ("fallback_mismatch", {"model": "claude-sonnet-4-6"}, "claude-sonnet-4-6", True),
                ("case_mismatch", {"model": "CLAUDE-OPUS-4-6"}, "CLAUDE-OPUS-4-6", True),
                ("decorated_mismatch", {"model": " `claude-opus-4-6` "}, "", False),
                (
                    "alias_conflict",
                    {"model": "claude-opus-4-6", "effective_model": "claude-sonnet-4-6"},
                    "",
                    False,
                ),
                (
                    "reported_alias_mismatch",
                    {"reported_effective_model": "claude-sonnet-4-6"},
                    "claude-sonnet-4-6",
                    True,
                ),
                (
                    "reported_alias_conflict",
                    {
                        "model": "claude-opus-4-6",
                        "reported_effective_model": "claude-sonnet-4-6",
                    },
                    "",
                    False,
                ),
                ("non_string_reported_alias", {"reported_effective_model": 0}, "", False),
                ("non_string_model", {"model": 0}, "", False),
            ):
                with self.subTest(entrypoint=entrypoint, case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    (session_dir / "roster.json").write_text(
                        json.dumps([canonical_claude]),
                        encoding="utf-8",
                    )
                    provider_payload = {
                        "result": "review complete",
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                        "duration_api_ms": 3,
                        "session_id": "provider-session",
                        "request_id": "provider-request",
                        "num_turns": 1,
                    }
                    provider_payload.update(model_fields)
                    completed = subprocess.CompletedProcess(
                        args=["claude"],
                        returncode=0,
                        stdout=json.dumps(provider_payload),
                        stderr="",
                    )
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        return_value=canonical_claude,
                    ), mock.patch.object(
                        builder.shutil,
                        "which",
                        return_value="/usr/bin/claude",
                    ), mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                        return_value=completed,
                    ) as run_mock:
                        hook_input = {
                            "session_id": "session",
                            "organization_instance_id": "org-claude-model",
                            "agent_id": "legacy-claude-role",
                            "request_id": f"req-{entrypoint}-{case}",
                            "prompt": "Review only.",
                        }
                        if entrypoint == "generic":
                            output = builder.agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        elif entrypoint == "direct":
                            output = builder.claude_cli_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        else:
                            output = builder.provider_activate(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )

                    run_mock.assert_called_once()
                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                    row = roster[0]
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    self.assertTrue(row["canonical_execution_policy_digest"])
                    self.assertEqual(
                        evidence["canonical_execution_policy_digest"],
                        row["canonical_execution_policy_digest"],
                    )
                    self.assertEqual(evidence["intended_model"], canonical_claude["intended_model"])
                    if case == "omitted":
                        self.assertNotIn("decision", output)
                        response = output["activation"] if entrypoint == "activation" else output["agentDispatch"]
                        self.assertEqual(response["effective_model"], "")
                        self.assertNotIn("effective_model", row)
                        self.assertNotIn("effective_model", evidence)
                        self.assertNotIn("reported_effective_model", evidence)
                        self.assertTrue(evidence["reported_model_metadata_valid"])
                        self.assertEqual(state["provider_response_ready_count"], 1)
                        self.assertEqual(state["readiness_scope"], "response_evidence")
                    else:
                        self.assertEqual(output["decision"], "block")
                        for untrusted_value in model_fields.values():
                            if isinstance(untrusted_value, str) and untrusted_value:
                                self.assertNotIn(untrusted_value, json.dumps(output))
                                self.assertNotIn(untrusted_value, row["notes"])
                        self.assertEqual(row["provider_status"], "provider_model_mismatch")
                        self.assertEqual(row["response_status"], "not_invoked")
                        self.assertEqual(row["effective_model"], "")
                        self.assertEqual(evidence["result"], "provider_model_mismatch")
                        self.assertEqual(evidence["effective_model"], expected_reported_model)
                        self.assertEqual(evidence["reported_effective_model"], expected_reported_model)
                        self.assertEqual(evidence["reported_model_metadata_valid"], metadata_valid)
                        self.assertEqual(state["provider_response_ready_count"], 0)
                        self.assertNotEqual(state["readiness_scope"], "response_evidence")

    def test_bound_policy_avoids_registry_reread_and_invalidates_policy_drift(self) -> None:
        """Use bound capabilities and stop counting evidence after policy drift."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-backend",
            "role_id": "tech-backend",
            "organization_instance_id": "org-policy-digest",
            "status": "active",
            "always_active": False,
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        persisted = dict(canonical)
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            execution_row, policy_error = builder.canonical_codex_execution_policy(
                persisted,
                organization_instance_id="org-policy-digest",
            )
        self.assertEqual(policy_error, "")
        self.assertTrue(execution_row["canonical_execution_policy_digest"])

        with mock.patch.object(
            builder,
            "codex_sandbox_for_role",
            side_effect=AssertionError("bound command must not reread registry"),
        ) as reread_mock:
            command = builder.codex_activation_command(execution_row, "Review only.", "/tmp/project")
        reread_mock.assert_not_called()
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

        response_row = dict(persisted)
        builder.bind_response_policy_identity(response_row, execution_row)
        response_row.update(
            {
                "activation_status": "response_active",
                "response_status": "invoked",
                "provider_status": "provider_response_ready",
                "usage_source": "codex_exec_json",
            }
        )
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            self.assertEqual(builder.provider_response_ready_count([response_row]), 1)

        for alias, value in (
            ("model", "gpt-5.6-sol"),
            ("effectiveModel", "`gpt-5.6-luna`"),
            ("reported_effective_model", 123),
        ):
            stale_alias_row = dict(response_row)
            stale_alias_row[alias] = value
            with self.subTest(stale_response_alias=alias), mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=canonical,
            ):
                self.assertEqual(builder.provider_response_ready_count([stale_alias_row]), 0)

        metadata_invalid_row = dict(response_row)
        metadata_invalid_row["reported_model_metadata_valid"] = False
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            self.assertEqual(builder.provider_response_ready_count([metadata_invalid_row]), 0)

        reset_row = dict(response_row)
        reset_row.update(
            {
                "model": "gpt-5.6-sol",
                "effectiveModel": "gpt-5.6-luna",
                "reported_effective_model": "gpt-5.6-sol",
                "reported_model_metadata_valid": False,
                "provider_identity_status": "invalid",
            }
        )
        builder.reset_response_evidence(reset_row, "2026-09-01T00:00:00+09:00", "reset")
        self.assertEqual(reset_row["effective_model"], "")
        for alias in ("model", "effectiveModel", "reported_effective_model"):
            self.assertNotIn(alias, reset_row)
        self.assertNotIn("reported_model_metadata_valid", reset_row)
        self.assertNotIn("provider_identity_status", reset_row)

        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            (session_dir / "roster.json").write_text(
                json.dumps([response_row]),
                encoding="utf-8",
            )
            (session_dir / "invocation-evidence.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
                preflight_errors, preflight_warnings = builder.validate_preflight_state(
                    session_dir,
                    {},
                )
        self.assertEqual(preflight_errors, [])
        self.assertIn(
            "tech-backend: provider-reported effective model unavailable; identity remains unknown",
            preflight_warnings,
        )

        incomplete_variants = (
            ("activation_status", "active"),
            ("response_status", "not_invoked"),
            ("provider_status", "provider_response_no_inference"),
            ("usage_source", "bootstrap_metadata_only"),
        )
        for field, value in incomplete_variants:
            incomplete_row = dict(response_row)
            incomplete_row[field] = value
            with self.subTest(incomplete_field=field), mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=canonical,
            ):
                self.assertEqual(builder.provider_response_ready_count([incomplete_row]), 0)

        changed_policy = dict(canonical)
        changed_policy["allowed_tools"] = ["Read", "Write"]
        state = {"readiness_scope": "response_evidence"}
        with mock.patch.object(builder, "role_agent_row_for", return_value=changed_policy):
            self.assertEqual(builder.provider_response_ready_count([response_row]), 0)
            builder.update_provider_response_state(state, [response_row])
        self.assertEqual(state["provider_response_ready_count"], 0)
        self.assertEqual(state["provider_response_scope"], "not_invoked")
        self.assertEqual(state["readiness_scope"], "metadata_only")

        invalid_variants = (
            ({**canonical, "intended_model": "gpt-5.6-sol"}, "canonical intended model policy is unsupported"),
            ({**canonical, "fallback_models": "gpt-5.5"}, "canonical fallback model policy is unsupported"),
            ({**canonical, "allowed_tools": ["Danger"]}, "canonical allowed tools policy is unsupported"),
        )
        for invalid_policy, expected_error in invalid_variants:
            with self.subTest(invalid_policy=expected_error), mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=invalid_policy,
            ):
                invalid_persisted = dict(invalid_policy)
                _, error = builder.canonical_codex_execution_policy(
                    invalid_persisted,
                    organization_instance_id="org-policy-digest",
                )
                self.assertEqual(error, expected_error)

    def test_unknown_effective_model_is_not_synthesized_in_metrics_or_reports(self) -> None:
        """Keep absent provider model identity unknown in every enrichment path."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        metric = {
            "role_id": "tech-qa",
            "provider": "openai",
            "intended_model": "gpt-5.6-luna",
            "model": "gpt-5.6-sol",
            "usage_source": "codex_exec_json",
        }
        omitted_metric = {
            key: value
            for key, value in metric.items()
            if key not in {"model", "provider", "intended_model"}
        }
        canonical_unknown_metric = {
            **omitted_metric,
            "provider": "openai",
            "intended_model": "gpt-5.6-luna",
        }
        with mock.patch.object(
            builder,
            "role_agent_row_for",
            side_effect=AssertionError("omitted effective identity must not read intent"),
        ) as role_lookup_mock:
            self.assertEqual(builder.metric_effective_model(omitted_metric), "")
            self.assertTrue(builder.metric_provider_identity_is_valid(omitted_metric))
            self.assertEqual(builder.latency_variant(omitted_metric), "codex_exec_json")
        role_lookup_mock.assert_not_called()
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            self.assertEqual(builder.metric_effective_model(metric), "")
            self.assertTrue(builder.metric_provider_identity_is_valid(canonical_unknown_metric))
            self.assertFalse(
                builder.metric_provider_identity_is_valid(
                    {**canonical_unknown_metric, "provider": "anthropic"}
                )
            )
            self.assertFalse(
                builder.metric_provider_identity_is_valid(
                    {**canonical_unknown_metric, "intended_model": "gpt-5.6-sol"}
                )
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "role_agent_row_for",
            return_value=canonical,
        ):
            evidence = builder.gate_latency_enrich_provider_evidence_from_report(
                state_root=Path(tmp),
                metric=canonical_unknown_metric,
                report={"provider_evidence": {"intended_model": "gpt-5.6-luna"}},
            )
        self.assertNotIn("effective_model", evidence)
        self.assertEqual(evidence["provider_identity_status"], "valid")
        self.assertEqual(
            builder.metric_effective_model(
                {**canonical_unknown_metric, "effective_model": "gpt-5.6-luna"}
            ),
            "gpt-5.6-luna",
        )
        for invalid_model in (
            "gpt-5.6-sol",
            " gpt-5.6-luna",
            "gpt-5.6-luna ",
            "`gpt-5.6-luna`",
            "claude-opus-4-6",
        ):
            with self.subTest(metric_model=invalid_model):
                self.assertEqual(
                    builder.metric_effective_model(
                        {**canonical_unknown_metric, "effective_model": invalid_model}
                    ),
                    "",
                )

        self.assertEqual(
            builder.metric_effective_model(
                {
                    **canonical_unknown_metric,
                    "provider": "anthropic",
                    "intended_model": "claude-opus-4-6",
                    "effective_model": "claude-opus-4-6",
                    "usage_source": "claude_print_json",
                }
            ),
            "",
        )

        conflicting_metric = {
            **metric,
            "effective_model": "gpt-5.6-luna",
            "model": "gpt-5.6-sol",
            "effectiveModel": "`gpt-5.6-luna`",
            "reported_effective_model": 123,
        }
        self.assertEqual(builder.metric_effective_model(conflicting_metric), "")
        self.assertFalse(builder.metric_provider_identity_is_valid(conflicting_metric))
        cleared_metric = builder.clear_metric_provider_identity(conflicting_metric)
        self.assertEqual(cleared_metric["effective_model"], "")
        self.assertEqual(cleared_metric["provider_identity_status"], "invalid")
        self.assertFalse(cleared_metric["reported_model_metadata_valid"])
        for alias in ("model", "effectiveModel", "reported_effective_model"):
            self.assertNotIn(alias, cleared_metric)

        invalid_report = {
            "provider_evidence": {
                "provider": "openai",
                "intended_model": "gpt-5.6-luna",
                "effective_model": "`gpt-5.6-luna`",
                "usage_source": "codex_exec_json",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "role_agent_row_for",
            return_value=canonical,
        ):
            invalid_evidence = builder.gate_latency_enrich_provider_evidence_from_report(
                state_root=Path(tmp),
                metric=metric,
                report=invalid_report,
            )
            with mock.patch.object(
                builder,
                "gate_latency_role_report_for_metric",
                return_value=(invalid_report, "/tmp/report.yaml"),
            ):
                rejected_metric, rejection = builder.gate_latency_enrich_metric_from_report(
                    state_root=Path(tmp),
                    queue_root=Path(tmp) / "queue",
                    metric={**metric, "effective_model": "`gpt-5.6-luna`"},
                )
        self.assertEqual(invalid_evidence["provider_identity_status"], "invalid")
        self.assertEqual(invalid_evidence["effective_model"], "")
        self.assertEqual(rejection["result"], "rejected_provider_identity")
        self.assertEqual(rejected_metric["effective_model"], "")
        self.assertEqual(rejected_metric["provider_identity_status"], "invalid")

        canonical_report = {
            "provider_evidence": {
                "provider": "openai",
                "intended_model": "gpt-5.6-luna",
                "effective_model": "gpt-5.6-luna",
                "usage_source": "codex_exec_json",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "role_agent_row_for",
            return_value=canonical,
        ), mock.patch.object(
            builder,
            "gate_latency_role_report_for_metric",
            return_value=(canonical_report, "/tmp/report.yaml"),
        ):
            rejected_conflict, conflict_rejection = builder.gate_latency_enrich_metric_from_report(
                state_root=Path(tmp),
                queue_root=Path(tmp) / "queue",
                metric=conflicting_metric,
            )
        self.assertEqual(conflict_rejection["result"], "rejected_provider_identity")
        self.assertEqual(rejected_conflict["effective_model"], "")
        for alias in ("model", "effectiveModel", "reported_effective_model"):
            self.assertNotIn(alias, rejected_conflict)

        invalid_summary_metric = {
            "role_id": "gate-prompt-formatter",
            "provider": "openai",
            "intended_model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-luna",
            "model": "gpt-5.6-sol",
            "usage_source": "codex_exec_json",
            "event_type": "finalized",
            "result": "done",
            "task_id": "task-invalid-identity",
            "message_id": "message-invalid-identity",
            "ts": "2026-09-01T00:00:01+09:00",
            "duration_sec": 1.0,
        }
        unknown_summary_metric = {
            key: value
            for key, value in invalid_summary_metric.items()
            if key not in {"effective_model", "model"}
        }
        unknown_summary_metric["task_id"] = "task-unknown-identity"
        unknown_summary_metric["message_id"] = "message-unknown-identity"
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            for mismatch in (
                {"provider": "anthropic"},
                {"provider": 123},
                {"provider": ""},
                {"intended_model": "gpt-5.6-sol"},
                {"intended_model": None},
                {"intended_model": ""},
                {"intendedModel": "gpt-5.6-sol"},
                {"primary_model": "gpt-5.6-sol"},
            ):
                with self.subTest(unknown_identity_mismatch=mismatch):
                    mismatched_unknown = unknown_summary_metric | mismatch
                    self.assertFalse(
                        builder.metric_provider_identity_is_valid(mismatched_unknown)
                    )
                    self.assertEqual(
                        builder.gate_latency_summary_rows([mismatched_unknown]),
                        [],
                    )
                    self.assertEqual(
                        builder.gate_latency_success_duration_metrics(
                            [mismatched_unknown]
                        ),
                        [],
                    )
                    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                        builder,
                        "gate_latency_role_report_for_metric",
                        return_value=(canonical_report, "/tmp/canonical-report.yaml"),
                    ):
                        rejected_metric, rejection = (
                            builder.gate_latency_enrich_metric_from_report(
                                state_root=Path(tmp),
                                queue_root=Path(tmp) / "queue",
                                metric=mismatched_unknown,
                            )
                        )
                    self.assertEqual(
                        rejection["result"],
                        "rejected_provider_identity",
                    )
                    self.assertEqual(
                        rejected_metric["provider_identity_status"],
                        "invalid",
                    )
                    self.assertEqual(rejected_metric["effective_model"], "")
                    self.assertEqual(
                        builder.gate_latency_summary_rows([rejected_metric]),
                        [],
                    )
                    mismatched_with_model = {
                        **mismatched_unknown,
                        "effective_model": "gpt-5.6-luna",
                    }
                    self.assertFalse(
                        builder.metric_provider_identity_is_valid(mismatched_with_model)
                    )
                    self.assertEqual(
                        builder.metric_effective_model(mismatched_with_model),
                        "",
                    )
        metadata_invalid_summary_metric = {
            **unknown_summary_metric,
            "reported_model_metadata_valid": False,
        }
        self.assertEqual(builder.gate_latency_summary_rows([invalid_summary_metric]), [])
        self.assertEqual(builder.task_latency_timeline_rows([invalid_summary_metric]), [])
        self.assertEqual(builder.gate_latency_success_duration_metrics([invalid_summary_metric]), [])
        self.assertEqual(builder.gate_latency_prompt_submit_chains([invalid_summary_metric]), {})
        self.assertEqual(
            builder.gate_latency_duration_bucket([invalid_summary_metric], lambda _metric: True)["sample_count"],
            0,
        )
        self.assertFalse(builder.metric_provider_identity_is_valid(metadata_invalid_summary_metric))
        self.assertEqual(builder.gate_latency_summary_rows([metadata_invalid_summary_metric]), [])
        self.assertEqual(builder.task_latency_timeline_rows([metadata_invalid_summary_metric]), [])
        self.assertEqual(builder.gate_latency_success_duration_metrics([metadata_invalid_summary_metric]), [])
        self.assertEqual(builder.gate_latency_prompt_submit_chains([metadata_invalid_summary_metric]), {})
        self.assertEqual(
            builder.gate_latency_duration_bucket(
                [metadata_invalid_summary_metric],
                lambda _metric: True,
            )["sample_count"],
            0,
        )
        for marker_fields in (
            {"provider_identity_status": "invalid"},
            {"provider_identity_status": "INVALID"},
            {"reported_model_metadata_valid": "false"},
            {"reported_model_metadata_valid": 0},
            {"reported_model_metadata_valid": None},
        ):
            with self.subTest(invalid_marker=marker_fields):
                marker_metric = unknown_summary_metric | marker_fields
                self.assertFalse(builder.metric_provider_identity_is_valid(marker_metric))
                self.assertEqual(builder.gate_latency_summary_rows([marker_metric]), [])
                self.assertEqual(builder.task_latency_timeline_rows([marker_metric]), [])
                self.assertEqual(builder.gate_latency_success_duration_metrics([marker_metric]), [])
                self.assertEqual(builder.gate_latency_prompt_submit_chains([marker_metric]), {})
                self.assertEqual(
                    builder.gate_latency_duration_bucket(
                        [marker_metric],
                        lambda _metric: True,
                    )["sample_count"],
                    0,
                )
                with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical,
                ):
                    rebound = builder.gate_latency_enrich_provider_evidence_from_report(
                        state_root=Path(tmp),
                        metric=marker_metric,
                        report=canonical_report,
                    )
                self.assertEqual(rebound["provider_identity_status"], "invalid")
        self.assertTrue(builder.metric_provider_identity_is_valid(unknown_summary_metric))
        self.assertEqual(builder.gate_latency_summary_rows([unknown_summary_metric])[0]["sample_count"], 1)
        self.assertEqual(builder.task_latency_timeline_rows([unknown_summary_metric])[0]["hop_count"], 1)
        self.assertTrue(
            builder.validate_provider_evidence(
                agent_id="legacy-claude-role",
                provider="anthropic",
                intended_model="claude-opus-4-6",
                effective_model="claude-sonnet-4-6",
                usage_source="claude_print_json",
            )
        )

    def test_report_and_generic_worker_reject_unbound_provider_identity(self) -> None:
        """Reject decorated, conflicting, cross-provider, and non-string report identity."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        evidence_base = {
            "usage_source": "codex_exec_json",
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "transcript_path": "/tmp/provider.jsonl",
        }

        for supplied in (
            {"effective_model": ""},
            {"effective_model": "gpt-5.6-sol"},
            {"effective_model": " gpt-5.6-luna"},
            {"effective_model": "gpt-5.6-luna "},
            {"effective_model": "`gpt-5.6-luna`"},
            {"effective_model": "claude-opus-4-6"},
            {"effective_model": 123},
            {"effective_model": "gpt-5.6-luna", "effectiveModel": "gpt-5.6-sol"},
            {"effective_model": "gpt-5.6-luna", "reported_model_metadata_valid": False},
            {"effective_model": "", "provider": "anthropic"},
            {"effective_model": "", "provider": 123},
            {"effective_model": "", "intended_model": "gpt-5.6-sol"},
            {"effective_model": "", "intendedModel": "gpt-5.6-sol"},
        ):
            with self.subTest(generic_identity=supplied):
                evidence = builder.role_agent_provider_evidence(
                    canonical,
                    {"provider_evidence": evidence_base | supplied},
                )
                self.assertTrue(builder.validate_role_agent_provider_evidence(evidence))
                sanitized = builder.sanitize_invalid_provider_evidence(evidence)
                self.assertEqual(sanitized["effective_model"], "")
                self.assertEqual(sanitized["provider_identity_status"], "invalid")
                self.assertEqual(
                    builder.provider_usage_metric_fields(sanitized)["provider_identity_status"],
                    "invalid",
                )
                self.assertNotIn(str(supplied), json.dumps(sanitized))

        for supplied in (
            {"provider": "anthropic", "effective_model": "gpt-5.6-luna"},
            {"intended_model": "gpt-5.6-sol", "effective_model": "gpt-5.6-luna"},
            {"effective_model": "`gpt-5.6-luna`"},
            {"effective_model": {"untrusted": "gpt-5.6-luna"}},
            {"model": "gpt-5.6-luna", "effective_model": "gpt-5.6-sol"},
        ):
            with self.subTest(role_report_identity=supplied), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                (session_dir / "bootstrap.json").write_text(
                    json.dumps({"organization_instance_id": "org-report-identity"}),
                    encoding="utf-8",
                )
                finalize_mock = mock.Mock()
                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical,
                ), mock.patch.object(
                    builder,
                    "queue_root_for",
                    return_value=state_root / "queue",
                ), mock.patch.object(
                    builder,
                    "queue_message_by_id",
                    return_value={
                        "message_id": "message-id",
                        "task_id": "task-id",
                        "retry_count": 0,
                        "payload": {"report_path": "reports/task-id/report.yaml"},
                    },
                ), mock.patch.object(
                    builder,
                    "enrich_role_report_provider_evidence_from_claude_transcript",
                ), mock.patch.object(
                    builder,
                    "finalize_role_queue_report",
                    finalize_mock,
                ):
                    output = builder.role_report(
                        runtime="codex",
                        state_root=state_root,
                        hook_input={
                            "session_id": "session",
                            "role_id": "tech-qa",
                            "message_id": "message-id",
                            "status": "done",
                            "provider_evidence": evidence_base | supplied,
                        },
                    )
                self.assertEqual(output["decision"], "block")
                expected_reason = (
                    "provider evidence effective_model is invalid"
                    if not isinstance(supplied.get("effective_model", ""), str)
                    else (
                        "provider evidence effective_model aliases conflict"
                        if "model" in supplied and "effective_model" in supplied
                        else "provider evidence identity does not match canonical role"
                    )
                )
                self.assertEqual(output["reason"], expected_reason)
                finalize_mock.assert_not_called()

        exact_evidence = builder.role_agent_provider_evidence(
            canonical,
            {
                "provider_evidence": evidence_base
                | {"effective_model": "gpt-5.6-luna"}
            },
        )
        unknown_evidence = builder.role_agent_provider_evidence(
            canonical,
            {"provider_evidence": evidence_base},
        )
        self.assertEqual(builder.validate_role_agent_provider_evidence(exact_evidence), [])
        self.assertEqual(builder.validate_role_agent_provider_evidence(unknown_evidence), [])

        transcript_metadata = builder.provider_transcript_metadata_fields_from_records(
            [{"model": "`gpt-5.6-luna`"}]
        )
        self.assertEqual(transcript_metadata["effective_model"], "`gpt-5.6-luna`")
        self.assertTrue(transcript_metadata["reported_model_metadata_valid"])
        conflicting_metadata = builder.provider_transcript_metadata_fields_from_records(
            [{"model": "gpt-5.6-luna", "effective_model": "gpt-5.6-sol"}]
        )
        self.assertFalse(conflicting_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", conflicting_metadata)
        for nested_marker in (
            {"reported_model_metadata_valid": False},
            {"reported_model_metadata_valid": "true"},
            {"provider_identity_status": "invalid"},
        ):
            with self.subTest(nested_transcript_marker=nested_marker):
                nested_marker_metadata = (
                    builder.provider_transcript_metadata_fields_from_records(
                        [{"message": {"model": "gpt-5.6-luna", **nested_marker}}]
                    )
                )
                self.assertFalse(
                    nested_marker_metadata["reported_model_metadata_valid"]
                )
                self.assertNotIn("effective_model", nested_marker_metadata)

        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "claude-transcript.jsonl"
            transcript_path.write_text(
                '{"message":{"model":"claude-sonnet-4-6","model":"claude-opus-4-6"}}\n',
                encoding="utf-8",
            )
            strict_transcript_metadata = builder.provider_transcript_metadata_fields_from_records(
                builder.provider_transcript_records(transcript_path)
            )
            transcript_path.write_text(
                '{"message":{"model":"gpt-5.6-luna"}}\n'
                '{"message":\n',
                encoding="utf-8",
            )
            malformed_transcript_metadata = builder.provider_transcript_metadata_fields_from_records(
                builder.provider_transcript_records(transcript_path)
            )
            transcript_path.write_bytes(
                b'{"message":{"model":"gpt-5.6-luna"}}\n'
                b'\xff\n'
            )
            invalid_utf8_transcript_metadata = builder.provider_transcript_metadata_fields_from_records(
                builder.provider_transcript_records(transcript_path)
            )
            transcript_path.write_text(
                '{"message":{"model":"gpt-5.6-luna"}}\n42\n',
                encoding="utf-8",
            )
            non_object_transcript_metadata = builder.provider_transcript_metadata_fields_from_records(
                builder.provider_transcript_records(transcript_path)
            )
            transcript_path.write_text(
                '[{"message":{"model":"gpt-5.6-luna"}},42]',
                encoding="utf-8",
            )
            mixed_list_transcript_metadata = builder.provider_transcript_metadata_fields_from_records(
                builder.provider_transcript_records(transcript_path)
            )
            transcript_path.write_text(
                '{"message":{"model":"gpt-5.6-luna",'
                '"reported_model_metadata_valid":false}}\n',
                encoding="utf-8",
            )
            nested_marker_transcript_metadata = (
                builder.provider_transcript_metadata_fields_from_records(
                    builder.provider_transcript_records(transcript_path)
                )
            )
            first_record = b'{"model":"gpt-5.6-sol"}\n'
            second_record = b'{"model":"gpt-5.6-luna"}\n'
            transcript_path.write_bytes(first_record + second_record)
            with mock.patch.dict(
                os.environ,
                {"ITB_PROVIDER_USAGE_TRANSCRIPT_MAX_BYTES": str(len(second_record))},
            ):
                boundary_records = builder.provider_transcript_records(transcript_path)
        self.assertFalse(strict_transcript_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", strict_transcript_metadata)
        self.assertFalse(malformed_transcript_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", malformed_transcript_metadata)
        self.assertFalse(invalid_utf8_transcript_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", invalid_utf8_transcript_metadata)
        self.assertFalse(non_object_transcript_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", non_object_transcript_metadata)
        self.assertFalse(mixed_list_transcript_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", mixed_list_transcript_metadata)
        self.assertFalse(nested_marker_transcript_metadata["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", nested_marker_transcript_metadata)
        self.assertEqual(boundary_records, [{"model": "gpt-5.6-luna"}])

        cli_args = mock.Mock()
        cli_args.report_json = (
            '{"provider_evidence":{"model":"gpt-5.6-sol",'
            '"model":"gpt-5.6-luna"}}'
        )
        cli_args.session_id = ""
        cli_merged = builder.merge_cli_hook_input(
            cli_args,
            {},
            include_report_json=True,
        )
        self.assertEqual(cli_merged["_cli_report_json_error"], "invalid_provider_json")
        self.assertNotIn("provider_evidence", cli_merged)

    def test_generic_worker_preserves_explicit_invalid_metadata_marker(self) -> None:
        """Prevent generic worker completion from upgrading an explicit false marker."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        message = {
            "message_id": "message-id",
            "task_id": "task-id",
            "payload": {"report_path": "reports/task-id/report.yaml"},
        }
        provider_evidence = {
            "usage_source": "codex_exec_json",
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "transcript_path": "/tmp/provider.jsonl",
            "effective_model": "gpt-5.6-luna",
            "reported_model_metadata_valid": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            queue_root = state_root / "queue"
            session_dir.mkdir(parents=True)
            queue_root.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps({"organization_instance_id": "org-generic-false-marker"}),
                encoding="utf-8",
            )
            metric_mock = mock.Mock()
            with mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=canonical,
            ), mock.patch.object(
                builder,
                "queue_root_for",
                return_value=queue_root,
            ), mock.patch.object(
                builder,
                "claim_pending_message",
                return_value=(message, {}),
            ), mock.patch.object(
                builder,
                "role_agent_load_instruction",
                return_value=("Review only.", "/tmp/instruction.md"),
            ), mock.patch.object(
                builder,
                "update_inbox_message",
            ), mock.patch.object(
                builder,
                "append_jsonl_atomic",
            ), mock.patch.object(
                builder,
                "append_queue_metric",
                metric_mock,
            ):
                output = builder.role_agent_step_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "role_id": "tech-qa",
                        "provider_evidence": provider_evidence,
                    },
                )

        worker = output["roleAgentWorker"]
        self.assertEqual(worker["result"], "message_failed")
        self.assertEqual(worker["messages_processed"], 0)
        failed_evidence = worker["report"]["evidence"]
        self.assertEqual(failed_evidence["effective_model"], "")
        self.assertFalse(failed_evidence["reported_model_metadata_valid"])
        self.assertEqual(failed_evidence["provider_identity_status"], "invalid")
        metric_extra = metric_mock.call_args.kwargs["extra"]
        self.assertEqual(metric_extra["effective_model"], "")
        self.assertEqual(metric_extra["provider_identity_status"], "invalid")
        recovered_metric = {
            **metric_extra,
            "role_id": "tech-qa",
            "provider": "openai",
            "intended_model": "gpt-5.6-luna",
            "usage_source": "codex_exec_json",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "role_agent_row_for",
            return_value=canonical,
        ):
            rebound_recovery = builder.gate_latency_enrich_provider_evidence_from_report(
                state_root=Path(tmp),
                metric=recovered_metric,
                report={
                    "provider_evidence": {
                        "provider": "openai",
                        "intended_model": "gpt-5.6-luna",
                        "effective_model": "",
                        "usage_source": "codex_exec_json",
                    }
                },
            )
        self.assertEqual(rebound_recovery["provider_identity_status"], "invalid")
        self.assertFalse(rebound_recovery["reported_model_metadata_valid"])

    def test_generic_worker_redacts_exception_text_from_all_failure_surfaces(self) -> None:
        """Keep queue/report/public failures useful without persisting raw exception text."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        message = {
            "message_id": "message-id",
            "task_id": "task-id",
            "payload": {"report_path": "reports/task-id/report.yaml"},
        }
        raw_secret = "UNTRUSTED-WORKER-EXCEPTION-SECRET"
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            queue_root = state_root / "queue"
            session_dir.mkdir(parents=True)
            queue_root.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps({"organization_instance_id": "org-worker-redaction"}),
                encoding="utf-8",
            )
            inbox_mock = mock.Mock()
            metric_mock = mock.Mock()
            with mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=canonical,
            ), mock.patch.object(
                builder,
                "queue_root_for",
                return_value=queue_root,
            ), mock.patch.object(
                builder,
                "claim_pending_message",
                return_value=(message, {}),
            ), mock.patch.object(
                builder,
                "role_agent_load_instruction",
                side_effect=ValueError(raw_secret),
            ), mock.patch.object(
                builder,
                "update_inbox_message",
                inbox_mock,
            ), mock.patch.object(
                builder,
                "append_queue_metric",
                metric_mock,
            ):
                output = builder.role_agent_step_once(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "role_id": "tech-qa",
                        "provider_evidence": {
                            "usage_source": "codex_exec_json",
                            "effective_model": "gpt-5.6-luna",
                            "provider_session_id": "provider-session",
                            "request_id": "provider-request",
                            "transcript_path": "/tmp/provider.jsonl",
                        },
                    },
                )

            worker = output["roleAgentWorker"]
            inbox_update = inbox_mock.call_args.args[4]
            metric_extra = metric_mock.call_args.kwargs["extra"]
            report_path = Path(worker["report_path"])
            report_text = report_path.read_text(encoding="utf-8")
        self.assertEqual(worker["error"], "role_agent_worker_failed")
        self.assertEqual(worker["error_type"], "ValueError")
        self.assertEqual(worker["report"]["error"], "role_agent_worker_failed")
        self.assertEqual(inbox_update["error"], "role_agent_worker_failed")
        self.assertEqual(inbox_update["error_type"], "ValueError")
        self.assertEqual(metric_extra["error"], "role_agent_worker_failed")
        self.assertEqual(metric_extra["error_type"], "ValueError")
        failed_evidence = worker["report"]["evidence"]
        self.assertEqual(
            set(failed_evidence),
            {
                "provider",
                "intended_model",
                "effective_model",
                "reported_model_metadata_valid",
                "provider_identity_status",
                "usage_source",
            },
        )
        for forbidden in (
            "provider_session_id",
            "request_id",
            "transcript_path",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            self.assertNotIn(forbidden, failed_evidence)
            self.assertNotIn(forbidden, metric_extra)
        for persisted in (output, inbox_update, metric_extra, report_text):
            self.assertNotIn(raw_secret, json.dumps(persisted))

    def test_failed_terminal_report_identity_is_sanitized_and_rebound_before_recovery(self) -> None:
        """Reject raw failed identity and preserve sanitized invalid status through recovery."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        raw_evidence = {
            "provider": "openai",
            "intended_model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-luna",
            "reported_effective_model": "gpt-5.6-sol",
            "usage_source": "codex_exec_json",
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "transcript_path": "/tmp/provider.jsonl",
        }
        base_report = {
            "report_version": "1",
            "report_type": "role_agent_worker_report",
            "from_role": "tech-qa",
            "task_id": "task-id",
            "message_id": "message-id",
            "created_at": "2026-09-01T00:00:00+09:00",
            "result": "failed",
            "status": "failed",
            "summary": "provider identity rejected",
        }
        raw_report = base_report | {"evidence": raw_evidence}
        sanitized_evidence = builder.sanitize_invalid_provider_evidence(raw_evidence)
        sanitized_report = base_report | {"evidence": sanitized_evidence}
        done_with_invalid = sanitized_report | {"result": "completed", "status": "done"}

        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            self.assertTrue(
                builder.validate_terminal_queue_report(
                    raw_report,
                    role_id="tech-qa",
                    message_id="message-id",
                )
            )
            self.assertEqual(
                builder.validate_terminal_queue_report(
                    sanitized_report,
                    role_id="tech-qa",
                    message_id="message-id",
                ),
                [],
            )
            self.assertTrue(
                builder.validate_terminal_queue_report(
                    done_with_invalid,
                    role_id="tech-qa",
                    message_id="message-id",
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            queue_root = state_root / "queue"
            report_ref = "reports/task-id/failed-message-id.yaml"
            report_path = queue_root / report_ref
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(sanitized_report), encoding="utf-8")
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(json.dumps([canonical]), encoding="utf-8")
            message = {
                "message_id": "message-id",
                "task_id": "task-id",
                "payload": {"report_path": report_ref},
            }
            merge_mock = mock.Mock(return_value=[canonical])
            metric_mock = mock.Mock()
            with mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=canonical,
            ), mock.patch.object(
                builder,
                "update_inbox_message",
            ), mock.patch.object(
                builder,
                "merge_roster_agent_row_locked",
                merge_mock,
            ), mock.patch.object(
                builder,
                "append_jsonl_atomic",
            ), mock.patch.object(
                builder,
                "append_queue_metric",
                metric_mock,
            ), mock.patch.object(
                builder,
                "maybe_update_tpm_team_completion_check",
                return_value={"result": "skipped_not_tpm"},
            ):
                event = builder.recover_pending_message_from_existing_report(
                    runtime="codex",
                    session_dir=session_dir,
                    session_id="session",
                    organization_instance_id="org-report-recovery",
                    queue_root=queue_root,
                    inbox_path=queue_root / "inbox/tech-qa.yaml",
                    role_id="tech-qa",
                    role_row=canonical,
                    message=message,
                    now="2026-09-01T00:01:00+09:00",
                )

        self.assertIsNotNone(event)
        row_update = merge_mock.call_args.args[3]
        self.assertEqual(row_update["effective_model"], "")
        self.assertEqual(row_update["provider_identity_status"], "invalid")
        self.assertEqual(row_update["provider_status"], "provider_report_recovered_invalid_identity")
        metric_extra = metric_mock.call_args.kwargs["extra"]
        self.assertEqual(metric_extra["effective_model"], "")
        self.assertEqual(metric_extra["provider_identity_status"], "invalid")

    def test_provider_policy_launch_lease_is_descriptor_bound_and_owner_checked(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "PROVIDER_POLICY_LAUNCH_LOCK_ROOT",
            Path(tmp) / "policy-locks",
        ):
            lease = builder.acquire_provider_policy_launch_lease(timeout_seconds=0.1)
            with self.assertRaises(TimeoutError):
                builder.acquire_provider_policy_launch_lease(timeout_seconds=0.01)
            with self.assertRaises(ValueError):
                builder.release_provider_policy_launch_lease(
                    lease,
                    lease_id="not-the-owner",
                )
            self.assertFalse(lease["released"])
            builder.release_provider_policy_launch_lease(
                lease,
                lease_id=lease["lease_id"],
            )
            builder.release_provider_policy_launch_lease(
                lease,
                lease_id=lease["lease_id"],
            )
            replacement = builder.acquire_provider_policy_launch_lease(timeout_seconds=0.1)
            builder.release_provider_policy_launch_lease(
                replacement,
                lease_id=replacement["lease_id"],
            )

    def test_role_runtime_provider_policy_lock_namespace_matches(self) -> None:
        repo_root = SKILL_ROOT.parents[2]
        role_builder_path = (
            repo_root
            / "organization/roles/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"
        )
        runtime_builder_path = (
            repo_root
            / "organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"
        )

        def load_named(path: Path, name: str):
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"failed to load builder mirror: {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        role_builder = load_named(role_builder_path, "itb_role_lock_namespace_test")
        runtime_builder = load_named(runtime_builder_path, "itb_runtime_lock_namespace_test")
        self.assertEqual(
            role_builder.provider_policy_launch_lock_name(),
            runtime_builder.provider_policy_launch_lock_name(),
        )
        self.assertEqual(
            role_builder.PROVIDER_POLICY_LAUNCH_LOCK_ROOT,
            runtime_builder.PROVIDER_POLICY_LAUNCH_LOCK_ROOT,
        )
        original_name = role_builder.provider_policy_launch_lock_name()
        with mock.patch.object(
            role_builder,
            "MODEL_REGISTRY",
            Path("/different/model-registry.md"),
        ), mock.patch.object(
            role_builder,
            "ROLE_AGENT_REGISTRY",
            Path("/different/role-agent-registry.yaml"),
        ):
            self.assertEqual(
                role_builder.provider_policy_launch_lock_name(),
                original_name,
            )

    def test_provider_launch_guard_releases_lease_at_process_start(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-backend",
            "role_id": "tech-backend",
            "organization_instance_id": "org-launch-linearization",
            "status": "active",
            "always_active": False,
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            bound, policy_error = builder.canonical_codex_execution_policy(
                canonical,
                organization_instance_id="org-launch-linearization",
            )
        self.assertEqual(policy_error, "")

        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            builder,
            "PROVIDER_POLICY_LAUNCH_LOCK_ROOT",
            Path(tmp) / "policy-locks",
        ), mock.patch.object(
            builder,
            "role_agent_row_for",
            return_value=canonical,
        ), mock.patch.object(
            builder.shutil,
            "which",
            return_value=sys.executable,
        ):
            writer_acquired = threading.Event()
            launch_finished = threading.Event()
            interlock = {
                "blocked_before_popen": False,
                "writer_acquired_before_output_complete": False,
            }
            writer_threads: list[threading.Thread] = []

            def cooperative_writer() -> None:
                writer_lease = builder.acquire_provider_policy_launch_lease(
                    timeout_seconds=2.0
                )
                interlock["writer_acquired_before_output_complete"] = not launch_finished.is_set()
                writer_acquired.set()
                builder.release_provider_policy_launch_lease(
                    writer_lease,
                    lease_id=writer_lease["lease_id"],
                )

            def checking_popen(*args, **kwargs):
                writer = threading.Thread(target=cooperative_writer)
                writer_threads.append(writer)
                writer.start()
                time.sleep(0.1)
                interlock["blocked_before_popen"] = not writer_acquired.is_set()
                return real_popen(*args, **kwargs)

            with mock.patch.object(
                builder.subprocess,
                "Popen",
                side_effect=checking_popen,
            ):
                launch = builder.launch_provider_with_canonical_policy(
                    bound_execution_row=bound,
                    organization_instance_id="org-launch-linearization",
                    executable_name="python3",
                    command_builder=lambda _final_row: [
                        sys.executable,
                        "-c",
                        "import sys,time; time.sleep(0.4); sys.stdout.write('{}')",
                    ],
                    runner=builder.run_command_with_bounded_output,
                    timeout=10,
                )
            launch_finished.set()
            for writer in writer_threads:
                writer.join(timeout=2.0)

        self.assertEqual(launch["status"], "started")
        self.assertEqual(launch["completed"].stdout, "{}")
        self.assertTrue(interlock["blocked_before_popen"])
        self.assertTrue(writer_acquired.is_set())
        self.assertTrue(interlock["writer_acquired_before_output_complete"])
        self.assertEqual(
            launch["initial_policy_digest"],
            launch["launch_policy_digest"],
        )

    def test_bounded_runner_terminates_child_when_process_start_callback_fails(self) -> None:
        builder = load_builder_module()
        process_ref: dict[str, object] = {}

        def reject_process_start(process) -> None:
            process_ref["process"] = process
            raise RuntimeError("launch lease release failed")

        with self.assertRaisesRegex(RuntimeError, "launch lease release failed"):
            builder.run_command_with_bounded_output(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=2,
                process_started=reject_process_start,
            )
        process = process_ref["process"]
        self.assertIsNotNone(process.poll())

    def test_prelaunch_digest_covers_authorization_and_routing_fields(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-backend",
            "role_id": "tech-backend",
            "organization_instance_id": "org-prelaunch-digest",
            "status": "active",
            "always_active": False,
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            bound, policy_error = builder.canonical_codex_execution_policy(
                canonical,
                organization_instance_id="org-prelaunch-digest",
            )
        self.assertEqual(policy_error, "")
        mutations = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "organization_instance_id": "org-prelaunch-digest-changed",
            "status": "inactive",
            "always_active": True,
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "gpt-5.6-sol",
            "fallback_models": "gpt-5.6-sol",
            "allowed_tools": ["Read", "Write"],
            "git_operations_allowed": True,
            "queue_consumer": True,
            "queue_finalizer": "none",
        }
        self.assertEqual(
            set(mutations),
            set(builder.CANONICAL_PROVIDER_EXECUTION_FIELDS),
        )
        initial_digest = builder.canonical_execution_policy_digest(bound)
        self.assertEqual(initial_digest, bound["canonical_execution_policy_digest"])
        for field_name, changed_value in mutations.items():
            changed = dict(bound)
            changed[field_name] = changed_value
            with self.subTest(field=field_name):
                final_digest = builder.canonical_execution_policy_digest(changed)
            self.assertTrue(final_digest)
            self.assertNotEqual(final_digest, initial_digest)

    def test_provider_consumers_block_policy_drift_before_launch(self) -> None:
        builder = load_builder_module()
        provider_cases = (
            ("codex_direct", "openai", "codex", "gpt-5.6-luna"),
            ("codex_activation", "openai", "codex", "gpt-5.6-luna"),
            ("claude_direct", "anthropic", "claude", "claude-opus-4-6"),
            ("claude_activation", "anthropic", "claude", "claude-opus-4-6"),
        )
        for consumer, provider, execution_mode, model in provider_cases:
            with self.subTest(consumer=consumer), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                agent_id = "tech-backend" if provider == "openai" else "legacy-claude-role"
                canonical = {
                    "agent_id": agent_id,
                    "role_id": agent_id,
                    "organization_instance_id": "org-prelaunch-drift",
                    "status": "active",
                    "always_active": False,
                    "provider": provider,
                    "execution_mode": execution_mode,
                    "intended_model": model,
                    "fallback_models": "",
                    "allowed_tools": ["Read"],
                    "git_operations_allowed": False,
                    "queue_consumer": False,
                    "queue_finalizer": "role-report",
                }
                changed = dict(canonical)
                changed["allowed_tools"] = ["Read", "Write"]
                stale = dict(canonical)
                stale.update(
                    {
                        "activation_status": "response_active",
                        "response_status": "invoked",
                        "provider_status": "provider_response_ready",
                        "usage_source": "codex_exec_json" if provider == "openai" else "claude_print_json",
                        "effective_model": model,
                        "session_id": "stale-provider-session",
                        "last_request_id": "stale-request",
                    }
                )
                (session_dir / "bootstrap.json").write_text(
                    json.dumps(
                        {
                            "organization_instance_id": "org-prelaunch-drift",
                            "readiness_scope": "response_evidence",
                            "provider_response_scope": "response_evidence",
                            "provider_response_ready_count": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                (session_dir / "roster.json").write_text(json.dumps([stale]), encoding="utf-8")
                lease_state = {"acquired": False, "released": False}

                def current_policy(*_args, **_kwargs):
                    return changed if lease_state["acquired"] else canonical

                def acquire_lease(*_args, **_kwargs):
                    lease_state["acquired"] = True
                    return {
                        "lease_id": "lease-prelaunch-drift",
                        "released": False,
                    }

                def release_lease(lease, *, lease_id):
                    self.assertEqual(lease_id, "lease-prelaunch-drift")
                    lease["released"] = True
                    lease_state["released"] = True

                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    side_effect=current_policy,
                ), mock.patch.object(
                    builder,
                    "acquire_provider_policy_launch_lease",
                    side_effect=acquire_lease,
                ), mock.patch.object(
                    builder,
                    "release_provider_policy_launch_lease",
                    side_effect=release_lease,
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    side_effect=AssertionError("command discovery must not run after prelaunch drift"),
                ) as which_mock, mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    side_effect=AssertionError("Codex must not launch after prelaunch drift"),
                ) as codex_mock, mock.patch.object(
                    builder,
                    "run_claude_command_with_bounded_output",
                    side_effect=AssertionError("Claude must not launch after prelaunch drift"),
                ) as claude_mock:
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-prelaunch-drift",
                        "agent_id": agent_id,
                        "request_id": f"req-{consumer}",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    }
                    if consumer == "codex_direct":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    elif consumer == "claude_direct":
                        output = builder.claude_cli_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )

                self.assertEqual(output["decision"], "block")
                self.assertEqual(output["reason"], builder.PROVIDER_POLICY_DRIFT_NOTE)
                self.assertTrue(lease_state["released"])
                which_mock.assert_not_called()
                codex_mock.assert_not_called()
                claude_mock.assert_not_called()
                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                row = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))[0]
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                self.assertEqual(row["provider_status"], "provider_policy_drift")
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["effective_model"], "")
                self.assertEqual(state["provider_response_ready_count"], 0)
                self.assertEqual(evidence["result"], "provider_policy_drift")
                self.assertFalse(evidence["provider_invoked"])
                self.assertEqual(evidence["policy_check_phase"], "prelaunch")
                self.assertEqual(evidence["launch_lock"], "acquired")
                self.assertTrue(evidence["initial_canonical_execution_policy_digest"])
                self.assertTrue(evidence["launch_policy_digest"])
                self.assertNotEqual(
                    evidence["initial_canonical_execution_policy_digest"],
                    evidence["launch_policy_digest"],
                )

    def test_provider_consumers_record_positive_launch_evidence(self) -> None:
        builder = load_builder_module()
        provider_cases = (
            ("codex_direct", "openai", "codex", "gpt-5.6-luna"),
            ("codex_activation", "openai", "codex", "gpt-5.6-luna"),
            ("claude_direct", "anthropic", "claude", "claude-opus-4-6"),
            ("claude_activation", "anthropic", "claude", "claude-opus-4-6"),
        )
        for consumer, provider, execution_mode, model in provider_cases:
            with self.subTest(consumer=consumer), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                agent_id = "tech-backend" if provider == "openai" else "legacy-claude-role"
                canonical = {
                    "agent_id": agent_id,
                    "role_id": agent_id,
                    "organization_instance_id": "org-positive-launch",
                    "status": "active",
                    "always_active": False,
                    "provider": provider,
                    "execution_mode": execution_mode,
                    "intended_model": model,
                    "fallback_models": "",
                    "allowed_tools": ["Read"],
                    "git_operations_allowed": False,
                    "queue_consumer": False,
                    "queue_finalizer": "role-report",
                }
                (session_dir / "roster.json").write_text(json.dumps([canonical]), encoding="utf-8")
                if provider == "openai":
                    completed = subprocess.CompletedProcess(
                        args=["codex"],
                        returncode=0,
                        stdout=current_codex_jsonl(),
                        stderr="",
                    )
                else:
                    completed = subprocess.CompletedProcess(
                        args=["claude"],
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "result": "review complete",
                                "model": model,
                                "usage": {"input_tokens": 1, "output_tokens": 2},
                                "duration_api_ms": 3,
                                "session_id": "provider-session",
                                "request_id": "provider-request",
                                "num_turns": 1,
                            }
                        ),
                        stderr="",
                    )

                def run_provider(command, *, timeout, process_started):
                    self.assertGreater(timeout, 0)
                    process_started(mock.Mock())
                    completed.args = command
                    return completed

                provider_runner = (
                    "run_command_with_bounded_output"
                    if provider == "openai"
                    else "run_claude_command_with_bounded_output"
                )
                other_runner = (
                    "run_claude_command_with_bounded_output"
                    if provider == "openai"
                    else "run_command_with_bounded_output"
                )
                with mock.patch.object(
                    builder,
                    "PROVIDER_POLICY_LAUNCH_LOCK_ROOT",
                    Path(tmp) / "policy-locks",
                ), mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical,
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value=f"/usr/bin/{'codex' if provider == 'openai' else 'claude'}",
                ), mock.patch.object(
                    builder,
                    provider_runner,
                    side_effect=run_provider,
                ) as run_mock, mock.patch.object(
                    builder,
                    other_runner,
                    side_effect=AssertionError("wrong provider runner selected"),
                ) as other_mock:
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-positive-launch",
                        "agent_id": agent_id,
                        "request_id": f"req-{consumer}",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    }
                    if consumer == "codex_direct":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    elif consumer == "claude_direct":
                        output = builder.claude_cli_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )

                self.assertNotIn("decision", output)
                run_mock.assert_called_once()
                other_mock.assert_not_called()
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                self.assertEqual(evidence["result"], "provider_response_ready")
                self.assertTrue(evidence["provider_invoked"])
                self.assertEqual(evidence["policy_check_phase"], "postlaunch")
                self.assertEqual(evidence["launch_lock"], "released_after_process_start")
                self.assertTrue(evidence["initial_canonical_execution_policy_digest"])
                self.assertEqual(
                    evidence["initial_canonical_execution_policy_digest"],
                    evidence["launch_policy_digest"],
                )

    def test_provider_consumers_block_launch_lease_failure_before_command_discovery(self) -> None:
        builder = load_builder_module()
        provider_cases = (
            ("codex_direct", "openai", "codex", "gpt-5.6-luna"),
            ("codex_activation", "openai", "codex", "gpt-5.6-luna"),
            ("claude_direct", "anthropic", "claude", "claude-opus-4-6"),
            ("claude_activation", "anthropic", "claude", "claude-opus-4-6"),
        )
        failure_cases = (
            (
                "timeout",
                TimeoutError("busy"),
                "provider policy launch lease timed out",
                "provider_launch_lock_timeout",
            ),
            (
                "unavailable",
                OSError("unsafe lock path detail"),
                "provider policy launch lease is unavailable",
                "provider_launch_lock_unavailable",
            ),
        )
        for consumer, provider, execution_mode, model in provider_cases:
            for failure_name, failure, expected_reason, expected_result in failure_cases:
                with self.subTest(consumer=consumer, failure=failure_name), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    agent_id = "tech-backend" if provider == "openai" else "legacy-claude-role"
                    canonical = {
                        "agent_id": agent_id,
                        "role_id": agent_id,
                        "organization_instance_id": "org-launch-lock-failure",
                        "status": "active",
                        "always_active": False,
                        "provider": provider,
                        "execution_mode": execution_mode,
                        "intended_model": model,
                        "fallback_models": "",
                        "allowed_tools": ["Read"],
                        "git_operations_allowed": False,
                        "queue_consumer": False,
                        "queue_finalizer": "role-report",
                    }
                    (session_dir / "roster.json").write_text(
                        json.dumps([canonical]),
                        encoding="utf-8",
                    )
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        return_value=canonical,
                    ), mock.patch.object(
                        builder,
                        "acquire_provider_policy_launch_lease",
                        side_effect=failure,
                    ), mock.patch.object(
                        builder.shutil,
                        "which",
                        side_effect=AssertionError("command discovery must not run on lease failure"),
                    ) as which_mock, mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                        side_effect=AssertionError("Codex must not run on lease failure"),
                    ) as codex_mock, mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                        side_effect=AssertionError("Claude must not run on lease failure"),
                    ) as claude_mock:
                        hook_input = {
                            "session_id": "session",
                            "organization_instance_id": "org-launch-lock-failure",
                            "agent_id": agent_id,
                            "request_id": f"req-{consumer}-{failure_name}",
                            "cwd": "/tmp/project",
                            "prompt": "Review only.",
                        }
                        if consumer == "codex_direct":
                            output = builder.codex_exec_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        elif consumer == "claude_direct":
                            output = builder.claude_cli_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        else:
                            output = builder.provider_activate(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )

                    self.assertEqual(output["decision"], "block")
                    self.assertEqual(output["reason"], expected_reason)
                    self.assertNotIn("unsafe lock path detail", json.dumps(output))
                    which_mock.assert_not_called()
                    codex_mock.assert_not_called()
                    claude_mock.assert_not_called()
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()[-1]
                    )
                    self.assertEqual(evidence["result"], expected_result)
                    self.assertFalse(evidence["provider_invoked"])
                    self.assertEqual(evidence["policy_check_phase"], "prelaunch")
                    self.assertEqual(evidence["launch_lock"], failure_name)

    def test_generic_dispatch_never_launches_provider_selected_by_stale_policy(self) -> None:
        builder = load_builder_module()
        codex_policy = {
            "agent_id": "tech-backend",
            "role_id": "tech-backend",
            "organization_instance_id": "org-generic-route-drift",
            "status": "active",
            "always_active": False,
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        claude_policy = dict(codex_policy)
        claude_policy.update(
            {
                "provider": "anthropic",
                "execution_mode": "claude",
                "intended_model": "claude-opus-4-6",
            }
        )
        calls = {"count": 0}

        def changing_policy(*_args, **_kwargs):
            calls["count"] += 1
            return codex_policy if calls["count"] == 1 else claude_policy

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(json.dumps([codex_policy]), encoding="utf-8")
            with mock.patch.object(
                builder,
                "role_agent_row_for",
                side_effect=changing_policy,
            ), mock.patch.object(
                builder,
                "acquire_provider_policy_launch_lease",
                side_effect=AssertionError("stale generic route must fail before launch lease"),
            ) as lease_mock, mock.patch.object(
                builder.shutil,
                "which",
                side_effect=AssertionError("stale generic route must not discover a command"),
            ) as which_mock, mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=AssertionError("stale generic route must not launch Codex"),
            ) as run_mock:
                output = builder.agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "organization_instance_id": "org-generic-route-drift",
                        "agent_id": "tech-backend",
                        "request_id": "req-generic-route-drift",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            self.assertEqual(output["decision"], "block")
            lease_mock.assert_not_called()
            which_mock.assert_not_called()
            run_mock.assert_not_called()
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(evidence["result"], "provider_model_policy_invalid")
            self.assertFalse(evidence["provider_invoked"])

    def test_provider_consumers_block_policy_drift_after_execution(self) -> None:
        """Never return success when canonical policy changes during a provider call."""
        builder = load_builder_module()
        provider_cases = (
            ("codex_direct", "openai", "codex", "gpt-5.6-luna"),
            ("codex_activation", "openai", "codex", "gpt-5.6-luna"),
            ("claude_direct", "anthropic", "claude", "claude-opus-4-6"),
            ("claude_activation", "anthropic", "claude", "claude-opus-4-6"),
        )
        for consumer, provider, execution_mode, model in provider_cases:
            with self.subTest(consumer=consumer), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                agent_id = "tech-backend" if provider == "openai" else "legacy-claude-role"
                canonical = {
                    "agent_id": agent_id,
                    "role_id": agent_id,
                    "organization_instance_id": "org-runtime-drift",
                    "status": "active",
                    "always_active": False,
                    "provider": provider,
                    "execution_mode": execution_mode,
                    "intended_model": model,
                    "fallback_models": "",
                    "allowed_tools": ["Read"],
                    "git_operations_allowed": False,
                    "queue_consumer": False,
                    "queue_finalizer": "role-report",
                }
                changed = dict(canonical)
                changed["allowed_tools"] = ["Read", "Write"]
                (session_dir / "roster.json").write_text(json.dumps([canonical]), encoding="utf-8")
                provider_called = {"value": False}

                def current_policy(*_args: object, **_kwargs: object) -> dict[str, object]:
                    return changed if provider_called["value"] else canonical

                if provider == "openai":
                    completed = subprocess.CompletedProcess(
                        args=["codex"],
                        returncode=0,
                        stdout=current_codex_jsonl(),
                        stderr="",
                    )

                    def run_provider(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                        provider_called["value"] = True
                        return completed

                    provider_patch = mock.patch.object(
                        builder,
                        "run_command_with_bounded_output",
                        side_effect=run_provider,
                    )
                else:
                    completed = subprocess.CompletedProcess(
                        args=["claude"],
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "result": "review complete",
                                "model": model,
                                "usage": {"input_tokens": 1, "output_tokens": 2},
                                "duration_api_ms": 3,
                                "session_id": "provider-session",
                                "request_id": "provider-request",
                                "num_turns": 1,
                            }
                        ),
                        stderr="",
                    )

                    def run_provider(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                        provider_called["value"] = True
                        return completed

                    provider_patch = mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                        side_effect=run_provider,
                    )

                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    side_effect=current_policy,
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value=f"/usr/bin/{'codex' if provider == 'openai' else 'claude'}",
                ), provider_patch as provider_mock:
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-runtime-drift",
                        "agent_id": agent_id,
                        "request_id": f"req-{consumer}",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    }
                    if consumer == "codex_direct":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    elif consumer == "claude_direct":
                        output = builder.claude_cli_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )

                provider_mock.assert_called_once()
                self.assertTrue(provider_called["value"])
                self.assertEqual(output["decision"], "block")
                self.assertEqual(output["reason"], builder.PROVIDER_POLICY_DRIFT_NOTE)
                if consumer.endswith("_direct"):
                    self.assertEqual(output["agentDispatch"]["result"], "provider_policy_drift")
                    self.assertEqual(output["agentDispatch"]["effective_model"], "")
                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                row = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))[0]
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                self.assertEqual(row["provider_status"], "provider_policy_drift")
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["effective_model"], "")
                self.assertEqual(state["provider_response_ready_count"], 0)
                self.assertNotEqual(state["readiness_scope"], "response_evidence")
                self.assertEqual(evidence["result"], "provider_policy_drift")
                self.assertTrue(evidence["policy_drift_detected"])
                self.assertEqual(
                    evidence["canonical_execution_policy_digest"],
                    row["canonical_execution_policy_digest"],
                )

    def test_codex_launch_oserror_clears_stale_readiness_without_leaking_details(self) -> None:
        """Route Codex launch errors through bounded evidence and stale-state reset."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-backend",
            "role_id": "tech-backend",
            "organization_instance_id": "org-codex-launch-failure",
            "status": "active",
            "always_active": False,
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        raw_exception_detail = "untrusted command path and prompt detail"
        for entrypoint in ("direct", "activation"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                stale_row = dict(canonical)
                stale_row.update(
                    {
                        "activation_status": "response_active",
                        "response_status": "invoked",
                        "provider_status": "provider_response_ready",
                        "effective_model": "gpt-5.6-luna",
                        "usage_source": "codex_exec_json",
                    }
                )
                (session_dir / "roster.json").write_text(
                    json.dumps([stale_row]),
                    encoding="utf-8",
                )
                (session_dir / "bootstrap.json").write_text(
                    json.dumps(
                        {
                            "runtime": "codex",
                            "session_id": "session",
                            "organization_instance_id": "org-codex-launch-failure",
                            "cwd": "/tmp/project",
                            "readiness_scope": "response_evidence",
                        }
                    ),
                    encoding="utf-8",
                )
                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical,
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value="/usr/bin/codex",
                ), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    side_effect=OSError(13, raw_exception_detail),
                ):
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-codex-launch-failure",
                        "agent_id": "tech-backend",
                        "request_id": f"req-{entrypoint}-launch-failure",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    }
                    if entrypoint == "direct":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )

                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                row = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))[0]
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                self.assertEqual(
                    output,
                    {"decision": "block", "reason": "codex provider process failed to start"},
                )
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["provider_status"], "provider_process_failed")
                self.assertEqual(row["effective_model"], "")
                self.assertEqual(state["provider_response_ready_count"], 0)
                self.assertEqual(state["readiness_scope"], "metadata_only")
                self.assertEqual(evidence["provider_exception_type"], "OSError")
                self.assertEqual(evidence["provider_exception_errno"], 13)
                expected_result = (
                    "provider_process_failed"
                    if entrypoint == "direct"
                    else "provider_activation_failed"
                )
                self.assertEqual(evidence["result"], expected_result)
                self.assertNotIn(raw_exception_detail, json.dumps(output))
                self.assertNotIn(raw_exception_detail, json.dumps(row))
                self.assertNotIn(raw_exception_detail, json.dumps(evidence))

    def test_missing_provider_commands_clear_stale_readiness_and_persist_evidence(self) -> None:
        """Fail closed at every direct/activation command-availability boundary."""
        builder = load_builder_module()
        cases = (
            (
                "codex",
                "direct",
                "tech-backend",
                "openai",
                "codex",
                "gpt-5.6-luna",
                "codex_exec_json_command_unavailable",
            ),
            (
                "codex",
                "activation",
                "tech-backend",
                "openai",
                "codex",
                "gpt-5.6-luna",
                "codex_exec_json_command_unavailable",
            ),
            (
                "claude",
                "direct",
                "legacy-claude-role",
                "anthropic",
                "claude",
                "claude-opus-4-6",
                "claude_print_json_command_unavailable",
            ),
            (
                "claude",
                "activation",
                "legacy-claude-role",
                "anthropic",
                "claude",
                "claude-opus-4-6",
                "claude_print_json_command_unavailable",
            ),
        )
        for (
            provider_label,
            entrypoint,
            agent_id,
            provider,
            execution_mode,
            intended_model,
            usage_source,
        ) in cases:
            with self.subTest(
                provider=provider_label,
                entrypoint=entrypoint,
            ), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                canonical = {
                    "agent_id": agent_id,
                    "role_id": agent_id,
                    "organization_instance_id": "org-command-unavailable",
                    "status": "active",
                    "always_active": False,
                    "provider": provider,
                    "execution_mode": execution_mode,
                    "intended_model": intended_model,
                    "fallback_models": "",
                    "allowed_tools": ["Read"],
                    "git_operations_allowed": False,
                    "queue_consumer": False,
                    "queue_finalizer": "role-report",
                }
                stale_row = {
                    **canonical,
                    "activation_status": "response_active",
                    "response_status": "invoked",
                    "provider_status": "provider_response_ready",
                    "model": intended_model,
                    "effective_model": intended_model,
                    "effectiveModel": intended_model,
                    "reported_effective_model": intended_model,
                    "reported_model_metadata_valid": True,
                    "provider_identity_status": "valid",
                    "usage_source": (
                        "codex_exec_json"
                        if provider_label == "codex"
                        else "claude_print_json"
                    ),
                }
                (session_dir / "roster.json").write_text(
                    json.dumps([stale_row]),
                    encoding="utf-8",
                )
                (session_dir / "bootstrap.json").write_text(
                    json.dumps(
                        {
                            "runtime": "codex",
                            "session_id": "session",
                            "organization_instance_id": "org-command-unavailable",
                            "cwd": "/tmp/project",
                            "readiness_scope": "response_evidence",
                        }
                    ),
                    encoding="utf-8",
                )
                codex_runner = mock.Mock(
                    side_effect=AssertionError("Codex runner must not be invoked")
                )
                claude_runner = mock.Mock(
                    side_effect=AssertionError("Claude runner must not be invoked")
                )
                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical,
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value=None,
                ), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    codex_runner,
                ), mock.patch.object(
                    builder,
                    "run_claude_command_with_bounded_output",
                    claude_runner,
                ):
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-command-unavailable",
                        "agent_id": agent_id,
                        "request_id": f"req-{provider_label}-{entrypoint}-missing",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    }
                    if entrypoint == "activation":
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    elif provider_label == "codex":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.claude_cli_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )

                self.assertEqual(
                    output,
                    {
                        "decision": "block",
                        "reason": f"{provider_label} command not found",
                    },
                )
                state = json.loads(
                    (session_dir / "bootstrap.json").read_text(encoding="utf-8")
                )
                row = json.loads(
                    (session_dir / "roster.json").read_text(encoding="utf-8")
                )[0]
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["provider_status"], "provider_command_unavailable")
                self.assertEqual(row["effective_model"], "")
                for alias in ("model", "effectiveModel", "reported_effective_model"):
                    self.assertNotIn(alias, row)
                self.assertNotIn("reported_model_metadata_valid", row)
                self.assertNotIn("provider_identity_status", row)
                self.assertEqual(state["provider_response_ready_count"], 0)
                self.assertEqual(state["readiness_scope"], "metadata_only")
                self.assertEqual(evidence["result"], "provider_command_unavailable")
                self.assertEqual(evidence["usage_source"], usage_source)
                self.assertFalse(evidence["provider_invoked"])
                self.assertFalse(evidence["provider_command_available"])
                self.assertEqual(
                    evidence["canonical_execution_policy_digest"],
                    row["canonical_execution_policy_digest"],
                )
                codex_runner.assert_not_called()
                claude_runner.assert_not_called()

    def test_claude_failures_clear_stale_readiness_and_preserve_policy_evidence(self) -> None:
        """Process and parse failures must invalidate an earlier Claude response."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-claude-failure",
            "status": "active",
            "always_active": False,
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        raw_process_detail = "untrusted provider stderr detail"
        def bounded_failure(attribute: str, value: object) -> subprocess.CompletedProcess[str]:
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=-9,
                stdout=raw_process_detail,
                stderr=raw_process_detail,
            )
            setattr(completed, attribute, value)
            return completed

        failure_cases = (
            (
                "process",
                subprocess.CompletedProcess(
                    args=["claude"],
                    returncode=1,
                    stdout="",
                    stderr=raw_process_detail,
                ),
                "provider_process_failed",
                "claude provider process failed",
            ),
            (
                "parse",
                subprocess.CompletedProcess(
                    args=["claude"],
                    returncode=0,
                    stdout="{not-json",
                    stderr="",
                ),
                "provider_output_parse_failed",
                "claude provider output was not valid JSON",
            ),
            (
                "duplicate_model_key",
                subprocess.CompletedProcess(
                    args=["claude"],
                    returncode=0,
                    stdout=(
                        '{"result":"review complete",'
                        '"model":"claude-sonnet-4-6",'
                        '"model":"claude-opus-4-6",'
                        '"usage":{"input_tokens":1,"output_tokens":2},'
                        '"duration_api_ms":3,"session_id":"provider-session","num_turns":1}'
                    ),
                    stderr="",
                ),
                "provider_output_parse_failed",
                "claude provider output was not valid JSON",
            ),
            (
                "bounded_timeout",
                bounded_failure("output_timed_out", True),
                "provider_response_timeout",
                "claude provider process exceeded its execution timeout",
            ),
            (
                "bounded_limit",
                bounded_failure("output_limit_exceeded", "stdout"),
                "provider_output_limit_exceeded",
                "claude provider output exceeded the bounded byte limit: stdout",
            ),
            (
                "bounded_read",
                bounded_failure("output_read_error", "OSError"),
                "provider_output_read_failed",
                "claude provider output could not be read safely: OSError",
            ),
            (
                "bounded_decode",
                bounded_failure("output_decode_error", True),
                "provider_output_decode_failed",
                "claude provider output was not valid UTF-8",
            ),
        )
        for entrypoint in ("direct", "activation"):
            for case, completed, evidence_result, public_reason in failure_cases:
                with self.subTest(entrypoint=entrypoint, case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    stale_row = dict(canonical)
                    stale_row.update(
                        {
                            "activation_status": "response_active",
                            "response_status": "invoked",
                            "provider_status": "provider_response_ready",
                            "effective_model": canonical["intended_model"],
                            "usage_source": "claude_print_json",
                        }
                    )
                    (session_dir / "roster.json").write_text(json.dumps([stale_row]), encoding="utf-8")
                    (session_dir / "bootstrap.json").write_text(
                        json.dumps(
                            {
                                "runtime": "codex",
                                "session_id": "session",
                                "organization_instance_id": "org-claude-failure",
                                "cwd": "/tmp/project",
                                "readiness_scope": "response_evidence",
                            }
                        ),
                        encoding="utf-8",
                    )
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        return_value=canonical,
                    ), mock.patch.object(
                        builder.shutil,
                        "which",
                        return_value="/usr/bin/claude",
                    ), mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                        return_value=completed,
                    ):
                        hook_input = {
                            "session_id": "session",
                            "organization_instance_id": "org-claude-failure",
                            "agent_id": "legacy-claude-role",
                            "request_id": f"req-{entrypoint}-{case}",
                            "prompt": "Review only.",
                        }
                        if entrypoint == "direct":
                            output = builder.claude_cli_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        else:
                            output = builder.provider_activate(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )

                    self.assertEqual(output, {"decision": "block", "reason": public_reason})
                    self.assertNotIn(raw_process_detail, json.dumps(output))
                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    row = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))[0]
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    self.assertEqual(row["response_status"], "not_invoked")
                    self.assertEqual(row["effective_model"], "")
                    self.assertNotIn(raw_process_detail, json.dumps(row))
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    expected_evidence_result = (
                        "provider_activation_failed"
                        if entrypoint == "activation" and case == "process"
                        else evidence_result
                    )
                    self.assertEqual(evidence["result"], expected_evidence_result)
                    self.assertEqual(evidence["effective_model"], "")
                    self.assertEqual(
                        evidence["canonical_execution_policy_digest"],
                        row["canonical_execution_policy_digest"],
                    )
                    if case == "process":
                        self.assertNotIn("provider_process_detail", evidence)
                        self.assertEqual(evidence["provider_returncode"], 1)
                    self.assertFalse(evidence["transcript_written"])
                    self.assertFalse(Path(evidence["transcript_path"]).exists())
                    self.assertNotIn(raw_process_detail, json.dumps(evidence))

    def test_claude_consumers_reject_untyped_or_conflicting_response_fields(self) -> None:
        """Reject ambiguous Claude JSON before transcript or readiness persistence."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-claude-typed-fields",
            "status": "active",
            "always_active": False,
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        raw_secret = "UNTRUSTED-CLAUDE-FIELD-SECRET"
        cases = (
            ("non_object", [raw_secret], (), {}, "provider_output_parse_failed", "claude provider output was not valid JSON"),
            ("result_object", None, (), {"result": {"secret": raw_secret}}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("result_list", None, (), {"result": [raw_secret]}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("message_object", None, ("result",), {"message": {"secret": raw_secret}}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("result_message_conflict", None, (), {"message": raw_secret}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("session_object", None, (), {"session_id": {"secret": raw_secret}}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("session_alias_conflict", None, (), {"sessionId": "other-session"}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("request_list", None, (), {"request_id": [raw_secret]}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("request_alias_conflict", None, (), {"requestId": "other-request"}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("usage_parent", None, (), {"usage": [raw_secret]}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("input_bool", None, (), {"usage": {"input_tokens": True, "output_tokens": 2}}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("input_alias_conflict", None, (), {"inputTokens": 99}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("duration_string", None, (), {"duration_api_ms": raw_secret}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("duration_alias_conflict", None, (), {"durationApiMs": 4}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("turns_bool", None, (), {"num_turns": True}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("turns_alias_conflict", None, (), {"numTurns": 2}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("cost_string", None, (), {"total_cost_usd": raw_secret}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
            ("cost_alias_conflict", None, (), {"totalCostUsd": 2.0}, "provider_output_metadata_invalid", "claude provider output metadata was invalid"),
        )
        for entrypoint in ("direct", "activation"):
            for case, exact_payload, removed, override, evidence_result, public_reason in cases:
                with self.subTest(entrypoint=entrypoint, case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    stale_row = dict(canonical)
                    stale_row.update(
                        {
                            "activation_status": "response_active",
                            "response_status": "invoked",
                            "provider_status": "provider_response_ready",
                            "effective_model": canonical["intended_model"],
                            "usage_source": "claude_print_json",
                        }
                    )
                    (session_dir / "roster.json").write_text(json.dumps([stale_row]), encoding="utf-8")
                    (session_dir / "bootstrap.json").write_text(
                        json.dumps(
                            {
                                "runtime": "codex",
                                "session_id": "session",
                                "organization_instance_id": canonical["organization_instance_id"],
                                "cwd": "/tmp/project",
                                "readiness_scope": "response_evidence",
                            }
                        ),
                        encoding="utf-8",
                    )
                    if exact_payload is None:
                        payload = {
                            "result": "review complete",
                            "model": canonical["intended_model"],
                            "usage": {"input_tokens": 1, "output_tokens": 2},
                            "duration_api_ms": 3,
                            "session_id": "provider-session",
                            "request_id": "provider-request",
                            "num_turns": 1,
                            "total_cost_usd": 1.0,
                            "untrusted": raw_secret,
                        }
                        for key in removed:
                            payload.pop(key, None)
                        payload.update(override)
                    else:
                        payload = exact_payload
                    completed = subprocess.CompletedProcess(
                        args=["claude"],
                        returncode=0,
                        stdout=json.dumps(payload),
                        stderr="",
                    )
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        return_value=canonical,
                    ), mock.patch.object(
                        builder.shutil,
                        "which",
                        return_value="/usr/bin/claude",
                    ), mock.patch.object(
                        builder,
                        "run_claude_command_with_bounded_output",
                        return_value=completed,
                    ) as run_mock:
                        hook_input = {
                            "session_id": "session",
                            "organization_instance_id": canonical["organization_instance_id"],
                            "agent_id": canonical["agent_id"],
                            "request_id": f"req-{entrypoint}-{case}",
                            "prompt": "Review only.",
                        }
                        if entrypoint == "direct":
                            output = builder.claude_cli_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        else:
                            output = builder.provider_activate(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )

                    run_mock.assert_called_once()
                    self.assertEqual(output, {"decision": "block", "reason": public_reason})
                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    row = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))[0]
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    self.assertEqual(row["response_status"], "not_invoked")
                    self.assertEqual(row["provider_status"], evidence_result)
                    self.assertEqual(row["effective_model"], "")
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], evidence_result)
                    self.assertFalse(evidence["transcript_written"])
                    self.assertFalse(Path(evidence["transcript_path"]).exists())
                    for persisted in (output, row, evidence):
                        self.assertNotIn(raw_secret, json.dumps(persisted))

    def test_claude_raised_failures_clear_stale_readiness_without_leaking_exception_data(self) -> None:
        """Raised launch, timeout, and parser errors use sanitized fail-closed paths."""
        builder = load_builder_module()
        canonical = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-claude-raised-failure",
            "status": "active",
            "always_active": False,
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        raw_secret = "UNTRUSTED-SECRET-PROMPT-DATA"
        parse_completed = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps({"result": "unused", "model": canonical["intended_model"]}),
            stderr="",
        )
        failure_cases = (
            (
                "decode",
                UnicodeDecodeError(
                    "utf-8",
                    raw_secret.encode("utf-8"),
                    0,
                    1,
                    raw_secret,
                ),
                None,
                "provider_output_decode_failed",
                "claude provider output was not valid UTF-8",
                "UnicodeDecodeError",
            ),
            (
                "timeout",
                subprocess.TimeoutExpired(
                    cmd=["claude", raw_secret],
                    timeout=120,
                    output=raw_secret,
                    stderr=raw_secret,
                ),
                None,
                "provider_response_timeout",
                "claude provider process exceeded its execution timeout",
                "TimeoutExpired",
            ),
            (
                "launch_oserror",
                OSError(13, raw_secret),
                None,
                "provider_process_failed",
                "claude provider process failed to start",
                "OSError",
            ),
            (
                "parse_recursion",
                None,
                RecursionError(raw_secret),
                "provider_output_parse_failed",
                "claude provider output was not valid JSON",
                "RecursionError",
            ),
            (
                "parse_type",
                None,
                TypeError(raw_secret),
                "provider_output_parse_failed",
                "claude provider output was not valid JSON",
                "TypeError",
            ),
        )
        for entrypoint in ("direct", "activation"):
            for case, run_exception, parse_exception, evidence_result, public_reason, exception_type in failure_cases:
                with self.subTest(entrypoint=entrypoint, case=case), tempfile.TemporaryDirectory() as tmp:
                    state_root = Path(tmp)
                    session_dir = state_root / "session"
                    session_dir.mkdir(parents=True)
                    stale_row = dict(canonical)
                    stale_row.update(
                        {
                            "activation_status": "response_active",
                            "response_status": "invoked",
                            "provider_status": "provider_response_ready",
                            "effective_model": canonical["intended_model"],
                            "usage_source": "claude_print_json",
                        }
                    )
                    (session_dir / "roster.json").write_text(json.dumps([stale_row]), encoding="utf-8")
                    (session_dir / "bootstrap.json").write_text(
                        json.dumps(
                            {
                                "runtime": "codex",
                                "session_id": "session",
                                "organization_instance_id": "org-claude-raised-failure",
                                "cwd": "/tmp/project",
                                "readiness_scope": "response_evidence",
                            }
                        ),
                        encoding="utf-8",
                    )
                    run_patch = (
                        mock.patch.object(builder, "run_claude_command_with_bounded_output", side_effect=run_exception)
                        if run_exception is not None
                        else mock.patch.object(builder, "run_claude_command_with_bounded_output", return_value=parse_completed)
                    )
                    parse_patch = (
                        mock.patch.object(builder, "parse_claude_json_output", side_effect=parse_exception)
                        if parse_exception is not None
                        else mock.patch.object(
                            builder,
                            "parse_claude_json_output",
                            wraps=builder.parse_claude_json_output,
                        )
                    )
                    with mock.patch.object(
                        builder,
                        "role_agent_row_for",
                        return_value=canonical,
                    ), mock.patch.object(
                        builder.shutil,
                        "which",
                        return_value="/usr/bin/claude",
                    ), run_patch, parse_patch:
                        hook_input = {
                            "session_id": "session",
                            "organization_instance_id": "org-claude-raised-failure",
                            "agent_id": "legacy-claude-role",
                            "request_id": f"req-{entrypoint}-{case}",
                            "prompt": "Review only.",
                        }
                        if entrypoint == "direct":
                            output = builder.claude_cli_agent_dispatch(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )
                        else:
                            output = builder.provider_activate(
                                runtime="codex",
                                state_root=state_root,
                                hook_input=hook_input,
                            )

                    self.assertEqual(output, {"decision": "block", "reason": public_reason})
                    state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                    row = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))[0]
                    evidence = json.loads(
                        (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                    )
                    self.assertNotIn(raw_secret, json.dumps(output))
                    self.assertNotIn(raw_secret, json.dumps(row))
                    self.assertNotIn(raw_secret, json.dumps(evidence))
                    self.assertEqual(row["response_status"], "not_invoked")
                    self.assertEqual(row["provider_status"], evidence_result)
                    self.assertEqual(row["effective_model"], "")
                    self.assertEqual(state["provider_response_ready_count"], 0)
                    self.assertEqual(state["readiness_scope"], "metadata_only")
                    self.assertEqual(evidence["result"], evidence_result)
                    self.assertEqual(evidence["effective_model"], "")
                    if run_exception is not None:
                        self.assertEqual(evidence["provider_exception_type"], exception_type)
                    else:
                        self.assertEqual(evidence["provider_parse_error_type"], exception_type)
                    self.assertEqual(
                        evidence["canonical_execution_policy_digest"],
                        row["canonical_execution_policy_digest"],
                    )

    def test_claude_exception_evidence_is_typed_and_bounded(self) -> None:
        """Reject non-finite, oversized, and subclass-controlled exception metadata."""
        builder = load_builder_module()

        for timeout in (float("nan"), float("inf"), -1, 86_401, True, "120"):
            with self.subTest(timeout=timeout):
                failure = subprocess.TimeoutExpired(cmd=["claude"], timeout=timeout)
                *_, evidence = builder.claude_process_exception_evidence(failure)
                self.assertEqual(evidence["provider_exception_type"], "TimeoutExpired")
                self.assertIsNone(evidence["timeout_seconds"])

        class UntrustedOSError(OSError):
            pass

        for errno_value in (-1, 4096, True, "13"):
            with self.subTest(errno=errno_value):
                failure = UntrustedOSError("untrusted")
                failure.errno = errno_value
                *_, evidence = builder.claude_process_exception_evidence(failure)
                self.assertEqual(evidence["provider_exception_type"], "OSError")
                self.assertIsNone(evidence["provider_exception_errno"])

        valid_timeout = subprocess.TimeoutExpired(cmd=["claude"], timeout=120.1259)
        *_, valid_timeout_evidence = builder.claude_process_exception_evidence(valid_timeout)
        self.assertEqual(valid_timeout_evidence["timeout_seconds"], 120.126)

        valid_oserror = OSError(13, "untrusted")
        *_, valid_oserror_evidence = builder.claude_process_exception_evidence(valid_oserror)
        self.assertEqual(valid_oserror_evidence["provider_exception_errno"], 13)

        decode_failure = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "untrusted")
        decode_result, decode_source, decode_note, decode_evidence = (
            builder.claude_process_exception_evidence(decode_failure)
        )
        self.assertEqual(decode_result, "provider_output_decode_failed")
        self.assertEqual(decode_source, "claude_print_json_decode_failed")
        self.assertEqual(decode_note, "claude provider output was not valid UTF-8")
        self.assertEqual(
            decode_evidence["provider_exception_type"],
            "UnicodeDecodeError",
        )

        class UntrustedValueError(ValueError):
            pass

        self.assertEqual(
            builder.claude_parse_exception_type(UntrustedValueError("untrusted")),
            "ValueError",
        )

    def test_direct_codex_consumer_preflights_canonical_model_policy(self) -> None:
        """Keep direct dispatch fail closed for missing or tampered routing."""
        builder = load_builder_module()
        variants = (
            (
                "missing_model",
                {"provider": "openai", "execution_mode": "codex", "intended_model": ""},
                "persisted intended model policy is unavailable",
            ),
            (
                "tampered_model",
                {"provider": "openai", "execution_mode": "codex", "intended_model": "gpt-5.6-sol"},
                "persisted intended model policy does not match canonical role",
            ),
            (
                "tampered_provider",
                {"provider": "anthropic", "execution_mode": "claude", "intended_model": "gpt-5.6-luna"},
                "persisted provider policy does not match canonical role",
            ),
            (
                "missing_provider",
                {"provider": "", "execution_mode": "codex", "intended_model": "gpt-5.6-luna"},
                "persisted provider policy does not match canonical role",
            ),
            (
                "unknown_provider",
                {"provider": "unknown", "execution_mode": "codex", "intended_model": "gpt-5.6-luna"},
                "persisted provider policy does not match canonical role",
            ),
            (
                "missing_execution",
                {"provider": "openai", "execution_mode": "", "intended_model": "gpt-5.6-luna"},
                "persisted execution policy does not match canonical role",
            ),
            (
                "unknown_execution",
                {"provider": "openai", "execution_mode": "unknown", "intended_model": "gpt-5.6-luna"},
                "persisted execution policy does not match canonical role",
            ),
            (
                "unknown_role",
                {
                    "agent_id": "unknown-role",
                    "provider": "anthropic",
                    "execution_mode": "claude",
                    "intended_model": "claude-opus-4-6",
                },
                "canonical codex role policy is unavailable",
            ),
        )

        for variant, routing, expected_reason in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                row_input = {
                    "agent_id": "tech-backend",
                    "allowed_tools": ["Read", "Grep", "Glob"],
                    "git_operations_allowed": False,
                }
                row_input.update(routing)
                (session_dir / "roster.json").write_text(
                    json.dumps([row_input]),
                    encoding="utf-8",
                )
                with mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value="/usr/bin/codex",
                ) as which_mock, mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                ) as run_mock:
                    output = builder.codex_exec_agent_dispatch(
                        runtime="codex",
                        state_root=state_root,
                        hook_input={
                            "session_id": "session",
                            "agent_id": row_input["agent_id"],
                            "request_id": f"req-{variant}",
                            "cwd": "/tmp/project",
                            "prompt": "Review only.",
                        },
                    )

                which_mock.assert_not_called()
                run_mock.assert_not_called()
                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                row = next(item for item in roster if item["agent_id"] == row_input["agent_id"])
                self.assertEqual(output["decision"], "block")
                self.assertEqual(output["reason"], expected_reason)
                self.assertNotIn("gpt-5.6-sol", json.dumps(output))
                self.assertNotIn("anthropic", json.dumps(output))
                self.assertEqual(row["provider_status"], "provider_model_policy_invalid")
                self.assertEqual(row["response_status"], "not_invoked")
                self.assertEqual(row["effective_model"], "")
                self.assertNotEqual(state["readiness_scope"], "response_evidence")
                self.assertEqual(evidence["result"], "provider_model_policy_invalid")
                self.assertEqual(evidence["effective_model"], "")
                self.assertFalse(evidence["provider_invoked"])
                self.assertEqual(output["agentDispatch"]["effective_model"], "")

        canonical_codex = builder.role_agent_row_for("tech-backend")
        raw_codex_model = " `gpt-5.6-luna` "
        codex_success = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(),
            stderr="",
        )
        for entrypoint in ("generic", "direct", "activation"):
            with self.subTest(entrypoint=entrypoint, case="codex_canonical_command_binding"), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                persisted = dict(canonical_codex)
                persisted["provider"] = " `openai` "
                persisted["execution_mode"] = " `codex` "
                persisted["intended_model"] = raw_codex_model
                persisted["fallback_models"] = "gpt-5.6-sol"
                persisted["allowed_tools"] = ["Bash", "Write"]
                (session_dir / "roster.json").write_text(json.dumps([persisted]), encoding="utf-8")
                with mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value="/usr/bin/codex",
                ), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    return_value=codex_success,
                ) as run_mock:
                    hook_input = {
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": f"req-codex-binding-{entrypoint}",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    }
                    if entrypoint == "generic":
                        output = builder.agent_dispatch(runtime="codex", state_root=state_root, hook_input=hook_input)
                    elif entrypoint == "direct":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    else:
                        output = builder.provider_activate(runtime="codex", state_root=state_root, hook_input=hook_input)

                command = run_mock.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                serialized_command = "\n".join(command)
                self.assertNotIn(raw_codex_model, serialized_command)
                self.assertNotIn(" `openai` ", serialized_command)
                self.assertNotIn(" `codex` ", serialized_command)
                self.assertNotIn("gpt-5.6-sol", serialized_command)
                if entrypoint == "activation":
                    self.assertEqual(output["activation"]["effective_model"], "gpt-5.6-luna")
                else:
                    self.assertEqual(output["agentDispatch"]["intended_model"], "gpt-5.6-luna")
                    self.assertEqual(output["agentDispatch"]["effective_model"], "gpt-5.6-luna")
                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                self.assertEqual(roster[0]["effective_model"], "gpt-5.6-luna")
                self.assertEqual(evidence["effective_model"], "gpt-5.6-luna")

    def test_provider_activate_reconciles_known_stale_routing_before_execution(self) -> None:
        builder = load_builder_module()
        canonical = builder.role_agent_row_for(
            "tech-backend",
            organization_instance_id="org-reconcile",
        )
        stale = dict(canonical)
        stale.update(
            {
                "provider": "anthropic",
                "execution_mode": "claude",
                "intended_model": "claude-opus-4-6",
                "fallback_models": "claude-sonnet-4-6",
                "activation_status": "response_active",
                "response_status": "invoked",
                "provider_status": "provider_response_ready",
                "usage_source": "claude_print_json",
                "effective_model": "claude-opus-4-6",
                "session_id": "stale-provider-session",
                "last_request_id": "stale-request",
                "canonical_execution_policy_digest": "stale-digest",
                "canonical_provider": "anthropic",
                "canonical_execution_mode": "claude",
                "canonical_intended_model": "claude-opus-4-6",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps(
                    {
                        "organization_instance_id": "org-reconcile",
                        "readiness_scope": "response_evidence",
                        "provider_response_scope": "response_evidence",
                        "provider_response_ready_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "roster.json").write_text(json.dumps([stale]), encoding="utf-8")

            def completed_after_reconciliation(*_args, **_kwargs):
                intermediate_roster = json.loads(
                    (session_dir / "roster.json").read_text(encoding="utf-8")
                )
                intermediate_state = json.loads(
                    (session_dir / "bootstrap.json").read_text(encoding="utf-8")
                )
                intermediate_row = intermediate_roster[0]
                self.assertEqual(intermediate_row["provider"], "openai")
                self.assertEqual(intermediate_row["execution_mode"], "codex")
                self.assertEqual(intermediate_row["intended_model"], "gpt-5.6-luna")
                self.assertEqual(intermediate_row["fallback_models"], "")
                self.assertEqual(intermediate_row["response_status"], "not_invoked")
                self.assertEqual(intermediate_row["effective_model"], "")
                self.assertEqual(intermediate_row["session_id"], "")
                self.assertEqual(intermediate_row["last_request_id"], "")
                self.assertEqual(intermediate_state["provider_response_ready_count"], 0)
                self.assertEqual(intermediate_state["provider_response_scope"], "not_invoked")
                self.assertEqual(intermediate_state["readiness_scope"], "metadata_only")
                reconciliation = json.loads(
                    (session_dir / "invocation-evidence.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                self.assertEqual(reconciliation["result"], "provider_routing_reconciled")
                self.assertFalse(reconciliation["provider_invoked"])
                self.assertEqual(
                    reconciliation["routing_fields_changed"],
                    ["provider", "execution_mode", "intended_model", "fallback_models"],
                )
                return subprocess.CompletedProcess(
                    args=["codex"],
                    returncode=0,
                    stdout=current_codex_jsonl(),
                    stderr="",
                )

            with mock.patch.object(
                builder.shutil,
                "which",
                return_value="/usr/bin/codex",
            ), mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=completed_after_reconciliation,
            ) as codex_mock, mock.patch.object(
                builder,
                "run_claude_command_with_bounded_output",
                side_effect=AssertionError("stale Claude route must not be selected"),
            ) as claude_mock:
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "organization_instance_id": "org-reconcile",
                        "agent_id": "tech-backend",
                        "request_id": "req-reconcile",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            self.assertEqual(output["activation"]["provider"], "openai")
            codex_mock.assert_called_once()
            claude_mock.assert_not_called()
            final_roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            final_evidence = [
                json.loads(line)
                for line in (session_dir / "invocation-evidence.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(final_roster[0]["provider"], "openai")
            self.assertEqual(final_roster[0]["response_status"], "invoked")
            self.assertEqual(final_evidence[0]["result"], "provider_routing_reconciled")
            self.assertEqual(final_evidence[-1]["result"], "provider_response_ready")

        with self.subTest(case="unknown_role"), tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(
                json.dumps(
                    [
                        {
                            "agent_id": "unknown-role",
                            "provider": "anthropic",
                            "execution_mode": "claude",
                            "intended_model": "claude-opus-4-6",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                builder,
                "run_command_with_bounded_output",
                side_effect=AssertionError("unknown role must not run Codex"),
            ) as codex_mock, mock.patch.object(
                builder,
                "run_claude_command_with_bounded_output",
                side_effect=AssertionError("unknown role must not run Claude"),
            ) as claude_mock:
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "unknown-role",
                        "request_id": "req-unknown-role",
                    },
                )

            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["reason"], "canonical provider role policy is unavailable")
            codex_mock.assert_not_called()
            claude_mock.assert_not_called()


    def test_codex_consumers_redact_nonzero_process_output(self) -> None:
        """Keep non-zero provider output out of public and mutable state."""
        builder = load_builder_module()
        raw_detail = "provider-secret gpt-5.6-sol"
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=1,
            stdout='{"model":"gpt-5.6-sol","secret":"stdout-secret"}',
            stderr=raw_detail,
        )

        for consumer in ("agent_dispatch", "provider_activate"):
            with self.subTest(consumer=consumer), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                (session_dir / "roster.json").write_text(
                    json.dumps([builder.role_agent_row_for("tech-backend")]),
                    encoding="utf-8",
                )
                with mock.patch.object(builder.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    return_value=completed,
                ):
                    if consumer == "agent_dispatch":
                        output = builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "agent_id": "tech-backend",
                                "request_id": "req-process-failure",
                                "cwd": "/tmp/project",
                                "prompt": "Review only.",
                            },
                        )
                    else:
                        output = builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input={
                                "session_id": "session",
                                "agent_id": "tech-backend",
                                "request_id": "req-process-failure",
                                "cwd": "/tmp/project",
                            },
                        )

                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
                )
                row = next(item for item in roster if item["agent_id"] == "tech-backend")
                self.assertEqual(output, {"decision": "block", "reason": "codex provider process failed"})
                self.assertNotIn(raw_detail, json.dumps(output))
                self.assertEqual(row["notes"], "codex provider process failed")
                self.assertNotIn(raw_detail, json.dumps(row))
                self.assertEqual(evidence["notes"], "codex provider process failed")
                self.assertNotIn("provider_process_detail", evidence)
                self.assertEqual(evidence["provider_returncode"], 1)
                self.assertNotIn("stdout-secret", json.dumps(evidence))
                self.assertEqual(evidence["effective_model"], "")

    def test_codex_consumers_prioritize_model_mismatch_without_response(self) -> None:
        """Persist a reported mismatch even when no authoritative response text exists."""
        builder = load_builder_module()
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(include_message=False, model="gpt-5.6-sol"),
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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
                return_value=completed,
            ):
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-no-response-model-mismatch",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(output["agentDispatch"]["result"], "provider_model_mismatch")
            self.assertEqual(output["agentDispatch"]["effective_model"], "")
            self.assertNotIn("gpt-5.6-sol", json.dumps(output))
            self.assertEqual(evidence["result"], "provider_model_mismatch")
            self.assertEqual(evidence["effective_model"], "gpt-5.6-sol")

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
                return_value=completed,
            ):
                output = builder.provider_activate(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={"session_id": "session", "agent_id": "tech-backend", "cwd": "/tmp/project"},
                )

            session_dir = state_root / "session"
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(output["decision"], "block")
            self.assertNotIn("gpt-5.6-sol", json.dumps(output))
            self.assertEqual(row["provider_status"], "provider_model_mismatch")
            self.assertEqual(evidence["result"], "provider_model_mismatch")
            self.assertEqual(evidence["effective_model"], "gpt-5.6-sol")

    def test_codex_consumers_do_not_synthesize_missing_effective_model(self) -> None:
        """Keep provider-omitted model evidence unknown instead of using intended_model."""
        builder = load_builder_module()
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=current_codex_jsonl(include_model=False),
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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
                return_value=completed,
            ):
                output = builder.codex_exec_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "agent_id": "tech-backend",
                        "request_id": "req-missing-model",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertEqual(output["agentDispatch"]["result"], "provider_response_ready")
            self.assertEqual(output["agentDispatch"]["intended_model"], "gpt-5.6-luna")
            self.assertEqual(output["agentDispatch"]["effective_model"], "")
            self.assertNotIn("effective_model", row)
            self.assertNotIn("effective_model", evidence)
            self.assertEqual(state["readiness_scope"], "response_evidence")

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

            session_dir = state_root / "session"
            state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
            roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            row = next(item for item in roster if item["agent_id"] == "tech-backend")
            self.assertNotIn("decision", output)
            self.assertEqual(output["activation"]["effective_model"], "")
            self.assertNotIn("effective_model", row)
            self.assertNotIn("effective_model", evidence)
            self.assertEqual(state["readiness_scope"], "response_evidence")

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
                                "execution_mode": "codex",
                                "intended_model": "gpt-5.6-luna",
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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

            self.assertEqual(ready_output["activation"]["effective_model"], "gpt-5.6-luna")
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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

            self.assertEqual(ready_output["activation"]["effective_model"], "gpt-5.6-luna")
            self.assertEqual(rejected_output["decision"], "block")
            self.assertIn("bounded byte limit", rejected_output["reason"])
            self.assertEqual(read_ready_output["activation"]["effective_model"], "gpt-5.6-luna")
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
                            "execution_mode": "codex",
                            "intended_model": "gpt-5.6-luna",
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
            real_open = builder.os.open

            def flaky_open(path, flags, *args, **kwargs):
                if path == "nested" and kwargs.get("dir_fd") is not None:
                    raise OSError("nested scan failed")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(builder.os, "open", side_effect=flaky_open):
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
            real_open = builder.os.open
            root_scans = 0

            def flaky_second_snapshot(path, flags, *args, **kwargs):
                nonlocal root_scans
                if path == queue_root:
                    root_scans += 1
                if path == "nested" and kwargs.get("dir_fd") is not None and root_scans >= 2:
                    raise OSError("nested scan failed")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                builder.os,
                "open",
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

        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            queue_root.mkdir()
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
                    queue_root=queue_root,
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

    def test_untrusted_ids_and_queue_paths_are_collision_safe_and_symlink_closed(self) -> None:
        builder = load_builder_module()
        encoded = builder.safe_id("a/b")
        self.assertNotEqual(encoded, builder.safe_id("a_b"))
        self.assertNotIn("/", encoded)
        self.assertLessEqual(len(builder.safe_id("a" * 10_000)), 81)
        for marker in (".", ".."):
            with self.subTest(marker=marker):
                self.assertNotEqual(builder.safe_id(marker), marker)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "session"
            expected_queue = (session_dir / "queue").resolve()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ITB_QUEUE_ROOT", None)
                self.assertEqual(
                    builder.queue_root_for(
                        session_dir,
                        {"queue_root": str(expected_queue)},
                    ),
                    expected_queue,
                )
                with self.assertRaisesRegex(ValueError, "trusted queue root"):
                    builder.queue_root_for(
                        session_dir,
                        {"queue_root": str(root / "attacker-queue")},
                    )

            trusted_queue = root / "trusted-queue"
            trusted_queue.mkdir()
            external = root / "external"
            external.mkdir()
            link = trusted_queue / "linked"
            link.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "traverses a symlink"):
                builder.safe_queue_relative_path(
                    trusted_queue,
                    "linked/report.yaml",
                    "report_path",
                )

            root_link = root / "queue-link"
            root_link.symlink_to(trusted_queue, target_is_directory=True)
            with mock.patch.dict(
                os.environ,
                {"ITB_QUEUE_ROOT": str(root_link)},
                clear=False,
            ), self.assertRaisesRegex(ValueError, "must not be a symlink"):
                builder.queue_root_for(session_dir)

            transcript = root / "transcript.jsonl"
            transcript.write_text('{"input_tokens":99}\n', encoding="utf-8")
            transcript_link = root / "transcript-link.jsonl"
            transcript_link.symlink_to(transcript)
            self.assertEqual(builder.provider_transcript_records(transcript_link), [])
            self.assertEqual(
                builder.provider_usage_metric_fields(
                    {"transcript_path": str(transcript)}
                ),
                {},
            )

            session_dir.mkdir()
            transcript_root = root / "external-transcripts"
            transcript_root.mkdir()
            (session_dir / "provider-exec").symlink_to(
                transcript_root,
                target_is_directory=True,
            )
            planned_path, transcript_error = builder.persist_validated_provider_transcript(
                session_dir,
                agent_id="tech-qa",
                request_id="request-id",
                suffix=".jsonl",
                content='{"result":"must-not-escape"}\n',
            )
            self.assertEqual(
                transcript_error["provider_transcript_error_type"],
                "filesystem_error",
            )
            self.assertFalse(planned_path.exists())
            self.assertFalse((transcript_root / "tech-qa" / "request-id.jsonl").exists())

    def test_generic_provider_evidence_rejects_coercive_metrics_and_invalid_ids(self) -> None:
        builder = load_builder_module()
        valid = {
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "usage_source": "provider_json",
            "transcript_path": "/tmp/provider.jsonl",
            "input_tokens": 1,
            "output_tokens": 2,
            "duration_api_ms": 3,
            "num_turns": 1,
            "duration_sec": 1.5,
            "retry_count": 0,
        }
        self.assertEqual(
            builder.provider_evidence_runtime_errors(
                valid,
                require_completion_ids=True,
            ),
            [],
        )
        for value in (None, "", "1", 1.5, -1, True, builder.CODEX_EVIDENCE_METRIC_MAX_VALUE + 1):
            with self.subTest(metric=value):
                evidence = valid | {"input_tokens": value}
                self.assertIn(
                    "provider evidence input_tokens is invalid",
                    builder.provider_evidence_runtime_errors(
                        evidence,
                        require_completion_ids=True,
                    ),
                )
                self.assertIsNone(builder.optional_metric_int(value))
        for field, value in (
            ("provider_session_id", ["provider-session"]),
            ("request_id", "request\x00secret"),
            ("usage_source", "x" * 129),
            ("transcript_path", "x" * 4097),
        ):
            with self.subTest(field=field):
                errors = builder.provider_evidence_runtime_errors(
                    valid | {field: value},
                    require_completion_ids=True,
                )
                self.assertTrue(any(field in error for error in errors))
        for conflicting in (
            valid | {"requestId": "different-request"},
            valid | {"durationSec": 2.0},
            valid | {"retryCount": 1},
        ):
            with self.subTest(conflicting_aliases=conflicting):
                self.assertTrue(
                    any(
                        "aliases conflict" in error
                        for error in builder.provider_evidence_runtime_errors(
                            conflicting,
                            require_completion_ids=True,
                        )
                    )
                )
        for duration in (-1, float("nan"), float("inf"), 10**1000, "1.0"):
            with self.subTest(duration=duration):
                self.assertIn(
                    "provider evidence duration_sec is invalid",
                    builder.provider_evidence_runtime_errors(
                        valid | {"duration_sec": duration},
                        require_completion_ids=True,
                    ),
                )

    def test_hook_ingress_and_error_evidence_never_reflect_untrusted_parser_text(self) -> None:
        builder = load_builder_module()

        class FakeStdin:
            def isatty(self):
                return False

            def fileno(self):
                return 99

        with mock.patch.object(builder.sys, "stdin", FakeStdin()), mock.patch.object(
            builder._select,
            "select",
            return_value=([99], [], []),
        ), mock.patch.object(
            builder.fcntl,
            "fcntl",
            side_effect=[0, 0, 0],
        ), mock.patch.object(
            builder.os,
            "read",
            side_effect=[b"\xff", b""],
        ), self.assertRaises(UnicodeDecodeError):
            builder.load_hook_input()

        with mock.patch.object(builder.sys, "stdin", FakeStdin()), mock.patch.object(
            builder._select,
            "select",
            return_value=([99], [], []),
        ), mock.patch.object(
            builder.fcntl,
            "fcntl",
            side_effect=[0, 0, 0],
        ), mock.patch.object(
            builder.os,
            "read",
            return_value=b"x" * (builder.CODEX_JSONL_MAX_CHARS + 1),
        ), self.assertRaisesRegex(ValueError, "bounded input limit"):
            builder.load_hook_input()

        secret = "UNTRUSTED-PARSER-SECRET"
        with self.assertRaises(ValueError) as duplicate_error:
            builder.parse_provider_json_object(
                f'{{"{secret}":1,"{secret}":2}}',
                context="hook stdin",
            )
        self.assertNotIn(secret, str(duplicate_error.exception))

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            event = builder.record_hook_error(
                runtime="codex",
                state_root=state_root,
                command="session-start",
                hook_input={"session_id": "session"},
                exc=ValueError(secret),
            )
            persisted = (state_root / "session" / "hook-errors.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertEqual(event["error_code"], "itb_hook_command_failed")
            self.assertNotIn("error", event)
            self.assertNotIn("traceback", event)
            self.assertNotIn(secret, persisted)

            stdout = io.StringIO()
            argv = [
                "itb_bootstrap_builder.py",
                "session-start",
                "--runtime",
                "codex",
                "--state-root",
                str(state_root),
                "--session-id",
                "main-session",
            ]
            with mock.patch.object(
                builder,
                "validate_vault",
                side_effect=ValueError(secret),
            ), mock.patch.object(builder.sys, "argv", argv), mock.patch(
                "sys.stdout",
                stdout,
            ):
                self.assertEqual(builder.main(), 1)
            public_output = stdout.getvalue()
            self.assertNotIn(secret, public_output)
            self.assertNotIn("traceback", public_output)
            self.assertEqual(json.loads(public_output)["reason"], "ITB hook command failed")

            fallback_stdout = io.StringIO()
            with mock.patch.object(
                builder,
                "validate_vault",
                side_effect=ValueError(secret),
            ), mock.patch.object(
                builder,
                "record_hook_error",
                side_effect=OSError("SECONDARY-ERROR-SECRET"),
            ), mock.patch.object(builder.sys, "argv", argv), mock.patch(
                "sys.stdout",
                fallback_stdout,
            ):
                self.assertEqual(builder.main(), 1)
            fallback_output = fallback_stdout.getvalue()
            self.assertNotIn(secret, fallback_output)
            self.assertNotIn("SECONDARY-ERROR-SECRET", fallback_output)
            self.assertEqual(
                json.loads(fallback_output)["hookError"]["error_code"],
                "itb_hook_error_evidence_unavailable",
            )

    def test_codex_invalid_output_is_not_persisted_as_a_transcript(self) -> None:
        builder = load_builder_module()
        raw_secret = "UNTRUSTED-CODEX-STDOUT-SECRET"
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=f'{{"type":"thread.started","thread_id":"provider-thread","secret":"{raw_secret}"}}\n{{',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(
                json.dumps([builder.role_agent_row_for("tech-backend")]),
                encoding="utf-8",
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
                        "request_id": "req-invalid-output",
                        "cwd": "/tmp/project",
                        "prompt": "Review only.",
                    },
                )

            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            transcript_path = session_dir / "provider-exec" / "tech-backend" / "req-invalid-output.jsonl"
            self.assertEqual(output["decision"], "block")
            self.assertEqual(output["reason"], "codex provider output was not valid JSON")
            self.assertFalse(evidence["transcript_written"])
            self.assertEqual(evidence["provider_parse_error_type"], "JSONDecodeError")
            self.assertFalse(transcript_path.exists())
            self.assertNotIn(raw_secret, json.dumps(evidence))

    def test_claude_explicit_empty_model_is_invalid_and_never_persisted(self) -> None:
        builder = load_builder_module()
        self.assertEqual(builder.claude_reported_model({"model": ""}), ("", False))
        canonical = {
            "agent_id": "legacy-claude-role",
            "role_id": "legacy-claude-role",
            "organization_instance_id": "org-empty-claude-model",
            "status": "active",
            "always_active": False,
            "provider": "anthropic",
            "execution_mode": "claude",
            "intended_model": "claude-opus-4-6",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        completed = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(
                {
                    "result": "review complete",
                    "model": "",
                    "session_id": "provider-session",
                    "request_id": "provider-request",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "duration_api_ms": 1,
                    "num_turns": 1,
                    "total_cost_usd": 0,
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "roster.json").write_text(
                json.dumps([canonical]),
                encoding="utf-8",
            )
            with mock.patch.object(
                builder,
                "role_agent_row_for",
                return_value=canonical,
            ), mock.patch.object(
                builder.shutil,
                "which",
                return_value="/usr/bin/claude",
            ), mock.patch.object(
                builder,
                "run_claude_command_with_bounded_output",
                return_value=completed,
            ):
                output = builder.claude_cli_agent_dispatch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": "session",
                        "organization_instance_id": "org-empty-claude-model",
                        "agent_id": "legacy-claude-role",
                        "request_id": "req-empty-model",
                        "prompt": "Review only.",
                    },
                )

            evidence = json.loads(
                (session_dir / "invocation-evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            transcript_path = session_dir / "provider-exec" / "legacy-claude-role" / "req-empty-model.json"
            self.assertEqual(output["decision"], "block")
            self.assertEqual(evidence["result"], "provider_model_mismatch")
            self.assertFalse(evidence["reported_model_metadata_valid"])
            self.assertFalse(evidence["transcript_written"])
            self.assertFalse(transcript_path.exists())

    def test_generic_usage_totals_fail_closed_before_persistence(self) -> None:
        builder = load_builder_module()
        maximum = builder.CODEX_EVIDENCE_METRIC_MAX_VALUE
        overflowing = {"input_tokens": maximum, "output_tokens": maximum}

        self.assertEqual(builder.collect_provider_usage_metrics(overflowing), ({}, False))
        self.assertEqual(
            builder.provider_transcript_usage_metric_fields_from_records([overflowing]),
            {"reported_usage_metadata_valid": False},
        )
        evidence = {
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "usage_source": "codex_exec_json",
            "transcript_path": "/tmp/provider.jsonl",
            **overflowing,
        }
        self.assertIn(
            "provider evidence total_tokens is invalid",
            builder.provider_evidence_runtime_errors(
                evidence,
                require_completion_ids=True,
            ),
        )
        self.assertEqual(
            builder.provider_usage_metric_fields(evidence),
            {"provider_usage_metadata_valid": False},
        )
        conflicting_identity = {
            "input_tokens": 7,
            "session_id": "session-a",
            "sessionId": "session-b",
        }
        self.assertIn(
            "provider evidence session_id aliases conflict",
            builder.provider_evidence_runtime_errors(conflicting_identity),
        )
        self.assertEqual(
            builder.provider_usage_metric_fields(conflicting_identity),
            {"provider_usage_metadata_valid": False},
        )
        self.assertEqual(
            builder.provider_transcript_usage_metric_fields_from_records(
                [
                    {"usage": {"input_tokens": 1}},
                    {"message": {"usage": {"inputTokens": 2}}},
                ]
            ),
            {"reported_usage_metadata_valid": False},
        )
        self.assertEqual(
            builder.provider_transcript_usage_metric_fields_from_records(
                [
                    {"usage": {"input_tokens": 1}},
                    {"usage": {"output_tokens": 2}},
                    {"usage": {"total_tokens": 3}},
                ]
            ),
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
        model_conflict = {
            "model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-sol",
            "input_tokens": 7,
        }
        self.assertIn(
            "provider evidence effective_model aliases conflict",
            builder.provider_evidence_runtime_errors(model_conflict),
        )
        self.assertEqual(
            builder.provider_usage_metric_fields(model_conflict),
            {"provider_usage_metadata_valid": False},
        )
        self.assertEqual(
            builder.provider_transcript_usage_metric_fields_from_records(
                [
                    {"usage": {"input_tokens": 1}},
                    {"usage": {"output_tokens": 2}},
                    {"usage": {"total_tokens": 999}},
                ]
            ),
            {"reported_usage_metadata_valid": False},
        )
        self.assertFalse(
            builder.claude_response_fields(
                {
                    "result": "review complete",
                    "usage": overflowing,
                    "session_id": "provider-session",
                    "request_id": "request-id",
                }
            )[1]
        )
        long_result, long_result_valid = builder.claude_response_fields(
            {"result": "x" * 1024}
        )
        self.assertTrue(long_result_valid)
        self.assertEqual(len(long_result["result_text"]), 1024)
        self.assertFalse(builder.codex_event_usage({"usage": overflowing})[1])

        persisted: list[dict[str, object]] = []
        with mock.patch.object(
            builder,
            "append_jsonl_atomic",
            side_effect=lambda _path, metric: persisted.append(metric),
        ), mock.patch.object(
            builder,
            "append_queue_jsonl",
            side_effect=lambda _root, _value, _field, metric: persisted.append(metric),
        ):
            builder.append_agent_dispatch_metric(
                session_dir=Path("/tmp/session"),
                queue_root=Path("/tmp/queue"),
                runtime="codex",
                session_id="session",
                organization_instance_id="org",
                agent_id="tech-qa",
                request_id="request-id",
                source_agent="manager",
                result="provider_response_ready",
                usage_source="codex_exec_json",
                effective_model="",
                started_at="2026-09-01T00:00:00+09:00",
                completed_at="2026-09-01T00:00:01+09:00",
                duration_seconds=1.0,
                input_tokens=maximum,
                output_tokens=maximum,
            )
        self.assertEqual(len(persisted), 2)
        self.assertTrue(all("total_tokens" not in metric for metric in persisted))
        self.assertTrue(all("effective_model" not in metric for metric in persisted))

    def test_role_report_and_worker_alias_conflicts_are_redacted(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        valid_evidence = {
            "usage_source": "codex_exec_json",
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "transcript_path": "/tmp/provider.jsonl",
            "effective_model": "gpt-5.6-luna",
            "input_tokens": 1,
        }
        worker_evidence = builder.role_agent_provider_evidence(
            canonical,
            {
                "provider_evidence": valid_evidence,
                "inputTokens": 2,
            },
        )
        self.assertTrue(builder.validate_role_agent_provider_evidence(worker_evidence))
        sanitized = builder.sanitize_invalid_provider_evidence(worker_evidence)
        self.assertEqual(
            set(sanitized),
            {
                "provider",
                "intended_model",
                "effective_model",
                "reported_model_metadata_valid",
                "provider_identity_status",
                "usage_source",
            },
        )
        self.assertNotIn("input_tokens", sanitized)
        self.assertNotIn("transcript_path", sanitized)

        raw_secret = "UNTRUSTED-ROLE-REPORT-ERROR"
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            session_dir = state_root / "session"
            queue_root = state_root / "queue"
            session_dir.mkdir(parents=True)
            queue_root.mkdir(parents=True)
            (session_dir / "bootstrap.json").write_text(
                json.dumps({"organization_instance_id": "org-role-report"}),
                encoding="utf-8",
            )
            message = {
                "message_id": "message-id",
                "task_id": "task-id",
                "retry_count": 0,
                "payload": {"report_path": "reports/task-id/report.yaml"},
            }
            base_hook = {
                "session_id": "session",
                "organization_instance_id": "org-role-report",
                "role_id": "tech-qa",
                "message_id": "message-id",
                "status": "done",
                "summary": "review complete",
                "provider_evidence": valid_evidence,
            }
            finalize_mock = mock.Mock()
            common_patches = (
                mock.patch.object(builder, "role_agent_row_for", return_value=canonical),
                mock.patch.object(builder, "queue_root_for", return_value=queue_root),
                mock.patch.object(builder, "queue_message_by_id", return_value=message),
                mock.patch.object(
                    builder,
                    "enrich_role_report_provider_evidence_from_claude_transcript",
                ),
                mock.patch.object(builder, "finalize_role_queue_report", finalize_mock),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4]:
                conflict_output = builder.role_report(
                    runtime="codex",
                    state_root=state_root,
                    hook_input=base_hook | {"inputTokens": 2},
                )
            self.assertEqual(conflict_output["reason"], "provider evidence aliases conflict")
            finalize_mock.assert_not_called()

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder,
                "queue_root_for",
                return_value=queue_root,
            ), mock.patch.object(
                builder,
                "queue_message_by_id",
                side_effect=ValueError(raw_secret),
            ):
                lookup_output = builder.role_report(
                    runtime="codex",
                    state_root=state_root,
                    hook_input=base_hook,
                )
            self.assertEqual(lookup_output["error_code"], "role_report_message_lookup_failed")
            self.assertNotIn(raw_secret, json.dumps(lookup_output))

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder,
                "queue_root_for",
                return_value=queue_root,
            ), mock.patch.object(
                builder,
                "queue_message_by_id",
                return_value=message,
            ), mock.patch.object(
                builder,
                "enrich_role_report_provider_evidence_from_claude_transcript",
            ), mock.patch.object(
                builder,
                "finalize_role_queue_report",
                side_effect=ValueError(raw_secret),
            ):
                finalization_output = builder.role_report(
                    runtime="codex",
                    state_root=state_root,
                    hook_input=base_hook,
                )
            self.assertEqual(finalization_output["error_code"], "role_report_finalization_failed")
            self.assertNotIn(raw_secret, json.dumps(finalization_output))

            event = builder.record_hook_error(
                runtime="codex",
                state_root=state_root,
                command="role-report",
                hook_input={"session_id": "one", "sessionId": "two"},
                exc=ValueError(raw_secret),
            )
            self.assertEqual(event["session_id"], "invalid-session")
            self.assertNotIn(raw_secret, json.dumps(event))

    def test_claude_transcript_identity_binding_is_exact(self) -> None:
        builder = load_builder_module()
        empty_model = builder.provider_transcript_metadata_fields_from_records(
            [{"model": ""}]
        )
        self.assertFalse(empty_model["reported_model_metadata_valid"])
        self.assertNotIn("effective_model", empty_model)

        conflicting_request = builder.provider_transcript_metadata_fields_from_records(
            [
                {
                    "request_id": "request-one",
                    "message": {"requestId": "request-two"},
                }
            ]
        )
        self.assertFalse(conflicting_request["request_id_metadata_valid"])
        self.assertNotIn("request_id", conflicting_request)

        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "transcript.jsonl"
            transcript_path.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "cwd": "/tmp/project",
                        "request_id": "request-one",
                        "message": {
                            "content": "review message-id",
                            "requestId": "request-two",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            status, *_rest = builder.role_report_transcript_match_for_path(
                transcript_path,
                provider_cwd=Path("/tmp/project"),
                message_id="message-id",
                task_id="task-id",
                message_created_at="",
                supplied_request_id="request-one",
            )
            self.assertEqual(status, "request_id_metadata_invalid")

            transcript_path.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "cwd": "`/tmp/project`",
                        "request_id": "request-one",
                        "message": {"content": "review message-id"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            status, *_rest = builder.role_report_transcript_match_for_path(
                transcript_path,
                provider_cwd=Path("/tmp/project"),
                message_id="message-id",
                task_id="task-id",
                message_created_at="",
                supplied_request_id="request-one",
            )
            self.assertEqual(status, "not_found")

    def test_terminal_recovery_rejects_unvalidated_provider_evidence(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        report = {
            "report_version": "1",
            "report_type": "role_agent_worker_report",
            "from_role": "tech-qa",
            "task_id": "task-id",
            "message_id": "message-id",
            "created_at": "2026-09-01T00:00:00+09:00",
            "result": "completed",
            "status": "done",
            "summary": "provider complete",
            "evidence": {
                "provider": "openai",
                "intended_model": "gpt-5.6-luna",
                "effective_model": "gpt-5.6-luna",
                "usage_source": "codex_exec_json",
                "provider_session_id": "provider-session",
                "request_id": "request-id",
                "transcript_path": "/tmp/provider.jsonl",
                "input_tokens": builder.CODEX_EVIDENCE_METRIC_MAX_VALUE + 1,
            },
        }
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            self.assertTrue(
                builder.validate_terminal_queue_report(
                    report,
                    role_id="tech-qa",
                    message_id="message-id",
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "session"
            queue_root = root / "queue"
            report_ref = "reports/task-id/report.yaml"
            report_path = queue_root / report_ref
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")
            session_dir.mkdir()
            message = {
                "message_id": "message-id",
                "task_id": "task-id",
                "payload": {"report_path": report_ref},
            }
            invalid_mock = mock.Mock()
            update_mock = mock.Mock()
            metric_mock = mock.Mock()
            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder,
                "record_invalid_queue_report",
                invalid_mock,
            ), mock.patch.object(
                builder,
                "update_inbox_message",
                update_mock,
            ), mock.patch.object(
                builder,
                "append_queue_metric",
                metric_mock,
            ):
                recovered = builder.recover_pending_message_from_existing_report(
                    runtime="codex",
                    session_dir=session_dir,
                    session_id="session",
                    organization_instance_id="org",
                    queue_root=queue_root,
                    inbox_path=queue_root / "inbox/tech-qa.yaml",
                    role_id="tech-qa",
                    role_row=canonical,
                    message=message,
                    now="2026-09-01T00:01:00+09:00",
                )
        self.assertIsNone(recovered)
        invalid_mock.assert_called_once()
        update_mock.assert_not_called()
        metric_mock.assert_not_called()

    def test_terminal_usage_evidence_and_parent_identity_aliases_are_exact(self) -> None:
        builder = load_builder_module()
        maximum = builder.CODEX_EVIDENCE_METRIC_MAX_VALUE
        completion = {
            "provider": "openai",
            "intended_model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-luna",
            "usage_source": "codex_exec_json",
            "provider_session_id": "provider-session",
            "request_id": "request-id",
            "transcript_path": "/tmp/provider.jsonl",
        }
        oversized_nested = completion | {
            "message": {"usage": {"input_tokens": maximum + 1}}
        }
        self.assertIn(
            "provider evidence input_tokens is invalid",
            builder.provider_evidence_runtime_errors(
                oversized_nested,
                require_completion_ids=True,
            ),
        )
        conflicting_nested = completion | {
            "input_tokens": 1,
            "response": {"usage": {"inputTokens": 2}},
        }
        self.assertIn(
            "provider evidence input_tokens is invalid",
            builder.provider_evidence_runtime_errors(
                conflicting_nested,
                require_completion_ids=True,
            ),
        )

        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
        }
        report = {
            "report_version": "1",
            "report_type": "role_agent_worker_report",
            "from_role": "tech-qa",
            "message_id": "message-id",
            "created_at": "2026-09-01T00:00:00+09:00",
            "result": "completed",
            "status": "done",
            "summary": "complete",
            "provider_evidence": completion,
            "evidence": completion | {"request_id": "other-request"},
        }
        self.assertIn(
            "terminal report provider evidence aliases conflict",
            builder.validate_terminal_queue_report(
                report,
                role_id="tech-qa",
                message_id="message-id",
            ),
        )
        conflicting_worker = builder.role_agent_provider_evidence(
            canonical,
            {
                "provider_evidence": completion,
                "evidence": completion | {"request_id": "other-request"},
            },
        )
        self.assertEqual(conflicting_worker["provider_identity_status"], "invalid")
        self.assertTrue(builder.validate_role_agent_provider_evidence(conflicting_worker))
        enriched_conflict = builder.gate_latency_enrich_provider_evidence_from_report(
            state_root=Path("/tmp/state"),
            metric={
                "role_id": "tech-qa",
                "task_id": "task-id",
                "message_id": "message-id",
                "provider": "openai",
                "intended_model": "gpt-5.6-luna",
            },
            report=report,
        )
        self.assertEqual(enriched_conflict["provider_identity_status"], "invalid")
        with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
            self.assertEqual(
                builder.validate_terminal_queue_report(
                    report | {"evidence": dict(completion)},
                    role_id="tech-qa",
                    message_id="message-id",
                ),
                [],
            )

        with self.assertRaisesRegex(ValueError, "session identity aliases"):
            builder.resolve_session_id(
                Path("/tmp/state"),
                {"session_id": "one", "sessionId": "two"},
            )
        self.assertEqual(
            builder.resolve_session_id(
                Path("/tmp/state"),
                {"session_id": "one", "sessionId": "one"},
            ),
            ("one", "hook_input"),
        )
        with self.assertRaisesRegex(ValueError, "organization identity"):
            builder.resolve_organization_instance_id(
                {"organization_instance_id": "org-one"},
                {"organizationInstanceId": "org-two"},
                "session",
            )
        with self.assertRaisesRegex(ValueError, "request identity aliases"):
            builder.exact_hook_request_id(
                {"request_id": "one", "requestId": "two"}
            )

    def test_git_prompt_policy_rejection_clears_stale_readiness_for_both_facades(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "organization_instance_id": "org-policy",
            "status": "active",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "canonical_execution_policy_digest": "policy-digest",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        stale = canonical | {
            "activation_status": "response_active",
            "response_status": "invoked",
            "provider_status": "provider_response_ready",
            "usage_source": "codex_exec_json",
            "effective_model": "gpt-5.6-luna",
            "session_id": "old-provider-session",
            "last_request_id": "old-request",
        }
        for facade in ("agent_dispatch", "provider_activation"):
            with self.subTest(facade=facade), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                (session_dir / "bootstrap.json").write_text(
                    json.dumps(
                        {
                            "organization_instance_id": "org-policy",
                            "provider_response_ready_count": 1,
                            "provider_response_scope": "response_evidence",
                            "readiness_scope": "response_evidence",
                        }
                    ),
                    encoding="utf-8",
                )
                (session_dir / "roster.json").write_text(
                    json.dumps([stale]),
                    encoding="utf-8",
                )
                with mock.patch.object(
                    builder,
                    "canonical_codex_execution_policy",
                    return_value=(dict(canonical), ""),
                ), mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=dict(canonical),
                ), mock.patch.object(
                    builder,
                    "registry_row_for",
                    return_value={},
                ), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    side_effect=AssertionError("provider must not run"),
                ):
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-policy",
                        "agent_id": "tech-qa",
                        "request_id": f"request-{facade}",
                        "prompt": "Please run git push origin main.",
                    }
                    output = (
                        builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                        if facade == "agent_dispatch"
                        else builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    )

                roster = json.loads((session_dir / "roster.json").read_text(encoding="utf-8"))
                state = json.loads((session_dir / "bootstrap.json").read_text(encoding="utf-8"))
                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                self.assertEqual(output["decision"], "block")
                self.assertEqual(roster[0]["provider_status"], "provider_prompt_policy_rejected")
                self.assertEqual(roster[0]["response_status"], "not_invoked")
                self.assertEqual(roster[0]["effective_model"], "")
                self.assertEqual(state["provider_response_ready_count"], 0)
                self.assertEqual(state["provider_response_scope"], "not_invoked")
                self.assertEqual(state["readiness_scope"], "metadata_only")
                self.assertEqual(evidence["result"], "provider_prompt_policy_rejected")
                self.assertFalse(evidence["provider_invoked"])

    def test_live_queue_consumers_survive_parent_replacement(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }

        def write_inbox(path: Path, messages: list[dict[str, object]]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "envelope_version": "1",
                        "role_id": "tech-qa",
                        "messages": messages,
                    }
                ),
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"

            lock_queue = root / "lock-queue"
            write_inbox(lock_queue / "inbox/tech-qa.yaml", [])
            lock_external = root / "lock-external"
            lock_external.mkdir()
            original_open = os.open
            lock_swapped = False

            def racing_lock_open(path, flags, *args, **kwargs):
                nonlocal lock_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "locks" and kwargs.get("dir_fd") is not None and not lock_swapped:
                    lock_swapped = True
                    (lock_queue / "locks").rename(lock_queue / "locks-original")
                    (lock_queue / "locks").symlink_to(lock_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder.os, "open", side_effect=racing_lock_open):
                locked_inbox = builder.append_inbox_message(
                    lock_queue / "inbox/tech-qa.yaml",
                    "tech-qa",
                    {
                        "message_id": "descriptor-lock-message",
                        "status": "pending",
                    },
                    lock_queue,
                )
            self.assertTrue(lock_swapped)
            self.assertEqual(
                locked_inbox["messages"][0]["message_id"],
                "descriptor-lock-message",
            )
            self.assertTrue((lock_queue / "locks-original/enqueue.flock").is_file())
            self.assertFalse((lock_external / "enqueue.flock").exists())

            contention_queue = root / "contention-queue"
            first_lock = builder.acquire_descriptor_queue_lock(
                contention_queue,
                "locks/enqueue.flock",
                "enqueue_lock_path",
                timeout_seconds=0,
            )
            try:
                with self.assertRaisesRegex(TimeoutError, "queue descriptor lock timeout"):
                    builder.acquire_descriptor_queue_lock(
                        contention_queue,
                        "locks/enqueue.flock",
                        "enqueue_lock_path",
                        timeout_seconds=0,
                    )
            finally:
                builder.release_descriptor_queue_lock(first_lock)
            reacquired_lock = builder.acquire_descriptor_queue_lock(
                contention_queue,
                "locks/enqueue.flock",
                "enqueue_lock_path",
                timeout_seconds=0,
            )
            builder.release_descriptor_queue_lock(reacquired_lock)

            inspect_session = "inspect-session"
            inspect_dir = state_root / inspect_session
            inspect_queue = inspect_dir / "queue"
            write_inbox(
                inspect_queue / "inbox/tech-qa.yaml",
                [{"message_id": "trusted", "status": "pending"}],
            )
            inspect_external = root / "inspect-external"
            write_inbox(
                inspect_external / "tech-qa.yaml",
                [{"message_id": "redirected", "status": "pending"}],
            )
            inspect_swapped = False

            def racing_inspect_open(path, flags, *args, **kwargs):
                nonlocal inspect_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "inbox" and kwargs.get("dir_fd") is not None and not inspect_swapped:
                    inspect_swapped = True
                    (inspect_queue / "inbox").rename(inspect_queue / "inbox-original")
                    (inspect_queue / "inbox").symlink_to(inspect_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder.os,
                "open",
                side_effect=racing_inspect_open,
            ):
                inspected = builder.role_queue(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": inspect_session,
                        "organization_instance_id": "org-inspect",
                        "role_id": "tech-qa",
                        "action": "inspect",
                    },
                )
            self.assertTrue(inspect_swapped)
            self.assertEqual(
                inspected["roleQueue"]["inbox"]["messages"][0]["message_id"],
                "trusted",
            )

            switch_session = "switch-session"
            switch_dir = state_root / switch_session
            switch_dir.mkdir(parents=True)
            (switch_dir / "bootstrap.json").write_text(
                json.dumps({"organization_instance_id": "org-switch"}),
                encoding="utf-8",
            )
            (switch_dir / "roster.json").write_text(
                json.dumps(
                    [
                        {
                            "agent_id": "tech-qa",
                            "provider": "openai",
                            "intended_model": "gpt-5.6-luna",
                            "execution_mode": "codex",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            switch_queue = switch_dir / "queue"
            write_inbox(switch_queue / "inbox/tech-qa.yaml", [])
            switch_external = root / "switch-external"
            write_inbox(
                switch_external / "tech-qa.yaml",
                [{"message_id": "redirected", "status": "processing"}],
            )
            switch_swapped = False

            def racing_switch_open(path, flags, *args, **kwargs):
                nonlocal switch_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "inbox" and kwargs.get("dir_fd") is not None and not switch_swapped:
                    switch_swapped = True
                    (switch_queue / "inbox").rename(switch_queue / "inbox-original")
                    (switch_queue / "inbox").symlink_to(switch_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder.os,
                "open",
                side_effect=racing_switch_open,
            ):
                switched = builder.agent_switch(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": switch_session,
                        "target_role": "tech-qa",
                        "to": {
                            "provider": "openai",
                            "model": "gpt-5.6-luna",
                            "execution_mode": "codex",
                        },
                        "reason": "descriptor-bound test",
                        "dry_run": True,
                    },
                )
            self.assertTrue(switch_swapped)
            self.assertEqual(switched["decision"], "ok")
            self.assertEqual(switched["agentSwitch"]["result"], "dry_run")

            close_session = "close-session"
            close_dir = state_root / close_session
            close_dir.mkdir(parents=True)
            (close_dir / "bootstrap.json").write_text(
                json.dumps({"organization_instance_id": "org-close"}),
                encoding="utf-8",
            )
            close_queue = close_dir / "queue"
            write_inbox(
                close_queue / "inbox/tech-qa.yaml",
                [
                    {
                        "message_id": "message-close",
                        "task_id": "task-close",
                        "status": "pending",
                        "payload": {
                            "report_path": "reports/tech-qa/task-close/report.yaml"
                        },
                    }
                ],
            )
            trusted_report = close_queue / "reports/tech-qa/task-close/report.yaml"
            trusted_report.parent.mkdir(parents=True)
            trusted_report.write_text(json.dumps({"status": "done"}), encoding="utf-8")
            close_external = root / "close-external"
            close_external.mkdir()
            close_swapped = False

            def racing_close_open(path, flags, *args, **kwargs):
                nonlocal close_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "reports" and kwargs.get("dir_fd") is not None and not close_swapped:
                    close_swapped = True
                    (close_queue / "reports").rename(close_queue / "reports-original")
                    (close_queue / "reports").symlink_to(close_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder.os,
                "open",
                side_effect=racing_close_open,
            ):
                closed = builder.role_queue_close_message(
                    runtime="codex",
                    state_root=state_root,
                    hook_input={
                        "session_id": close_session,
                        "organization_instance_id": "org-close",
                        "role_id": "tech-qa",
                        "message_id": "message-close",
                    },
                )
            self.assertTrue(close_swapped)
            self.assertEqual(closed["decision"], "block")
            self.assertIn("existing terminal report", closed["reason"])

            watch_queue = root / "watch-queue"
            watch_child = watch_queue / "child"
            watch_child.mkdir(parents=True)
            (watch_child / "trusted.txt").write_text("trusted", encoding="utf-8")
            expected_snapshot = builder.queue_watch_snapshot(watch_queue)
            watch_external = root / "watch-external"
            watch_external.mkdir()
            (watch_external / "redirected.txt").write_text("redirected", encoding="utf-8")
            watch_swapped = False

            def racing_watch_open(path, flags, *args, **kwargs):
                nonlocal watch_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "child" and kwargs.get("dir_fd") is not None and not watch_swapped:
                    watch_swapped = True
                    watch_child.rename(watch_child.with_name("child-original"))
                    watch_child.symlink_to(watch_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder.os, "open", side_effect=racing_watch_open):
                raced_snapshot = builder.queue_watch_snapshot(watch_queue)
            self.assertTrue(watch_swapped)
            self.assertEqual(raced_snapshot, expected_snapshot)

    def test_completion_wait_consumer_survives_parent_replacement(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        message = {
            "message_id": "completion-message",
            "task_id": "completion-task",
            "status": "pending",
            "retry_count": 0,
            "payload": {
                "report_path": "reports/tech-qa/completion-task/report.yaml"
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            session_id = "completion-session"
            session_dir = state_root / session_id
            queue_root = session_dir / "queue"
            inbox_path = queue_root / "inbox/tech-qa.yaml"
            inbox_path.parent.mkdir(parents=True)
            inbox_path.write_text(
                json.dumps(
                    {
                        "envelope_version": "1",
                        "role_id": "tech-qa",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            watched = queue_root / "watched"
            watched.mkdir()
            (watched / "trusted.txt").write_text("trusted", encoding="utf-8")
            external = root / "completion-external"
            external.mkdir()
            (external / "redirected.txt").write_text("redirected", encoding="utf-8")
            original_open = os.open
            original_scandir = os.scandir
            swapped = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "watched" and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    watched.rename(watched.with_name("watched-original"))
                    watched.symlink_to(external, target_is_directory=True)
                return fd

            def guarded_scandir(path):
                if not isinstance(path, int) and Path(path) == watched:
                    raise AssertionError("completion watcher followed a replaced pathname")
                return original_scandir(path)

            with mock.patch.object(builder.os, "open", side_effect=racing_open), mock.patch.object(
                builder.os,
                "scandir",
                side_effect=guarded_scandir,
            ):
                output = builder.wait_for_role_queue_completion(
                    runtime="codex",
                    state_root=state_root,
                    session_dir=session_dir,
                    session_id=session_id,
                    organization_instance_id="org-completion",
                    queue_root=queue_root,
                    inbox_path=inbox_path,
                    role_id="tech-qa",
                    role_row=canonical,
                    message=message,
                    timeout_seconds=0.05,
                    poll_interval_seconds=0.01,
                    event_driven=True,
                    hook_input={},
                )
            self.assertTrue(swapped)
            self.assertEqual(output["wait_result"], "timeout")
            self.assertEqual(output["completion_source"], "bounded_wait")

    def test_gate_latency_consumers_are_descriptor_bound_and_fail_closed(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        original_open = os.open

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            payload_queue = root / "payload-queue"
            trusted_payload = payload_queue / "tasks/task-id/message-id.yaml"
            trusted_payload.parent.mkdir(parents=True)
            trusted_payload.write_text(
                json.dumps({"created_at": "2026-09-01T00:00:00+09:00"}),
                encoding="utf-8",
            )
            payload_external = root / "payload-external"
            (payload_external / "task-id").mkdir(parents=True)
            (payload_external / "task-id/message-id.yaml").write_text(
                json.dumps({"created_at": "redirected"}),
                encoding="utf-8",
            )
            payload_swapped = False

            def racing_payload_open(path, flags, *args, **kwargs):
                nonlocal payload_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "tasks" and kwargs.get("dir_fd") is not None and not payload_swapped:
                    payload_swapped = True
                    (payload_queue / "tasks").rename(payload_queue / "tasks-original")
                    (payload_queue / "tasks").symlink_to(payload_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder.os, "open", side_effect=racing_payload_open):
                created_at = builder.gate_latency_task_payload_created_at(
                    payload_queue,
                    {"task_id": "task-id", "message_id": "message-id"},
                )
            self.assertTrue(payload_swapped)
            self.assertEqual(created_at, "2026-09-01T00:00:00+09:00")

            inbox_queue = root / "inbox-queue"
            trusted_inbox = inbox_queue / "inbox/tech-qa.yaml"
            trusted_inbox.parent.mkdir(parents=True)
            trusted_inbox.write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "message_id": "message-id",
                                "created_at": "2026-09-01T01:00:00+09:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            inbox_external = root / "latency-inbox-external"
            inbox_external.mkdir()
            (inbox_external / "tech-qa.yaml").write_text(
                json.dumps(
                    {
                        "messages": [
                            {"message_id": "message-id", "created_at": "redirected"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            inbox_swapped = False

            def racing_inbox_open(path, flags, *args, **kwargs):
                nonlocal inbox_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "inbox" and kwargs.get("dir_fd") is not None and not inbox_swapped:
                    inbox_swapped = True
                    (inbox_queue / "inbox").rename(inbox_queue / "inbox-original")
                    (inbox_queue / "inbox").symlink_to(inbox_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder.os,
                "open",
                side_effect=racing_inbox_open,
            ):
                inbox_created_at = builder.gate_latency_inbox_message_created_at(
                    inbox_queue,
                    {"role_id": "tech-qa", "message_id": "message-id"},
                )
            self.assertTrue(inbox_swapped)
            self.assertEqual(inbox_created_at, "2026-09-01T01:00:00+09:00")

            report_queue = root / "report-queue"
            trusted_report_dir = report_queue / "reports/tech-qa/task-id"
            trusted_report_dir.mkdir(parents=True)
            trusted_report = {
                "report_type": "role_queue_report",
                "from_role": "tech-qa",
                "task_id": "task-id",
                "message_id": "message-id",
                "summary": "trusted",
            }
            (trusted_report_dir / "report.json").write_text(
                json.dumps(trusted_report),
                encoding="utf-8",
            )
            report_external = root / "report-external"
            report_external.mkdir()
            (report_external / "report.json").write_text(
                json.dumps({**trusted_report, "summary": "redirected"}),
                encoding="utf-8",
            )
            report_swapped = False

            def racing_report_open(path, flags, *args, **kwargs):
                nonlocal report_swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "task-id" and kwargs.get("dir_fd") is not None and not report_swapped:
                    report_swapped = True
                    trusted_report_dir.rename(trusted_report_dir.with_name("task-id-original"))
                    trusted_report_dir.symlink_to(report_external, target_is_directory=True)
                return fd

            with mock.patch.object(builder.os, "open", side_effect=racing_report_open):
                report, _report_path = builder.gate_latency_role_report_for_metric(
                    report_queue,
                    {
                        "role_id": "tech-qa",
                        "task_id": "task-id",
                        "message_id": "message-id",
                    },
                )
            self.assertTrue(report_swapped)
            self.assertEqual(report["summary"], "trusted")

            invalid_queue = root / "invalid-queue"
            invalid_report_ref = "reports/tech-qa/task-id/report.json"
            invalid_report_path = invalid_queue / invalid_report_ref
            invalid_report_path.parent.mkdir(parents=True)
            invalid_report_path.write_text(
                json.dumps(
                    {
                        **trusted_report,
                        "provider_evidence": {
                            "provider": "openai",
                            "intended_model": "gpt-5.6-luna",
                            "effective_model": "gpt-5.6-luna",
                            "usage_source": "codex_exec_json",
                            "session_id": "session-a",
                            "sessionId": "session-b",
                            "input_tokens": 7,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
                enriched, enrichment = builder.gate_latency_enrich_metric_from_report(
                    state_root=root / "state",
                    queue_root=invalid_queue,
                    metric={
                        "role_id": "tech-qa",
                        "task_id": "task-id",
                        "message_id": "message-id",
                        "report_ref": invalid_report_ref,
                    },
                )
            self.assertEqual(enrichment["result"], "rejected_provider_identity")
            self.assertNotIn("input_tokens", enriched)
            self.assertEqual(enriched["effective_model"], "")
            self.assertEqual(enriched["provider_identity_status"], "invalid")

    def test_descriptor_bound_queue_and_transcript_io_survive_parent_replacement(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_root = root / "queue"
            report_dir = queue_root / "reports" / "task-id"
            report_dir.mkdir(parents=True)
            external = root / "external"
            external.mkdir()
            original_open = os.open
            swapped = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "reports" and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    (queue_root / "reports").rename(queue_root / "reports-original")
                    (queue_root / "reports").symlink_to(external, target_is_directory=True)
                return fd

            with mock.patch.object(builder.os, "open", side_effect=racing_open):
                builder.write_queue_json_yaml(
                    queue_root,
                    "reports/task-id/report.yaml",
                    "report_path",
                    {"result": "descriptor-bound"},
                )
            self.assertTrue(swapped)
            self.assertEqual(
                json.loads(
                    (queue_root / "reports-original/task-id/report.yaml").read_text(
                        encoding="utf-8"
                    )
                )["result"],
                "descriptor-bound",
            )
            self.assertFalse((external / "task-id/report.yaml").exists())

            project_dir = root / "project"
            project_dir.mkdir()
            (project_dir / "turn.jsonl").write_text(
                json.dumps({"usage": {"input_tokens": 1}}) + "\n",
                encoding="utf-8",
            )
            malicious = root / "malicious"
            malicious.mkdir()
            (malicious / "turn.jsonl").write_text(
                json.dumps({"usage": {"input_tokens": 999}}) + "\n",
                encoding="utf-8",
            )
            project_fd = os.open(
                project_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                project_dir.rename(root / "project-original")
                project_dir.symlink_to(malicious, target_is_directory=True)
                records = builder.provider_transcript_records(
                    project_dir / "turn.jsonl",
                    directory_fd=project_fd,
                )
            finally:
                os.close(project_fd)
            self.assertEqual(records[0]["usage"]["input_tokens"], 1)

    def test_codex_provider_identifiers_are_bounded_and_local_request_identity_wins(self) -> None:
        builder = load_builder_module()

        def mutate_current(mutator) -> str:
            events = [
                json.loads(line)
                for line in current_codex_jsonl().splitlines()
                if line.strip()
            ]
            mutator(events)
            return "\n".join(json.dumps(event) for event in events) + "\n"

        oversized = "a" * (builder.PROVIDER_IDENTIFIER_MAX_CHARS + 1)
        maximum = "a" * builder.PROVIDER_IDENTIFIER_MAX_CHARS
        self.assertEqual(
            builder.parse_codex_json_output(
                mutate_current(lambda events: events[0].update({"thread_id": oversized}))
            ),
            {},
        )
        self.assertEqual(
            builder.parse_codex_json_output(
                mutate_current(lambda events: events[0].update({"thread_id": "thread\u0001id"}))
            ),
            {},
        )
        self.assertEqual(
            builder.parse_codex_json_output(
                mutate_current(
                    lambda events: events[2]["item"].update({"id": "item\u0001id"})
                )
            ),
            {},
        )
        accepted = builder.parse_codex_json_output(
            mutate_current(lambda events: events[0].update({"thread_id": maximum}))
        )
        self.assertEqual(accepted["session_id"], maximum)

        legacy = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "review complete",
                "session_id": "provider-session",
                "request_id": "provider-request",
                "model": "gpt-5.6-luna",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "duration_api_ms": 1,
                "num_turns": 1,
            }
        ) + "\n"
        invalid_legacy = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "review complete",
                "session_id": "provider-session",
                "request_id": oversized,
            }
        ) + "\n"
        self.assertEqual(builder.parse_codex_json_output(invalid_legacy), {})

        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "organization_instance_id": "org-request-binding",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "fallback_models": "",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
            "queue_consumer": False,
            "queue_finalizer": "role-report",
        }
        completed = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=legacy,
            stderr="",
        )
        for entrypoint in ("direct", "activation"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as tmp:
                state_root = Path(tmp)
                session_dir = state_root / "session"
                session_dir.mkdir(parents=True)
                (session_dir / "roster.json").write_text(
                    json.dumps([canonical]),
                    encoding="utf-8",
                )
                with mock.patch.object(
                    builder,
                    "role_agent_row_for",
                    return_value=canonical,
                ), mock.patch.object(
                    builder,
                    "registry_row_for",
                    return_value={},
                ), mock.patch.object(
                    builder.shutil,
                    "which",
                    return_value="/usr/bin/codex",
                ), mock.patch.object(
                    builder,
                    "run_command_with_bounded_output",
                    return_value=completed,
                ):
                    hook_input = {
                        "session_id": "session",
                        "organization_instance_id": "org-request-binding",
                        "agent_id": "tech-qa",
                        "request_id": "local-request",
                        "prompt": "Review only.",
                    }
                    output = (
                        builder.codex_exec_agent_dispatch(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                        if entrypoint == "direct"
                        else builder.provider_activate(
                            runtime="codex",
                            state_root=state_root,
                            hook_input=hook_input,
                        )
                    )

                evidence = json.loads(
                    (session_dir / "invocation-evidence.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                roster = json.loads(
                    (session_dir / "roster.json").read_text(encoding="utf-8")
                )
                public = (
                    output["agentDispatch"]
                    if entrypoint == "direct"
                    else output["activation"]
                )
                self.assertEqual(public["request_id"], "local-request")
                self.assertEqual(
                    public["provider_reported_request_id"],
                    "provider-request",
                )
                self.assertEqual(evidence["request_id"], "local-request")
                self.assertEqual(
                    evidence["provider_reported_request_id"],
                    "provider-request",
                )
                self.assertEqual(roster[0]["last_request_id"], "local-request")

    def test_invalid_terminal_report_is_bounded_and_terminalized_once(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        raw_secret = "SECRET-DO-NOT-PERSIST-" + ("x" * 4096)
        message = {
            "message_id": "message-id",
            "from_role": "tech-director",
            "to_role": "tech-qa",
            "task_id": "task-id",
            "created_at": "2026-09-01T00:00:00+09:00",
            "status": "pending",
            "retry_count": 0,
            "payload": {"report_path": "reports/tech-qa/task-id/report.yaml"},
        }
        invalid_report = {
            "report_version": raw_secret,
            "report_type": "role_queue_report",
            "from_role": raw_secret,
            "message_id": raw_secret,
            "created_at": "2026-09-01T00:00:01+09:00",
            "result": "failed",
            "status": raw_secret,
            "summary": "invalid",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "session"
            queue_root = session_dir / "queue"
            inbox_path = queue_root / "inbox/tech-qa.yaml"
            report_path = queue_root / "reports/tech-qa/task-id/report.yaml"
            inbox_path.parent.mkdir(parents=True)
            report_path.parent.mkdir(parents=True)
            inbox_path.write_text(
                json.dumps(
                    {
                        "envelope_version": "1",
                        "role_id": "tech-qa",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text(json.dumps(invalid_report), encoding="utf-8")

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
                for _attempt in range(2):
                    recovered = builder.recover_pending_message_from_existing_report(
                        runtime="codex",
                        session_dir=session_dir,
                        session_id="session",
                        organization_instance_id="org-invalid-report",
                        queue_root=queue_root,
                        inbox_path=inbox_path,
                        role_id="tech-qa",
                        role_row=canonical,
                        message=message,
                        now="2026-09-01T00:01:00+09:00",
                    )
                    self.assertIsNone(recovered)

            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
            persisted_message = inbox["messages"][0]
            queue_events = (session_dir / "queue-events.jsonl").read_text(
                encoding="utf-8"
            )
            gate_metrics = (session_dir / "gate-metrics.jsonl").read_text(
                encoding="utf-8"
            )
            queue_metrics = (
                queue_root / "metrics/tech-qa.jsonl"
            ).read_text(encoding="utf-8")
            self.assertEqual(persisted_message["status"], "failed")
            self.assertEqual(persisted_message["error"], "terminal_report_invalid")
            self.assertEqual(len(queue_events.splitlines()), 1)
            self.assertEqual(len(gate_metrics.splitlines()), 1)
            self.assertEqual(len(queue_metrics.splitlines()), 1)
            for persisted in (
                json.dumps(persisted_message),
                queue_events,
                gate_metrics,
                queue_metrics,
            ):
                self.assertNotIn(raw_secret, persisted)
                self.assertLess(len(persisted.encode("utf-8")), 16 * 1024)

    def test_invalid_terminal_report_never_overwrites_terminal_done(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        stale_message = {
            "message_id": "message-id",
            "from_role": "tech-director",
            "to_role": "tech-qa",
            "task_id": "task-id",
            "created_at": "2026-09-01T00:00:00+09:00",
            "status": "pending",
            "retry_count": 0,
            "payload": {"report_path": "reports/tech-qa/task-id/report.yaml"},
        }
        persisted_message = stale_message | {
            "status": "done",
            "done_at": "2026-09-01T00:00:30+09:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session"
            queue_root = session_dir / "queue"
            inbox_path = queue_root / "inbox/tech-qa.yaml"
            report_path = queue_root / "reports/tech-qa/task-id/report.yaml"
            inbox_path.parent.mkdir(parents=True)
            report_path.parent.mkdir(parents=True)
            inbox_path.write_text(
                json.dumps(
                    {
                        "envelope_version": "1",
                        "role_id": "tech-qa",
                        "messages": [persisted_message],
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps({"report_version": "invalid"}),
                encoding="utf-8",
            )

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical):
                recovered = builder.recover_pending_message_from_existing_report(
                    runtime="codex",
                    session_dir=session_dir,
                    session_id="session",
                    organization_instance_id="org-terminal-race",
                    queue_root=queue_root,
                    inbox_path=inbox_path,
                    role_id="tech-qa",
                    role_row=canonical,
                    message=stale_message,
                    now="2026-09-01T00:01:00+09:00",
                )

            self.assertIsNone(recovered)
            current = json.loads(inbox_path.read_text(encoding="utf-8"))["messages"][0]
            self.assertEqual(current["status"], "done")
            self.assertNotIn("invalid_report_sha256", current)
            self.assertFalse((session_dir / "queue-events.jsonl").exists())
            self.assertFalse((session_dir / "gate-metrics.jsonl").exists())

    def test_invalid_terminal_report_rejects_changed_snapshot_before_failure(self) -> None:
        builder = load_builder_module()
        canonical = {
            "agent_id": "tech-qa",
            "role_id": "tech-qa",
            "provider": "openai",
            "execution_mode": "codex",
            "intended_model": "gpt-5.6-luna",
            "inbox_path": "inbox/tech-qa.yaml",
            "report_dir": "reports/tech-qa",
            "allowed_tools": ["Read"],
            "git_operations_allowed": False,
        }
        message = {
            "message_id": "message-id",
            "from_role": "tech-director",
            "to_role": "tech-qa",
            "task_id": "task-id",
            "created_at": "2026-09-01T00:00:00+09:00",
            "status": "pending",
            "retry_count": 0,
            "payload": {"report_path": "reports/tech-qa/task-id/report.yaml"},
        }
        invalid_report = {"report_version": "invalid"}
        valid_replacement = {
            "report_version": "1",
            "report_type": "role_queue_report",
            "from_role": "tech-qa",
            "message_id": "message-id",
            "created_at": "2026-09-01T00:00:30+09:00",
            "result": "failed",
            "status": "failed",
            "summary": "replacement",
        }
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session"
            queue_root = session_dir / "queue"
            inbox_path = queue_root / "inbox/tech-qa.yaml"
            report_ref = "reports/tech-qa/task-id/report.yaml"
            report_path = queue_root / report_ref
            inbox_path.parent.mkdir(parents=True)
            report_path.parent.mkdir(parents=True)
            inbox_path.write_text(
                json.dumps(
                    {
                        "envelope_version": "1",
                        "role_id": "tech-qa",
                        "messages": [message],
                    }
                ),
                encoding="utf-8",
            )
            report_path.write_text(json.dumps(invalid_report), encoding="utf-8")
            invalid_bytes = report_path.read_bytes()
            original_read = builder.read_queue_file_bytes
            replaced = False

            def replace_after_invalid_snapshot(*args, **kwargs):
                nonlocal replaced
                raw = original_read(*args, **kwargs)
                if not replaced and raw == invalid_bytes:
                    report_path.write_text(json.dumps(valid_replacement), encoding="utf-8")
                    replaced = True
                return raw

            with mock.patch.object(builder, "role_agent_row_for", return_value=canonical), mock.patch.object(
                builder,
                "read_queue_file_bytes",
                side_effect=replace_after_invalid_snapshot,
            ):
                recovered = builder.recover_pending_message_from_existing_report(
                    runtime="codex",
                    session_dir=session_dir,
                    session_id="session",
                    organization_instance_id="org-snapshot-race",
                    queue_root=queue_root,
                    inbox_path=inbox_path,
                    role_id="tech-qa",
                    role_row=canonical,
                    message=message,
                    now="2026-09-01T00:01:00+09:00",
                )

            self.assertTrue(replaced)
            self.assertIsNone(recovered)
            current = json.loads(inbox_path.read_text(encoding="utf-8"))["messages"][0]
            self.assertEqual(current["status"], "pending")
            self.assertNotIn("invalid_report_sha256", current)
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["summary"],
                "replacement",
            )
            self.assertFalse((session_dir / "queue-events.jsonl").exists())
            self.assertFalse((session_dir / "gate-metrics.jsonl").exists())

    def test_queue_writer_rejects_oversized_serialized_output_before_touching_path(self) -> None:
        builder = load_builder_module()
        with tempfile.TemporaryDirectory() as tmp:
            queue_root = Path(tmp) / "queue"
            with mock.patch.object(builder, "QUEUE_FILE_MAX_BYTES", 128):
                with self.assertRaisesRegex(ValueError, "bounded serialized file limit"):
                    builder.write_queue_json_yaml(
                        queue_root,
                        "reports/task-id/report.yaml",
                        "report_path",
                        {"summary": "x" * 256},
                    )
                self.assertFalse(queue_root.exists())
                builder.write_queue_json_yaml(
                    queue_root,
                    "reports/task-id/report.yaml",
                    "report_path",
                    {"summary": "ok"},
                )
            self.assertEqual(
                builder.read_queue_json_yaml(
                    queue_root,
                    "reports/task-id/report.yaml",
                    "report_path",
                )["summary"],
                "ok",
            )
            self.assertEqual(list(queue_root.rglob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
