#!/usr/bin/env python3
"""Tests for workflow-run lifecycle transitions."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

run_store = importlib.import_module("run_store")
run_lock = importlib.import_module("run_lock")
run_lifecycle = importlib.import_module("run_lifecycle")
frontdoor = importlib.import_module("frontdoor_orchestrator")


def manual_principal() -> dict[str, str]:
    return {"principal_type": "manual_operator", "principal_id": "test-operator", "authn_method": "local_cli"}


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


def valid_run(**overrides) -> dict:
    candidate = {
        "run_version": "1",
        "run_id": "run-lifecycle",
        "task_id": "TSK-run-lifecycle",
        "request_id": "req-run-lifecycle",
        "workflow_id": "single_step_external_review",
        "goal_state": "approved",
        "run_state": "created",
        "current_step": "review",
        "iteration": 1,
        "max_steps": 1,
        "step_history": [],
        "activation": {
            "activation_version": "1",
            "activation_source": "manual_cli",
            "activation_status": "approved",
            "approved_by": "manual_operator",
            "approved_at": "2026-07-04T00:00:00+0900",
            "workflow_selection": {
                "status": "selected",
                "workflow_id": "single_step_external_review",
                "initial_step": "review",
                "safety_class": "readonly",
                "required_safety_class": "readonly",
                "publication_gate_required": False,
                "required_gates": [],
            },
            "classification_provenance": {
                "source": "deterministic_fixture",
                "confidence": 1.0,
                "evidence_refs": ["test-fixture"],
                "selector_threshold": 0.85,
                "tie_break": "deterministic_status_order:selected_waiting_human_blocked",
            },
            "context_scope": {
                "mode": "refs_only",
                "refs": ["organization/runtime/workflows/README.md"],
                "raw_transcript_sharing": "forbidden",
            },
            "activation_scope": {
                "allowed_paths": [],
                "allowed_ops": {"edit": False, "commit": False, "push": False, "network": False},
                "step_budget": 1,
                "expires_at": "run_terminal",
            },
            "next_action": "create_workflow_run",
        },
        "terminal": {"status": None, "reason": None},
        "requester": {"frontdoor": "manual"},
        "scheduling": {
            "scheduler_mode": "invocation-drain",
            "concurrency_group": "global",
            "state_persistence": "durable_state",
            "lock_policy": "global_advisory_lock",
            "concurrency": 1,
            "resume_policy": "manual",
        },
        "context_sharing": {
            "shared_run_state": "typed_durable_state",
            "step_local_snapshot": "immutable_step_attempt_snapshot",
            "provider_transcript": "confined_evidence_path_only",
        },
        "transitions": [],
    }
    candidate.update(overrides)
    return candidate


def store_run(state_root: Path, **overrides) -> dict:
    run = valid_run(**overrides)
    run_store.store_run(state_root, run)
    return run


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def assert_invalid_work_order_path(state_root: Path, run_id: str, step_id: str, label: str) -> None:
    try:
        run_lifecycle.work_order_path(state_root, run_id, step_id)
    except run_lifecycle.LifecycleError as exc:
        if exc.reason_class != "invalid_work_order":
            raise AssertionError(
                f"{label} reason: expected 'invalid_work_order', got {exc.reason_class!r}"
            )
    else:
        raise AssertionError(f"{label} must fail closed")


def transition(state_root: Path, run_id: str, to_state: str, reason_class: str) -> dict:
    return run_lifecycle.transition_run(
        state_root,
        run_id,
        to_state=to_state,
        reason_class=reason_class,
        transition="test_transition",
        principal=manual_principal(),
    )


def test_work_order_path_uses_confined_safe_constructor() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp) / "state"
        path = run_lifecycle.work_order_path(state_root, "run-lifecycle", "review")
        expected = state_root.resolve() / "work-orders" / "run-lifecycle" / "review.json"
        if path != expected:
            raise AssertionError(f"normal work order path: expected {expected!r}, got {path!r}")


def test_work_order_path_rejects_traversal_and_unsafe_components() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp) / "state"
        assert_invalid_work_order_path(state_root, "run-lifecycle", "../outside", "traversal step")
        assert_invalid_work_order_path(state_root, "run lifecycle", "review", "unsafe run component")


def test_work_order_path_rejects_symlinked_namespace() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        state_root = root / "state"
        outside = root / "outside"
        state_root.mkdir()
        outside.mkdir()
        (state_root / "work-orders").symlink_to(outside, target_is_directory=True)
        assert_invalid_work_order_path(state_root, "run-lifecycle", "review", "symlink namespace")


def test_transition_table_allows_p0_happy_path() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root)

        transition(state_root, "run-lifecycle", "step_queued", "step_queued")
        transition(state_root, "run-lifecycle", "waiting_provider", "provider_started")
        transition(state_root, "run-lifecycle", "validating", "report_received")
        transition(state_root, "run-lifecycle", "complete", "report_valid")

        run = run_store.load_run(state_root, "run-lifecycle")
        assert_equal(run["run_state"], "complete", "happy path run state")
        assert_equal(run["goal_state"], "complete", "happy path goal state")
        assert_equal([item["seq"] for item in run["transitions"]], [1, 2, 3, 4], "transition seq")
        assert_equal(
            [item["reason_class"] for item in run["transitions"]],
            ["step_queued", "provider_started", "report_received", "report_valid"],
            "transition reasons",
        )


def test_transition_signature_covers_full_record_payload() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root)

        record = run_lifecycle.transition_run(
            state_root,
            "run-lifecycle",
            to_state="step_queued",
            reason_class="step_queued",
            transition="test_transition",
            principal=manual_principal(),
            artifact_refs=["work-orders/run-lifecycle/review.json"],
        )
        unsigned_record = {key: value for key, value in record.items() if key != "signature"}
        expected = run_lifecycle.sign_transition(
            state_root=state_root,
            principal=manual_principal(),
            transition="test_transition",
            subject=unsigned_record,
        )
        assert_equal(record["signature"]["signature"], expected["signature"], "full record signature")

        tampered_reason = dict(unsigned_record)
        tampered_reason["reason_class"] = "tampered_reason"
        tampered_reason_signature = run_lifecycle.sign_transition(
            state_root=state_root,
            principal=manual_principal(),
            transition="test_transition",
            subject=tampered_reason,
        )
        assert record["signature"]["signature"] != tampered_reason_signature["signature"], "reason must be signed"

        tampered_refs = dict(unsigned_record)
        tampered_refs["artifact_refs"] = []
        tampered_ref_signature = run_lifecycle.sign_transition(
            state_root=state_root,
            principal=manual_principal(),
            transition="test_transition",
            subject=tampered_refs,
        )
        assert record["signature"]["signature"] != tampered_ref_signature["signature"], "artifact refs must be signed"


def test_terminal_rejects_further_transitions() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root)
        transition(state_root, "run-lifecycle", "step_queued", "step_queued")
        transition(state_root, "run-lifecycle", "waiting_provider", "provider_started")
        transition(state_root, "run-lifecycle", "validating", "report_received")
        transition(state_root, "run-lifecycle", "complete", "report_valid")
        before = (state_root / "runs" / "run-lifecycle.json").read_text(encoding="utf-8")
        try:
            transition(state_root, "run-lifecycle", "aborted", "operator_abort")
        except run_lifecycle.LifecycleError as exc:
            assert_equal(exc.reason_class, "terminal_state_immutable", "terminal rejection reason")
        else:
            raise AssertionError("terminal transition should be rejected")
        after = (state_root / "runs" / "run-lifecycle.json").read_text(encoding="utf-8")
        assert_equal(after, before, "terminal run file unchanged")


def test_illegal_transition_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root)
        try:
            transition(state_root, "run-lifecycle", "validating", "report_received")
        except run_lifecycle.LifecycleError as exc:
            assert_equal(exc.reason_class, "illegal_transition", "illegal transition reason")
            assert "created -> validating" in exc.errors[0]
        else:
            raise AssertionError("created -> validating should be rejected")


def test_abort_non_terminal() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root, run_state="step_queued", goal_state="active")
        aborted = run_lifecycle.abort_run(
            state_root,
            "run-lifecycle",
            reason="operator cancelled",
            principal=manual_principal(),
        )
        assert_equal(aborted["aborted"], True, "abort result")
        assert_equal(aborted["workflow_run"]["run_state"], "aborted", "aborted run state")
        assert_equal(aborted["workflow_run"]["terminal"]["status"], "aborted", "aborted terminal status")
        assert_equal(aborted["transition"]["reason_class"], "operator_abort", "abort reason class")


def test_abort_terminal_is_replay() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(
            state_root,
            run_state="complete",
            goal_state="complete",
            terminal={"status": "complete", "reason": "report_valid"},
        )
        aborted = run_lifecycle.abort_run(
            state_root,
            "run-lifecycle",
            reason="operator cancelled",
            principal=manual_principal(),
        )
        assert_equal(aborted["aborted"], False, "terminal abort replay")
        assert_equal(aborted["reason"], "terminal_run_already_set", "terminal abort reason")


def test_resume_maps_states_to_next_action() -> None:
    cases = [
        ("created", "approved", None, "drain", None),
        ("step_queued", "active", None, "run_step", None),
        ("validating", "active", None, "validate_report", None),
        ("complete", "complete", {"status": "complete", "reason": "report_valid"}, None, "terminal_run_already_set"),
    ]
    for run_state, goal_state, terminal, next_action, reason in cases:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_root = Path(raw_tmp)
            overrides = {"run_state": run_state, "goal_state": goal_state}
            if terminal is not None:
                overrides["terminal"] = terminal
            store_run(state_root, **overrides)
            resumed = run_lifecycle.resume_run(
                state_root,
                "run-lifecycle",
                principal=manual_principal(),
            )
            assert_equal(resumed["decision"], "ok", f"{run_state} resume decision")
            if next_action is not None:
                assert_equal(resumed["next_action"], next_action, f"{run_state} next action")
            if reason is not None:
                assert_equal(resumed["reason"], reason, f"{run_state} reason")


def test_resume_waiting_human_requires_flag() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root, run_state="waiting_human", goal_state="blocked")
        blocked = run_lifecycle.resume_run(
            state_root,
            "run-lifecycle",
            principal=manual_principal(),
        )
        assert_equal(blocked["decision"], "blocked", "waiting human blocked")
        assert_equal(blocked["reason"], "waiting_human", "waiting human reason")

        resumed = run_lifecycle.resume_run(
            state_root,
            "run-lifecycle",
            principal=manual_principal(),
            requeue=True,
        )
        assert_equal(resumed["decision"], "ok", "waiting human requeue decision")
        assert_equal(resumed["workflow_run"]["run_state"], "step_queued", "waiting human requeued state")
        assert_equal(resumed["transition"]["reason_class"], "human_resumed", "waiting human transition reason")


def test_resume_reclaims_expired_lease() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root, run_state="waiting_provider", goal_state="active")
        order_path = run_lifecycle.work_order_path(state_root, "run-lifecycle", "review")
        order_path.parent.mkdir(parents=True, exist_ok=True)
        order_path.write_text(
            json.dumps(
                {
                    "work_order_authority": {
                        "runner_claim": {
                            "claim_state": "claimed",
                            "lease_expires_at": "2000-01-01T00:00:00+0000",
                        }
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        resumed = run_lifecycle.resume_run(
            state_root,
            "run-lifecycle",
            principal=manual_principal(),
        )
        assert_equal(resumed["decision"], "ok", "expired lease resume decision")
        assert_equal(resumed["workflow_run"]["run_state"], "step_queued", "expired lease state")
        assert_equal(resumed["transition"]["reason_class"], "provider_lease_expired", "expired lease reason")
        work_order = json.loads(order_path.read_text(encoding="utf-8"))
        assert_equal(
            work_order["work_order_authority"]["runner_claim"],
            {"claim_state": "claimed", "lease_expires_at": "2000-01-01T00:00:00+0000"},
            "signed work order remains immutable",
        )


def expired_provider_execution(*, attempt_number: int = 1, retries_used: int = 0) -> dict:
    digest = "sha256:" + "1" * 64
    return {
        "execution_version": "1",
        "step_id": "review",
        "adapter_id": "claude_headless_p0",
        "work_order_digest": digest,
        "adapter_request_digest": "sha256:" + "2" * 64,
        "context_snapshot_digest": "sha256:" + "3" * 64,
        "phase": "invoking",
        "attempt_number": attempt_number,
        "attempt_id": f"provider-attempt-expired-{attempt_number}",
        "timeout_seconds": 1800,
        "lease": {
            "lease_id": f"provider-lease-expired-{attempt_number}",
            "claimed_by": manual_principal(),
            "claimed_at": "2000-01-01T00:00:00+00:00",
            "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
            "lease_expires_at": "2000-01-01T00:00:00+00:00",
        },
        "retry": {
            "last_failure_fingerprint": None,
            "consecutive_failures": 0,
            "auto_retries_used": retries_used,
            "max_auto_retries": 5,
        },
        "last_outcome": None,
    }


def test_expired_provider_attempts_persist_retry_budget_and_reach_human_gate() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(
            state_root,
            run_state="waiting_provider",
            goal_state="active",
            provider_execution=expired_provider_execution(),
        )
        for attempt_number in range(1, 7):
            resumed = run_lifecycle.resume_run(
                state_root,
                "run-lifecycle",
                principal=manual_principal(),
            )
            persisted = run_store.load_run(state_root, "run-lifecycle")
            if attempt_number <= 5:
                assert_equal(resumed["decision"], "ok", "expired retry decision")
                assert_equal(
                    persisted["provider_execution"]["retry"]["auto_retries_used"],
                    attempt_number,
                    "durable expired retry count",
                )
                persisted["run_state"] = "waiting_provider"
                persisted["goal_state"] = "active"
                persisted["provider_execution"]["phase"] = "invoking"
                persisted["provider_execution"]["attempt_number"] = attempt_number + 1
                persisted["provider_execution"]["attempt_id"] = f"provider-attempt-expired-{attempt_number + 1}"
                persisted["provider_execution"]["lease"]["lease_id"] = f"provider-lease-expired-{attempt_number + 1}"
                persisted["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
                run_store.store_run(state_root, persisted, expected_current_state="step_queued")
            else:
                assert_equal(resumed["decision"], "blocked", "exhausted retry decision")
                assert_equal(resumed["reason"], "provider_retry_exhausted", "exhausted reason")
                assert_equal(persisted["run_state"], "waiting_human", "human gate state")
                assert_equal(persisted["provider_execution"]["phase"], "human_gate", "human gate phase")
                journal = Path(persisted["provider_execution"]["last_outcome"]["attempt_result_path"])
                assert_equal(journal.stat().st_mode & 0o777, 0o600, "abandoned journal mode")


def test_resume_ignores_abandoned_journal_and_accounts_expired_lease() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(
            state_root,
            run_state="waiting_provider",
            goal_state="active",
            provider_execution=expired_provider_execution(),
        )
        interrupted = run_store.load_run(state_root, "run-lifecycle")
        retry_allowed, journal_path = run_lifecycle.account_expired_provider_attempt(
            state_root, interrupted
        )
        assert retry_allowed and journal_path is not None
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        assert_equal(journal["abandoned"], True, "abandoned journal marker")

        resumed = run_lifecycle.resume_run(
            state_root,
            "run-lifecycle",
            principal=manual_principal(),
        )
        assert_equal(resumed["decision"], "ok", "abandoned journal resume decision")
        assert_equal(resumed["reason"], "provider_lease_expired", "abandoned journal reason")
        assert_equal(resumed["workflow_run"]["run_state"], "step_queued", "abandoned journal state")
        assert_equal(
            resumed["workflow_run"]["provider_execution"]["retry"]["auto_retries_used"],
            1,
            "abandoned journal retry count",
        )


def test_resume_waiting_human_requeue_enforces_p0_concurrency() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root, run_state="waiting_human", goal_state="blocked")
        store_run(state_root, run_id="run-other", run_state="step_queued", goal_state="active")

        try:
            run_lifecycle.resume_run(
                state_root,
                "run-lifecycle",
                principal=manual_principal(),
                requeue=True,
            )
        except run_lock.LockContentionError as exc:
            assert_equal(exc.reason_class, "concurrency_limit_reached", "waiting human concurrency reason")
            assert_equal(exc.owner["inflight_run_ids"], ["run-other"], "waiting human competing runs")
        else:
            raise AssertionError("waiting human requeue must honor P0 concurrency")

        run = run_store.load_run(state_root, "run-lifecycle")
        assert_equal(run["run_state"], "waiting_human", "waiting human state unchanged")
        assert_equal(run["transitions"], [], "waiting human transitions unchanged")


def test_resume_expired_provider_lease_enforces_p0_concurrency_before_reset() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root, run_state="waiting_provider", goal_state="active")
        store_run(state_root, run_id="run-other", run_state="step_queued", goal_state="active")
        order_path = run_lifecycle.work_order_path(state_root, "run-lifecycle", "review")
        order_path.parent.mkdir(parents=True, exist_ok=True)
        order_path.write_text(
            json.dumps(
                {
                    "work_order_authority": {
                        "runner_claim": {
                            "claim_state": "claimed",
                            "lease_expires_at": "2000-01-01T00:00:00+0000",
                        }
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            run_lifecycle.resume_run(
                state_root,
                "run-lifecycle",
                principal=manual_principal(),
            )
        except run_lock.LockContentionError as exc:
            assert_equal(exc.reason_class, "concurrency_limit_reached", "provider lease concurrency reason")
            assert_equal(exc.owner["inflight_run_ids"], ["run-other"], "provider lease competing runs")
        else:
            raise AssertionError("expired provider lease resume must honor P0 concurrency")

        run = run_store.load_run(state_root, "run-lifecycle")
        assert_equal(run["run_state"], "waiting_provider", "provider lease state unchanged")
        assert_equal(run["transitions"], [], "provider lease transitions unchanged")
        work_order = json.loads(order_path.read_text(encoding="utf-8"))
        assert_equal(
            work_order["work_order_authority"]["runner_claim"]["claim_state"],
            "claimed",
            "runner claim remains claimed",
        )


def test_create_from_unapproved_activation_fails() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        frontdoor.proposed_request(
            state_root=state_root,
            task_id="TSK-unapproved",
            request_id="req-unapproved",
            user_prompt="Run bounded external review",
            refs=["organization/runtime/workflows/README.md"],
            classification=external_review_classification(),
            allowed_paths=[],
            expires_at="run_terminal",
            frontdoor="codex",
            chat_session_id="thread-unapproved",
            principal=manual_principal(),
        )
        try:
            frontdoor.create_run(
                state_root=state_root,
                request_id="req-unapproved",
                run_id="run-unapproved",
                resume_policy="manual",
                principal=manual_principal(),
            )
        except frontdoor.FrontdoorError as exc:
            assert "approved activation envelope required" in str(exc)
        else:
            raise AssertionError("create_run should reject unapproved activation")


def test_resume_does_not_duplicate_runs() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        store_run(state_root, run_id="run-no-duplicate")
        first = run_lifecycle.resume_run(
            state_root,
            "run-no-duplicate",
            principal=manual_principal(),
        )
        second = run_lifecycle.resume_run(
            state_root,
            "run-no-duplicate",
            principal=manual_principal(),
        )
        assert_equal(first["next_action"], "drain", "first resume next action")
        assert_equal(second["next_action"], "drain", "second resume next action")
        run_files = sorted((state_root / "runs").glob("*.json"))
        assert_equal([path.name for path in run_files], ["run-no-duplicate.json"], "single run file")


def main() -> None:
    tests = [
        test_work_order_path_uses_confined_safe_constructor,
        test_work_order_path_rejects_traversal_and_unsafe_components,
        test_work_order_path_rejects_symlinked_namespace,
        test_transition_table_allows_p0_happy_path,
        test_transition_signature_covers_full_record_payload,
        test_terminal_rejects_further_transitions,
        test_illegal_transition_rejected,
        test_abort_non_terminal,
        test_abort_terminal_is_replay,
        test_resume_maps_states_to_next_action,
        test_resume_waiting_human_requires_flag,
        test_resume_reclaims_expired_lease,
        test_expired_provider_attempts_persist_retry_budget_and_reach_human_gate,
        test_resume_ignores_abandoned_journal_and_accounts_expired_lease,
        test_resume_waiting_human_requeue_enforces_p0_concurrency,
        test_resume_expired_provider_lease_enforces_p0_concurrency_before_reset,
        test_create_from_unapproved_activation_fails,
        test_resume_does_not_duplicate_runs,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
