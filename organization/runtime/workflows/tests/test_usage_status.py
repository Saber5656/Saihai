"""Self-contained saved-status CLI tests; no worker, Git or network calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[4]


class UsageStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.execution_id = 'EXE-status-fixture-1'
        self.directory = self.state / 'trusted-local' / self.execution_id
        for directory in (self.state, self.state / 'trusted-local', self.directory):
            directory.mkdir(mode=0o700)
        catalog = self.root / 'directory-path.env'
        keys = ('SAIHAI_ROOT', 'AGENTS_VAULT_ROOT', 'USER_VAULT_ROOT',
                'SKILLS_REPO_ROOT', 'SKILLS_ROOT', 'DOTFILES_ROOT', 'DEV_ROOT',
                'DEV_WORKTREES_ROOT', 'TASK_WORKTREE_ROOT')
        catalog.write_text(''.join(f'{key}={self.root}\n' for key in keys))
        self.env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
                    'SAIHAI_DIRECTORY_PATH_ENV': str(catalog)}
        self.write('claim', {'profile': 'trusted_local_v1'})
        self.write('activation', {'status': 'authorized_by_user_task'})

    def write_text(self, name, value, directory=None):
        path = (directory or self.directory) / (name + '.json')
        path.write_text(value)
        path.chmod(0o600)

    def write(self, name, value, directory=None):
        self.write_text(name, json.dumps(value), directory)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (
            p.lstat().st_mode, p.lstat().st_mtime_ns,
            os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
            for p in [self.root, *self.root.rglob('*')]}

    def status(self, execution_id=None, state=None, success=True):
        before = self.snapshot()
        result = subprocess.run(
            [sys.executable, '-B', str(REPO / 'scripts/saihai.py'), 'usage', 'status',
             '--execution-id', execution_id if execution_id is not None else self.execution_id,
             '--state-root', str(state if state is not None else self.state)],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(self.snapshot(), before, 'status mutated fixture files or directories')
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        if success:
            self.assertEqual(payload['profile'], 'trusted_local_v1')
            self.assertTrue(payload['next_action'])
        else:
            self.assertEqual(payload['decision'], 'blocked')
        return payload

    def validated(self):
        self.write('process', {'exit': 0, 'process_start_token': 'fixture-token'})
        self.write('validation', {'status': 'passed'})
        self.write('outcome', {'status': 'validated'})

    def test_running_and_read_only(self):
        # An empty worker result is normal while the process is still writing it.
        self.write_text('worker-result', '')
        for _ in range(2):
            payload = self.status()
            self.assertEqual(payload['intake']['status'], 'authorized_by_user_task')
            self.assertEqual(payload['execution']['status'], 'running')
            self.assertEqual(payload['validation']['status'], 'pending')
            self.assertEqual(payload['publication']['status'], 'not_started')
            self.assertIn('wait for worker', payload['next_action'])

    def test_validation_failed(self):
        self.validated()
        self.write('validation', {'status': 'failed', 'commands': [{'exit': 1}]})
        self.write('outcome', {'status': 'blocked', 'reason': 'host_validation_failed'})
        payload = self.status()
        self.assertEqual(payload['validation']['status'], 'failed')
        self.assertIn('fix validation', payload['next_action'])

    def test_ci_pending_and_complete(self):
        self.validated()
        for state, action in [('ci_pending', 'wait for required PR CI'),
                              ('integrated_ci_pending', 'wait for integrated CI'),
                              ('complete', 'none')]:
            with self.subTest(state=state):
                self.write('publication', {'status': state, 'pr': 161})
                payload = self.status()
                self.assertEqual(payload['publication']['status'], state)
                self.assertEqual(payload['publication']['pr'], 161)
                self.assertIn(action, payload['next_action'])

    def test_validated_next_step(self):
        self.validated()
        self.assertIn('usage advance', self.status()['next_action'])

    def test_unknown_and_missing_root_are_not_created(self):
        self.assertIn('unknown_execution_id', self.status('unknown-id', success=False)['reason'])
        missing = self.root / 'absent-state'
        self.status(state=missing, success=False)
        self.assertFalse(missing.exists())

    def test_malformed_ids(self):
        for value in ('', '../escape', '/absolute', 'with/slash', 'with space'):
            with self.subTest(value=value):
                self.assertIn('invalid_execution_id', self.status(value, success=False)['reason'])

    def test_corrupt_record_is_clear_error(self):
        self.write_text('validation', '{')
        self.assertIn('status_record_unreadable: validation', self.status(success=False)['reason'])
        self.write('validation', [])
        self.assertIn('status_record_invalid: validation', self.status(success=False)['reason'])

    def test_current_repair_evidence_and_read_only(self):
        self.write('process', {'exit': 0, 'process_start_token': 'original-token'})
        self.write('validation', {'status': 'failed'})
        self.write('outcome', {'status': 'blocked', 'reason': 'host_validation_failed'})
        self.write('report', {'result': 'original-failed'})
        child_id = self.execution_id + '-repair-1'
        child = self.directory.parent / child_id
        child.mkdir(mode=0o700)
        # Intake and publication in the child must not replace the original records.
        self.write('activation', {'status': 'wrong-intake'}, child)
        self.write('publication', {'status': 'complete'}, child)
        for state in ('running', 'failed', 'validated'):
            with self.subTest(state=state):
                self.write('validation-repair', {'execution_id': child_id, 'status': state})
                if state != 'running':
                    self.write('process', {'exit': 0, 'process_start_token': 'child-token'}, child)
                    self.write('validation', {'status': 'failed' if state == 'failed' else 'passed'}, child)
                    self.write('outcome', {'status': 'blocked' if state == 'failed' else 'validated',
                                           'reason': 'child-failure' if state == 'failed' else None}, child)
                if state == 'validated':
                    self.write('report', {'result': 'completed'}, child)
                for _ in range(2):
                    payload = self.status()
                    self.assertEqual(payload['execution_id'], self.execution_id)
                    self.assertEqual(payload['current_execution_id'], child_id)
                    self.assertEqual(payload['intake']['status'], 'authorized_by_user_task')
                    self.assertEqual(payload['publication']['status'], 'not_started')
                    self.assertEqual(payload['execution']['status'], 'running' if state == 'running' else 'completed')
                    self.assertEqual(payload['validation']['status'],
                                     {'running': 'pending', 'failed': 'failed', 'validated': 'passed'}[state])
                    self.assertEqual(payload['execution']['reason'], 'child-failure' if state == 'failed' else None)
                    self.assertEqual(payload['report']['status'], 'completed' if state == 'validated' else 'not_available')
                    self.assertIn({'running': 'wait for worker', 'failed': 'fix validation',
                                   'validated': 'usage advance'}[state], payload['next_action'])
                    if state == 'validated':
                        self.assertEqual(payload['report']['path'], str(child / 'report.json'))
        self.write('publication', {'status': 'ci_pending', 'pr': 161})
        self.assertEqual(self.status()['publication']['status'], 'ci_pending')

    def test_malformed_repair_pointer(self):
        for value in ('../escape', '/absolute', '', None):
            with self.subTest(value=value):
                self.write('validation-repair', {'execution_id': value, 'status': 'running'})
                self.assertIn('status_record_invalid: validation-repair',
                              self.status(success=False)['reason'])
        self.write('validation-repair', {'execution_id': self.execution_id, 'status': 'unknown'})
        self.assertIn('status_record_invalid: validation-repair status', self.status(success=False)['reason'])

    def test_nonprivate_record_is_rejected(self):
        (self.directory / 'claim.json').chmod(0o644)
        self.assertIn('status_record_unreadable: claim', self.status(success=False)['reason'])

    def test_symlink_record_is_rejected(self):
        target = self.directory / 'validation.json'
        for destination in ('claim.json', 'missing.json'):
            with self.subTest(destination=destination):
                target.symlink_to(destination)
                self.assertIn('status_record_unreadable: validation',
                              self.status(success=False)['reason'])
                target.unlink()

    def test_symlink_execution_directory_is_rejected(self):
        alias = self.directory.parent / 'EXE-alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.assertIn('status_directory_unreadable',
                      self.status('EXE-alias', success=False)['reason'])
        self.write('validation-repair', {'execution_id': 'EXE-alias', 'status': 'running'})
        self.assertIn('status_directory_unreadable', self.status(success=False)['reason'])

    def test_nonprivate_directory_is_rejected(self):
        self.directory.chmod(0o755)
        self.assertIn('status_directory_not_private', self.status(success=False)['reason'])

    def test_directory_record_is_rejected(self):
        (self.directory / 'validation.json').mkdir(mode=0o700)
        self.assertIn('status_record_unreadable: validation', self.status(success=False)['reason'])

    def test_empty_repair_record_is_invalid(self):
        self.write('validation-repair', {})
        self.assertIn('status_record_invalid: validation-repair execution_id',
                      self.status(success=False)['reason'])


if __name__ == '__main__':
    unittest.main()
