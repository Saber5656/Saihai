"""Host-only App mediation journal. No App IPC, credentials or worker authority.

Only the host callback invoked by app_execution.mjs supplies tool returns.
Receipts are transport observations, never proof of isolation or task completion.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import subprocess
import uuid

import host_publication_adapter as publication
import run_lock
import run_store
import vault_task_records


class AppExecutionError(RuntimeError):
    pass


def _digest(value):
    return publication.digest(value)


def _id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value))


def _actual_id(value):
    try:
        return isinstance(value,str) and str(uuid.UUID(value))==value
    except (ValueError,AttributeError):
        return False


def _data(raw):
    """Only structured tool result or a single JSON text block; no prose inference."""
    if not isinstance(raw, dict) or len(json.dumps(raw).encode()) > 1024 * 1024:
        raise AppExecutionError('tool_response_invalid')
    if raw.get('isError'):
        raise AppExecutionError('tool_error_response')
    if isinstance(raw.get('structuredContent'), dict):
        return raw['structuredContent']
    blocks = raw.get('content')
    if isinstance(blocks, list):
        texts = [v.get('text') for v in blocks if isinstance(v, dict) and v.get('type') == 'text']
        if len(texts) == 1:
            try:
                value = json.loads(texts[0])
            except (ValueError, TypeError):
                raise AppExecutionError('tool_response_unstructured')
            if isinstance(value, dict):
                return value
    raise AppExecutionError('tool_response_unstructured')


def _git(root, *args):
    import os
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0',GIT_TERMINAL_PROMPT='0')
    try:
        p=subprocess.run(['git','-c','core.fsmonitor=false',*args],cwd=root,env=env,
                         capture_output=True,timeout=30)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise AppExecutionError('checkout_unavailable') from exc
    if p.returncode:
        raise AppExecutionError('checkout_unavailable')
    return p.stdout.decode().strip()


def canonical_repository(key):
    if key not in {'SAIHAI_ROOT','DOTFILES_ROOT','SKILLS_REPO_ROOT'}:
        raise AppExecutionError('repository_catalog_key_invalid')
    import sys
    primary=Path.home()/'dev/Saihai'
    sys.path.insert(0,str(primary))
    import directory_paths
    env={}
    if directory_paths.load_environment(checkout_root=primary,environ=env,require_catalog=True)['status']!='loaded':
        raise AppExecutionError('catalog_unavailable')
    directory_paths.validate_vault(env)
    return Path(env[key])


class HostAppJournal:
    """Construct only in the trusted host; never expose accept() as worker RPC.

    State must be outside every child checkout. Existing task authority binds
    plans; this object does not grant App filesystem access or model overrides.
    """
    def __init__(self, authorization, state_root: Path, *, capacity=1, coordinator_reserve=0, review_reserve=0):
        from trusted_local_executor import TrustedLocalAuthorization, _authorization_material
        if not isinstance(authorization,TrustedLocalAuthorization):
            raise AppExecutionError('independent_host_authorization_required')
        if (any(type(v)!=int or v<0 for v in (capacity,coordinator_reserve,review_reserve))
                or not 1<=capacity<=8 or capacity<=coordinator_reserve+review_reserve):
            raise AppExecutionError('capacity_invalid')
        root=Path(state_root);worker=Path(authorization.publication.worktree).resolve()
        if not root.is_absolute() or root.resolve()!=root or root==worker or worker in root.parents:
            raise AppExecutionError('host_private_state_required')
        self.auth=authorization;self.binding=_digest(_authorization_material(authorization));self.root=root/'app-execution'
        self.capacity=capacity-coordinator_reserve-review_reserve
        self.profile={'capacity':capacity,'coordinator_reserve':coordinator_reserve,'review_reserve':review_reserve}
        run_store.ensure_private_directory(self.root)

    def _read(self):
        path=self.root/'journal.json'
        if not run_store.private_artifact_exists(path):
            return {'version':1,'profile':self.profile,'operations':{},'revision':0}
        value=run_store.read_json(path,max_bytes=4*1024*1024)
        if value.get('version')!=1 or value.get('profile')!=self.profile:
            raise AppExecutionError('journal_profile_changed')
        return value

    def _save(self,value):
        value['revision']+=1
        run_store.atomic_write_json(self.root/'journal.json',value)

    def _lock(self):
        return run_lock.hold_global_lock(self.root,operation='app_execution',run_id=self.auth.publication.run_id,
            principal={'principal_type':'harness_runner','principal_id':'app-host-mediator','authn_method':'local_cli'})

    def _row(self,state,operation_id):
        row=state['operations'].get(operation_id)
        if not row or row['authority_digest']!=self.binding:
            raise AppExecutionError('operation_authority_mismatch')
        return row

    def reserve(self, plan: dict):
        fields={'operation_id','issue_id','repository_key','project_id','host_id','base','instruction','resources','dependencies'}
        if (not isinstance(plan,dict) or set(plan)!=fields or not _id(plan['operation_id'])
                or not re.fullmatch(r'[0-9]{1,10}',str(plan['issue_id'])) or not _id(plan['project_id']) or not _id(plan['host_id'])
                or not re.fullmatch(r'[a-f0-9]{40}',str(plan['base']))
                or not isinstance(plan['instruction'],str) or not 1<=len(plan['instruction'].encode())<=65536
                or any(not isinstance(plan[k],list) or len(plan[k])>32 or any(not _id(v) for v in plan[k]) for k in ('resources','dependencies'))
                or len(set(plan['resources']))!=len(plan['resources']) or plan['operation_id'] in plan['dependencies']):
            raise AppExecutionError('plan_invalid')
        if plan['host_id']!='local':
            raise AppExecutionError('remote_checkout_verifier_unavailable')
        host=self.auth.publication
        vault_task_records.bind_task(host.task_id,authority_ref=host.authority_evidence_ref)
        canonical=canonical_repository(plan['repository_key']).resolve()
        if (_git(canonical,'rev-parse','--show-toplevel')!=str(canonical)
                or not publication._remote_matches(_git(canonical,'remote','get-url','origin'),host.repository)):
            raise AppExecutionError('repository_identity_mismatch')
        if plan['base']!=host.base:
            raise AppExecutionError('base_authority_mismatch')
        with self._lock():
            state=self._read();existing=state['operations'].get(plan['operation_id'])
            if existing:
                if existing['plan_digest']!=_digest(plan) or existing['authority_digest']!=self.binding:
                    raise AppExecutionError('operation_plan_changed')
                return self._summary(existing)
            if len(state['operations'])>=128:
                raise AppExecutionError('journal_capacity_exhausted')
            row={'plan':plan,'plan_digest':_digest(plan),'authority_digest':self.binding,'task_id':host.task_id,
                 'repository':host.repository,'canonical_path':str(canonical),'state':'planned','leased':False,
                 'receipts':[],'client_id':None,'thread_id':None,'checkout':None,'inflight':None,'project_verified':False}
            state['operations'][plan['operation_id']]=row;self._save(state);return self._summary(row)

    def _summary(self,row):
        return {k:row.get(k) for k in ('state','reason','client_id','thread_id','checkout','turn_id','leased')} | {
            'operation_id':row['plan']['operation_id'],'evidence_kind':('host_mediated_tool_observation' if row.get('receipt_count') else 'host_plan_record'),
            'task_complete':False,'app_filesystem_fencing_proven':False}

    def next(self,operation_id):
        """Claim one supported tool invocation under the short state lock."""
        with self._lock():
            state=self._read();row=self._row(state,operation_id);plan=row['plan']
            if row['inflight']:
                return {'status':'waiting','reason':'tool_response_pending_or_unknown'}
            if row['state'] in {'pending','creation_unknown','dispatch_unknown','blocked','terminal','quota_queued'}:
                return {'status':'waiting','reason':row['state']}
            def queued(reason):
                row['reason']=reason;self._save(state)
                return {'status':'queued','reason':reason}
            if not row['leased']:
                active=[r for r in state['operations'].values() if r['leased']]
                dependencies=[state['operations'].get(v) for v in plan['dependencies']]
                if any(not d or d['state']!='terminal' or not d.get('result_accepted') for d in dependencies):
                    return queued('dependency_pending')
                if any(r['repository']==row['repository'] and r['plan']['issue_id']==plan['issue_id'] for r in active):
                    return queued('issue_writer_leased')
                if any(set(r['plan']['resources']) & set(plan['resources']) for r in active):
                    return queued('resource_leased')
                if len(active)>=self.capacity:
                    return queued('capacity_reserved')
                row['leased']=True
            if row['state']=='planned':
                if not row['project_verified']:
                    method,args='list_projects',{}
                else:
                    method='create_thread';args={'title':f"Issue {plan['issue_id']}",
                        'prompt':'Read-only startup only. Do not edit files or start implementation. Wait for a separate authorized implementation message.',
                        'target':{'type':'project','projectId':plan['project_id'],'environment':{
                            'type':'worktree','startingState':{'type':'branch','branchName':plan['base']}}}}
                    row['state']='creating'
            elif row['state']=='identity_waiting':
                method,args='list_threads',{'limit':50}
            elif row['state']=='running':
                method,args='wait_threads',{'targets':[{'threadId':row['thread_id'],'hostId':plan['host_id']}],'timeoutMs':30000}
            elif row['state'] in {'checkout_waiting','sent','polling'}:
                method,args='read_thread',{'threadId':row['thread_id'],'hostId':plan['host_id'],'turnLimit':3,'includeOutputs':False}
            elif row['state']=='ready':
                try:
                    self._verify_checkout(row,row['checkout'])
                except AppExecutionError as exc:
                    row.update(state='blocked',reason=str(exc));self._save(state)
                    return {'status':'waiting','reason':str(exc)}
                method,args='send_message_to_thread',{'threadId':row['thread_id'],'hostId':plan['host_id'],'prompt':plan['instruction']}
                row['state']='dispatching'
            else:
                return {'status':'waiting','reason':'unsupported_state'}
            token=uuid.uuid4().hex
            row['reason']=None
            row['inflight']={'token':token,'method':method,'args_digest':_digest(args)}
            self._save(state)
            return {'status':'invoke','token':token,'method':method,'args':args}

    def _verify_checkout(self,row,cwd):
        root=Path(cwd);canonical=Path(row['canonical_path'])
        if (not root.is_absolute() or root.resolve()!=root or root==canonical
                or root==self.root or self.root in root.parents or root in self.root.parents
                or canonical_repository(row['plan']['repository_key']).resolve()!=canonical):
            raise AppExecutionError('checkout_identity_mismatch')
        if _git(root,'rev-parse','--show-toplevel')!=str(root):
            raise AppExecutionError('checkout_identity_mismatch')
        entries=_git(canonical,'worktree','list','--porcelain').split('\n\n')
        if sum(e.splitlines()[0]=='worktree '+str(root) for e in entries if e.splitlines())!=1:
            raise AppExecutionError('checkout_registry_mismatch')
        common=lambda p:Path(_git(p,'rev-parse','--path-format=absolute','--git-common-dir')).resolve()
        if common(root)!=common(canonical) or not publication._remote_matches(_git(root,'remote','get-url','origin'),row['repository']):
            raise AppExecutionError('checkout_repository_mismatch')
        if not _git(root,'branch','--show-current').startswith('codex/'):
            raise AppExecutionError('checkout_branch_mismatch')
        if _git(root,'rev-parse','HEAD')!=row['plan']['base']:
            raise AppExecutionError('checkout_stale_base')
        if _git(root,'status','--porcelain','--untracked-files=all'):
            raise AppExecutionError('checkout_dirty')
        return str(root)

    def accept(self,token,raw):
        """Private host callback; never an untrusted result-ingestion endpoint."""
        with self._lock():
            state=self._read();rows=[r for r in state['operations'].values() if r.get('inflight',{} ) and r['inflight']['token']==token]
            if len(rows)!=1 or rows[0]['authority_digest']!=self.binding:
                raise AppExecutionError('tool_invocation_unknown')
            row=rows[0];call=row['inflight'];method=call['method'];row['inflight']=None
            receipt={'method':method,'args_digest':call['args_digest'],'response_digest':_digest(raw)}
            row['receipt_chain_digest']=_digest([row.get('receipt_chain_digest'),receipt])
            row['receipt_count']=row.get('receipt_count',0)+1
            row['receipts']=(row['receipts']+[receipt])[-256:]
            try:
                data=_data(raw)
                row['reason']=None
                self._apply(state,row,method,data)
            except (AppExecutionError,KeyError,TypeError,ValueError) as exc:
                row['state']='creation_unknown' if method=='create_thread' else 'dispatch_unknown' if method=='send_message_to_thread' else 'blocked'
                row['reason']=str(exc) if isinstance(exc,AppExecutionError) else 'tool_shape_invalid'
            self._save(state);return self._summary(row)

    def lost(self,token):
        # Host transport threw/was cancelled: reserve ownership, never retry create.
        return self.accept(token,{'isError':True,'content':[]})

    def _apply(self,state,row,method,data):
        plan=row['plan']
        if method=='list_projects':
            found=[p for p in data['projects'] if p.get('projectId')==plan['project_id']]
            if len(found)!=1 or found[0].get('hostId')!=plan['host_id'] or not found[0].get('isGitRepository') or Path(found[0]['path']).resolve()!=Path(row['canonical_path']):
                raise AppExecutionError('project_identity_mismatch')
            row['project_verified']=True
        elif method=='create_thread':
            actual=data.get('threadId');client=data.get('clientThreadId')
            if actual:
                if not _actual_id(actual) or data.get('hostId')!=plan['host_id']:
                    raise AppExecutionError('created_thread_identity_invalid')
                if any(r is not row and r.get('thread_id')==actual for r in state['operations'].values()):
                    raise AppExecutionError('thread_already_owned')
                row.update(thread_id=actual,state='identity_waiting')
            elif _id(client):
                row.update(client_id=client,state='pending')
            else:raise AppExecutionError('creation_unresolved')
        elif method=='list_threads':
            candidates=data.get('pinnedThreads',[])+data['threads']
            found=[t for t in candidates if t.get('id')==row['thread_id']]
            if len(found)!=1:
                row['reason']='actual_thread_not_visible';return
            info=found[0]
            if info.get('hostId')!=plan['host_id'] or info.get('projectId')!=plan['project_id'] or info.get('kind')!='codex':
                raise AppExecutionError('thread_identity_mismatch')
            row['listed_checkout']=info['cwd'];row['state']='checkout_waiting'
        elif method=='read_thread':
            info=data['thread']
            if info.get('id')!=row['thread_id'] or info.get('hostId')!=plan['host_id'] or info.get('cwd')!=row['listed_checkout']:
                raise AppExecutionError('actual_checkout_mismatch')
            if row['state']=='checkout_waiting':
                row['checkout']=self._verify_checkout(row,info['cwd'])
                if any(r is not row and r['leased'] and r.get('checkout')==row['checkout'] for r in state['operations'].values()):
                    raise AppExecutionError('checkout_writer_leased')
                row['state']='ready';return
            for turn in data.get('turns',[]):
                matches=any(item.get('type')=='userMessage' and item.get('text')==plan['instruction'] for item in turn.get('items',[]))
                if (_actual_id(row.get('turn_id')) and row['turn_id']==turn.get('id')) or (matches and _actual_id(turn.get('id'))):
                    row['turn_id']=turn['id'];row['state']='running'
                    if turn.get('status') in {'failed','interrupted','error'}:
                        row.update(state='blocked',reason='child_turn_failed_reservation_retained')
                        return
                    status=info.get('status',{});status=status.get('type') if isinstance(status,dict) else status
                    if turn.get('status')=='completed' and status in {'idle','notLoaded','completed'}:
                        row.update(state='terminal',leased=False)
                    return
            row['reason']='startup_or_result_not_attributable'
        elif method=='wait_threads':
            # A wake/status snapshot is not an attributed implementation result.
            row['state']='polling'
        elif method=='send_message_to_thread':
            # Structured acknowledgement is not proof that the task started.
            row['state']='sent'
        else:raise AppExecutionError('unsupported_tool')

    def preserve_pending(self,operation_id,client_id):
        """Reserve a previously issued unresolved fork; never makes it ready."""
        if not _id(client_id) or not client_id.startswith('client-'):
            raise AppExecutionError('pending_client_id_invalid')
        with self._lock():
            state=self._read();row=self._row(state,operation_id)
            if row['state'] not in {'planned','pending'} or (row['client_id'] and row['client_id']!=client_id):
                raise AppExecutionError('pending_reservation_conflict')
            row.update(state='pending',client_id=client_id,leased=True,reason='historical_host_pending_reservation_not_ready_proof')
            self._save(state)
            return self._summary(row)

    def accept_completion(self,operation_id,child_authorization):
        """Dependency release uses existing host validation/publication evidence."""
        from trusted_local_executor import TrustedLocalAuthorization
        if not isinstance(child_authorization,TrustedLocalAuthorization):
            raise AppExecutionError('independent_child_authority_required')
        with self._lock():
            state=self._read();row=self._row(state,operation_id);host=child_authorization.publication
            if (row['state']!='terminal' or host.task_id!=row['task_id'] or host.repository!=row['repository']
                    or host.worktree!=row['checkout']):
                raise AppExecutionError('completion_identity_mismatch')
            run_store.validate_artifact_id(host.execution_id,'execution_id')
            directory=self.root.parent/'trusted-local'/host.execution_id
            from trusted_local_executor import _authorization_material
            claim=run_store.read_json(directory/'claim.json')
            request=run_store.read_json(directory/'request.json')
            if (claim.get('authorization_digest')!=_digest(_authorization_material(child_authorization))
                    or request.get('instruction')!=row['plan']['instruction']
                    or request.get('task_id')!=row['task_id']
                    or request.get('execution_id')!=host.execution_id):
                raise AppExecutionError('completion_dispatch_mismatch')
            report=run_store.read_json(directory/'report.json')
            publication.validate_report(report,host)
            receipt=run_store.read_json(directory/'publication.json')
            if receipt.get('status')!='complete' or not re.fullmatch(r'[a-f0-9]{40}',receipt.get('merge_commit','')):
                raise AppExecutionError('completion_not_proven')
            row['result_accepted']=True;row['completion_digest']=_digest(receipt);self._save(state)
            return self._summary(row)

    def queue_unavailable(self,operation_id,reason='provider_unavailable'):
        """Host capacity observation only, never change model or retry creation."""
        if reason not in {'provider_unavailable','quota_exhausted'}:
            raise AppExecutionError('availability_reason_invalid')
        with self._lock():
            state=self._read();row=self._row(state,operation_id)
            if row['state']!='planned' or row['inflight']:
                raise AppExecutionError('operation_already_issued')
            row.update(state='quota_queued',reason=reason);self._save(state)
            return self._summary(row)

    def resume_queue(self,operation_id):
        with self._lock():
            state=self._read();row=self._row(state,operation_id)
            if row['state']!='quota_queued':raise AppExecutionError('operation_not_queued')
            row.update(state='planned',reason=None);self._save(state)
            return self._summary(row)

    def snapshot(self,operation_id):
        with self._lock():return self._summary(self._row(self._read(),operation_id))
