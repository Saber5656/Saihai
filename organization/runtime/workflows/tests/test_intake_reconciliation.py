"""Strict schema transport and actual failed fixture processes, no live API claim."""
import copy
import dataclasses
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import request_intake as intake
import trusted_local_executor as local
import run_store
import test_trusted_local_executor as fixture
from test_request_intake import ledger, provider


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TrustedLocalTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        f=self.f;self.doc=ledger()
        self.stop=f.root/'stop-fixture';self.stop.write_text('fail')
        self.count=f.root/'fixture-count'
        classification,_=provider(stage='classify',context={},attempt=1,diagnostic='',schema_path=None)
        classification['notes']=None
        shaped,_=provider(stage='shape',context={'source':{'ledger':self.doc,'user_prompt':f.request['instruction']}},
                          attempt=1,diagnostic='',schema_path=None)
        f.cli.write_text('''#!/usr/bin/env python3
import sys,json
from pathlib import Path
schema=json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
def check(s):
    assert 'type' in s
    if s['type']=='object':
        assert set(s['required'])==set(s['properties']) and s['additionalProperties'] is False
        for c in s['properties'].values():check(c)
    if s['type']=='array':check(s['items'])
check(schema)
count=Path('''+repr(str(self.count))+''');count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
if Path('''+repr(str(self.stop))+''').exists():
    print(json.dumps({'fixture_error':'invalid_json_schema'}))
    print('fixture stderr detail',file=sys.stderr)
    raise SystemExit(1)
context=json.loads(sys.stdin.read().split('\\n',1)[1])
value='''+repr(classification)+''' if context['stage']=='classify' else '''+repr(shaped)+'''
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(value))
''')
        f.auth=dataclasses.replace(f.auth,executable_digest=local.publication.digest(f.cli.read_bytes()))
        self.params=dict(state_root=f.state,request_id=f.request['request_id'],task_id=f.request['task_id'],
            user_prompt=f.request['instruction'],ledger=self.doc,intended_model=f.auth.model)
    def prepare(self):
        return intake.prepare(**self.params,provider=intake.CodexIntakeProvider(
            authorization=self.f.auth,request=self.f.request,state_root=self.f.state))
    def failure(self):
        with self.assertRaisesRegex(intake.IntakeError,'provider_process_failed'):self.prepare()
        paths=list((self.f.state/'intakes'/self.f.request['request_id']).glob('*-process.json'))
        path=max(paths,key=lambda p:p.stat().st_mtime_ns)
        return path.name.removesuffix('-process.json'),path
    def reconcile(self,invocation,**kwargs):
        return intake.reconcile_failed_attempt(state_root=self.f.state,request=self.f.request,
            authorization=kwargs.get('authorization',self.f.auth),invocation_id=invocation)
    def test_provider_schema_types_required_nullable_and_original_validator(self):
        for name in ['typed-classification.schema.json','work-brief.schema.json']:
            schema=json.loads((intake.SCHEMAS/name).read_text());wire=intake.provider_schema(schema)
            def check(node):
                self.assertIn('type',node)
                if node['type']=='object':
                    self.assertEqual(set(node['required']),set(node['properties']))
                    self.assertIs(node['additionalProperties'],False)
                    for row in node['properties'].values():check(row)
                if node['type']=='array':check(node['items'])
            check(wire)
            self.assertEqual(schema,json.loads((intake.SCHEMAS/name).read_text()))
        schema=json.loads((intake.SCHEMAS/'typed-classification.schema.json').read_text())
        wire=intake.provider_schema(schema)
        self.assertEqual(wire['properties']['classification_version'],{'type':'string','enum':['1']})
        self.assertEqual(wire['properties']['notes']['type'],['string','null'])
        self.assertNotIn('notes',schema['required'])
        self.assertEqual(intake.decode_provider_value({'classification_version':None,'notes':None},schema),{'classification_version':None})
    def test_failed_diagnostics_are_private_and_claim_cannot_implicitly_replay(self):
        invocation,path=self.failure();receipt=run_store.read_json(path)
        diagnostic=run_store.read_json(Path(receipt['diagnostic_path']))
        self.assertIn('invalid_json_schema',diagnostic['stdout_tail'])
        self.assertIn('fixture stderr',diagnostic['stderr_tail'])
        self.assertEqual(Path(receipt['diagnostic_path']).stat().st_mode&0o777,0o600)
        self.assertEqual(diagnostic['stdout_digest'],receipt['stdout_digest'])
        self.assertEqual(receipt['exit'],1)
        with self.assertRaisesRegex(intake.IntakeError,'requires_reconciliation'):self.prepare()
        self.assertEqual(self.count.read_text(),'1')
    def test_explicit_reconcile_consumes_failure_without_reset_and_preserves_receipts(self):
        invocation,path=self.failure();old=path.read_bytes();claim=path.with_name(invocation+'-claim.json');old_claim=claim.read_bytes()
        one=self.reconcile(invocation);self.assertFalse(one['budget_reset'])
        self.assertEqual(one,self.reconcile(invocation))
        self.stop.unlink()
        reference=self.prepare();artifact=intake.resolve(self.f.state,reference)
        self.assertEqual([r['status'] for r in artifact['provenance']],['provider_failed','valid','valid'])
        self.assertEqual([r['receipt']['exit'] for r in artifact['provenance']],[1,0,0])
        self.assertNotIn('notes',artifact['classification'])
        self.assertEqual(path.read_bytes(),old);self.assertEqual(claim.read_bytes(),old_claim)
        self.assertEqual(self.count.read_text(),'3')
        self.assertEqual(self.prepare(),reference);self.assertEqual(self.count.read_text(),'3')
    def test_known_legacy_claim_can_be_reconciled_without_rewriting_original(self):
        invocation,path=self.failure();claim=path.with_name(invocation+'-claim.json')
        original=run_store.read_json(claim)
        legacy={k:original[k] for k in ('invocation_id','stage','attempt')}
        run_store.atomic_write_json(claim,legacy);before=claim.read_bytes()
        marker=self.reconcile(invocation)
        self.assertTrue(marker['legacy_claim']);self.assertEqual(claim.read_bytes(),before)
    def test_running_successful_unknown_and_changed_authority_are_not_reconcilable(self):
        invocation,path=self.failure();original=run_store.read_json(path)
        with patch.object(intake.run_lock,'process_start_token',return_value=original['process_start_token']):
            with self.assertRaisesRegex(intake.IntakeError,'still_running'):self.reconcile(invocation)
        changed=dataclasses.replace(self.f.auth,model='different-model')
        with self.assertRaisesRegex(intake.IntakeError,'not_reconcilable'):self.reconcile(invocation,authorization=changed)
        run_store.atomic_write_json(path,dict(original,exit=0))
        with self.assertRaisesRegex(intake.IntakeError,'not_reconcilable'):self.reconcile(invocation)
        run_store.atomic_write_json(path,dict(original,exit=None))
        with self.assertRaisesRegex(intake.IntakeError,'not_reconcilable'):self.reconcile(invocation)
    def test_cli_reconcile_keeps_failed_process_and_then_same_prepare_can_resume(self):
        invocation,path=self.failure();before=path.read_bytes();f=self.f
        authority=f.root/'authority.json';run_store.atomic_write_json(authority,dataclasses.asdict(f.auth))
        cli=Path(__file__).resolve().parents[4]/'scripts'/'saihai.py'
        result=subprocess.run([sys.executable,str(cli),'usage','reconcile-intake','--request',json.dumps(f.request),
            '--invocation-id',invocation,'--authorization',str(authority),'--state-root',str(f.state)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(json.loads(result.stdout)['original_claim_preserved'])
        self.assertEqual(path.read_bytes(),before)

    def test_five_same_cause_failures_stop_across_restarts(self):
        for _ in range(5):
            invocation,path=self.failure();self.reconcile(invocation)
        with self.assertRaisesRegex(intake.IntakeError,'recovery_exhausted:classify'):self.prepare()
        self.assertEqual(self.count.read_text(),'5')
        with self.assertRaisesRegex(intake.IntakeError,'recovery_exhausted:classify'):self.prepare()
        self.assertEqual(self.count.read_text(),'5')


if __name__=='__main__':unittest.main()
