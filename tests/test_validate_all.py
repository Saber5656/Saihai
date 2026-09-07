"""Exercise suite evidence using real child processes, never recursive full runs."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_all", ROOT / "scripts/validate_all.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class SuiteEvidenceTests(unittest.TestCase):
    def run_fixture(self, source: str, *, timeout: float = 5) -> dict:
        with tempfile.TemporaryDirectory(prefix="suite-evidence-") as raw:
            root = Path(raw)
            suite = root / "test_fixture.py"
            suite.write_text(textwrap.dedent(source), encoding="utf-8")
            with patch.object(runner, "REPO_ROOT", root):
                result = runner.run_suite(suite, timeout=timeout)
        return result

    def json_fixture(self, payload: dict, *, exit_code: int = 0) -> dict:
        return self.run_fixture(f"print({json.dumps(payload)!r})\nraise SystemExit({exit_code})")

    def test_exit_zero_without_result_is_not_success(self):
        self.assertEqual(self.run_fixture("print('no tests executed')")["result"], "fail")

    def test_missing_count_is_not_success(self):
        self.assertEqual(self.json_fixture({"result": "pass"})["result"], "fail")

    def test_counts_are_positive_strict_integers(self):
        for count in (0, -1, True, False, "1", "invalid", 1.5, None, [], {}):
            with self.subTest(count=count):
                result = self.json_fixture({"result": "pass", "cases": count})
                self.assertEqual(result["result"], "fail")

    def test_process_failure_overrides_pass_payload(self):
        result = self.json_fixture({"result": "pass", "cases": 2}, exit_code=7)
        self.assertEqual(result["result"], "fail")
        self.assertEqual(result["exit_code"], 7)

    def test_structured_failure_cannot_pass(self):
        self.assertEqual(self.json_fixture({"result": "fail", "cases": 2})["result"], "fail")

    def test_nonobject_and_malformed_results_fail(self):
        for output in ('[]', 'true', '{"result":', '{"result":"pass","cases":1,"cases":2}'):
            with self.subTest(output=output):
                self.assertEqual(self.run_fixture(f"print({output!r})")["result"], "fail")

    def test_final_result_must_not_fall_back_to_earlier_pass(self):
        output = '{"result":"pass","cases":1}\n{"result":'
        self.assertEqual(self.run_fixture(f"print({output!r})")["result"], "fail")

    def test_success_keeps_execution_metadata(self):
        result = self.json_fixture({"result": "pass", "cases": 2})
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["cases"], 2)
        self.assertEqual(result["executed"], 2)
        self.assertEqual((result["failed"], result["skipped"], result["unknown"]), (0, 0, 0))
        self.assertEqual(result["count_method"], "structured_result")
        self.assertEqual(result["command"][0], runner.sys.executable)
        self.assertTrue(result["command"][1].endswith("test_fixture.py"))
        self.assertEqual(result["exit_code"], 0)
        self.assertLessEqual(datetime.fromisoformat(result["started_at"]), datetime.fromisoformat(result["finished_at"]))
        self.assertNotIn("stdout_tail", result)
        self.assertNotIn("environment", result)

    def test_unittest_actual_pass_is_counted(self):
        result = self.run_fixture('''
            import unittest
            class Checks(unittest.TestCase):
                def test_one(self): self.assertTrue(True)
                def test_two(self): self.assertEqual(2, 2)
            unittest.main()
        ''')
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["executed"], 2)
        self.assertEqual(result["count_method"], "unittest_summary")

    def test_unittest_skipped_required_work_is_blocked(self):
        result = self.run_fixture('''
            import unittest
            class Checks(unittest.TestCase):
                def test_one(self): self.assertTrue(True)
                @unittest.skip("fixture unavailable")
                def test_skip(self): pass
            unittest.main()
        ''')
        self.assertEqual(result["result"], "fail")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["executed"], 1)

    def test_unittest_zero_and_failure_are_preserved(self):
        zero = self.run_fixture("import unittest\nunittest.main()")
        self.assertEqual(zero["result"], "fail")
        self.assertEqual(zero["executed"], 0)
        failure = self.run_fixture('''
            import unittest
            class Checks(unittest.TestCase):
                def test_failure(self): self.fail("expected fixture failure")
            unittest.main()
        ''')
        self.assertEqual(failure["result"], "fail")
        self.assertEqual(failure["failed"], 1)
        self.assertNotEqual(failure["exit_code"], 0)

    def test_unittest_count_without_terminal_outcome_is_unknown(self):
        result = self.run_fixture("print('Ran 3 tests in 0.001s')")
        self.assertEqual(result["result"], "fail")
        self.assertIsNone(result["executed"])

    def test_expected_failure_is_not_required_success(self):
        result = self.run_fixture('''
            import unittest
            class Checks(unittest.TestCase):
                @unittest.expectedFailure
                def test_expected(self): self.fail("fixture")
            unittest.main()
        ''')
        self.assertEqual(result["result"], "fail")
        self.assertEqual(result["failed"], 1)

    def test_structured_nonzero_or_invalid_outcome_counts_block(self):
        for key in ("failed", "skipped", "unknown"):
            for value in (1, True, "0", -1):
                with self.subTest(key=key, value=value):
                    result = self.json_fixture({"result": "pass", "cases": 2, key: value})
                    self.assertEqual(result["result"], "fail")

    def test_timeout_preserves_unknown_not_zero_success(self):
        result = self.run_fixture("import time\ntime.sleep(10)", timeout=0.05)
        self.assertEqual(result["result"], "fail")
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["exit_code"])
        self.assertIsNone(result["executed"])
        self.assertEqual(result["unknown"], 1)

    def test_custom_completed_method_and_mismatch(self):
        result = self.json_fixture({"result": "pass", "cases": 3, "count_method": "completed_test_functions"})
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["count_method"], "completed_test_functions")
        for extra in ({"executed": 1}, {"executed": True}, {"count_method": "source_function_inventory"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.json_fixture({"result": "pass", "cases": 3, **extra})["result"], "fail")

    def test_pass_json_cannot_mask_unittest_failure_or_skip(self):
        for terminal in ("FAILED (failures=1)", "OK (skipped=1)"):
            with self.subTest(terminal=terminal):
                summary = f"Ran 2 tests in 0.001s\n\n{terminal}\n"
                result = self.run_fixture(f"import sys\nprint('{{\"result\":\"pass\",\"cases\":2}}')\nsys.stderr.write({summary!r})")
                self.assertEqual(result["result"], "fail")

    def test_ambiguous_or_invalid_unittest_summary_fails(self):
        for summary in (
            "Ran 1 test in 0.001s\n\nOK\nRan 1 test in 0.001s\n\nOK\n",
            "Ran 1 test in 0.001s\n\nOK (skipped=2)\n",
            "Ran 1 test in 0.001s\n\nOK (skipped=0, skipped=1)\n",
            "Ran 1 test in 0.001s\n\nOK (unknown=1)\n",
        ):
            with self.subTest(summary=summary):
                self.assertEqual(self.run_fixture(f"print({summary!r})")["result"], "fail")

    def test_existing_e2e_skip_list_contract(self):
        for skipped, expected in (([], "pass"), (["fixture_unavailable"], "fail")):
            with self.subTest(skipped=skipped):
                result = self.json_fixture({"result": "pass", "cases": 3, "skipped": skipped})
                self.assertEqual(result["result"], expected)
                self.assertEqual(result["skipped"], len(skipped))

    def test_nonobject_json_after_unittest_cannot_fall_back_to_success(self):
        for final_line in ('null', 'true', '42', '"invalid result"', '[]'):
            with self.subTest(final_line=final_line):
                result = self.run_fixture(f'''
                    import unittest
                    class Checks(unittest.TestCase):
                        def test_one(self): self.assertTrue(True)
                    unittest.main(exit=False)
                    print({final_line!r})
                ''')
                self.assertEqual(result["result"], "fail")
                self.assertIsNone(result["executed"])

    def test_unittest_allows_ordinary_stdout_logging(self):
        result = self.run_fixture('''
            import unittest
            class Checks(unittest.TestCase):
                def test_one(self):
                    print("ordinary test progress")
                    self.assertTrue(True)
            unittest.main()
        ''')
        self.assertEqual(result["result"], "pass")


if __name__ == "__main__":
    unittest.main()
