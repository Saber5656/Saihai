"""Hermetic App-tool fixtures and real temporary Git checkout gates.

No fixture below creates a visible App task or claims live tool provenance.
"""
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import app_execution as app
import host_publication_adapter as pub
import trusted_local_executor as local


def response(value):return {'content':[{'type':'text','text':json.dumps(value)}]}

class AppExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.repo=self.root/'repo';self.repo.mkdir()
        self.git('init','-b','main');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        (self.repo/'file').write_text('base');self.git('add','.');self.git('commit','-m','base')
        self.base=self.git('rev-parse','HEAD');self.git('remote','add','origin','https://github.com/example/repo.git')
        host=pub.HostAuthorization('TSK-20260907-app','request','run','execution','example/repo',str(self.repo),
                                  'main',self.base,self.base,('.',),('ci',),'sha256:'+'1'*64,'existing-task-authority')
        self.auth=local.TrustedLocalAuthorization(host,'unused','unused','unused','existing-model',(('true',),))
        for patcher in (patch.object(app,'canonical_repository',return_value=self.repo),
                        patch.object(app.vault_task_records,'bind_task',return_value={'task_id':host.task_id})):
            patcher.start();self.addCleanup(patcher.stop)
        self.j=app.HostAppJournal(self.auth,self.root/'state',capacity=6,coordinator_reserve=1)
    def git(self,*args):return subprocess.check_output(['git',*args],cwd=self.repo,stderr=subprocess.DEVNULL,text=True).strip()
    def plan(self,n,**extra):
        return dict(operation_id='op'+str(n),issue_id=str(n),repository_key='SAIHAI_ROOT',project_id='project',host_id='local',
                    base=self.base,instruction='Implement approved fixture '+str(n),resources=['resource'+str(n)],dependencies=[],**extra)
    def accept(self,op,method,value):
        command=self.j.next(op);self.assertEqual(command['method'],method,command)
        return self.j.accept(command['token'],response(value))
    def create(self,n):
        plan=self.plan(n);self.j.reserve(plan);op=plan['operation_id']
        self.accept(op,'list_projects',{'projects':[{'projectId':'project','hostId':'local','path':str(self.repo),'isGitRepository':True}]})
        thread=str(uuid.uuid4());self.accept(op,'create_thread',{'threadId':thread,'hostId':'local'})
        cwd=self.root/('child'+str(n));self.git('worktree','add','-b','codex/task'+str(n),str(cwd))
        return op,thread,cwd
    def ready(self,n):
        op,thread,cwd=self.create(n)
        self.accept(op,'list_threads',{'threads':[{'id':thread,'kind':'codex','hostId':'local','projectId':'project','cwd':str(cwd)}]})
        value={'thread':{'id':thread,'hostId':'local','cwd':str(cwd),'status':{'type':'idle'}},'turns':[]}
        result=self.accept(op,'read_thread',value);self.assertEqual(result['state'],'ready')
        return op,thread,cwd,value
    def test_five_distinct_fixture_children_and_attributed_results(self):
        children=[self.ready(n) for n in range(5)]
        self.assertEqual(len({x[1] for x in children}),5);self.assertEqual(len({x[2] for x in children}),5)
        self.assertEqual(self.git('rev-parse','HEAD'),self.base);self.assertEqual(self.git('status','--porcelain'),'')
        for n,(op,thread,cwd,value) in enumerate(children):
            self.accept(op,'send_message_to_thread',{'status':'sent'})
            value['turns']=[{'id':str(uuid.uuid4()),'status':'completed','items':[{'type':'userMessage','text':self.plan(n)['instruction']}]}]
            result=self.accept(op,'read_thread',value)
            self.assertEqual(result['state'],'terminal');self.assertFalse(result['task_complete']);self.assertFalse(result['leased'])
    def test_pending_restart_never_recreates(self):
        plan=self.plan(1);self.j.reserve(plan)
        self.accept('op1','list_projects',{'projects':[{'projectId':'project','hostId':'local','path':str(self.repo),'isGitRepository':True}]})
        self.accept('op1','create_thread',{'clientThreadId':'client-new-thread:'+str(uuid.uuid4())})
        restarted=app.HostAppJournal(self.auth,self.root/'state',capacity=6,coordinator_reserve=1)
        self.assertEqual(restarted.next('op1'),{'status':'waiting','reason':'pending'})
        self.assertEqual(restarted.reserve(plan)['state'],'pending')
    def test_lost_creation_response_holds_reservation(self):
        self.j.reserve(self.plan(1));self.accept('op1','list_projects',{'projects':[{'projectId':'project','hostId':'local','path':str(self.repo),'isGitRepository':True}]})
        request=self.j.next('op1');self.assertEqual(request['method'],'create_thread')
        self.assertEqual(self.j.next('op1')['reason'],'tool_response_pending_or_unknown')
        self.j.lost(request['token']);self.assertEqual(self.j.next('op1')['reason'],'creation_unknown')
    def test_wrong_project_host_and_malformed_id(self):
        self.j.reserve(self.plan(1));result=self.accept('op1','list_projects',{'projects':[{'projectId':'project','hostId':'wrong','path':str(self.repo),'isGitRepository':True}]})
        self.assertEqual(result['state'],'blocked')
        self.j.reserve(self.plan(2));self.accept('op2','list_projects',{'projects':[{'projectId':'project','hostId':'local','path':str(self.repo),'isGitRepository':True}]})
        result=self.accept('op2','create_thread',{'threadId':'made-up-id','hostId':'local'})
        self.assertEqual(result['state'],'creation_unknown')
    def test_wrong_actual_cwd_or_stale_base_blocks_dispatch(self):
        op,thread,cwd=self.create(1)
        self.accept(op,'list_threads',{'threads':[{'id':thread,'kind':'codex','hostId':'local','projectId':'project','cwd':str(cwd)}]})
        result=self.accept(op,'read_thread',{'thread':{'id':thread,'hostId':'local','cwd':str(self.repo)}})
        self.assertEqual(result['state'],'blocked')
        op,thread,cwd,value=self.ready(2)
        (cwd/'file').write_text('modified');subprocess.run(['git','add','.'],cwd=cwd,check=True)
        subprocess.run(['git','commit','-m','stale'],cwd=cwd,check=True,capture_output=True)
        self.assertEqual(self.j.next(op)['reason'],'checkout_stale_base')
        self.assertEqual(self.j.snapshot(op)['state'],'blocked')
    def test_leases_dependencies_capacity_and_quota(self):
        first=self.plan(1);self.j.reserve(first);self.j.next('op1')
        second=self.plan(2);second['resources']=first['resources'];self.j.reserve(second)
        self.assertEqual(self.j.next('op2')['reason'],'resource_leased')
        self.assertEqual(self.j.snapshot('op2')['reason'],'resource_leased')
        third=self.plan(3);third['dependencies']=['op1'];self.j.reserve(third)
        self.assertEqual(self.j.next('op3')['reason'],'dependency_pending')
        fourth=self.plan(4);self.j.reserve(fourth);self.j.queue_unavailable('op4','quota_exhausted')
        self.assertEqual(self.j.next('op4')['reason'],'quota_queued');self.j.resume_queue('op4')
        self.assertEqual(self.j.next('op4')['method'],'list_projects')
    def test_duplicate_issue_and_foreign_response_rejected(self):
        first=self.plan(1);self.j.reserve(first);self.j.next('op1')
        second=self.plan(2);second['issue_id']='1';self.j.reserve(second)
        self.assertEqual(self.j.next('op2')['reason'],'issue_writer_leased')
        with self.assertRaisesRegex(app.AppExecutionError,'invocation_unknown'):
            self.j.accept('arbitrary',response({'threadId':str(uuid.uuid4()),'hostId':'local'}))
    def test_wait_is_not_result_success_and_send_not_startup(self):
        op,thread,cwd,value=self.ready(1);self.accept(op,'send_message_to_thread',{'status':'sent'})
        self.assertEqual(self.accept(op,'read_thread',value)['state'],'sent')
        value['turns']=[{'id':None,'status':'completed','items':[]}]
        self.assertEqual(self.accept(op,'read_thread',value)['state'],'sent')
        value['thread']['status']={'type':'active'}
        value['turns']=[{'id':str(uuid.uuid4()),'status':'inProgress','items':[{'type':'userMessage','text':self.plan(1)['instruction']}]}]
        self.assertEqual(self.accept(op,'read_thread',value)['state'],'running')
        self.assertEqual(self.accept(op,'wait_threads',{'status':'completed'})['state'],'polling')
    def test_existing_pending_is_not_creation_proof(self):
        self.j.reserve(self.plan(1));result=self.j.preserve_pending('op1','client-new-thread:'+str(uuid.uuid4()))
        self.assertEqual(result['state'],'pending');self.assertIsNone(result['thread_id'])
        self.assertEqual(result['evidence_kind'],'host_plan_record')
        self.assertEqual(self.j.next('op1')['reason'],'pending')
    def test_failed_attributed_turn_retains_reservation_without_running_claim(self):
        op,thread,cwd,value=self.ready(1);self.accept(op,'send_message_to_thread',{'status':'sent'})
        value['turns']=[{'id':str(uuid.uuid4()),'status':'failed','items':[{'type':'userMessage','text':self.plan(1)['instruction']}]}]
        result=self.accept(op,'read_thread',value)
        self.assertEqual(result['state'],'blocked');self.assertTrue(result['leased'])
        self.assertEqual(result['reason'],'child_turn_failed_reservation_retained')
    def test_capacity_reserves_coordinator_and_concurrent_claim_is_single(self):
        import concurrent.futures
        for n in range(6):self.j.reserve(self.plan(n))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.j.next('op0'),range(2)))
        self.assertEqual(sum(r['status']=='invoke' for r in results),1)
        for n in range(1,5):self.assertEqual(self.j.next('op'+str(n))['status'],'invoke')
        self.assertEqual(self.j.next('op5')['reason'],'capacity_reserved')
    def test_resources_are_shared_across_repositories(self):
        first=self.plan(1);first['resources']=['port-5000'];self.j.reserve(first);self.j.next('op1')
        other=self.root/'other'
        subprocess.run(['git','clone',str(self.repo),str(other)],capture_output=True,check=True)
        subprocess.run(['git','remote','set-url','origin','https://github.com/example/other.git'],cwd=other,check=True)
        auth=dataclasses.replace(self.auth,publication=dataclasses.replace(self.auth.publication,repository='example/other',worktree=str(other)))
        journal=app.HostAppJournal(auth,self.root/'state',capacity=6,coordinator_reserve=1)
        plan=self.plan(2);plan['resources']=['port-5000'];plan['repository_key']='DOTFILES_ROOT'
        with patch.object(app,'canonical_repository',return_value=other):
            journal.reserve(plan);self.assertEqual(journal.next('op2')['reason'],'resource_leased')
    def test_remote_host_cannot_use_local_checkout_as_proof(self):
        plan=self.plan(1);plan['host_id']='remote'
        with self.assertRaisesRegex(app.AppExecutionError,'remote_checkout_verifier_unavailable'):self.j.reserve(plan)
    def test_js_adapter_calls_only_captured_capabilities(self):
        script=Path(__file__).with_name('app_execution_adapter_fixture.mjs')
        r=subprocess.run(['node',str(script)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

if __name__=='__main__':unittest.main()
