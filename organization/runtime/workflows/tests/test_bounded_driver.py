#!/usr/bin/env python3
"""Bounded driving through actual approval, transport and deterministic gates."""
from __future__ import annotations

import json
import tempfile
import unittest
import concurrent.futures
import threading
import hashlib
import time
from pathlib import Path
from unittest import mock

from test_frontdoor_orchestrator import external_review_classification, load_payload, run_frontdoor
import frontdoor_orchestrator as frontdoor
import run_store
import provider_runner
import run_lifecycle
import report_gate


class BoundedDriverTests(unittest.TestCase):
    actor = {"principal_type": "harness_runner", "principal_id": "local-harness", "authn_method": "local_cli"}

    def drive(self, root, **kwargs):
        return frontdoor.drive_run(state_root=root, principal=self.actor, fake_provider_mode="success", **kwargs)

    def prepared(self, root):
        request = self.approve(root)
        result = self.drive(root, request_id=request, max_iterations=1)
        self.assertEqual("iteration_exhausted", result["reason_class"])
        return result["run_id"]

    def approve(self, root, suffix="chain"):
        request = "req-" + suffix
        proposed = load_payload(run_frontdoor(
            root, "propose", "--task-id", "TSK-" + suffix, "--request-id", request,
            "--prompt", "Research and independently review bounded evidence",
            "--classification", json.dumps(external_review_classification(
                task_kind="research", expected_artifacts=["research_report", "typed_report", "final_evidence"])),
            "--ref", "organization/runtime/workflows/README.md"))
        load_payload(run_frontdoor(root, "approve", "--request-id", request,
                                  "--human-action-id", proposed["approval"]["human_action_id"]))
        return request

    def test_one_approval_one_actual_cli_drive_including_create(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            request = self.approve(root)
            self.assertFalse(frontdoor.read_json(frontdoor.request_path(root, request)).get("run_id"))
            result = run_frontdoor(root, "drive-run", "--request-id", request,
                                   "--fake-provider-mode", "success", check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            payload = load_payload(result)
            self.assertEqual("terminal", payload["stop"])
            run = run_store.load_run(root, payload["run_id"])
            self.assertEqual("complete", run["run_state"])
            self.assertEqual(["research", "review", "final_evidence"],
                             [h["step_id"] for h in run["step_history"] if h.get("status") == "accepted"])
            self.assertEqual(2, sum(t.get("reason_class") == "provider_claimed" for t in run["transitions"]))

    def test_restart_and_idempotent_request_binding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            self.assertEqual("complete", self.drive(root, run_id=run_id)["run_state"])
            before = run_store.run_path(root, run_id).read_bytes()
            result = self.drive(root, request_id="req-chain")
            self.assertEqual(run_id, result["run_id"])
            self.assertEqual("terminal", result["stop"])
            self.assertEqual(before, run_store.run_path(root, run_id).read_bytes())

    def test_waiting_human_and_bounds_do_not_resume_or_approve(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            run_lifecycle.transition_run(root, run_id, to_state="waiting_human", reason_class="fixture_gate",
                transition="fixture", principal=self.actor)
            before = run_store.run_path(root, run_id).read_bytes()
            with mock.patch.object(provider_runner, "execute_provider", side_effect=AssertionError("forbidden")):
                self.assertEqual("waiting_human", self.drive(root, run_id=run_id)["stop"])
            self.assertEqual(before, run_store.run_path(root, run_id).read_bytes())

    def test_duration_and_invalid_selectors(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            self.assertEqual("duration_exhausted", self.drive(root, run_id=run_id, duration_seconds=0.1)["reason_class"])
            for opts in ({}, {"run_id": run_id, "request_id": "req-chain"},
                         {"run_id": run_id, "max_iterations": 0}, {"run_id": run_id, "duration_seconds": float("nan")}):
                with self.assertRaises(frontdoor.FrontdoorError): self.drive(root, **opts)

    def test_existing_gate_failure_stops_without_final_producer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); request = self.approve(root)
            result = frontdoor.drive_run(state_root=root, request_id=request, principal=self.actor,
                                         fake_provider_mode="blocked")
            self.assertIn(result["stop"], {"waiting_human", "terminal", "blocked"})
            self.assertNotEqual("complete", result["run_state"])
            self.assertFalse(report_gate.report_path(root, result["run_id"], "final_evidence").exists())

    def test_pending_and_publication_routes_are_not_dispatched(self):
        for state, step, expected in (("remediating", "research", "integration_pending"),
                                      ("step_queued", "publication_gate", "waiting_human")):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); run_id = self.prepared(root)
                run = run_store.load_run(root, run_id); run.update(run_state=state, current_step=step)
                # These future states have no current readonly producer; selection-only negative.
                with mock.patch.object(run_store, "load_run", return_value=run), \
                     mock.patch.object(provider_runner, "execute_provider", side_effect=AssertionError("forbidden")):
                    self.assertEqual(expected, self.drive(root, run_id=run_id)["stop"])

    def test_unapproved_request_and_bridge_cannot_drive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            with self.assertRaises(frontdoor.FrontdoorError):
                frontdoor.drive_run(state_root=root, run_id=run_id,
                    principal={"principal_type": "main_agent_bridge", "principal_id": "untrusted", "authn_method": "local_cli"})
            record = frontdoor.read_json(frontdoor.request_path(root, "req-chain"))
            record.pop("approved_activation")
            frontdoor.write_json(frontdoor.request_path(root, "req-chain"), record)
            with self.assertRaises(frontdoor.FrontdoorError): self.drive(root, request_id="req-chain")

    def test_same_run_concurrent_claim_and_cross_run_concurrency(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            other = self.approve(root, "other")
            entered, release = threading.Event(), threading.Event()
            execute = provider_runner.execute_provider
            def slow(**kwargs):
                entered.set()
                self.assertTrue(release.wait(10))
                return execute(**kwargs)
            with mock.patch.object(provider_runner, "execute_provider", side_effect=slow) as calls, \
                 concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                running = pool.submit(self.drive, root, run_id=run_id)
                try:
                    self.assertTrue(entered.wait(10))
                    before = run_store.load_run(root, run_id)["provider_execution"]["retry"].copy()
                    second = self.drive(root, run_id=run_id)
                    self.assertEqual("waiting_provider", second["stop"], second)
                    self.assertEqual(before, run_store.load_run(root, run_id)["provider_execution"]["retry"])
                    blocked = self.drive(root, request_id=other)
                    self.assertEqual("blocked", blocked["stop"], blocked)
                    self.assertEqual(1, calls.call_count)
                finally:
                    release.set()
                self.assertEqual("complete", running.result()["run_state"])
                self.assertEqual(2, calls.call_count)
            self.assertEqual("complete", self.drive(root, request_id=other)["run_state"])

    def test_journal_restart_does_not_reexecute_completed_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            write = provider_runner.private_atomic_write_json
            def crash(state_root, path, payload):
                result = write(state_root, path, payload)
                if payload.get("attempt_result_version") == "1": raise RuntimeError("fixture_crash")
                return result
            with mock.patch.object(provider_runner, "execute_provider", wraps=provider_runner.execute_provider) as calls:
                with mock.patch.object(provider_runner, "private_atomic_write_json", side_effect=crash):
                    with self.assertRaisesRegex(RuntimeError, "fixture_crash"): self.drive(root, run_id=run_id)
                self.assertEqual(1, calls.call_count)
                run = run_store.load_run(root, run_id)
                run["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
                run_store.store_run(root, run, expected_current_state="waiting_provider")
                self.assertEqual("complete", self.drive(root, run_id=run_id)["run_state"])
                self.assertEqual(2, calls.call_count)  # one original research, one review

    def test_persisted_retry_exhaustion_and_stricter_cap(self):
        for cap in (5, 2):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); run_id = self.prepared(root)
                with mock.patch.object(provider_runner, "execute_provider", side_effect=RuntimeError("fixture_crash")):
                    with self.assertRaisesRegex(RuntimeError, "fixture_crash"): self.drive(root, run_id=run_id)
                run = run_store.load_run(root, run_id); execution = run["provider_execution"]
                execution["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
                fingerprint = "sha256:" + hashlib.sha256(run_lifecycle.canonical_json(
                    {"adapter_id": execution["adapter_id"], "reason_class": "provider_lease_expired"})).hexdigest()
                execution["retry"].update(max_auto_retries=cap, auto_retries_used=cap, last_failure_fingerprint=fingerprint)
                run_store.store_run(root, run, expected_current_state="waiting_provider")
                with mock.patch.object(provider_runner, "execute_provider", side_effect=AssertionError("forbidden")):
                    result = self.drive(root, run_id=run_id)
                    self.assertEqual("provider_retry_exhausted", result["reason_class"], result)
                    self.assertEqual("waiting_human", result["stop"])
                    again = self.drive(root, run_id=run_id)
                    self.assertEqual("waiting_human", again["stop"])
                self.assertEqual(cap, run_store.load_run(root, run_id)["provider_execution"]["retry"]["auto_retries_used"])

    def test_unexhausted_stricter_cap_survives_actual_reclaim(self):
        for used in (0, 1):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); run_id = self.prepared(root)
                with mock.patch.object(provider_runner, "execute_provider", side_effect=RuntimeError("fixture_crash")):
                    with self.assertRaisesRegex(RuntimeError, "fixture_crash"): self.drive(root, run_id=run_id)
                run = run_store.load_run(root, run_id); execution = run["provider_execution"]
                execution["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
                fingerprint = "sha256:" + hashlib.sha256(run_lifecycle.canonical_json(
                    {"adapter_id": execution["adapter_id"], "reason_class": "provider_lease_expired"})).hexdigest()
                execution["retry"].update(max_auto_retries=2, auto_retries_used=used, last_failure_fingerprint=fingerprint)
                run_store.store_run(root, run, expected_current_state="waiting_provider")
                with mock.patch.object(provider_runner, "execute_provider", side_effect=RuntimeError("reclaim_crash")):
                    with self.assertRaisesRegex(RuntimeError, "reclaim_crash"): self.drive(root, run_id=run_id)
                retry = run_store.load_run(root, run_id)["provider_execution"]["retry"]
                self.assertEqual(2, retry["max_auto_retries"])
                self.assertEqual(used + 1, retry["auto_retries_used"])

    def test_alternating_failures_yield_and_keep_total_retry_cap(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            execute = provider_runner.execute_provider
            calls = []
            def alternating(**kwargs):
                calls.append(len(calls))
                if len(calls) > 3: raise RuntimeError("unbounded_retry_detected")
                kwargs["fake_provider_mode"] = "timeout" if len(calls) % 2 else "nonzero"
                return execute(**kwargs)
            with mock.patch.object(provider_runner, "execute_provider", side_effect=alternating):
                first = self.drive(root, run_id=run_id, max_iterations=1)
                self.assertEqual("iteration_exhausted", first["reason_class"])
                self.assertEqual(1, len(calls))
                run = run_store.load_run(root, run_id)
                run["provider_execution"]["retry"]["max_auto_retries"] = 2
                run_store.store_run(root, run, expected_current_state="step_queued")
                second = self.drive(root, run_id=run_id, max_iterations=1)
                self.assertEqual("iteration_exhausted", second["reason_class"])
                self.assertEqual(2, run_store.load_run(root, run_id)["provider_execution"]["retry"]["auto_retries_used"])
                third = self.drive(root, run_id=run_id, max_iterations=1)
                self.assertEqual("waiting_human", third["stop"])
                self.assertEqual(3, len(calls))
                self.assertEqual(2, run_store.load_run(root, run_id)["provider_execution"]["retry"]["auto_retries_used"])

    def test_failed_journal_yields_without_new_call_or_budget_reset(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            frontdoor.drive_run(state_root=root, run_id=run_id, principal=self.actor,
                               fake_provider_mode="timeout", max_iterations=1)
            run = run_store.load_run(root, run_id)
            self.assertEqual("step_queued", run["run_state"])
            run["provider_execution"]["retry"]["max_auto_retries"] = 2
            run_store.store_run(root, run, expected_current_state="step_queued")
            write = provider_runner.private_atomic_write_json
            def crash(state_root, path, payload):
                result = write(state_root, path, payload)
                if payload.get("attempt_result_version") == "1": raise RuntimeError("journal_crash")
                return result
            with mock.patch.object(provider_runner, "private_atomic_write_json", side_effect=crash):
                with self.assertRaisesRegex(RuntimeError, "journal_crash"):
                    frontdoor.drive_run(state_root=root, run_id=run_id, principal=self.actor,
                                       fake_provider_mode="nonzero", max_iterations=1)
            run = run_store.load_run(root, run_id)
            run["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
            run_store.store_run(root, run, expected_current_state="waiting_provider")
            with mock.patch.object(provider_runner, "execute_provider", side_effect=AssertionError("recovery must yield")):
                result = self.drive(root, run_id=run_id, max_iterations=1)
                self.assertEqual("iteration_exhausted", result["reason_class"])
            retry = run_store.load_run(root, run_id)["provider_execution"]["retry"]
            self.assertEqual(2, retry["auto_retries_used"])
            self.assertEqual(2, retry["max_auto_retries"])

    def test_expired_claim_keeps_retry_spent_before_crash(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            frontdoor.drive_run(state_root=root, run_id=run_id, principal=self.actor,
                               fake_provider_mode="timeout", max_iterations=1)
            run = run_store.load_run(root, run_id)
            self.assertEqual("step_queued", run["run_state"])
            run["provider_execution"]["retry"]["max_auto_retries"] = 2
            run_store.store_run(root, run, expected_current_state="step_queued")
            with mock.patch.object(provider_runner, "execute_provider", side_effect=RuntimeError("claim_crash")):
                with self.assertRaisesRegex(RuntimeError, "claim_crash"): self.drive(root, run_id=run_id)
            run = run_store.load_run(root, run_id)
            run["provider_execution"]["lease"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
            run_store.store_run(root, run, expected_current_state="waiting_provider")
            result = frontdoor.drive_run(state_root=root, run_id=run_id, principal=self.actor,
                                        fake_provider_mode="nonzero", max_iterations=1)
            self.assertEqual("waiting_human", result["stop"])
            retry = run_store.load_run(root, run_id)["provider_execution"]["retry"]
            self.assertEqual(2, retry["auto_retries_used"])

    def test_retry_yield_rechecks_duration_before_new_claim(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            execute = provider_runner.execute_provider
            def delayed_failure(**kwargs):
                kwargs["fake_provider_mode"] = "timeout"
                result = execute(**kwargs)
                time.sleep(0.06)
                return result
            with mock.patch.object(provider_runner, "execute_provider", side_effect=delayed_failure) as calls:
                result = self.drive(root, run_id=run_id, duration_seconds=1.05)
                self.assertEqual("duration_exhausted", result["reason_class"])
                self.assertEqual(1, calls.call_count)

    def test_single_provider_command_keeps_default_retry_control_flow(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            execute = provider_runner.execute_provider
            count = 0
            def alternating(**kwargs):
                nonlocal count
                count += 1
                if count > 6: raise AssertionError("default command exceeded total cap")
                kwargs["fake_provider_mode"] = "timeout" if count % 2 else "nonzero"
                return execute(**kwargs)
            with mock.patch.object(provider_runner, "execute_provider", side_effect=alternating):
                result = frontdoor.run_provider(state_root=root, run_id=run_id, principal=self.actor)
            self.assertEqual("blocked", result["decision"])
            self.assertEqual(6, count)  # initial attempt plus five retries, one command
            run = run_store.load_run(root, run_id)
            self.assertEqual("waiting_human", run["run_state"])
            self.assertEqual(5, run["provider_execution"]["retry"]["auto_retries_used"])

    def test_new_step_owns_separate_total_retry_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); run_id = self.prepared(root)
            frontdoor.drive_run(state_root=root, run_id=run_id, principal=self.actor,
                               fake_provider_mode="timeout", max_iterations=1)
            self.drive(root, run_id=run_id, max_iterations=1)  # real research acceptance
            self.assertEqual("review", run_store.load_run(root, run_id)["current_step"])
            self.drive(root, run_id=run_id, max_iterations=1)  # next work order
            frontdoor.drive_run(state_root=root, run_id=run_id, principal=self.actor,
                               fake_provider_mode="timeout", max_iterations=1)
            run = run_store.load_run(root, run_id)
            self.assertEqual("review", run["provider_execution"]["step_id"])
            self.assertEqual(1, run["provider_execution"]["retry"]["auto_retries_used"])
            self.assertEqual("complete", self.drive(root, run_id=run_id)["run_state"])


if __name__ == "__main__":
    unittest.main()
