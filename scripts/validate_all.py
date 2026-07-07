#!/usr/bin/env python3
"""Run Saihai offline validation suites and contract checks."""

from __future__ import annotations

import argparse
import glob
import json
import os
import py_compile
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_GLOBS = [
    "organization/runtime/workflows/tests/test_*.py",
    "tests/test_*.py",
]
CONTRACT_CMDS = [
    [sys.executable, "organization/runtime/workflows/scripts/workflow_selector.py", "validate-contracts"],
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


def tail(value: str, limit: int = 500) -> str:
    compact = value.strip()
    if len(compact) <= limit:
        return compact
    return compact[-limit:]


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["SAIHAI_ALLOW_LIVE_PROVIDERS"] = ""
    env["SAIHAI_VALIDATE_ALL_CHILD"] = "1"
    return env


def run_suite(path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env=child_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "path": rel(path),
            "result": "fail",
            "cases": 0,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "detail": "timeout",
            "stderr_tail": tail(exc.stderr or ""),
        }
    duration = round(time.perf_counter() - started, 3)
    payload = last_json_line(completed.stdout)
    if completed.returncode == 0 and payload and payload.get("result") == "pass":
        return {
            "path": rel(path),
            "result": "pass",
            "cases": int(payload.get("cases") or 0),
            "duration_seconds": duration,
            "detail": "",
        }
    detail = "missing_result_json" if payload is None else f"result:{payload.get('result')}"
    if completed.returncode != 0:
        detail = f"exit:{completed.returncode}"
    return {
        "path": rel(path),
        "result": "fail",
        "cases": int((payload or {}).get("cases") or 0),
        "duration_seconds": duration,
        "detail": detail,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
    }


def run_contract(command: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=child_env(),
        check=False,
    )
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
