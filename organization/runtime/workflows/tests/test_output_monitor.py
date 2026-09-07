"""Actual host projection/ack observation with no additional bridge action."""
import json
from pathlib import Path
import tempfile
import unittest
from test_frontdoor_orchestrator import create_approved_run, load_server_module
import output_monitor


class OutputMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.host=load_server_module().frontdoor
        create_approved_run(self.root,request_id='req-output',run_id='run-output')
        self.owner=self.host.bridge_principal('codex','fixture')
        path=self.host.request_path(self.root,'req-output');record=self.host.read_json(path)
        record['owner_principal']=self.owner
        self.host.write_json(path,record)

    def status(self, now):
        return output_monitor.status(self.host,state_root=self.root,principal=self.host.default_manual_principal(),stale_seconds=10,now_epoch=now)

    def test_age_raise_ack_clear_and_replay_are_audited(self):
        first=self.status(100);self.assertFalse(first['stale_outputs'])
        raised=self.status(111);self.assertEqual(raised['stale_outputs'][0]['run_id'],'run-output')
        self.assertEqual(raised['stale_outputs'][0]['age_seconds'],11)
        self.status(112)
        projection,_=self.host.build_bridge_projection(state_root=self.root,request_id='req-output',principal=self.owner)
        self.host.bridge_ack_output(state_root=self.root,request_id='req-output',projection_digest=self.host.bridge_projection_digest(projection),frontdoor='codex',chat_session_id='fixture',principal=self.owner,enforce_rate_limit=False)
        result=self.status(113);self.assertFalse(result['stale_outputs']);self.assertTrue(result['outputs'][0]['acknowledged'])
        events=[json.loads(line) for line in (self.root/'audit/events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e['event_type']=='stale_output_raised' for e in events),1)
        self.assertEqual(sum(e['event_type']=='stale_output_cleared' for e in events),1)

    def test_run_state_change_invalidates_old_projection(self):
        before=self.status(100)['outputs'][0]['projection_digest']
        run=self.host.run_store.load_run(self.root,'run-output');run['run_state']='waiting_human'
        self.host.run_store.store_run(self.root,run,expected_current_state='created')
        after=self.status(200)['outputs'][0]
        self.assertNotEqual(before,after['projection_digest']);self.assertEqual(after['age_seconds'],0)

    def test_bridge_cannot_monitor_and_state_hook_is_never_executed(self):
        with self.assertRaisesRegex(self.host.FrontdoorError,'requires_host'):
            output_monitor.status(self.host,state_root=self.root,principal=self.owner)
        marker=self.root/'must-not-exist'
        self.host.write_json(self.root/'output-monitor-config.json',{'notification_hook':['touch',str(marker)],'stale_seconds':0})
        result=self.status(100)
        self.assertEqual(result['notification'],'disabled_no_hook');self.assertFalse(marker.exists())
        self.assertEqual(self.host.BRIDGE_ALLOWED_ACTIONS,['submit_request','read_projection','ack_output'])

if __name__=='__main__':unittest.main()
