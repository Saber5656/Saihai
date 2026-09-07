"""Real filesystem tests for bounded host Vault writes."""
import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import vault_task_records as vault


class VaultRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.task = 'TSK-20260907-vault-test'
        self.brief = dict(objective='Deliver a task', scope='This task only', acceptance_criteria='Validate and integrate')
        self.binding = vault.scaffold(self.root, self.task, project='Test', brief=self.brief)
        self.path = Path(self.binding['path'])

    def test_scaffold_and_discovery_reuse_canonical_identity(self):
        self.assertEqual(vault.resolve_task(self.root, self.task)['path'], str(self.path))
        with self.assertRaisesRegex(vault.VaultTaskError, 'exists'):
            vault.scaffold(self.root, self.task, project='Test', brief=self.brief)
        self.assertIn('Deliver a task', self.path.read_text())

    def test_missing_and_traversal_rejected(self):
        for task in ['../../other', '/tmp/task', 'TSK-20260907-absent']:
            with self.assertRaises(vault.VaultTaskError):
                vault.resolve_task(self.root, task)
        with self.assertRaises(vault.VaultTaskError):
            vault.resolve_task(self.root, self.task, record_path=self.root / '..' / 'task.md')

    def test_symlink_and_hardlink_rejected(self):
        original = self.path.read_bytes()
        target = self.root / 'outside.md'
        target.write_bytes(original)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(vault.VaultTaskError):
            vault.resolve_task(self.root, self.task, record_path=self.path)
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(vault.VaultTaskError):
            vault.resolve_task(self.root, self.task, record_path=self.path)

    def test_single_append_idempotent_and_digest(self):
        before = self.path.read_bytes()
        evidence = {'result': 'complete', 'report_sha256': 'sha256:' + 'a' * 64}
        first = vault.append_completion(self.root, self.binding, run_id='RUN-1', evidence=evidence)
        second = vault.append_completion(self.root, self.binding, run_id='RUN-1', evidence=evidence)
        self.assertFalse(first['replayed'])
        self.assertTrue(second['replayed'])
        self.assertEqual(first['content_digest'], vault.digest(self.path.read_bytes()))
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual(self.path.read_text().count('## Saihai completion'), 1)

    def test_concurrent_appends_do_not_lose_existing_content(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda n: vault.append_completion(
                self.root, self.binding, run_id='RUN-' + str(n), evidence={'result': 'complete'}), range(12)))
        self.assertEqual(len(results), 12)
        self.assertEqual(self.path.read_text().count('## Saihai completion'), 12)

    def test_untrusted_obsidian_payload_is_inert_and_bounded(self):
        value = '[[link]]\n[x](javascript:alert(1)) <% code %> `=dv` ${code} |' + 'x' * 2000
        vault.append_completion(self.root, self.binding, run_id='RUN-1', evidence={'result': value})
        written = self.path.read_text()
        for active in ['[[link]]', '[x](', '<%', '`=dv`', '${code}']:
            self.assertNotIn(active, written)
        self.assertLess(len(written), 2000)

    def test_write_denial_is_not_success(self):
        self.path.chmod(0o444)
        with self.assertRaisesRegex(vault.VaultTaskError, 'not_writable'):
            vault.append_completion(self.root, self.binding, run_id='RUN-1', evidence={'result': 'complete'})

    def test_binding_swap_and_duplicate_identity_rejected(self):
        self.path.write_text(self.path.read_text().replace(self.task, 'TSK-20260907-other'))
        with self.assertRaisesRegex(vault.VaultTaskError, 'identity_mismatch'):
            vault.append_completion(self.root, self.binding, run_id='RUN-1', evidence={})

    def test_cli_scaffold_uses_typed_brief_and_preserves_existing_record(self):
        import json
        import subprocess
        repo=Path(__file__).resolve().parents[4]
        wrapper='import sys,runpy; from pathlib import Path; sys.path.insert(0,sys.argv.pop(1)); import vault_task_records as v; root=Path(sys.argv.pop(1)); v.canonical_root=lambda:root; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
        command=[sys.executable,'-c',wrapper,str(repo/'organization/runtime/workflows/scripts'),str(self.root),
                 str(repo/'scripts/saihai.py'),'task','scaffold','--task-id','TSK-20260907-cli',
                 '--project','Test','--brief',json.dumps(self.brief)]
        created=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(created.returncode,0,created.stdout+created.stderr)
        result=json.loads(created.stdout)
        record=Path(result['task']['path']); before=record.read_bytes()
        repeated=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(repeated.returncode,2,repeated.stdout+repeated.stderr)
        self.assertIn('vault_task_record_exists',repeated.stdout)
        self.assertEqual(record.read_bytes(),before)

    def test_fifo_is_rejected_without_blocking(self):
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaisesRegex(vault.VaultTaskError, 'unsafe'):
            vault.resolve_task(self.root,self.task,record_path=self.path)
        with self.assertRaisesRegex(vault.VaultTaskError, 'invalid'):
            vault.checked_attachments([{'path':str(self.path),'digest':'sha256:'+'a'*64}])

    def test_duplicate_identity_in_another_project_is_not_created(self):
        with self.assertRaisesRegex(vault.VaultTaskError,'exists'):
            vault.scaffold(self.root,self.task,project='Other',brief=self.brief)
        self.assertFalse((self.root/'01-Projects'/'Other').exists())

    def test_attachment_reference_is_verified_without_copying_contents(self):
        from unittest.mock import patch
        attachment = self.root / 'private-evidence.json'
        attachment.write_text('unique-private-body-not-for-vault')
        refs = [{'path': str(attachment), 'digest': vault.digest(attachment.read_bytes())}]
        with patch.object(vault, 'canonical_root', return_value=self.root):
            result = vault.persist_completion(self.binding, run_id='RUN-refs',
                evidence={'result': 'complete'}, attachments=refs)
            self.assertEqual(result['status'], 'persisted')
            before = self.path.read_bytes()
            attachment.write_text('changed')
            with self.assertRaisesRegex(vault.VaultTaskError, 'digest_mismatch'):
                vault.persist_completion(self.binding, run_id='RUN-bad', evidence={}, attachments=refs)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('unique-private-body-not-for-vault', self.path.read_text())
        self.assertIn(refs[0]['digest'], self.path.read_text())

    def test_attachment_missing_and_excess_count_fail_closed(self):
        with self.assertRaisesRegex(vault.VaultTaskError, 'unavailable'):
            vault.checked_attachments([{'path':str(self.root/'missing'), 'digest':'sha256:'+'a'*64}])
        with self.assertRaisesRegex(vault.VaultTaskError, 'limit'):
            vault.checked_attachments([{}] * 9)


if __name__ == '__main__':
    unittest.main()
