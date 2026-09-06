#!/usr/bin/env python3
"""Run Saihai offline validation suites and contract checks."""

from __future__ import annotations

import argparse
import glob
import json
import os
import py_compile
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from directory_paths import load_environment  # noqa: E402

ENV_DIAGNOSTICS = load_environment(checkout_root=REPO_ROOT, require_catalog=True)
SUITE_GLOBS = [
    "organization/runtime/workflows/tests/test_*.py",
    "organization/runtime/infra-team-bootstrap/tests/test_*.py",
    "organization/roles/infra-team-bootstrap/tests/test_*.py",
    "tests/test_*.py",
]
CONTRACT_CMDS = [
    [sys.executable, "organization/runtime/workflows/scripts/workflow_selector.py", "validate-contracts"],
    [sys.executable, "organization/runtime/workflows/scripts/template_role_validator.py"],
]
COMPILE_GLOBS = [
    "organization/runtime/workflows/scripts/*.py",
    "scripts/*.py",
    "server.py",
]


def discover_suites() -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for pattern in SUITE_GLOBS:
        for raw in sorted(glob.glob(str(REPO_ROOT / pattern))):
            path = Path(raw).resolve()
            if path not in seen:
                seen.add(path)
                discovered.append(path)
    return discovered


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return last_json_line(stdout)
    return parsed if isinstance(parsed, dict) else None


def parse_unittest_cases(*outputs: Any) -> int:
    text = "\n".join(output_text(output) for output in outputs if output_text(output))
    match = re.search(r"\bRan\s+(\d+)\s+tests?\b", text)
    return int(match.group(1)) if match else 0


def output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def tail(value: Any, limit: int = 500) -> str:
    value = output_text(value)
    compact = value.strip()
    if len(compact) <= limit:
        return compact
    return compact[-limit:]


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["SAIHAI_ALLOW_LIVE_PROVIDERS"] = ""
    env["SAIHAI_VALIDATE_ALL_CHILD"] = "1"
    return env


def unknown_counts() -> dict[str, Any]:
    # None means no usable count; zero is reserved for an observed zero.
    return {"cases": None, "executed": None, "failed": None, "skipped": None,
            "unknown": 1, "count_method": "unknown"}


