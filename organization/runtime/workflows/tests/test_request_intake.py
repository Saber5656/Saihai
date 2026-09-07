"""Intake producer/transport regressions; fixture evidence is not live proof."""
import copy
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import request_intake as intake
import requirement_scope as scope
import work_order_builder as orders
import frontdoor_orchestrator as frontdoor
from test_frontdoor_orchestrator import external_review_classification


def ledger():
    def unit(name):
        return dict(unit_id=name, title='Review API ' + name, main_team='tech', assignee='reviewer', repository='example/repo',
            scope={'in':['Review API ' + name], 'out':['Do not change behavior']}, deliverables=['report'],
            done_criteria=['Report concrete findings'], allowed_paths=['app.txt'], requirement_ids=['R'+name],
            depends_on=[], binding={'status':'pending'})
    return dict(scope_contract_version=1, requirements_version='v1', selection_requirements_version='v1',
        requirements_history=[], requirements=[dict(requirement_id='RA', text='Review API A'), dict(requirement_id='RB', text='Review API B')],
        task_units=[unit('A'),unit('B')], selected_unit_id='B', constraints=['Keep API compatibility'],
        incidental_findings=[dict(id='F1', disposition='defer')])


def provider(stage, context, attempt, diagnostic, schema_path):
    if stage == 'classify':
        value = external_review_classification(classification_source='bounded_classifier_step')
    else:
        doc = context['source']['ledger']
        value = dict(brief_version='1', objective='Review API B', scope={'in':['Review API B'], 'out':['Do not change behavior']},
            constraints=['Keep API compatibility'], acceptance_criteria=['Report concrete findings'], risk_notes=[], open_questions=[],
            source_prompt_digest=scope.digest(context['source']['user_prompt']), requirements_digest=scope.digest(doc),
            requirements_version=doc['requirements_version'], selected_unit_id='B', safety_class='readonly',
            requirement_dispositions=[dict(requirement_id='RA',status='pending'),dict(requirement_id='RB',status='selected')])
    return value, dict(invocation_id=stage+str(attempt), intended_model='approved-model', effective_model='approved-model',
        evidence_ref='fixture://'+stage+str(attempt), exit=0)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.state=Path(self.tmp.name).resolve()/'state'
        import vault_task_records as vault
        self.vault=self.state.parent/'vault';self.vault.mkdir()
        vault.scaffold(self.vault,'TSK-20260907-intake',project='Fixtures',brief=dict(objective='Intake fixture',scope='Fixture',acceptance_criteria='Fixture'))
        self.vault_patch=patch.object(vault,'canonical_root',return_value=self.vault)
        self.vault_patch.start();self.addCleanup(self.vault_patch.stop)
    def tearDown(self): self.tmp.cleanup()
    def prepare(self, **kw):
        params=dict(state_root=self.state,request_id='req-intake',task_id='TSK-20260907-intake',user_prompt='Review API A and API B.',
                    ledger=ledger(),provider=provider,intended_model='approved-model')
        params.update(kw);return intake.prepare(**params)
    def test_preserves_all_requirements_and_pending_and_raw_source_private(self):
        ref=self.prepare(); artifact=intake.resolve(self.state,ref)
        self.assertEqual(artifact['requirement_ledger'],ledger())
        self.assertEqual(artifact['brief']['requirement_dispositions'][0],{'requirement_id':'RA','status':'pending'})
        self.assertNotIn('user_prompt',artifact)
        self.assertEqual([row['stage'] for row in artifact['provenance']],['classify','shape'])
        sources=list((self.state/'intakes'/'req-intake').glob('source-*.json'))
        self.assertEqual(json.loads(sources[0].read_text())['user_prompt'],'Review API A and API B.')
    def test_replay_never_reinvokes_provider(self):
        reference=self.prepare()
        self.assertEqual(reference,self.prepare(provider=lambda **kw:self.fail('provider replayed')))
    def test_malformed_and_low_confidence_internal_recovery_zero_question(self):
        calls=[]
        def recovering(**kw):
            calls.append((kw['stage'],kw['attempt']))
            value,receipt=provider(**kw)
            if kw['stage']=='classify':
                if kw['attempt']==1:value=None
                elif kw['attempt']==2:value['classification_confidence']=0.2
            return value,receipt
        result=intake.resolve(self.state,self.prepare(provider=recovering))
        self.assertEqual(calls,[('classify',1),('classify',2),('classify',3),('shape',1)])
        self.assertEqual(result['brief']['open_questions'],[])
    def test_exhaustion_is_internal_block_and_resumes_without_budget_reset(self):
        calls=[]
        def malformed(**kw):
            calls.append(1);return None,provider(**kw)[1]
        with self.assertRaisesRegex(intake.IntakeError,'recovery_(?:exhausted|yielded)'):self.prepare(provider=malformed)
        with self.assertRaisesRegex(intake.IntakeError,'recovery_(?:exhausted|yielded)'):self.prepare(provider=malformed)
        with self.assertRaisesRegex(intake.IntakeError,'recovery_exhausted'):self.prepare(provider=malformed)
        self.assertEqual(len(calls),5)
    def test_model_mismatch_blocks(self):
        def wrong(**kw):
            value,receipt=provider(**kw);receipt['effective_model']='unapproved';return value,receipt
        with self.assertRaisesRegex(intake.IntakeError,'provenance'):self.prepare(provider=wrong)
    def test_brief_omission_expansion_and_invented_question_recover_or_block(self):
        for mutate in (lambda b:b['requirement_dispositions'].pop(0),lambda b:b['scope']['in'].append('Add feature C'),
            lambda b:b['open_questions'].append(dict(requirement_id='RB',kind='material_scope',question='May I fix formatting?')),
            lambda b:b.update(objective='Entirely unrelated task'),lambda b:b.update(workflow_id='privileged')):
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory() as temp:
                def wrong(**kw):
                    value,receipt=provider(**kw)
                    if kw['stage']=='shape':mutate(value)
                    return value,receipt
                with self.assertRaisesRegex(intake.IntakeError,'recovery_(?:exhausted|yielded)'):
                    self.prepare(provider=wrong,state_root=Path(temp)/'state')
    def test_actual_material_ambiguity_one_question_blocks_transport(self):
        doc=ledger();doc['requirements'][1]['requires_decision']='product_requirement'
        def ambiguous(**kw):
            value,receipt=provider(**kw)
            if kw['stage']=='shape':value['open_questions']=[dict(requirement_id='RB',kind='product_requirement',question='Which API B version is in scope?')]
            return value,receipt
        ref=self.prepare(ledger=doc,provider=ambiguous)
        with self.assertRaisesRegex(intake.IntakeError,'material_requirement_unresolved'):
            intake.for_order(self.state,dict(task_id='TSK-20260907-intake',request_id='req-intake',workflow_id='single_step_external_review',work_brief_ref=ref))
    def test_private_source_denied_benign_short_exact_words_allowed(self):
        for kind in ('private_transcript','secret'):
            with self.assertRaisesRegex(intake.IntakeError,'private_source'):self.prepare(source_kind=kind)
        ref=self.prepare(user_prompt='Review API B')
        self.assertEqual(intake.resolve(self.state,ref)['brief']['objective'],'Review API B')
    def test_material_question_cannot_be_silently_omitted(self):
        doc=ledger();doc['requirements'][1]['requires_decision']='material_scope'
        with self.assertRaisesRegex(intake.IntakeError,'recovery_(?:exhausted|yielded)'):
            self.prepare(ledger=doc)
        new=scope.revise(doc,expected_digest=scope.digest(doc),version='v2',replacements={'RB':'Review chosen API B'},
            additions=[],units=doc['task_units'],selected_unit_id='B',resolved_decisions=('RB',))
        self.assertNotIn('requires_decision',new['requirements'][1])
        self.assertEqual(new['requirements_history'][0]['resolved_decisions'],['RB'])
    def test_private_ledger_data_never_reaches_provider(self):
        doc=ledger();doc['private_context']={'raw_transcript':'private conversation'}
        with self.assertRaisesRegex(intake.IntakeError,'private_source'):
            self.prepare(ledger=doc,provider=lambda **kw:self.fail('private content sent'))
    def test_malformed_ledger_is_typed_block(self):
        with self.assertRaisesRegex(scope.ScopeError,'object_required'):self.prepare(ledger=None)

    def test_digest_swap_and_version_staleness(self):
        ref=self.prepare();artifact=intake.resolve(self.state,ref)
        path=self.state/'intakes'/'req-intake'/(ref['digest'][7:]+'.json')
        artifact['brief']['objective']='swapped';path.write_text(json.dumps(artifact))
        with self.assertRaisesRegex(intake.IntakeError,'digest_mismatch'):intake.resolve(self.state,ref)
    def test_approval_material_binds_brief_without_changing_legacy(self):
        record=dict(task_id='TSK-20260907-intake',request_id='req-intake',proposal={})
        with patch.object(frontdoor,'approval_provider_binding',return_value={}):
            old=frontdoor.approval_action_id(record)
            record['work_brief_ref']=self.prepare();new=frontdoor.approval_action_id(record)
            self.assertNotEqual(old,new)
            record['work_brief_ref']=dict(record['work_brief_ref'],digest='sha256:'+'f'*64)
            self.assertNotEqual(new,frontdoor.approval_action_id(record))
    def test_bound_classification_cannot_be_self_attested(self):
        with self.assertRaisesRegex(frontdoor.FrontdoorError,'host_intake'):
            frontdoor.proposed_request(state_root=self.state,task_id='TSK-20260907-intake',request_id='req-intake',
                user_prompt='Review API B',refs=['README.md'],classification=external_review_classification(classification_source='bounded_classifier_step'),
                allowed_paths=['README.md'],expires_at='run_terminal',frontdoor='manual',chat_session_id='test')

    def test_frontdoor_proposal_approval_drain_transport_and_tamper(self):
        doc=ledger()
        for unit in doc['task_units']:unit['allowed_paths']=['README.md']
        proposed=frontdoor.proposed_request(state_root=self.state,task_id='TSK-20260907-intake',request_id='req-intake',
            user_prompt='Review API A and API B.',refs=['README.md'],classification=None,allowed_paths=['README.md'],
            expires_at='run_terminal',frontdoor='manual',chat_session_id='fixture',intake_provider=provider,
            requirement_ledger=doc,intake_model='approved-model')
        self.assertEqual(proposed['decision'],'ok')
        record=frontdoor.read_json(frontdoor.request_path(self.state,'req-intake'))
        frontdoor.approve_request(state_root=self.state,request_id='req-intake',human_action_id=record['approval']['human_action_id'])
        created=frontdoor.create_run(state_root=self.state,request_id='req-intake',run_id='run-intake',resume_policy='manual')
        drained=frontdoor.drain_run(state_root=self.state,run_id='run-intake')
        self.assertEqual(drained['decision'],'ok');order=drained['work_order']
        self.assertNotIn('Review API A and API B.',order['instruction'])
        shaped=intake.for_order(self.state,order,expected_ref=created['workflow_run']['work_brief_ref'])
        self.assertEqual(shaped['brief']['objective'],'Review API B')
        self.assertEqual(len(shaped['requirement_ledger']['requirements']),2)
        self.assertNotIn('incidental_findings',shaped['requirement_ledger'])
        self.assertEqual(frontdoor.drain_run(state_root=self.state,run_id='run-intake')['drained'],False)
        path=self.state/'intakes'/'req-intake'/(order['work_brief_ref']['digest'][7:]+'.json')
        value=json.loads(path.read_text());value['brief']['objective']='swap';path.write_text(json.dumps(value))
        with self.assertRaisesRegex(frontdoor.FrontdoorError,'digest_mismatch'):
            frontdoor.drain_run(state_root=self.state,run_id='run-intake')


