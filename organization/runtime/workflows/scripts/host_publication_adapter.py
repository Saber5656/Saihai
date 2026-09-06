"""Usage-first publication, invoked by the trusted host (never a worker report).

No credentials are provisioned. Existing git/gh authentication and native GitHub
protection are used. This is deliberately separate from the legacy atomic gate
broker; it does not claim that broker's all-gate atomicity or worker isolation.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Any


class PublicationError(RuntimeError):
    pass


def digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return 'sha256:' + hashlib.sha256(data).hexdigest()


@dataclasses.dataclass(frozen=True)
class HostAuthorization:
    """Trusted host input. Do not deserialize this from worker output/stdout."""
    task_id: str
    request_id: str
    run_id: str
    execution_id: str
    repository: str
    worktree: str
    branch: str
    head: str
    base: str
    allowed_paths: tuple[str, ...]
    required_checks: tuple[str, ...]
    policy_digest: str
    authority_evidence_ref: str
    destination: str = 'main'
    risk_kind: str = 'ordinary'
    scope_review_receipt: str | None = None

    @property
    def scope_digest(self) -> str:
        return digest(dataclasses.asdict(self))


class Commands:
    def run(self, args: list[str], *, cwd: Path, env: dict | None = None) -> bytes:
        try:
            result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublicationError('command_unavailable_or_uncertain') from exc
        if result.returncode and not (args[:3] == ['gh', 'pr', 'checks'] and result.returncode == 8):
            # Do not persist potentially credential-bearing stderr/remote URLs.
            raise PublicationError('command_failed:' + args[0])
        return result.stdout


def _relative(path: str) -> bool:
    return isinstance(path, str) and bool(path) and not path.startswith('-') and '\\' not in path and (
        not PurePosixPath(path).is_absolute() and all(p not in {'', '.', '..', '.git'} for p in path.split('/')))


def _git(cmd: Commands, root: Path, *args: str, env: dict | None = None) -> bytes:
    return cmd.run(['git', *args], cwd=root, env=env)


def _json(cmd: Commands, root: Path, *args: str) -> Any:
    try:
        return json.loads(cmd.run(['gh', *args], cwd=root))
    except (ValueError, TypeError) as exc:
        raise PublicationError('github_response_invalid') from exc


def snapshot(root: Path, paths: list[str], *, commands: Commands | None = None) -> dict:
    """Capture staged-tree identity without touching the caller's real index."""
    cmd = commands or Commands()
    if not paths or len(paths) != len(set(paths)) or not all(_relative(p) for p in paths):
        raise PublicationError('changed_paths_invalid')
    for p in paths:
        candidate = root / p
        if candidate.is_symlink() or any((root / part).is_symlink() for part in PurePosixPath(p).parents if str(part) != '.'):
            raise PublicationError('symlink_change_unsupported')
    if _git(cmd, root, 'diff', '--cached', '--name-only').strip():
        raise PublicationError('preexisting_staged_changes')
    dirty = set(_git(cmd, root, 'diff', '--name-only', '--no-renames', 'HEAD', '-z').decode().split('\0')) - {''}
    dirty |= set(_git(cmd, root, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''}
    if dirty != set(paths):
        raise PublicationError('unrelated_or_missing_changes')
    with tempfile.TemporaryDirectory(prefix='saihai-publication-index-') as raw:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(raw) / 'index'))
        _git(cmd, root, 'read-tree', 'HEAD', env=env)
        _git(cmd, root, 'add', '--', *paths, env=env)
        tree = _git(cmd, root, 'write-tree', env=env).decode().strip()
        patch = _git(cmd, root, 'diff', '--cached', '--binary', '--full-index', '--no-ext-diff', '--no-renames', 'HEAD', env=env)
    return {'tree': tree, 'diff_digest': digest(patch)}


