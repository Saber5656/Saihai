"""Real temporary canonical records and host receipts; never live model evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import request_intake as intake
import ledger_lifecycle as life
import requirement_scope as scope
import vault_task_records as vault
import run_store
from test_request_intake import ledger, provider


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.state = self.root / 'state'
        self.vault = self.root / 'vault'; self.vault.mkdir()
        self.task = 'TSK-20260907-ledger'
        vault.scaffold(self.vault, self.task, project='Fixtures',
                       brief=dict(objective='Fixture', scope='Fixture', acceptance_criteria='Fixture'))
        self.mock = patch.object(vault, 'canonical_root', return_value=self.vault)
        self.mock.start(); self.addCleanup(self.mock.stop)
        self.params = dict(state_root=self.state, request_id='req', task_id=self.task,
                           user_prompt='Review API A and B.', ledger=ledger(), provider=provider,
                           intended_model='approved-model')
    def prepare(self):
        ref = intake.prepare(**self.params)
        return ref, intake.resolve(self.state, ref)
    def test_full_ledger_content_ids_and_ack_are_read_back(self):
        ref, artifact = self.prepare()
        ack = life.acknowledge(artifact, ref)
        saved = json.loads(Path(ack['path']).read_bytes())
        self.assertEqual(saved['ledger'], self.params['ledger'])
        self.assertEqual([r['original_content_digest'] for r in saved['requirements']],
                         [scope.digest(r) for r in self.params['ledger']['requirements']])
        Path(ack['path']).write_text('{}')
        with self.assertRaises(vault.VaultTaskError): intake.resolve(self.state, ref)
    def test_partial_sidecar_recovers_without_provider_reinvoke(self):
        ref, artifact = self.prepare(); ack = life.acknowledge(artifact, ref)
        path = Path(ack['path']); expected = path.read_bytes(); path.write_bytes(expected[:31])
        with self.assertRaises(vault.VaultTaskError): intake.resolve(self.state, ref)
        self.params['provider'] = lambda **kw: self.fail('provider replayed')
        self.assertEqual(intake.prepare(**self.params), ref)
        self.assertEqual(path.read_bytes(), expected)
    def test_sidecar_and_marker_each_required_and_symlink_blocked(self):
        ref, artifact = self.prepare(); ack = life.acknowledge(artifact, ref)
        path = Path(ack['path']); content = path.read_bytes()
        path.unlink(); target = self.root/'other'; target.write_bytes(content); path.symlink_to(target)
        with self.assertRaises(vault.VaultTaskError): intake.resolve(self.state, ref)
        self.assertEqual(target.read_bytes(), content)
    def test_partial_task_marker_recovers_but_unrelated_tail_is_not_overwritten(self):
        ref,artifact=self.prepare();ack=life.acknowledge(artifact,ref)
        task_path=Path(vault.bind_task(self.task)['path']);original=task_path.read_bytes()
        marker=('\n<!-- saihai-ledger:'+ack['digest'][7:]+' -->\n').encode()
        cut=original.index(marker)+len(marker)+15
        task_path.write_bytes(original[:cut])
        with self.assertRaises(vault.VaultTaskError):intake.resolve(self.state,ref)
        self.params['provider']=lambda **kw:self.fail('provider replayed')
        self.assertEqual(intake.prepare(**self.params),ref)
        self.assertEqual(task_path.read_bytes(),original)
        conflicting=original[:cut]+b'UNRELATED CONTENT'
        task_path.write_bytes(conflicting)
        with self.assertRaises(vault.VaultTaskError):intake.prepare(**self.params)
        self.assertEqual(task_path.read_bytes(),conflicting)

    def test_ledger_completion_boolean_cannot_satisfy_dependency(self):
        self.params['ledger']['task_units'][1]['depends_on'] = ['A']
        self.params['ledger']['task_units'][0]['completed'] = True
        ref, artifact = self.prepare()
        with self.assertRaisesRegex(life.LedgerError, 'completion_missing'):
            life.require_prerequisites(self.state, artifact)
    def test_completion_receipt_bound_to_unit_requirements_and_canonical_record(self):
        ref, artifact = self.prepare()
        doc = artifact['requirement_ledger']; a = doc['task_units'][0]
        a['binding'] = {'status': 'existing', 'task_id': self.task, 'issue': 'https://example.com/1'}
        doc['task_units'][1]['depends_on'] = ['A']
        identity = life.unit_identity(doc, a)
        receipt = dict(version=1, kind='integrated_unit_completion', task_id=self.task, unit_id='A',
                       unit_identity=identity, result='complete', merge_commit='a'*40, integrated_checks={'test':'success'})
        run_store.atomic_write_json(life._completion_path(self.state, identity), receipt)
        with self.assertRaises(vault.VaultTaskError): life.require_prerequisites(self.state, artifact)
        vault.ledger_checkpoint(vault.bind_task(self.task), receipt, persist=True)
        self.assertEqual(len(life.require_prerequisites(self.state, artifact)), 1)
        doc['requirements_version']='v2';doc['selection_requirements_version']='v2'
        self.assertEqual(len(life.require_prerequisites(self.state, artifact)), 1)
        doc['requirements'][0]['text'] += ' expanded'
        with self.assertRaisesRegex(life.LedgerError, 'completion_missing'): life.require_prerequisites(self.state, artifact)
    def test_every_stage_dispositions_dedup_and_resolution_survives_empty_stage(self):
        ref, artifact = self.prepare()
        finding = {'text':'Actual in-scope defect', 'requirement_ids':['RB'], 'kind':'ordinary'}
        for stage in sorted(life.STAGES):
            one = life.record_stage(self.state, artifact, ref, stage=stage, observation={'stage':stage}, findings=[finding])
            self.assertEqual(one, life.record_stage(self.state, artifact, ref, stage=stage, observation={'stage':stage}, findings=[finding]))
        status = life.findings_status(self.state, artifact)
        row = next(r for r in status['findings'] if r['original']==finding)
        self.assertEqual(set(row['stages']), life.STAGES); self.assertTrue(status['blocked'])
        evidence = self.root/'validation.json';run_store.atomic_write_json(evidence, {'status':'passed','commands':[{'exit':0}]})
        run_store.atomic_write_json(self.root/'report.json', {'task_id':self.task,'intake_digest':ref['digest'],
            'validation':{'evidence_path':str(evidence),'evidence_digest':vault.digest(evidence.read_bytes())}})
        with self.assertRaisesRegex(life.LedgerError,'cannot_defer'):
            life.resolve_findings(self.state, artifact, ref, finding_ids=[row['finding_id']],
                evidence={'path':str(evidence),'digest':vault.digest(evidence.read_bytes())},disposition='deferred')
        life.resolve_findings(self.state, artifact, ref, finding_ids=[row['finding_id']],
                evidence={'path':str(evidence),'digest':vault.digest(evidence.read_bytes())})
        life.record_stage(self.state, artifact, ref, stage='publication', observation={'new':'empty'})
        self.assertFalse(life.findings_status(self.state, artifact)['blocked'])
    def test_unknown_requirement_finding_needs_explicit_lossless_host_revision(self):
        ref,before=self.prepare()
        finding={'text':'Account for C','requirement_ids':['RC'],'kind':'requirement_choice'}
        event=life.record_stage(self.state,before,ref,stage='plan',observation={},findings=[finding])
        units=copy.deepcopy(before['requirement_ledger']['task_units']);units[0]['requirement_ids'].append('RC')
        revised=scope.revise(before['requirement_ledger'],expected_digest=scope.digest(before['requirement_ledger']),
            version='v2',replacements={},additions=[{'requirement_id':'RC','text':'Review API C'}],
            units=units,selected_unit_id='B')
        def revised_provider(**kw):
            value,receipt=provider(**kw)
            if kw['stage']=='shape':value['requirement_dispositions'].append({'requirement_id':'RC','status':'pending'})
            return value,receipt
        self.params.update(ledger=revised,provider=revised_provider)
        newer,after=self.prepare()
        self.assertTrue(life.findings_status(self.state,after)['blocked'])
        life.resolve_requirement_findings(self.state,before,ref,after,newer,
            finding_ids=[event['findings'][0]['finding_id']])
        self.assertFalse(life.findings_status(self.state,after)['blocked'])
        self.assertEqual(after['requirement_ledger']['requirements'][:2],before['requirement_ledger']['requirements'])

    def test_zero_omission_and_no_false_semantic_completeness(self):
        ref, artifact = self.prepare()
        artifact['brief']['requirement_dispositions'].pop()
        with self.assertRaisesRegex(life.LedgerError,'coverage'):life.checkpoint(artifact, ref)
    def test_prerequisite_identity_does_not_ignore_hunk_permission_changes(self):
        doc=ledger();unit=doc['task_units'][0]
        unit['change_contracts']=[{'path':'app.txt','base':'a'*40,'ranges':[[1,1]],'insertions':[],
                                  'operations':['edit'],'requirement_ids':['RA']}]
        identity=life.unit_identity(doc,unit)
        unit['change_contracts'][0]['ranges']=[[4,4]]
        self.assertNotEqual(life.unit_identity(doc,unit),identity)

    def test_non_success_publication_cannot_produce_prerequisite(self):
        ref, artifact = self.prepare()
        with self.assertRaisesRegex(life.LedgerError,'evidence_incomplete'):
            life.record_completion(self.state, artifact, ref, {}, {'status':'merged'})


if __name__ == '__main__': unittest.main()
