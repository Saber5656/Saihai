#!/usr/bin/env python3
"""Deterministic offline E2E regression matrix for orchestrator failures."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from e2e_harness import HarnessAssertion, OrchestratorHarness


ScenarioRunner = Callable[[OrchestratorHarness], dict[str, Any]]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    group: str
    requires: tuple[str, ...]
    expect: dict[str, Any]
    run: ScenarioRunner


def require(condition: bool, label: str, context: Any = None) -> None:
    if not condition:
        suffix = "" if context is None else f": {context!r}"
        raise AssertionError(f"{label}{suffix}")


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def require_subset(actual: Any, expected: Any, label: str) -> None:
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{label} must be object", actual)
        for key, value in expected.items():
            require(key in actual, f"{label}.{key} missing", actual)
            require_subset(actual[key], value, f"{label}.{key}")
        return
    if isinstance(expected, list):
        require_equal(actual, expected, label)
        return
    require_equal(actual, expected, label)


def capture_blocked(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        call()
    except Exception as exc:
        return {
            "exception": type(exc).__name__,
            "reason": str(exc),
            "reason_class": getattr(exc, "reason_class", None),
        }
    raise AssertionError("operation unexpectedly succeeded")


def audit_events(state_root: Path) -> list[dict[str, Any]]:
    path = state_root / "audit" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def artifact_paths_containing(state_root: Path, marker: str, *, exclude: set[Path] | None = None) -> list[str]:
    excluded = {path.resolve() for path in (exclude or set())}
    hits: list[str] = []
    for path in state_root.rglob("*"):
        if not path.is_file() or path.resolve() in excluded:
            continue
        if marker.encode("utf-8") in path.read_bytes():
            hits.append(path.relative_to(state_root).as_posix())
    return sorted(hits)


def prepare_run(harness: OrchestratorHarness, suffix: str) -> str:
    request_id = f"req-failure-{suffix}"
    run_id = f"run-failure-{suffix}"
    harness.propose(task_id=f"TSK-failure-{suffix}", request_id=request_id)
    harness.approve(request_id)
    harness.create_run(request_id, run_id)
    harness.drain(run_id)
    return run_id


def run_provider_mode(harness: OrchestratorHarness, run_id: str, mode: str) -> dict[str, Any]:
    return harness.optional_modules["provider_runner"].run_provider_step(
        state_root=harness.state_root,
        run_id=run_id,
        adapter="fake_pass",
        fake_provider_mode=mode,
        live=False,
    )


def write_review_artifacts(
    harness: OrchestratorHarness,
    run_id: str,
    *,
    report_updates: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    prepared = harness.frontdoor.prepare_claude_adapter(state_root=harness.state_root, run_id=run_id)
    require_equal(prepared.get("decision"), "ok", "adapter preparation")
    request = prepared["adapter_request"]
    evidence_path = Path(request["evidence_path"])
    transcript_path = Path(request["transcript_path"])
    report_path = Path(request["report_path"])
    for path in (evidence_path, transcript_path, report_path):
        harness.frontdoor.run_store.ensure_private_directory(path.parent)
    transcript_path.write_text(json.dumps({"signal_only": True}) + "\n", encoding="utf-8")
    transcript_path.chmod(0o600)
    fixed_fields = request["evidence_contract"]["fixed_fields"]
    evidence = {
        **fixed_fields,
        "provider": "claude_headless",
        "effective_model": "offline-failure-fixture",
        "provider_request_id": f"provider-{run_id}",
        "provider_session_id": f"session-{run_id}",
        "duration_ms": 1,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "stdout_sha256": "sha256:" + hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
    }
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8")
    evidence_path.chmod(0o600)
    report = {
        "report_version": "1",
        "report_id": f"report-{run_id}",
        "request_id": request["request_id"],
        "run_id": run_id,
        "workflow_id": request["workflow_id"],
        "step_id": request["step_id"],
        "result": "pass",
        "summary": "Offline failure-matrix fixture.",
        "provider_evidence": {
            "provider": "claude_headless",
            "effective_model": "offline-failure-fixture",
            "request_id": request["request_id"],
            "provider_session_id": f"session-{run_id}",
            "transcript_path": str(transcript_path),
            "evidence_path": str(evidence_path),
        },
        "findings": [],
        "authority": {
            "canonical_result": "typed_report_file",
            "stdout_is_signal_only": True,
            "raw_transcript_shared": False,
        },
    }
    report.update(report_updates or {})
    report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.chmod(0o600)
    return report_path, evidence_path, transcript_path


def scenario_prompt_only_cannot_run(harness: OrchestratorHarness) -> dict[str, Any]:
    harness.propose(task_id="TSK-prompt-only", request_id="req-prompt-only")
    blocked = capture_blocked(
        lambda: harness.frontdoor.create_run(
            state_root=harness.state_root,
            request_id="req-prompt-only",
            run_id="run-prompt-only",
            resume_policy="manual",
        )
    )
    require_equal(blocked["reason"], "approved activation envelope required", "prompt-only rejection")
    require(not (harness.state_root / "runs" / "run-prompt-only.json").exists(), "prompt-only run absent")
    return {"reason": blocked["reason"], "run_created": False}


def scenario_approval_rate_limit(harness: OrchestratorHarness) -> dict[str, Any]:
    request_id = "req-rate-limit"
    harness.propose(task_id="TSK-rate-limit", request_id=request_id)
    for attempt in range(3):
        blocked = capture_blocked(
            lambda attempt=attempt: harness.frontdoor.approve_request(
                state_root=harness.state_root,
                request_id=request_id,
                human_action_id=f"wrong-{attempt}",
            )
        )
        require_equal(blocked["reason"], "approval challenge mismatch", "challenge rejection")
    blocked = capture_blocked(
        lambda: harness.frontdoor.approve_request(
            state_root=harness.state_root,
            request_id=request_id,
            human_action_id=harness.challenge(request_id),
        )
    )
    require_equal(blocked["reason"], "approval challenge rate limit exceeded", "rate-limit rejection")
    record = harness.frontdoor.read_json(harness.frontdoor.request_path(harness.state_root, request_id))
    require_equal(record["approval_rate_limit"]["failed_attempts"], 3, "failed approval count")
    require("approved_activation" not in record, "rate-limited request remains unapproved")
    return {"reason": blocked["reason"], "failed_attempts": 3}


def scenario_destructive_blocked(harness: OrchestratorHarness) -> dict[str, Any]:
    request_id = "req-destructive"
    proposal = harness.frontdoor.proposed_request(
        state_root=harness.state_root,
        task_id="TSK-destructive",
        request_id=request_id,
        user_prompt="Attempt a destructive operation.",
        refs=[harness.make_ref("destructive.md")],
        classification=harness.classification(destructive_operation=True, permission_required="write"),
        allowed_paths=[],
        expires_at="run_terminal",
        frontdoor="codex",
        chat_session_id="failure-matrix",
    )
    require_equal(proposal.get("decision"), "blocked", "destructive proposal decision")
    require_equal(proposal["activation"].get("activation_status"), "blocked", "destructive activation state")
    require_equal(proposal["activation"].get("approval_required_reason"), "invalid_classification", "destructive reason")
    blocked = capture_blocked(
        lambda: harness.frontdoor.create_run(
            state_root=harness.state_root, request_id=request_id, run_id="run-destructive", resume_policy="manual"
        )
    )
    require_equal(blocked["reason"], "approved activation envelope required", "destructive create rejection")
    return {"proposal": proposal["request_status"], "run_created": False}


def scenario_publication_waits_human(harness: OrchestratorHarness) -> dict[str, Any]:
    request_id = "req-publication"
    proposal = harness.frontdoor.proposed_request(
        state_root=harness.state_root,
        task_id="TSK-publication",
        request_id=request_id,
        user_prompt="Publish the reviewed result.",
        refs=[harness.make_ref("publication.md")],
        classification=harness.classification(publication_required=True),
        allowed_paths=[],
        expires_at="run_terminal",
        frontdoor="codex",
        chat_session_id="failure-matrix",
    )
    activation = proposal["activation"]
    require_equal(proposal.get("decision"), "blocked", "publication proposal decision")
    require_equal(activation.get("next_action"), "ask_human", "publication next action")
    require_equal(activation.get("approval_required_reason"), "publication_gate_required", "publication gate reason")
    return {"activation_status": activation["activation_status"], "reason": activation["approval_required_reason"], "next_action": activation["next_action"]}


def scenario_bridge_cannot_execute(harness: OrchestratorHarness) -> dict[str, Any]:
    request_id = "req-bridge-exec"
    harness.propose(task_id="TSK-bridge-exec", request_id=request_id)
    harness.approve(request_id)
    bridge = harness.frontdoor.bridge_principal("codex", "failure-matrix")
    blocked = capture_blocked(
        lambda: harness.frontdoor.create_run(
            state_root=harness.state_root,
            request_id=request_id,
            run_id="run-bridge-exec",
            resume_policy="manual",
            principal=bridge,
        )
    )
    require_equal(
        blocked["reason"],
        "bridge principal cannot perform execution transition: main_agent_bridge",
        "bridge execution rejection",
    )
    require(not (harness.state_root / "runs" / "run-bridge-exec.json").exists(), "bridge cannot create run")
    return {"reason": blocked["reason"], "run_created": False}


def scenario_bridge_smuggled_authority(harness: OrchestratorHarness) -> dict[str, Any]:
    payload = {
        "task_id": "TSK-bridge-smuggle",
        "request_id": "req-bridge-smuggle",
        "request_kind": "external_review",
        "prompt": "Review the bounded fixture.",
        "refs": [harness.make_ref("bridge-smuggle.md")],
        "allowed_paths": [],
        "idempotency_key": "bridge-smuggle-key",
        "frontdoor": "codex",
        "chat_session_id": "failure-matrix",
        "classification": harness.classification(),
        "run_id": "run-attacker-selected",
    }
    blocked = capture_blocked(
        lambda: harness.frontdoor.bridge_submit_request(
            state_root=harness.state_root,
            payload=payload,
            frontend_kind="codex",
        )
    )
    require_equal(
        blocked["reason"],
        "invalid bridge submit_request: unexpected_fields:classification,run_id; forbidden_fields:classification,run_id; request_kind unsupported:'external_review'",
        "bridge smuggling rejection",
    )
    events = [e for e in audit_events(harness.state_root) if e.get("event_type") == "bridge_submit_request"]
    require_equal(events[-1]["outcome"], "blocked", "bridge smuggling audit")
    return {"reason": blocked["reason"], "audit_outcome": "blocked"}


def scenario_invalid_report_blocks(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "invalid-report")
    report_path, _, _ = write_review_artifacts(harness, run_id)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    del report["provider_evidence"]["provider_session_id"]
    report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
    before = report_path.read_bytes()
    payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
    require_equal(payload.get("decision"), "blocked", "invalid report decision")
    require_equal(payload.get("outcome"), "report_invalid", "invalid report outcome")
    require_equal(payload["workflow_run"]["run_state"], "failed", "invalid report state")
    require_equal(report_path.read_bytes(), before, "invalid report preservation")
    rejections = list((harness.state_root / "reports" / run_id).glob("*rejection*.json"))
    require(bool(rejections), "invalid report rejection artifact")
    return {"outcome": payload["outcome"], "state": payload["workflow_run"]["run_state"]}


def scenario_malformed_report_file(harness: OrchestratorHarness) -> dict[str, Any]:
    cases = {
        "syntax": b"{not-json\n",
        "utf8": b"\xff\xfe",
        "array": b"[]\n",
    }
    results: dict[str, Any] = {}
    for suffix, content in cases.items():
        run_id = prepare_run(harness, f"malformed-report-{suffix}")
        report_path, _, _ = write_review_artifacts(harness, run_id)
        report_path.write_bytes(content)
        payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
        require_equal(payload.get("decision"), "blocked", f"{suffix} report decision")
        require_equal(payload.get("outcome"), "report_invalid", f"{suffix} report outcome")
        require_equal(payload["workflow_run"]["run_state"], "failed", f"{suffix} report state")
        results[suffix] = payload["outcome"]
    return {"outcome": "report_invalid", "state": "failed", "cases": results}


def scenario_missing_report_file(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "missing-report-file")
    payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
    require_equal(payload.get("decision"), "blocked", "missing report decision")
    require_equal(payload.get("outcome"), "report_not_written", "missing report outcome")
    require_equal(payload["workflow_run"]["run_state"], "waiting_human", "missing report state")
    return {"outcome": payload["outcome"], "state": payload["workflow_run"]["run_state"]}


def scenario_directory_report_path(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "directory-report-path")
    prepared = harness.frontdoor.prepare_claude_adapter(state_root=harness.state_root, run_id=run_id)
    report_path = Path(prepared["adapter_request"]["report_path"])
    harness.frontdoor.run_store.ensure_private_directory(report_path.parent)
    report_path.mkdir()
    payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
    require_equal(payload.get("decision"), "blocked", "directory report decision")
    require_equal(payload.get("outcome"), "report_not_written", "directory report outcome")
    require_equal(payload["workflow_run"]["run_state"], "waiting_human", "directory report state")
    artifact = json.loads(Path(payload["transition_artifact_path"]).read_text(encoding="utf-8"))
    require_equal(artifact["report_sha256"], None, "directory report digest")
    return {"outcome": payload["outcome"], "state": payload["workflow_run"]["run_state"], "digest": None}


def scenario_cross_run_report(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "cross-run-report")
    write_review_artifacts(harness, run_id, report_updates={"run_id": "run-other"})
    payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
    require_equal(payload.get("decision"), "blocked", "cross-run decision")
    require_equal(payload.get("outcome"), "scope_violation", "cross-run outcome")
    require_equal(payload["workflow_run"]["run_state"], "waiting_human", "cross-run state")
    return {"outcome": payload["outcome"], "state": payload["workflow_run"]["run_state"]}


def scenario_transcript_leak_is_scope_violation(harness: OrchestratorHarness) -> dict[str, Any]:
    marker = "RAW-REPORT-MARKER-7b4c2"
    run_id = prepare_run(harness, "transcript-leak")
    report_path, _, _ = write_review_artifacts(harness, run_id, report_updates={"raw_transcript": marker})
    payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
    require_equal(payload.get("outcome"), "scope_violation", "transcript leak outcome")
    require_equal(payload["workflow_run"]["run_state"], "waiting_human", "transcript leak state")
    hits = artifact_paths_containing(harness.state_root, marker, exclude={report_path})
    require_equal(hits, [], "raw report marker confinement")
    require(marker not in json.dumps(payload, ensure_ascii=False), "raw report marker excluded from response")
    return {"outcome": payload["outcome"], "leaked_artifacts": hits}


def scenario_provider_failure_matrix(harness: OrchestratorHarness) -> dict[str, Any]:
    expected = {
        "timeout": ("provider_timeout", "waiting_human"),
        "unavailable": ("provider_unavailable", "waiting_human"),
        "nonzero": ("provider_nonzero_exit", "waiting_human"),
        "malformed": ("provider_malformed_output", "waiting_human"),
    }
    results: dict[str, Any] = {}
    for mode, (reason, run_state) in expected.items():
        run_id = prepare_run(harness, f"provider-{mode}")
        payload = run_provider_mode(harness, run_id, mode)
        require_equal(payload.get("decision"), "blocked", f"{mode} decision")
        require_equal(payload.get("reason"), reason, f"{mode} reason")
        require_equal(payload["workflow_run"]["run_state"], run_state, f"{mode} state")
        evidence = json.loads(Path(payload["evidence_path"]).read_text(encoding="utf-8"))
        require_equal(evidence["outcome"], reason, f"{mode} evidence outcome")
        results[mode] = {"reason": reason, "state": run_state}
    return results


def scenario_report_not_written(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "report-not-written")
    module = harness.optional_modules["provider_runner"]
    original = module.execute_provider
    module.execute_provider = lambda **_kwargs: ("ok", None, {"duration_ms": 1})
    try:
        payload = run_provider_mode(harness, run_id, "success")
    finally:
        module.execute_provider = original
    require_equal(payload.get("reason"), "report_not_written", "missing report reason")
    require_equal(payload["workflow_run"]["run_state"], "waiting_human", "missing report state")
    return {"reason": payload["reason"], "state": payload["workflow_run"]["run_state"]}


def scenario_resume_after_timeout(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "resume-timeout")
    timed_out = run_provider_mode(harness, run_id, "timeout")
    require_equal(timed_out["workflow_run"]["run_state"], "waiting_human", "timeout state")
    resumed = harness.frontdoor.resume_run(state_root=harness.state_root, run_id=run_id, requeue=True)
    require_equal(resumed.get("reason"), "human_resumed", "resume reason")
    completed = harness.run_step(run_id, fake_provider_mode="success")
    require_equal(completed["workflow_run"]["run_state"], "complete", "recovered state")
    reasons = [item.get("reason_class") for item in harness.run_record(run_id).get("transitions", [])]
    for reason in ("provider_timeout", "human_resumed", "report_valid"):
        require(reason in reasons, f"transition reason {reason}", reasons)
    return {"state": "complete", "recovered": True}


def set_provider_claim(harness: OrchestratorHarness, run_id: str, lease_expires_at: str) -> Path:
    run = harness.run_record(run_id)
    harness.frontdoor.run_lifecycle.transition_run(
        harness.state_root,
        run_id,
        to_state="waiting_provider",
        reason_class="provider_invoked",
        transition="failure_matrix_interrupt",
        principal=harness.frontdoor.default_manual_principal(),
        run=run,
    )
    order_path = harness.frontdoor.run_lifecycle.work_order_path(harness.state_root, run_id, str(run["current_step"]))
    order = json.loads(order_path.read_text(encoding="utf-8"))
    order["work_order_authority"]["runner_claim"] = {
        "claim_state": "claimed",
        "lease_expires_at": lease_expires_at,
    }
    harness.frontdoor.run_store.atomic_write_json(order_path, order)
    return order_path


def scenario_claim_blocks_second_runner(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "claim-live")
    set_provider_claim(harness, run_id, "2999-01-01T00:00:00+0000")
    run_path = harness.state_root / "runs" / f"{run_id}.json"
    before = run_path.read_bytes()
    payload = run_provider_mode(harness, run_id, "success")
    require_equal(payload.get("decision"), "blocked", "live claim decision")
    require_equal(payload.get("reason"), "provider_in_flight", "live claim reason")
    require_equal(payload.get("run_state"), "waiting_provider", "live claim state")
    require_equal(run_path.read_bytes(), before, "live claim run immutability")
    return {"reason": payload["reason"], "state": payload["run_state"]}


def scenario_validate_respects_live_claim(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "validate-live-claim")
    set_provider_claim(harness, run_id, "2999-01-01T00:00:00+0000")
    run_path = harness.state_root / "runs" / f"{run_id}.json"
    before = run_path.read_bytes()
    payload = harness.frontdoor.validate_report(state_root=harness.state_root, run_id=run_id)
    require_equal(payload.get("decision"), "blocked", "live claim validation decision")
    require_equal(payload.get("reason"), "provider_in_flight", "live claim validation reason")
    require_equal(payload["workflow_run"]["run_state"], "waiting_provider", "live claim validation state")
    require_equal(run_path.read_bytes(), before, "live claim validation run immutability")
    return {"reason": payload["reason"], "state": payload["workflow_run"]["run_state"], "immutable": True}


def scenario_resume_after_interrupt(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "resume-interrupt")
    run = harness.run_record(run_id)
    harness.frontdoor.run_lifecycle.transition_run(
        harness.state_root,
        run_id,
        to_state="waiting_provider",
        reason_class="provider_claimed",
        transition="failure_matrix_interrupt",
        principal=harness.frontdoor.default_manual_principal(),
        run=run,
    )
    run["provider_execution"] = {
        "execution_version": "1",
        "step_id": str(run["current_step"]),
        "adapter_id": "claude_headless_p0",
        "work_order_digest": "sha256:" + "1" * 64,
        "adapter_request_digest": "sha256:" + "2" * 64,
        "context_snapshot_digest": "sha256:" + "3" * 64,
        "phase": "invoking",
        "attempt_number": 1,
        "attempt_id": "provider-attempt-interrupted",
        "timeout_seconds": 1800,
        "lease": {
            "lease_id": "provider-lease-interrupted",
            "claimed_by": harness.frontdoor.default_manual_principal(),
            "claimed_at": "2000-01-01T00:00:00+00:00",
            "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
            "lease_expires_at": "2000-01-01T00:00:00+00:00",
        },
        "retry": {
            "last_failure_fingerprint": None,
            "consecutive_failures": 0,
            "auto_retries_used": 0,
            "max_auto_retries": 5,
        },
        "last_outcome": None,
    }
    harness.frontdoor.run_store.store_run(
        harness.state_root,
        run,
        expected_current_state="waiting_provider",
    )
    resumed = harness.frontdoor.resume_run(state_root=harness.state_root, run_id=run_id)
    require_equal(resumed.get("reason"), "provider_lease_expired", "expired lease reason")
    completed = harness.run_step(run_id, fake_provider_mode="success")
    require_equal(completed["workflow_run"]["run_state"], "complete", "interrupt recovery state")
    return {"reason": resumed["reason"], "state": "complete"}


def scenario_lock_contention_is_typed(harness: OrchestratorHarness) -> dict[str, Any]:
    request_id, run_id = "req-lock-contention", "run-lock-contention"
    harness.propose(task_id="TSK-lock-contention", request_id=request_id)
    harness.approve(request_id)
    harness.create_run(request_id, run_id)
    run_path = harness.state_root / "runs" / f"{run_id}.json"
    before = run_path.read_bytes()
    harness.frontdoor.run_lock.acquire_global_lock(
        harness.state_root,
        operation="failure-matrix-prelock",
        run_id=run_id,
        principal=harness.frontdoor.default_manual_principal(),
    )
    try:
        blocked = capture_blocked(lambda: harness.frontdoor.drain_run(state_root=harness.state_root, run_id=run_id))
    finally:
        harness.frontdoor.run_lock.release_global_lock(harness.state_root)
    require_equal(blocked["reason_class"], "lock_contention", "typed lock contention")
    require_equal(run_path.read_bytes(), before, "lock contention run immutability")
    return {"reason_class": "lock_contention", "state_unchanged": True}


def scenario_stale_lock_recovered(harness: OrchestratorHarness) -> dict[str, Any]:
    request_id, run_id = "req-stale-lock", "run-stale-lock"
    harness.propose(task_id="TSK-stale-lock", request_id=request_id)
    harness.approve(request_id)
    harness.create_run(request_id, run_id)
    lock_path = harness.frontdoor.run_lock.global_lock_path(harness.state_root)
    lock_path.mkdir(parents=True, mode=0o700)
    owner = {
        "lock_version": "1",
        "lock_type": "workflow-run-global",
        "pid": 999999999,
        "hostname": harness.frontdoor.run_lock.current_hostname(),
        "process_start_token": None,
        "owner_nonce": "failure-matrix-stale",
        "created_at": "2000-01-01T00:00:00+0000",
        "stale_after_seconds": 1.0,
        "operation": "dead-runner",
        "run_id": run_id,
        "principal_type": "harness_runner",
    }
    harness.frontdoor.run_store.atomic_write_json(lock_path / "owner.json", owner)
    old = time.time() - 3600
    os.utime(lock_path, (old, old))
    drained = harness.frontdoor.drain_run(state_root=harness.state_root, run_id=run_id)
    require_equal(drained["workflow_run"]["run_state"], "step_queued", "stale lock recovery state")
    require(not lock_path.exists(), "recovered lock released")
    return {"state": "step_queued", "lock_released": True}


def scenario_concurrency_one_enforced(harness: OrchestratorHarness) -> dict[str, Any]:
    first = prepare_run(harness, "concurrency-first")
    request_id, second = "req-concurrency-second", "run-concurrency-second"
    harness.propose(task_id="TSK-concurrency-second", request_id=request_id)
    harness.approve(request_id)
    harness.create_run(request_id, second)
    before = (harness.state_root / "runs" / f"{second}.json").read_bytes()
    blocked = capture_blocked(lambda: harness.frontdoor.drain_run(state_root=harness.state_root, run_id=second))
    require_equal(blocked["reason_class"], "concurrency_limit_reached", "P0 concurrency rejection")
    require_equal((harness.state_root / "runs" / f"{second}.json").read_bytes(), before, "second run unchanged")
    require_equal(harness.run_record(first)["run_state"], "step_queued", "first run remains active")
    return {"reason_class": "concurrency_limit_reached", "second_unchanged": True}


def scenario_corrupt_run_is_quarantined(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "corrupt")
    run_path = harness.state_root / "runs" / f"{run_id}.json"
    run_path.write_text("{not-json", encoding="utf-8")
    blocked = capture_blocked(lambda: harness.frontdoor.drain_run(state_root=harness.state_root, run_id=run_id))
    require_equal(blocked["reason_class"], "corrupt_json", "corrupt run reason")
    quarantined = list((harness.state_root / "runs").glob(f"{run_id}.corrupt-*.json"))
    error_path = harness.state_root / "runs" / f"{run_id}.error.json"
    require(bool(quarantined) and error_path.exists(), "corrupt run quarantine artifacts")
    error = json.loads(error_path.read_text(encoding="utf-8"))
    require_equal(error.get("reason_class"), "corrupt_json", "corrupt error class")
    return {"reason_class": "corrupt_json", "quarantined": True}


def scenario_terminal_immutable(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "terminal")
    completed = harness.run_step(run_id, fake_provider_mode="success")
    require_equal(completed["workflow_run"]["run_state"], "complete", "terminal setup")
    run_path = harness.state_root / "runs" / f"{run_id}.json"
    before = run_path.read_bytes()
    resumed = harness.frontdoor.resume_run(state_root=harness.state_root, run_id=run_id)
    aborted = harness.frontdoor.abort_run(state_root=harness.state_root, run_id=run_id, reason="late abort")
    provider = run_provider_mode(harness, run_id, "success")
    require_equal(resumed.get("reason"), "terminal_run_already_set", "terminal resume replay")
    require_equal(aborted.get("reason"), "terminal_run_already_set", "terminal abort replay")
    require_equal(provider.get("reason"), "run_not_runnable", "terminal provider replay")
    require_equal(run_path.read_bytes(), before, "terminal run byte immutability")
    return {"state": "complete", "immutable": True}


def scenario_abort_wins_race(harness: OrchestratorHarness) -> dict[str, Any]:
    run_id = prepare_run(harness, "abort-race")
    aborted = harness.frontdoor.abort_run(state_root=harness.state_root, run_id=run_id, reason="operator cancelled")
    require_equal(aborted["workflow_run"]["run_state"], "aborted", "abort race winner")
    run_path = harness.state_root / "runs" / f"{run_id}.json"
    before = run_path.read_bytes()
    late = run_provider_mode(harness, run_id, "success")
    require_equal(late.get("reason"), "run_not_runnable", "late provider rejected")
    require_equal(late.get("run_state"), "aborted", "late provider sees abort")
    require_equal(run_path.read_bytes(), before, "abort terminal immutability")
    return {"state": "aborted", "late_result": "run_not_runnable"}


def scenario_transcript_never_shared(harness: OrchestratorHarness) -> dict[str, Any]:
    marker = "RAW-PROVIDER-MARKER-94f8a"
    run_id = prepare_run(harness, "raw-marker")
    module = harness.optional_modules["provider_runner"]
    original = module.execute_provider
    module.execute_provider = lambda **_kwargs: (
        "provider_malformed_output",
        None,
        {
            "_raw_stdout": marker.encode("utf-8"),
            "_raw_stderr": b"",
            "_live": True,
            "exit_code": 2,
        },
    )
    try:
        payload = run_provider_mode(harness, run_id, "success")
    finally:
        module.execute_provider = original
    transcript_paths = set((harness.state_root / "provider-evidence" / run_id).rglob("*transcript.json"))
    require(bool(transcript_paths), "raw marker transcripts recorded")
    for transcript in transcript_paths:
        transcript_payload = json.loads(transcript.read_text(encoding="utf-8"))
        decoded_stdout = base64.b64decode(transcript_payload["stdout_base64"]).decode("utf-8")
        require_equal(decoded_stdout, marker, "raw marker confined transcript payload")
    encoded_marker = base64.b64encode(marker.encode("utf-8")).decode("ascii")
    hits = artifact_paths_containing(harness.state_root, marker, exclude=transcript_paths)
    hits += artifact_paths_containing(harness.state_root, encoded_marker, exclude=transcript_paths)
    hits = sorted(set(hits))
    require_equal(hits, [], "raw marker artifact confinement")
    response_without_path = {key: value for key, value in payload.items() if key != "transcript_path"}
    require(marker not in json.dumps(response_without_path, ensure_ascii=False), "raw marker response confinement")
    return {"outcome": payload["reason"], "leaked_artifacts": hits}


def scenario_context_ref_confinement(harness: OrchestratorHarness) -> dict[str, Any]:
    marker = "CONTEXT-BODY-MARKER-c31d"
    ref = harness.make_ref("context-fixture.md", marker + "\n")
    request_id, run_id = "req-context", "run-context"
    harness.propose(task_id="TSK-context", request_id=request_id, refs=[ref])
    approved = harness.approve(request_id)
    harness.create_run(request_id, run_id)
    harness.drain(run_id)
    serialized_activation = json.dumps(approved["activation"], ensure_ascii=False)
    require(marker not in serialized_activation, "context body excluded from activation")
    hits = artifact_paths_containing(harness.state_root, marker)
    require_equal(hits, [], "context body excluded from durable state")
    order = json.loads((harness.state_root / "work-orders" / run_id / "review.json").read_text(encoding="utf-8"))
    refs = order.get("context_refs") or order.get("resolved_context_refs") or []
    require(bool(refs), "work order retains context reference")
    require("sha256:" in json.dumps(order), "work order retains context digest")
    return {"body_shared": False, "reference_retained": True}


def scenario_frontdoor_parity(harness: OrchestratorHarness) -> dict[str, Any]:
    selections: dict[str, dict[str, Any]] = {}
    for frontdoor in ("claude", "codex"):
        request_id = f"req-parity-{frontdoor}"
        harness.propose(
            task_id=f"TSK-parity-{frontdoor}",
            request_id=request_id,
            frontdoor=frontdoor,
            chat_session_id=f"session-{frontdoor}",
        )
        activation = harness.approve(request_id)["activation"]
        selections[frontdoor] = {
            "activation_status": activation["activation_status"],
            "workflow_selection": activation["workflow_selection"],
            "next_action": activation["next_action"],
        }
    require_equal(selections["claude"], selections["codex"], "Claude/Codex parity")
    return {"equal": True, "activation_status": selections["codex"]["activation_status"]}


def scenario_scoped_worker_gateway_boundary(harness: OrchestratorHarness) -> dict[str, Any]:
    bridge = harness.frontdoor.bridge_principal("codex", "failure-matrix")
    derive = capture_blocked(
        lambda: harness.frontdoor.derive_scoped_worker_capability(
            state_root=harness.state_root, run_id="run-not-authorized", step_id="implement", principal=bridge
        )
    )
    execute = capture_blocked(
        lambda: harness.frontdoor.execute_scoped_worker(
            state_root=harness.state_root, capability_id="cap-not-authorized", principal=bridge
        )
    )
    require_equal(
        derive["reason"],
        "derive_scoped_worker_capability requires action gateway executor: main_agent_bridge",
        "derive authority",
    )
    require_equal(
        execute["reason"],
        "execute_scoped_worker requires action gateway executor: main_agent_bridge",
        "execute authority",
    )
    return {"derive_blocked": True, "execute_blocked": True}


SCENARIOS = [
    Scenario("prompt_only_cannot_run", "approval", ("frontdoor.create_run",), {"reason": "approved activation envelope required", "run_created": False}, scenario_prompt_only_cannot_run),
    Scenario("approval_rate_limit", "approval", ("frontdoor.approve_request",), {"reason": "approval challenge rate limit exceeded", "failed_attempts": 3}, scenario_approval_rate_limit),
    Scenario("destructive_blocked", "approval", ("frontdoor.approve_request",), {"run_created": False}, scenario_destructive_blocked),
    Scenario("publication_waits_human", "approval", ("frontdoor.proposed_request",), {"activation_status": "blocked", "reason": "publication_gate_required", "next_action": "ask_human"}, scenario_publication_waits_human),
    Scenario("bridge_cannot_execute", "authority", ("frontdoor.bridge_principal",), {"run_created": False}, scenario_bridge_cannot_execute),
    Scenario("bridge_smuggled_authority", "authority", ("frontdoor.bridge_submit_request",), {"audit_outcome": "blocked"}, scenario_bridge_smuggled_authority),
    Scenario("invalid_report_blocks", "report_integrity", ("frontdoor.validate_report",), {"outcome": "report_invalid", "state": "failed"}, scenario_invalid_report_blocks),
    Scenario("malformed_report_file", "report_integrity", ("frontdoor.validate_report",), {"outcome": "report_invalid", "state": "failed"}, scenario_malformed_report_file),
    Scenario("missing_report_file", "report_integrity", ("frontdoor.validate_report",), {"outcome": "report_not_written", "state": "waiting_human"}, scenario_missing_report_file),
    Scenario("directory_report_path", "report_integrity", ("frontdoor.validate_report",), {"outcome": "report_not_written", "state": "waiting_human", "digest": None}, scenario_directory_report_path),
    Scenario("cross_run_report", "report_integrity", ("frontdoor.validate_report",), {"outcome": "scope_violation", "state": "waiting_human"}, scenario_cross_run_report),
    Scenario("transcript_leak_is_scope_violation", "confinement", ("frontdoor.validate_report",), {"outcome": "scope_violation", "leaked_artifacts": []}, scenario_transcript_leak_is_scope_violation),
    Scenario("provider_failure_matrix", "provider", ("provider_runner.run_provider_step",), {"nonzero": {"reason": "provider_nonzero_exit", "state": "waiting_human"}}, scenario_provider_failure_matrix),
    Scenario("report_not_written", "provider", ("provider_runner.execute_provider",), {"reason": "report_not_written", "state": "waiting_human"}, scenario_report_not_written),
    Scenario("resume_after_timeout", "durability", ("frontdoor.resume_run",), {"state": "complete", "recovered": True}, scenario_resume_after_timeout),
    Scenario("claim_blocks_second_runner", "durability", ("provider_runner.run_provider_step",), {"reason": "provider_in_flight", "state": "waiting_provider"}, scenario_claim_blocks_second_runner),
    Scenario("validate_respects_live_claim", "durability", ("frontdoor.validate_report",), {"reason": "provider_in_flight", "state": "waiting_provider", "immutable": True}, scenario_validate_respects_live_claim),
    Scenario("resume_after_interrupt", "durability", ("frontdoor.resume_run",), {"reason": "provider_lease_expired", "state": "complete"}, scenario_resume_after_interrupt),
    Scenario("lock_contention_is_typed", "locking", ("run_lock.acquire_global_lock",), {"reason_class": "lock_contention", "state_unchanged": True}, scenario_lock_contention_is_typed),
    Scenario("stale_lock_recovered", "locking", ("run_lock.global_lock_path",), {"state": "step_queued", "lock_released": True}, scenario_stale_lock_recovered),
    Scenario("concurrency_one_enforced", "locking", ("run_lock.assert_p0_concurrency",), {"reason_class": "concurrency_limit_reached", "second_unchanged": True}, scenario_concurrency_one_enforced),
    Scenario("corrupt_run_is_quarantined", "durability", ("frontdoor.drain_run",), {"reason_class": "corrupt_json", "quarantined": True}, scenario_corrupt_run_is_quarantined),
    Scenario("terminal_immutable", "durability", ("frontdoor.abort_run",), {"state": "complete", "immutable": True}, scenario_terminal_immutable),
    Scenario("abort_wins_race", "durability", ("frontdoor.abort_run",), {"state": "aborted", "late_result": "run_not_runnable"}, scenario_abort_wins_race),
    Scenario("transcript_never_shared", "confinement", ("provider_runner.execute_provider",), {"outcome": "provider_malformed_output", "leaked_artifacts": []}, scenario_transcript_never_shared),
    Scenario("context_ref_confinement", "confinement", ("frontdoor.proposed_request",), {"body_shared": False, "reference_retained": True}, scenario_context_ref_confinement),
    Scenario("frontdoor_parity", "parity", ("frontdoor.approve_request",), {"equal": True, "activation_status": "approved"}, scenario_frontdoor_parity),
    Scenario("scoped_worker_gateway_boundary", "authority", ("frontdoor.derive_scoped_worker_capability", "frontdoor.execute_scoped_worker"), {"derive_blocked": True, "execute_blocked": True}, scenario_scoped_worker_gateway_boundary),
]


def resolve_requirement(harness: OrchestratorHarness, requirement: str) -> Any:
    root_name, attribute = requirement.split(".", 1)
    roots = {
        "frontdoor": harness.frontdoor,
        "provider_runner": harness.optional_modules.get("provider_runner"),
        "run_lock": getattr(harness.frontdoor, "run_lock", None),
    }
    root = roots.get(root_name)
    require(root is not None, f"required component unavailable: {root_name}")
    value = getattr(root, attribute, None)
    require(callable(value), f"required capability unavailable: {requirement}")
    return value


ORIGINAL_POPEN = subprocess.Popen


def guarded_popen(*args: Any, **kwargs: Any) -> Any:
    command = args[0] if args else kwargs.get("args")
    expected_ps = ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())]
    if command == expected_ps:
        return ORIGINAL_POPEN(*args, **kwargs)
    raise AssertionError(f"external process attempted: args={args!r}, kwargs={kwargs!r}")


def blocked_external_io(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"external I/O attempted: args={args!r}, kwargs={kwargs!r}")


def test_failure_mode_matrix() -> None:
    previous_live = os.environ.get("SAIHAI_ALLOW_LIVE_PROVIDERS")
    os.environ["SAIHAI_ALLOW_LIVE_PROVIDERS"] = ""
    try:
        for scenario in SCENARIOS:
            with tempfile.TemporaryDirectory() as raw_tmp:
                with OrchestratorHarness(Path(raw_tmp)) as harness:
                    for requirement in scenario.requires:
                        resolve_requirement(harness, requirement)
                    try:
                        with ExitStack() as guards:
                            guards.enter_context(patch.object(subprocess, "Popen", guarded_popen))
                            guards.enter_context(patch.object(socket, "socket", blocked_external_io))
                            guards.enter_context(patch.object(socket, "create_connection", blocked_external_io))
                            guards.enter_context(patch.object(urllib.request, "urlopen", blocked_external_io))
                            result = scenario.run(harness)
                        require_subset(result, scenario.expect, scenario.scenario_id)
                    except Exception as exc:
                        if isinstance(exc, HarnessAssertion):
                            raise AssertionError(f"{scenario.scenario_id}: {exc}") from exc
                        raise AssertionError(f"{scenario.scenario_id}: {exc}") from exc
    finally:
        if previous_live is None:
            os.environ.pop("SAIHAI_ALLOW_LIVE_PROVIDERS", None)
        else:
            os.environ["SAIHAI_ALLOW_LIVE_PROVIDERS"] = previous_live


def main() -> None:
    test_failure_mode_matrix()
    print(
        json.dumps(
            {
                "result": "pass",
                "cases": len(SCENARIOS),
                "skipped": [],
                "external_io": "mechanically_blocked",
                "scenario_ids": [scenario.scenario_id for scenario in SCENARIOS],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
