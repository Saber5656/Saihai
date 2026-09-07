"""Mechanical remaps against real immutable Git objects and a real merge conflict."""
import base64
import copy
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import request_intake as intake
import requirement_scope as scope
import trusted_local_executor as local
import host_publication_adapter as publication
import run_store
import test_trusted_local_executor as fixture
from test_request_intake import ledger, provider


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TrustedLocalTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.root = self.f.repo
        (self.root/'app.txt').write_text('one\ntwo\nthree\nfour\nfive\nsix\n')
        self.f.git('add','app.txt'); self.f.git('commit','-m','multiline fixture')
        self.base = self.f.git('rev-parse','HEAD')
        self.doc = ledger()
        self.doc['task_units'][1]['change_contracts'] = [dict(path='app.txt',base=self.base,
            requirement_ids=['RB'],operations=['edit'],ranges=[[3,3]],insertions=[])]
    def histories(self, upstream='one\nUPSTREAM\nthree\nfour\nfive\nsix\n', task='one\ntwo\nTASK\nfour\nfive\nsix\n'):
        (self.root/'app.txt').write_text(task);self.f.git('add','app.txt');self.f.git('commit','-m','task')
        head = self.f.git('rev-parse','HEAD')
        self.f.git('checkout','-b','upstream',self.base)
        (self.root/'app.txt').write_text(upstream);self.f.git('add','app.txt');self.f.git('commit','-m','upstream')
        fresh=self.f.git('rev-parse','HEAD');self.f.git('checkout','codex/task')
        self.f.git('update-ref','refs/remotes/origin/main',fresh)
        return head, fresh
    def test_disjoint_adjacent_git_conflict_has_exact_deterministic_plan(self):
        head,fresh=self.histories()
        plan=scope.mechanical_refresh(self.root,self.doc,old_base=self.base,new_base=fresh,task_head=head)
        self.assertEqual(base64.b64decode(plan['outputs'][0]['content_base64']),b'one\nUPSTREAM\nTASK\nfour\nfive\nsix\n')
        self.assertEqual(plan['ledger']['requirements'],self.doc['requirements'])
        self.assertEqual(plan['ledger']['incidental_findings'],self.doc['incidental_findings'])
        self.assertEqual(self.f.git('status','--porcelain'),'')
    def test_upstream_line_insertion_remaps_coordinates(self):
        head,fresh=self.histories(upstream='zero\none\ntwo\nthree\nfour\nfive\nsix\n')
        plan=scope.mechanical_refresh(self.root,self.doc,old_base=self.base,new_base=fresh,task_head=head)
        self.assertEqual(plan['ledger']['task_units'][1]['change_contracts'][0]['ranges'],[[4,4]])
        self.assertEqual(base64.b64decode(plan['outputs'][0]['content_base64']),b'zero\none\ntwo\nTASK\nfour\nfive\nsix\n')
    def test_overlap_is_internal_refresh_without_mutation(self):
        head,fresh=self.histories(upstream='one\ntwo\nUPSTREAM\nfour\nfive\nsix\n')
        with self.assertRaisesRegex(scope.ScopeError,'overlaps_contract'):
            scope.mechanical_refresh(self.root,self.doc,old_base=self.base,new_base=fresh,task_head=head)
        self.assertEqual(self.f.git('status','--porcelain'),'')
    def test_actual_unapproved_hunk_cannot_be_laundered_by_refresh(self):
        head,fresh=self.histories(task='TASK\ntwo\nthree\nfour\nfive\nsix\n')
        with self.assertRaisesRegex(scope.ScopeError,'hunk_outside_scope'):
            scope.mechanical_refresh(self.root,self.doc,old_base=self.base,new_base=fresh,task_head=head)
    def test_mode_drift_rejected(self):
        head,fresh=self.histories()
        self.f.git('checkout','upstream');(self.root/'app.txt').chmod(0o755)
        self.f.git('add','app.txt');self.f.git('commit','-m','mode')
        fresh=self.f.git('rev-parse','HEAD');self.f.git('checkout','codex/task')
        with self.assertRaisesRegex(scope.ScopeError,'mode_overlap'):
            scope.mechanical_refresh(self.root,self.doc,old_base=self.base,new_base=fresh,task_head=head)
    def test_refreshed_reference_cannot_change_original_non_goals(self):
        f=self.f
        ref=intake.prepare(state_root=f.state,request_id=f.request['request_id'],task_id=f.request['task_id'],
            user_prompt=f.request['instruction'],ledger=self.doc,provider=provider,intended_model=f.auth.model)
        head,fresh=self.histories()
        current,plan=intake.refresh_hunks(f.state,ref,root=self.root,old_base=self.base,new_base=fresh,task_head=head)
        artifact=intake.resolve(f.state,current)
        artifact['requirement_ledger']['task_units'][1]['scope']['out']=['A different non-goal']
        artifact['brief']['scope']['out']=['A different non-goal']
        artifact['brief']['requirements_digest']=scope.digest(artifact['requirement_ledger'])
        malicious=dict(current,digest=scope.digest(artifact))
        intake._save_immutable(intake._path(f.state,ref['request_id'],malicious['digest'][7:]),artifact)
        intake.ledger_lifecycle.acknowledge(artifact,malicious,persist=True)
        with self.assertRaisesRegex(intake.IntakeError,'semantic_or_contract_drift'):
            intake.verify_refresh_chain(f.state,malicious,ref,root=self.root)

    def test_real_host_merge_refresh_revalidates_and_keeps_original_authority(self):
        f=self.f
        ref=intake.prepare(state_root=f.state, request_id=f.request['request_id'], task_id=f.request['task_id'],
            user_prompt=f.request['instruction'],ledger=self.doc,provider=provider,intended_model=f.auth.model)
        head,fresh=self.histories()
        host=dataclasses.replace(f.auth.publication,head=self.base,base=self.base)
        auth=dataclasses.replace(f.auth,publication=host,intake_digest=ref['digest'],
            validation_commands=((sys.executable,'-c',"from pathlib import Path; assert Path('app.txt').read_text().splitlines()[1:3]==['UPSTREAM','TASK']"),))
        directory=f.state/'trusted-local'/host.execution_id
        run_store.atomic_write_json(directory/'request.json',dict(f.request,work_brief_ref=ref))
        class Commands(publication.Commands):
            def run(self,argv,**kw):
                if argv[:2]==['git','fetch']: return ''
                return super().run(argv,**kw)
        outcome=local._integrate_conflict(auth,auth,directory,f.state,{'head':head},Commands())
        self.assertEqual(outcome['status'],'integration_validated')
        self.assertEqual(f.git('status','--porcelain'),'')
        continuation=run_store.read_json(directory/'continuation.json')
        self.assertNotEqual(continuation['work_brief_ref']['digest'],ref['digest'])
        self.assertEqual(continuation['original_authorization_digest'],publication.digest(local._authorization_material(auth)))
        self.assertEqual(continuation['report']['requirement_scope']['base'],fresh)
        intake.verify_refresh_chain(f.state,continuation['work_brief_ref'],ref,root=self.root)
        self.assertEqual(run_store.read_json(directory/'request.json')['work_brief_ref'],ref)
        self.assertEqual(continuation['authorization']['publication']['allowed_paths'],list(host.allowed_paths))
        self.assertEqual(continuation['report']['validation']['status'],'passed')
        # A second main update must preserve the original authority and use the
        # first continuation's execution identity, not the original request ID.
        first_head=f.git('rev-parse','HEAD')
        f.git('checkout','upstream')
        (self.root/'app.txt').write_text('one\nUPSTREAM\nthree\nfour\nfive\nSIX\n')
        f.git('add','app.txt');f.git('commit','-m','second upstream')
        second_base=f.git('rev-parse','HEAD');f.git('checkout','codex/task')
        f.git('update-ref','refs/remotes/origin/main',second_base)
        second=local._integrate_conflict(auth,local._authority_from_record(continuation['authorization']),
            directory,f.state,{'head':first_head},Commands())
        self.assertEqual(second['status'],'integration_validated')
        final=run_store.read_json(directory/'continuation.json')
        intake.verify_refresh_chain(f.state,final['work_brief_ref'],ref,root=self.root)
        self.assertEqual(final['original_authorization_digest'],publication.digest(local._authorization_material(auth)))
        self.assertEqual((self.root/'app.txt').read_text(),'one\nUPSTREAM\nTASK\nfour\nfive\nSIX\n')


if __name__ == '__main__': unittest.main()
