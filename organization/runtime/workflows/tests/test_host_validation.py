"""Strict actual observations, non-impact reuse and runtime profile wiring."""
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import host_validation as validation
import trusted_local_executor as local
from test_trusted_local_executor import TrustedLocalTests


class ObservationTests(unittest.TestCase):
    def test_zero_skipped_bool_missing_and_fake_pass_are_rejected(self):
        for payload in [{'result':'pass','cases':0},{'result':'pass','cases':True},{'result':'pass'},
                        {'result':'pass','cases':1,'skipped':1},{'result':'pass','cases':1,'unknown':1}]:
            row=validation.observe([sys.executable,'test_x.py'],json.dumps(payload).encode(),b'',0)
            self.assertFalse(row['passed'])
        row=validation.observe([sys.executable,'test_x.py'],b'',b'',0)
        self.assertFalse(row['passed'])

    def test_actual_unittest_result_and_non_test_commands_are_distinct(self):
        code='import unittest; unittest.TextTestRunner().run(unittest.TestSuite([unittest.FunctionTestCase(lambda:None)]))'
        done=subprocess.run([sys.executable,'-c',code],capture_output=True)
        row=validation.observe([sys.executable,'-m','unittest'],done.stdout,done.stderr,done.returncode)
        self.assertTrue(row['passed']);self.assertEqual(row['executed'],1)
        check=validation.observe([sys.executable,'-c','assert True'],b'',b'',0)
        self.assertTrue(check['passed']);self.assertIsNone(check['executed']);self.assertEqual(check['kind'],'check')

    def test_full_output_requires_every_suite_and_contract(self):
        suite={'result':'pass','status':'passed','exit_code':0,'executed':1,'failed':0,'skipped':0,'unknown':0}
        data={'result':'pass','compiled':True,'suites':[suite],'contracts':[{'result':'pass'}]}
        self.assertTrue(validation.observe(['python','validate_all.py'],json.dumps(data), '',0)['passed'])
        data['suites'][0]['executed']=True
        self.assertFalse(validation.observe(['python','validate_all.py'],json.dumps(data),'',0)['passed'])


class HostRuntimeTests(TrustedLocalTests):
    def test_luna_effort_is_explicit_without_authority_change(self):
        auth=dataclasses.replace(self.auth,model='gpt-5.6-luna')
        before=dataclasses.asdict(auth)
        argv=local._argv(auth,self.repo,self.root/'out')
        self.assertIn('model_reasoning_effort="max"',argv)
        self.assertEqual(dataclasses.asdict(auth),before)

    def test_runtime_blocks_exit_zero_with_zero_tests(self):
        command=(sys.executable,'-c','print(\'{"result":"pass","cases":0}\')')
        auth=dataclasses.replace(self.auth,validation_commands=(command,))
        with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):
            local.execute(self.request,auth,self.state)
        receipt=json.loads((self.state/'trusted-local/exec-usage/validation.json').read_text())
        self.assertEqual(receipt['commands'][0]['exit'],0)
        self.assertFalse(receipt['commands'][0]['passed'])

    def test_reuse_requires_same_source_commands_and_executable(self):
        result=local.execute(self.request,self.auth,self.state)
        path=Path(result['report']['validation']['evidence_path']);receipt=json.loads(path.read_text())
        self.assertTrue(validation.reusable(receipt,root=self.repo,commands=self.auth.validation_commands))
        receipt['tree']='f'*40  # Commit identity itself is not a source dependency.
        self.assertTrue(validation.reusable(receipt,root=self.repo,commands=self.auth.validation_commands))
        (self.repo/'app.txt').write_text('changed again')
        self.assertFalse(validation.reusable(receipt,root=self.repo,commands=self.auth.validation_commands))
        (self.repo/'app.txt').write_text('after\n')
        self.assertFalse(validation.reusable(receipt,root=self.repo,commands=((sys.executable,'-c','assert False'),)))

    def test_same_input_reuse_does_not_spawn_validation_again(self):
        result=local.execute(self.request,self.auth,self.state)
        path=Path(result['report']['validation']['evidence_path']);receipt=json.loads(path.read_text())
        new=path.with_name('reused.json');identity={k:receipt[k] for k in ('tree','diff_digest')}
        original = local.subprocess.run
        calls = []
        def observe_run(args, **kwargs):
            if len(args)>1 and args[1]=='sandbox': calls.append(args)
            return original(args, **kwargs)
        with patch.object(local.subprocess,'run',side_effect=observe_run):
            output=local._validate(self.repo,self.auth,identity,new,reuse_from=path)
        self.assertEqual(calls, [])
        self.assertIn('reused_from_digest',output)

