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

    def quota(self, **overrides):
        row = {k:v for k,v in self.request().items() if k != 'trigger_mode'}
        row.update(reason='usage_limit', issuer='github-observer', evidence_ref='receipt/quota',
                   evidence_digest='a'*64)
        row.update(overrides)
        return row

    def alternate_request(self, **overrides):
        row = self.request(bot='chatgpt', request_id='alternate-1')
        row.update(overrides)
        return row

    def alternate_response(self, **overrides):
        row = self.response(bot='chatgpt', request_id='alternate-1')
        row.update(overrides)
        return row

    def quota_observed(self):
        self.observed()
        return self.event('quota_observed', candidate=self.quota())

    def test_quota_is_failure_candidate_and_never_success_or_authority(self):
        self.observed()
        lifecycle.apply_event(self.root, self.run_id, principal=OWNER,
                              event={'kind':'findings','findings':[finding()]})
        before = lifecycle.observe(self.root, self.run_id)
        result = self.event('quota_observed', candidate=self.quota())
        self.assertEqual(result['alternate']['status'], 'integration_pending')
        self.assertEqual(result['alternate']['failure_candidate']['reason'], 'usage_limit')
        self.assertIsNone(result['first_response_candidate'])
        after = lifecycle.observe(self.root, self.run_id)
        for key in ('phase','findings','repair_rounds','owner','snapshot'):
            self.assertEqual(after[key], before[key])
        for kind in ('fallback_authorized','alternate_accepted','quota_verified'):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event(kind, candidate=self.quota())

    def test_quota_rejects_general_failure_wrong_identity_and_fake_auth(self):
        self.observed()
        for change in ({'reason':'timeout'},{'reason':'error'},{'reason':'skipped'},
                       {'reason':'429'},{'issuer':''},{'verified':True},
                       {'snapshot':dict(SNAPSHOT,head='c'*40)},{'pr':137},
                       {'bot':'other'},{'request_id':'other'},{'evidence_digest':'bad'}):
            with self.subTest(change=change), self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('quota_observed', candidate=self.quota(**change))
        self.assertNotIn('alternate', self.prepare())
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('response_observed', candidate=self.response(outcome='usage_limit'))

    def test_alternate_requires_quota_then_separate_request_and_result(self):
        self.observed()
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_request_observed', candidate=self.alternate_request())
        self.event('quota_observed', candidate=self.quota())
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_response_observed', candidate=self.alternate_response())
        self.event('alternate_request_observed', candidate=self.alternate_request())
        result = self.event('alternate_response_observed', candidate=self.alternate_response())
        self.assertEqual(result['request_candidate']['request_id'], 'request-1')
        self.assertEqual(result['alternate']['request_candidate']['request_id'], 'alternate-1')
        self.assertEqual(result['alternate']['status'], 'integration_pending')
        self.assertEqual(result['authentication_status'], 'integration_pending')
        self.assertEqual(lifecycle.observe(self.root,self.run_id)['phase'], 'triage')

    def test_alternate_duplicates_restart_unknown_delivery_and_conflicts(self):
        result = self.quota_observed()
        self.assertEqual(self.event('quota_observed',candidate=self.quota()),result)
        unknown = self.event('alternate_delivery_unknown')
        self.assertEqual(unknown['alternate']['request_status'], 'unknown')
        self.assertEqual(self.prepare(),unknown)
        observed = self.event('alternate_request_observed',candidate=self.alternate_request())
        self.assertEqual(self.event('alternate_delivery_unknown'), observed)
        for kind,candidate in (
            ('quota_observed',self.quota(evidence_digest='b'*64)),
            ('alternate_request_observed',self.alternate_request(request_id='another'))):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event(kind,candidate=candidate)
        first=self.event('alternate_response_observed',candidate=self.alternate_response(outcome='error'))
        self.assertEqual(self.event('alternate_response_observed',candidate=self.alternate_response(
            outcome='error',response_id='redelivery')),first)
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_response_observed',candidate=self.alternate_response())

    def test_alternate_negative_preserves_primary_findings_and_cannot_be_rewritten(self):
        self.quota_observed()
        row=dict(rule_id='r',path='src/app.py',anchor='main',severity='high',summary='blocking')
        self.event('response_observed',candidate=self.response(outcome='findings',findings=[row]))
        self.event('alternate_request_observed',candidate=self.alternate_request())
        result=self.event('alternate_response_observed',candidate=self.alternate_response(outcome='rejected',findings=[row]))
        self.assertEqual(result['first_response_candidate']['findings'],[row])
        self.assertEqual(result['alternate']['response_candidate']['outcome'],'rejected')
        self.assertEqual(result['alternate']['status'],'integration_pending')

    def test_alternate_rejects_stale_result_and_wrong_request_snapshot(self):
        self.quota_observed()
        for change in ({'bot':'codex'},{'request_id':'request-1'},
                       {'snapshot':dict(SNAPSHOT,head='c'*40)},{'verified':True}):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('alternate_request_observed',candidate=self.alternate_request(**change))
        self.event('alternate_request_observed',candidate=self.alternate_request())
        lifecycle.apply_event(self.root,self.run_id,principal=OWNER,event={'kind':'findings','findings':[finding()]})
        lifecycle.apply_event(self.root,self.run_id,principal=OWNER,event={'kind':'reserve_repair','batch_id':'b'})
        lifecycle.apply_event(self.root,self.run_id,principal=OWNER,event={
            'kind':'repair_produced','batch_id':'b','snapshot':dict(SNAPSHOT,head='c'*40)})
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_response_observed',candidate=self.alternate_response())
        self.assertEqual(self.prepare()['alternate']['request_candidate']['snapshot'],SNAPSHOT)

    def test_alternate_corrupt_durable_authority_and_binding_rejected(self):
        self.quota_observed()
        run=run_store.load_run(self.root,self.run_id)
        key=next(iter(run['review_lifecycle']['intakes']))
        for change in ({'status':'accepted'},{'request_status':'observed'},
                       {'failure_candidate':self.quota(reason='timeout')}):
            bad=copy.deepcopy(run)
            bad['review_lifecycle']['intakes'][key]['alternate'].update(change)
            with self.assertRaises(run_store.RunStoreError):
                run_store.store_run(self.root,bad)

    def test_existing_configured_chatgpt_intake_is_reused_without_new_request(self):
        self.quota_observed()
        self.prepare(bot='codex',trigger_mode='automatic_initial')
        key=lifecycle.intake_key(SNAPSHOT['repository'],136,'codex','phase-v1')
        def source_event(kind,candidate):
            return lifecycle.record_intake_candidate(self.root,self.run_id,principal=OWNER,
                event=dict(kind=kind,intake_key=key,candidate=candidate))
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_request_observed',candidate=self.alternate_request())
        request=self.request(bot='codex',request_id='existing-auto',trigger_mode='automatic_initial')
        source_event('request_observed',request)
        response=self.response(bot='codex',request_id='existing-auto')
        source_event('response_observed',response)
        result=self.event('alternate_existing_intake_linked',source_intake_key=key)
        self.assertEqual(result['alternate']['request_candidate'],request)
        self.assertEqual(result['alternate']['response_candidate'],response)
        self.assertEqual(result['alternate']['source_intake_key'],key)
        self.assertEqual(result['alternate']['status'],'integration_pending')
        self.assertEqual(self.event('alternate_existing_intake_linked',source_intake_key=key),result)
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_request_observed',candidate=self.alternate_request())

    def test_existing_alternate_wrong_pr_or_stale_identity_cannot_link(self):
        self.quota_observed()
        self.prepare(pr=137,bot='codex',trigger_mode='automatic_initial')
        key=lifecycle.intake_key(SNAPSHOT['repository'],137,'codex','phase-v1')
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('alternate_existing_intake_linked',source_intake_key=key)

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


