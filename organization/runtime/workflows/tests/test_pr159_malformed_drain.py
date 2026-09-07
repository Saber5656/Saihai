#!/usr/bin/env python3
"""3944542972: malformed durable state is bounded without rewriting it."""
import json
import tempfile
from pathlib import Path
from test_run_store import valid_run
import run_store
import frontdoor_orchestrator as frontdoor


def test_malformed_drain():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw)
        for mutation in ({'step_history':[None]}, {'step_history':['bad']}, {'iteration':0}, {'iteration':None}):
            run=valid_run(workflow_id='standard_code_change')
            run.update(mutation)
            path=run_store.run_path(root,run['run_id'])
            run_store.atomic_write_json(path,run)
            before=path.read_bytes()
            result=frontdoor.drain_run(state_root=root,run_id=run['run_id'])
            assert result['decision']=='blocked' and result['reason']=='work_order_invalid',result
            assert path.read_bytes()==before
        run=valid_run(workflow_id='standard_code_change')
        del run['iteration']
        run_store.atomic_write_json(path,run)
        result=frontdoor.drain_run(state_root=root,run_id=run['run_id'])
        assert result['reason']=='work_order_invalid'
        assert not (root/'work-orders').exists()

if __name__ == '__main__':
    test_malformed_drain()
    print(json.dumps({'result':'pass','cases':1}))
