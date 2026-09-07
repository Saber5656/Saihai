"""Actual host validation observations and conservative reuse; no worker authority."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
from functools import lru_cache


class ValidationError(ValueError):
    pass


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def runner():
    path = Path(__file__).resolve().parents[4] / 'scripts/validate_all.py'
    spec = importlib.util.spec_from_file_location('saihai_host_result_parser', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def observe(command, stdout, stderr, exit_code):
    """A command check has no invented test count; declared tests require counts."""
    parser = runner()
    out = stdout.decode(errors='replace') if isinstance(stdout, bytes) else stdout
    err = stderr.decode(errors='replace') if isinstance(stderr, bytes) else stderr
    from validation_evidence import parse_json, EvidenceError
    try:
        payload = parse_json(out.strip().encode())
        json_state = 'object'
    except EvidenceError:
        lines = out.strip().splitlines()
        payload, json_state = parser.suite_json_result(lines[-1]) if lines else (None, 'text')
    is_test = any(Path(x).name.startswith('test_') or Path(x).name in {'pytest','unittest','validate_all.py'} for x in command)
    result = {'kind':'check','passed':type(exit_code) is int and exit_code == 0,
              'executed':None,'failed':None,'skipped':None,'unknown':None,'count_method':'not_applicable'}
    if payload is not None and 'suites' in payload:
        suites = payload['suites']; contracts = payload.get('contracts')
        result.update(kind='tests',executed=0,failed=0,skipped=0,unknown=0,count_method='suite_results')
        valid = (payload.get('result') == 'pass' and payload.get('compiled') is True
                 and type(suites) is list and bool(suites) and type(contracts) is list and bool(contracts)
                 and all(type(c) is dict and c.get('result')=='pass' for c in contracts))
        if type(suites) is list:
            for suite in suites:
                if type(suite) is not dict:
                    valid=False;continue
                counts={k:suite.get(k) for k in ('executed','failed','skipped','unknown')}
                if suite.get('status') != 'passed' or type(suite.get('exit_code')) is not int or suite['exit_code'] != 0:
                    valid=False
                if any(type(counts[k]) is not int for k in ('executed','failed','skipped','unknown')):
                    valid=False;continue
                for key in ('executed','failed','skipped','unknown'):result[key]+=counts[key]
                valid=valid and suite.get('result')=='pass' and counts['executed']>0
        result['passed'] = result['passed'] and valid and result['executed']>0 and not any(result[k] for k in ('failed','skipped','unknown'))
        if 'selection' in payload:
            result['selection']=payload['selection']
    elif is_test or (payload is not None and ('cases' in payload or 'result' in payload)) or parser.unittest_counts(out,err) is not None:
        counts=parser.unknown_counts(); terminal=False
        summary=parser.unittest_counts(out,err)
        if payload is not None:
            counts=parser.structured_counts(payload); terminal=payload.get('result')=='pass'
            if summary is not None:
                observed,passed=summary
                terminal=terminal and passed and all(observed[k]==counts[k] for k in ('executed','failed','skipped','unknown'))
        elif summary is not None:
            counts,terminal=summary
            terminal = terminal and json_state == 'text'
        elif any('pytest' in x for x in command):
            final=out.strip().splitlines()[-1] if out.strip() else ''
            match=re.fullmatch(r'=*\s*(.*?) in [0-9.]+s(?: \([^\n]*\))?\s*=*',final)
            if match:
                values=re.findall(r'(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed)',match[1])
                if values:
                    totals={k:sum(int(n) for n,name in values if name==k) for k in ('passed','failed','skipped','error','errors','xfailed','xpassed')}
                    counts={'executed':totals['passed']+totals['failed']+totals['error']+totals['errors'],
                            'failed':sum(totals[k] for k in ('failed','error','errors','xfailed','xpassed')),
                            'skipped':totals['skipped'],'unknown':0,'count_method':'pytest_summary'}
                    terminal=True
        result.update(kind='tests',**{k:counts[k] for k in ('executed','failed','skipped','unknown','count_method')})
        result['passed']=result['passed'] and terminal and type(counts['executed']) is int and counts['executed']>0 and all(counts[k]==0 for k in ('failed','skipped','unknown'))
    return result


def source_digest(root):
    """Conservative dependency boundary includes all nonignored source and modes."""
    from validation_evidence import read_bytes, EvidenceError
    try:
        raw=subprocess.check_output(['git','ls-files','-co','--exclude-standard','-z'],cwd=root,timeout=30)
        names=sorted(set(p.decode() for p in raw.split(b'\0') if p)); rows=[]
        if len(names)>20000:raise ValidationError('source_budget')
        for name in names:
            path=root/name
            if path.is_symlink():content=path.readlink().as_posix().encode();mode='symlink'
            elif path.is_file():content=read_bytes(root,name);mode=oct(path.stat().st_mode & 0o777)
            elif not path.exists():content=b'';mode='deleted'
            else:raise ValidationError('source_nonregular')
            rows.append([name,mode,digest(content)])
        return digest(rows)
    except (OSError,UnicodeError,subprocess.SubprocessError,EvidenceError) as exc:
        raise ValidationError('source_identity_unavailable') from exc


def command_digest(command):
    executable=Path(command[0])
    if not executable.is_absolute() or not executable.is_file():
        return None  # PATH resolution is unsuitable for cross-run reuse.
    return digest({'argv':list(command),'executable':digest(executable.read_bytes())})


def validate_receipt(receipt, *, identity, commands=None):
    if type(receipt) is not dict or receipt.get('validation_version')!=2 or type(receipt.get('validation_version')) is not int:
        raise ValidationError('validation_version_missing')
    if receipt.get('status')!='passed' or any(receipt.get(k)!=v for k,v in identity.items()):
        raise ValidationError('validation_identity_mismatch')
    rows=receipt.get('commands')
    if type(rows) is not list or not rows or len(rows)>128:
        raise ValidationError('validation_commands_missing')
    if commands is not None and [r.get('argv') for r in rows]!=[list(c) for c in commands]:
        raise ValidationError('validation_plan_mismatch')
    for row in rows:
        if (type(row) is not dict or type(row.get('exit')) is not int or row['exit']!=0 or row.get('passed') is not True
                or type(row.get('argv')) is not list or not row['argv'] or any(type(a) is not str or not a for a in row['argv'])):
            raise ValidationError('validation_command_not_passed')
        if any(type(row.get(k)) not in (int,float) or not math.isfinite(row[k]) for k in ('started_at_epoch','ended_at_epoch')) or row['ended_at_epoch']<row['started_at_epoch']:
            raise ValidationError('validation_time_invalid')
        if row.get('kind')=='tests':
            if (any(type(row.get(k)) is not int for k in ('executed','failed','skipped','unknown'))
                    or row['executed']<=0 or any(row[k]!=0 for k in ('failed','skipped','unknown'))
                    or row.get('count_method') not in ('structured_result','completed_test_functions','unittest_summary','suite_results','pytest_summary')):
                raise ValidationError('required_test_evidence_invalid')
        elif row.get('kind')!='check' or any(row.get(k) is not None for k in ('executed','failed','skipped','unknown')):
            raise ValidationError('validation_kind_invalid')
        for key in ('stdout_digest','stderr_digest'):
            if type(row.get(key)) is not str or re.fullmatch('sha256:[a-f0-9]{64}',row[key]) is None:
                raise ValidationError('output_binding_missing')
    profile = receipt.get('delivery_profile')
    if type(profile) is not dict or profile.get('state') not in ('passed','host_commands'):
        raise ValidationError('delivery_profile_not_passed')
    if receipt.get('plan_digest')!=digest([r['argv'] for r in rows]):
        raise ValidationError('validation_plan_digest_mismatch')
    if type(receipt.get('source_digest')) is not str or re.fullmatch('sha256:[a-f0-9]{64}',receipt['source_digest']) is None:
        raise ValidationError('source_binding_missing')


def reusable(receipt, *, root, commands):
    try:
        validate_receipt(receipt,identity={},commands=commands)
        return receipt['source_digest']==source_digest(root) and all(row.get('command_digest')==command_digest(command) and row.get('command_digest') is not None for row,command in zip(receipt['commands'],commands))
    except (ValidationError,KeyError,TypeError,OSError):
        return False


def workflow_parity(root):
    """Use the existing actual workflow inventory when this repository owns it."""
    path=root/'organization/runtime/workflows/profiles/delivery-inventory/saihai.v1.json'
    if not path.exists():return {'state':'not_configured'}
    import delivery_workflow_inventory as inventory
    try:
        expected=json.loads(path.read_bytes())
        head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,timeout=20).decode().strip()
        result=inventory.audit_repository(root,expected,{'event':'pull_request','ref':'refs/heads/task','base_ref':'refs/heads/main','fork':False,'action':None},
                    {'repository':expected['repository'],'head_sha':head,'base_sha':head})
        if result.get('parsing')!='valid' or result.get('parity')!='match':raise ValidationError('workflow_inventory_drift')
        return {'state':'match','inventory_digest':digest(path.read_bytes()),'policy':'host_task_authority; inventory grants no permission'}
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        raise ValidationError('workflow_inventory_drift') from exc


def load_profile(reference, root):
    """Optional host-owned plan; default absent keeps prior authority digests stable."""
    if reference is None:return None
    from validation_evidence import read_bytes, parse_json
    if type(reference) is not dict or set(reference)!={'path','sha256'}:
        raise ValidationError('host_profile_reference_invalid')
    path=Path(reference['path'])
    if (not path.is_absolute() or path.resolve(strict=True)!=path or path.is_relative_to(root)
            or path.stat().st_mode & 0o077):
        raise ValidationError('host_profile_must_be_private_and_outside_worker')
    raw=read_bytes(path.parent,path.name)
    if hashlib.sha256(raw).hexdigest()!=reference['sha256']:
        raise ValidationError('host_profile_changed')
    plan=parse_json(raw)
    if set(plan)!={'profile','layers','behavior','owners','owner_evidence','artifact','device_evidence','tdd'}:
        raise ValidationError('host_profile_shape')
    import delivery_contract
    if delivery_contract.validate_profile(plan['profile']):raise ValidationError('delivery_profile_invalid')
    return plan,path.parent


def assess_profile(plan_source, *, root, repository, commands, observations, changed_paths):
    if plan_source is None:return {'state':'host_commands','profile_digest':None}
    from validation_evidence import read_bound, parse_json, period, instant, EvidenceError
    plan,directory=plan_source
    try:
        profile=plan['profile']; layers=plan['layers']
        if profile['repository']!=repository:raise ValidationError('profile_repository_mismatch')
        required_names={'static','unit','feature','e2e','build','security','full','device'}
        if type(layers) is not dict or set(layers)!=required_names:raise ValidationError('layer_inventory_missing')
        if plan['behavior'] not in ('behavioral','docs_only'):raise ValidationError('behavior_unknown')
        if plan['behavior']=='docs_only' and any(Path(p).suffix not in ('.md','.rst','.txt') or Path(p).name in {'AGENTS.md','SKILL.md','CLAUDE.md'} or p.startswith(('organization/roles/','organization/runtime/')) for p in changed_paths):
            raise ValidationError('runtime_change_is_not_docs_only')
        for name,layer in layers.items():
            if (type(layer) is not dict or set(layer)!={'required','reason','commands'} or type(layer['required']) is not bool
                    or type(layer['reason']) is not str or not layer['reason'].strip() or type(layer['commands']) is not list
                    or any(type(i) is not int or not 0<=i<len(commands) for i in layer['commands'])
                    or len(set(layer['commands']))!=len(layer['commands'])):
                raise ValidationError('layer_contract_invalid')
            if profile['layers'].get(name,{}).get('required') and not layer['required']:
                raise ValidationError('profile_required_layer_disabled')
            if layer['required'] and name!='device' and (not layer['commands'] or any(not observations[i]['passed'] for i in layer['commands'])):
                raise ValidationError('required_layer_not_passed')
            if layer['required'] and name in {'unit','feature','e2e'} and any(observations[i].get('kind')!='tests' or type(observations[i].get('executed')) is not int or observations[i]['executed']<=0 for i in layer['commands']):
                raise ValidationError('required_test_layer_count_missing')
            if layer['required'] and name=='full' and any(observations[i].get('selection') or observations[i].get('count_method')!='suite_results' for i in layer['commands']):
                raise ValidationError('shard_is_not_full_validation')
        owners=plan['owners']
        if (type(owners) is not dict or set(owners)!={'ci','cd','rollback'}
                or any(type(x) is not str or not x.strip() or 'example' in x.lower() for x in owners.values())):
            raise ValidationError('project_owner_invalid')
        actual=parse_json(read_bound(directory,plan['owner_evidence']))
        expected={'repository':repository,'profile_digest':digest(profile),'owners':owners,
                  'targets':profile['release']['targets'],'rollback_prerequisites':profile['release']['rollback_prerequisites']}
        if actual!=expected:raise ValidationError('project_owner_evidence_mismatch')
        if profile['project_type']=='mobile' and plan['behavior']=='behavioral' or layers['device']['required']:
            artifact=read_bound(root,plan['artifact'])
            device=parse_json(read_bound(directory,plan['device_evidence']))
            fields={'surface','model','os','operation','start','end','status','artifact_sha256','image'}
            if (set(device)!=fields or device['surface']!='physical' or device['status']!='passed' or not period(device)
                    or any(type(device[k]) is not str or not device[k].strip() for k in ('model','os','operation'))
                    or device['artifact_sha256']!=hashlib.sha256(artifact).hexdigest()):
                raise ValidationError('physical_device_evidence_invalid')
            read_bound(directory,device['image'])
        if plan['behavior']=='behavioral':
            tdd=plan['tdd']
            if type(tdd) is not dict or set(tdd)!={'red','green','refactor','refactor_evidence'}:
                raise ValidationError('test_first_evidence_missing')
            red=parse_json(read_bound(directory,tdd['red'])); green=parse_json(read_bound(directory,tdd['green']))
            fields={'repository','source_digest','tests','command','start','end','exit','executed','status'}
            if any(set(event)!=fields or event['repository']!=repository or not period(event)
                   or type(event['exit']) is not int or type(event['executed']) is not int or event['executed']<=0 for event in (red,green)):
                raise ValidationError('test_first_evidence_invalid')
            if (red['exit']==0 or red['status']!='failed' or green['exit']!=0 or green['status']!='passed'
                    or red['source_digest']==green['source_digest'] or green['source_digest']!=source_digest(root)
                    or red['command']!=green['command'] or instant(red['end'])>instant(green['start'])
                    or red['tests']!=green['tests'] or type(green['tests']) is not list or not green['tests']):
                raise ValidationError('test_first_order_or_identity_mismatch')
            for reference in green['tests']:read_bound(root,reference)
            if tdd['refactor']=='performed':
                refactor=parse_json(read_bound(directory,tdd['refactor_evidence']))
                if refactor!=green:raise ValidationError('refactor_current_validation_missing')
            elif tdd['refactor']!='none' or tdd['refactor_evidence'] is not None:
                raise ValidationError('refactor_disposition_invalid')
        return {'state':'passed','profile_digest':digest(plan),'layers':{name:'passed' if layer['required'] else 'host_not_applicable' for name,layer in layers.items()}}
    except (EvidenceError,KeyError,TypeError,ValueError,OSError,IndexError) as exc:
        raise ValidationError(str(exc) if type(exc) is ValidationError else 'host_profile_evidence_invalid') from exc


def verify_standard_receipt(state_root, run):
    """Legacy code-change QA also needs host measurements, not a provider pass label."""
    import scoped_worker_executor as scoped
    import host_publication_adapter as publication
    from validation_evidence import read_bytes, parse_json
    try:
        scoped.load_completed_review_context(state_root,run)
        history=[r for r in run['step_history'] if r.get('step_id')=='implement' and r.get('status')=='completed' and r.get('execution_id')]
        row=history[-1]
        execution=scoped._read_state_json(scoped._state_artifact_path(state_root,'executions',row['execution_id']+'.json'),reason='execution_artifact_invalid')
        capability=scoped._load_canonical_capability(state_root,execution['capability_id'])
        root=Path(capability['worktree']['worktree_path'])
        identity=publication.snapshot(root,scoped._changed_paths(root))
        relative='reports/'+run['run_id']+'/host-validation.json'
        raw=read_bytes(state_root,relative); receipt=parse_json(raw)
        validate_receipt(receipt,identity={**identity,'execution_id':row['execution_id']})
        if receipt['source_digest']!=source_digest(root):raise ValidationError('validation_source_changed')
        return {'path':relative,'digest':digest(raw)}
    except (OSError,ValueError,KeyError,IndexError,scoped.ScopedWorkerError,publication.PublicationError) as exc:
        raise ValidationError('standard_host_validation_missing_or_stale') from exc
