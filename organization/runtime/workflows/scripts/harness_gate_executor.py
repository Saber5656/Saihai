"""Deterministic, harness-owned final evidence producer; no provider dispatch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import report_gate
import run_lifecycle
import run_lock
import run_store
import work_order_builder
import workflow_selector


def build_final_evidence_report(verified_inputs: dict[str, Any]) -> dict[str, Any]:
    research, review = verified_inputs["prior_acceptances"]
    return {
        "report_version": "1", "workflow_id": "readonly_review_chain",
        "step_id": "final_evidence", "result": "complete",
        "research_report_ref": research["report_path"], "review_report_ref": review["report_path"],
        "review_status": "pass", "validation_status": "passed",
        "evidence_refs": [research["report_path"], review["report_path"]], "no_diff_completion": True,
    }


def execute_harness_gate(*, state_root: Path, run_id: str,
                         principal: dict[str, Any]) -> dict[str, Any]:
    """Create exact report bytes, release the producer lock, then re-enter the gate."""
    try:
        run_id = run_store.validate_artifact_id(run_id, "run_id")
        report_gate.precheck_execution_principal(state_root=state_root, principal=principal,
            transition="run_harness_gate", subject={"run_id": run_id})
        with run_lock.hold_global_lock(state_root, operation="run_harness_gate", run_id=run_id,
                                       principal=principal):
            run = run_store.load_run(state_root, run_id)
            if run.get("workflow_id") != report_gate.CHAIN_ID or run.get("current_step") != "final_evidence":
                raise report_gate.ReportGateError("unsupported_harness_gate")
            if any(h.get("status") == "accepted" and h.get("step_id") == "final_evidence"
                   for h in run.get("step_history", [])):
                raise report_gate.ReportGateError("duplicate_step_report")
            if run.get("run_state") != "step_queued":
                raise report_gate.ReportGateError("out_of_order_report")
            report_gate.require_existing_signing_key(state_root, principal)
            template = workflow_selector.load_template(run["workflow_id"])
            verified = report_gate.verify_readonly_final_inputs(state_root, run=run, template=template)
            report = build_final_evidence_report(verified)
            schema = json.loads((workflow_selector.SCHEMA_ROOT / "readonly-final-evidence-report.schema.json").read_text())
            if work_order_builder._validate_schema_fragment(report, schema, "$"):
                raise report_gate.ReportGateError("final_report_invalid")
            path = Path(verified["report_path"])
            payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            if not run_store.create_private_file(path, payload) and run_store.read_bytes(path) != payload:
                raise report_gate.ReportGateError("final_report_conflict")
        # The gate reacquires the lock and validates current artifacts, never a
        # cached producer decision. A crash here leaves only a recoverable report.
        return report_gate.gate_report(state_root, run_id, report_path_arg=str(path), principal=principal)
    except (report_gate.ReportGateError, run_store.RunStoreError, run_lifecycle.LifecycleError,
            work_order_builder.WorkOrderError, run_lock.LockContentionError,
            OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, report_gate.ReportGateError) else "harness_gate_input_invalid: " + type(exc).__name__
        if isinstance(exc, run_lock.LockContentionError): reason = "lock_contention"
        return {"schema_version": 1, "decision": "blocked", "validated": False,
                "reason": reason.split(":", 1)[0], "outcome": reason.split(":", 1)[0], "errors": [reason]}
