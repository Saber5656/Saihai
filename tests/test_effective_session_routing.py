"""Effective session selection, current policy and final invocation identity."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
PATH=ROOT/'organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py'
spec=importlib.util.spec_from_file_location('session_route_builder',PATH)
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


class SessionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.session=self.root/'session';self.session.mkdir()
        self.org=b.organization_id('session');self.role='tech-backend'
        self.row=b.role_agent_row_for(self.role,organization_instance_id=self.org)
        self.assertEqual(self.row['intended_model'],'gpt-5.6-luna')
        self.state={'session_id':'session','organization_instance_id':self.org,'route_generation':1}
        self.row['route_generation']=1
        self.save()
        lock=patch.object(b,'PROVIDER_POLICY_LAUNCH_LOCK_ROOT',self.root/'locks');lock.start();self.addCleanup(lock.stop)
        self.hook={'session_id':'session','organization_instance_id':self.org,'agent_id':self.role,'request_id':'request-one','prompt':'Review the approved fixture.'}
    def save(self):
        (self.session/'bootstrap.json').write_text(json.dumps(self.state))
        (self.session/'roster.json').write_text(json.dumps([self.row]))
    def select(self):return b.effective_session_route(state_root=self.root,session_id='session',organization_instance_id=self.org,role_id=self.role)
    def dispatch(self):return b.agent_dispatch(runtime='codex',state_root=self.root,hook_input=self.hook)
    def launch(self,selection,runner):
        row,error=b.canonical_codex_execution_policy(selection['row'],organization_instance_id=self.org);self.assertFalse(error)
        row['_effective_session_route']=selection
        with patch.object(b.shutil,'which',return_value=sys.executable):
            return b.launch_provider_with_canonical_policy(bound_execution_row=row,organization_instance_id=self.org,
                executable_name='codex',command_builder=lambda r:[sys.executable,'-c','print("fixture")'],runner=runner,timeout=5)

    def test_public_dispatch_real_fake_provider_uses_selected_luna_max(self):
        executable = self.root / 'fake-codex'
        events = [
            {'type':'thread.started','thread_id':'fixture-thread','model':'gpt-5.6-luna'},
            {'type':'item.completed','item':{'id':'answer','type':'agent_message','text':'fixture review complete'}},
            {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}},
        ]
        capture = self.root / 'captured-argv.json'
        executable.write_text('#!' + sys.executable + '\nimport sys,json\nfrom pathlib import Path\n'
            + 'Path(' + repr(str(capture)) + ').write_text(json.dumps(sys.argv))\n'
            + 'print(' + repr('\n'.join(json.dumps(event) for event in events)) + ')\n')
        executable.chmod(0o700)
        with patch.object(b.shutil,'which',return_value=str(executable)):
            result = self.dispatch()
        self.assertNotEqual(result.get('decision'),'block',result)
        argv=json.loads(capture.read_text())
        self.assertEqual(argv[argv.index('--model')+1],'gpt-5.6-luna')
        self.assertIn('model_reasoning_effort="max"',argv)
        self.assertEqual(result['sessionRoute']['generation'],1)
        self.assertEqual(result['agentDispatch']['effective_model'],'gpt-5.6-luna')
        self.assertFalse((self.session/'roster.json.lock.d').exists())

    def test_existing_session_supplies_organization_when_hook_omits_it(self):
        self.state['organization_instance_id']='existing-org'
        self.row=b.role_agent_row_for(self.role,organization_instance_id='existing-org')
        self.row['route_generation']=1;self.save()
        del self.hook['organization_instance_id']
        with patch.object(b,'codex_exec_agent_dispatch',return_value={}) as adapter:
            result=self.dispatch()
        self.assertEqual(result['sessionRoute']['organization_instance_id'],'existing-org')
        adapter.assert_called_once()

    def test_missing_adapter_is_host_failure_and_preserves_route(self):
        before=(self.session/'roster.json').read_bytes()
        with patch.object(b.shutil,'which',return_value=None):
            result=self.dispatch()
        self.assertEqual(result['reason'],'session_route_command_unavailable')
        self.assertEqual(result['origin_layer'],'provider_launch')
        self.assertFalse(result['provider_invoked'])
        self.assertEqual((self.session/'roster.json').read_bytes(),before)

    def test_facade_carries_binding_without_claiming_host_execution(self):
        hook=dict(self.hook,task_id='task-one',from_role='tech-backend',to_role=self.role,
            instruction='Review the bounded fixture.',expected_output='review report')
        with patch.object(b,'role_queue',return_value={'roleQueue':{'result':'queued'}}) as queue:
            result=b.agent_call(runtime='codex',state_root=self.root,hook_input=hook)
        receipt=result['agentCall']
        self.assertEqual(receipt['session_route_binding'],self.select()['binding'])
        self.assertEqual(queue.call_args.kwargs['hook_input']['payload']['session_route_binding'],receipt['session_route_binding'])
        self.assertFalse(receipt['provider_invoked'])
        self.assertEqual(receipt['host_execution_status'],'host_commissioning_unverified')
        self.hook['expected_session_route_digest']=receipt['session_route_binding']['digest']
        self.state['route_generation']=2;self.row['route_generation']=2;self.save()
        with patch.object(b,'codex_exec_agent_dispatch') as adapter:
            self.assertEqual(self.dispatch()['reason'],'session_route_receipt_stale')
        adapter.assert_not_called()

    def test_transport_status_does_not_confuse_cli_presence_with_host_authority(self):
        with patch.object(b.shutil,'which',return_value='/fixture/codex'):
            status=b.transport_status(runtime='codex',state_root=self.root,hook_input=self.hook)['transportStatus']
        self.assertTrue(status['providers']['codex_exec']['available'])
        self.assertEqual(status['host_execution_status'],'host_commissioning_unverified')
        self.assertEqual(status['origin_layer'],'host_authorization')

    def test_current_all_active_roles_stay_luna_max(self):
        rows=b.role_agent_rows(organization_instance_id=self.org)
        self.assertEqual(len(rows),35)
        self.assertTrue(all(r['provider']=='openai' and r['intended_model']=='gpt-5.6-luna' and r['execution_mode']=='codex' for r in rows))
        self.assertEqual(b.DEFAULT_CODEX_REASONING_EFFORT,'max')

    def test_valid_session_is_selected_and_passed_to_adapter(self):
        self.row['notes']='preserve session context';self.save()
        selected=self.select();self.assertEqual(selected['row']['notes'],'preserve session context')
        self.assertEqual(selected['binding']['source'],'validated_session')
        with patch.object(b,'codex_exec_agent_dispatch',return_value={'decision':'ok'}) as adapter:
            result=self.dispatch()
        self.assertEqual(adapter.call_args.kwargs['selected_route'],selected)
        self.assertEqual(result['sessionRoute'],selected['binding'])

    def test_genuine_roster_absence_alone_allows_compatibility_fallback(self):
        (self.session/'roster.json').unlink()
        self.assertEqual(self.select()['binding']['source'],'static_absent_roster')
        (self.session/'bootstrap.json').write_text('{bad')
        with self.assertRaises(ValueError):self.select()

    def test_corruption_missing_role_identity_generation_and_symlink_block(self):
        cases=['corrupt','missing_role','duplicate','wrong_session','wrong_org','stale','dangling','missing_bootstrap']
        for case in cases:
            with self.subTest(case=case):
                path=self.session/'roster.json'
                if path.is_symlink():path.unlink()
                self.state={'session_id':'session','organization_instance_id':self.org,'route_generation':1}
                self.save()
                if case=='corrupt':path.write_text('{bad')
                elif case=='missing_role':path.write_text(json.dumps([dict(self.row,agent_id='other')]))
                elif case=='duplicate':path.write_text(json.dumps([self.row,self.row]))
                elif case=='wrong_session':self.state['session_id']='other';self.save()
                elif case=='wrong_org':self.state['organization_instance_id']='other';self.save()
                elif case=='stale':self.state['route_generation']=2;self.save()
                elif case=='dangling':path.unlink();path.symlink_to(self.root/'missing')
                elif case=='missing_bootstrap':(self.session/'bootstrap.json').unlink()
                before=path.read_bytes() if path.is_file() else None
                with patch.object(b,'codex_exec_agent_dispatch') as adapter:
                    result=self.dispatch()
                self.assertEqual(result['decision'],'block');adapter.assert_not_called()
                if before is not None:self.assertEqual(path.read_bytes(),before)

    def test_duplicate_json_keys_and_oversized_state_are_not_absence(self):
        path=self.session/'bootstrap.json'
        for raw in ('{"session_id":"session","session_id":"other"}', ' '* (2*1024*1024+1)):
            path.write_text(raw)
            self.assertEqual(self.dispatch()['decision'],'block')

    def test_old_sol_or_anthropic_switch_is_not_current_authority(self):
        for provider,model,mode in [('openai','gpt-5.6-sol','codex'),('anthropic','claude-opus-4-6','claude')]:
            self.row.update(provider=provider,intended_model=model,execution_mode=mode);self.save()
            with patch.object(b,'codex_exec_agent_dispatch') as codex,patch.object(b,'claude_cli_agent_dispatch') as claude:
                self.assertEqual(self.dispatch()['decision'],'block')
            codex.assert_not_called();claude.assert_not_called()

    def test_no_caller_provider_override(self):
        for key in ('provider','model','intended_model','execution_mode','selected_route'):
            with patch.dict(self.hook,{key:'unapproved'}):
                self.assertEqual(self.dispatch()['reason'],'caller_provider_override_forbidden')

    def test_revalidation_at_launch_blocks_changed_generation_without_overwrite(self):
        selected=self.select();self.state['route_generation']=2;self.row['route_generation']=2;self.save()
        before=(self.session/'roster.json').read_bytes()
        with patch.object(b,'run_command_with_bounded_output') as runner:
            result=self.launch(selected,runner)
        self.assertEqual(result['status'],'policy_drift');runner.assert_not_called()
        self.assertEqual((self.session/'roster.json').read_bytes(),before)

    def test_unchanged_route_launches_actual_mock_process_under_existing_lease(self):
        result=self.launch(self.select(),b.run_command_with_bounded_output)
        self.assertEqual(result['status'],'started')
        self.assertEqual(result['completed'].returncode,0)
        self.assertIn('fixture',result['completed'].stdout)
        self.assertEqual(result['execution_row']['_effective_session_route']['binding'],self.select()['binding'])

    def test_static_generation_cannot_silently_become_a_session_route(self):
        (self.session/'roster.json').unlink();selected=self.select();self.save()
        with patch.object(b,'run_command_with_bounded_output') as runner:
            self.assertEqual(self.launch(selected,runner)['status'],'policy_drift')
        runner.assert_not_called()

    def test_role_runtime_mirror_matches(self):
        self.assertEqual(PATH.read_bytes(),(ROOT/'organization/roles/infra-team-bootstrap/scripts/itb_bootstrap_builder.py').read_bytes())


if __name__=='__main__':unittest.main()
