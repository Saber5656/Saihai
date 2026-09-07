"""Host-owned installation readback and conservative post-merge primary sync.

Plans constrain existing authority, never grant it. No installer, role/model
selection, credential provisioning or process-reload claim is implemented here.
"""
from __future__ import annotations
import dataclasses
import os
from pathlib import Path
import re
import subprocess
import sys

import effective_bundle as bundle
import host_publication_adapter as publication
import run_lock
import run_store


class InstallationError(RuntimeError):
    pass


def catalog() -> dict:
    primary = Path.home() / 'dev/Saihai'
    sys.path.insert(0, str(primary))
    import directory_paths
    env = {}
    try:
        result = directory_paths.load_environment(checkout_root=primary, environ=env, require_catalog=True)
        if result['status'] != 'loaded':
            raise InstallationError('catalog_unavailable')
        directory_paths.validate_vault(env)
    except (OSError, ValueError, RuntimeError) as exc:
        raise InstallationError('catalog_unavailable') from exc
    return env


def _path(auth, state_root):
    run_store.validate_artifact_id(auth.publication.execution_id, 'execution_id')
    root = Path(state_root)
    worker = Path(auth.publication.worktree).resolve()
    if not root.is_absolute() or root.resolve() != root or root == worker or worker in root.parents:
        raise InstallationError('host_state_required')
    return root / 'host-installation' / (auth.publication.execution_id + '.json')


def configure(auth, state_root: Path, plan: dict) -> dict:
    """Explicit host API; immutable for an execution ID, before worker claim.

    plan is not read from a request or worker report. Roots use catalog keys;
    surface roots are fixed existing discovery directories only.
    """
    path = _path(auth, state_root)
    claim = Path(state_root) / 'trusted-local' / auth.publication.execution_id / 'claim.json'
    if run_store.private_artifact_exists(claim):
        raise InstallationError('installation_plan_after_claim')
    expected = {'version', 'surface', 'roots', 'members', 'policy_snapshots',
                'approved_symlinks', 'source_identity', 'expected_content_digest', 'sync_catalog_key'}
    if not isinstance(plan, dict) or set(plan) != expected or plan['version'] != 1:
        raise InstallationError('installation_plan_invalid')
    if plan['sync_catalog_key'] not in {'SAIHAI_ROOT', 'DOTFILES_ROOT', 'SKILLS_REPO_ROOT'}:
        raise InstallationError('sync_catalog_key_invalid')
    record = {'authorization_digest': publication.digest(dataclasses.asdict(auth)), 'plan': plan,
              'primary_identity': publication.digest(str(Path(catalog()[plan['sync_catalog_key']]).resolve()))}
    # Read back before freezing; no drift can be acknowledged by configuration.
    _observe(plan)
    run_store.ensure_private_directory(path.parent)
    if run_store.private_artifact_exists(path):
        if run_store.read_json(path) != record:
            raise InstallationError('installation_plan_changed')
        return record
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    import json
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream, sort_keys=True)
    return record


def _observe(plan):
    env = catalog()
    if plan['surface'] not in {'codex-app', 'codex-cli', 'claude-cli'}:
        raise InstallationError('surface_unknown')
    values = dict(env, CODEX_SURFACE=str(Path.home() / '.codex'), CLAUDE_SURFACE=str(Path.home() / '.claude'))
    if not isinstance(plan['roots'], dict) or any(k not in values for k in plan['roots'].values()):
        raise InstallationError('catalog_root_unknown')
    roots = {key: values[value] for key, value in plan['roots'].items()}
    try:
        observation = bundle.observe_bundle(catalog_roots=roots,
            member_spec={'members': plan['members'], 'policy_snapshots': plan['policy_snapshots']},
            target_surface=plan['surface'], expected_source_identity=plan['source_identity'],
            approved_symlinks=plan['approved_symlinks'])
    except (bundle.ObservationError, OSError, ValueError, TypeError, KeyError) as exc:
        raise InstallationError('installation_readback_unavailable') from exc
    if observation['missing_categories']:
        raise InstallationError('installation_members_incomplete')
    if any(m['relation'] not in {'same_file', 'matching_copy'} for m in observation['members']):
        raise InstallationError('installed_artifact_drift')
    if any(p['status'] != 'match' for p in observation['policy_snapshots']):
        raise InstallationError('stale_policy_snapshot')
    if observation['source_identity']['status'] != 'match':
        raise InstallationError('installation_source_changed')
    if observation['content_digest'] != plan['expected_content_digest']:
        raise InstallationError('installation_digest_changed')
    return observation


def verify(auth, state_root: Path) -> dict:
    path = _path(auth, state_root)
    if not run_store.private_artifact_exists(path):
        claim = Path(state_root) / 'trusted-local' / auth.publication.execution_id / 'claim.json'
        if run_store.private_artifact_exists(claim) and run_store.read_json(claim).get('installation_required'):
            raise InstallationError('installation_plan_missing')
        return {'status': 'legacy_not_configured', 'active_runtime': 'not_proven'}
    record = run_store.read_json(path)
    if record.get('authorization_digest') != publication.digest(dataclasses.asdict(auth)):
        raise InstallationError('installation_authority_changed')
    observed = _observe(record['plan'])
    receipt = {'status': 'installed_bytes_verified', 'authorization_digest': record['authorization_digest'],
               'plan_digest': publication.digest(record['plan']), 'content_digest': observed['content_digest'],
               'surface': observed['target_surface'], 'active_runtime': 'not_proven',
               'observation': observed}
    run_store.atomic_write_json(path.with_suffix('.readback.json'), receipt)
    return receipt