def committed_snapshot(root: Path, base: str, *, commands: Commands | None = None) -> dict:
    """Host-only integration evidence after an actual main merge commit."""
    cmd = commands or Commands()
    if not re.fullmatch(r'[a-f0-9]{40}', base) or _git(cmd, root, 'status', '--porcelain').strip():
        raise PublicationError('integrated_worktree_not_clean')
    tree = _git(cmd, root, 'rev-parse', 'HEAD^{tree}').decode().strip()
    paths = _git(cmd, root, 'diff', '--name-only', '--no-renames', base, 'HEAD', '-z').decode().split('\0')
    patch = _git(cmd, root, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-renames', base, 'HEAD')
    return {'tree': tree, 'diff_digest': digest(patch), 'changed_paths': sorted(p for p in paths if p)}


def validate_report(report: dict, authorization: HostAuthorization) -> Path:
    if not isinstance(authorization, HostAuthorization):
        raise PublicationError('trusted_host_authorization_required')
    if report.get('version') != '1' or report.get('profile') != 'trusted_local_v1' or report.get('publication_allowed') is not False:
        raise PublicationError('worker_publication_forbidden')
    if authorization.destination != 'main' or not re.fullmatch(r'[^/\s]+/[^/\s]+', authorization.repository):
        raise PublicationError('destination_invalid')
    if not authorization.branch.startswith('codex/') or '..' in authorization.branch or authorization.branch.startswith('-'):
        raise PublicationError('branch_invalid')
    if authorization.risk_kind not in {'ordinary', 'permission_expansion', 'credentials', 'data_loss'}:
        raise PublicationError('risk_kind_invalid')
    if authorization.risk_kind != 'ordinary' and not authorization.scope_review_receipt:
        raise PublicationError('scope_review_required')
    if not authorization.required_checks or not re.fullmatch(r'sha256:[a-f0-9]{64}', authorization.policy_digest):
        raise PublicationError('required_check_policy_missing')
    for key in ('task_id', 'request_id', 'run_id', 'execution_id', 'repository', 'worktree', 'branch', 'head', 'base'):
        if not getattr(authorization, key) or report.get(key) != getattr(authorization, key):
            raise PublicationError('host_identity_mismatch:' + key)
    if any(not re.fullmatch(r'[a-f0-9]{40}', report.get(key, '')) for key in ('head', 'base', 'tree')):
        raise PublicationError('git_identity_invalid')
    if report.get('approved_scope_digest') != authorization.scope_digest or report.get('result') != 'completed':
        raise PublicationError('scope_or_result_invalid')
    paths = report.get('changed_paths')
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)) or not all(_relative(p) for p in paths):
        raise PublicationError('changed_paths_invalid')
    if any(not (a == '.' or _relative(a)) for a in authorization.allowed_paths) or any(
            not any(a == '.' or p == a or p.startswith(a + '/') for a in authorization.allowed_paths) for p in paths):
        raise PublicationError('change_outside_authorized_scope')
    execution = report.get('execution', {})
    process = Path(execution.get('process_evidence_path', ''))
    if (execution.get('actor_kind') != 'agent_under_explicit_user_task_authority'
            or execution.get('authority_evidence_ref') != authorization.authority_evidence_ref
            or not authorization.authority_evidence_ref or not process.is_absolute()
            or process.is_symlink() or not process.is_file()
            or digest(process.read_bytes()) != execution.get('process_evidence_digest')):
        raise PublicationError('host_process_evidence_invalid')
    try:
        process_receipt = json.loads(process.read_bytes())
    except ValueError as exc:
        raise PublicationError('host_process_evidence_invalid') from exc
    if process_receipt.get('execution_id') != report['execution_id'] or process_receipt.get('exit') != 0:
        raise PublicationError('host_process_identity_or_exit_invalid')
    validation = report.get('validation', {})
    evidence = Path(validation.get('evidence_path', ''))
    if validation.get('status') != 'passed' or not evidence.is_absolute() or evidence.is_symlink() or not evidence.is_file():
        raise PublicationError('host_validation_evidence_missing')
    raw = evidence.read_bytes()
    if digest(raw) != validation.get('evidence_digest'):
        raise PublicationError('host_validation_evidence_changed')
    try:
        receipt = json.loads(raw)
    except ValueError as exc:
        raise PublicationError('host_validation_evidence_invalid') from exc
    if receipt.get('status') != 'passed' or receipt.get('tree') != report['tree'] or receipt.get('diff_digest') != report.get('diff_digest') or receipt.get('execution_id') != report['execution_id']:
        raise PublicationError('host_validation_identity_mismatch')
    # Host caller owns the evidence location; a matching worker-authored file is
    # not authentication. This contract intentionally requires a trusted caller.
    root = Path(authorization.worktree)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise PublicationError('worktree_invalid')
    return root


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
        temp = Path(stream.name)
    os.replace(temp, path)


def _remote_matches(remote: str, repository: str) -> bool:
    return remote.strip() in {f'https://github.com/{repository}.git', f'https://github.com/{repository}',
                             f'git@github.com:{repository}.git'}


def required_inventory(cmd: Commands, root: Path, auth: HostAuthorization) -> set[str]:
    """Read active native rules; inability to enumerate is not an empty policy."""
    rules = _json(cmd, root, 'api', f'repos/{auth.repository}/rules/branches/{auth.destination}')
    if not isinstance(rules, list):
        raise PublicationError('native_rules_unavailable')
    required = set(auth.required_checks)
    for rule in rules:
        if rule.get('type') == 'required_status_checks':
            contexts = rule.get('parameters', {}).get('required_status_checks')
            if not isinstance(contexts, list):
                raise PublicationError('native_check_inventory_invalid')
            for row in contexts:
                if not isinstance(row.get('context'), str) or not row['context']:
                    raise PublicationError('native_check_inventory_invalid')
                required.add(row['context'])
    return required