def suite_json_result(text: str) -> tuple[dict[str, Any] | None, str]:
    """Distinguish ordinary unittest logging from an invalid final JSON result."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    try:
        value = json.loads(text, object_pairs_hook=unique_pairs)
    except json.JSONDecodeError:
        return None, "malformed" if text.lstrip().startswith(("{", "[", '"')) else "text"
    except (ValueError, RecursionError):
        return None, "malformed"
    return (value, "object") if isinstance(value, dict) else (None, "nonobject")


def structured_counts(payload: dict[str, Any]) -> dict[str, Any]:
    """Legacy cases remain supported, with no coercion or inferred zero success.

    Counts are suite-reported completions, not assertion/branch coverage or
    authenticated execution provenance. Optional outcome counts cannot be hidden
    behind a pass label. The six custom loops explicitly report their method.
    """
    cases = payload.get("cases")
    if type(cases) is not int or cases < 0:
        return unknown_counts()
    outcomes = {key: payload.get(key, 0) for key in ("failed", "skipped", "unknown")}
    # Existing E2E suites report skipped identifiers as a list. Count entries;
    # any nonempty list still blocks required success, regardless of its labels.
    if isinstance(outcomes["skipped"], list):
        outcomes["skipped"] = len(outcomes["skipped"])
    if any(type(value) is not int or value < 0 for value in outcomes.values()):
        return unknown_counts()
    if "executed" in payload and (type(payload["executed"]) is not int or payload["executed"] != cases):
        return unknown_counts()
    method = payload.get("count_method", "structured_result")
    if method not in ("structured_result", "completed_test_functions"):
        return unknown_counts()
    return {"cases": cases, "executed": cases, **outcomes, "count_method": method}


def unittest_counts(*outputs: str) -> tuple[dict[str, Any], bool] | None:
    """Read one complete unittest terminal summary, including non-success work.

    unittest's Ran count includes skipped tests. executed/cases exclude them;
    failures include errors, expected failures and unexpected successes because
    none of those satisfy required passing work. A missing/ambiguous summary is
    unknown, even when the child exits zero.
    """
    summaries = []
    for output in outputs:
        starts = list(re.finditer(r"^Ran (\d+) tests? in [^\n]+$", output, re.MULTILINE))
        for start in starts:
            ending = output[start.end():].strip()
            match = re.fullmatch(r"(OK|FAILED)(?: \(([^\n]+)\))?", ending)
            if match is None:
                summaries.append((unknown_counts(), False))
                continue
            counts = {key: 0 for key in ("failures", "errors", "skipped", "expected failures", "unexpected successes")}
            seen = set()
            valid = True
            for item in match.group(2).split(", ") if match.group(2) else []:
                key, separator, number = item.partition("=")
                if not separator or key not in counts or key in seen or not re.fullmatch(r"[0-9]+", number):
                    valid = False
                    break
                counts[key] = int(number)
                seen.add(key)
            ran = int(start.group(1))
            if not valid or counts["skipped"] > ran:
                summaries.append((unknown_counts(), False))
                continue
            executed = ran - counts["skipped"]
            failed = sum(counts[key] for key in counts if key != "skipped")
            value = {"cases": executed, "executed": executed, "failed": failed,
                     "skipped": counts["skipped"], "unknown": 0, "count_method": "unittest_summary"}
            summaries.append((value, match.group(1) == "OK"))
    if not summaries:
        return None
    return summaries[0] if len(summaries) == 1 else (unknown_counts(), False)


def run_suite(path: Path, *, timeout: float = 300) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {
        "path": rel(path), "command": [sys.executable, str(path)], "cwd": str(REPO_ROOT),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        completed = subprocess.run(
            result["command"], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=timeout, env=child_env(), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {**result, **unknown_counts(), "result": "fail", "status": "timed_out",
                "exit_code": None, "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(time.perf_counter() - started, 3), "detail": "timeout",
                "stdout_tail": tail(exc.stdout), "stderr_tail": tail(exc.stderr)}
    result.update(exit_code=completed.returncode, finished_at=datetime.now(timezone.utc).isoformat(),
                  duration_seconds=round(time.perf_counter() - started, 3))
    lines = completed.stdout.strip().splitlines()
    # Only the final stdout line may be a structured result. Never recover an
    # earlier pass after a malformed, missing or contradictory final record.
    payload, json_state = suite_json_result(lines[-1]) if lines else (None, "text")
    summary = unittest_counts(completed.stdout, completed.stderr)
    counts = unknown_counts()
    declared_pass = False
    if payload is not None:
        counts = structured_counts(payload)
        declared_pass = payload.get("result") == "pass"
        if summary is not None:
            # Two reporting channels must agree; a pass JSON cannot mask skips
            # or failures in the real unittest terminal output.
            observed, terminal_ok = summary
            if any(counts[key] != observed[key] for key in ("executed", "failed", "skipped", "unknown")):
                counts = unknown_counts()
            declared_pass = declared_pass and terminal_ok
    elif summary is not None:
        counts, declared_pass = summary
        # Scalar/null JSON is an invalid result too, not ordinary log text.
        if json_state != "text":
            counts, declared_pass = unknown_counts(), False
    passed = (completed.returncode == 0 and declared_pass
              and type(counts["executed"]) is int and counts["executed"] > 0
              and counts["failed"] == counts["skipped"] == counts["unknown"] == 0)
    result.update(counts, result="pass" if passed else "fail", status="passed" if passed else "failed",
                  detail="" if passed else (f"exit:{completed.returncode}" if completed.returncode else "required_test_evidence_not_passed"))
    if not passed:
        # Bounded diagnostics remain private; downstream public transport must
        # explicitly allowlist metadata rather than publish these output tails.
        result.update(stdout_tail=tail(completed.stdout), stderr_tail=tail(completed.stderr))
    return result


def run_contract(command: list[str], *, timeout: float = 300) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "result": "fail",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "detail": "timeout",
            "stdout_tail": tail(exc.stdout),
            "stderr_tail": tail(exc.stderr),
        }
    duration = round(time.perf_counter() - started, 3)
    payload = parse_json_stdout(completed.stdout)
    passed = completed.returncode == 0 and payload is not None and payload.get("decision") == "ok"
    return {
        "command": command,
        "result": "pass" if passed else "fail",
        "duration_seconds": duration,
        "detail": "" if passed else (f"exit:{completed.returncode}" if completed.returncode else "decision_not_ok"),
        "stdout_tail": "" if passed else tail(completed.stdout),
        "stderr_tail": "" if passed else tail(completed.stderr),
    }


def compile_targets() -> tuple[bool, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    targets: list[Path] = []
    seen: set[Path] = set()
    for pattern in COMPILE_GLOBS:
        for raw in sorted(glob.glob(str(REPO_ROOT / pattern))):
            path = Path(raw).resolve()
            if path.is_file() and path not in seen:
                seen.add(path)
                targets.append(path)
    for path in targets:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append({"path": rel(path), "error": tail(str(exc))})
    return not errors, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline Saihai validation")
    parser.add_argument("--only", default="", help="only run suites whose path contains this substring")
    parser.add_argument("--list", action="store_true", help="list discovered suites and exit")
    args = parser.parse_args()

    suites = discover_suites()
    if args.only:
        suites = [path for path in suites if args.only in rel(path)]
    if args.list:
        for path in suites:
            print(rel(path))
        return

    started = time.perf_counter()
    suite_results = [run_suite(path) for path in suites]
    contract_results = [run_contract(command) for command in CONTRACT_CMDS]
    compiled, compile_errors = compile_targets()
    no_suites = not suites
    failed = (
        no_suites
        or any(item["result"] != "pass" for item in suite_results)
        or any(item["result"] != "pass" for item in contract_results)
        or not compiled
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "result": "fail" if failed else "pass",
        "suites": suite_results,
        "contracts": contract_results,
        "compiled": compiled,
        "total_duration_seconds": round(time.perf_counter() - started, 3),
    }
    if no_suites:
        summary["detail"] = "no_suites_matched"
    if compile_errors:
        summary["compile_errors"] = compile_errors
    print(json.dumps(summary, ensure_ascii=False))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
