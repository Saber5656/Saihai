#!/usr/bin/env python3
"""3944515358: approved informational findings seal without requiring repair."""
import copy
import json
from test_review_lifecycle import ReviewLifecycleTests, OWNER
import review_lifecycle as lifecycle
import report_gate


def test_informational_review():
    fixture = ReviewLifecycleTests()
    fixture.setUp()
    try:
        run = fixture.run
        order = {'activation_scope':copy.deepcopy(run['activation']['activation_scope']),
                 'context_scope': {'allowed_paths':['src']},
                 'context_refs':[{'type':'repo_file','value':'src/app.py'}]}
        report = {'report_id':'info-review','provider_evidence':{},'result':'completed',
                  'review':{'status':'approved','findings':[{'finding_id':'INFO-1','status':'informational',
                      'severity':'info','evidence_refs':['src/app.py'],'summary':'Optional observation'}]}}
        view = report_gate.standard_review_view(report)
        assert view['result'] == 'findings'
        assert lifecycle.start_from_gated_findings(run, view, work_order=order, principal=OWNER)
        lifecycle.consume_gated_report(run, view, work_order=order, report_ref='reports/info.json', digest='sha256:'+'1'*64)
        assert lifecycle.next_review_action(run) == 'merge_preflight'
        assert not lifecycle.unresolved_keys(run['review_lifecycle'])
        assert run['review_lifecycle']['repair_rounds'] == 0
        assert run['review_lifecycle']['resolution_flow']['initial']['findings'][0]['finding_id']=='INFO-1'
        report['result']='blocked'
        assert report_gate.standard_review_view(report)['result']=='blocked'
    finally:
        fixture.tearDown()

if __name__ == '__main__':
    test_informational_review()
    print(json.dumps({'result':'pass','cases':1}))
