#!/usr/bin/env python3
"""3944542985: persisted lifecycle/step mismatch raises the drain-boundary type."""
import json
from test_review_lifecycle import ReviewLifecycleTests
from test_work_order_builder import build, standard_template
import work_order_builder


def test_review_action_error():
    fixture=ReviewLifecycleTests()
    fixture.setUp()
    try:
        fixture.seal(fixture.enable_flow())
        run=fixture.produced()
        tpl=standard_template()
        for step in (tpl['steps'][0],tpl['steps'][2]):
            try: build(fixture.root,run=run,template=tpl,step=step)
            except work_order_builder.WorkOrderError as exc:
                assert str(exc)=='review_work_order_not_requested:verify_original_findings'
            else: raise AssertionError('mismatched lifecycle step accepted')
    finally:
        fixture.tearDown()

if __name__ == '__main__':
    test_review_action_error()
    print(json.dumps({'result':'pass','cases':1}))
