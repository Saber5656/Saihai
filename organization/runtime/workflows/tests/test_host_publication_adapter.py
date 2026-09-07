"""Host boundary tests: real temporary Git, synthetic GitHub, no network."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import host_publication_adapter as adapter


class FakeGitHub(adapter.Commands):
    def __init__(self):
        self.calls = []
        self.pr_exists = False
        self.success = False
        self.stale = False
        self.merge_head = None
        self.failure = False

    def run(self, args, *, cwd, env=None):
        self.calls.append(args)
        if args[:2] == ['git', 'push']:
            return b''
        if args[0] == 'git':
            return super().run(args, cwd=cwd, env=env)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd).decode().strip()
        if args[1:3] == ['pr', 'list']:
            value = [{'number': 9, 'headRefOid': head}] if self.pr_exists else []
        elif args[1:3] == ['pr', 'create']:
            self.pr_exists = True
            return b'https://github.com/example/repo/pull/9'
        elif args[1:3] == ['pr', 'checks']:
            value = [{'name': 'ci', 'state': 'SUCCESS' if self.success else 'IN_PROGRESS'}]
        elif '--method' in args:
            assert 'sha=' + head in args
            self.merge_head = head
            value = {'merged': True, 'sha': 'f' * 40}
        elif any('/rules/branches/' in a for a in args):
            value = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'ci'}]}}]
        elif any('/check-runs?' in a for a in args):
            value = [{'check_runs': [{'id': 5, 'name': 'ci', 'head_sha': head,
                                     'status': 'completed' if self.success else 'in_progress',
                                     'conclusion': 'failure' if self.failure else 'success' if self.success else None}]}]
        elif any('/statuses?' in a for a in args):
            value = [[]]
        else:
            value = {'state': 'open', 'merged': False, 'mergeable': True,
                     'head': {'sha': '0' * 40 if self.stale else head, 'repo': {'full_name': 'example/repo'}},
                     'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {'full_name': 'example/repo'}}}
        return json.dumps(value).encode()


class HostPublicationTests(unittest.TestCase):
    def test_failed_gh_checks_json_is_persisted_as_ci_failed(self):
        original=self.commands.run
        def run(args,*,cwd,env=None):
            if args[:3]==['gh','pr','checks']:
                response=subprocess.CompletedProcess(args,1,b'[{"name":"ci","state":"FAILURE"}]',b'')
                with patch.object(adapter.subprocess,'run',return_value=response):
                    return adapter.Commands().run(args,cwd=cwd,env=env)
            return original(args,cwd=cwd,env=env)
        with patch.object(self.commands,'run',side_effect=run):
            result=self.publish()
        self.assertEqual(result['status'],'ci_failed')
        self.assertIsNone(self.commands.merge_head)
        self.assertTrue(any(json.loads(p.read_text()).get('status')=='ci_failed' for p in (self.root/'state').glob('*.json')))

    def test_gh_failure_without_valid_check_rows_still_blocks(self):
        for output in (b'',b'not json',b'{"message":"auth failed"}',b'[]',b'[{}]'):
            response=subprocess.CompletedProcess([],1,output,b'private error')
            with patch.object(adapter.subprocess,'run',return_value=response):
                with self.assertRaisesRegex(adapter.PublicationError,'command_failed:gh'):
                    adapter.Commands().run(['gh','pr','checks','9','--json','name,state'],cwd=self.repo)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.repo, stderr=subprocess.DEVNULL).decode().strip()
        self.git = git
        git('init', '-b', 'codex/task')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/example/repo.git')
        (self.repo / 'app.txt').write_text('before\n')
        git('add', 'app.txt'); git('commit', '-m', 'fixture')
        head = git('rev-parse', 'HEAD')
        (self.repo / 'app.txt').write_text('after\n')
        self.auth = adapter.HostAuthorization('task-128', 'req-1', 'run-1', 'exec-1', 'example/repo',
            str(self.repo), 'codex/task', head, head, ('app.txt',), ('ci',), 'sha256:' + 'c' * 64, 'parent-task-authority')
        identity = adapter.snapshot(self.repo, ['app.txt'])
        evidence = self.root / 'validation.json'
        import host_validation
        command = [sys.executable, '-c', 'assert True']
        done = subprocess.run(command, capture_output=True)
        row = dict(argv=command, exit=done.returncode, started_at_epoch=1, ended_at_epoch=2,
            stdout_digest=adapter.digest(done.stdout), stderr_digest=adapter.digest(done.stderr),
            command_digest=host_validation.command_digest(command),
            **host_validation.observe(command, done.stdout, done.stderr, done.returncode))
        evidence.write_text(json.dumps(dict(validation_version=2, status='passed', execution_id='exec-1', **identity,
            source_digest=host_validation.source_digest(self.repo), plan_digest=host_validation.digest([command]), commands=[row],
            profile_reference=None, delivery_profile={'state':'host_commands','profile_digest':None})))
        process = self.root / 'process.json'
        process.write_text('{"exit":0,"execution_id":"exec-1"}')
        self.report = dict(version='1', profile='trusted_local_v1',
            **{k: getattr(self.auth, k) for k in ('task_id','request_id','run_id','execution_id','repository','worktree','branch','head','base')},
            approved_scope_digest=self.auth.scope_digest, changed_paths=['app.txt'], result='completed',
            publication_allowed=False, **identity,
            execution=dict(actor_kind='agent_under_explicit_user_task_authority', authority_evidence_ref='parent-task-authority',
                           process_evidence_path=str(process), process_evidence_digest=adapter.digest(process.read_bytes())),
            validation=dict(status='passed', evidence_path=str(evidence), evidence_digest=adapter.digest(evidence.read_bytes())))
        self.commands = FakeGitHub()

    def tearDown(self):
        self.temp.cleanup()

    def publish(self):
        return adapter.publish(self.report, self.auth, self.root / 'state', commands=self.commands)

    def test_commit_pr_pending_resume_head_pinned_merge(self):
        first = self.publish()
        self.assertEqual(first['status'], 'ci_pending')
        self.assertNotEqual(first['head'], self.auth.head)
        self.commands.success = True
        final = self.publish()
        self.assertEqual(final['status'], 'merged')
        self.assertEqual(self.commands.merge_head, first['head'])
        self.assertEqual(self.publish(), final)
        self.assertEqual(sum(c[:2] == ['git', 'commit'] for c in self.commands.calls), 1)
        self.assertFalse(any('--force' in c or '--admin' in c for c in self.commands.calls))
        self.assertTrue(all('main' not in c for c in self.commands.calls if c[:2] == ['git','push']))

    def test_worker_permission_and_untrusted_auth_rejected(self):
        self.report['publication_allowed'] = True
        with self.assertRaises(adapter.PublicationError): self.publish()
        self.assertEqual(self.commands.calls, [])
        with self.assertRaises(adapter.PublicationError): adapter.validate_report({}, {})

    def test_modified_or_wrong_validation_rejected(self):
        evidence = Path(self.report['validation']['evidence_path'])
        evidence.write_text('{}')
        with self.assertRaisesRegex(adapter.PublicationError, 'evidence_changed'): self.publish()
        self.assertEqual(self.commands.calls, [])

    def test_scope_escape_and_stale_source_rejected(self):
        self.report['changed_paths'] = ['../outside']
        with self.assertRaises(adapter.PublicationError): self.publish()
        self.report['changed_paths'] = ['app.txt']
        (self.repo / 'app.txt').write_text('unvalidated\n')
        with self.assertRaisesRegex(adapter.PublicationError, 'current_source_changed'): self.publish()
        self.assertFalse(any(c[0] == 'gh' for c in self.commands.calls))

    def test_unrelated_dirty_and_real_index_preserved(self):
        (self.repo / 'other.txt').write_text('unrelated')
        before = self.git('diff', '--cached')
        with self.assertRaisesRegex(adapter.PublicationError, 'current_source_changed'): self.publish()
        self.assertEqual(self.git('diff', '--cached'), before)
        self.assertEqual((self.repo / 'other.txt').read_text(), 'unrelated')

    def test_stale_remote_head_never_merges(self):
        self.commands.stale = True
        self.commands.success = True
        with self.assertRaisesRegex(adapter.PublicationError, 'pull_request_identity_changed'): self.publish()
        self.assertIsNone(self.commands.merge_head)

    def test_uncertain_commit_is_reconciled_without_second_commit(self):
        original = self.commands.run
        def uncertain(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ['git', 'commit']:
                raise adapter.PublicationError('simulated lost commit response')
            return result
        self.commands.run = uncertain
        with self.assertRaises(adapter.PublicationError): self.publish()
        self.commands.run = original
        self.assertEqual(self.publish()['status'], 'ci_pending')
        self.assertEqual(sum(c[:2] == ['git', 'commit'] for c in self.commands.calls), 1)

    def test_remote_change_blocks_before_commit(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com/other/repo.git')
        with self.assertRaisesRegex(adapter.PublicationError, 'remote_changed'): self.publish()
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.auth.head)

    def test_failed_required_check_never_merges(self):
        self.commands.success = True
        self.commands.failure = True
        self.assertEqual(self.publish()['status'], 'ci_failed')
        self.assertIsNone(self.commands.merge_head)

    def test_process_receipt_wrong_execution_rejected(self):
        evidence = Path(self.report['execution']['process_evidence_path'])
        evidence.write_text('{"exit":0,"execution_id":"different"}')
        self.report['execution']['process_evidence_digest'] = adapter.digest(evidence.read_bytes())
        with self.assertRaisesRegex(adapter.PublicationError, 'host_process_identity_or_exit_invalid'): self.publish()
        self.assertEqual(self.commands.calls, [])

    def test_sensitive_scope_requires_separate_receipt(self):
        auth = dataclasses.replace(self.auth, risk_kind='permission_expansion')
        with self.assertRaisesRegex(adapter.PublicationError, 'scope_review_required'):
            adapter.validate_report(self.report, auth)


if __name__ == '__main__':
    unittest.main()
