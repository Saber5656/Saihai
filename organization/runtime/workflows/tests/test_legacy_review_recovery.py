"""Legacy-format producer fixtures; recovery never rewrites stored execution evidence."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_scoped_worker_executor as fixture
import legacy_review_recovery as recovery
import provider_runner
import run_store

worker = fixture.executor
frontdoor = fixture.frontdoor


class LegacyReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = fixture.create_repo(self.root)
        self.state, self.cap = fixture.derive_e2e_capability(self.root, self.repo)
        self.run_id = self.cap['run_id']
        original = run_store.atomic_write_json
        def old_producer(path, payload):
            # Produce the old format before first persistence and hashing, not
            # by modifying an already recorded execution after the fact.
            if isinstance(payload, dict) and payload.get('evidence_version') == '1':
                payload.pop('review_context', None)
            return original(path, payload)
        with patch.object(run_store, 'atomic_write_json', side_effect=old_producer):
            fixture.execute_capability(state_root=self.state, capability_id=self.cap['capability_id'],
                principal=fixture.EXECUTOR, gateway_principal=fixture.GATEWAY,
                signing_key=fixture.SIGNING_KEY, runner=fixture.FakeCodexRunner(), now_epoch=1_800_000_010)
        self.tree = Path(self.cap['worktree']['worktree_path'])
        self.run = run_store.load_run(self.state, self.run_id)
        self.owner = frontdoor.default_manual_principal()
        self.bundle = worker.completed_review_evidence(self.state, self.run)
        self.path = recovery._path(self.state, self.run, self.bundle[1])
        self.originals = {p: p.read_bytes() for folder in ('worker-executions', 'worker-evidence', 'worker-capabilities', 'runs')
                          for p in (self.state/folder).rglob('*.json')}

    def tearDown(self):
        self.tmp.cleanup()

    def recover(self):
        return frontdoor.recover_review_context(state_root=self.state, run_id=self.run_id)

    def assert_preserved(self):
        for path, content in self.originals.items():
            self.assertEqual(content, path.read_bytes(), str(path))

    def test_explicit_current_snapshot_then_initial_review_without_reexecution(self):
        with self.assertRaisesRegex(worker.ScopedWorkerError, 'review_execution_context_missing'):
            worker.load_completed_review_context(self.state, self.run)
        (self.tree/'README.md').write_text('Current content after the legacy execution.\n')
        first = self.recover()
        self.assertEqual('created', first['status'])
        self.assertEqual(first['recovery_id'], self.recover()['recovery_id'])
        snapshot = run_store.read_json(self.path)
        self.assertEqual('current_content_review_recovery', snapshot['kind'])
        self.assertTrue(any('Current content' in r['content'] for r in snapshot['context']))
        self.assert_preserved()
        budget = self.run['activation']['activation_scope']['step_budget']
        order = frontdoor.drain_run(state_root=self.state, run_id=self.run_id)['work_order']
        self.assertEqual('review', order['step_id'])
        result = provider_runner.run_provider(state_root=self.state, run_id=self.run_id,
            adapter_id='codex_cli_openai_p0', fake_provider_mode='success', principal=self.owner)
        self.assertEqual('ok', result['decision'], result)
        current = run_store.load_run(self.state, self.run_id)
        self.assertEqual(budget, current['activation']['activation_scope']['step_budget'])
        self.assertEqual(snapshot['context'], worker.load_completed_review_context(self.state, current))
        self.assertEqual(self.originals[next(p for p in self.originals if p.parent.name == 'worker-capabilities')],
                         next(p for p in self.originals if p.parent.name == 'worker-capabilities').read_bytes())

    def test_actual_cli_and_repeat_preserve_receipts(self):
        from test_frontdoor_orchestrator import run_frontdoor
        # Existing fixture binds the private test state root; the actual parser
        # and handler run in a separate process.
        result = run_frontdoor(self.state, 'recover-review-context', '--run-id', self.run_id, check=False)
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertEqual('created', json.loads(result.stdout)['status'])
        self.assert_preserved()

    def test_sealed_review_cannot_create_recovery(self):
        run_store.atomic_write_json(self.state/'work-orders'/self.run_id/'review-snapshot-2.json', {'sealed': True})
        with self.assertRaisesRegex(frontdoor.FrontdoorError, 'review_recovery_already_sealed'):
            self.recover()
        self.assertFalse(self.path.exists())
        self.assert_preserved()

    def test_scope_or_identity_change_is_rejected(self):
        for field in ('task_id', 'scope'):
            run = copy.deepcopy(self.run)
            if field == 'task_id': run[field] = 'TSK-other'
            else: run['activation']['activation_scope']['step_budget'] += 1
            run_store.atomic_write_json(self.state/'runs'/f'{self.run_id}.json', run)
            with self.assertRaises((frontdoor.FrontdoorError, worker.ScopedWorkerError)):
                self.recover()
            self.assertFalse(self.path.exists())
        run_store.atomic_write_json(self.state/'runs'/f'{self.run_id}.json', self.run)
        self.assert_preserved()

    def test_content_drift_or_tampering_does_not_replace_snapshot(self):
        self.recover(); before = self.path.read_bytes()
        (self.tree/'README.md').write_text('drift')
        with self.assertRaisesRegex(frontdoor.FrontdoorError, 'review_recovery_content_drift'):
            self.recover()
        self.assertEqual(before, self.path.read_bytes())
        snapshot = json.loads(before); snapshot['context'][0]['content'] = 'forged'
        run_store.atomic_write_json(self.path, snapshot)
        with self.assertRaisesRegex(worker.ScopedWorkerError, 'review_recovery_snapshot_mismatch'):
            worker.load_completed_review_context(self.state, self.run)
        self.assert_preserved()

    def test_modern_or_malformed_context_is_never_replaced(self):
        for context in ([], None, [{'content': 'modern'}]):
            bundle = list(copy.deepcopy(self.bundle)); bundle[3]['review_context'] = context
            with patch.object(worker, 'completed_review_evidence', return_value=tuple(bundle)):
                with self.assertRaisesRegex(frontdoor.FrontdoorError, 'review_recovery_not_legacy'):
                    self.recover()
        self.assertFalse(self.path.exists())
        self.assert_preserved()

    def test_wrong_branch_and_symlink_cannot_be_captured(self):
        subprocess.run(['git','switch','-c','wrong-branch'], cwd=self.tree, check=True, capture_output=True)
        with self.assertRaisesRegex(frontdoor.FrontdoorError, 'worktree_branch_changed'):
            self.recover()
        subprocess.run(['git','switch',self.cap['worktree']['branch']], cwd=self.tree, check=True, capture_output=True)
        (self.tree/'README.md').unlink(); (self.tree/'README.md').symlink_to(self.repo/'README.md')
        with self.assertRaises(frontdoor.FrontdoorError): self.recover()
        self.assertFalse(self.path.exists())

    def test_completed_review_or_unapproved_caller_cannot_recover(self):
        with self.assertRaises(frontdoor.FrontdoorError):
            frontdoor.recover_review_context(state_root=self.state, run_id=self.run_id,
                principal={'principal_type':'main_agent','principal_id':'not-host','authn_method':'local_cli'})
        run = copy.deepcopy(self.run); run['provider_execution'] = {'step_id': 'review'}
        with patch.object(run_store, 'load_run', return_value=run):
            with self.assertRaisesRegex(frontdoor.FrontdoorError, 'review_recovery_not_initial'): self.recover()
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