def publish(report: dict, authorization: HostAuthorization, state_root: Path,
            *, commands: Commands | None = None, integrated_parent: str | None = None) -> dict:
    """One bounded poll per call; host scheduler re-enters while CI is pending.

    Only trusted host code may call this function. No CLI takes authorization
    from a worker, no shell commands/credentials are accepted in the report.
    Mutation failures remain uncertain and must be reconciled, not blindly retried.
    """
    cmd = commands or Commands()
    root = validate_report(report, authorization)
    key = digest({'report': report, 'authorization': dataclasses.asdict(authorization)})[7:]
    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    # Lock only this execution. The host serializes distinct publications to a repo.
    import fcntl
    with (state_root / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state_root / (key + '.json')
        state = json.loads(path.read_text()) if path.exists() else {'status': 'new', 'binding': key}
        if state.get('binding') != key:
            raise PublicationError('publication_state_binding_mismatch')
        if state['status'] == 'merged':
            return state
        if _git(cmd, root, 'branch', '--show-current').decode().strip() != authorization.branch:
            raise PublicationError('branch_changed')
        if not _remote_matches(_git(cmd, root, 'remote', 'get-url', 'origin').decode(), authorization.repository):
            raise PublicationError('remote_changed')
        head = _git(cmd, root, 'rev-parse', 'HEAD').decode().strip()
        if state['status'] == 'commit_uncertain':
            if (head != authorization.head
                    and _git(cmd, root, 'rev-parse', 'HEAD^').decode().strip() == authorization.head
                    and _git(cmd, root, 'rev-parse', 'HEAD^{tree}').decode().strip() == report['tree']):
                state.update(status='committed', head=head); _save(path, state)
            else:
                return state
        if state['status'] == 'push_uncertain':
            remote = _git(cmd, root, 'ls-remote', 'origin', 'refs/heads/' + authorization.branch).decode().split()
            if remote and remote[0] == state['head']:
                state['status'] = 'pushed'; _save(path, state)
            else:
                return state
        if state['status'] == 'pr_uncertain':
            matches = _json(cmd, root, 'pr', 'list', '--repo', authorization.repository,
                '--head', authorization.branch, '--base', authorization.destination, '--state', 'open', '--json', 'number,headRefOid')
            if len(matches) == 1 and matches[0]['headRefOid'] == state['head']:
                state.update(status='ci_pending', pr=matches[0]['number']); _save(path, state)
            else:
                return state
        if state['status'] == 'merge_uncertain':
            observed = _json(cmd, root, 'api', f'repos/{authorization.repository}/pulls/{state["pr"]}')
            if (observed.get('merged') and observed['head']['sha'] == state['head']
                    and observed['base']['ref'] == authorization.destination
                    and re.fullmatch(r'[a-f0-9]{40}', observed.get('merge_commit_sha', ''))):
                state.update(status='merged', merge_commit=observed['merge_commit_sha']); _save(path, state)
            return state
        if state['status'] == 'new' and integrated_parent is not None:
            # This argument is supplied by trusted host integration code, never report data.
            parents = _git(cmd, root, 'show', '-s', '--format=%P', 'HEAD').decode().split()
            if head != authorization.head or integrated_parent not in parents or authorization.base not in parents:
                raise PublicationError('host_integration_parent_mismatch')
            actual = committed_snapshot(root, authorization.base, commands=cmd)
            if any(actual[k] != report[k] for k in actual):
                raise PublicationError('host_integrated_evidence_changed')
            state.update(status='committed', head=head)
            _save(path, state)
        if state['status'] == 'new':
            if head != authorization.head:
                raise PublicationError('head_changed')
            actual = snapshot(root, report['changed_paths'], commands=cmd)
            if any(actual[k] != report[k] for k in actual):
                raise PublicationError('validated_tree_changed')
            state.update(status='commit_uncertain')
            _save(path, state)
            _git(cmd, root, 'add', '--', *report['changed_paths'])
            staged_tree = _git(cmd, root, 'write-tree').decode().strip()
            if staged_tree != report['tree']:
                raise PublicationError('staged_tree_changed')
            _git(cmd, root, 'commit', '-m', f'[{authorization.task_id}] Apply validated task changes')
            head = _git(cmd, root, 'rev-parse', 'HEAD').decode().strip()
            if _git(cmd, root, 'rev-parse', 'HEAD^{tree}').decode().strip() != report['tree']:
                raise PublicationError('committed_tree_changed')
            state.update(status='committed', head=head)
            _save(path, state)
        if head != state['head'] or _git(cmd, root, 'status', '--porcelain').strip():
            raise PublicationError('publication_worktree_changed')
        if state['status'] == 'committed':
            state['status'] = 'push_uncertain'; _save(path, state)
            _git(cmd, root, 'push', '--set-upstream', 'origin', authorization.branch)
            state['status'] = 'pushed'; _save(path, state)
        if state['status'] == 'pushed':
            prs = _json(cmd, root, 'pr', 'list', '--repo', authorization.repository, '--head', authorization.branch,
                        '--base', authorization.destination, '--state', 'open', '--json', 'number,headRefOid')
            if not prs:
                state['status'] = 'pr_uncertain'; _save(path, state)
                cmd.run(['gh', 'pr', 'create', '--repo', authorization.repository, '--base', authorization.destination,
                         '--head', authorization.branch, '--title', f'[{authorization.task_id}] Validated task changes',
                         '--body', f'Host publication for task {authorization.task_id}. Validation tree: {report["tree"]}.'], cwd=root)
                prs = _json(cmd, root, 'pr', 'list', '--repo', authorization.repository, '--head', authorization.branch,
                            '--base', authorization.destination, '--state', 'open', '--json', 'number,headRefOid')
            if len(prs) != 1 or prs[0]['headRefOid'] != head:
                raise PublicationError('pull_request_identity_mismatch')
            state.update(status='ci_pending', pr=prs[0]['number']); _save(path, state)
        pr = _json(cmd, root, 'api', f'repos/{authorization.repository}/pulls/{state["pr"]}')
        if (pr['head']['sha'] != head or pr['head']['repo']['full_name'].lower() != authorization.repository.lower()
                or pr['base']['ref'] != authorization.destination or pr['base']['repo']['full_name'].lower() != authorization.repository.lower()):
            raise PublicationError('pull_request_identity_changed')
        if pr.get('merged'):
            state.update(status='merged', merge_commit=pr['merge_commit_sha']); _save(path, state); return state
        if pr.get('mergeable') is False:
            state.update(status='conflict_pending'); _save(path, state); return state
        if pr.get('state') != 'open' or pr.get('mergeable') is not True:
            state.update(status='ci_pending'); _save(path, state); return state
        required = required_inventory(cmd, root, authorization)
        # gh checks --required also includes legacy branch protection checks.
        native = _json(cmd, root, 'pr', 'checks', str(state['pr']), '--repo', authorization.repository,
                       '--required', '--json', 'name,state')
        if not isinstance(native, list):
            raise PublicationError('native_check_inventory_invalid')
        required.update(row['name'] for row in native)
        checks = _json(cmd, root, 'api', '--paginate', '--slurp', f'repos/{authorization.repository}/commits/{head}/check-runs?per_page=100')
        status_pages = _json(cmd, root, 'api', '--paginate', '--slurp', f'repos/{authorization.repository}/commits/{head}/statuses?per_page=100')
        latest: dict[str, tuple[int, str]] = {}
        for page in checks:
            for row in page['check_runs']:
                if row['head_sha'] != head: raise PublicationError('check_head_mismatch')
                if row['id'] > latest.get(row['name'], (-1, ''))[0]:
                    latest[row['name']] = (row['id'], row['conclusion'] if row['status'] == 'completed' else 'pending')
        statuses = {}
        for page in status_pages:
            for row in page:
                if row['id'] > statuses.get(row['context'], (-1, ''))[0]:
                    statuses[row['context']] = (row['id'], row['state'])
        for name, value in statuses.items():
            if name in required and value[1] != 'success':
                state.update(status='ci_failed' if value[1] in {'failure', 'error'} else 'ci_pending'); _save(path, state); return state
            latest.setdefault(name, value)
        if any(latest.get(name, (-1, 'missing'))[1] in {'failure','cancelled','timed_out','action_required','skipped','neutral','stale'} for name in required):
            state.update(status='ci_failed'); _save(path, state); return state
        if any(latest.get(name, (-1, 'missing'))[1] != 'success' for name in required) or any(row['state'] != 'SUCCESS' for row in native):
            state.update(status='ci_pending'); _save(path, state); return state
        fresh = _json(cmd, root, 'api', f'repos/{authorization.repository}/pulls/{state["pr"]}')
        if fresh['head']['sha'] != head or fresh['base']['sha'] != pr['base']['sha']:
            raise PublicationError('merge_identity_changed')
        state.update(status='merge_uncertain', required_checks=sorted(required), base=pr['base']['sha']); _save(path, state)
        result = _json(cmd, root, 'api', '--method', 'PUT', f'repos/{authorization.repository}/pulls/{state["pr"]}/merge',
                       '-f', 'sha=' + head, '-f', 'merge_method=squash')
        if result.get('merged') is not True or not re.fullmatch(r'[a-f0-9]{40}', result.get('sha', '')):
            raise PublicationError('merge_not_confirmed')
        state.update(status='merged', merge_commit=result['sha']); _save(path, state)
        return state
