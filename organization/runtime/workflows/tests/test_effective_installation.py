"""Real temporary Git and artifact readback; no live host installation."""
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import effective_installation as install
import effective_bundle as bundle
import trusted_local_executor as local
import host_publication_adapter as pub

class InstallationTests(unittest.TestCase):
    def setUp(self):
        # Avoid the shared macOS per-user T directory mutated by unrelated
        # subprocess fixtures; retain real ancestor identity checks unchanged.
        self.tmp=tempfile.TemporaryDirectory(dir='/tmp');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.repo=self.root/'primary';self.repo.mkdir()
        self.remote=self.root/'remote.git';self.state=self.root/'state'
        self.git('init','-b','main');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        for name in bundle.CATEGORIES:(self.repo/name).write_text(name)
        self.git('add','.');self.git('commit','-m','base');self.base=self.git('rev-parse','HEAD')
        subprocess.run(['git','init','--bare',str(self.remote)],capture_output=True,check=True)
        self.git('remote','add','origin',str(self.remote));self.git('push','-u','origin','main')
        self.worker=self.root/'worker';self.git('worktree','add','-b','codex/task',str(self.worker))
        host=pub.HostAuthorization('TSK-20260907-fixture','req','run','exec','example/repo',str(self.worker),'codex/task',self.base,self.base,('.',),('validate',),'sha256:'+'1'*64,'task-evidence')
        self.auth=local.TrustedLocalAuthorization(host,'/unused','unused','/unused','model',(('true',),))
        self.env={'SAIHAI_ROOT':str(self.repo)}
        self.patcher=patch.object(install,'catalog',return_value=self.env);self.patcher.start();self.addCleanup(self.patcher.stop)
        members=[dict(id=n,category=n,source={'root':'repo','path':n},installed={'root':'repo','path':n}) for n in bundle.CATEGORIES]
        self.plan=dict(version=1,surface='codex-app',roots={'repo':'SAIHAI_ROOT'},members=members,policy_snapshots=[],approved_symlinks={},source_identity={'root':'repo','commit':self.base},expected_content_digest='',sync_catalog_key='SAIHAI_ROOT')
        self.plan['expected_content_digest']=bundle.observe_bundle(catalog_roots={'repo':str(self.repo)},member_spec={'members':members,'policy_snapshots':[]},target_surface='codex-app',expected_source_identity=self.plan['source_identity'])['content_digest']
    def git(self,*args):
        return subprocess.check_output(['git',*args],cwd=self.repo,stderr=subprocess.DEVNULL,text=True).strip()
    def merged(self):
        (self.worker/'new').write_text('new');subprocess.run(['git','add','.'],cwd=self.worker,check=True)
        subprocess.run(['git','commit','-m','new'],cwd=self.worker,capture_output=True,check=True)
        sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.worker,text=True).strip()
        subprocess.run(['git','push','origin','HEAD:main'],cwd=self.worker,capture_output=True,check=True)
        return {'status':'complete','merge_commit':sha,'integrated_checks':{'validate':'success'}}
    def test_configure_verify_immutable_and_drift(self):
        install.configure(self.auth,self.state,self.plan)
        self.assertEqual(install.verify(self.auth,self.state)['status'],'installed_bytes_verified')
        self.assertEqual(install.configure(self.auth,self.state,self.plan)['plan'],self.plan)
        (self.repo/'common').write_text('changed')
        with self.assertRaisesRegex(install.InstallationError,'source_changed|digest_changed'):install.verify(self.auth,self.state)
    def test_authority_rebinding_and_legacy(self):
        self.assertEqual(install.verify(self.auth,self.state)['status'],'legacy_not_configured')
        install.configure(self.auth,self.state,self.plan)
        with self.assertRaisesRegex(install.InstallationError,'authority_changed'):
            install.verify(dataclasses.replace(self.auth,model='other'),self.state)
    def test_drift_and_incomplete_rejected(self):
        self.plan['members']=self.plan['members'][:-1]
        with self.assertRaisesRegex(install.InstallationError,'incomplete'):install.configure(self.auth,self.state,self.plan)
    def test_ff_and_duplicate_receipt(self):
        install.configure(self.auth,self.state,self.plan);result=self.merged()
        with patch.object(pub,'_remote_matches',return_value=True):
            first=install.sync_primary(self.auth,self.state,result)
            second=install.sync_primary(self.auth,self.state,result)
        self.assertEqual(first,second);self.assertEqual(first['dependent_base'],result['merge_commit'])
        self.assertEqual(self.git('status','--porcelain'),'')
    def test_dirty_preserves_bytes(self):
        install.configure(self.auth,self.state,self.plan);result=self.merged()
        (self.repo/'common').write_text('user change')
        with patch.object(pub,'_remote_matches',return_value=True):
            with self.assertRaisesRegex(install.InstallationError,'primary_dirty'):install.sync_primary(self.auth,self.state,result)
        self.assertEqual((self.repo/'common').read_text(),'user change');self.assertEqual(self.git('rev-parse','HEAD'),self.base)
    def test_detached_and_diverged(self):
        install.configure(self.auth,self.state,self.plan);result=self.merged();self.git('checkout','--detach')
        with self.assertRaisesRegex(install.InstallationError,'not_main'):install.sync_primary(self.auth,self.state,result)
        self.git('checkout','main');(self.repo/'local').write_text('local');self.git('add','.');self.git('commit','-m','local')
        with patch.object(pub,'_remote_matches',return_value=True):
            with self.assertRaisesRegex(install.InstallationError,'diverged'):install.sync_primary(self.auth,self.state,result)
        self.assertEqual((self.repo/'local').read_text(),'local')
    def test_plan_loss_after_claim_cannot_downgrade(self):
        install.configure(self.auth,self.state,self.plan)
        claim=self.state/'trusted-local'/'exec'/'claim.json'
        install.run_store.atomic_write_json(claim,{'installation_required':True})
        (self.state/'host-installation'/'exec.json').unlink()
        with self.assertRaisesRegex(install.InstallationError,'plan_missing'):install.verify(self.auth,self.state)
    def test_repair_inherits_bound_plan(self):
        install.configure(self.auth,self.state,self.plan)
        child=dataclasses.replace(self.auth,publication=dataclasses.replace(self.auth.publication,execution_id='repair'))
        install.inherit(self.auth,child,self.state)
        self.assertEqual(install.verify(child,self.state)['status'],'installed_bytes_verified')
    def test_catalog_primary_replacement_blocks_sync(self):
        install.configure(self.auth,self.state,self.plan);result=self.merged()
        other=self.root/'other';other.mkdir();self.env['SAIHAI_ROOT']=str(other)
        with self.assertRaisesRegex(install.InstallationError,'catalog_primary_changed'):install.sync_primary(self.auth,self.state,result)
    def test_network_failure_preserves_primary(self):
        install.configure(self.auth,self.state,self.plan);result=self.merged()
        self.git('remote','set-url','origin',str(self.root/'missing.git'))
        with patch.object(pub,'_remote_matches',return_value=True):
            with self.assertRaisesRegex(install.InstallationError,'sync_git_failed'):install.sync_primary(self.auth,self.state,result)
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)
        self.assertEqual(self.git('status','--porcelain'),'')
    def test_replaced_file_symlink_cannot_become_active(self):
        install.configure(self.auth,self.state,self.plan)
        value=(self.repo/'common').read_bytes();outside=self.root/'outside';outside.write_bytes(value)
        (self.repo/'common').unlink();(self.repo/'common').symlink_to(outside)
        with self.assertRaisesRegex(install.InstallationError,'artifact_drift'):install.verify(self.auth,self.state)
    def test_newer_remote_and_pending_checks(self):
        install.configure(self.auth,self.state,self.plan);result=self.merged()
        with patch.object(pub,'_remote_matches',return_value=True):
            with self.assertRaisesRegex(install.InstallationError,'remote_advanced'):install.sync_primary(self.auth,self.state,dict(result,merge_commit=self.base))
        with self.assertRaisesRegex(install.InstallationError,'validation_required'):install.sync_primary(self.auth,self.state,dict(result,integrated_checks={'validate':'pending'}))
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

if __name__=='__main__':unittest.main()
