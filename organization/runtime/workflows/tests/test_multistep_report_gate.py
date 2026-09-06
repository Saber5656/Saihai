#!/usr/bin/env python3
"""Actual report-gate chain tests; fake artifacts do not prove live producers."""

from __future__ import annotations

import json
import copy
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from test_frontdoor_orchestrator import (
    external_review_classification,
    load_payload,
    run_frontdoor as _run_frontdoor,
    external_review_report,
)
import report_gate
import workflow_selector
import run_store
import provider_runner
import work_order_builder
import run_lifecycle
import run_lock
import scoped_worker_executor


def run_frontdoor(*args, **kwargs):
    # Consumer tests own a fake host. Production deliberately blocks activation
    # until the real chain runtime exists; substitute only that readiness result,
    # not approvals, provider claims, signatures or gate-owned state transitions.
    import test_frontdoor_orchestrator as fixture
    readiness = """
import workflow_selector as selector
original_candidate = selector.validate_workflow_candidate
def fake_host_candidate(workflow_id, classification, registry=None):
    result = original_candidate(workflow_id, classification, registry)
    if workflow_id != "readonly_review_chain" or result.get("reason") != "readonly_chain_runtime_unavailable":
        return result
    template = selector.load_template(workflow_id, registry)
    gates = selector.required_gates_for_candidate(template, classification, False)
    required = selector.required_artifacts_for_candidate(template, classification, False, gates)
    if not required.issubset(set(classification.get("expected_artifacts") or [])):
        return result
    return selector.selected_workflow(workflow_id, template["initial_step"], [],
        safety_class=template["safety_class"], required_safety=selector.required_safety_class(classification),
        publication_gate_required=False, required_gates=gates)
selector.validate_workflow_candidate = fake_host_candidate
"""
    wrapper = fixture.FRONTDOOR_TEST_WRAPPER.replace("frontdoor.main()", readiness + "\nfrontdoor.main()")
    with mock.patch.object(fixture, "FRONTDOOR_TEST_WRAPPER", wrapper):
        return _run_frontdoor(*args, **kwargs)


