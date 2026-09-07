"""Real process/Git driver coverage; GitHub transport is explicitly synthetic."""
import concurrent.futures
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_trusted_local_executor as fixture
from test_host_publication_adapter import FakeGitHub
from test_trusted_local_validation_repair import ValidationRepairTests
import trusted_local_driver as driver
import trusted_local_executor as local


class CompletingGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.polls = 0
        self.integrated_polls = 0

    def run(self, args, *, cwd, env=None):
        if args[:3] == ['gh', 'pr', 'checks']:
            self.polls += 1
            self.success = self.polls >= 2
        if any('/commits/' + 'f' * 40 + '/check-runs?' in a for a in args):
            self.calls.append(args)
            self.integrated_polls += 1
            passed = self.integrated_polls >= 2
            return json.dumps([{'check_runs': [{'id': 80, 'name': 'ci', 'head_sha': 'f' * 40,
                'status': 'completed' if passed else 'in_progress',
                'conclusion': 'success' if passed else None}]}]).encode()
        return super().run(args, cwd=cwd, env=env)


class TrustedLocalDriverTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TrustedLocalTests(); self.f.setUp()
        self.f.auth = dataclasses.replace(self.f.auth, publication=dataclasses.replace(
            self.f.auth.publication, required_checks=('ci',)))
        self.commands = CompletingGitHub()

    def tearDown(self):
        self.f.tearDown()

    def drive(self, **kwargs):
        return driver.drive(authorization=self.f.auth, state_root=self.f.state,
                            commands=self.commands, poll_interval_seconds=0, **kwargs)

    def test_one_call_runs_polls_merges_and_checks_integrated_ci(self):
        result = self.drive(request=self.f.request)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual('terminal', result['stop'])
        self.assertEqual(2, result['polls'])
        self.assertEqual(0, result['repairs'])
        self.assertEqual(1, len(list((self.f.state/'trusted-local').glob('*/claim.json'))))
        self.assertEqual(1, sum(c[:3] == ['gh','pr','create'] for c in self.commands.calls))
        self.assertEqual('f'*40, result['merge_commit'])

    def test_existing_non_intake_claim_keeps_legacy_authorization_digest(self):
        local.execute(self.f.request, self.f.auth, self.f.state)
        directory = self.f.state/'trusted-local'/self.f.auth.publication.execution_id
        claim = json.loads((directory/'claim.json').read_text())
        legacy = dataclasses.asdict(self.f.auth); legacy.pop('intake_digest')
        self.assertEqual(local.publication.digest(legacy), claim['authorization_digest'])
        with patch.object(local, '_run_process', side_effect=AssertionError('existing worker replayed')):
            result = self.drive(max_iterations=1)
        self.assertEqual('ci_pending', result['last_status'])
        self.assertEqual(claim['authorization_digest'], json.loads((directory/'drive.json').read_text())['authorization_digest'])

    def test_intake_scope_refresh_preserves_host_action_without_human_question(self):
        local.execute(self.f.request, self.f.auth, self.f.state)
        for status,action in [('intake_scope_refresh_required','host_refresh_base_and_hunk_contracts'),
                              ('intake_findings_pending','host_triage_or_resolve_recorded_findings')]:
            response = {'status': status, 'next_action': action, 'intake_digest': 'sha256:' + 'a'*64}
            with patch.object(local, 'advance_publication', return_value=response), \
                 patch.object(local, '_run_process', side_effect=AssertionError('worker replayed')):
                result = self.drive()
            self.assertEqual(response['status'], result['status'])
            self.assertEqual(response['next_action'], result['next_action'])
            self.assertEqual('blocked', result['stop']); self.assertTrue(result['resumable'])
            self.assertEqual(0, result['polls'])

    def test_pending_bound_and_resume_never_replays_worker(self):
        first = self.drive(request=self.f.request, max_iterations=2)
        self.assertEqual('iteration_exhausted', first['status'])
        self.assertEqual('ci_pending', first['last_status'])
        self.assertTrue(first['resumable'])
        claim = self.f.state/'trusted-local'/self.f.auth.publication.execution_id/'claim.json'
        before = claim.read_bytes()
        with patch.object(local, '_run_process', side_effect=AssertionError('worker replay')):
            done = self.drive()
            self.assertEqual('complete', done['status'], done)
            self.assertEqual('complete', self.drive()['status'])
        self.assertEqual(before, claim.read_bytes())

    def test_actual_cli_entry_initial_request(self):
        authority = self.f.root/'authority.json'
        authority.write_text(json.dumps(dataclasses.asdict(self.f.auth))); authority.chmod(0o600)
        request = self.f.root/'request.json'; request.write_text(json.dumps(self.f.request))
        cli = Path(__file__).resolve().parents[4]/'scripts'/'saihai.py'
        # One real CLI invocation, one real subprocess and validation. A ceiling
        # before publication keeps this CLI test entirely offline without a shim.
        wrapper = 'import sys,runpy; from pathlib import Path; sys.path.insert(0,sys.argv.pop(1)); import vault_task_records as v; root=Path(sys.argv.pop(1)); v.canonical_root=lambda:root; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
        result = subprocess.run([sys.executable, '-c', wrapper,
            str(cli.parents[1]/'organization/runtime/workflows/scripts'), str(self.f.vault), str(cli), 'usage', 'drive',
            '--authorization', str(authority), '--request', str(request), '--state-root', str(self.f.state),
            '--max-iterations', '1'], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual('validated', payload['last_status'])
        self.assertEqual('bounded', payload['stop'])

    def test_changed_authority_or_request_cannot_resume(self):
        self.drive(request=self.f.request, max_iterations=1)
        with self.assertRaisesRegex(local.TrustedLocalError, 'drive_authority_changed'):
            driver.drive(authorization=dataclasses.replace(self.f.auth, model='other'), state_root=self.f.state)
        with self.assertRaisesRegex(local.TrustedLocalError, 'drive_request_changed'):
            self.drive(request=dict(self.f.request, instruction='changed'))

    def test_lock_contention_cannot_overwrite_other_authority_receipt(self):
        self.drive(request=self.f.request, max_iterations=1)
        receipt = self.f.state/'trusted-local'/self.f.auth.publication.execution_id/'drive.json'
        before = receipt.read_bytes()
        with patch.object(driver.run_lock, 'hold_global_lock', side_effect=driver.run_lock.LockContentionError()):
            result = driver.drive(authorization=dataclasses.replace(self.f.auth, model='other'),
                                  state_root=self.f.state)
        self.assertEqual('lock_contention', result['status'])
        self.assertEqual(before, receipt.read_bytes())

    def test_claim_without_process_receipt_requires_inspection(self):
        with patch.object(local, '_run_process', side_effect=RuntimeError('crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                self.drive(request=self.f.request)
        with patch.object(local, '_run_process', side_effect=AssertionError('worker replay')):
            stopped = self.drive()
        self.assertEqual('execution_incomplete_inspection_required', stopped['status'])

    def test_automatic_validation_repair_uses_original_authority(self):
        repair = ValidationRepairTests(); repair.setUp()
        try:
            result = driver.drive(authorization=repair.f.auth, state_root=repair.f.state,
                request=repair.f.request, commands=CompletingGitHub(), poll_interval_seconds=0)
            self.assertEqual('complete', result['status'], result)
            self.assertEqual(1, result['repairs'])
            progress = json.loads((repair.directory/'validation-repair.json').read_text())
            self.assertEqual('validated', progress['status'])
            self.assertEqual(1, progress['attempt'])
            self.assertEqual('failed', json.loads((repair.directory/'validation.json').read_text())['status'])
        finally:
            repair.tearDown()

    def test_future_completion_receipt_is_not_lost_or_marked_complete(self):
        self.drive(request=self.f.request, max_iterations=1)
        receipt = {'status': 'complete', 'completion_persistence': {'status': 'pending'},
                   'continuation': {'operation': 'usage advance'}}
        with patch.object(local, 'advance_publication', return_value=receipt):
            stopped = self.drive()
        self.assertNotEqual('complete', stopped['status'])
        self.assertEqual(receipt['continuation'], stopped['continuation'])
        self.assertTrue(stopped['resumable'])

    def test_intake_and_installation_receipts_survive_driver_summary(self):
        self.drive(request=self.f.request, max_iterations=1)
        receipt={'status':'complete','intake_digest':'sha256:'+'2'*64,
                 'effective_installation':{'status':'installed_bytes_verified','active_runtime':'not_proven'},
                 'canonical_sync':{'status':'synced','dependent_base':'a'*40}}
        with patch.object(local,'advance_publication',return_value=receipt):
            result=self.drive()
        for key in ('intake_digest','effective_installation','canonical_sync'):
            self.assertEqual(result[key],receipt[key])
    def test_completion_failure_and_unknown_status_stop(self):
        self.drive(request=self.f.request, max_iterations=1)
        for status in ('failed', 'unknown'):
            with self.subTest(status=status), patch.object(local, 'advance_publication',
                    return_value={'status': 'complete', 'completion_persistence': {'status': status}}):
                result = self.drive()
                self.assertEqual('blocked', result['stop'])
                self.assertEqual('completion_persistence_' + status, result['status'])

    def test_concurrent_invocations_serialize_same_claim(self):
        entered, release = threading.Event(), threading.Event()
        original = local._run_process
        def slow(*args, **kwargs):
            entered.set(); self.assertTrue(release.wait(5)); return original(*args, **kwargs)
        with patch.object(local, '_run_process', side_effect=slow) as process, \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(self.drive, request=self.f.request, max_iterations=1)
            self.assertTrue(entered.wait(5))
            two = pool.submit(self.drive, request=self.f.request, max_iterations=1)
            release.set(); one.result(); two.result()
            self.assertEqual(1, process.call_count)

    def test_bounds_and_state_outside_worker_scope(self):
        for values in ({'max_iterations': 0}, {'duration_seconds': float('nan')},
                       {'poll_interval_seconds': -1}):
            with self.assertRaisesRegex(local.TrustedLocalError, 'drive_bounds_invalid'):
                driver.drive(authorization=self.f.auth, state_root=self.f.state, **values)
        with self.assertRaisesRegex(local.TrustedLocalError, 'drive_state_root_invalid'):
            driver.drive(authorization=self.f.auth, state_root=self.f.repo/'unsafe')


if __name__ == '__main__':
    unittest.main()