class ProfileTests(TrustedLocalTests):
    def plan(self, kind='docs'):
        import hashlib
        profile=json.loads((Path(__file__).resolve().parents[1]/'profiles/delivery'/f'{kind}.json').read_text())
        profile['repository']=self.auth.publication.repository
        profile['release']['owner']='fixture/release-team'
        owners={'ci':'fixture-ci','cd':'fixture-cd','rollback':'fixture-ops'}
        owner={'repository':profile['repository'],'profile_digest':validation.digest(profile),'owners':owners,
               'targets':profile['release']['targets'],'rollback_prerequisites':profile['release']['rollback_prerequisites']}
        def save(name,value):
            raw=json.dumps(value).encode();path=self.root/name;path.write_bytes(raw);path.chmod(0o600)
            return {'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        owner_ref=save('owner.json',owner)
        layers={name:{'required':profile['layers'].get(name,{}).get('required',False),
                      'reason':'Host-selected applicability for this fixture task','commands':[0]} for name in ('static','unit','feature','e2e','build','security','full','device')}
        plan={'profile':profile,'layers':layers,'behavior':'docs_only' if kind=='docs' else 'behavioral',
              'owners':owners,'owner_evidence':owner_ref,'artifact':None,'device_evidence':None,'tdd':None}
        return plan,save

    def test_actual_host_profile_and_optional_authority_digest_compatibility(self):
        legacy=dataclasses.asdict(self.auth);legacy.pop('validation_profile');legacy.pop('intake_digest');legacy['publication'].pop('expected_assignees')
        self.assertEqual(local.authorization_payload(self.auth),legacy)
        plan,save=self.plan();ref=save('profile.json',plan);ref.pop('bytes');ref['path']=str(self.root/'profile.json')
        command=(sys.executable,'-c',"import unittest; unittest.TextTestRunner().run(unittest.TestSuite([unittest.FunctionTestCase(lambda:None)]))")
        auth=dataclasses.replace(self.auth,validation_profile=ref,validation_commands=(command,))
        result=local.execute(self.request,auth,self.state)
        receipt=json.loads(Path(result['report']['validation']['evidence_path']).read_bytes())
        self.assertEqual(receipt['delivery_profile']['state'],'passed')
        self.assertIsNone(receipt.get('reused_from_digest'))

    def test_worker_owned_profile_and_changed_profile_are_rejected(self):
        plan,save=self.plan();ref=save('profile.json',plan);ref.pop('bytes');ref['path']=str(self.root/'profile.json')
        (self.root/'profile.json').write_text('{}')
        with self.assertRaises(validation.ValidationError):validation.load_profile(ref,self.repo)
        ref['path']=str(self.repo/'profile.json');(self.repo/'profile.json').write_text('{}');(self.repo/'profile.json').chmod(0o600)
        with self.assertRaises(validation.ValidationError):validation.load_profile(ref,self.repo)

    def test_owner_mismatch_and_runtime_docs_exemption_rejected(self):
        plan,save=self.plan();source=(plan,self.root)
        args=dict(root=self.repo,repository=self.auth.publication.repository,commands=self.auth.validation_commands,
                  observations=[{'passed':True,'kind':'tests','executed':1}],changed_paths=['app.txt'])
        plan['owner_evidence']=save('owner.json',{'repository':'unrelated'})
        with self.assertRaisesRegex(validation.ValidationError,'project_owner_evidence_mismatch'):validation.assess_profile(source,**args)
        args['changed_paths']=['organization/runtime/instructions.md']
        with self.assertRaisesRegex(validation.ValidationError,'runtime_change_is_not_docs_only'):validation.assess_profile(source,**args)

    def test_required_full_cannot_be_satisfied_by_one_check_or_shard(self):
        plan,save=self.plan()
        plan['layers']['full']['required']=True
        for row in ({'passed':True,'kind':'tests','executed':1},
                    {'passed':True,'kind':'tests','executed':1,'count_method':'suite_results','selection':{'kind':'shard','index':0,'count':8}}):
            with self.assertRaisesRegex(validation.ValidationError,'shard_is_not_full_validation'):
                validation.assess_profile((plan,self.root),root=self.repo,repository=self.auth.publication.repository,
                    commands=self.auth.validation_commands,observations=[row],changed_paths=['app.txt'])

    def test_simulator_and_wrong_build_are_not_physical_device_results(self):
        plan,save=self.plan('mobile');(self.repo/'artifact.bin').write_bytes(b'build')
        import hashlib
        plan['artifact']={'path':'artifact.bin','sha256':hashlib.sha256(b'build').hexdigest(),'bytes':5}
        for surface,sha in [('simulator',plan['artifact']['sha256']),('physical','f'*64)]:
            device={'surface':surface,'model':'fixture phone','os':'fixture OS','operation':'open app','start':'2026-01-01T00:00:00Z',
                    'end':'2026-01-01T00:01:00Z','status':'passed','artifact_sha256':sha,'image':save('image.json',{'fixture':True})}
            plan['device_evidence']=save('device.json',device)
            with self.assertRaisesRegex(validation.ValidationError,'physical_device_evidence_invalid'):
                validation.assess_profile((plan,self.root),root=self.repo,repository=self.auth.publication.repository,
                    commands=self.auth.validation_commands,observations=[{'passed':True,'kind':'tests','executed':1}],changed_paths=['app.txt'])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (ObservationTests, HostRuntimeTests, ProfileTests):
        for name in cls.__dict__:
            if name.startswith('test_'): suite.addTest(cls(name))
    return suite

if __name__=='__main__':unittest.main()
