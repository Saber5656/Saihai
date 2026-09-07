"""Real subprocess/Git fixtures for the explicit trusted-local host boundary."""
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
import trusted_local_executor as local
import host_publication_adapter as publication


class TrustedLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.repo, stderr=subprocess.DEVNULL).decode().strip()
        self.git = git
        git('init', '-b', 'codex/task'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/example/repo.git')
        (self.repo / 'app.txt').write_text('before\n'); git('add', '.'); git('commit', '-m', 'fixture')
        self.cli = self.root / 'codex-fixture'
        self.cli.write_text('''#!/usr/bin/env python3
import sys,json,time,os,subprocess
from pathlib import Path
if sys.argv[1]=='sandbox':
    raise SystemExit(subprocess.run(sys.argv[sys.argv.index('--')+1:]).returncode)
time.sleep(0.15)
request=json.loads(sys.stdin.read().split('\\n',1)[1])
root=Path(sys.argv[sys.argv.index('--cd')+1])
(root/'app.txt').write_text('after\\n')
out=Path(sys.argv[sys.argv.index('--output-last-message')+1])
out.write_text(json.dumps(dict(result_version='1',status='completed',summary='fixture',changed_paths=['app.txt'],tests=[],evidence=[])))
'''); self.cli.chmod(0o755)
        self.home = self.root / 'existing-home'; self.home.mkdir(mode=0o700)
        head = git('rev-parse', 'HEAD')
        host = publication.HostAuthorization('task-usage', 'req-usage', 'run-usage', 'exec-usage', 'example/repo',
            str(self.repo), 'codex/task', head, head, ('app.txt',), ('validate','Analyze (actions)','Analyze (python)'),
            'sha256:'+'1'*64, 'parent-task-explicit-authority')
        self.auth = local.TrustedLocalAuthorization(host, str(self.cli), publication.digest(self.cli.read_bytes()),
            str(self.home), 'approved-model', ((sys.executable, '-c', "from pathlib import Path; assert Path('app.txt').read_text()=='after\\n'"),))
        self.request = dict(task_id=host.task_id,request_id=host.request_id,run_id=host.run_id,execution_id=host.execution_id,instruction='Make the approved fixture change.')
        self.state = self.root / 'state'

    def tearDown(self): self.tmp.cleanup()

    def test_real_process_validation_report_and_not_required(self):
        result=local.execute(self.request,self.auth,self.state)
        self.assertEqual(result['status'],'validated')
        publication.validate_report(result['report'],self.auth.publication)
        directory=Path(result['report_path']).parent
        process=json.loads((directory/'process.json').read_text())
        self.assertGreater(process['pid'],0); self.assertTrue(process['process_start_token']); self.assertEqual(process['exit'],0)
        self.assertEqual(json.loads((directory/'review.json').read_text())['status'],'not_required')
        self.assertFalse(result['report']['publication_allowed'])
        self.assertFalse((self.home/'auth.json').exists())
        self.assertEqual(self.git('diff','--cached'),'')

    def test_request_cannot_inject_argv_or_other_task(self):
        for change in ({'argv':['sh','-c','bad']},{'task_id':'other-task'}):
            with self.assertRaises(local.TrustedLocalError): local.execute(dict(self.request,**change),self.auth,self.state)
        self.assertEqual(self.git('status','--porcelain'),'')

    def test_wrong_worktree_and_runtime_digest_block_before_spawn(self):
        wrong=dataclasses.replace(self.auth,executable_digest='sha256:'+'0'*64)
        with self.assertRaisesRegex(local.TrustedLocalError,'runtime_digest_changed'):local.execute(self.request,wrong,self.state)
        host=dataclasses.replace(self.auth.publication,branch='codex/another')
        with self.assertRaisesRegex(local.TrustedLocalError,'branch_identity_mismatch'):local.execute(self.request,dataclasses.replace(self.auth,publication=host),self.state)

    def test_replay_never_runs_worker_twice(self):
        local.execute(self.request,self.auth,self.state)
        self.git('restore','app.txt')
        with self.assertRaisesRegex(local.TrustedLocalError,'execution_already_claimed'):local.execute(self.request,self.auth,self.state)
        self.assertEqual((self.repo/'app.txt').read_text(),'before\n')

    def test_scope_escape_rejected_before_validation(self):
        host=dataclasses.replace(self.auth.publication,allowed_paths=('different.txt',))
        with self.assertRaisesRegex(local.TrustedLocalError,'changed_paths_outside_scope'):
            local.execute(self.request,dataclasses.replace(self.auth,publication=host),self.state)
        self.assertFalse((self.state/'trusted-local'/'exec-usage'/'validation.json').exists())

    def test_failed_actual_validation_has_no_publishable_report(self):
        auth=dataclasses.replace(self.auth,validation_commands=((sys.executable,'-c','raise SystemExit(9)'),))
        with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):local.execute(self.request,auth,self.state)
        self.assertFalse((self.state/'trusted-local'/'exec-usage'/'report.json').exists())

    def test_risk_profile_needs_separate_review_receipt(self):
        auth=dataclasses.replace(self.auth,publication=dataclasses.replace(self.auth.publication,risk_kind='permission_expansion'))
        with self.assertRaisesRegex(local.TrustedLocalError,'scoped_risk_review_required'):local.execute(self.request,auth,self.state)

    def test_cli_run_and_missing_authorization_return_json(self):
        cli=Path(__file__).resolve().parents[4]/'scripts'/'saihai.py'
        authority=self.root/'authority.json'
        authority.write_text(json.dumps(dataclasses.asdict(self.auth))); authority.chmod(0o600)
        args=[sys.executable,str(cli),'usage','run','--request',json.dumps(self.request),
              '--authorization',str(authority),'--state-root',str(self.state)]
        result=subprocess.run(args,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertEqual(json.loads(result.stdout)['status'],'validated')
        missing=subprocess.run([sys.executable,str(cli),'usage','advance','--authorization',str(self.root/'missing'),
                                '--state-root',str(self.state)],capture_output=True,text=True)
        self.assertEqual(missing.returncode,2,missing.stderr)
        self.assertEqual(json.loads(missing.stdout)['reason'],'host_authorization_unavailable_or_invalid')

    def test_fixed_permissions_do_not_grant_other_task_or_publication(self):
        argv=local._argv(self.auth,self.repo,self.root/'output.json')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',argv)
        self.assertNotIn('--add-dir',argv)
        policy=local._permissions(self.auth,self.repo)
        self.assertIn('":root"="deny"',policy)
        self.assertIn(json.dumps(str(self.repo/'.git'))+'="deny"',policy)
        self.assertIn(json.dumps(str(self.repo/'app.txt'))+'="write"',policy)

if __name__=='__main__': unittest.main()
