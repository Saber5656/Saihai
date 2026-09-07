"""Bounded host startup/recovery diagnostics, distinct from execution authority."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import directory_paths
import run_store
import vault_task_records


def _git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE, timeout=10)


def _read(root: Path, relative: Path) -> bytes:
    handles = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); handles.append(fd)
        for name in relative.parts[:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); handles.append(fd)
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); handles.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 256 * 1024:
            raise ValueError('role_file_unsafe')
        data = os.read(fd, 256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError('role_file_too_large')
        return data
    finally:
        for fd in reversed(handles): os.close(fd)


def _managed_launch(checkout: Path, primary: Path, workspace: str, profile: str, principal: str, surface: str) -> dict:
    import frontdoor_orchestrator as host
    selected_surface=host.resolve_surface_identity(surface,expected_checkout=checkout,launch_session_present=True)
    if selected_surface.get('assurance_profile_id') != profile:
        raise ValueError('managed_surface_profile_mismatch')
    identity = host.resolve_checkout_identity(workspace_id=workspace, managed_primary=primary, checkout_root=checkout)
    verified = host.HostLaunchSessionVerifier().verify_parent_session(subject_pid=os.getppid(), profile_id=profile,
        principal_id=principal, workspace_id=workspace, checkout_identity=identity)
    if verified.get('session_kind') != 'standard':
        raise ValueError('commissioning_session_is_not_ordinary_work')
    return {'status':'verified', 'session_id':verified['session_id'], 'profile_id':profile,
            'checkout_identity_digest':identity.get('identity_digest'), 'native_digest':verified['native_digest'],
            'profile_digest':verified['profile_digest'], 'valid_until':verified['valid_until'], 'session_kind':verified['session_kind']}


def inspect_startup(*, checkout: Path, expected_commit: str, expected_origin: str, roles: list[str],
                    execution_profile: str, surface: str, task_id: str, audit_directory: Path,
                    profile_id: str = '', principal_id: str = '', workspace_id: str = '',
                    bootstrap_brief: dict | None = None, project: str = 'Saihai-Bootstrap',
                    _primary: Path | None = None, _launch_verifier=None) -> dict:
    """Arguments are host inputs; test injection is never exposed by the CLI."""
    if execution_profile not in {'trusted_local_v1','legacy_managed'} or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',surface):
        raise ValueError('startup_profile_or_surface_invalid')
    if not roles or len(roles) > 16 or any(not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',r) for r in roles):
        raise ValueError('startup_role_scope_invalid')
    if not re.fullmatch(r'[0-9a-f]{40}',expected_commit) or not expected_origin or len(expected_origin) > 2048:
        raise ValueError('startup_trusted_source_identity_required')
    primary = _primary or Path.home() / 'dev/Saihai'
    result = dict(schema_version=1, operation='startup_recovery', execution_profile=execution_profile,
                  surface=surface, task_id=task_id, decision='blocked', formal_harness_assurance=False,
                  diagnostics={}, actions=[], ordinary_work_allowed=False)
    reasons = []
    env = {}
    vault = None
    try:
        catalog = directory_paths.load_environment(checkout_root=primary, environ=env, require_catalog=True)
        if catalog['status'] != 'loaded': raise ValueError('catalog_not_loaded')
        result['diagnostics']['catalog'] = {'status':'valid','source':'primary_directory_catalog','empty_environment':True}
        vault = Path(env['AGENTS_VAULT_ROOT'])
        # Availability observations do not prove a separate sandbox or launch grant.
        metadata = vault.stat()
        accessible = not vault.is_symlink() and stat.S_ISDIR(metadata.st_mode) and os.access(vault,os.R_OK|os.W_OK|os.X_OK)
        result['diagnostics']['vault'] = {'status':'available' if accessible else 'agents_vault_not_read_write',
            'path':str(vault), 'host_access':accessible, 'sandbox_permission':'not_proven_by_catalog', 'substitute_created':False}
        if not accessible: reasons.append('agents_vault_not_read_write')
    except (OSError, ValueError, KeyError, directory_paths.EnvError) as exc:
        if isinstance(exc,directory_paths.EnvError) and ('agents_vault' in str(exc) or 'AGENTS_VAULT_ROOT' in str(exc)) and env.get('AGENTS_VAULT_ROOT'):
            vault=Path(env['AGENTS_VAULT_ROOT'])
            result['diagnostics']['catalog']={'status':'parsed_availability_validation_failed','empty_environment':True}
        reason = 'directory_catalog_invalid' if vault is None else 'agents_vault_not_read_write'
        reasons.append(reason)
        result['diagnostics'][('catalog' if vault is None else 'vault')] = {'status':reason,'error_kind':type(exc).__name__,
            'host_access':False,'sandbox_permission':'not_proven_by_catalog','substitute_created':False}
        result['actions'].append('Human must recover the existing catalog/Vault; no substitute or automatic path change.')
    try:
        actual = _git(checkout,'rev-parse','HEAD').decode().strip()
        origin = _git(checkout,'remote','get-url','origin').decode().strip()
        tree = _git(checkout,'rev-parse','HEAD^{tree}').decode().strip()
        if actual != expected_commit or origin != expected_origin: raise ValueError('checkout_identity_mismatch')
        parsed_origin=urlsplit(origin)
        safe_origin=urlunsplit((parsed_origin.scheme,parsed_origin.hostname or '',parsed_origin.path,'','')) if parsed_origin.scheme and parsed_origin.username else origin
        worktree_status=_git(checkout,'status','--porcelain=v1','--untracked-files=normal')
        result['diagnostics']['checkout'] = {'status':'verified','commit':actual,'tree':tree,'origin':safe_origin,'origin_sha256':hashlib.sha256(origin.encode()).hexdigest(),
            'worktree_dirty':bool(worktree_status),'worktree_status_sha256':hashlib.sha256(worktree_status).hexdigest()}
        role_rows=[]
        for role in sorted(set(roles)):
            relative=Path('organization/roles')/role/'skill.md'
            observed=_read(checkout,relative)
            committed=_git(checkout,'show',actual+':'+relative.as_posix())
            if observed != committed: raise ValueError('role_definition_digest_mismatch')
            role_rows.append({'role':role,'path':relative.as_posix(),'sha256':hashlib.sha256(observed).hexdigest()})
        result['diagnostics']['roles']={'status':'verified','definitions':role_rows}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reasons.append('checkout_or_role_identity_invalid')
        result['diagnostics']['roles']={'status':'blocked','error_kind':type(exc).__name__}
        result['actions'].append('Recover roles only from the separately approved source and immutable commit; do not substitute a generic role.')
    if execution_profile == 'legacy_managed':
        try:
            if not all((profile_id,principal_id,workspace_id)): raise ValueError('managed_launch_identity_missing')
            verifier = _launch_verifier or _managed_launch
            launch=verifier(checkout,primary,workspace_id,profile_id,principal_id,surface)
            if launch.get('status') != 'verified': raise ValueError('managed_launch_unverified')
            result['diagnostics']['launch']=launch
            result['formal_harness_assurance']=not reasons
        except (OSError, ValueError, RuntimeError) as exc:
            reasons.append('managed_launch_uncommissioned_or_invalid')
            result['diagnostics']['launch']={'status':'diagnostic_only','error_kind':type(exc).__name__}
    else:
        result['diagnostics']['launch']={'status':'explicit_trusted_local_profile','formal_assurance':False,
            'execution_authority':'separate host task authorization remains required'}
    # Save pre-registration observations privately even when the Vault is unavailable.
    result['reasons']=list(dict.fromkeys(reasons))
    audit_directory=Path(audit_directory).expanduser()
    audit_path=audit_directory / ('startup-'+str(time.time_ns())+'.json')
    run_store.atomic_write_json(audit_path,result)
    result['audit_path']=str(audit_path)
    result['audit_digest']=vault_task_records.digest(audit_path.read_bytes())
    if vault is not None and not any(r in reasons for r in ('agents_vault_not_read_write','directory_catalog_invalid','checkout_or_role_identity_invalid')):
        try:
            if bootstrap_brief is not None:
                brief=dict(bootstrap_brief)
                if set(brief) != {'objective','scope','acceptance_criteria'} or any(not isinstance(v,str) for v in brief.values()): raise ValueError('bootstrap_brief_invalid')
                brief['scope']=brief['scope']+'; Prior startup diagnostic evidence: '+str(audit_path)+' '+result['audit_digest']
                binding=vault_task_records.scaffold(vault,task_id,project=project,brief=brief)
                result['bootstrap_registration']={'status':'registered','binding':binding,'retroactive_audit_reference':str(audit_path)}
            else:
                binding=vault_task_records.resolve_task(vault,task_id)
            result['task_binding']=binding
        except (ValueError, OSError) as exc:
            reasons.append('startup_task_registration_required')
            result['actions'].append('Register the bootstrap task with an explicitly approved typed brief after the canonical Vault is recovered.')
            result['diagnostics']['task']={'status':'blocked','error_kind':type(exc).__name__}
    result['reasons']=list(dict.fromkeys(reasons))
    result['fresh_bootstrap_required']=bool(set(reasons)&{'directory_catalog_invalid','agents_vault_not_read_write','startup_task_registration_required'})
    result['human_confirmation_required']=result['fresh_bootstrap_required']
    result['formal_harness_assurance']=result['formal_harness_assurance'] and not reasons
    result['ordinary_work_allowed']=not reasons
    result['decision']='ok' if not reasons else 'blocked'
    result['status']='ready_for_separate_task_authorization' if not reasons else 'diagnostic_only'
    # Final receipt is separate; the reference embedded during registration stays immutable.
    final_path=audit_path.with_name(audit_path.stem+'-result.json')
    run_store.atomic_write_json(final_path,result)
    result['result_audit_path']=str(final_path)
    return result


def cli(argv: list[str]) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout',default=str(REPO_ROOT));parser.add_argument('--expected-commit',required=True)
    parser.add_argument('--expected-origin',required=True);parser.add_argument('--role',action='append',required=True)
    parser.add_argument('--execution-profile',choices=['trusted_local_v1','legacy_managed'],required=True)
    parser.add_argument('--surface',required=True);parser.add_argument('--task-id',required=True)
    parser.add_argument('--profile-id',default='');parser.add_argument('--principal-id',default='');parser.add_argument('--workspace-id',default='')
    parser.add_argument('--bootstrap-brief',default='');parser.add_argument('--project',default='Saihai-Bootstrap')
    args=parser.parse_args(argv)
    try:
        import host_state_root
        result=inspect_startup(checkout=Path(args.checkout),expected_commit=args.expected_commit,expected_origin=args.expected_origin,
            roles=args.role,execution_profile=args.execution_profile,surface=args.surface,task_id=args.task_id,
            audit_directory=host_state_root.DEFAULT_STATE_ROOT/'startup-recovery',profile_id=args.profile_id,
            principal_id=args.principal_id,workspace_id=args.workspace_id,
            bootstrap_brief=json.loads(args.bootstrap_brief) if args.bootstrap_brief else None,project=args.project)
    except (OSError, ValueError, run_store.RunStoreError) as exc:
        result={'decision':'blocked','reason':'startup_diagnostic_unavailable','error_kind':type(exc).__name__}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['decision']=='ok' else 2


if __name__=='__main__':raise SystemExit(cli(sys.argv[1:]))
