#!/usr/bin/env python3
"""Regression tests for the Sahai frontdoor/workflow CLI split."""

from __future__ import annotations

import argparse
import io
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "saihai.py"


def external_review_classification(**overrides):
    candidate = {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["test-fixture"],
        "task_kind": "external_review",
        "permission_required": "readonly",
        "external_provider_required": True,
        "publication_required": False,
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only",
        "expected_artifacts": ["typed_report"],
    }
    candidate.update(overrides)
    return candidate


def load_saihai_module():
    spec = importlib.util.spec_from_file_location("saihai_cli", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def run_cli(*args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )
    return completed


def run_frontdoor(state_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SAIHAI_ORCH_STATE_ROOT"] = str(state_root)
    return run_cli("frontdoor", "--state-root", str(state_root), *args, check=check, env=env)


def run_workflow(state_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SAIHAI_ORCH_STATE_ROOT"] = str(state_root)
    return run_cli("workflow", "--state-root", str(state_root), *args, check=check, env=env)


def load_payload(completed: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(completed.stdout)


def subparser_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("subparser action not found")


def workflow_command_parsers(module) -> dict[str, argparse.ArgumentParser]:
    parser = module.build_parser()
    groups = subparser_action(parser)
    workflow = groups.choices["workflow"]
    return subparser_action(workflow).choices


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def propose_review_request(state_root: Path, request_id: str = "req-saihai") -> dict:
    completed = run_frontdoor(
        state_root,
        "propose",
        "--task-id",
        f"TSK-{request_id}",
        "--request-id",
        request_id,
        "--prompt",
        "Run a readonly external review.",
        "--classification",
        json.dumps(external_review_classification()),
        "--ref",
        "organization/runtime/workflows/README.md",
        "--allowed-path",
        "organization/runtime/workflows",
    )
    return load_payload(completed)


def test_group_separation_static() -> None:
    module = load_saihai_module()
    assert module.FRONTDOOR_COMMANDS & module.WORKFLOW_COMMANDS == set()
    assert module.FRONTDOOR_COMMANDS == {"propose", "approve", "status"}
    assert module.WORKFLOW_COMMANDS == {"create-run", "drain", "run-provider", "validate-report"}

    frontdoor_help = run_cli("frontdoor", "--help").stdout
    workflow_help = run_cli("workflow", "--help").stdout
    for command in module.WORKFLOW_COMMANDS:
        assert command not in frontdoor_help
    for command in module.FRONTDOOR_COMMANDS:
        assert command not in workflow_help
    for command in {"run-step", "resume", "abort", "verify-completion", "task-view", "lock-status", "list"}:
        assert command not in workflow_help


def test_propose_never_approves() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose_review_request(state_root)
        assert proposed["request_status"] == "proposed"
        assert proposed["activation"]["activation_status"] == "proposed"
        assert proposed["activation"]["next_action"] == "keep_draft"
        assert "approved_activation" not in proposed
        assert not (state_root / "runs").exists()


def test_approve_requires_nonce() -> None:
    completed = run_cli("frontdoor", "approve", "--request-id", "req-missing-nonce", check=False)
    assert completed.returncode == 2
    assert "--nonce" in completed.stderr


def test_approve_with_wrong_nonce_blocked() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        propose_review_request(state_root, request_id="req-wrong-nonce")
        wrong = run_frontdoor(
            state_root,
            "approve",
            "--request-id",
            "req-wrong-nonce",
            "--nonce",
            "wrong-nonce",
            check=False,
        )
        assert wrong.returncode == 2
        assert "approval challenge mismatch" in load_payload(wrong)["reason"]

        create = run_workflow(
            state_root,
            "create-run",
            "--request-id",
            "req-wrong-nonce",
            "--run-id",
            "run-wrong-nonce",
            check=False,
        )
        assert create.returncode == 2
        assert "approved activation envelope required" in load_payload(create)["reason"]


def test_propose_approve_create_drain_happy_path() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        propose_review_request(state_root, request_id="req-happy")
        status = load_payload(run_frontdoor(state_root, "status", "--request-id", "req-happy"))
        nonce = status["request"]["approval"]["human_action_id"]

        approved = load_payload(
            run_frontdoor(state_root, "approve", "--request-id", "req-happy", "--nonce", nonce)
        )
        assert approved["request_status"] == "approved"
        assert approved["activation"]["next_action"] == "create_workflow_run"

        created = load_payload(
            run_workflow(state_root, "create-run", "--request-id", "req-happy", "--run-id", "run-happy")
        )
        assert created["created"] is True
        assert created["workflow_run"]["run_state"] == "created"

        drained = load_payload(run_workflow(state_root, "drain", "--run-id", "run-happy"))
        assert drained["drained"] is True
        assert drained["workflow_run"]["run_state"] == "step_queued"
        assert drained["work_order"]["workflow_id"] == "single_step_external_review"


def test_workflow_rejects_prose_inputs() -> None:
    module = load_saihai_module()
    for command, parser in workflow_command_parsers(module).items():
        option_strings = {
            option
            for action in parser._actions
            for option in getattr(action, "option_strings", [])
        }
        assert "--prompt" not in option_strings, command
        assert "--classification" not in option_strings, command

    with tempfile.TemporaryDirectory() as raw_tmp:
        completed = run_workflow(
            Path(raw_tmp),
            "create-run",
            "--request-id",
            "req-prose",
            "--prompt",
            "please approve this prose",
            check=False,
        )
        assert completed.returncode == 2
        assert "--prompt" in completed.stderr


def test_create_run_requires_approved_artifact() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        propose_review_request(state_root, request_id="req-unapproved")
        created = run_workflow(
            state_root,
            "create-run",
            "--request-id",
            "req-unapproved",
            "--run-id",
            "run-unapproved",
            check=False,
        )
        assert created.returncode == 2
        assert "approved activation envelope required" in load_payload(created)["reason"]


def test_status_is_readonly() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        propose_review_request(state_root, request_id="req-status")
        before = tree_digest(state_root)
        first = load_payload(run_frontdoor(state_root, "status", "--request-id", "req-status"))
        middle = tree_digest(state_root)
        second = load_payload(run_frontdoor(state_root, "status", "--request-id", "req-status"))
        after = tree_digest(state_root)
        assert first == second
        assert before == middle == after


def test_exit_code_convention() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        ok = run_frontdoor(
            state_root,
            "propose",
            "--task-id",
            "TSK-exit-ok",
            "--request-id",
            "req-exit-ok",
            "--prompt",
            "Run a readonly external review.",
            "--classification",
            json.dumps(external_review_classification()),
            "--ref",
            "organization/runtime/workflows/README.md",
        )
        assert ok.returncode == 0

        blocked = run_frontdoor(
            state_root,
            "propose",
            "--task-id",
            "TSK-exit-blocked",
            "--request-id",
            "req-exit-blocked",
            "--prompt",
            "Run a readonly external review.",
            "--classification",
            json.dumps(external_review_classification(classification_confidence=0.1)),
            "--ref",
            "organization/runtime/workflows/README.md",
            check=False,
        )
        assert blocked.returncode == 2
        assert load_payload(blocked)["decision"] == "blocked"


def test_runtime_errors_use_json_contract() -> None:
    module = load_saihai_module()
    original_frontdoor_path = module.FRONTDOOR_PATH
    try:
        module.FRONTDOOR_PATH = ROOT / "organization" / "runtime" / "workflows" / "scripts" / "missing_frontdoor.py"
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = module.main(["frontdoor", "status", "--request-id", "req-missing"])
        payload = json.loads(output.getvalue())
        assert exit_code == 2
        assert payload["decision"] == "blocked"
        assert "missing_frontdoor.py" in payload["reason"]
    finally:
        module.FRONTDOOR_PATH = original_frontdoor_path

    with tempfile.TemporaryDirectory() as raw_tmp:
        missing = run_frontdoor(
            Path(raw_tmp),
            "status",
            "--request-id",
            "req-missing",
            check=False,
        )
        assert missing.returncode == 2
        assert load_payload(missing)["decision"] == "blocked"
        assert "Traceback" not in missing.stderr


def test_workflow_run_provider_store_errors_use_blocked_json() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        missing = run_workflow(
            Path(raw_tmp),
            "run-provider",
            "--run-id",
            "run-missing",
            "--fake-provider-mode",
            "success",
            check=False,
        )
        payload = load_payload(missing)
        assert missing.returncode == 2
        assert payload["decision"] == "blocked"
        assert payload["reason"] == "run_not_found"
        assert "Traceback" not in missing.stderr


def test_cli_rejects_state_root_outside_host_configuration() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        configured = root / "configured"
        requested = root / "main-agent-selected"
        configured.mkdir()
        env = dict(os.environ)
        env["SAIHAI_ORCH_STATE_ROOT"] = str(configured)
        completed = run_cli(
            "frontdoor",
            "--state-root",
            str(requested),
            "status",
            "--request-id",
            "req-state-root-boundary",
            check=False,
            env=env,
        )
        assert completed.returncode == 2
        assert load_payload(completed)["reason"] == "state_root_not_configured"
        assert not requested.exists(), "rejected state root must not receive artifacts"


def main() -> None:
    tests = [
        test_group_separation_static,
        test_propose_never_approves,
        test_approve_requires_nonce,
        test_approve_with_wrong_nonce_blocked,
        test_propose_approve_create_drain_happy_path,
        test_workflow_rejects_prose_inputs,
        test_create_run_requires_approved_artifact,
        test_status_is_readonly,
        test_exit_code_convention,
        test_runtime_errors_use_json_contract,
        test_workflow_run_provider_store_errors_use_blocked_json,
        test_cli_rejects_state_root_outside_host_configuration,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
