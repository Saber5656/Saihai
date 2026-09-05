#!/usr/bin/env python3
"""Bounded review lifecycle regression tests (offline, no external authority)."""
from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_run_store import valid_run
import run_store
import review_lifecycle as lifecycle

OWNER = {'principal_type': 'harness_runner', 'principal_id': 'owner', 'authn_method': 'local_cli'}
SNAPSHOT = {'repository': 'Saber5656/Saihai', 'base': 'a' * 40, 'head': 'b' * 40}


def finding(rule='rule-1', *, task='TSK-run-store', mandatory=True, path='src/app.py', **extra):
    return dict(rule_id=rule, path=path, anchor='function:main', task_id=task,
                mandatory=mandatory, severity='high', evidence_ref='reports/review.json', **extra)


class ReviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = valid_run(workflow_id='standard_code_change')
        self.run['activation']['activation_scope']['allowed_paths'] = ['src']
        self.run['activation']['activation_scope']['allowed_ops']['edit'] = True
        run_store.store_run(self.root, self.run)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, **kwargs):
        return lifecycle.initialize(self.root, self.run['run_id'], principal=OWNER,
                                    snapshot=SNAPSHOT, **kwargs)

    def event(self, kind, **kwargs):
        return lifecycle.apply_event(self.root, self.run['run_id'], principal=OWNER,
                                     event={'kind': kind, **kwargs})

    def test_restart_retains_owner_budget_and_snapshot(self):
        self.start()
        self.event('findings', findings=[finding()])
        state = self.event('reserve_repair', batch_id='batch-1')
        importlib.reload(lifecycle)
        self.assertEqual(self.start(), state)
        self.assertEqual(state['repair_rounds'], 1)
        self.assertEqual(state['snapshot'], SNAPSHOT)
        self.assertEqual(state['integration_status'], 'integration_pending')

    def test_owner_conflict_and_watcher_cannot_mutate(self):
        self.start()
        for owner in [dict(OWNER, principal_id='other'), dict(OWNER, principal_type='main_agent_bridge')]:
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                lifecycle.apply_event(self.root, self.run['run_id'], principal=owner,
                                      event={'kind': 'findings', 'findings': [finding()]})
        self.assertEqual(self.start()['findings'], {})

    def test_duplicate_delivery_and_batch_do_not_spend_twice(self):
        self.start()
        self.event('findings', findings=[finding(external_id='old')])
        first = self.event('reserve_repair', batch_id='batch-1')
        self.event('findings', findings=[finding(external_id='new')])
        replay = self.event('reserve_repair', batch_id='batch-1')
        self.assertEqual(replay['repair_rounds'], 1)
        self.assertEqual(len(replay['findings']), 1)
        self.assertEqual(first['batches'], replay['batches'])
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('reserve_repair', batch_id='batch-2')

    def test_ci_pending_and_optional_findings_never_spend_or_reopen(self):
        self.start()
        for _ in range(6):
            self.event('ci_pending')
            self.event('findings', findings=[finding(task='incidental', mandatory=False)])
        state = self.start()
        self.assertEqual(state['repair_rounds'], 0)
        self.assertEqual(state['no_progress'], 0)
        self.assertEqual(state['phase'], 'triage')
        self.assertEqual(len(state['findings']), 1)
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('reserve_repair', batch_id='batch-1')

    def test_mandatory_incidental_stops_without_repair_grant(self):
        self.start()
        self.event('findings', findings=[finding(task='incidental')])
        state = self.event('findings', findings=[finding('optional', task='incidental', mandatory=False)])
        self.assertEqual(state['phase'], 'stopped')
        self.assertEqual(state['stop_reason'], 'incidental_mandatory')
        self.assertEqual(state['repair_rounds'], 0)
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('reserve_repair', batch_id='batch-1')

    def test_scope_and_live_activation_are_checked_before_reservation(self):
        self.start()
        self.event('findings', findings=[finding(path='other/app.py')])
        self.assertEqual(self.start()['phase'], 'stopped')
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('reserve_repair', batch_id='batch-1')

    def test_revoked_or_changed_activation_cannot_replay_reservation(self):
        self.start()
        self.event('findings', findings=[finding()])
        self.event('reserve_repair', batch_id='batch-1')
        run = run_store.load_run(self.root, self.run['run_id'])
        run['activation']['activation_status'] = 'revoked'
        # Simulate canonical revocation outside this module; preserve all review state.
        run_store.atomic_write_json(run_store.run_path(self.root, self.run['run_id']), run)
        with self.assertRaises((lifecycle.ReviewLifecycleError, run_store.RunStoreError)):
            self.event('reserve_repair', batch_id='batch-1')

    def test_two_failed_same_blocker_rounds_stop_and_replays_do_not_reset(self):
        self.start()
        self.event('findings', findings=[finding()])
        for number in (1, 2):
            self.event('reserve_repair', batch_id=f'batch-{number}')
            result = self.event('repair_failed', batch_id=f'batch-{number}')
            replay = self.event('repair_failed', batch_id=f'batch-{number}')
            self.assertEqual(result, replay)
        self.assertEqual(result['repair_rounds'], 2)
        self.assertEqual(result['phase'], 'stopped')
        self.assertEqual(result['stop_reason'], 'same_blocker_cap')
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('reserve_repair', batch_id='batch-3')

    def test_stricter_budget_is_retained_and_five_is_upper_bound(self):
        for bad in (0, 6, True):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.start(max_repairs=bad)
        self.start(max_repairs=1)
        self.event('findings', findings=[finding()])
        self.event('reserve_repair', batch_id='batch-1')
        state = self.event('repair_failed', batch_id='batch-1')
        self.assertEqual(state['stop_reason'], 'repair_budget_exhausted')
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.start(max_repairs=5)

    def test_changed_snapshot_requires_current_validation_and_preserves_obligations(self):
        self.start()
        self.event('findings', findings=[finding()])
        self.event('reserve_repair', batch_id='batch-1')
        new_snapshot = dict(SNAPSHOT, head='c' * 40)
        result = self.event('repair_produced', batch_id='batch-1', snapshot=new_snapshot)
        self.assertEqual(result['phase'], 'current_snapshot_validation')
        self.assertTrue(all(f['disposition'] == 'repair' for f in result['findings'].values()))
        self.event('findings', findings=[finding('optional', mandatory=False)])
        self.assertEqual(lifecycle.observe(self.root, self.run['run_id'])['phase'], 'current_snapshot_validation')
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('reserve_repair', batch_id='batch-2')
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.event('validation_passed', verified=True)

    def test_stale_base_and_unchanged_head_cannot_claim_repair_progress(self):
        self.start()
        self.event('findings', findings=[finding()])
        self.event('reserve_repair', batch_id='batch-1')
        for snapshot in (SNAPSHOT, dict(SNAPSHOT, base='d' * 40, head='c' * 40)):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('repair_produced', batch_id='batch-1', snapshot=snapshot)
        self.assertEqual(lifecycle.observe(self.root, self.run['run_id'])['phase'], 'repair')

    def test_conflicting_ownership_stops_and_optional_cannot_downgrade_blocker(self):
        self.start()
        self.event('findings', findings=[finding()])
        state = self.event('findings', findings=[finding(mandatory=False)])
        self.assertEqual(next(iter(state['findings'].values()))['disposition'], 'repair')
        state = self.event('findings', findings=[finding(task='incidental')])
        self.assertEqual(state['stop_reason'], 'contradictory_finding')

    def test_malformed_state_is_rejected_by_run_store(self):
        state = self.start()
        for field, value in [('repair_rounds', -1), ('max_repairs', 6), ('phase', 'complete'),
                             ('owner', {}), ('findings', []), ('integration_status', 'effective')]:
            with self.subTest(field=field):
                run = copy.deepcopy(self.run)
                run['review_lifecycle'] = dict(state, **{field: value})
                with self.assertRaises(run_store.RunStoreError):
                    run_store.store_run(self.root, run)

    def test_unknown_authority_fields_and_traversal_are_rejected(self):
        self.start()
        for candidate in [finding(path='../src/app.py'), finding(allowed=True), finding(path='/src/app.py')]:
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('findings', findings=[candidate])
        self.assertEqual(self.start()['findings'], {})

    def test_no_progress_cap_survives_changed_finding_set(self):
        self.start()
        self.event('findings', findings=[finding()])
        self.event('reserve_repair', batch_id='batch-1')
        self.event('repair_failed', batch_id='batch-1')
        self.event('findings', findings=[finding('second')])
        self.event('reserve_repair', batch_id='batch-2')
        state = self.event('repair_failed', batch_id='batch-2')
        self.assertEqual(state['stop_reason'], 'no_progress_cap')
        self.assertEqual(state['repair_rounds'], 2)

    def test_schema_is_linked_and_covers_durable_fields(self):
        root = Path(__file__).resolve().parents[1] / 'schemas'
        schema = json.loads((root / 'review-lifecycle.schema.json').read_text())
        run_schema = json.loads((root / 'workflow-run.schema.json').read_text())
        self.assertEqual(run_schema['properties']['review_lifecycle']['$ref'], 'review-lifecycle.schema.json')
        self.assertEqual(set(schema['required']), set(self.start()))
        self.assertFalse(schema['additionalProperties'])

    def test_expired_activation_blocks_initialization_and_live_reservation(self):
        run = run_store.load_run(self.root, self.run['run_id'])
        run['activation']['activation_scope']['expires_at'] = '2000-01-01T00:00:00+00:00'
        run_store.store_run(self.root, run)
        with self.assertRaises(lifecycle.ReviewLifecycleError):
            self.start()
        run['activation']['activation_scope']['expires_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        run_store.store_run(self.root, run)
        self.start()
        self.event('findings', findings=[finding()])

        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(3000, 1, 1, tzinfo=timezone.utc)

        with patch.object(lifecycle, 'datetime', ExpiredClock):
            with self.assertRaises(lifecycle.ReviewLifecycleError):
                self.event('reserve_repair', batch_id='batch-1')
        self.assertEqual(lifecycle.observe(self.root, self.run['run_id'])['repair_rounds'], 0)

    def test_readonly_observe_does_not_mutate_persisted_state(self):
        self.start()
        path = run_store.run_path(self.root, self.run['run_id'])
        before = path.read_bytes()
        for _ in range(5):
            lifecycle.observe(self.root, self.run['run_id'])
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
