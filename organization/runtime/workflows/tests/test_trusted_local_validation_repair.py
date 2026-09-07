"""Real subprocess/Git validation-failure repair with immutable original evidence."""
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_trusted_local_executor as fixture
from test_host_publication_adapter import FakeGitHub
import trusted_local_executor as local
import host_publication_adapter as publication


class ValidationRepairTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TrustedLocalTests();self.f.setUp()
        text=self.f.cli.read_text().replace("(root/'app.txt').write_text('after\\n')",
            "(root/'app.txt').write_text('fixed\\n' if 'Repair only the host validation failure' in request['task']['instruction'] else 'after\\n')")
        self.f.cli.write_text(text)
        self.f.auth=dataclasses.replace(self.f.auth,executable_digest=publication.digest(self.f.cli.read_bytes()),
            publication=dataclasses.replace(self.f.auth.publication,required_checks=('ci',)),
            validation_commands=((sys.executable,'-c',"from pathlib import Path; assert Path('app.txt').read_text()=='fixed\\n', 'fix existing output'"),))
        self.directory=self.f.state/'trusted-local'/self.f.auth.publication.execution_id

    def tearDown(self):self.f.tearDown()

    def fail_initial(self):
        with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):
            local.execute(self.f.request,self.f.auth,self.f.state)

    def test_cli_repairs_existing_tree_and_original_authority_publishes(self):
        self.fail_initial()
        originals={p.name:p.read_bytes() for p in self.directory.glob('*.json')}
        authority=self.f.root/'authority.json';authority.write_text(json.dumps(dataclasses.asdict(self.f.auth)));authority.chmod(0o600)
        cli=Path(__file__).resolve().parents[4]/'scripts'/'saihai.py'
        command=[sys.executable,str(cli),'usage','repair-validation','--authorization',str(authority),'--state-root',str(self.f.state),'--repair-instruction','Retain the task result and fix its validation only.']
        done=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        result=json.loads(done.stdout);self.assertEqual(result['status'],'validated')
        self.assertEqual(result['execution_id'],self.f.auth.publication.execution_id+'-repair-1')
        for name,data in originals.items():self.assertEqual((self.directory/name).read_bytes(),data,name)
        repaired=Path(result['report_path']).parent
        self.assertTrue((repaired/'claim.json').exists())
        self.assertIn('Retain the task result',json.loads((repaired/'request.json').read_text())['instruction'])
        context=json.loads((repaired/'repair-input.json').read_text())
        self.assertIn('fix existing output',context['untrusted_validation_diagnostics']['stderr'])
        self.assertEqual(context['previous_execution_id'],self.f.auth.publication.execution_id)
        self.assertFalse(result['report']['publication_allowed'])
        again=local.repair_validation(self.f.auth,self.f.state)
        self.assertEqual(again['execution_id'],result['execution_id'])
        commands=FakeGitHub()
        published=local.advance_publication(self.f.auth,self.f.state,commands=commands)
        self.assertEqual(published['status'],'ci_pending',published)
        self.assertEqual(sum(c[:3]==['gh','pr','create'] for c in commands.calls),1)

    def test_changed_tree_and_authority_block_before_repair(self):
        self.fail_initial()
        with patch.object(local,'_run_process') as run:
            wrong=dataclasses.replace(self.f.auth,model='other-model')
            with self.assertRaisesRegex(local.TrustedLocalError,'repair_authority_mismatch'):
                local.repair_validation(wrong,self.f.state)
            (self.f.repo/'app.txt').write_text('unrelated edit\n')
            with self.assertRaisesRegex(local.TrustedLocalError,'failed_validation_tree_changed'):
                local.repair_validation(self.f.auth,self.f.state)
            run.assert_not_called()
        self.assertFalse((self.directory/'validation-repair.json').exists())

    def test_legacy_digest_only_failure_reobserved_without_overwrite(self):
        self.fail_initial()
        receipt=json.loads((self.directory/'validation.json').read_text())
        for row in receipt['commands']:
            row.pop('diagnostic_path',None);row.pop('diagnostic_digest',None)
        local._save(self.directory/'validation.json',receipt)
        original=(self.directory/'validation.json').read_bytes()
        result=local.repair_validation(self.f.auth,self.f.state)
        self.assertEqual(result['status'],'validated')
        self.assertEqual((self.directory/'validation.json').read_bytes(),original)
        self.assertEqual(json.loads((self.directory/'repair-diagnostic-validation.json').read_text())['status'],'failed')

    def test_same_failure_five_repairs_then_blocks_without_replay(self):
        self.f.auth=dataclasses.replace(self.f.auth,validation_commands=((sys.executable,'-c',"import sys,os,uuid,datetime; print(os.environ['TMPDIR']+'/tmp-'+uuid.uuid4().hex+'/fixture.json at '+datetime.datetime.now().isoformat()+': same cause',file=sys.stderr); raise SystemExit(7)"),))
        self.fail_initial()
        for i in range(5):
            with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):
                local.repair_validation(self.f.auth,self.f.state)
        with patch.object(local,'_run_process') as run:
            stopped=local.repair_validation(self.f.auth,self.f.state)
            self.assertEqual(stopped['status'],'same_validation_retry_limit');run.assert_not_called()
        self.assertEqual(len(list((self.f.state/'trusted-local').glob('*-repair-*/claim.json'))),5)

    def test_new_failure_cause_resets_only_consecutive_counter(self):
        self.f.auth=dataclasses.replace(self.f.auth,validation_commands=((sys.executable,'-c',"import sys; from pathlib import Path; print(Path('app.txt').read_text(),file=sys.stderr); raise SystemExit(7)"),))
        self.fail_initial()
        for _ in range(2):
            with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):
                local.repair_validation(self.f.auth,self.f.state)
        progress=json.loads((self.directory/'validation-repair.json').read_text())
        self.assertEqual(progress['attempt'],2)
        self.assertEqual(progress['same_cause_retries'],1)
        with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):
            local.repair_validation(self.f.auth,self.f.state)
        progress=json.loads((self.directory/'validation-repair.json').read_text())
        self.assertEqual(progress['attempt'],3)
        self.assertEqual(progress['same_cause_retries'],2)

    def test_stdout_failure_change_resets_counter(self):
        self.f.auth=dataclasses.replace(self.f.auth,validation_commands=((sys.executable,'-c',"from pathlib import Path; print(Path('app.txt').read_text()); raise SystemExit(7)"),))
        self.fail_initial()
        for _ in range(2):
            with self.assertRaisesRegex(local.TrustedLocalError,'host_validation_failed'):
                local.repair_validation(self.f.auth,self.f.state)
        progress=json.loads((self.directory/'validation-repair.json').read_text())
        self.assertEqual(progress['same_cause_retries'],1)

    def test_maximum_execution_id_keeps_fresh_valid_repair_identity(self):
        identity='EXE-'+('x'*92)
        self.f.auth=dataclasses.replace(self.f.auth,publication=dataclasses.replace(self.f.auth.publication,execution_id=identity))
        self.f.request['execution_id']=identity
        self.directory=self.f.state/'trusted-local'/identity
        self.fail_initial()
        result=local.repair_validation(self.f.auth,self.f.state)
        self.assertEqual(result['status'],'validated')
        self.assertLessEqual(len(result['execution_id']),96)
        self.assertNotEqual(result['execution_id'],identity)

    def test_maximum_instruction_preserved_and_guidance_transported(self):
        text=self.f.cli.read_text().replace("request['task']['instruction']", "str(request)")
        self.f.cli.write_text(text)
        self.f.auth=dataclasses.replace(self.f.auth,executable_digest=publication.digest(self.f.cli.read_bytes()))
        self.f.request['instruction']='x'*65536
        self.fail_initial()
        result=local.repair_validation(self.f.auth,self.f.state,repair_instruction='Keep original scope.')
        repaired=Path(result['report_path']).parent
        self.assertEqual(json.loads((repaired/'request.json').read_text())['instruction'],self.f.request['instruction'])
        self.assertIn('Keep original scope.',json.loads((repaired/'repair-input.json').read_text())['host_repair_guidance'])
        self.assertEqual(result['status'],'validated')

    def test_failed_diagnostics_are_private_bounded_and_digest_bound(self):
        self.f.auth=dataclasses.replace(self.f.auth,validation_commands=((sys.executable,'-c',"import sys; print('x'*20000,file=sys.stderr); raise SystemExit(1)"),))
        self.fail_initial()
        receipt=json.loads((self.directory/'validation.json').read_text());row=receipt['commands'][0]
        path=Path(row['diagnostic_path']);self.assertEqual(path.stat().st_mode&0o077,0)
        diagnostic=json.loads(path.read_text());self.assertLessEqual(len(diagnostic['stderr'].encode()),16384)
        self.assertEqual(publication.digest(path.read_bytes()),row['diagnostic_digest'])
        local._save(path,dict(diagnostic,stderr='tampered'))
        with self.assertRaisesRegex(local.TrustedLocalError,'validation_diagnostic_identity_mismatch'):
            local.repair_validation(self.f.auth,self.f.state)

if __name__=='__main__':unittest.main()
