"""Finished worker recovery against real local subprocesses and Git fixtures."""
import dataclasses
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_trusted_local_executor as fixture
import trusted_local_executor as local
import trusted_local_driver as driver
import host_publication_adapter as pub


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TrustedLocalTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.directory = self.f.state / 'trusted-local' / self.f.auth.publication.execution_id

    def configure(self, mode='once'):
        text = self.f.cli.read_text()
        prefix = """schema=json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
assert set(schema['required'])==set(schema['properties'])
assert 'null' in schema['properties']['incidental_findings']['type']
"""
        if mode != 'success':
            prefix += """if MODE == 'always' or not (out.parent/'repair-input.json').exists():
    print('invalid_json_schema: fixture request-id-'+str(os.getpid()),file=sys.stderr)
    raise SystemExit(1)
""".replace('MODE',repr(mode))
        text = text.replace("(root/'app.txt').write_text('after\\n')", "out=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"+prefix+"(root/'app.txt').write_text('after\\n')")
        text = text.replace("tests=[],evidence=[]", "tests=[],evidence=[],incidental_findings=None")
        self.f.cli.write_text(text)
        self.f.auth = dataclasses.replace(self.f.auth, executable_digest=pub.digest(self.f.cli.read_bytes()))

    def fail(self):
        with self.assertRaisesRegex(local.TrustedLocalError,'worker_process_failed'):
            local.execute(self.f.request,self.f.auth,self.f.state)

    def test_wire_nullable_decode_and_private_diagnostics(self):
        self.configure('success')
        result=local.execute(self.f.request,self.f.auth,self.f.state)
        self.assertEqual(result['status'],'validated')
        self.assertIsNone(json.loads((self.directory/'worker-result.json').read_text())['incidental_findings'])
        process=json.loads((self.directory/'process.json').read_text())
        diagnostic=Path(process['diagnostic_path'])
        self.assertEqual(diagnostic.stat().st_mode&0o777,0o600)
        self.assertEqual(pub.digest(diagnostic.read_bytes()),process['diagnostic_digest'])

    def test_drive_recovers_and_preserves_all_old_records(self):
        self.configure();self.fail()
        old={p:p.read_bytes() for p in self.directory.glob('*.json')}
        result=driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
        self.assertEqual(result['last_status'],'validated',result)
        self.assertEqual(result['repairs'],1)
        for p,raw in old.items():self.assertEqual(p.read_bytes(),raw,p.name)
        status=local.usage_status(self.f.auth.publication.execution_id,self.f.state)
        self.assertEqual(status['validation']['status'],'passed')
        child=self.directory.parent/status['current_execution_id']
        request=json.loads((child/'request.json').read_text())
        self.assertEqual(request['instruction'],self.f.request['instruction'])
        self.assertNotEqual(request['execution_id'],self.f.request['execution_id'])

    def test_explicit_child_plan_survives_stale_parent_through_publication(self):
        import effective_installation as install
        import copy
        from test_host_publication_adapter import FakeGitHub
        self.configure()
        plan=dict(version=1,surface='codex-app',roots={'repo':'SAIHAI_ROOT'},members=[],policy_snapshots=[],
                  approved_symlinks={},source_identity={'root':'repo','commit':'a'*40},expected_content_digest='old',sync_catalog_key='SAIHAI_ROOT')
        active=['old']
        def observe(value):
            if value['expected_content_digest'] != active[0]:raise install.InstallationError('installation_source_changed')
            return dict(content_digest=active[0],target_surface='codex-app')
        with patch.object(install,'catalog',return_value={'SAIHAI_ROOT':str(self.f.repo)}),patch.object(install,'_observe',side_effect=observe):
            install.configure(self.f.auth,self.f.state,plan);self.fail()
            old=install._path(self.f.auth,self.f.state).read_bytes()
            active[0]='new';replacement=copy.deepcopy(plan)
            replacement['expected_content_digest']='new';replacement['source_identity']['commit']='b'*40
            result=driver.drive(authorization=self.f.auth,state_root=self.f.state,worker_recovery_plan=replacement,max_iterations=1)
            self.assertEqual(result['last_status'],'validated')
            self.assertEqual(install._path(self.f.auth,self.f.state).read_bytes(),old)
            with self.assertRaises(install.InstallationError):install.verify(self.f.auth,self.f.state)
            published=local.advance_publication(self.f.auth,self.f.state,commands=FakeGitHub())
            self.assertEqual(published['status'],'ci_pending')
            self.assertEqual(published['effective_installation']['content_digest'],'new')

    def test_cli_drive_resumes_existing_failed_request(self):
        import subprocess,sys
        self.configure();self.fail()
        authority=self.f.root/'authority.json';authority.write_text(json.dumps(dataclasses.asdict(self.f.auth)));authority.chmod(0o600)
        cli=Path(__file__).resolve().parents[4]/'scripts/saihai.py'
        wrapper='import sys,runpy; from pathlib import Path; sys.path.insert(0,sys.argv.pop(1)); import vault_task_records as v; root=Path(sys.argv.pop(1)); v.canonical_root=lambda:root; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
        result=subprocess.run([sys.executable,'-c',wrapper,str(cli.parent.parent/'organization/runtime/workflows/scripts'),str(self.f.vault),str(cli),'usage','drive','--authorization',str(authority),'--state-root',str(self.f.state),'--max-iterations','1'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['last_status'],'validated')

    def test_canonical_required_null_stays_invalid(self):
        self.configure('success')
        self.f.cli.write_text(self.f.cli.read_text().replace("summary='fixture'",'summary=None'))
        self.f.auth=dataclasses.replace(self.f.auth,executable_digest=pub.digest(self.f.cli.read_bytes()))
        with self.assertRaisesRegex(local.TrustedLocalError,'worker_result_invalid'):
            local.execute(self.f.request,self.f.auth,self.f.state)
        with patch.object(local,'_run_process') as run:
            result=driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
            self.assertEqual(result['status'],'execution_incomplete_inspection_required');run.assert_not_called()

    def test_same_cause_stops_at_five_failures_across_drive_restarts(self):
        self.configure('always');self.fail()
        for _ in range(4):
            result=driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
            self.assertEqual(result['last_status'],'worker_failed')
        with patch.object(local,'_run_process') as run:
            result=driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=2)
            self.assertEqual(result['status'],'same_worker_retry_limit');run.assert_not_called()
        self.assertEqual(len(list((self.f.state/'trusted-local').glob('*/process.json'))),5)

    def test_live_unknown_changed_tree_and_authority_refused(self):
        self.configure();self.fail()
        with patch.object(local.os,'kill',return_value=None),patch.object(local,'_run_process') as run:
            with self.assertRaisesRegex(local.TrustedLocalError,'alive_or_unknown'):
                driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
            run.assert_not_called()
        with self.assertRaisesRegex(local.TrustedLocalError,'drive_authority_changed'):
            driver.drive(authorization=dataclasses.replace(self.f.auth,model='other'),state_root=self.f.state)
        (self.f.repo/'app.txt').write_text('outside change')
        with self.assertRaisesRegex(local.TrustedLocalError,'failed_worker_tree_changed'):
            driver.drive(authorization=self.f.auth,state_root=self.f.state)

    def test_legacy_clean_failed_process_retained(self):
        self.configure();self.fail()
        process=json.loads((self.directory/'process.json').read_text());process.pop('source_identity')
        local._save(self.directory/'process.json',process)
        old=(self.directory/'process.json').read_bytes()
        result=driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
        self.assertEqual(result['last_status'],'validated')
        self.assertEqual((self.directory/'process.json').read_bytes(),old)

    def test_missing_terminal_evidence_and_success_not_retried(self):
        self.configure();self.fail()
        p=self.directory/'process.json';process=json.loads(p.read_text());process.pop('ended_at_epoch');local._save(p,process)
        with patch.object(local,'_run_process') as run:
            with self.assertRaisesRegex(local.TrustedLocalError,'finished_failed_worker_required'):
                driver.drive(authorization=self.f.auth,state_root=self.f.state)
            run.assert_not_called()

    def test_corrected_wire_strategy_allows_retry_without_erasing_failures(self):
        self.configure('always');self.fail()
        for _ in range(4):driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
        originals={p:p.read_bytes() for p in (self.f.state/'trusted-local').glob('*/process.json')}
        real=local.request_intake.provider_schema
        def corrected(schema):
            result=real(schema)
            if 'result_version' in result.get('properties',{}):result['description']='Corrected provider format'
            return result
        # A real wire schema difference creates a new measured action; IDs do not.
        with patch.object(local.request_intake,'provider_schema',corrected):
            result=driver.drive(authorization=self.f.auth,state_root=self.f.state,max_iterations=1)
        self.assertEqual(result['last_status'],'worker_failed')
        self.assertEqual(len(list((self.f.state/'trusted-local').glob('*/process.json'))),6)
        for p,raw in originals.items():self.assertEqual(p.read_bytes(),raw)