class MultistepReportGateTests(unittest.TestCase):
    def test_prior_transition_signature_and_states_are_rechecked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            actor = {"principal_type": "harness_runner", "principal_id": "fixture-host", "authn_method": "local_cli"}
            acceptance = {"run_id": "unit-run", "on": "research_complete", "to_step": "review"}
            record = {"transition": "validate_report", "run_id": "unit-run", "from_state": "validating",
                      "to_state": "step_queued", "reason_class": "research_complete",
                      "report_binding": acceptance, "principal": actor}
            record["signature"] = run_lifecycle.sign_transition(state_root=root, principal=actor,
                transition="validate_report", subject=record)
            report_gate._verify_chain_transition(root, record, acceptance)
            for field, value in (("from_state", "created"), ("to_state", "complete"), ("reason_class", "forged"),
                                 ("signature", {}), ("run_id", "another-run")):
                with self.subTest(field=field), self.assertRaises(report_gate.ReportGateError):
                    report_gate._verify_chain_transition(root, {**record, field: value}, acceptance)

    def test_changed_gate_and_role_contract_is_blocked(self):
        run = {"activation": {"activation_status": "approved", "workflow_selection": {"workflow_id": "readonly_review_chain"}}}
        template = workflow_selector.load_template("readonly_review_chain")
        self.assertEqual(3, len(report_gate._chain_contract(template, run)))
        mutations = [
            lambda t: t["steps"][0]["quality_gates"][0].update(requires="unimplemented_requirement"),
            lambda t: t["steps"][0].update(role="unimplemented_role"),
            lambda t: t["steps"][0]["provider_route"].update(transition_authority="provider"),
            lambda t: t["steps"][0]["allowed_ops"].update(edit=0),
            lambda t: t["steps"][0].update(permission_mode="edit"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(template)
                mutate(changed)
                with self.assertRaises(report_gate.ReportGateError):
                    report_gate._chain_contract(changed, run)

    def test_declared_transition_targets_and_budgets(self):
        template = workflow_selector.load_template("readonly_review_chain")
        def choose(value, event="research_complete", accepted=(), budget=3):
            return report_gate.resolve_step_transition(
                value, "research", event, accepted_steps=list(accepted), step_budget=budget,
            )
        self.assertEqual("review", choose(template)["to"])
        for label, mutate, reason in [
            ("missing", lambda t: t["steps"][0].update(transitions=[]), "undeclared_transition_event"),
            ("duplicate", lambda t: t["steps"][0]["transitions"].append({"on": "research_complete", "to": "review"}), "duplicate_transition_event"),
            ("unknown", lambda t: t["steps"][0]["transitions"][0].update(to="unknown"), "unknown_transition_target"),
            ("premature", lambda t: t["steps"][0]["transitions"][0].update(to="complete"), "intermediate_terminal_transition"),
            ("cycle", lambda t: t["steps"][0]["transitions"][0].update(to="research"), "cyclic_step_transition"),
        ]:
            with self.subTest(label=label):
                changed = copy.deepcopy(template)
                mutate(changed)
                with self.assertRaisesRegex(report_gate.ReportGateError, reason):
                    choose(changed)
        for budget in (0, 1, True, "3"):
            with self.subTest(budget=budget), self.assertRaisesRegex(report_gate.ReportGateError, "step_budget_exceeded"):
                choose(template, budget=budget)
        with self.assertRaisesRegex(report_gate.ReportGateError, "duplicate_step_report"):
            choose(template, accepted=("research",))
        changed = copy.deepcopy(template)
        changed["steps"][0]["transitions"][0]["to"] = "waiting_human"
        self.assertEqual("waiting_human", choose(changed)["to"])

    def setup_chain(self, root):
        classification = external_review_classification(
            task_kind="research", expected_artifacts=["research_report", "typed_report", "final_evidence"],
        )
        proposed = load_payload(run_frontdoor(
            root, "propose", "--task-id", "TSK-chain", "--request-id", "req-chain",
            "--prompt", "Research and independently review bounded evidence",
            "--classification", json.dumps(classification),
            "--ref", "organization/runtime/workflows/README.md",
        ))
        load_payload(run_frontdoor(root, "approve", "--request-id", "req-chain", "--human-action-id",
                                  proposed["approval"]["human_action_id"]))
        created = load_payload(run_frontdoor(root, "create-run", "--request-id", "req-chain", "--run-id", "run-chain"))
        self.assertEqual("research", created["workflow_run"]["current_step"])
        self.drain(root)

    def drain(self, root):
        drained = load_payload(run_frontdoor(root, "drain", "--run-id", "run-chain", check=False))
        self.assertEqual("ok", drained["decision"], drained)
        self.assertEqual("step_queued", drained["workflow_run"]["run_state"])
        return drained

    def claim_attempt(self, root):
        actor = {"principal_type": "harness_runner", "principal_id": "fixture-host", "authn_method": "local_cli"}
        provider_runner.precheck_execution_principal(state_root=root, principal=actor, subject={"run_id": "run-chain"})
        with run_lock.hold_global_lock(root, operation="claim_provider_attempt", run_id="run-chain", principal=actor):
            run = run_store.load_run(root, "run-chain")
            self.assertEqual("step_queued", run["run_state"])
            step = run["current_step"]
            self.assertIn(step, ("research", "review"))
            order, digest, snapshot = scoped_worker_executor.verify_frozen_work_order(
                root, run_id="run-chain", step_id=step, expected_run_states={"step_queued"},
                expected_iteration=run["iteration"],
            )
            self.assertFalse(run_lifecycle.provider_claim_is_live(run, order))
            self.assertEqual("readonly", order["permission_mode"])
            self.assertTrue(all(v is False for v in order["activation_scope"]["allowed_ops"].values()))
            adapter = provider_runner.load_provider_adapters()[order["provider_adapter_id"]]
            self.assertEqual([], provider_runner.validate_adapter_descriptor(adapter))
            self.assertTrue(provider_runner.provider_adapter_model_binding_matches(order, adapter))
            template = workflow_selector.load_template("readonly_review_chain")
            contract = next(s for s in template["steps"] if s["id"] == step)
            self.assertEqual([], work_order_builder.validate_work_order(order, template=template, step=contract, state_root=root, run=run))
            attempt = f"fixture-{step}-{run['iteration']}-{len(run['transitions'])}"
            lease = "lease-" + attempt
            request = provider_runner.adapter_request(
                state_root=root, run=run, work_order=order, adapter=adapter, principal=actor,
                work_order_digest=digest, snapshot_path=snapshot, attempt_id=attempt, lease_id=lease,
                timeout_seconds=30, execution_binding={},
            )
            execution = provider_runner.build_provider_execution(
                run=run, adapter_id=adapter["provider_adapter_id"], work_order_digest=digest,
                request=request, attempt_id=attempt, lease_id=lease, timeout_seconds=30, principal=actor,
            )
            request_path = provider_runner.adapter_request_path(root, "run-chain", step, adapter["provider_adapter_id"])
            provider_runner.private_atomic_write_json(root, request_path, request)
            provider_runner.verified_request_artifact_paths(state_root=root, run_id="run-chain", step_id=step, request=request)
            # Authorized fake host composition mirrors the existing claim/store
            # path. It never repairs current_step, iteration, or accepted history.
            run["provider_execution"] = execution
            run_lifecycle.transition_run(root, "run-chain", to_state="waiting_provider", reason_class="provider_claimed",
                transition="run_provider", principal=actor, run=run, expected_current_state="step_queued",
                artifact_refs=[str(report_gate.work_order_path(root, "run-chain", step)), str(snapshot), str(request_path)])
        provider_runner.authorize_provider_dispatch(state_root=root, run_id="run-chain", request=request,
            attempt_id=attempt, lease_id=lease, principal=actor)
        return request, adapter, actor

    def promote_fake_result(self, root, request, adapter, actor, report, *, transcript_kind="signal"):
        result_path, transcript_path = provider_runner.provider_attempt_paths(root, "run-chain", request["attempt_id"])
        if transcript_kind == "live-format":
            # Exercise the existing raw transcript writer with deterministic bytes;
            # this is format compatibility, never a live provider invocation.
            provider_runner.write_live_transcript(root, transcript_path, stdout=b"fixture output",
                stderr=b"", outcome="ok", exit_code=0)
        else:
            provider_runner.write_signal_transcript(root, transcript_path,
                {"fixture": True, "provider_session_id": "fixture-" + request["step_id"], "live_provider": False})
        details = {"effective_model": request["intended_model"], "provider_session_id": "fixture-" + request["step_id"],
                   "provider_request_id": request["attempt_id"], "duration_ms": 0, "usage": {},
                   "transcript_sha256": report_gate.file_sha256(transcript_path)}
        journal = {"attempt_result_version": "1", "attempt_id": request["attempt_id"], "lease_id": request["lease_id"],
                   "work_order_digest": request["work_order_digest"], "adapter_request_digest": request["adapter_request_digest"],
                   "context_snapshot_digest": request["context_snapshot_digest"], "adapter_id": adapter["provider_adapter_id"],
                   "outcome": "ok", "report": report, "details": details, "transcript_path": str(transcript_path),
                   "transcript_sha256": details["transcript_sha256"], "recorded_at": provider_runner.now_iso()}
        provider_runner.private_atomic_write_json(root, result_path, journal)
        with run_lock.hold_global_lock(root, operation="finalize_provider_attempt", run_id="run-chain", principal=actor):
            run = run_store.load_run(root, "run-chain")
            self.assertEqual("waiting_provider", run["run_state"])
            self.assertEqual(request["step_id"], run["current_step"])
            self.assertEqual(request["attempt_id"], run["provider_execution"]["attempt_id"])
            self.assertEqual(request["lease_id"], run["provider_execution"]["lease"]["lease_id"])
            recovered = provider_runner.recover_completed_provider_attempt(state_root=root, run=run, adapter=adapter)
            self.assertIsNotNone(recovered)
            self.assertEqual("ok", recovered["outcome"])
            self.assertEqual("result_ready", run["provider_execution"]["phase"])
            run_store.store_run(root, run, expected_current_state="waiting_provider")
        return Path(request["report_path"])

    def fake_produce(self, root, *, report_overrides=None, transcript_kind="signal"):
        # Test-owned fake host registers real host lifecycle authority before
        # producing an attempt journal; actual recovery promotes canonical output.
        # No live binary/provider execution or direct step/history advancement.
        run = run_store.load_run(root, "run-chain")
        step = run["current_step"]
        if step == "final_evidence":
            refs = [str(report_gate.report_path(root, "run-chain", sid)) for sid in ("research", "review")]
            report = {"report_version": "1", "workflow_id": "readonly_review_chain", "step_id": step,
                      "result": "complete", "research_report_ref": refs[0], "review_report_ref": refs[1],
                      "review_status": "pass", "validation_status": "passed", "evidence_refs": refs,
                      "no_diff_completion": True}
            path = report_gate.report_path(root, "run-chain", step)
            run_store.atomic_write_json(path, {**report, **(report_overrides or {})})
            return path
        request, adapter, actor = self.claim_attempt(root)
        if step == "research":
            report = {"report_version": "1", "workflow_id": "readonly_review_chain", "step_id": step,
                      "result": "findings", "source_refs": ["organization/runtime/workflows/README.md"],
                      "findings": [{"summary": "Fixture research", "evidence_refs": ["organization/runtime/workflows/README.md"]}],
                      "uncertainty": ["Fake producer; no live dispatch"], "no_diff_completion": True}
        else:
            report = external_review_report({**request, "evidence_contract": {"fixed_fields": {
                "intended_model": request["intended_model"],
                "model_assurance": report_gate.MODEL_ASSURANCE_FOR_POLICY[adapter["effective_model_policy"]]}}},
                request_id="req-chain", run_id="run-chain")
            report["workflow_id"] = "readonly_review_chain"
            report["provider_evidence"]["provider_session_id"] = "fixture-" + step
        report.update(report_overrides or {})
        return self.promote_fake_result(root, request, adapter, actor, report, transcript_kind=transcript_kind)

    def submit(self, root, path=None):
        args = ["validate-report", "--run-id", "run-chain"]
        if path is not None:
            args += ["--report-path", str(path)]
        return load_payload(run_frontdoor(root, *args, check=False))

    def test_real_chain_setup_generates_research_work_order(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            order = report_gate.read_json(report_gate.work_order_path(root, "run-chain", "research"))
            self.assertIs(True, order["external_provider_allowed"])

    def test_actual_gate_three_step_e2e(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            for step, target, event in (("research", "review", "research_complete"), ("review", "final_evidence", "review_complete"), ("final_evidence", "complete", "final_evidence_valid")):
                before = run_store.load_run(root, "run-chain")
                self.assertEqual(step, before["current_step"])
                path = self.fake_produce(root)
                after_production = run_store.load_run(root, "run-chain")
                for field in ("current_step", "iteration", "step_history"):
                    self.assertEqual(before[field], after_production[field], "only gate advances " + field)
                result = self.submit(root, path)
                self.assertEqual("ok", result["decision"], result)
                run = result["workflow_run"]
                self.assertEqual(event, result["outcome"])
                self.assertEqual(step, run["step_history"][-1]["step_id"])
                if target != "complete":
                    self.assertEqual(target, run["current_step"])
                    self.assertEqual("step_queued", run["run_state"])
                    self.assertIsNone(run["terminal"]["status"])
                    self.assertEqual("drain", result["next_action"])
                    self.assertFalse(report_gate.work_order_path(root, "run-chain", target).exists())
                    self.drain(root)
                    self.assertEqual(run, run_store.load_run(root, "run-chain"), "drain generates order only")
                else:
                    self.assertEqual("complete", run["run_state"])
                    self.assertEqual("complete", run["terminal"]["status"])
            rejected = self.submit(root, path)
            self.assertEqual("duplicate_step_report", rejected["reason"])
            self.assertEqual(run, run_store.load_run(root, "run-chain"))


    def test_final_evidence_ignores_completed_previous_claim(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.advance_to(root, "final_evidence")
            run = run_store.load_run(root, "run-chain")
            # Match run_provider's post-gate completion bookkeeping. This changes
            # only retained provider metadata, never the gate-owned step/history.
            run["provider_execution"]["phase"] = "completed"
            run["provider_execution"]["lease"]["lease_expires_at"] = "2099-01-01T00:00:00+00:00"
            run_store.store_run(root, run, expected_current_state="step_queued")
            result = self.submit(root, self.fake_produce(root))
            self.assertEqual("ok", result["decision"], result)
            self.assertEqual("complete", result["workflow_run"]["run_state"])

    def advance_to(self, root, target):
        self.setup_chain(root)
        for step in ("research", "review"):
            if step == target:
                return
            path = self.fake_produce(root)
            result = self.submit(root, path)
            self.assertEqual("ok", result["decision"], result)
            self.drain(root)

    def assert_rejected_without_acceptance(self, root, path=None):
        before = run_store.load_run(root, "run-chain")
        result = self.submit(root, path)
        self.assertEqual("blocked", result["decision"], result)
        after = run_store.load_run(root, "run-chain")
        self.assertEqual(before["current_step"], after["current_step"])
        self.assertEqual(before["step_history"], after["step_history"])
        return result

    def test_out_of_order_duplicate_and_wrong_run_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            for sid in ("review", "final_evidence"):
                path = report_gate.report_path(root, "run-chain", sid)
                result = self.assert_rejected_without_acceptance(root, path)
                self.assertEqual("out_of_order_report", result["reason"], result)
            other = report_gate.report_path(root, "another-run", "research")
            self.assertEqual("step_report_mismatch", self.assert_rejected_without_acceptance(root, other)["reason"])
            research = self.fake_produce(root)
            self.assertEqual("ok", self.submit(root, research)["decision"])
            self.assertEqual("duplicate_step_report", self.assert_rejected_without_acceptance(root, research)["reason"])

    def test_research_report_payload_rejections(self):
        variants = [
            ({"step_id": "review"}, "step_report_mismatch"),
            ({"workflow_id": "another-workflow"}, "step_report_mismatch"),
            ({"run_id": "another-run"}, "report_invalid"),
            ({"request_id": "req-forged"}, "report_invalid"),
            ({"provider_evidence": {}}, "report_invalid"),
            ({"no_diff_completion": 1}, "report_invalid"),
            ({"result": "blocked"}, "report_invalid"),
            ({"source_refs": []}, "report_invalid"),
            ({"findings": []}, "report_invalid"),
            ({"raw_transcript": "forbidden"}, "report_invalid"),
            ({"source_refs": ["../../unapproved/private-source"]}, "report_invalid"),
        ]
        for overrides, reason in variants:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                # Invalid producer input is promoted consistently into the journal
                # and report so evidence tamper checks cannot mask this contract.
                path = self.fake_produce(root, report_overrides=overrides)
                result = self.assert_rejected_without_acceptance(root, path)
                self.assertEqual(reason, result["reason"], result)
                if reason == "report_invalid":
                    if overrides == {"result": "blocked"}:
                        self.assertEqual([], result["errors"], result)
                    else:
                        self.assertTrue(result["errors"], result)
                if "no_diff_completion" in overrides:
                    self.assertIn("no_diff_completion must be boolean true", result["errors"])
                if "findings" in overrides:
                    self.assertTrue(any("min_items" in e for e in result["errors"]), result)
                if overrides.get("source_refs") == ["../../unapproved/private-source"]:
                    self.assertIn("source_refs outside bounded work order", result["errors"])

    def test_promoted_artifacts_must_match_attempt_result(self):
        for variant in ("session", "transcript", "provider_request_id", "duration"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                report = self.fake_produce(root)
                path = report_gate.provider_evidence_path(root, "run-chain", "research")
                evidence = report_gate.read_json(path)
                if variant == "session":
                    evidence["provider_session_id"] = "forged-session"
                elif variant == "transcript":
                    transcript = report_gate.provider_transcript_path(root, "run-chain", "research")
                    run_store.atomic_write_json(transcript, {"forged": "different provider output"})
                    evidence["transcript_sha256"] = report_gate.file_sha256(transcript)
                elif variant == "provider_request_id":
                    evidence["provider_request_id"] = "forged-request"
                else:
                    evidence["duration_ms"] = 999
                run_store.atomic_write_json(path, evidence)
                result = self.assert_rejected_without_acceptance(root, report)
                self.assertEqual("provider_result_promotion_mismatch", result["reason"], result)

    def test_existing_finalize_transcript_formats_match_journal(self):
        for kind in ("signal", "live-format"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                report = self.fake_produce(root, transcript_kind=kind)
                request = report_gate.read_json(provider_runner.adapter_request_path(
                    root, "run-chain", "research", "claude_headless_p0"))
                result_path, _ = provider_runner.provider_attempt_paths(root, "run-chain", request["attempt_id"])
                journal = report_gate.read_json(result_path)
                transcript = Path(request["transcript_path"])
                before = run_store.load_run(root, "run-chain")
                # Existing normal-finalize writers regenerate timestamps. Exercise
                # both representations without invoking a provider or editing run.
                with mock.patch.object(provider_runner, "now_iso", return_value="2026-09-05T23:59:59+0900"):
                    if kind == "signal":
                        provider_runner.write_signal_transcript(root, transcript,
                            {"outcome": journal["outcome"], "details": journal["details"]})
                    else:
                        provider_runner.write_live_transcript(root, transcript,
                            stdout=b"fixture output", stderr=b"", outcome="ok", exit_code=0)
                details = {**journal["details"], "transcript_sha256": report_gate.file_sha256(transcript)}
                if kind == "live-format":
                    payload = report_gate.read_json(transcript)
                    details.update(stdout_sha256=payload["stdout_sha256"], stderr_sha256=payload["stderr_sha256"])
                evidence = provider_runner.normalized_evidence(request=request, adapter=request["adapter"],
                    report=journal["report"], outcome=journal["outcome"], details=details)
                provider_runner.private_atomic_write_json(root, Path(request["evidence_path"]), evidence)
                self.assertEqual(before, run_store.load_run(root, "run-chain"))
                self.assertEqual("ok", self.submit(root, report)["decision"])

    def test_canonical_run_store_failure_leaves_no_acceptance(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            report = self.fake_produce(root)
            before = run_store.load_run(root, "run-chain")
            with mock.patch.object(run_store, "store_run", side_effect=OSError("simulated store failure")):
                result = report_gate.gate_report(root, "run-chain")
            self.assertEqual("blocked", result["decision"], result)
            self.assertEqual(before, run_store.load_run(root, "run-chain"))
            # A prewritten transition artifact is not canonical acceptance.
            self.assertEqual("ok", self.submit(root, report)["decision"])

    def test_attempt_evidence_order_and_snapshot_mismatches(self):
        variants = [
            ("evidence", "attempt_id", "late-attempt"), ("evidence", "run_id", "other-run"),
            ("evidence", "step_id", "review"), ("evidence", "effective_model", "wrong-model"),
            ("evidence", "effective_model_policy", "record_without_equality"),
            ("evidence", "transcript_sha256", "sha256:" + "0" * 64),
            ("request", "attempt_id", "stale-attempt"), ("request", "adapter_request_digest", "sha256:" + "0" * 64),
            ("request", "work_order_snapshot_path", "missing"),
            ("request", "approved_context", []),
            ("order", "to_role", "git-publisher"), ("order", "permission_mode", "edit"),
            ("snapshot", "iteration", 2), ("snapshot", "work_order_digest", "sha256:" + "0" * 64),
        ]
        for kind, field, replacement in variants:
            with self.subTest(kind=kind, field=field), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                report = self.fake_produce(root)
                paths = {
                    "evidence": report_gate.provider_evidence_path(root, "run-chain", "research"),
                    "request": provider_runner.adapter_request_path(root, "run-chain", "research", "claude_headless_p0"),
                    "order": report_gate.work_order_path(root, "run-chain", "research"),
                    "snapshot": work_order_builder.snapshot_path(root, "run-chain", "research", 1),
                }
                path = paths[kind]
                value = report_gate.read_json(path)
                value[field] = replacement
                run_store.atomic_write_json(path, value)
                self.assert_rejected_without_acceptance(root, report)

    def test_review_nonpass_and_numeric_authority_are_rejected(self):
        finding = {"finding_id": "QA1", "severity": "low", "status": "open", "summary": "Valid finding",
                   "evidence_refs": ["organization/runtime/workflows/README.md"]}
        for outcome in ("findings", "blocked", "invalid", "numeric-authority"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.advance_to(root, "review")
                if outcome == "numeric-authority":
                    overrides = {"authority": {"canonical_result": "typed_report_file",
                        "stdout_is_signal_only": 1, "raw_transcript_shared": False}}
                else:
                    overrides = {"result": outcome}
                    if outcome == "findings":
                        overrides["findings"] = [finding]
                path = self.fake_produce(root, report_overrides=overrides)
                result = self.assert_rejected_without_acceptance(root, path)
                self.assertEqual("review_blocked", result["reason"], result)
                if outcome == "findings":
                    self.assertEqual([], result["errors"], result)  # Valid shape, pass-only policy rejects.
                if outcome == "numeric-authority":
                    self.assertIn("review authority must contain strict booleans", result["errors"])

    def test_final_status_and_reference_rejections(self):
        variants = [("result", "blocked"), ("review_status", "findings"), ("review_status", "blocked"),
                    ("review_status", "invalid"), ("validation_status", "failed"),
                    ("validation_status", "not_run"), ("no_diff_completion", 1),
                    ("research_report_ref", "../other-run/report.json"), ("review_report_ref", ""),
                    ("evidence_refs", []), ("research_report_ref", None)]
        for field, replacement in variants:
            with self.subTest(field=field, value=replacement), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.advance_to(root, "final_evidence")
                path = self.fake_produce(root)
                value = report_gate.read_json(path)
                if replacement is None:
                    del value[field]
                else:
                    value[field] = replacement
                run_store.atomic_write_json(path, value)
                self.assert_rejected_without_acceptance(root, path)

    def test_final_rechecks_prior_accepted_artifacts(self):
        for kind in ("report", "evidence", "request", "order", "snapshot", "missing", "acceptance"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.advance_to(root, "final_evidence")
                path = self.fake_produce(root)
                paths = {
                    "report": report_gate.report_path(root, "run-chain", "research"),
                    "evidence": report_gate.provider_evidence_path(root, "run-chain", "research"),
                    "request": provider_runner.adapter_request_path(root, "run-chain", "research", "claude_headless_p0"),
                    "order": report_gate.work_order_path(root, "run-chain", "research"),
                    "snapshot": work_order_builder.snapshot_path(root, "run-chain", "research", 1),
                }
                if kind == "missing":
                    paths["report"].unlink()
                elif kind == "acceptance":
                    # Adversarial state corruption is used only in a rejection test.
                    run = run_store.load_run(root, "run-chain")
                    run["step_history"][1]["acceptance"]["result"] = "forged"
                    run_store.store_run(root, run)
                else:
                    target = paths[kind]
                    value = report_gate.read_json(target)
                    value["tampered"] = True
                    run_store.atomic_write_json(target, value)
                self.assert_rejected_without_acceptance(root, path)

    def test_gate_honors_event_rejections_under_real_lock(self):
        from unittest.mock import patch
        for mutation, reason in [
            (lambda t: t["steps"][0].update(transitions=[]), "undeclared_transition_event"),
            (lambda t: t["steps"][0]["transitions"].append({"on": "research_complete", "to": "review"}), "duplicate_transition_event"),
            (lambda t: t["steps"][0]["transitions"][0].update(to="complete"), "intermediate_terminal_transition"),
            (lambda t: t["steps"][0]["transitions"][0].update(to="research"), "cyclic_step_transition"),
        ]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                self.fake_produce(root)
                template = workflow_selector.load_template("readonly_review_chain")
                mutation(template)
                before = run_store.load_run(root, "run-chain")
                with patch.object(workflow_selector, "load_template", return_value=template):
                    result = report_gate.gate_report(root, "run-chain")
                self.assertEqual(reason, result["reason"], result)
                self.assertEqual(before, run_store.load_run(root, "run-chain"))

    def test_transition_artifact_failure_does_not_accept(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            self.fake_produce(root)
            before = run_store.load_run(root, "run-chain")
            with patch.object(report_gate, "write_transition_artifact", side_effect=OSError("fixture write failure")):
                result = report_gate.gate_report(root, "run-chain")
            self.assertEqual("blocked", result["decision"])
            self.assertEqual(before, run_store.load_run(root, "run-chain"))

    def test_coordinated_attempt_tamper_without_registration_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            path = self.fake_produce(root)
            request_path = provider_runner.adapter_request_path(root, "run-chain", "research", "claude_headless_p0")
            request = report_gate.read_json(request_path)
            request["attempt_id"] = "forged-coordinated-attempt"
            request["adapter_request_digest"] = "sha256:" + report_gate.stable_digest({k: v for k, v in request.items() if k != "adapter_request_digest"})
            run_store.atomic_write_json(request_path, request)
            evidence_path = report_gate.provider_evidence_path(root, "run-chain", "research")
            evidence = report_gate.read_json(evidence_path)
            evidence["attempt_id"] = request["attempt_id"]
            run_store.atomic_write_json(evidence_path, evidence)
            run = run_store.load_run(root, "run-chain")
            del run["provider_execution"]  # Adversarial removal, not successful fixture setup.
            run_store.store_run(root, run)
            self.assert_rejected_without_acceptance(root, path)

    def test_success_waiting_is_rejected_before_acceptance(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            self.fake_produce(root)
            before = run_store.load_run(root, "run-chain")
            template = workflow_selector.load_template("readonly_review_chain")
            template["steps"][0]["transitions"][0]["to"] = "waiting_human"
            with patch.object(workflow_selector, "load_template", return_value=template):
                result = report_gate.gate_report(root, "run-chain")
            self.assertEqual("unsupported_success_waiting_transition", result["reason"], result)
            self.assertEqual(before, run_store.load_run(root, "run-chain"))

    def test_non_success_waiting_requires_resume_and_fresh_result(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            self.fake_produce(root, report_overrides={"result": "blocked"})
            before = run_store.load_run(root, "run-chain")
            template = workflow_selector.load_template("readonly_review_chain")
            template["steps"][0]["transitions"][1]["to"] = "waiting_human"
            with patch.object(workflow_selector, "load_template", return_value=template):
                result = report_gate.gate_report(root, "run-chain")
            self.assertEqual("blocked", result["decision"], result)
            waiting = run_store.load_run(root, "run-chain")
            self.assertEqual("waiting_human", waiting["run_state"])
            self.assertEqual(before["step_history"], waiting["step_history"])
            self.assert_rejected_without_acceptance(root)
            resumed = load_payload(run_frontdoor(root, "resume", "--run-id", "run-chain", "--requeue"))
            self.assertTrue(resumed["resumed"])
            self.fake_produce(root)
            result = self.submit(root)
            self.assertEqual("ok", result["decision"], result)
            self.assertEqual("review", result["workflow_run"]["current_step"])
            accepted = [h for h in result["workflow_run"]["step_history"] if h["status"] == "accepted"]
            self.assertEqual(1, len(accepted))

    def test_registered_claim_and_result_authority_rejections(self):
        variants = ("missing-claim", "duplicate-ref", "wrong-step-claim", "claim-signature", "claim-state",
                    "execution-context", "execution-lease", "execution-model", "invoking", "expired-invoking",
                    "missing-journal", "journal-attempt", "journal-report", "journal-transcript", "missing-request")
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                path = self.fake_produce(root)
                run = run_store.load_run(root, "run-chain")
                execution = run["provider_execution"]
                claim = next(t for t in run["transitions"] if t["transition"] == "run_provider")
                if variant == "missing-claim":
                    run["transitions"].remove(claim)
                elif variant == "duplicate-ref":
                    claim["artifact_refs"].append(claim["artifact_refs"][-1])
                elif variant == "wrong-step-claim":
                    claim["artifact_refs"] = [r.replace("research-", "review-") for r in claim["artifact_refs"]]
                elif variant == "claim-signature":
                    claim["signature"]["signature"] = "sha256:" + "0" * 64
                elif variant == "claim-state":
                    claim["from_state"] = "created"
                elif variant == "execution-context":
                    execution["context_snapshot_digest"] = "sha256:" + "0" * 64
                elif variant == "execution-lease":
                    execution["lease"]["lease_id"] = "wrong-lease"
                elif variant == "execution-model":
                    execution["provider_binding"]["intended_model"] = "wrong-model"
                elif variant in ("invoking", "expired-invoking"):
                    execution["phase"] = "invoking"
                    if variant.startswith("expired"):
                        execution["lease"]["lease_expires_at"] = "2000-01-01T00:00:00Z"
                elif variant == "missing-request":
                    provider_runner.adapter_request_path(root, "run-chain", "research", "claude_headless_p0").unlink()
                else:
                    journal_path = Path(execution["last_outcome"]["attempt_result_path"])
                    if variant == "missing-journal":
                        journal_path.unlink()
                    else:
                        journal = report_gate.read_json(journal_path)
                        if variant == "journal-attempt":
                            journal["attempt_id"] = "late-journal"
                        elif variant == "journal-report":
                            journal["report"]["result"] = "blocked"
                        else:
                            journal["transcript_sha256"] = "sha256:" + "0" * 64
                        run_store.atomic_write_json(journal_path, journal)
                run_store.store_run(root, run)
                self.assert_rejected_without_acceptance(root, path)

    def test_completed_result_does_not_require_future_lease(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            self.fake_produce(root)
            run = run_store.load_run(root, "run-chain")
            run["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00Z"
            run_store.store_run(root, run)
            self.assertEqual("ok", self.submit(root)["decision"])

    def test_malformed_execution_fails_closed_in_run_store(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            self.fake_produce(root)
            run = run_store.load_run(root, "run-chain")
            run["provider_execution"] = "malformed"
            path = run_store.run_path(root, "run-chain")
            run_store.atomic_write_json(path, run)
            before = path.read_bytes()
            result = self.submit(root)
            self.assertEqual("blocked", result["decision"], result)
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
