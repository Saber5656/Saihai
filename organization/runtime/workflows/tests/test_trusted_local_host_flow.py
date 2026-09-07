"""Host conflict repair and merge-SHA CI continuation with real Git fixtures."""
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_trusted_local_executor as fixture
from test_host_publication_adapter import FakeGitHub
import trusted_local_executor as local
import host_publication_adapter as publication


class FlowGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.conflict = True
        self.integrated_success = False

    def run(self, args, *, cwd, env=None):
        if args[:2] == ['git','fetch']:
            self.calls.append(args); return b''
        result = super().run(args,cwd=cwd,env=env)
        if args[0]=='gh' and any('/pulls/9' == a[-8:] for a in args) and '--method' not in args:
            row=json.loads(result);row['mergeable']=not self.conflict;return json.dumps(row).encode()
        if args[0]=='gh' and any('/commits/'+'f'*40+'/check-runs?' in a for a in args):
            return json.dumps([{'check_runs':[{'id':80,'name':'ci','head_sha':'f'*40,
                'status':'completed' if self.integrated_success else 'in_progress',
                'conclusion':'success' if self.integrated_success else None}]}]).encode()
        return result


class HostFlowTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TrustedLocalTests();self.f.setUp()
        host=dataclasses.replace(self.f.auth.publication,required_checks=('ci',))
        self.f.auth=dataclasses.replace(self.f.auth,publication=host,
            validation_commands=((sys.executable,'-c',"from pathlib import Path; assert Path('app.txt').read_text().startswith('after')"),))
        # The synthetic worker resolves the real Git conflict while retaining the upstream change.
        text=self.f.cli.read_text().replace("(root/'app.txt').write_text('after\\n')",
            "(root/'app.txt').write_text('after\\nmain\\n' if 'Resolve only the Git conflicts' in request['task']['instruction'] else 'after\\n')")
        self.f.cli.write_text(text)
        self.f.auth=dataclasses.replace(self.f.auth,executable_digest=publication.digest(self.f.cli.read_bytes()))
        self.f.git('checkout','-b','upstream')
        (self.f.repo/'app.txt').write_text('main\n');self.f.git('add','.');self.f.git('commit','-m','upstream change')
        self.main=self.f.git('rev-parse','HEAD');self.f.git('update-ref','refs/remotes/origin/main',self.main)
        self.f.git('checkout','codex/task')
        self.commands=FlowGitHub()

    def tearDown(self):self.f.tearDown()

    def test_conflict_repairs_revalidates_same_pr_and_waits_merge_sha_ci(self):
        original=local.execute(self.f.request,self.f.auth,self.f.state)
        fixed=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(fixed['status'],'integration_validated',fixed)
        self.assertEqual(fixed['base'],self.main)
        self.assertIn('main',(self.f.repo/'app.txt').read_text())
        self.assertNotEqual(fixed['head'],self.f.auth.publication.head)
        directory=Path(original['report_path']).parent
        continuation=json.loads((directory/'continuation.json').read_text())
        self.assertEqual(continuation['report']['base'],self.main)
        self.assertNotEqual(continuation['report']['execution_id'],self.f.auth.publication.execution_id)
        self.commands.conflict=False;self.commands.success=True
        pending=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(pending['status'],'integrated_ci_pending',pending)
        self.commands.integrated_success=True
        complete=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(complete['status'],'complete',complete)
        self.assertEqual(complete['merge_commit'],'f'*40)
        self.assertEqual(complete['vault_persistence']['status'],'persisted')
        task_path=Path(complete['vault_persistence']['path'])
        self.assertEqual(task_path.read_text().count('## Saihai completion'),1)
        repeated=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertTrue(repeated['vault_persistence']['replayed'])
        self.assertEqual(task_path.read_text().count('## Saihai completion'),1)
        self.assertEqual(sum(c[:3]==['gh','pr','create'] for c in self.commands.calls),1)
        self.assertFalse(any('--force' in c for c in self.commands.calls))
        self.assertTrue(any('/commits/'+'f'*40+'/check-runs?' in a for c in self.commands.calls for a in c))

    def test_failed_worker_resumes_before_publisher_dirty_check(self):
        local.execute(self.f.request,self.f.auth,self.f.state)
        with patch.object(local,'_run_process',return_value=({'exit':1},b'')):
            failed=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(failed['status'],'retryable_worker')
        self.assertTrue(self.f.git('rev-parse','MERGE_HEAD'))
        resumed=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(resumed['status'],'integration_validated',resumed)
        self.assertEqual(sum(c[:2]==['git','fetch'] for c in self.commands.calls),1)

    def test_failed_validation_resumes_before_republishing(self):
        local.execute(self.f.request,self.f.auth,self.f.state)
        with patch.object(local,'_validate',side_effect=local.TrustedLocalError('host_validation_failed')):
            failed=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(failed['status'],'retryable_validation')
        resumed=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(resumed['status'],'integration_validated',resumed)

    def test_repeated_worker_failure_reaches_limit_without_republishing(self):
        local.execute(self.f.request,self.f.auth,self.f.state)
        with patch.object(local,'_run_process',return_value=({'exit':1},b'')):
            for _ in range(5):
                result=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
                self.assertEqual(result['status'],'retryable_worker')
            stopped=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(stopped['status'],'same_conflict_retry_limit')
        self.assertEqual(stopped['decision'],'blocked')
        again=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.assertEqual(again['status'],'same_conflict_retry_limit')
        self.assertEqual(sum(c[:2]==['git','fetch'] for c in self.commands.calls),1)

    def test_merge_sha_classic_status_latest_state_controls_completion(self):
        local.execute(self.f.request,self.f.auth,self.f.state)
        local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
        self.commands.conflict=False;self.commands.success=True
        original_run=self.commands.run
        status='pending'
        def run(args,*,cwd,env=None):
            if any('/commits/'+'f'*40+'/check-runs?' in a for a in args):
                return b'[{"check_runs":[]}]'
            if any('/commits/'+'f'*40+'/statuses?' in a for a in args):
                return json.dumps([[{'id':90,'context':'ci','state':status}],
                                   [{'id':2,'context':'ci','state':'success'}]]).encode()
            return original_run(args,cwd=cwd,env=env)
        with patch.object(self.commands,'run',side_effect=run):
            for status,expected in [('pending','integrated_ci_pending'),('error','integrated_ci_failed'),
                                    ('failure','integrated_ci_failed'),('success','complete')]:
                result=local.advance_publication(self.f.auth,self.f.state,commands=self.commands)
                self.assertEqual(result['status'],expected,result)

    def test_wrong_parent_cannot_admit_a_committed_report(self):
        result=local.execute(self.f.request,self.f.auth,self.f.state)
        with self.assertRaisesRegex(publication.PublicationError,'host_integration_parent_mismatch'):
            publication.publish(result['report'],self.f.auth.publication,self.f.state/'publication',
                                commands=self.commands,integrated_parent='a'*40)

if __name__=='__main__':unittest.main()
