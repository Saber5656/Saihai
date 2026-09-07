#!/usr/bin/env python3
"""3944515355: a late stop blocks scheduling and a frozen capability."""
import json
from unittest.mock import patch
from test_review_lifecycle import ReviewLifecycleTests, OWNER, finding
import run_store
import run_lifecycle
import scoped_worker_executor as executor
import work_order_builder


def test_stopped_run():
    fixture = ReviewLifecycleTests()
    fixture.setUp()
    try:
        fixture.seal(fixture.enable_flow())
        fixture.event('findings', findings=[finding('late')])
        run = run_store.load_run(fixture.root, fixture.run['run_id'])
        run.update(run_state='step_queued', current_step='implement')
        run_store.store_run(fixture.root, run)
        snapshot = work_order_builder.snapshot_path(fixture.root, run['run_id'], 'implement', run['iteration'])
        for frozen in (False, True):
            if frozen:
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_text('{}')
            result = run_lifecycle.resume_run(fixture.root, run['run_id'], principal=OWNER)
            assert result['decision'] == 'blocked' and result['next_action'] == 'stopped', result
        capability = dict(run_id=run['run_id'], task_id=run['task_id'], step_id='implement', work_order_digest='frozen')
        with patch.object(executor, 'load_frozen_work_order', return_value=({}, 'frozen')) as load:
            try:
                executor.validate_live_run_authority(fixture.root, capability)
            except executor.ScopedWorkerError as exc:
                assert exc.reason_class == 'review_lifecycle_stopped'
            else:
                raise AssertionError('stopped capability accepted')
            load.assert_not_called()
    finally:
        fixture.tearDown()

if __name__ == '__main__':
    test_stopped_run()
    print(json.dumps({'result':'pass','cases':1}))