class ScopeTests(unittest.TestCase):
    def test_revision_addC_replaceB_preservesA_non_goals_findings_history(self):
        old=ledger(); units=copy.deepcopy(old['task_units']);units[1]['requirement_ids'].append('RC')
        new=scope.revise(old,expected_digest=scope.digest(old),version='v2',replacements={'RB':'Review new API B'},
            additions=[dict(requirement_id='RC',text='Also check C')],units=units,selected_unit_id='B')
        self.assertEqual(new['requirements'][0],old['requirements'][0]);self.assertEqual(new['incidental_findings'],old['incidental_findings'])
        self.assertEqual(new['requirements_history'][0]['previous_requirements'],old['requirements'])
        self.assertEqual(len(new['requirements']),3)
        with self.assertRaisesRegex(scope.ScopeError,'stale'):
            scope.revise(new,expected_digest=scope.digest(old),version='v3',replacements={},additions=[],units=units,selected_unit_id='B')
    def test_revision_cannot_drop_non_goals(self):
        old=ledger();units=copy.deepcopy(old['task_units']);units[1]['scope']['out']=['New exclusion']
        with self.assertRaisesRegex(scope.ScopeError,'non_goals_lost'):
            scope.revise(old,expected_digest=scope.digest(old),version='v2',replacements={},additions=[],units=units,selected_unit_id='B')
    def test_actual_git_hunks_reject_same_file_unrelated_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);git=lambda *a:subprocess.check_output(['git',*a],cwd=root,stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            text=''.join(str(n)+'\n' for n in range(1,31));(root/'app.txt').write_text(text);git('add','.');git('commit','-m','base')
            base=git('rev-parse','HEAD');doc=ledger();doc['task_units'][1]['change_contracts']=[dict(path='app.txt',base=base,
                requirement_ids=['RB'],operations=['edit'],ranges=[[1,2]],insertions=[2])]
            (root/'app.txt').write_text(text.replace('1\n','one\n',1))
            evidence=scope.validate_diff(root,doc,base=base,actual_paths=['app.txt'])
            self.assertEqual(evidence['changes'][0]['requirement_ids'],['RB'])
            (root/'app.txt').write_text(text.replace('1\n','one\n',1).replace('30\n','unrelated\n'))
            with self.assertRaisesRegex(scope.ScopeError,'hunk_outside_scope'):scope.validate_diff(root,doc,base=base,actual_paths=['app.txt'])
            with self.assertRaisesRegex(scope.ScopeError,'outside_selected_unit'):scope.validate_diff(root,doc,base=base,actual_paths=['dependency.lock'])
            doc['task_units'][1]['change_contracts'][0]['requirement_ids']=['RA']
            with self.assertRaisesRegex(scope.ScopeError,'binding_invalid'):scope.validate_diff(root,doc,base=base,actual_paths=[])
            contract=doc['task_units'][1]['change_contracts'][0]
            contract['requirement_ids']=['RB'];contract['operations']=['create']
            git('rm','-f','app.txt')
            with self.assertRaisesRegex(scope.ScopeError,'operation_outside_scope'):
                scope.validate_diff(root,doc,base=base,actual_paths=['app.txt'])
            contract['operations']=['delete']
            self.assertEqual(scope.validate_diff(root,doc,base=base,actual_paths=['app.txt'])['changes'][0]['operation'],'delete')


if __name__=='__main__':unittest.main()
