#!/usr/bin/env python3
"""Deterministic final producer after the actual offline #108 runner."""
from __future__ import annotations

import copy
import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_multistep_provider_runner as runner_fixture
from test_frontdoor_orchestrator import run_frontdoor, load_payload
import frontdoor_orchestrator as frontdoor
import provider_runner
import report_gate
import run_lifecycle
import run_lock
import run_store
import workflow_selector


class HarnessGateTests(unittest.TestCase):
    setup_chain = runner_fixture.MultistepProviderRunnerTests.setup_chain
    drain = runner_fixture.MultistepProviderRunnerTests.drain
    actor = {"principal_type": "harness_runner", "principal_id": "local-harness", "authn_method": "local_cli"}

    def prepare(self, root):
        self.setup_chain(root)
        for _ in range(2):
            result = frontdoor.run_provider(state_root=root, run_id="run-chain", fake_provider_mode="success")
            self.assertEqual("ok", result["decision"], result)
            self.drain(root)

    def invoke(self, root):
        return frontdoor.run_harness_gate(state_root=root, run_id="run-chain")

    def path(self, root):
        return report_gate.report_path(root, "run-chain", "final_evidence")

    def test_cli_actual_runner_to_final(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            completed = run_frontdoor(root, "run-harness-gate", "--run-id", "run-chain", check=False)
            self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
            result = load_payload(completed)
            self.assertEqual("final_evidence_valid", result["reason"])
            run = run_store.load_run(root, "run-chain")
            self.assertEqual("complete", run["run_state"])
            self.assertEqual("complete", run["terminal"]["status"])
            self.assertEqual(["research", "review", "final_evidence"],
                [h["step_id"] for h in run["step_history"] if h.get("status") == "accepted"])
            report = report_gate.read_json(self.path(root))
            self.assertEqual("complete", report["result"])
            self.assertNotIn("provider_evidence", report)
            request = frontdoor.read_json(frontdoor.request_path(root, "req-chain"))
            self.assertEqual("complete", request["status"])

    def test_no_provider_and_duplicate_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            with mock.patch.object(provider_runner, "execute_provider", side_effect=AssertionError("provider forbidden")), \
                 mock.patch.object(provider_runner, "run_provider", side_effect=AssertionError("runner forbidden")):
                self.assertEqual("ok", self.invoke(root)["decision"])
                before = self.path(root).read_bytes()
                result = self.invoke(root)
                self.assertEqual("duplicate_step_report", result["reason"])
                self.assertEqual(before, self.path(root).read_bytes())

    def test_unsupported_contracts_never_create_report(self):
        for variant in ("publication_gate", "managed_worker", "schema", "permission", "workflow"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.prepare(root)
                template = copy.deepcopy(workflow_selector.load_template("readonly_review_chain"))
                run = run_store.load_run(root, "run-chain")
                if variant == "publication_gate": run["current_step"] = variant
                elif variant == "workflow": run["workflow_id"] = "standard_code_change"
                elif variant == "managed_worker": template["steps"][-1]["provider_route"]["adapter_kind"] = variant
                elif variant == "permission": template["steps"][-1]["permission_mode"] = "full"
                else: template["output_contracts"]["final_evidence"]["schema_path"] = "foreign.json"
                with mock.patch.object(run_store, "load_run", return_value=run), \
                     mock.patch.object(workflow_selector, "load_template", return_value=template):
                    result = self.invoke(root)
                self.assertEqual("blocked", result["decision"], result)
                self.assertTrue(result["reason"])
                self.assertFalse(self.path(root).exists())

    def test_prior_history_and_artifact_drift(self):
        for variant in ("signature", "missing", "reordered", "transition", "report", "snapshot", "request", "journal", "transcript"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.prepare(root)
                run = run_store.load_run(root, "run-chain")
                records = [h for h in run["step_history"] if h.get("status") == "accepted"]
                acceptance = records[0]["acceptance"]
                if variant == "signature": acceptance["signature"]["signature"] = "sha256:forged"
                elif variant == "missing": run["step_history"].remove(records[0])
                elif variant == "reordered": run["step_history"] = list(reversed(run["step_history"]))
                elif variant == "transition":
                    run["transitions"] = [t for t in run["transitions"] if t.get("report_binding") != acceptance]
                else:
                    key = {"report":"report_path", "snapshot":"snapshot_path", "request":"request_path",
                           "journal":"attempt_result_path", "transcript":"transcript_path"}[variant]
                    Path(acceptance[key]).write_bytes(b'{}\n')
                with mock.patch.object(run_store, "load_run", return_value=run): result = self.invoke(root)
                self.assertEqual("blocked", result["decision"], result)
                self.assertFalse(self.path(root).exists())

    def test_crash_after_report_then_exact_bytes_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            with mock.patch.object(report_gate, "gate_report", side_effect=RuntimeError("crash before gate")):
                with self.assertRaisesRegex(RuntimeError, "crash before gate"): self.invoke(root)
            before = self.path(root).read_bytes()
            self.assertEqual("step_queued", run_store.load_run(root,"run-chain")["run_state"])
            self.assertEqual("ok", self.invoke(root)["decision"])
            self.assertEqual(before, self.path(root).read_bytes())

    def test_existing_conflicting_or_symlink_report_is_preserved(self):
        for variant in ("partial", "different", "symlink"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.prepare(root)
                path = self.path(root)
                if variant == "symlink": path.symlink_to(root / "outside")
                else: run_store.create_private_file(path, b'{' if variant == "partial" else b'{}\n')
                result = self.invoke(root)
                self.assertEqual("blocked", result["decision"], result)
                if variant == "symlink": self.assertTrue(path.is_symlink())
                else: self.assertEqual(b'{' if variant == "partial" else b'{}\n', path.read_bytes())
                self.assertEqual("step_queued", run_store.load_run(root,"run-chain")["run_state"])

    def test_late_drift_is_revalidated_by_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            gate = report_gate.gate_report
            def late(*args, **kwargs):
                report_gate.report_path(root,"run-chain","research").write_bytes(b'{}\n')
                return gate(*args, **kwargs)
            with mock.patch.object(report_gate, "gate_report", side_effect=late): result = self.invoke(root)
            self.assertEqual("prior_artifact_drift", result["reason"])
            self.assertEqual("step_queued", run_store.load_run(root,"run-chain")["run_state"])

    def test_missing_or_unsafe_key_is_not_created_or_repaired(self):
        for variant in ("missing", "unsafe"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.prepare(root)
                key = run_lifecycle.signing_key_path(root,self.actor)
                if variant == "missing": key.unlink()
                else: key.chmod(0o644)
                result = self.invoke(root)
                self.assertEqual("signing_key_unavailable", result["reason"])
                self.assertFalse(self.path(root).exists())
                if variant == "missing": self.assertFalse(key.exists())
                else: self.assertEqual(0o644,key.stat().st_mode & 0o777)

    def test_source_contract_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            template = copy.deepcopy(workflow_selector.load_template("readonly_review_chain"))
            template["description"] = "changed source contract"
            with mock.patch.object(workflow_selector, "load_template", return_value=template):
                result = self.invoke(root)
            self.assertEqual("prior_acceptance_invalid", result["reason"])
            self.assertFalse(self.path(root).exists())

    def test_live_or_foreign_completed_claim_is_rejected(self):
        for variant in ("live", "foreign_attempt"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.prepare(root)
                run = run_store.load_run(root, "run-chain")
                if variant == "live": run["provider_execution"]["phase"] = "claimed"
                else: run["provider_execution"]["attempt_id"] = "foreign-attempt"
                with mock.patch.object(run_store, "load_run", return_value=run): result = self.invoke(root)
                self.assertEqual("provider_in_flight", result["reason"])
                self.assertFalse(self.path(root).exists())

    def test_concurrent_executors_accept_final_once(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.invoke, root) for _ in range(2)]
                results = [f.result(timeout=15) for f in futures]
            self.assertEqual(["blocked", "ok"], sorted(r["decision"] for r in results))
            self.assertEqual("duplicate_step_report", next(r for r in results if r["decision"] == "blocked")["reason"])
            run = run_store.load_run(root,"run-chain")
            self.assertEqual(1, sum(h.get("status") == "accepted" and h.get("step_id") == "final_evidence"
                                    for h in run["step_history"]))

    def test_precreate_and_run_commit_faults_are_recoverable(self):
        for variant in ("create", "run_commit"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); self.prepare(root)
                target = "create_private_file" if variant == "create" else "store_run"
                with mock.patch.object(run_store, target, side_effect=OSError("fixture write fault")):
                    result = self.invoke(root)
                self.assertEqual("blocked", result["decision"])
                self.assertEqual("step_queued", run_store.load_run(root,"run-chain")["run_state"])
                self.assertEqual(variant == "run_commit", self.path(root).exists())
                self.assertEqual("ok", self.invoke(root)["decision"])

    def test_bridge_principal_is_rejected_before_report(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            result = frontdoor.run_harness_gate(state_root=root, run_id="run-chain",
                principal={"principal_type": "main_agent_bridge", "principal_id": "bridge", "authn_method": "local_cli"})
            self.assertEqual("blocked", result["decision"])
            self.assertFalse(self.path(root).exists())

    def test_prior_semantic_validation_is_reused(self):
        # Unit-level coherent input to the shared semantic checks. Integration
        # drift tests above separately enforce signatures and exact artifact bytes.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            run = run_store.load_run(root,"run-chain")
            template = workflow_selector.load_template("readonly_review_chain")
            order = report_gate.read_json(report_gate.work_order_path(root,"run-chain","research"))
            original = report_gate.read_json(report_gate.report_path(root,"run-chain","research"))
            for change, expected in (({"no_diff_completion":1},"boolean true"),
                                     ({"source_refs":["outside.md"]},"outside bounded"),
                                     ({"findings":[]},"findings"), ({"extra":True},"extra")):
                with self.subTest(change=change):
                    errors = report_gate._chain_report_errors(root,run,order,{**original,**change},"research")
                    self.assertIn(expected,"; ".join(errors))

    def test_committed_run_replay_repairs_terminal_request_sync(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            with mock.patch.object(frontdoor, "synchronize_terminal_request", side_effect=RuntimeError("sync crash")):
                with self.assertRaisesRegex(RuntimeError,"sync crash"): self.invoke(root)
            self.assertEqual("complete",run_store.load_run(root,"run-chain")["run_state"])
            self.assertEqual("duplicate_step_report",self.invoke(root)["reason"])
            request = frontdoor.read_json(frontdoor.request_path(root,"req-chain"))
            self.assertEqual("complete",request["status"])

    def test_held_lock_is_not_stolen_or_released(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); self.prepare(root)
            with run_lock.hold_global_lock(root, operation="fixture-holder", principal=self.actor):
                before = run_lock.read_lock_owner(run_lock.global_lock_path(root))
                with mock.patch.object(run_lock,"DEFAULT_TIMEOUT_SECONDS",0.01): result = self.invoke(root)
                self.assertEqual("lock_contention",result["reason"])
                self.assertEqual(before,run_lock.read_lock_owner(run_lock.global_lock_path(root)))
                self.assertFalse(self.path(root).exists())


if __name__ == "__main__": unittest.main()
