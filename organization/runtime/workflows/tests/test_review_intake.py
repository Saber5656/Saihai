#!/usr/bin/env python3
"""Offline Bot intake candidates never authorize requests or acceptance."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_run_store import valid_run
from test_review_lifecycle import OWNER, SNAPSHOT, finding
import review_lifecycle as lifecycle
import run_store


class ReviewIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_id = 'run-store'
        self.create(self.run_id)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, run_id):
        run = valid_run(run_id=run_id, workflow_id='publication_required')
        run['activation']['activation_scope']['allowed_ops']['edit'] = True
        run['activation']['activation_scope']['allowed_paths'] = ['src']
        run_store.store_run(self.root, run)
        lifecycle.initialize(self.root, run_id, principal=OWNER, snapshot=SNAPSHOT)

    def prepare(self, **overrides):
        kwargs = dict(principal=OWNER, pr=136, bot='coderabbitai', policy_version='phase-v1', trigger_mode='manual')
        kwargs.update(overrides)
        return lifecycle.prepare_intake(self.root, self.run_id, **kwargs)

    def request(self, **overrides):
        data = dict(repository=SNAPSHOT['repository'], pr=136, bot='coderabbitai',
                    policy_version='phase-v1', snapshot=SNAPSHOT, request_id='request-1', trigger_mode='manual')
        data.update(overrides)
        return data

    def response(self, **overrides):
        data = dict(repository=SNAPSHOT['repository'], pr=136, bot='coderabbitai',
                    policy_version='phase-v1', snapshot=SNAPSHOT, request_id='request-1',
                    response_id='response-1', outcome='no_findings', findings=[])
        data.update(overrides)
        return data

    def event(self, kind, **overrides):
        key = lifecycle.intake_key(SNAPSHOT['repository'], 136, 'coderabbitai', 'phase-v1')
        return lifecycle.record_intake_candidate(self.root, self.run_id, principal=OWNER,
                                                event=dict(kind=kind, intake_key=key, **overrides))

    def observed(self):
        self.prepare()
        return self.event('request_observed', candidate=self.request())

    def test_logical_key_does_not_include_head_or_delivery(self):
        first = self.prepare()
        lifecycle.apply_event(self.root, self.run_id, principal=OWNER, event={'kind':'findings','findings':[finding()]})
        lifecycle.apply_event(self.root, self.run_id, principal=OWNER, event={'kind':'reserve_repair','batch_id':'repair-1'})
        lifecycle.apply_event(self.root, self.run_id, principal=OWNER,
                              event={'kind':'repair_produced','batch_id':'repair-1','snapshot':dict(SNAPSHOT, head='c'*40)})
        self.assertEqual(self.prepare(), first)
        self.assertEqual(len(lifecycle.observe(self.root, self.run_id)['intakes']), 1)

    def test_second_run_cannot_claim_existing_logical_owner_even_expired(self):
        self.prepare()
        old = run_store.load_run(self.root, self.run_id)
        old['activation']['activation_scope']['expires_at'] = '2000-01-01T00:00:00+00:00'
        run_store.store_run(self.root, old)
        self.create('run-second')
        self.run_id = 'run-second'
        with self.assertRaisesRegex(lifecycle.ReviewLifecycleError, 'intake_owned_by_other_run'):
            self.prepare()
        self.assertNotIn('intakes', lifecycle.observe(self.root, self.run_id))

    def test_corrupt_or_unreadable_or_quarantined_owner_is_not_absence(self):
        self.prepare()
        self.create('run-second')
        owner_path = run_store.run_path(self.root, self.run_id)
        self.run_id = 'run-second'
        original = owner_path.read_bytes()
        for content in (b'{broken', json.dumps({'run_id':'run-store'}).encode()):
            owner_path.write_bytes(content)
            with self.assertRaises((lifecycle.ReviewLifecycleError, run_store.RunStoreError)):
                self.prepare()
        owner_path.write_bytes(original)
        owner_path.chmod(0o644)
        with self.assertRaises((lifecycle.ReviewLifecycleError, run_store.RunStoreError)):
            self.prepare()
        owner_path.chmod(0o600)
        owner_path.rename(owner_path.with_name('run-store.corrupt-1.json'))
        with self.assertRaises((lifecycle.ReviewLifecycleError, run_store.RunStoreError)):
            self.prepare()

    def test_unknown_delivery_and_restart_never_reset_to_planned(self):
        self.prepare()
        unknown = self.event('delivery_unknown')
        self.assertEqual(unknown['request_status'], 'unknown')
        self.assertEqual(self.prepare(), unknown)
        self.assertEqual(self.event('delivery_unknown'), unknown)
        self.assertEqual(unknown['authentication_status'], 'integration_pending')
        self.assertEqual(unknown['policy_status'], 'inactive')
        observed = self.event('request_observed', candidate=self.request())
        self.assertEqual(self.event('delivery_unknown'), observed)

    def test_request_receipt_conflict_and_trigger_mode_conflict_rejected(self):
        observed = self.observed()
        self.assertEqual(self.event('request_observed', candidate=self.request()), observed)
        for request in (self.request(request_id='different'), self.request(trigger_mode='automatic_initial'),
                        self.request(snapshot=dict(SNAPSHOT, head='c'*40))):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('request_observed', candidate=request)
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.prepare(trigger_mode='automatic_initial')

    def test_no_findings_is_immutable_candidate_not_acceptance(self):
        self.observed()
        result = self.event('response_observed', candidate=self.response())
        self.assertEqual(result['first_response_candidate']['outcome'], 'no_findings')
        self.assertEqual(result['authentication_status'], 'integration_pending')
        duplicate = self.event('response_observed', candidate=self.response(response_id='delivery-changed'))
        self.assertEqual(duplicate, result)
        self.assertEqual(lifecycle.observe(self.root, self.run_id)['phase'], 'triage')
        self.assertFalse(any(k in result for k in ('accepted', 'verified', 'may_trigger', 'merge_ready')))

    def test_findings_candidate_does_not_grant_repairs_or_clear_existing_obligations(self):
        self.observed()
        row = dict(rule_id='r',path='src/app.py',anchor='main',severity='high',summary='candidate finding')
        result = self.event('response_observed', candidate=self.response(outcome='findings',findings=[row]))
        self.assertEqual(result['first_response_candidate']['findings'], [row])
        state = lifecycle.observe(self.root, self.run_id)
        self.assertEqual(state['findings'], {})
        self.assertEqual(state['repair_rounds'], 0)

    def test_rejects_silence_reaction_error_timeout_and_fake_authentication(self):
        self.observed()
        for response in (None, {}, self.response(outcome='silence'), self.response(outcome='reaction'),
                         self.response(outcome='error'), self.response(outcome='timeout'),
                         self.response(verified=True), self.response(outcome='findings'),
                         self.response(findings=[{'arbitrary':'data'}])):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('response_observed', candidate=response)
        self.assertIsNone(self.prepare()['first_response_candidate'])

    def test_wrong_bot_pr_repository_policy_or_snapshot_rejected(self):
        self.observed()
        for change in ({'bot':'someone'},{'pr':137},{'repository':'Other/Repo'},
                       {'policy_version':'different'},{'snapshot':dict(SNAPSHOT,base='d'*40)},
                       {'snapshot':dict(SNAPSHOT,head='c'*40)},{'request_id':'other'}):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('response_observed', candidate=self.response(**change))

    def test_response_before_request_observation_is_not_accepted(self):
        self.prepare()
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('response_observed', candidate=self.response())
        self.assertIsNone(self.prepare()['first_response_candidate'])

    def test_baseline_cannot_be_overwritten_and_later_blocker_is_retained(self):
        self.observed()
        self.event('response_observed', candidate=self.response())
        row = dict(rule_id='r',path='src/app.py',anchor='main',severity='critical',summary='late candidate')
        later = self.response(response_id='response-2',outcome='findings',findings=[row],snapshot=dict(SNAPSHOT,head='c'*40))
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('response_observed', candidate=later)
        result = self.event('later_response_observed', candidate=later)
        self.assertEqual(result['first_response_candidate'], self.response())
        self.assertEqual(len(result['later_response_candidates']), 1)
        self.assertEqual(self.event('later_response_observed',candidate=dict(later,response_id='another-delivery')),result)
        self.assertEqual(result['request_candidate']['request_id'],'request-1')
        self.assertEqual(lifecycle.observe(self.root,self.run_id)['repair_rounds'],0)

    def test_legacy_state_valid_and_malformed_intake_fails_store(self):
        self.assertNotIn('intakes', lifecycle.observe(self.root, self.run_id))
        self.prepare()
        run = run_store.load_run(self.root,self.run_id)
        key = next(iter(run['review_lifecycle']['intakes']))
        for change in ({'authentication_status':'verified'},{'policy_status':'active'},
                       {'request_status':'accepted'},{'original_snapshot':dict(SNAPSHOT,base='x')},
                       {'first_response_candidate':{'verified':True}}):
            bad = copy.deepcopy(run)
            bad['review_lifecycle']['intakes'][key].update(change)
            with self.assertRaises(run_store.RunStoreError):
                run_store.store_run(self.root,bad)

    def test_separate_bot_and_policy_keys_preserve_shared_repair_budget(self):
        first=self.prepare()
        other=self.prepare(bot='codex',trigger_mode='automatic_initial')
        policy=self.prepare(policy_version='phase-v2')
        self.assertNotEqual(first['bot'],other['bot'])
        self.assertNotEqual(first['policy_version'],policy['policy_version'])
        state=lifecycle.observe(self.root,self.run_id)
        self.assertEqual(len(state['intakes']),3)
        self.assertEqual(state['repair_rounds'],0)
        self.assertTrue(all(row['policy_status']=='inactive' for row in state['intakes'].values()))

    def test_later_candidate_capacity_preserves_baseline_and_prior_state(self):
        self.observed()
        self.event('response_observed',candidate=self.response())
        for number in range(32):
            row=dict(rule_id=f'r-{number}',path='src/app.py',anchor='main',severity='high',summary='late')
            self.event('later_response_observed',candidate=self.response(outcome='findings',findings=[row]))
        before=self.prepare()
        row=dict(rule_id='over-limit',path='src/app.py',anchor='main',severity='high',summary='late')
        with self.assertRaisesRegex(lifecycle.ReviewLifecycleError,'capacity'):
            self.event('later_response_observed',candidate=self.response(outcome='findings',findings=[row]))
        self.assertEqual(self.prepare(),before)

    def test_identity_case_cannot_duplicate_repository_or_bot(self):
        self.assertEqual(lifecycle.intake_key('Saber5656/Saihai',136,'CodeRabbitAI','phase-v1'),
                         lifecycle.intake_key('saber5656/saihai',136,'coderabbitai','phase-v1'))
        for repository in ('https://github.com/Saber5656/Saihai','../Saihai','Saber5656/Saihai/extra'):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                lifecycle.intake_key(repository,136,'coderabbitai','phase-v1')

    def test_schema_declares_optional_intake_without_changing_u1_required_fields(self):
        path=Path(__file__).resolve().parents[1] / 'schemas/review-lifecycle.schema.json'
        schema=json.loads(path.read_text())
        self.assertIn('intakes',schema['properties'])
        self.assertNotIn('intakes',schema['required'])

    def test_policy_example_cannot_activate_and_rejects_auto_manual_conflict(self):
        path = Path(__file__).resolve().parents[1] / 'profiles/review-phase-policy-v1.example.json'
        plan = json.loads(path.read_text())
        self.assertEqual(lifecycle.validate_intake_policy_plan(plan), [])
        for field,value in [('status','active'),('producer_contract','verified')]:
            bad=copy.deepcopy(plan);bad[field]=value
            self.assertTrue(lifecycle.validate_intake_policy_plan(bad))
        bad=copy.deepcopy(plan);bad['bots'][0]['automatic_on_push']=True
        self.assertTrue(lifecycle.validate_intake_policy_plan(bad))
        bad=copy.deepcopy(plan);bad['bots'][0]['automatic_on_open']=True
        self.assertTrue(lifecycle.validate_intake_policy_plan(bad))
        self.assertEqual(plan['settings_readback'],'pending')


if __name__ == '__main__':
    unittest.main(verbosity=2)