def inherit(parent, child, state_root: Path) -> None:
    """Host repair continuation retains the frozen artifact constraints."""
    if verify(parent, state_root)['status'] == 'legacy_not_configured':
        return
    record = run_store.read_json(_path(parent, state_root))
    configure(child, state_root, record['plan'])


def _git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    try:
        p = subprocess.run(['git', '-c', 'core.fsmonitor=false', *args], cwd=root,
                           env=env, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallationError('sync_transport_unavailable_or_uncertain') from exc
    if p.returncode:
        raise InstallationError('sync_git_failed')
    return p.stdout.decode().strip()


def sync_primary(auth, state_root: Path, merged: dict) -> dict:
    """Only after integrated checks; never stash/reset/checkout a primary."""
    path = _path(auth, state_root)
    if not run_store.private_artifact_exists(path):
        verify(auth, state_root)  # A lost required plan is never legacy.
        return {'status': 'legacy_not_configured', 'dependent_base': None}
    record = run_store.read_json(path)
    if record.get('authorization_digest') != publication.digest(dataclasses.asdict(auth)):
        raise InstallationError('installation_authority_changed')
    host = auth.publication
    sha = merged.get('merge_commit', '')
    checks = merged.get('integrated_checks', {})
    if (host.destination != 'main' or not re.fullmatch(r'[a-f0-9]{40}', sha)
            or merged.get('status') != 'complete' or not checks
            or any(v != 'success' for v in checks.values())):
        raise InstallationError('sync_integrated_validation_required')
    root = Path(catalog()[record['plan']['sync_catalog_key']])
    if publication.digest(str(root.resolve())) != record.get('primary_identity'):
        raise InstallationError('sync_catalog_primary_changed')
    if not root.is_absolute() or root.resolve() != root:
        raise InstallationError('sync_primary_identity_invalid')
    actor = {'principal_type':'harness_runner','principal_id':'canonical-main-sync','authn_method':'local_cli'}
    with run_lock.hold_global_lock(Path(state_root) / 'primary-sync-lock', operation='canonical_main_sync', run_id=host.run_id, principal=actor):
        if _git(root, 'rev-parse', '--show-toplevel') != str(root):
            raise InstallationError('sync_primary_identity_invalid')
        if _git(root, 'branch', '--show-current') != 'main':
            raise InstallationError('sync_primary_not_main')
        if _git(root, 'status', '--porcelain', '--untracked-files=all'):
            raise InstallationError('sync_primary_dirty')
        if _git(root, 'rev-parse', '--abbrev-ref', '@{upstream}') != 'origin/main':
            raise InstallationError('sync_upstream_mismatch')
        if not publication._remote_matches(_git(root, 'remote', 'get-url', 'origin'), host.repository):
            raise InstallationError('sync_repository_mismatch')
        entries = _git(root, 'worktree', 'list', '--porcelain').split('\n\n')
        main = [e for e in entries if 'branch refs/heads/main' in e.splitlines()]
        if len(main) != 1 or main[0].splitlines()[0] != 'worktree ' + str(root):
            raise InstallationError('sync_primary_shared')
        old = _git(root, 'rev-parse', 'HEAD')
        _git(root, 'fetch', '--no-tags', 'origin', 'refs/heads/main:refs/remotes/origin/main')
        tip = _git(root, 'rev-parse', 'refs/remotes/origin/main')
        # A newer tip needs its own integrated validation, not the merge's CI.
        if tip != sha:
            raise InstallationError('sync_remote_advanced_revalidation_required')
        try:
            _git(root, 'merge-base', '--is-ancestor', old, tip)
        except InstallationError as exc:
            raise InstallationError('sync_primary_diverged') from exc
        if _git(root, 'status', '--porcelain', '--untracked-files=all') or _git(root, 'rev-parse', 'HEAD') != old:
            raise InstallationError('sync_primary_changed')
        _git(root, 'merge', '--ff-only', '--no-edit', tip)
        if _git(root, 'rev-parse', 'HEAD') != tip or _git(root, 'status', '--porcelain', '--untracked-files=all'):
            raise InstallationError('sync_readback_mismatch')
        remote = _git(root, 'ls-remote', 'origin', 'refs/heads/main').split()
        if not remote or remote[0] != tip:
            raise InstallationError('sync_remote_advanced_revalidation_required')
        receipt = {'status':'synced','merge_commit':sha,'dependent_base':tip,
                   'repository':host.repository,'branch':'main','authorization_digest':record['authorization_digest']}
        run_store.atomic_write_json(path.with_suffix('.sync.json'), receipt)
        return receipt
