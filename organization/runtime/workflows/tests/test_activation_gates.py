#!/usr/bin/env python3
"""Tests for explicit frontdoor activation gates."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
FACADE = ROOT / "scripts" / "configure_organization.py"
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import frontdoor_orchestrator as frontdoor  # noqa: E402


def typed_classification(task_kind: str = "external_review", **overrides):
    default_permission = {
        "external_review": "readonly",
        "research": "readonly",
        "code_change": "edit",
        "publication": "full",
        "policy_change": "edit",
    }[task_kind]
    default_artifacts = {
        "external_review": ["typed_report"],
        "research": ["research_report"],
        "code_change": ["code_change_report", "final_evidence"],
        "publication": ["code_change_report", "publication_result", "final_evidence"],
        "policy_change": ["policy_change_report", "final_evidence"],
    }[task_kind]
    candidate = {
        "classification_version": "1",
        "classification_source": "deterministic_fixture",
        "classification_confidence": 1.0,
        "classification_evidence": ["test-fixture"],
        "task_kind": task_kind,
        "permission_required": default_permission,
        "external_provider_required": task_kind == "external_review",
        "publication_required": task_kind == "publication",
        "security_sensitive": False,
        "destructive_operation": False,
        "context_scope": "refs_only" if default_permission == "readonly" else "diff_summary",
        "expected_artifacts": default_artifacts,
    }
    candidate.update(overrides)
    return candidate


def external_review_classification(**overrides):
    return typed_classification("external_review", **overrides)


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def read_request(state_root: Path, request_id: str) -> dict:
    return json.loads(frontdoor.request_path(state_root, request_id).read_text(encoding="utf-8"))


def read_audit_events(state_root: Path) -> list[dict]:
    path = state_root / "audit" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def propose(
    state_root: Path,
    request_id: str,
    *,
    task_id: str | None = None,
    classification: dict | None = None,
    refs: list[str] | None = None,
    allowed_paths: list[str] | None = None,
) -> dict:
    return frontdoor.proposed_request(
        state_root=state_root,
        task_id=task_id or f"TSK-{request_id}",
        request_id=request_id,
        user_prompt="Run a bounded workflow.",
        refs=refs if refs is not None else ["organization/runtime/workflows/README.md"],
        classification=classification if classification is not None else external_review_classification(),
        allowed_paths=allowed_paths if allowed_paths is not None else ["organization/runtime/workflows"],
        expires_at="run_terminal",
        frontdoor="codex",
        chat_session_id="thread-test",
    )


def expect_frontdoor_error(label: str, expected_fragment: str, func) -> str:
    try:
        func()
    except frontdoor.FrontdoorError as exc:
        message = str(exc)
        assert expected_fragment in message, f"{label}: expected {expected_fragment!r} in {message!r}"
        return message
    raise AssertionError(f"{label}: expected FrontdoorError")


def snapshot_names(state_root: Path, request_id: str) -> list[str]:
    return [Path(path).name for path in frontdoor.list_envelope_snapshots(state_root, request_id)]


def test_prompt_source_cannot_approve_or_start_run() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-prompt")
        assert_equal(proposed["activation"]["activation_source"], "frontdoor_prompt", "prompt source")
        assert_equal(proposed["activation"]["activation_status"], "proposed", "prompt status")
        assert "approved_by" not in proposed["activation"], "prompt proposal must not be approved"
        assert_equal(snapshot_names(state_root, "req-prompt"), ["0001-proposed.json"], "prompt snapshot")

        record = read_request(state_root, "req-prompt")
        assert "approved_activation" not in record, "prompt proposal must not store approval"
        expect_frontdoor_error(
            "prompt-only run creation",
            "approved activation envelope required",
            lambda: frontdoor.create_run(
                state_root=state_root,
                request_id="req-prompt",
                run_id="run-prompt",
                resume_policy="manual",
            ),
        )


def test_orchestrator_start_approves_with_challenge_and_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-orch")
        approved = frontdoor.orchestrator_start_approve(
            state_root=state_root,
            request_id="req-orch",
            human_action_id=proposed["approval"]["human_action_id"],
            invocation={
                "skill": "orchestrator-start",
                "invoked_at": "2026-07-09T00:00:00+0900",
                "chat_session_id": "thread-orch",
            },
        )
        activation = approved["activation"]
        assert_equal(approved["request_status"], "approved", "orchestrator approval status")
        assert_equal(activation["activation_source"], "orchestrator-start", "orchestrator source")
        assert_equal(
            activation["approved_by"],
            "human_explicit_skill_invocation",
            "orchestrator approved_by",
        )
        assert activation.get("approved_at"), "approved envelope must carry approved_at"
        assert_equal(activation["next_action"], "create_workflow_run", "orchestrator next action")
        assert "activation_scope" in activation, "approved envelope must carry activation scope"
        assert_equal(snapshot_names(state_root, "req-orch"), ["0001-proposed.json", "0002-approved.json"], "snapshots")

        record = read_request(state_root, "req-orch")
        assert_equal(
            record["orchestrator_start_invocation"]["skill"],
            "orchestrator-start",
            "stored invocation evidence",
        )
        events = [event for event in read_audit_events(state_root) if event["event_type"] == "orchestrator_start_approve"]
        assert events, "orchestrator-start approval should be audited"
        assert_equal(events[-1]["outcome"], "ok", "orchestrator audit outcome")


def test_orchestrator_start_rejects_bad_challenge_and_missing_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-orch-missing")
        expect_frontdoor_error(
            "missing orchestrator invocation evidence",
            "orchestrator_start_invocation_missing:invoked_at",
            lambda: frontdoor.orchestrator_start_approve(
                state_root=state_root,
                request_id="req-orch-missing",
                human_action_id=proposed["approval"]["human_action_id"],
                invocation={
                    "skill": "orchestrator-start",
                    "invoked_at": "",
                    "chat_session_id": "thread-orch",
                },
            ),
        )

    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-orch-rate")
        invocation = {
            "skill": "orchestrator-start",
            "invoked_at": "2026-07-09T00:00:00+0900",
            "chat_session_id": "thread-orch",
        }
        for index in range(frontdoor.MAX_APPROVAL_FAILURES):
            expect_frontdoor_error(
                f"bad orchestrator challenge {index}",
                "approval challenge mismatch",
                lambda: frontdoor.orchestrator_start_approve(
                    state_root=state_root,
                    request_id="req-orch-rate",
                    human_action_id=f"approve-wrong-{index}",
                    invocation=invocation,
                ),
            )
        record = read_request(state_root, "req-orch-rate")
        assert_equal(
            record["approval_rate_limit"]["failed_attempts"],
            frontdoor.MAX_APPROVAL_FAILURES,
            "failed challenge counter",
        )
        expect_frontdoor_error(
            "orchestrator challenge rate limit",
            "approval challenge rate limit exceeded",
            lambda: frontdoor.orchestrator_start_approve(
                state_root=state_root,
                request_id="req-orch-rate",
                human_action_id=proposed["approval"]["human_action_id"],
                invocation=invocation,
            ),
        )


def test_manual_cli_requires_confirm_nonce() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-manual")
        expect_frontdoor_error(
            "manual confirmation mismatch",
            "manual_confirmation_mismatch",
            lambda: frontdoor.manual_cli_approve(
                state_root=state_root,
                request_id="req-manual",
                human_action_id=proposed["approval"]["human_action_id"],
                confirm_nonce="approve-other",
            ),
        )
        approved = frontdoor.manual_cli_approve(
            state_root=state_root,
            request_id="req-manual",
            human_action_id=proposed["approval"]["human_action_id"],
            confirm_nonce="approve-req-manual",
        )
        assert_equal(approved["request_status"], "approved", "manual approval status")
        assert_equal(approved["activation"]["activation_source"], "manual_cli", "manual source")
        assert_equal(approved["activation"]["approved_by"], "manual_operator", "manual approved_by")


def test_human_ui_approved_by_mapping() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-human-ui")
        approved = frontdoor.approve_request(
            state_root=state_root,
            request_id="req-human-ui",
            human_action_id=proposed["approval"]["human_action_id"],
        )
        assert_equal(approved["activation"]["activation_source"], "human_ui", "human ui source")
        assert_equal(approved["activation"]["approved_by"], "human_ui_action", "human ui approved_by")


def test_bridge_principal_cannot_use_any_approve_path() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        bridge = frontdoor.bridge_principal("codex", "thread-bridge")
        proposed = propose(state_root, "req-bridge-approve")
        challenge = proposed["approval"]["human_action_id"]
        expect_frontdoor_error(
            "bridge human_ui approve",
            "unsupported approval principal: main_agent_bridge",
            lambda: frontdoor.approve_request(
                state_root=state_root,
                request_id="req-bridge-approve",
                human_action_id=challenge,
                principal=bridge,
            ),
        )
        expect_frontdoor_error(
            "bridge orchestrator-start approve",
            "unsupported approval principal: main_agent_bridge",
            lambda: frontdoor.orchestrator_start_approve(
                state_root=state_root,
                request_id="req-bridge-approve",
                human_action_id=challenge,
                invocation={
                    "skill": "orchestrator-start",
                    "invoked_at": "2026-07-09T00:00:00+0900",
                    "chat_session_id": "thread-bridge",
                },
                principal=bridge,
            ),
        )
        expect_frontdoor_error(
            "bridge manual approve",
            "unsupported approval principal: main_agent_bridge",
            lambda: frontdoor.manual_cli_approve(
                state_root=state_root,
                request_id="req-bridge-approve",
                human_action_id=challenge,
                confirm_nonce="approve-req-bridge-approve",
                principal=bridge,
            ),
        )


def test_refs_changed_between_proposal_and_approval_blocks() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-ref-change")
        record = read_request(state_root, "req-ref-change")
        record["resolved_context_refs"][0]["digest"] = "sha256:" + "0" * 64
        frontdoor.write_json(frontdoor.request_path(state_root, "req-ref-change"), record)
        expect_frontdoor_error(
            "changed ref digest approval",
            "context_refs_changed_since_proposal",
            lambda: frontdoor.approve_request(
                state_root=state_root,
                request_id="req-ref-change",
                human_action_id=proposed["approval"]["human_action_id"],
            ),
        )
        events = [event for event in read_audit_events(state_root) if event["event_type"] == "approve_request"]
        assert events, "changed refs should be audited"
        assert_equal(events[-1]["outcome"], "blocked", "changed refs audit outcome")


def test_destructive_publication_policy_fail_closed() -> None:
    unsupported = frontdoor.workflow_selector.activation_envelope(
        external_review_classification(),
        activation_source="chat_text",
        task_id="TSK-unsupported",
        request_id="req-unsupported",
        refs=["organization/runtime/workflows/README.md"],
    )
    assert_equal(unsupported["activation_status"], "blocked", "unsupported source status")
    assert_equal(unsupported["approval_required_reason"], "unsupported_activation_source", "unsupported source reason")

    cases = [
        (
            "req-destructive",
            external_review_classification(destructive_operation=True),
            "blocked",
            "destructive_operation_requires_separate_approval",
        ),
        (
            "req-publication",
            typed_classification("publication"),
            "waiting_human",
            "publication_requires_separate_human_gate",
        ),
        (
            "req-policy",
            typed_classification("policy_change"),
            "waiting_human",
            "policy_change_requires_separate_human_gate",
        ),
    ]
    for request_id, classification, expected_status, expected_reason in cases:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            proposed = propose(state_root, request_id, classification=classification)
            record = read_request(state_root, request_id)
            assert "approved_activation" not in record, f"{request_id} must not start approved"
            if proposed["request_status"] == "proposed":
                approved = frontdoor.manual_cli_approve(
                    state_root=state_root,
                    request_id=request_id,
                    human_action_id=proposed["approval"]["human_action_id"],
                    confirm_nonce=f"approve-{request_id}",
                )
                activation = approved["activation"]
            else:
                activation = proposed["activation"]
            assert_equal(activation["activation_status"], expected_status, f"{request_id} status")
            assert_equal(activation["approval_required_reason"], expected_reason, f"{request_id} reason")
            assert activation.get("next_action") != "create_workflow_run", f"{request_id} must not create runs"
            record = read_request(state_root, request_id)
            assert "approved_activation" not in record, f"{request_id} approval must not be stored"


def test_create_run_links_request_record() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        proposed = propose(state_root, "req-linked-run")
        frontdoor.approve_request(
            state_root=state_root,
            request_id="req-linked-run",
            human_action_id=proposed["approval"]["human_action_id"],
        )
        created = frontdoor.create_run(
            state_root=state_root,
            request_id="req-linked-run",
            run_id="run-linked-run",
            resume_policy="manual",
        )
        assert_equal(created["created"], True, "linked run created")
        assert created["request_record_path"].endswith("req-linked-run.json"), "request path should be returned"
        assert_equal(
            [Path(path).name for path in created["envelope_snapshots"]],
            ["0001-proposed.json", "0002-approved.json"],
            "create-run snapshot list",
        )
        record = read_request(state_root, "req-linked-run")
        assert_equal(record["linked_runs"], ["run-linked-run"], "request linked runs")

        replayed = frontdoor.create_run(
            state_root=state_root,
            request_id="req-linked-run",
            run_id="run-linked-run",
            resume_policy="manual",
        )
        assert_equal(replayed["created"], False, "linked run replay")
        record = read_request(state_root, "req-linked-run")
        assert_equal(record["linked_runs"], ["run-linked-run"], "linked run should not duplicate")


def test_cli_surfaces_are_available() -> None:
    for command in ("orchestrator-start-approve", "manual-approve"):
        completed = subprocess.run(
            [
                sys.executable,
                str(FACADE),
                "workflow-frontdoor",
                command,
                "--help",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert command in completed.stdout, f"{command} help should render"


def main() -> None:
    tests = [
        test_prompt_source_cannot_approve_or_start_run,
        test_orchestrator_start_approves_with_challenge_and_evidence,
        test_orchestrator_start_rejects_bad_challenge_and_missing_evidence,
        test_manual_cli_requires_confirm_nonce,
        test_human_ui_approved_by_mapping,
        test_bridge_principal_cannot_use_any_approve_path,
        test_refs_changed_between_proposal_and_approval_blocks,
        test_destructive_publication_policy_fail_closed,
        test_create_run_links_request_record,
        test_cli_surfaces_are_available,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
