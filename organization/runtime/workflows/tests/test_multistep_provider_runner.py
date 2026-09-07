#!/usr/bin/env python3
"""Real frontdoor/runner integration with offline provider results, never live custody."""
from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_multistep_report_gate as chain_fixture
from test_frontdoor_orchestrator import load_payload, run_frontdoor
import provider_runner
import report_gate
import run_store
import run_lifecycle
import run_lock
import workflow_selector
import scoped_worker_executor
import work_order_builder
import frontdoor_orchestrator


class MultistepProviderRunnerTests(unittest.TestCase):
    # Reuse entry fixtures only; claims and journals must be produced by run_provider.
    setup_chain = chain_fixture.MultistepReportGateTests.setup_chain
    drain = chain_fixture.MultistepReportGateTests.drain

    def invoke(self, root, mode="success"):
        return provider_runner.run_provider(
            state_root=root, run_id="run-chain", fake_provider_mode=mode,
            principal={"principal_type": "harness_runner", "principal_id": "runner-chain-test",
                       "authn_method": "local_test"},
        )

    def test_actual_frontdoor_runner_two_steps(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            attempts, requests = [], []
            for step, next_step in (("research", "review"), ("review", "final_evidence")):
                result = load_payload(run_frontdoor(root, "run-provider", "--run-id", "run-chain",
                    "--fake-provider-mode", "success", check=False))
                self.assertEqual("ok", result["decision"], result)
                run = run_store.load_run(root, "run-chain")
                self.assertEqual(next_step, run["current_step"])
                self.assertEqual("step_queued", run["run_state"])
                execution = run["provider_execution"]
                attempts.append(execution["attempt_id"])
                requests.append(execution["adapter_request_digest"])
                self.assertEqual("completed", execution["phase"])
                journal, _ = provider_runner.provider_attempt_paths(root, "run-chain", attempts[-1])
                self.assertTrue(journal.is_file())
                request = self.request_for(root, step)
                self.assertEqual(requests[-1], request["adapter_request_digest"])
                claims = [t for t in run["transitions"] if t.get("reason_class") == "provider_claimed"]
                self.assertEqual(len(attempts), len(claims))
                self.assertTrue(all(t.get("signature") for t in claims))
                self.assertEqual(["research", "review"][:len(attempts)],
                                 [h["step_id"] for h in self.accepted(root)])
                if step == "research":
                    self.assertNotIn("provider_evidence", provider_runner.read_json(
                        report_gate.report_path(root, "run-chain", step)))
                else:
                    self.assertEqual(self.request_for(root)["context_refs"], request["context_refs"])
                self.assertTrue(report_gate.report_path(root, "run-chain", step).is_file())
                self.drain(root)
            self.assertEqual(2, len(set(attempts)))
            self.assertEqual(2, len(set(requests)))
            with mock.patch.object(provider_runner, "execute_provider") as execute:
                rejected = self.invoke(root)
            self.assertEqual("blocked", rejected["decision"])
            execute.assert_not_called()
            self.assertEqual("final_evidence", run_store.load_run(root, "run-chain")["current_step"])

    def accepted(self, root):
        return [h for h in run_store.load_run(root, "run-chain")["step_history"]
                if h.get("status") == "accepted"]

    def request_for(self, root, step="research"):
        return provider_runner.read_json(provider_runner.adapter_request_path(
            root, "run-chain", step, "claude_headless_p0"))

    def crash_after_journal(self, root):
        original = provider_runner.private_atomic_write_json
        def write(state_root, path, payload):
            result = original(state_root, path, payload)
            if payload.get("attempt_result_version") == "1":
                raise RuntimeError("fixture crash after journal, before promotion")
            return result
        with mock.patch.object(provider_runner, "private_atomic_write_json", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "fixture crash after journal"):
                self.invoke(root)
        run = run_store.load_run(root, "run-chain")
        run["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        run_store.store_run(root, run, expected_current_state="waiting_provider")
        self.assertFalse(report_gate.report_path(root, "run-chain", run["current_step"]).exists())
        return run

    def test_journal_recovery_for_both_steps_at_most_once(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            for index, step in enumerate(("research", "review"), 1):
                with mock.patch.object(provider_runner, "execute_provider", wraps=provider_runner.execute_provider) as execute:
                    self.crash_after_journal(root)
                    self.assertEqual(1, execute.call_count)
                    recovered = self.invoke(root)
                    self.assertEqual("ok", recovered["decision"], recovered)
                    self.assertEqual(1, execute.call_count)
                    self.assertEqual(index, len(self.accepted(root)))
                    self.invoke(root)  # Next order is not issued yet; restart cannot replay the old provider.
                    self.assertEqual(1, execute.call_count)
                    self.assertEqual(index, len(self.accepted(root)))
                self.drain(root)

    def test_recovery_tamper_never_promotes_or_invokes(self):
        variants = ["attempt_id", "lease_id", "work_order_digest", "adapter_request_digest",
                    "context_snapshot_digest", "adapter_id", "transcript_sha256",
                    "coordinated_contract", "other_step", "other_run", "model_binding"]
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                run = self.crash_after_journal(root)
                execution = run["provider_execution"]
                journal_path, _ = provider_runner.provider_attempt_paths(root, "run-chain", execution["attempt_id"])
                journal = provider_runner.read_json(journal_path)
                request = self.request_for(root)
                if variant in {"coordinated_contract", "other_step", "other_run", "model_binding"}:
                    if variant == "coordinated_contract":
                        request["step_contract"]["role"] = "forged-role"
                    elif variant == "model_binding":
                        request["intended_model"] = "unapproved-model"
                    else:
                        request["step_id" if variant == "other_step" else "run_id"] = "foreign"
                    request.pop("adapter_request_digest")
                    digest = "sha256:" + provider_runner.stable_digest(request)
                    request["adapter_request_digest"] = digest
                    execution["adapter_request_digest"] = digest
                    journal["adapter_request_digest"] = digest
                    provider_runner.private_atomic_write_json(root,
                        provider_runner.adapter_request_path(root, "run-chain", "research", "claude_headless_p0"), request)
                    run_store.store_run(root, run, expected_current_state="waiting_provider")
                else:
                    journal[variant] = "tampered"
                provider_runner.private_atomic_write_json(root, journal_path, journal)
                with mock.patch.object(provider_runner, "execute_provider") as execute:
                    result = self.invoke(root)
                execute.assert_not_called()
                self.assertEqual("blocked", result["decision"], result)
                self.assertTrue(result["reason"], result)
                self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())
                self.assertEqual([], self.accepted(root))

    def test_abandoned_journal_not_recoverable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.setup_chain(root)
            run = self.crash_after_journal(root)
            path, _ = provider_runner.provider_attempt_paths(root, "run-chain", run["provider_execution"]["attempt_id"])
            journal = provider_runner.read_json(path); journal["abandoned"] = True
            provider_runner.private_atomic_write_json(root, path, journal)
            self.assertIsNone(provider_runner.recover_completed_provider_attempt(state_root=root, run=run,
                adapter=provider_runner.load_provider_adapters()["claude_headless_p0"]))
            self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())

    def test_coherent_invalid_provider_results_reach_schema_gate(self):
        mutations = [
            ("missing", lambda r: r.pop("source_refs"), "source_refs"),
            ("extra", lambda r: r.update(provider_evidence={}), "provider_evidence"),
            ("boolean", lambda r: r.update(no_diff_completion=1), "boolean true"),
            ("foreign_source", lambda r: r.update(source_refs=["unapproved.md"]), "outside bounded"),
            ("foreign_finding", lambda r: r["findings"][0].update(evidence_refs=["unapproved.md"]), "outside bounded"),
        ]
        original = provider_runner.execute_provider
        for label, mutate, error in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.setup_chain(root)
                def execute(**kwargs):
                    outcome, report, details = original(**kwargs)
                    mutate(report)
                    return outcome, report, details
                with mock.patch.object(provider_runner, "execute_provider", side_effect=execute) as invocation:
                    result = self.invoke(root)
                self.assertEqual(1, invocation.call_count)
                self.assertEqual("blocked", result["decision"], result)
                self.assertIn(error, json.dumps(result))
                self.assertEqual([], self.accepted(root))
                self.assertTrue(report_gate.report_path(root, "run-chain", "research").exists())

    def test_review_findings_and_blocked_do_not_advance(self):
        for mode in ("findings", "blocked"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.setup_chain(root)
                self.assertEqual("ok", self.invoke(root)["decision"]); self.drain(root)
                result = self.invoke(root, mode)
                self.assertEqual("blocked", result["decision"], result)
                self.assertIn("review_blocked", json.dumps(result))
                self.assertEqual(1, len(self.accepted(root)))

    def test_research_model_policy_is_not_bypassed(self):
        for mode in ("model_mismatch", "missing_effective_model"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.setup_chain(root)
                result = self.invoke(root, mode)
                self.assertEqual("provider_model_mismatch", result["reason"], result)
                self.assertEqual([], self.accepted(root))
                self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())

    def test_signed_unsafe_orders_fail_before_invocation(self):
        mutations = [lambda o: o.update(permission_mode="edit"),
                     lambda o: o["activation_scope"]["allowed_ops"].update(edit=0),
                     lambda o: o["activation_scope"]["allowed_ops"].update(extra=False),
                     lambda o: o.update(to_role="other-role"),
                     lambda o: o.update(expected_output="external_review_report")]
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.setup_chain(root)
                run = run_store.load_run(root, "run-chain")
                path = provider_runner.work_order_path(root, "run-chain", "research")
                order = provider_runner.read_json(path); mutate(order)
                authority = order["work_order_authority"]
                authority["signature"] = frontdoor_orchestrator.sign_transition(
                    state_root=root, principal=authority["issuer_principal"], transition="issue_work_order",
                    subject={"unsigned_work_order_digest": scoped_worker_executor._unsigned_work_order_digest(order)})
                run_store.atomic_write_json(path, order)
                snapshot_path = work_order_builder.snapshot_path(root, "run-chain", "research", run["iteration"])
                snapshot = provider_runner.read_json(snapshot_path)
                snapshot.update(work_order=order, work_order_digest=work_order_builder.sha256_digest(order),
                                activation_scope=order["activation_scope"])
                run_store.atomic_write_json(snapshot_path, snapshot)
                scoped_worker_executor.verify_frozen_work_order(root, run_id="run-chain", step_id="research",
                    expected_run_states={"step_queued"}, expected_iteration=run["iteration"])
                with mock.patch.object(provider_runner, "execute_provider") as execute:
                    result = self.invoke(root)
                execute.assert_not_called()
                self.assertEqual("work_order_not_provider_safe", result["reason"], result)
                self.assertEqual([], self.accepted(root))
                self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())

    def test_route_and_schema_contracts_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.setup_chain(root)
            order = provider_runner.read_json(provider_runner.work_order_path(root, "run-chain", "research"))
            template = workflow_selector.load_template("readonly_review_chain")
            for kind in ("managed_worker", "harness_gate", "unknown"):
                changed = copy.deepcopy(template); changed["steps"][0]["provider_route"]["adapter_kind"] = kind
                with self.subTest(route=kind), mock.patch.object(workflow_selector, "load_template", return_value=changed):
                    self.assertIn("unsupported_step_contract", provider_runner.validate_work_order_for_runner(order))
            for path in ("/tmp/schema.json", "../schema.json", "organization/runtime/workflows/schemas/unknown.json"):
                changed = copy.deepcopy(template); changed["output_contracts"]["research_report"]["schema_path"] = path
                with self.subTest(schema=path), mock.patch.object(workflow_selector, "load_template", return_value=changed):
                    self.assertTrue(provider_runner.validate_work_order_for_runner(order))

    def test_contract_drift_at_dispatch_and_recovery(self):
        for seam in ("dispatch", "recovery"):
            for kind in ("schema", "template", "request"):
                with self.subTest(seam=seam, kind=kind), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw).resolve(); self.setup_chain(root)
                    original_resolve = provider_runner.resolve_step_contract
                    def drift(*args, **kwargs):
                        contract = original_resolve(*args, **kwargs)
                        contract["report_schema_sha256" if kind == "schema" else "template_contract_sha256"] = "sha256:changed"
                        return contract
                    if seam == "recovery":
                        self.crash_after_journal(root)
                        with mock.patch.object(provider_runner, "resolve_step_contract", side_effect=drift), \
                             mock.patch.object(provider_runner, "execute_provider") as execute:
                            result = self.invoke(root)
                        execute.assert_not_called()
                    else:
                        original_authorize = provider_runner.authorize_provider_dispatch
                        def authorize(**kwargs):
                            if kind == "request":
                                kwargs["request"]["step_contract"]["role"] = "forged"
                                kwargs["request"].pop("adapter_request_digest")
                                kwargs["request"]["adapter_request_digest"] = "sha256:" + provider_runner.stable_digest(kwargs["request"])
                            with mock.patch.object(provider_runner, "resolve_step_contract", side_effect=drift):
                                return original_authorize(**kwargs)
                        with mock.patch.object(provider_runner, "authorize_provider_dispatch", side_effect=authorize) as authorization, \
                             mock.patch.object(provider_runner, "execute_provider") as execute:
                            result = self.invoke(root)
                        self.assertGreaterEqual(authorization.call_count, 1)
                        execute.assert_not_called()
                    self.assertEqual("blocked", result["decision"], result)
                    self.assertIn("mismatch", json.dumps(result))
                    self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())
                    self.assertEqual([], self.accepted(root))

    def test_contract_drift_during_provider_execution_blocks_acceptance(self):
        for kind in ("schema", "template"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                self.setup_chain(root)
                execute = provider_runner.execute_provider
                resolve = provider_runner.resolve_step_contract
                finished = False
                def run_provider(**kwargs):
                    nonlocal finished
                    result = execute(**kwargs)
                    finished = True
                    return result
                def current_contract(*args, **kwargs):
                    contract = resolve(*args, **kwargs)
                    if finished:
                        key = "report_schema_sha256" if kind == "schema" else "template_contract_sha256"
                        contract[key] = "sha256:changed"
                    return contract
                with mock.patch.object(provider_runner, "execute_provider", side_effect=run_provider) as invoked, \
                     mock.patch.object(provider_runner, "resolve_step_contract", side_effect=current_contract):
                    result = self.invoke(root)
                self.assertEqual(1, invoked.call_count)
                self.assertEqual("blocked", result["decision"], result)
                self.assertIn("provider_step_contract_mismatch", json.dumps(result))
                self.assertEqual([], self.accepted(root))
                self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())
                self.assertEqual("waiting_human", run_store.load_run(root, "run-chain")["run_state"])

    def test_retry_budget_is_independent_between_chain_steps(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.setup_chain(root)
            original = provider_runner.execute_provider
            calls = {"research": 0, "review": 0}
            def execute(**kwargs):
                step = kwargs["request"]["step_id"]
                calls[step] += 1
                if calls[step] <= (5 if step == "research" else 1):
                    return "timeout", None, {"reason": "provider_timeout", "duration_ms": 0}
                return original(**kwargs)
            with mock.patch.object(provider_runner, "execute_provider", side_effect=execute):
                self.assertEqual("ok", self.invoke(root)["decision"])
                self.assertEqual(5, run_store.load_run(root, "run-chain")["provider_execution"]["retry"]["auto_retries_used"])
                self.drain(root)
                self.assertEqual("ok", self.invoke(root)["decision"])
            self.assertEqual({"research": 6, "review": 2}, calls)
            retry = run_store.load_run(root, "run-chain")["provider_execution"]["retry"]
            self.assertEqual(1, retry["auto_retries_used"])
            self.assertEqual(1, retry["consecutive_failures"])

    def test_in_flight_and_heartbeat_outside_global_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.setup_chain(root)
            original = provider_runner.execute_provider
            def execute(**kwargs):
                with run_lock.hold_global_lock(root, operation="fixture_probe", run_id="run-chain",
                    principal={"principal_type": "harness_runner", "principal_id": "probe", "authn_method": "local_test"}):
                    pass
                self.assertTrue(kwargs["heartbeat"]())
                self.assertEqual("provider_in_flight", self.invoke(root)["reason"])
                return original(**kwargs)
            with mock.patch.object(provider_runner, "execute_provider", side_effect=execute) as invocation:
                result = self.invoke(root)
            self.assertEqual(1, invocation.call_count)
            self.assertEqual("ok", result["decision"], result)

    def test_real_schema_bytes_drift_before_dispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.setup_chain(root)
            original_authorize = provider_runner.authorize_provider_dispatch
            original_read = provider_runner.read_report_schema
            def changed_bytes(path):
                data = original_read(path)
                return data + b" " if path.name == "research-report.schema.json" else data
            def authorize(**kwargs):
                with mock.patch.object(provider_runner, "read_report_schema", changed_bytes):
                    return original_authorize(**kwargs)
            with mock.patch.object(provider_runner, "authorize_provider_dispatch", side_effect=authorize) as checked, \
                 mock.patch.object(provider_runner, "execute_provider") as execute:
                result = self.invoke(root)
            self.assertGreaterEqual(checked.call_count, 1)
            execute.assert_not_called()
            self.assertEqual("blocked", result["decision"])
            self.assertIn("provider_step_contract_mismatch", json.dumps(result))
            self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())

    def test_context_order_snapshot_rechecked_at_dispatch(self):
        for variant in ("context", "order", "snapshot", "role_binding"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.setup_chain(root)
                original = provider_runner.authorize_provider_dispatch
                def authorize(**kwargs):
                    request = kwargs["request"]
                    if variant == "context":
                        request["context_refs"][0]["digest"] = "sha256:" + "0" * 64
                    else:
                        path = Path(request["work_order_snapshot_path"] if variant == "snapshot"
                                    else request["work_order_path"])
                        changed = provider_runner.read_json(path)
                        if variant == "snapshot":
                            changed["work_order_digest"] = "sha256:" + "0" * 64
                        else:
                            changed["to_role" if variant == "role_binding" else "instruction"] = "changed"
                        run_store.atomic_write_json(path, changed)
                    return original(**kwargs)
                with mock.patch.object(provider_runner, "authorize_provider_dispatch", side_effect=authorize) as checked, \
                     mock.patch.object(provider_runner, "execute_provider") as execute:
                    result = self.invoke(root)
                self.assertGreaterEqual(checked.call_count, 1)
                execute.assert_not_called()
                self.assertEqual("blocked", result["decision"])
                self.assertIn("mismatch", json.dumps(result))
                self.assertFalse(report_gate.report_path(root, "run-chain", "research").exists())

    def test_schema_reader_rejects_symlink_missing_invalid_and_oversized(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.setup_chain(root)
            order = provider_runner.read_json(provider_runner.work_order_path(root, "run-chain", "research"))
            for variant in ("symlink", "missing", "invalid", "oversized"):
                with self.subTest(variant=variant), tempfile.TemporaryDirectory() as schema_raw:
                    schema_root = Path(schema_raw).resolve()
                    path = schema_root / "organization/runtime/workflows/schemas/research-report.schema.json"
                    path.parent.mkdir(parents=True)
                    if variant == "symlink":
                        target = schema_root / "other.json"; target.write_text("{}")
                        path.symlink_to(target)
                    elif variant != "missing":
                        path.write_text("x" * 8193 if variant == "oversized" else "[]")
                    with mock.patch.object(provider_runner, "REPO_ROOT", schema_root):
                        with self.assertRaises(provider_runner.ProviderRunnerError):
                            provider_runner.resolve_step_contract(order)

    def test_legacy_contract_fallback_is_exact_single_step_only(self):
        import test_provider_runner as legacy
        # This compatibility test uses the real legacy frozen order, not a multi-step downgrade.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            legacy.prepare_run(root, request_id="req-legacy", run_id="run-legacy")
            run = run_store.load_run(root, "run-legacy")
            order = provider_runner.read_json(provider_runner.work_order_path(root, "run-legacy", "review"))
            adapter = provider_runner.load_provider_adapters()["claude_headless_p0"]
            request = provider_runner.adapter_request(state_root=root, run=run, work_order=order, adapter=adapter,
                principal={"principal_type": "harness_runner", "principal_id": "legacy", "authn_method": "local_test"})
            request.pop("step_contract")
            provider_runner.verify_request_step_contract(request, order, state_root=root, run=run)
            request["step_contract"] = None
            with self.assertRaisesRegex(provider_runner.ProviderRunnerError, "provider_step_contract_mismatch"):
                provider_runner.verify_request_step_contract(request, order, state_root=root, run=run)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.setup_chain(root); self.invoke(root)
            request = self.request_for(root); request.pop("step_contract")
            order = provider_runner.read_json(Path(request["work_order_path"]))
            run = run_store.load_run(root, "run-chain")
            run["current_step"] = "research"  # Pure validator input only; never stored or used as positive execution.
            with self.assertRaisesRegex(provider_runner.ProviderRunnerError, "provider_step_contract_mismatch"):
                provider_runner.verify_request_step_contract(request, order, state_root=root, run=run)


if __name__ == "__main__":
    unittest.main()
