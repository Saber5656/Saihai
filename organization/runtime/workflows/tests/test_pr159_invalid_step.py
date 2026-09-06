#!/usr/bin/env python3
"""3944542977: invalid persisted standard steps fail before snapshot access."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from test_run_store import valid_run
import run_store
import run_lifecycle
import work_order_builder


def test_invalid_step():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw)
        for step in ('obsolete-step', '../outside'):
            run=valid_run(workflow_id='standard_code_change',run_state='step_queued',current_step=step)
            run_store.store_run(root,run)
            with patch.object(work_order_builder,'snapshot_path') as snapshot:
                try:
                    run_lifecycle.resume_run(root,run['run_id'],principal={'principal_type':'manual_operator','principal_id':'manual-cli','authn_method':'local_cli'})
                except run_lifecycle.LifecycleError as exc:
                    assert exc.reason_class=='standard_step_invalid',exc.reason_class
                else:
                    raise AssertionError('invalid step accepted')
                snapshot.assert_not_called()

if __name__ == '__main__':
    test_invalid_step()
    print(json.dumps({'result':'pass','cases':1}))
