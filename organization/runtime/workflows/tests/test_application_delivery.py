"""Offline host processes: immutable promotion and bounded delivery recovery."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import application_delivery as app
import delivery_contract
import host_validation as validation


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.repo = self.directory/'repo'; self.repo.mkdir()
        self.state = self.directory/'state'
        self.git('init','-q');self.git('config','user.email','fixture@example.invalid');self.git('config','user.name','Fixture')
        (self.repo/'app.txt').write_text('source')
        self.git('add','app.txt');self.git('commit','-qm','fixture')
        self.profile = json.loads((Path(__file__).resolve().parents[1]/'profiles/delivery/docs.json').read_text())
        self.profile['repository']='fixture/app'
        self.profile['release']['owner']='fixture/release'
        self.profile['dependencies']={'mode':'none','reason':'No external dependencies.','lockfiles':[]}
        self.policy=app.ReleasePolicy(validation.source_digest(self.repo),('staging','production'),'package',True,
                                      'Signature adapter required.',(),False,'No persistent data change.','fixture/recovery')
        self.registry={};self.calls=[]
        self.observer=self.directory/'observer.py'
        self.observer.write_text('''import hashlib,json,os,pathlib,sys,sqlite3,tempfile
name=sys.argv[1]; binding=json.loads(os.environ['SAIHAI_DELIVERY_BINDING']);artifact=pathlib.Path(os.environ['SAIHAI_ARTIFACT'])
flag=pathlib.Path(__file__).with_name('flags.json');flags=json.loads(flag.read_text()) if flag.exists() else {}
if flags.get(name)=='timeout': import time;time.sleep(3)
if name=='build': artifact.write_bytes(b'build-once');artifact.chmod(0o600)
else:
 assert artifact.read_bytes()==b'build-once'
 assert 'sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest()==binding['artifact_digest']
if name in ('deployment_identity','health','smoke'):
 deployed=pathlib.Path(__file__).with_name('deployed-'+binding['environment'])
 assert deployed.read_bytes()==artifact.read_bytes()
facts={}
if name=='recovery_compatibility':
 deployed=pathlib.Path(__file__).with_name('deployed-'+binding['environment'])
 assert 'sha256:'+hashlib.sha256(deployed.read_bytes()).hexdigest()==binding['current_artifact_digest']
 assert binding['artifact_digest']==binding['recovery_artifact_digest']
 facts={'compatible':True,'non_lossy':True,'destructive':False}
if flags.get('mutate_source') and name=='migration_applicability':pathlib.Path('app.txt').write_text('changed during measurement')
if name=='migration_applicability':facts={'applicable':bool(flags.get('migration')), 'paths':['app.txt'] if flags.get('migration') else [], 'reason':'Observed changed files.'}
if name=='migration_compatibility':facts={'old_app':True,'new_app':True,'destructive':False,'sequence':'expand_contract','recovery':'tested_non_lossy'}
if name in ('migration_dry_run','migration_restore_test'):
 with tempfile.TemporaryDirectory() as raw:
  db=sqlite3.connect(raw+'/db');db.execute('create table sample(value text)');db.execute("insert into sample values ('retained')");db.commit()
  backup=sqlite3.connect(raw+'/backup');db.backup(backup)
  db.execute('alter table sample add column optional text');db.commit()
  assert db.execute('select value from sample').fetchone()[0]=='retained'
  if name=='migration_restore_test':
   backup.backup(db);assert len(db.execute('pragma table_info(sample)').fetchall())==1
  facts={'representative':True} if name=='migration_dry_run' else {'restored':True,'backup_digest':'sha256:'+hashlib.sha256(pathlib.Path(raw+'/backup').read_bytes()).hexdigest()}
if isinstance(flags.get(name),dict):facts.update(flags[name])
if flags.get(name)=='bad_binding':binding=dict(binding,artifact_digest='sha256:'+'0'*64)
print(json.dumps({'result':'fail' if flags.get(name)=='fail' else 'pass','cases':0 if flags.get(name)=='zero' else 1,'binding':binding,'facts':facts}))
''')
        names=('build','artifact_validation','sbom','provenance','signature','migration_applicability',
               'migration_compatibility','migration_dry_run','migration_restore_test','recovery_compatibility','deployment_identity','smoke','health')
        self.commands={name:(sys.executable,str(self.observer),name) for name in names}
        self.host=self.make_host()
        self.receipt=self.directory/'validation.json'
        argv=[sys.executable,'-c','assert 2+2==4']
        start=time.time();done=subprocess.run(argv,capture_output=True);end=time.time()
        row=dict(argv=argv,exit=done.returncode,started_at_epoch=start,ended_at_epoch=end,
                 stdout_digest=validation.digest(done.stdout),stderr_digest=validation.digest(done.stderr),
                 command_digest=validation.command_digest(argv),**validation.observe(argv,done.stdout,done.stderr,done.returncode))
        self.receipt.write_text(json.dumps({'validation_version':2,'status':'passed','commands':[row],
                                           'plan_digest':validation.digest([argv]),'source_digest':self.policy.source_digest,
                                           'delivery_profile':{'state':'host_commands'}}));self.receipt.chmod(0o600)

    def git(self,*args):return subprocess.check_output(['git',*args],cwd=self.repo)
    def make_host(self):return delivery_contract.application_delivery_host(self.profile,root=self.repo,state=self.state,policy=self.policy,commands=self.commands,grants=self.registry.get)
    def flags(self,**flags):(self.directory/'flags.json').write_text(json.dumps(flags))
    def build(self):return self.host.build_once(validation_receipt=self.receipt,validation_identity={})
    def grant(self,candidate,action='deploy',environment='staging',**changes):
        grant=app.ReleaseGrant(action,self.profile['repository'],environment,'package',candidate['artifact_digest'],(action,),
                               'fixture/recovery' if action=='recover' else 'fixture/release',time.time()-1,time.time()+300,
                               False,True,'short_lived_workload',(action,),'existing-host-grant')
        self.registry[action]=dataclasses.replace(grant,**changes)
        return action
    def perform(self,artifact,binding,grant):
        self.calls.append((artifact.read_bytes(),binding,grant.grant_id))
        (self.directory/('deployed-'+binding['environment'])).write_bytes(artifact.read_bytes())
    def promote(self,candidate,**kwargs):
        args=dict(environment='staging',grant_id='deploy',operation_id='one',perform=self.perform);args.update(kwargs)
        return self.host.promote(candidate,**args)

    def test_actual_build_once_same_digest_across_environments(self):
        candidate=self.build();self.flags(build='fail')
        self.assertEqual(candidate,self.build())
        self.grant(candidate);first=self.promote(candidate)
        self.assertEqual(first['state'],'verified')
        self.grant(candidate,environment='production')
        second=self.promote(candidate,environment='production',operation_id='two')
        self.assertEqual(second['state'],'verified')
        self.assertEqual(first['binding']['artifact_digest'],second['binding']['artifact_digest'])
        self.assertEqual(len(self.calls),2)
        metrics=self.host.metrics()
        self.assertEqual(metrics['metrics']['change_failure_rate']['value'],0)
        self.assertIsNotNone(metrics['metrics']['ci_duration']['value'])
        self.assertIsNone(metrics['metrics']['flaky_test_rate']['value'])
        self.assertTrue(all(e['evidence'].startswith('sha256:') for e in metrics['events']))

    def test_substituted_bytes_or_record_block(self):
        candidate=self.build();self.grant(candidate)
        with self.assertRaises(ValueError):self.promote(dict(candidate,artifact_digest='sha256:'+'a'*64))
        path=self.state/candidate['artifact'];path.chmod(0o600);path.write_bytes(b'rebuilt')
        with self.assertRaises(ValueError):self.promote(candidate)
        self.assertEqual(self.calls,[])

    def test_missing_revoked_expired_wrong_environment_and_excess_privilege_grants(self):
        candidate=self.build()
        with self.assertRaisesRegex(app.DeliveryBlocked,'grant_required'):self.promote(candidate)
        for changes in ({'revoked':True},{'expires_at':time.time()-1},{'environment':'elsewhere'},
                        {'protected_environment':False},{'privileges':('deploy','admin')},{'artifact_digest':'other'}):
            self.grant(candidate,**changes)
            with self.assertRaisesRegex(app.DeliveryBlocked,'grant_required'):self.promote(candidate)
        self.assertFalse(self.calls)

    def test_merge_and_deploy_grants_do_not_authorize_release(self):
        candidate=self.build();self.grant(candidate)
        with self.assertRaisesRegex(app.DeliveryBlocked,'verified_deployment'):self.promote(candidate,action='release')
        deployment=self.promote(candidate)
        with self.assertRaisesRegex(app.DeliveryBlocked,'grant_required'):
            self.promote(candidate,action='release',deployment=deployment,operation_id='release')
        self.grant(candidate,action='release')
        result=self.promote(candidate,action='release',deployment=deployment,operation_id='release',grant_id='release')
        self.assertEqual(result['state'],'verified')

    def test_failed_zero_and_wrong_identity_observations_block_candidate(self):
        for condition in ('fail','zero','bad_binding'):
            with self.subTest(condition=condition):
                self.state=self.directory/condition;self.host=self.make_host();self.flags(provenance=condition)
                with self.assertRaises(app.DeliveryBlocked):self.build()
                with self.assertRaisesRegex(app.DeliveryBlocked,'build_uncertain'):self.build()

    def test_invalid_signature_and_stale_source_block(self):
        self.flags(signature='fail')
        with self.assertRaises(app.DeliveryBlocked):self.build()
        (self.repo/'app.txt').write_text('changed')
        with self.assertRaisesRegex(app.DeliveryBlocked,'source_changed'):self.build()

    def test_target_lock_and_unresolved_deployment_block_races(self):
        candidate=self.build();self.grant(candidate)
        target=['target','fixture/app','staging','package']
        with self.host._lock(target):
            with self.assertRaisesRegex(app.DeliveryBlocked,'lease_busy'):self.promote(candidate)
        self.flags(health='fail');failed=self.promote(candidate)
        self.assertEqual(failed['state'],'failed_or_unknown')
        with self.assertRaisesRegex(app.DeliveryBlocked,'unresolved'):self.promote(candidate,operation_id='two')
        self.assertEqual(self.promote(candidate),failed);self.assertEqual(len(self.calls),1)

    def test_unknown_crash_intent_does_not_repeat_action(self):
        candidate=self.build();self.grant(candidate)
        def crash(*args):
            self.perform(*args)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.promote(candidate,perform=crash)
        self.host=self.make_host()
        result=self.promote(candidate)
        self.assertEqual(result['state'],'uncertain');self.assertEqual(len(self.calls),1)
        reconciled=self.host.reconcile(result,candidate,grant_id='deploy')
        self.assertEqual(reconciled['state'],'verified');self.assertEqual(len(self.calls),1)

    def test_actual_staged_migration_and_independent_restore(self):
        self.policy=dataclasses.replace(self.policy,migration_required=True,migration_paths=('app.txt',))
        self.host=self.make_host();self.flags(migration=True)
        candidate=self.build();self.grant(candidate)
        row=self.promote(candidate)
        self.assertEqual(row['state'],'verified')
        self.assertEqual(len(row['migration']),4)
        self.assertTrue(row['migration'][-1]['facts']['restored'])

    def test_destructive_incompatible_unknown_restore_migrations_block(self):
        self.policy=dataclasses.replace(self.policy,migration_required=True,migration_paths=('app.txt',))
        self.host=self.make_host();self.flags(migration=True);candidate=self.build();self.grant(candidate)
        for check,facts in [('migration_compatibility',{'destructive':True}),('migration_compatibility',{'old_app':False}),
                            ('migration_compatibility',{'recovery':'unknown'}),('migration_restore_test',{'restored':False})]:
            self.flags(migration=True,**{check:facts})
            with self.assertRaises(app.DeliveryBlocked):self.promote(candidate)
        self.assertFalse(self.calls)

    def test_non_data_change_records_applicability_and_needs_no_migration_action(self):
        candidate=self.build();self.grant(candidate);row=self.promote(candidate)
        self.assertEqual(len(row['migration']),1);self.assertFalse(row['migration'][0]['facts']['applicable'])

    def test_single_owner_bounded_recovery_and_duplicate_corrective_identity(self):
        candidate=self.build();self.grant(candidate);self.flags(health='fail');failed=self.promote(candidate)
        self.grant(candidate,action='recover',owner='wrong')
        with self.assertRaises(app.DeliveryBlocked):self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        self.grant(candidate,action='recover');self.flags()
        recovered=self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        self.assertEqual(recovered['state'],'recovered_verified')
        self.host=self.make_host()
        self.assertEqual(recovered,self.host.recover(failed,candidate,grant_id='recover',perform=self.perform))
        self.assertEqual(len(self.calls),2)

    def test_failed_recovery_is_terminal_and_does_not_loop(self):
        candidate=self.build();self.grant(candidate);self.flags(smoke='bad_binding');failed=self.promote(candidate)
        self.grant(candidate,action='recover');row=self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        self.assertEqual(row['state'],'recovery_failed_or_unknown')
        self.flags();self.assertEqual(row,self.host.recover(failed,candidate,grant_id='recover',perform=self.perform))
        self.assertEqual(len(self.calls),2)

    def test_timeout_is_non_success(self):
        self.policy=dataclasses.replace(self.policy,timeout_seconds=1);self.host=self.make_host()
        candidate=self.build();self.grant(candidate);self.flags(health='timeout')
        self.assertEqual(self.promote(candidate)['state'],'failed_or_unknown')


    def test_repository_policy_loader_pins_actual_bytes(self):
        value=dataclasses.asdict(self.policy);value.pop('source_digest')
        raw=json.dumps(value).encode();path=self.repo/'application-delivery.json';path.write_bytes(raw)
        self.git('add','application-delivery.json');self.git('commit','-qm','release policy')
        reference={'path':path.name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        policy=app.load_release_policy(self.repo,reference)
        self.assertEqual(policy.source_digest,validation.source_digest(self.repo))
        self.assertEqual(policy.environments,('staging','production'))
        path.write_text('{}')
        with self.assertRaises(ValueError):app.load_release_policy(self.repo,reference)

    def test_revocation_during_action_cannot_report_success(self):
        candidate=self.build();self.grant(candidate)
        def revoke(*args):
            self.perform(*args)
            self.registry['deploy']=dataclasses.replace(self.registry['deploy'],revoked=True)
        row=self.promote(candidate,perform=revoke)
        self.assertEqual(row['state'],'failed_or_unknown')
        self.assertEqual(row['verification'],'unknown')

    def test_measured_failure_preserves_evidence_and_failure_metric(self):
        candidate=self.build();self.grant(candidate);self.flags(health='fail')
        row=self.promote(candidate)
        self.assertEqual(row['verification'],'failed')
        self.assertEqual(row['evidence'][-1]['status'],'failed')
        self.assertTrue(row['evidence'][-1]['stdout_digest'].startswith('sha256:'))
        self.assertEqual(self.host.metrics()['metrics']['change_failure_rate']['value'],1)

    def test_recovery_crash_can_be_observed_without_repeating_mutation(self):
        candidate=self.build();self.grant(candidate);self.flags(health='fail');failed=self.promote(candidate)
        self.grant(candidate,action='recover')
        def crash(*args):
            self.perform(*args)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.host.recover(failed,candidate,grant_id='recover',perform=crash)
        row=self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        self.assertEqual(row['state'],'uncertain')
        self.flags();self.assertEqual(self.host.reconcile(row,candidate,grant_id='recover',recovery=True)['state'],'recovered_verified')
        self.assertEqual(len(self.calls),2)

    def test_missing_host_adapter_and_unknown_applicability_block(self):
        candidate=self.build();self.grant(candidate)
        self.host.commands.pop('migration_applicability')
        with self.assertRaisesRegex(app.DeliveryBlocked,'host_observer_missing'):self.promote(candidate)
        self.host=self.make_host();self.flags(migration=True)
        with self.assertRaisesRegex(app.DeliveryBlocked,'applicability_unknown'):self.promote(candidate)

    def test_applicability_exemptions_do_not_invent_signature_or_sbom_checks(self):
        self.policy=dataclasses.replace(self.policy,signature_required=False,signature_reason='Unsigned internal docs bundle.',
                                       sbom_required=False,sbom_reason='No dependencies in documentation bundle.')
        self.host=self.make_host();self.host.commands.pop('signature');self.host.commands.pop('sbom')
        candidate=self.build()
        self.assertNotIn('signature',{r['name'] for r in candidate['evidence']})
        self.assertNotIn('sbom',{r['name'] for r in candidate['evidence']})


    def test_actual_deployed_bytes_must_match_validated_artifact(self):
        candidate=self.build();self.grant(candidate)
        def substitute(artifact,binding,grant):
            self.perform(artifact,binding,grant)
            (self.directory/('deployed-'+binding['environment'])).write_bytes(b'substituted after deployment')
        row=self.promote(candidate,perform=substitute)
        self.assertEqual(row['state'],'failed_or_unknown')
        self.assertNotEqual(row['state'],'verified')

    def test_host_state_inside_worker_and_invalid_validation_receipt_block(self):
        with self.assertRaises(app.DeliveryBlocked):
            app.DeliveryHost(root=self.repo,state=self.repo/'state',profile=self.profile,policy=self.policy,
                             commands=self.commands,grants=self.registry.get)
        receipt=json.loads(self.receipt.read_text());receipt['commands'][0]['passed']=False
        self.receipt.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):self.build()


    def test_sec_rel_1_source_change_during_measurement_prevents_deploy(self):
        candidate=self.build();self.grant(candidate);self.flags(mutate_source=True)
        with self.assertRaisesRegex(app.DeliveryBlocked,'source_changed'):self.promote(candidate)
        self.assertEqual(self.calls,[])

    def test_sec_rel_2_migration_recovery_needs_bound_non_lossy_measurement(self):
        self.policy=dataclasses.replace(self.policy,migration_required=True,migration_paths=('app.txt',))
        self.host=self.make_host();self.flags(migration=True)
        candidate=self.build();self.grant(candidate);self.flags(migration=True,health='fail')
        failed=self.promote(candidate);self.grant(candidate,action='recover')
        self.host.commands.pop('recovery_compatibility')
        with self.assertRaisesRegex(app.DeliveryBlocked,'host_observer_missing'):
            self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        self.assertEqual(len(self.calls),1)
        self.host=self.make_host()
        for facts in ({'compatible':False},{'non_lossy':False},{'destructive':True},'bad_binding'):
            self.flags(migration=True,recovery_compatibility=facts)
            with self.assertRaises(app.DeliveryBlocked):
                self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
            self.assertEqual(len(self.calls),1)
        self.flags(migration=True)
        result=self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        self.assertEqual(result['state'],'recovered_verified');self.assertEqual(len(self.calls),2)
        measurement=result['evidence'][0]
        self.assertEqual(measurement['name'],'recovery_compatibility')
        self.assertEqual(measurement['binding']['current_artifact_digest'],failed['binding']['artifact_digest'])
        self.assertEqual(measurement['binding']['recovery_artifact_digest'],candidate['artifact_digest'])
        self.assertEqual(measurement['binding']['migrations'],failed['migration'][1]['binding']['migrations'])


    def test_reconciliation_preserves_preflight_evidence_after_recovery_crash(self):
        self.policy=dataclasses.replace(self.policy,migration_required=True,migration_paths=('app.txt',))
        self.host=self.make_host();self.flags(migration=True)
        candidate=self.build();self.grant(candidate);self.flags(migration=True,health='fail')
        failed=self.promote(candidate);self.grant(candidate,action='recover');self.flags(migration=True)
        def crash(*args):
            self.perform(*args)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.host.recover(failed,candidate,grant_id='recover',perform=crash)
        pending=self.host.recover(failed,candidate,grant_id='recover',perform=self.perform)
        preflight=pending['evidence'][0]
        terminal=self.host.reconcile(pending,candidate,grant_id='recover',recovery=True)
        self.assertEqual(terminal['state'],'recovered_verified')
        self.assertEqual(terminal['evidence'][0],preflight)
        self.assertEqual(preflight['name'],'recovery_compatibility')
        self.assertEqual(len(self.calls),2)


class MetricsTests(unittest.TestCase):
    def test_traceable_pairs_deduplication_and_missing_data(self):
        events=[{'id':'a','subject':'one','kind':'commit','at':1,'evidence':'host:a'},
                {'id':'b','subject':'one','kind':'deployed','at':6,'evidence':'host:b'}]
        result=app.delivery_metrics(events+[events[0]])
        self.assertEqual(result['lead_time']['value'],5)
        self.assertEqual(result['lead_time']['samples'][0]['events'],['a','b'])
        self.assertIsNone(result['ci_duration']['value']);self.assertIsNone(result['flaky_test_rate']['value'])
        with self.assertRaises(app.DeliveryBlocked):app.delivery_metrics(events+[dict(events[0],at=4)])


if __name__=='__main__':unittest.main()