class RecoveryInstallationTests(unittest.TestCase):
    def setUp(self):
        import test_effective_installation as installation_fixture
        self.f=installation_fixture.InstallationTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_new_source_is_pinned_only_to_child_and_old_plan_preserved(self):
        import effective_installation as install
        import effective_bundle as bundle
        import copy
        f=self.f;install.configure(f.auth,f.state,f.plan)
        original=install._path(f.auth,f.state);raw=original.read_bytes()
        (f.repo/'common').write_text('corrected installed runtime')
        f.git('add','common');f.git('commit','-m','runtime correction')
        plan=copy.deepcopy(f.plan);plan['source_identity']['commit']=f.git('rev-parse','HEAD')
        plan['expected_content_digest']=bundle.observe_bundle(catalog_roots={'repo':str(f.repo)},
            member_spec={'members':plan['members'],'policy_snapshots':[]},target_surface='codex-app',
            expected_source_identity=plan['source_identity'])['content_digest']
        child=dataclasses.replace(f.auth,publication=dataclasses.replace(f.auth.publication,execution_id='child'))
        with self.assertRaises(install.InstallationError):install.verify(f.auth,f.state)
        install.continue_failed_worker(f.auth,child,f.state,plan)
        self.assertEqual(install.verify(child,f.state)['status'],'installed_bytes_verified')
        self.assertEqual(original.read_bytes(),raw)
        self.assertEqual(json.loads(install._path(child,f.state).with_suffix('.continuation.json').read_text())['parent_plan_digest'],pub.digest(f.plan))
        changed=copy.deepcopy(plan);changed['surface']='codex-cli'
        next_child=dataclasses.replace(child,publication=dataclasses.replace(child.publication,execution_id='next'))
        with self.assertRaisesRegex(install.InstallationError,'constraints_changed'):
            install.continue_failed_worker(f.auth,next_child,f.state,changed)
        with self.assertRaisesRegex(install.InstallationError,'authority_constraints_changed'):
            install.continue_failed_worker(f.auth,dataclasses.replace(next_child,model='new-model'),f.state,plan)

if __name__=='__main__':unittest.main()