class ObservationAdapterTests(unittest.TestCase):
    def setUp(self):
        import review_observation_adapter
        self.adapter = review_observation_adapter
        self.expected = dict(repository='Saber5656/Saihai', pr=154, base='a'*40,
                             head='b'*40, request_ref='coderabbit-run:12345678-1234-1234-1234-123456789abc')

    def quota_body(self):
        return ('<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n'
                '<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n\n'
                '> [!WARNING]\n> ## Review limit reached\n> \n'
                '> **Run ID**: `12345678-1234-1234-1234-123456789abc`\n> \n'
                '> Reviewing files that changed from the base of the PR and between '+ 'a'*40+' and '+'b'*40+'.\n'
                '\n<!-- end of auto-generated comment: rate limited by coderabbit.ai -->')

    def observation(self, **changes):
        row=dict(id=10, user=dict(id=136622811,login='coderabbitai[bot]',type='Bot'),
                 html_url='https://github.com/Saber5656/Saihai/pull/154#issuecomment-10',
                 body=self.quota_body(), created_at='2026-09-06T07:00:00Z', updated_at='2026-09-06T07:01:00Z')
        row.update(changes)
        return row

    def classify(self, row=None, expected=None):
        raw=json.dumps(row if row is not None else self.observation()).encode()
        return self.adapter.classify_observation(raw, expected=expected or self.expected)

    def test_quota_is_pure_candidate_with_version_and_raw_digest(self):
        row=self.observation();before=copy.deepcopy(row)
        result=self.classify(row)
        self.assertEqual(row,before)
        self.assertEqual(result['classification'],'quota_candidate')
        self.assertEqual(result['association_status'],'candidate_match')
        self.assertEqual(result['authentication_status'],'integration_pending')
        self.assertEqual(result['parser_version'],'github-review-observation-v1')
        import hashlib
        self.assertEqual(result['raw_digest'],hashlib.sha256(json.dumps(row).encode()).hexdigest())
        self.assertEqual(result['provider'],'coderabbit')
        self.assertFalse(any(key in result for key in ('grant','accepted','dispatch','merge_ready')))

    def test_actor_numeric_id_login_and_type_must_all_match(self):
        for actor in (dict(id=1,login='coderabbitai[bot]',type='Bot'),
                      dict(id=136622811,login='someone',type='Bot'),
                      dict(id=136622811,login='coderabbitai[bot]',type='User'),
                      dict(id=True,login='coderabbitai[bot]',type='Bot')):
            with self.subTest(actor=actor):
                result=self.classify(self.observation(user=actor))
                self.assertNotEqual(result['classification'],'quota_candidate')
                self.assertIn('actor_mismatch',result['pending_reasons'])

    def test_quoted_spoof_general_error_skip_and_ambiguous_grammar_are_not_quota(self):
        body=self.quota_body()
        for value in ('Review rate limited.','HTTP 429','timeout','skipped',
                      '```\n'+body+'\n```','User example:\n'+body,
                      body+'\n'+body,body+'\n<!-- recent_review_start -->',
                      body.replace('> **Run ID**', '> Review finished.\n> **Run ID**'),
                      body.replace('> **Run ID**', '> Unexpected transport error. No usage limit was reached.\n> **Run ID**'),
                      body.replace('> **Run ID**', '> Review skipped because the pull request is closed.\n> **Run ID**'),
                      body+'\n\n<!-- tips_start -->\nReview finished. No actionable comments were generated.\n<!-- tips_end -->',
                      body.replace('Review limit reached','Unexpected error')):
            with self.subTest(value=value[:30]):
                self.assertNotEqual(self.classify(self.observation(body=value))['classification'],'quota_candidate')

    def test_command_reply_grammar_does_not_invent_snapshot(self):
        body=('<!-- This is an auto-generated reply by CodeRabbit -->\n'
              '<!-- CodeRabbit review command invocation: v2:'+'c'*64+' -->\n'
              '<details>\n<summary>⚠️ Action not completed</summary>\n\nReview rate limited.\n\n'
              '> Note: CodeRabbit is an incremental review system and does not re-review already reviewed commits. '
              'This command is applicable only when automatic reviews are paused.\n\n</details>')
        result=self.classify(self.observation(body=body))
        self.assertEqual(result['classification'],'quota_candidate')
        self.assertEqual(result['association_status'],'pending')
        self.assertIsNone(result['snapshot']['base'])
        self.assertIn('base_missing',result['pending_reasons'])
        self.assertNotEqual(self.classify(self.observation(body=body.replace('Review rate limited.','Review finished.')))['classification'],'quota_candidate')

    def test_request_and_snapshot_mismatch_remain_pending(self):
        for field,value in [('request_ref','other'),('base','c'*40),('head','d'*40)]:
            result=self.classify(expected=dict(self.expected,**{field:value}))
            self.assertEqual(result['classification'],'quota_candidate')
            self.assertEqual(result['association_status'],'pending')
            self.assertIn(field+'_mismatch',result['pending_reasons'])
        result=self.classify(expected=dict(self.expected,request_ref=None))
        self.assertIn('expected_request_ref_missing',result['pending_reasons'])

    def test_repository_pr_and_url_spoof_are_pending(self):
        for url in ('https://github.com/Other/Repo/pull/154#issuecomment-10',
                    'https://github.com/Saber5656/Saihai/pull/155#issuecomment-10',
                    'https://evil.example/Saber5656/Saihai/pull/154#issuecomment-10',
                    'https://github.com/Saber5656/Saihai/pull/154#issuecomment-11'):
            result=self.classify(self.observation(html_url=url))
            self.assertEqual(result['association_status'],'pending')
            self.assertIn('resource_identity_mismatch',result['pending_reasons'])

    def test_real_review_identity_does_not_infer_pass_or_current_base(self):
        row=self.observation(id=20,user=dict(id=199175422,login='chatgpt-codex-connector[bot]',type='Bot'),
            html_url='https://github.com/Saber5656/Saihai/pull/154#pullrequestreview-20',
            state='COMMENTED',commit_id='b'*40,submitted_at='2026-09-06T07:02:00Z')
        result=self.classify(row)
        self.assertEqual(result['classification'],'review_observed')
        self.assertEqual(result['provider'],'chatgpt')
        self.assertEqual(result['review_state'],'COMMENTED')
        self.assertEqual(result['snapshot']['head'],'b'*40)
        self.assertIsNone(result['snapshot']['base'])
        self.assertIsNone(result['request_ref'])
        self.assertEqual(result['association_status'],'pending')
        result=self.classify(row,expected=dict(self.expected,head='d'*40))
        self.assertIn('head_mismatch',result['pending_reasons'])

    def test_summary_comment_is_not_review_result_and_negative_state_is_retained(self):
        actor=dict(id=199175422,login='chatgpt-codex-connector[bot]',type='Bot')
        result=self.classify(self.observation(user=actor,body='Completed. No findings.'))
        self.assertNotEqual(result['classification'],'review_observed')
        row=self.observation(id=20,user=actor,state='CHANGES_REQUESTED',commit_id='b'*40,
            html_url='https://github.com/Saber5656/Saihai/pull/154#pullrequestreview-20',submitted_at='2026-09-06T07:02:00Z')
        self.assertEqual(self.classify(row)['review_state'],'CHANGES_REQUESTED')

    def test_updated_content_has_distinct_observation_digest(self):
        first=self.classify()
        second=self.classify(self.observation(updated_at='2026-09-06T07:02:00Z'))
        self.assertNotEqual(first['raw_digest'],second['raw_digest'])
        self.assertNotEqual(first['observation_key'],second['observation_key'])
        self.assertEqual(first,self.classify())

    def test_malformed_input_is_bounded_and_never_classified_as_quota(self):
        for raw in (b'{broken',b'[]',b'{"id":1,"id":2}',b'x'*(1024*1024+1)):
            result=self.adapter.classify_observation(raw,expected=self.expected)
            self.assertEqual(result['association_status'],'pending')
            self.assertNotEqual(result['classification'],'quota_candidate')
        for changes in ({'updated_at':'bad'},{'id':True},{'body':[]},{'user':None}):
            self.assertNotEqual(self.classify(self.observation(**changes))['classification'],'quota_candidate')


if __name__ == '__main__':
    unittest.main(verbosity=2)
