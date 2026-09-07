"""Real temporary catalog/checkout/Vault diagnostics; no host commissioning."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import startup_recovery as startup
import vault_task_records as vault


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.repo=self.root/'repo';self.repo.mkdir()
        self.vault=self.root/'vault';self.vault.mkdir()
        self.task='TSK-20260907-startup'
        self.brief=dict(objective='diagnostic',scope='temporary fixture',acceptance_criteria='expected typed result')
        vault.scaffold(self.vault,self.task,project='Test',brief=self.brief)
        self.role=self.repo/'organization/roles/tech-backend/skill.md';self.role.parent.mkdir(parents=True);self.role.write_text('trusted role\n')
        for args in [('init','-b','codex/test'),('config','user.name','Fixture'),('config','user.email','fixture@example.invalid'),('remote','add','origin','https://github.com/example/repo.git'),('add','.'),('commit','-m','fixture')]:
            subprocess.run(['git',*args],cwd=self.repo,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.repo).decode().strip()
        keys=[k for k,v in startup.directory_paths.SCHEMA.items() if v.required]
        self.catalog=self.repo/'directory-path.env'
        self.catalog.write_text('\n'.join(k+'='+json.dumps(str(self.vault if k=='AGENTS_VAULT_ROOT' else self.repo if k=='SAIHAI_ROOT' else self.root)) for k in keys)+'\n')

    def inspect(self,**kwargs):
        values=dict(checkout=self.repo,expected_commit=self.head,expected_origin='https://github.com/example/repo.git',roles=['tech-backend'],execution_profile='trusted_local_v1',surface='codex',task_id=self.task,audit_directory=self.root/'audit',_primary=self.repo)
        values.update(kwargs);return startup.inspect_startup(**values)

    def test_trusted_local_checks_do_not_claim_formal_launch(self):
        with patch.dict(os.environ,{'AGENTS_VAULT_ROOT':'/attacker-override'}):result=self.inspect()
        self.assertEqual(result['decision'],'ok',result)
        self.assertFalse(result['formal_harness_assurance'])
        self.assertEqual(result['diagnostics']['vault']['path'],str(self.vault))
        self.assertTrue(Path(result['result_audit_path']).exists())

    def test_missing_catalog_is_diagnostic_and_does_not_create_vault(self):
        self.catalog.unlink();result=self.inspect()
        self.assertEqual(result['decision'],'blocked');self.assertFalse(result['ordinary_work_allowed'])
        self.assertIn('directory_catalog_invalid',result['reasons']);self.assertTrue(result['human_confirmation_required'])

    def test_role_digest_and_commit_mismatch_block(self):
        self.role.write_text('changed role\n')
        self.assertIn('checkout_or_role_identity_invalid',self.inspect()['reasons'])
        self.assertIn('checkout_or_role_identity_invalid',self.inspect(expected_commit='0'*40)['reasons'])

    def test_missing_vault_separates_host_access_from_launch(self):
        original=os.access
        with patch.object(startup.os,'access',side_effect=lambda path,mode: False if Path(path)==self.vault else original(path,mode)):result=self.inspect()
        self.assertIn('agents_vault_not_read_write',result['reasons'])
        self.assertEqual(result['diagnostics']['vault']['sandbox_permission'],'not_proven_by_catalog')
        self.assertFalse(result['diagnostics']['vault']['substitute_created'])

    def test_managed_requires_existing_verified_standard_launch(self):
        rejected=self.inspect(execution_profile='legacy_managed')
        self.assertIn('managed_launch_uncommissioned_or_invalid',rejected['reasons'])
        accepted=self.inspect(execution_profile='legacy_managed',profile_id='fixture',principal_id='fixture',workspace_id='example/repo',
            _launch_verifier=lambda *a:{'status':'verified','session_id':'synthetic-existing-verifier-result'})
        self.assertTrue(accepted['formal_harness_assurance'])
        self.assertEqual(accepted['decision'],'ok')

    def test_explicit_bootstrap_registration_retains_prior_audit(self):
        new='TSK-20260907-bootstrap-new';missing=self.inspect(task_id=new)
        self.assertIn('startup_task_registration_required',missing['reasons'])
        created=self.inspect(task_id=new,bootstrap_brief=self.brief)
        self.assertEqual(created['bootstrap_registration']['status'],'registered')
        text=Path(created['task_binding']['path']).read_text()
        self.assertIn(created['audit_digest'],text)
        before=text
        duplicate=self.inspect(task_id=new,bootstrap_brief=self.brief)
        self.assertEqual(duplicate['decision'],'blocked')
        self.assertEqual(Path(created['task_binding']['path']).read_text(),before)

if __name__=='__main__':unittest.main()
