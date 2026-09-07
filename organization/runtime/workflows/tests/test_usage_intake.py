"""Actual fixture subprocess intake -> pinned worker -> actual diff/validation.

The executable below is a test fixture, never evidence of a live model invocation.
"""
import copy
import dataclasses
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import request_intake as intake
import trusted_local_executor as local
import host_publication_adapter as publication
import test_trusted_local_executor as base_fixture
from test_request_intake import ledger, provider


class UsageIntakeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base_fixture.TrustedLocalTests();self.fixture.setUp()
        self.doc=ledger();host=self.fixture.auth.publication
        self.doc['task_units'][1]['change_contracts']=[dict(path='app.txt',base=host.head,requirement_ids=['RB'],
            operations=['edit'],ranges=[[1,1]],insertions=[])]
    def tearDown(self):self.fixture.tearDown()
    def typed_provider(self,**kw):
        value,receipt=provider(**kw)
        if kw['stage']=='classify':value.update(task_kind='code_change',permission_required='edit',external_provider_required=False,
            expected_artifacts=['code_diff','validation_result','code_change_report','final_evidence'])
        else:value['safety_class']='standard'
        return value,receipt
    def prepare(self):
        f=self.fixture
        ref=intake.prepare(state_root=f.state,request_id=f.request['request_id'],task_id=f.request['task_id'],
            user_prompt=f.request['instruction'],ledger=self.doc,provider=self.typed_provider,intended_model=f.auth.model)
        return ref,dataclasses.replace(f.auth,intake_digest=ref['digest']),dict(f.request,work_brief_ref=ref)
    def test_host_binding_required_not_worker_self_authorization(self):
        ref,auth,request=self.prepare()
        with self.assertRaises(local.TrustedLocalError):local.execute(request,self.fixture.auth,self.fixture.state)
        with self.assertRaisesRegex(local.TrustedLocalError,'host_intake_binding'):
            local.execute(dict(request,work_brief_ref=dict(ref,digest='sha256:'+'0'*64)),auth,self.fixture.state)
        self.assertEqual(self.fixture.git('status','--porcelain'),'')
    def test_prepared_worker_gets_grounded_brief_not_private_prompt_and_real_diff(self):
        ref,auth,request=self.prepare();prompts=[];original=local._run_process
        def capture(argv,prompt,*args):prompts.append(prompt);return original(argv,prompt,*args)
        with patch.object(local,'_run_process',side_effect=capture):
            result=local.execute(request,auth,self.fixture.state)
        self.assertEqual(result['status'],'validated')
        self.assertNotIn(self.fixture.request['instruction'],prompts[0])
        self.assertIn('Review API B',prompts[0]);self.assertNotIn('incidental_findings',prompts[0])
        self.assertEqual(result['report']['requirement_scope']['changes'][0]['requirement_ids'],['RB'])
        self.assertEqual(result['report']['intake_digest'],ref['digest'])
    def test_same_file_unapproved_range_blocks_before_host_validation(self):
        self.doc['task_units'][1]['change_contracts'][0]['ranges']=[[5,5]]
        ref,auth,request=self.prepare()
        with patch.object(local,'_validate',side_effect=AssertionError('validation should not run')):
            with self.assertRaisesRegex(local.TrustedLocalError,'hunk_outside_scope'):local.execute(request,auth,self.fixture.state)
    def test_intake_publication_rechecks_binding_and_conflict_base(self):
        ref,auth,request=self.prepare();f=self.fixture
        result=local.execute(request,auth,f.state)
        report_path=Path(result['report_path']);report=json.loads(report_path.read_text());report['intake_digest']='sha256:'+'0'*64
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(local.TrustedLocalError,'publication_intake_binding_changed'):
            local.advance_publication(auth,f.state)
        conflict=local._integrate_conflict(auth,auth,report_path.parent,f.state,{'head':auth.publication.head},None)
        self.assertEqual(conflict['status'],'intake_scope_refresh_required')
    def test_legacy_authority_material_does_not_change_existing_digests(self):
        material=local._authorization_material(self.fixture.auth)
        self.assertNotIn('intake_digest',material)
        legacy = {k:v for k,v in dataclasses.asdict(self.fixture.auth).items() if k not in {'intake_digest','validation_profile'}}
        legacy['publication'].pop('expected_assignees')
        self.assertEqual(publication.digest(material),publication.digest(legacy))
        for profile in (None, {'path':'/host/plan','sha256':'a'*64}):
            for intake in ('', 'sha256:'+'b'*64):
                auth=dataclasses.replace(self.fixture.auth,validation_profile=profile,intake_digest=intake)
                value=local.authorization_payload(auth)
                self.assertEqual(value,local._authorization_material(auth))
                self.assertEqual('validation_profile' in value,profile is not None)
                self.assertEqual('intake_digest' in value,bool(intake))
    def test_driver_binds_intake_claim_and_rejects_digest_changes(self):
        import trusted_local_driver as driver
        ref,auth,request=self.prepare();f=self.fixture
        result=driver.drive(authorization=auth,state_root=f.state,request=request,max_iterations=1)
        self.assertEqual(result['last_status'],'validated')
        directory=f.state/'trusted-local'/auth.publication.execution_id
        self.assertEqual(json.loads((directory/'claim.json').read_text())['authorization_digest'],
                         json.loads((directory/'drive.json').read_text())['authorization_digest'])
        with self.assertRaisesRegex(local.TrustedLocalError,'drive_authority_changed'):
            driver.drive(authorization=dataclasses.replace(auth,intake_digest='sha256:'+'a'*64),state_root=f.state)
    def test_actual_intake_process_receipts_and_no_repo_mutation(self):
        f=self.fixture
        f.cli.write_text('''#!/usr/bin/env python3
import sys,json,time
from pathlib import Path
time.sleep(0.15)
p=json.loads(sys.stdin.read().split('\\n',1)[1]); c=p['context']; ledger=c['source']['ledger']
if p['stage']=='classify':
 value=dict(classification_version='1',classification_source='bounded_classifier_step',classification_confidence=1.0,
 classification_evidence=['fixture'],task_kind='code_change',permission_required='edit',external_provider_required=False,
 publication_required=False,security_sensitive=False,destructive_operation=False,context_scope='refs_only',expected_artifacts=['code_diff','validation_result','code_change_report','final_evidence'])
else:
 value=dict(brief_version='1',objective='Review API B',scope={'in':['Review API B'],'out':['Do not change behavior']},
 constraints=['Keep API compatibility'],acceptance_criteria=['Report concrete findings'],risk_notes=[],open_questions=[],
 source_prompt_digest=c['source_prompt_digest'],requirements_digest=c['requirements_digest'],requirements_version='v1',selected_unit_id='B',
 safety_class=c['safety_class'],requirement_dispositions=[dict(requirement_id='RA',status='pending'),dict(requirement_id='RB',status='selected')])
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(value))
''')
        auth=dataclasses.replace(f.auth,executable_digest=publication.digest(f.cli.read_bytes()))
        real=intake.CodexIntakeProvider(authorization=auth,request=f.request,state_root=f.state)
        ref=intake.prepare(state_root=f.state,request_id=f.request['request_id'],task_id=f.request['task_id'],
            user_prompt=f.request['instruction'],ledger=self.doc,provider=real,intended_model=auth.model)
        artifact=intake.resolve(f.state,ref)
        self.assertEqual(len(artifact['provenance']),2)
        for row in artifact['provenance']:
            receipt=json.loads(Path(row['receipt']['evidence_ref']).read_text())
            self.assertGreater(receipt['pid'],0);self.assertTrue(receipt['process_start_token']);self.assertEqual(receipt['exit'],0)
        self.assertEqual(f.git('status','--porcelain'),'')


if __name__=='__main__':unittest.main()
