#!/usr/bin/env python3
"""Invocation-bounded consumer of existing readonly frontdoor transitions."""
from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from typing import Any

import run_lock
import run_store


def drive_run(*, state_root: Path, run_id: str = "", request_id: str = "",
              principal: dict[str, Any], adapter_id: str = "claude_headless_p0",
              timeout_seconds: int = 120, fake_provider_mode: str = "", live: bool = False,
              max_iterations: int = 32, duration_seconds: float = 300) -> dict[str, Any]:
    # Import lazily: the public facade also exposes this consumer.
    import frontdoor_orchestrator as api

    if bool(run_id) == bool(request_id):
        raise api.FrontdoorError("drive_selector_required")
    if (type(max_iterations) is not int or not 1 <= max_iterations <= 256
            or isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 3600
            or type(timeout_seconds) is not int or timeout_seconds < 1):
        raise api.FrontdoorError("drive_bounds_invalid")
    api.validate_artifact_id(run_id or request_id, "run_id" if run_id else "request_id")
    api.precheck_execution_principal(state_root=state_root, principal=principal,
                                    transition="drive_run", subject={"run_id": run_id, "request_id": request_id})
    started = time.monotonic()
    invocation = "drive-" + uuid.uuid4().hex
    iterations = progress = 0
    current: dict[str, Any] = {}

    def audit(action: str, outcome: str, reason: str = "") -> None:
        api.append_audit_event(state_root=state_root, event_type="drive_iteration", principal=principal,
            subject={"run_id": run_id, "request_id": request_id}, outcome=outcome,
            details={"invocation_id": invocation, "iteration": iterations, "action": action,
                     "reason_class": reason, "run_state": current.get("run_state", ""),
                     "step_id": current.get("current_step", ""), "progress_count": progress})

    def stop(kind: str, reason: str) -> dict[str, Any]:
        audit("stop", kind, reason)
        return {"schema_version": 1, "decision": "ok" if kind == "terminal" else "blocked",
                "stop": kind, "reason_class": reason, "run_id": run_id,
                "run_state": current.get("run_state", ""), "current_step": current.get("current_step", ""),
                "iterations": iterations, "progress_count": progress, "invocation_id": invocation}

    def identity(run: dict[str, Any]) -> tuple[Any, ...]:
        execution = run.get("provider_execution") or {}
        return (run.get("run_state"), run.get("current_step"), run.get("iteration"),
                len(run.get("transitions", [])), execution.get("attempt_id"), execution.get("phase"))

    # The existing request binding is checked again by create_run under its lock.
    if request_id:
        record = api.read_json(api.request_path(state_root, request_id))
        run_id = str(record.get("run_id") or "")
        audit("create_run", "started")
        created = api.create_run(state_root=state_root, request_id=request_id, run_id=run_id,
                                 resume_policy="manual", principal=principal)
        run_id = created["workflow_run"]["run_id"]
        audit("create_run", "ok")

    while iterations < max_iterations:
        current = run_store.load_run(state_root, run_id)
        request_id = str(current.get("request_id") or "")
        state = current.get("run_state")
        if state in run_store.TERMINAL_RUN_STATES:
            return stop("terminal", str(state))
        if state == "waiting_human":
            return stop("waiting_human", "waiting_human")
        if state == "remediating" or current.get("review_lifecycle"):
            return stop("integration_pending", "review_fix_integration_pending")
        if current.get("current_step") == "publication_gate":
            return stop("waiting_human", "published_human_gate")
        if current.get("workflow_id") != "readonly_review_chain":
            return stop("unsupported", "unsupported_workflow")
        remaining = duration_seconds - (time.monotonic() - started)
        if remaining < 1:
            return stop("bounded", "duration_exhausted")
        if state in {"created", "step_queued"}:
            order = api.work_order_path(state_root, run_id, str(current.get("current_step") or ""))
            if state == "created" or not api.state_file_exists(order):
                action = "drain"
            elif current.get("current_step") in {"research", "review"}:
                action = "run_provider"
            elif current.get("current_step") == "final_evidence":
                action = "run_harness_gate"
            else:
                return stop("unsupported", "unsupported_step")
        elif state == "waiting_provider":
            action = "run_provider"
        elif state == "validating":
            action = "validate_report"
        else:
            return stop("unsupported", "unsupported_state")
        before = identity(current)
        iterations += 1
        audit(action, "started")
        try:
            if action == "drain":
                result = api.drain_run(state_root=state_root, run_id=run_id, principal=principal)
            elif action == "run_provider":
                result = api.run_provider(state_root=state_root, run_id=run_id, principal=principal,
                    adapter_id=adapter_id, timeout_seconds=min(timeout_seconds, int(remaining)),
                    fake_provider_mode=fake_provider_mode, live=live, return_on_retry=True)
            elif action == "run_harness_gate":
                result = api.run_harness_gate(state_root=state_root, run_id=run_id, principal=principal)
            else:
                # The existing gate resolves the canonical report and verifies it.
                result = api.validate_report(state_root=state_root, run_id=run_id, principal=principal)
        except run_lock.LockContentionError:
            audit(action, "blocked", "lock_contention")
            return stop("blocked", "lock_contention")
        except api.FrontdoorError:
            # Exception text can contain paths or private diagnostics. Do not echo it.
            audit(action, "blocked", "transition_rejected")
            return stop("blocked", "transition_rejected")
        current = run_store.load_run(state_root, run_id)
        changed = identity(current) != before
        # Issuing a work order can progress without changing the run state.
        changed = changed or (action == "drain" and result.get("drained") is True)
        progress += int(changed)
        reason = str(result.get("reason_class") or result.get("reason") or "")
        # Only established machine tokens enter the public audit, never free text.
        reason = reason if reason and len(reason) <= 96 and all(c.isalnum() or c == "_" for c in reason) else "transition_result"
        audit(action, "progress" if changed else "unchanged", reason)
        if current.get("run_state") in run_store.TERMINAL_RUN_STATES:
            return stop("terminal", str(current["run_state"]))
        if current.get("run_state") == "waiting_human":
            return stop("waiting_human", reason)
        if reason == "provider_in_flight":
            return stop("waiting_provider", reason)
        if result.get("decision") != "ok":
            return stop("blocked", reason)
        if not changed:
            return stop("bounded", "no_progress")
    current = run_store.load_run(state_root, run_id)
    return stop("bounded", "iteration_exhausted")
