#!/usr/bin/env python3
"""Read-only evidence checks; fixtures describe local results, never authority."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import validation_evidence as ve
except ModuleNotFoundError:
    ve = None


class EvidenceTests(unittest.TestCase):
    def test_missing_and_self_asserted_evidence_is_not_success(self):
        self.assertIsNotNone(ve, 'Required evidence consumer is absent')
        for value in ({}, {'authenticated': True, 'status': 'success'}, None, True, []):
            self.assertTrue(ve.validate_validation_evidence(value))

    def test_duplicate_json_is_rejected_before_assessment(self):
        self.assertIsNotNone(ve, 'No bounded evidence parser exists')
        for raw in (b'{"a":1,"a":2}', b'[]', b'{"x":NaN}', b'{'):
            with self.assertRaises(ve.EvidenceError):
                ve.parse_json(raw)

    def test_cli_cannot_turn_self_asserted_success_into_authority(self):
        script = ROOT / 'scripts/validation_evidence.py'
        self.assertTrue(script.exists(), 'No production evidence CLI exists')
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory); (p/'request.json').write_text('{"authenticated":true,"status":"success"}')
            result = subprocess.run([sys.executable, str(script), '--root', str(p), '--request', 'request.json'], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIs(json.loads(result.stdout)['authorizes_execution'], False)



class NativeAndApplicabilityTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'tests').mkdir(); (self.root/'.github/workflows').mkdir(parents=True)
        self.test = self.root/'tests/test_case.py'
        self.test.write_text("import unittest\nclass Case(unittest.TestCase):\n def test_one(self): self.assertTrue(True)\nunittest.main()\n")
        workflow = {'name':'Test','on':{'push':{}},'jobs':{'test':{'runs-on':'macos-14','steps':[{'run':'python3 tests/test_case.py'}]}}}
        (self.root/'.github/workflows/validate.yml').write_text(json.dumps(workflow))
        (self.root/'deps.lock').write_text('fixture dependencies\n')
        self.runtime = {'version':'3.11.16','machine':'arm64','url':'https://example.invalid/never-downloaded','sha256':'a'*64,'size':1,'interpreter':'python/bin/python3.11'}
        lock = {'python':{'Darwin-arm64':self.runtime},'dependency_lock':{'path':'deps.lock','sha256':ve.sha((self.root/'deps.lock').read_bytes())}}
        (self.root/'.github/delivery-toolchain.lock.json').write_text(json.dumps(lock))
        def git(*args):
            return subprocess.check_output(['git',*args],cwd=self.root,stderr=subprocess.DEVNULL).decode().strip()
        self.git = git
        git('init'); git('add','.'); git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture')
        self.head = git('rev-parse','HEAD'); self.tree=git('rev-parse','HEAD^{tree}')
        self.target={'repository':'fixture/project','task':'fixture-task','unit':'fixture-unit','base':self.head,
                     'snapshot':{'kind':'commit','head':self.head,'tree':self.tree,'files':[],'patch_sha256':ve.sha(b'')}}
        sources={'.github/workflows/validate.yml':(self.root/'.github/workflows/validate.yml').read_text()}
        observation=ve.inventory.observe_workflows(sources)
        self.inventory={'inventory_version':'1','repository':'fixture/project','source_digests':observation['source_digests'],
                        'jobs':observation['jobs'],'lock_digests':{'deps.lock':lock['dependency_lock']['sha256']}}
        self.observed=ve.observe_repository(self.root,self.target,self.inventory)
        self.target=self.observed['target']
        spec=importlib.util.spec_from_file_location('fixture_actual_runner',REPO/'scripts/validate_all.py')
        runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
        with patch.object(runner,'REPO_ROOT',self.root):
            suite=runner.run_suite(self.test)
        # Local synthetic outer receipt around actual subprocess result, never acquisition authority.
        fields={'path','result','cases','command','cwd','started_at','finished_at','exit_code','status','executed','failed','skipped','unknown','count_method','duration_seconds'}
        self.value={'schema_version':1,'suite_evidence_version':1,'result':'pass','compiled':True,
                    'suites':[{k:v for k,v in suite.items() if k in fields}],
                    'contracts':[{'command':[sys.executable,p,*args],'result':'pass'} for p,args in [
                        ('organization/runtime/workflows/scripts/workflow_contract_inventory.py',['--check']),
                        ('organization/integrations/github/scripts/main.py',['validate-contract'])]]}
        probe={'executable':sys.executable,'prefix':str(Path(sys.executable).parent.parent),'version':'3.11.16','machine':'arm64'}
        self.receipt={'schema_version':1,'attempt':'synthetic-local-fixture','start':'2020-01-01T00:00:00Z','end':'2030-01-01T00:00:00Z',
                     'status':'success','exit':0,'bootstrap':{'executable':sys.executable,'version':'3.11.16'},
                     'host':{'system':'Darwin','machine':'arm64','release':'fixture','ci_image':dict.fromkeys(['ImageOS','ImageVersion','RUNNER_OS','RUNNER_ARCH'])},
                     'stages':[{'name':'full','command':[sys.executable,'-B','scripts/validate_all.py'],'start':'2020-01-01T00:00:00Z',
                                'end':'2030-01-01T00:00:00Z','status':'success','exit':0,'log_sha256':'b'*64}],
                     'authorizes_execution':False,'other_platforms':'not_run','codeql':'not_run','policy':'not_adopted',
                     'target_before':self.observed['producer_target'],'target_after':self.observed['producer_target'],
                     'lock_digest':self.observed['toolchain_lock_sha256'],'selected_platform':'Darwin-arm64','artifact':self.runtime,
                     'dependency_lock_digest':lock['dependency_lock']['sha256'],'interpreter':probe,'consumer_interpreter':probe}
        self.profile=json.loads((ROOT/'profiles/delivery/cli.json').read_text())
        self.profile.update(repository='fixture/project',runtimes=[{'name':'python','version':'3.11.16','sha256':'a'*64}])
        self.profile['dependencies']['lockfiles']=[{'path':'deps.lock','sha256':lock['dependency_lock']['sha256']}]
        self.command={'workflow':'.github/workflows/validate.yml','workflow_body':'python3 tests/test_case.py','job':'test','matrix':[],
                      'ordinal':0,'layer':'unit','placement':'local','invocation':{'kind':'argv','argv':suite['command'],'body':None},
                      'cwd':str(self.root),'interpreter':sys.executable,'runtime_sha256':'a'*64,'dependency_lock_sha256':lock['dependency_lock']['sha256'],
                      'runner':{'os':'Darwin','arch':'arm64','image':None},'services_sha256':ve.digest({}),
                      'context':{'event':'push','ref':'refs/heads/test','base_ref':'refs/heads/main','fork':False,'action':None},
                      'source':'suite','result_name':'tests/test_case.py'}
        self.change={'target':self.target,'profile_sha256':ve.digest(self.profile),'inventory_sha256':ve.digest(self.inventory),'behavior':'behavioral',
                     'layers':[{'name':n,'applicability':'required','reason':'fixture required','policy_ref':None} for n in ve.LAYERS],
                     'commands':[self.command],'tdd':{'red':None,'green':None,'refactor':'none','refactor_result':None,'journal':None},
                     'devices':[],'build_sha256':None,'project_start':{'repository':'fixture/project','profile_sha256':ve.digest(self.profile),
                         'ci_owner':'ci-team','cd_owner':'cd-team','rollback_owner':'ops-team','commands':[self.command['invocation']],
                         'targets':['test-package'],'rollback_prerequisites':['retain previous artifact'],'owner_evidence':None},
                     'adoption':None,'commit_bridge':None}

    def bundle(self):
        raw=json.dumps(self.value).encode()
        receipt=copy.deepcopy(self.receipt)
        receipt['validation_result']={'path':'validation.json','sha256':ve.sha(raw),'bytes':len(raw)}
        return ve.ExecutionBundle(json.dumps(receipt).encode(),raw)

    def assess(self, **kwargs):
        args=dict(expected_target=self.target,delivery_profile=self.profile,inventory_observation=self.observed,
                  execution_bundle=self.bundle(),change_contract=self.change,phase='change_unit')
        args.update(kwargs); return ve.assess_validation_evidence(**args)

    def codes(self, result):
        return {r['code'] for r in result['blocking_reasons']}

    def test_actual_child_metadata_and_consistent_bytes_never_authorize(self):
        report=self.assess()
        self.assertEqual(report['integrity'],'consistent',report)
        self.assertEqual(report['cells'][0]['state'],'consistent',report)
        self.assertFalse(report['authorizes_execution'])
        self.assertEqual(report['readiness'],'blocked')
        self.assertIn('execution_provenance_unknown',report['pending_dependencies'])
        self.assertIn('original_red_missing_or_invalid',self.codes(report))

    def test_strict_count_exit_and_terminal_matrix(self):
        original=copy.deepcopy(self.value)
        for key,values in {'executed':[True,0,-1,None],'cases':[True,0],'skipped':[1],'unknown':[1],'failed':[1],
                           'exit_code':[True,1],'status':['skipped','queued','cancelled','unknown'],'count_method':['inferred']}.items():
            for value in values:
                with self.subTest(key=key,value=value):
                    self.value=copy.deepcopy(original);self.value['suites'][0][key]=value
                    self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')
        self.value=original
        for status in ['pending','queued','skipped','cancelled','timed_out','failed','stale','unknown']:
            self.receipt['status']=status
            self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')

    def test_exact_result_bytes_and_missing_binding(self):
        bundle=self.bundle()
        self.assertEqual(ve.inspect_native_bundle(ve.ExecutionBundle(bundle.receipt_bytes,bundle.validation_bytes+b' '))['integrity'],'invalid')
        receipt=json.loads(bundle.receipt_bytes);del receipt['validation_result']
        result=ve.inspect_native_bundle(ve.ExecutionBundle(json.dumps(receipt).encode(),bundle.validation_bytes))
        self.assertEqual(result['reasons'][0]['code'],'sanitized_result_binding_missing_or_mismatch')

    def test_nested_extra_fields_duplicate_suites_and_focused_not_full(self):
        self.receipt['host']['authenticated']=True
        self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')
        del self.receipt['host']['authenticated']
        self.value['suites'].append(copy.deepcopy(self.value['suites'][0]))
        self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')
        self.value['suites'].pop(); self.receipt['stages'][0]['name']='focused-toolchain'
        self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')

    def test_current_git_source_mode_untracked_lock_and_workflow_invalidation(self):
        for operation in ('source','mode','untracked','lock','workflow','delete','symlink'):
            with self.subTest(operation=operation):
                path=self.test; old=path.read_bytes()
                if operation=='source':path.write_bytes(old+b'# edit\n')
                elif operation=='mode':path.chmod(0o755)
                elif operation=='untracked':(self.root/'extra.py').write_text('new')
                elif operation=='lock':(self.root/'deps.lock').write_text('changed')
                elif operation=='workflow':(self.root/'.github/workflows/validate.yml').write_text('{}')
                elif operation=='delete':path.unlink()
                else:path.unlink();path.symlink_to('other')
                observed=ve.observe_repository(self.root,self.target,self.inventory)
                self.assertIn('current_source_identity_mismatch',self.codes(self.assess(inventory_observation=observed)))
                if path.is_symlink():path.unlink()
                self.git('restore','--worktree','.'); self.test.chmod(0o644)
                if (self.root/'extra.py').exists():(self.root/'extra.py').unlink()

    def test_wrong_repository_base_profile_attempt_and_interpreter(self):
        for field in ('repository','base','unit'):
            target=copy.deepcopy(self.target); target[field]='b'*40 if field=='base' else 'other/value'
            self.assertTrue(self.assess(expected_target=target)['blocking_reasons'])
        profile=copy.deepcopy(self.profile);profile['runtimes'][0]['sha256']='b'*64
        self.assertIn('profile_digest_mismatch',self.codes(self.assess(delivery_profile=profile)))
        self.receipt['consumer_interpreter']['executable']='/unexpected/python'
        self.assertEqual(self.assess()['integrity'],'invalid')

    def test_cell_inventory_context_runner_services_and_remote_are_not_equivalent(self):
        for key,value in [('ordinal',1),('matrix',[{'name':'os','value':'linux'}]),('workflow_body','echo substituted')]:
            change=copy.deepcopy(self.change);change['commands'][0][key]=value
            self.assertTrue(self.assess(change_contract=change)['blocking_reasons'])
        change=copy.deepcopy(self.change);change['commands'].append(copy.deepcopy(self.command))
        self.assertIn('duplicate_command_cell',self.codes(self.assess(change_contract=change)))
        self.command['runner']['os']='Linux'
        self.assertEqual(self.assess()['cells'][0]['state'],'local_unavailable')
        self.command['placement']='remote'
        self.assertEqual(self.assess(phase='final_pre_pr')['cells'][0]['state'],'remote_pending')
        self.assertEqual(self.assess(phase='change_unit')['cells'][0]['state'],'missing')

    def test_docs_and_mobile_self_exemptions_remain_blocked(self):
        self.change['behavior']='docs_only';self.change['layers'][0]['applicability']='not_applicable'
        self.assertIn('applicability_unverified',self.codes(self.assess()))
        self.profile=json.loads((ROOT/'profiles/delivery/mobile.json').read_text());self.profile['repository']='fixture/project'
        self.change['profile_sha256']=ve.digest(self.profile);self.change['behavior']='behavioral'
        self.assertIn('development_physical_device_missing',self.codes(self.assess()))

    def test_confined_actual_bytes_reader_rejects_escape_swap_symlinks_and_limits(self):
        p=self.root/'value.json';p.write_bytes(b'{}')
        ref={'path':'value.json','sha256':ve.sha(b'{}'),'bytes':2}
        self.assertEqual(ve.read_bound(self.root,ref),b'{}')
        p.write_bytes(b'[]')
        with self.assertRaises(ve.EvidenceError):ve.read_bound(self.root,ref)
        for name in ('../outside','/etc/passwd','tests/../value.json','tests'):
            with self.assertRaises(ve.EvidenceError):ve.read_bytes(self.root,name)
        link=self.root/'link';link.symlink_to(self.root/'tests',target_is_directory=True)
        with self.assertRaises(ve.EvidenceError):ve.read_bytes(self.root,'link/test_case.py')
        p.write_bytes(b'x'*(ve.MAX_BYTES+1))
        with self.assertRaises(ve.EvidenceError):ve.read_bytes(self.root,'value.json')

    def test_invalid_time_and_source_swap_do_not_become_success(self):
        for start in ('bad','2035-01-01T00:00:00Z','2026-01-01T00:00:00'):
            self.value['suites'][0]['started_at']=start
            self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')
        self.receipt['target_after']=dict(self.receipt['target_before'],head='f'*40)
        self.assertEqual(ve.inspect_native_bundle(self.bundle())['integrity'],'invalid')


    def test_owner_evidence_must_describe_the_declared_project_start(self):
        raw=b'{"repository":"unrelated/project","owner":"different-team"}'
        ref={'path':'owner.json','sha256':ve.sha(raw),'bytes':len(raw)}
        self.change['project_start']['owner_evidence']=ref
        original=self.bundle()
        bundle=ve.ExecutionBundle(original.receipt_bytes,original.validation_bytes,(('owner.json',raw),))
        report=self.assess(execution_bundle=bundle)
        self.assertIn('project_owner_evidence_mismatch',self.codes(report))

    def test_device_result_must_bind_the_reported_operation_and_build(self):
        self.profile=json.loads((ROOT/'profiles/delivery/mobile.json').read_text());self.profile['repository']='fixture/project'
        self.change['profile_sha256']=ve.digest(self.profile);self.change['build_sha256']='a'*64
        image=b'fixture-image'; result=b'{"operation":"different operation","artifact_sha256":"wrong"}'
        image_ref={'path':'image.bin','sha256':ve.sha(image),'bytes':len(image)}
        result_ref={'path':'device.json','sha256':ve.sha(result),'bytes':len(result)}
        self.change['devices']=[{'surface':'physical','model':'fixture-device','os':'fixture-os','artifact_sha256':'a'*64,
             'operation':'required operation','start':'2026-01-01T00:00:00Z','end':'2026-01-01T00:01:00Z','status':'passed',
             'image':image_ref,'result':result_ref,'provenance':None}]
        original=self.bundle();bundle=ve.ExecutionBundle(original.receipt_bytes,original.validation_bytes,(('image.bin',image),('device.json',result)))
        self.assertIn('physical_device_result_mismatch',self.codes(self.assess(execution_bundle=bundle)))


if __name__ == '__main__':
    unittest.main()
