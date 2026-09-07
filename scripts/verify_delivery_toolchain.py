#!/usr/bin/env python3
"""Execute the fixed delivery suite with a verified, private native CPython.

Bootstrap Python only acquires and verifies the selected artifact. No runtime,
URL, command, credential, cache or existing-environment fallback is accepted.
"""
from __future__ import annotations

import argparse
import gzip
import selectors
import unicodedata
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_MEMBERS = 20000
DOWNLOAD_TIMEOUT = 120
KNOWN = {
    'Darwin-arm64': 'aarch64-apple-darwin',
    'Linux-x86_64': 'x86_64-unknown-linux-gnu',
}


class ContractError(ValueError): pass
class StageFailure(ContractError): pass


def now(): return datetime.now(timezone.utc).isoformat()
def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def validate_lock(lock):
    if type(lock) is not dict or set(lock) != {'schema_version', 'python', 'dependency_lock', 'cache', 'policy_status', 'codeql'}:
        raise ContractError('invalid lock shape')
    if type(lock['schema_version']) is not int or lock['schema_version'] != 1 or lock['cache'] != 'disabled' or lock['policy_status'] != 'not_adopted':
        raise ContractError('unsupported lock contract')
    if type(lock['python']) is not dict or set(lock['python']) != set(KNOWN):
        raise ContractError('unsupported platform inventory')
    for key, triple in KNOWN.items():
        item = lock['python'][key]
        if type(item) is not dict or set(item) != {'version', 'machine', 'url', 'sha256', 'size', 'interpreter'}:
            raise ContractError('invalid artifact entry')
        expected_url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.11.16%2B20260901-' + triple + '-install_only.tar.gz'
        if item['url'] != expected_url or item['version'] != '3.11.16' or item['machine'] != key.split('-')[1] or item['interpreter'] != 'python/bin/python3.11':
            raise ContractError('unknown artifact selection')
        if type(item['size']) is not int or not 1 <= item['size'] <= MAX_DOWNLOAD or type(item['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', item['sha256']):
            raise ContractError('invalid artifact bounds/digest')
    dep = lock['dependency_lock']
    if type(dep) is not dict or set(dep) != {'path', 'sha256'} or dep['path'] != '.github/requirements-delivery.lock' or type(dep['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', dep['sha256']):
        raise ContractError('invalid dependency contract')
    validate_codeql_lock(lock['codeql'])
    return lock


def load_lock(root):
    path = root / '.github/delivery-toolchain.lock.json'
    if path.is_symlink() or path.stat().st_size > 16384: raise ContractError('invalid lock file')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ContractError('duplicate lock key')
            result[key] = value
        return result
    return validate_lock(json.loads(path.read_text(), object_pairs_hook=unique))


def select(lock, system, machine):
    validate_lock(lock)
    key = system + '-' + machine
    if key not in KNOWN: raise ContractError('local_unavailable: unsupported native platform')
    if system == 'Linux' and platform.libc_ver()[0] != 'glibc':
        raise ContractError('local_unavailable: glibc required')
    return dict(lock['python'][key])


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 4
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {'github.com', 'release-assets.githubusercontent.com'} or parsed.username or parsed.password:
            raise ContractError('unapproved artifact redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_download(url):
    # No environment proxy/credential configuration; public immutable assets only.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), DownloadRedirect())
    return opener.open(url, timeout=20)


def verify_archive(path, selected):
    h = hashlib.sha256(); size = 0
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            size += len(chunk)
            if size > selected['size'] or size > MAX_DOWNLOAD: raise ContractError('archive size exceeded')
            h.update(chunk)
    if size != selected['size'] or h.hexdigest() != selected['sha256']:
        raise ContractError('archive size/digest mismatch')


def download(selected, destination):
    start = time.monotonic(); total = 0
    with open_download(selected['url']) as response, destination.open('xb') as output:
        if response.status != 200: raise ContractError('artifact HTTP failure')
        length = response.headers.get('Content-Length')
        if length is not None and int(length) != selected['size']: raise ContractError('artifact declared size mismatch')
        while True:
            if time.monotonic() - start > DOWNLOAD_TIMEOUT: raise ContractError('artifact download timed out')
            chunk = response.read(1024 * 1024)
            if not chunk: break
            total += len(chunk)
            if total > selected['size'] or total > MAX_DOWNLOAD: raise ContractError('artifact download too large')
            output.write(chunk)
    verify_archive(destination, selected)


def member_name(name):
    parts = PurePosixPath(name).parts
    if not name or len(name) > 512 or name.startswith('/') or '\\' in name or any(x in ('', '.', '..') for x in name.rstrip('/').split('/')) or parts[0] != 'python':
        raise ContractError('unsafe archive path')
    return '/'.join(parts)


def parent_fd(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try: os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError: pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd); raise


def case_sensitive_destination(destination):
    """Measure the new destination filesystem, not the host OS name."""
    with tempfile.TemporaryDirectory(prefix='.case-probe-', dir=destination) as raw:
        probe = Path(raw) / 'case-probe'
        probe.write_bytes(b'')
        return not (Path(raw) / 'CASE-PROBE').exists()


def safe_extract(archive, destination):
    if not callable(getattr(tarfile, 'data_filter', None)):
        raise ContractError('safe extraction filter unavailable')
    destination.mkdir(mode=0o700)  # Must be a new private directory, never reused.
    case_sensitive = case_sensitive_destination(destination)
    start = time.monotonic(); members = {}; link_targets = {}; names_folded = {}; total = 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            if len(members) >= MAX_MEMBERS or time.monotonic() - start > 120: raise ContractError('archive member/time budget')
            name = member_name(member.name)
            if name in members: raise ContractError('duplicate/colliding archive entry')
            if not case_sensitive:
                # Include implicit parents: A/one and a/two also collide.
                for candidate in (PurePosixPath(name), *PurePosixPath(name).parents):
                    spelling = str(candidate); folded = spelling.casefold()
                    if folded in names_folded and names_folded[folded] != spelling:
                        raise ContractError('duplicate/colliding archive entry')
                    names_folded[folded] = spelling
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()) or member.sparse:
                raise ContractError('special/sparse archive member')
            total += member.size
            if member.size < 0 or total > MAX_EXPANDED: raise ContractError('archive expansion budget')
            tarfile.data_filter(member, str(destination))
            members[name] = member
        for name, member in members.items():
            for ancestor in PurePosixPath(name).parents:
                if str(ancestor) in members and not members[str(ancestor)].isdir():
                    raise ContractError('archive parent collision')
            if member.issym() or member.islnk():
                target = member.linkname
                if not target or target.startswith('/') or '\\' in target: raise ContractError('unsafe archive link')
                # Normalize internal relative symlinks; never resolve through the host filesystem.
                stack = list(PurePosixPath(name).parent.parts) if member.issym() else []
                for part in target.split('/'):
                    if part == '..':
                        if not stack: raise ContractError('outward archive link')
                        stack.pop()
                    elif part not in ('', '.'): stack.append(part)
                target = '/'.join(stack)
                if not stack or stack[0] != 'python' or target not in members or not (members[target].isfile() or members[target].isdir()):
                    raise ContractError('unresolved/chained/outward archive link')
                if member.islnk() and not members[target].isfile(): raise ContractError('invalid hardlink target')
                link_targets[name] = target
        root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Links last; every directory traversal is dirfd-relative and refuses symlinks.
            for name, member in sorted(members.items(), key=lambda pair: (pair[1].issym() or pair[1].islnk(), pair[0])):
                if time.monotonic() - start > 120: raise ContractError('extraction timed out')
                parts = PurePosixPath(name).parts; fd = parent_fd(root_fd, parts[:-1])
                try:
                    leaf = parts[-1]
                    if member.isdir():
                        try: os.mkdir(leaf, 0o700, dir_fd=fd)
                        except FileExistsError:
                            check = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); os.close(check)
                    elif member.isfile():
                        file_fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700 if member.mode & 0o111 else 0o600, dir_fd=fd)
                        with os.fdopen(file_fd, 'wb') as output, source.extractfile(member) as input_file:
                            remaining = member.size
                            while remaining:
                                data = input_file.read(min(1024 * 1024, remaining))
                                if not data: raise ContractError('truncated archive member')
                                output.write(data); remaining -= len(data)
                    elif member.issym(): os.symlink(member.linkname, leaf, dir_fd=fd)
                    else: os.link(link_targets[name], leaf, src_dir_fd=root_fd, dst_dir_fd=fd, follow_symlinks=False)
                finally: os.close(fd)
        finally: os.close(root_fd)
    return destination


def check_probe(probe, selected, executable):
    if type(probe) is not dict or probe.get('version') != selected['version'] or probe.get('machine') != selected['machine'] or probe.get('executable') != str(executable):
        raise ContractError('actual interpreter version/architecture/path mismatch')


def require_identity(before, after):
    if before != after: raise ContractError('target changed after validation began')


def target_identity(root):
    def git(*args): return subprocess.check_output(['git', *args], cwd=root, timeout=20)
    files = {}
    for raw in git('ls-files', '-co', '--exclude-standard', '-z').split(b'\0'):
        if raw:
            name = os.fsdecode(raw); path = root / name
            if path.is_symlink(): content = os.fsencode(os.readlink(path))
            elif path.is_file(): content = path.read_bytes()
            else: raise ContractError('missing/nonregular target file')
            files[name] = {'mode': oct(path.lstat().st_mode), 'sha256': digest(content)}
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'head_tree': git('rev-parse', 'HEAD^{tree}').decode().strip(),
            'files_digest': canonical(files),
            'working_patch_digest': digest(git('diff', 'HEAD', '--binary', '--full-index')),
            'index_patch_digest': digest(git('diff', '--cached', '--binary', '--full-index')),
            'workflow_digest': digest((root / '.github/workflows/validate.yml').read_bytes())}


def save_receipt(output, receipt):
    temporary = output / 'receipt.pending'
    temporary.write_text(json.dumps(receipt, indent=2) + '\n')
    temporary.replace(output / 'receipt.json')


def run_stage(name, command, directory, receipt, env, timeout):
    item = {'name': name, 'command': command, 'start': now(), 'status': 'running'}
    receipt['stages'].append(item)
    save_receipt(directory, receipt)
    log = directory / (name + '.log')
    try:
        with log.open('xb') as stream:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try: code = child.wait(timeout=timeout)
            except KeyboardInterrupt:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
                item.update(status='cancelled', exit=130); raise
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
                item.update(status='timed_out', exit=124); raise StageFailure(name + ' timed out')
        item.update(status='success' if code == 0 else 'failure', exit=code)
        if code != 0: raise StageFailure(name + ' failed')
    except OSError as exc:
        item.update(status='failure', exit=1, error_type=type(exc).__name__); raise StageFailure(name + ' unavailable') from exc
    finally:
        item['end'] = now()
        if log.exists(): item['log_sha256'] = digest(log.read_bytes())
        save_receipt(directory, receipt)
    return log


def child_environment():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('PYTHON', 'PIP_', 'LD_', 'DYLD_')) or key.lower().endswith('_proxy') or key in {'VIRTUAL_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'NETRC', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}: env.pop(key)
    env['PIP_CONFIG_FILE'] = os.devnull
    env['NETRC'] = os.devnull
    env['PYTHONNOUSERSITE'] = '1'
    env['SAIHAI_ALLOW_LIVE_PROVIDERS'] = ''
    return env


# Python validation projection only; independent of the larger CodeQL bounds.
VALIDATION_JSON_LIMIT = 2 * 1024 * 1024
SUITE_EVIDENCE_FIELDS = ('path', 'result', 'cases', 'command', 'cwd', 'started_at',
                         'finished_at', 'exit_code', 'status', 'executed', 'failed',
                         'skipped', 'unknown', 'count_method', 'duration_seconds')


def validation_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > VALIDATION_JSON_LIMIT:
        os.close(fd)
        raise ContractError('invalid validation file bounds/type')
    with os.fdopen(fd, 'rb') as stream:
        data = stream.read(VALIDATION_JSON_LIMIT + 1)
    if len(data) > VALIDATION_JSON_LIMIT: raise ContractError('validation byte budget')
    return data


def validation_document(path, expected_digest=None):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ContractError('duplicate validation key')
            result[key] = value
        return result
    def check(value, depth=0):
        if depth > 32: raise ContractError('validation depth budget')
        if type(value) is dict:
            for item in value.values(): check(item, depth + 1)
        elif type(value) is list:
            if len(value) > 1000: raise ContractError('validation item budget')
            for item in value: check(item, depth + 1)
        elif type(value) is float and not math.isfinite(value):
            raise ContractError('nonfinite validation number')
    try:
        data = validation_bytes(path)
        if expected_digest is not None and digest(data) != expected_digest:
            raise ContractError('full validation log changed')
        value = json.loads(data, object_pairs_hook=unique)
        check(value)
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ContractError('invalid validation JSON') from exc
    return value


def sanitize_validation(result, python):
    """Project current U0 metadata, never fill legacy/missing fields with success.

    Version 1 of suite_evidence is additive to the original schema_version 1.
    Old artifacts remain unchanged and lack this contract. This projection is
    descriptive evidence, not authenticated execution or applicability policy.
    """
    if type(result) is not dict or type(result.get('schema_version')) is not int or result['schema_version'] != 1 or result.get('result') != 'pass' or result.get('compiled') is not True:
        raise ContractError('invalid full validation result')
    suites = result.get('suites')
    if type(suites) is not list or not 1 <= len(suites) <= 1000:
        raise ContractError('invalid validation suites')
    public = {key: result[key] for key in ('schema_version', 'result', 'compiled')}
    public.update(suite_evidence_version=1, suites=[])
    seen = set()
    for row in suites:
        if type(row) is not dict or any(key not in row for key in SUITE_EVIDENCE_FIELDS):
            raise ContractError('missing required suite evidence')
        path = row['path']
        if type(path) is not str or not re.fullmatch(r'(?:tests|organization/runtime/workflows/tests|organization/runtime/infra-team-bootstrap/tests|organization/roles/infra-team-bootstrap/tests)/test_[A-Za-z0-9_]+\.py', path) or path in seen:
            raise ContractError('invalid/duplicate suite path')
        if not (ROOT / path).is_file() or (ROOT / path).is_symlink():
            raise ContractError('suite source unavailable')
        seen.add(path)
        if row['command'] != [str(python), str(ROOT / path)] or row['cwd'] != str(ROOT):
            raise ContractError('suite command/cwd mismatch')
        if row['result'] != 'pass' or row['status'] != 'passed' or type(row['exit_code']) is not int or row['exit_code'] != 0:
            raise ContractError('suite did not pass')
        if any(type(row[key]) is not int or row[key] < 0 for key in ('cases', 'executed', 'failed', 'skipped', 'unknown')):
            raise ContractError('invalid suite counts')
        if row['cases'] != row['executed'] or row['executed'] == 0 or any(row[key] for key in ('failed', 'skipped', 'unknown')):
            raise ContractError('required work incomplete')
        if row['count_method'] not in ('structured_result', 'completed_test_functions', 'unittest_summary'):
            raise ContractError('unknown count method')
        times = []
        for key in ('started_at', 'finished_at'):
            value = row[key]
            if type(value) is not str or len(value) > 64: raise ContractError('invalid suite time')
            try: parsed = datetime.fromisoformat(value)
            except ValueError as exc: raise ContractError('invalid suite time') from exc
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ContractError('suite time must be UTC')
            times.append(parsed)
        duration = row['duration_seconds']
        if times[1] < times[0] or type(duration) not in (int, float) or not 0 <= duration <= 1e9:
            raise ContractError('invalid suite duration/order')
        # No stdout/stderr/environment/details or arbitrary additional payload.
        public['suites'].append({key: row[key] for key in SUITE_EVIDENCE_FIELDS})
    expected_contracts = [[str(python), 'organization/runtime/workflows/scripts/workflow_selector.py', 'validate-contracts'],
                          [str(python), 'organization/runtime/workflows/scripts/template_role_validator.py']]
    contracts = result.get('contracts')
    if type(contracts) is not list or len(contracts) != len(expected_contracts):
        raise ContractError('missing required contracts')
    public['contracts'] = []
    for row, command in zip(contracts, expected_contracts):
        if type(row) is not dict or row.get('command') != command or row.get('result') != 'pass':
            raise ContractError('contract did not pass or command mismatch')
        public['contracts'].append({'command': command, 'result': row['result']})
    return public


def publish_validation(full_log, output, receipt, python):
    stage = receipt['stages'][-1]
    if stage.get('name') != 'full' or stage.get('status') != 'success' or type(stage.get('exit')) is not int or stage['exit'] != 0 or type(stage.get('log_sha256')) is not str or not re.fullmatch('[a-f0-9]{64}', stage['log_sha256']):
        raise ContractError('missing successful full stage evidence')
    public = sanitize_validation(validation_document(full_log, stage['log_sha256']), python)
    if 'selection' in receipt:
        selection = validation_document(full_log, stage['log_sha256']).get('selection')
        if selection != receipt['selection']: raise ContractError('shard selection mismatch')
        public['selection'] = selection
    elif 'selection' in validation_document(full_log, stage['log_sha256']):
        raise ContractError('unexpected shard selection')
    data = (json.dumps(public, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if len(data) > VALIDATION_JSON_LIMIT: raise ContractError('public validation byte budget')
    with (output / 'validation.json').open('xb') as stream: stream.write(data)
    # This hashes exact published bytes, including formatting/newline, not full.log.
    receipt['validation_result'] = {'path': 'validation.json', 'sha256': digest(data), 'bytes': len(data)}
    save_receipt(output, receipt)


def verify_validation_result(output, receipt):
    binding = receipt['validation_result']
    data = validation_bytes(output / 'validation.json')
    if len(data) != binding['bytes'] or digest(data) != binding['sha256']:
        raise ContractError('sanitized validation result changed')


def execute(output, run, shard_index=None, shard_count=None):
    selection = None
    if shard_index is not None or shard_count is not None:
        if run != 'full' or type(shard_index) is not int or type(shard_count) is not int or not 0 <= shard_index < shard_count <= 64:
            raise ContractError('invalid shard selection')
        selection = {'kind':'shard','index':shard_index,'count':shard_count}
    output.mkdir(mode=0o700)  # Attempt directory must not already exist.
    receipt = {'schema_version': 1, 'attempt': output.name, 'start': now(), 'status': 'running',
               'bootstrap': {'executable': sys.executable, 'version': platform.python_version()},
               'host': {'system': platform.system(), 'machine': platform.machine(), 'release': platform.release(),
                        'ci_image': {k: os.environ.get(k) for k in ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH')}},
               'stages': [], 'authorizes_execution': False,
               'other_platforms': 'not_run', 'codeql': 'local_unavailable/remote_pending', 'policy': 'not_adopted'}
    if selection is not None: receipt['selection'] = selection
    private = Path(tempfile.mkdtemp(prefix='saihai-delivery-runtime-'))
    # Only receipt.json and sanitized validation.json are publication artifacts.
    save_receipt(output, receipt)
    try:
        receipt['target_before'] = target_identity(ROOT)
        lock = load_lock(ROOT); selected = select(lock, platform.system(), platform.machine())
        receipt.update(lock_digest=digest((ROOT / '.github/delivery-toolchain.lock.json').read_bytes()),
                       selected_platform=platform.system() + '-' + platform.machine(), artifact=selected,
                       dependency_lock_digest=digest((ROOT / lock['dependency_lock']['path']).read_bytes()))
        if receipt['dependency_lock_digest'] != lock['dependency_lock']['sha256']: raise ContractError('dependency lock drift')
        env = child_environment()
        for name, action in [('download', lambda: download(selected, private / 'python.tar.gz')),
                             ('extract', lambda: safe_extract(private / 'python.tar.gz', private / 'runtime'))]:
            stage = {'name': name, 'start': now(), 'status': 'running'}; receipt['stages'].append(stage); save_receipt(output, receipt)
            try: action(); stage.update(status='success', exit=0)
            except KeyboardInterrupt:
                stage.update(status='cancelled', exit=130); raise
            except Exception as exc:
                stage.update(status='failure', exit=1, error_type=type(exc).__name__); raise
            finally: stage['end'] = now(); save_receipt(output, receipt)
        executable = private / 'runtime' / selected['interpreter']
        probe_code = 'import json,platform,sys;print(json.dumps(dict(version=platform.python_version(),machine=platform.machine(),executable=sys.executable,prefix=sys.prefix)))'
        probe_log = run_stage('interpreter-probe', [str(executable), '-I', '-c', probe_code], output, receipt, env, 30)
        probe = json.loads(probe_log.read_text()); check_probe(probe, selected, executable); receipt['interpreter'] = probe
        venv = private / 'venv'
        run_stage('venv', [str(executable), '-I', '-m', 'venv', str(venv)], output, receipt, env, 120)
        python = venv / 'bin/python3'
        run_stage('dependencies', [str(python), '-I', '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple', '--disable-pip-version-check', '--no-input', '--keyring-provider', 'disabled', '--no-cache-dir', '--require-hashes', '--only-binary=:all:', '--no-deps', '-r', str(ROOT / lock['dependency_lock']['path'])], output, receipt, env, 180)
        probe_log = run_stage('venv-probe', [str(python), '-I', '-c', probe_code], output, receipt, env, 30)
        probe = json.loads(probe_log.read_text()); check_probe(probe, selected, python)
        if probe['prefix'] != str(venv): raise ContractError('venv prefix mismatch')
        receipt['consumer_interpreter'] = probe
        env['PATH'] = str(venv / 'bin') + os.pathsep + env.get('PATH', '')
        run_stage('focused-toolchain', [str(python), '-B', 'tests/test_delivery_ci_contract.py'], output, receipt, env, 180)
        run_stage('focused-inventory', [str(python), '-B', 'organization/runtime/workflows/tests/test_delivery_workflow_inventory.py'], output, receipt, env, 180)
        if run == 'full':
            full_command = [str(python), '-B', 'scripts/validate_all.py']
            if selection is not None: full_command.extend(['--shard-index',str(shard_index),'--shard-count',str(shard_count)])
            full_log = run_stage('full', full_command, output, receipt, env, 900)
            publish_validation(full_log, output, receipt, python)
        receipt['target_after'] = target_identity(ROOT); require_identity(receipt['target_before'], receipt['target_after'])
        if receipt['dependency_lock_digest'] != digest((ROOT / lock['dependency_lock']['path']).read_bytes()): raise ContractError('dependency lock changed during run')
        if run == 'full': verify_validation_result(output, receipt)
        receipt.update(status='success', exit=0)
    except KeyboardInterrupt:
        receipt.update(status='cancelled', exit=130)
    except Exception as exc:
        receipt.update(status='failure', exit=receipt['stages'][-1].get('exit', 1) or 1 if receipt['stages'] else 1,
                       error_type=type(exc).__name__, error=str(exc)[:500])
    finally:
        receipt['end'] = now(); save_receipt(output, receipt)
    return receipt['exit']


# CodeQL is a separate fixed consumer. Its larger archive bounds never affect Python.
CODEQL_DOWNLOAD_LIMIT = 1536 * 1024 * 1024
CODEQL_EXPANDED_LIMIT = 16 * 1024 * 1024 * 1024
CODEQL_MEMBER_LIMIT = 200000
CODEQL_MANIFEST_LIMIT = 96 * 1024 * 1024
CODEQL_BUNDLES = {
    'linux64': (822687033, '0144d4bc415aee0d5638119dfb626a2d8689e2ff21758ba211a3984862b8522c'),
    'osx64': (1366660336, '25c79916a4886359b70b4823735c075d7fdc8de8edc567a8bc14b78636b309bb'),
}


def validate_codeql_lock(entries):
    if type(entries) is not dict or set(entries) != set(CODEQL_BUNDLES): raise ContractError('unknown CodeQL inventory')
    for name, (size, sha) in CODEQL_BUNDLES.items():
        item = entries[name]
        if type(item) is not dict or set(item) != {'version', 'size', 'sha256', 'url'}: raise ContractError('invalid CodeQL row')
        if type(item['size']) is not int or item['size'] != size or item['sha256'] != sha or item['version'] != '2.26.0' or item['url'] != 'https://github.com/github/codeql-action/releases/download/codeql-bundle-v2.26.0/codeql-bundle-' + name + '.tar.gz':
            raise ContractError('CodeQL fixed artifact mismatch')


def codeql_select(lock, bundle):
    validate_lock(lock)
    if bundle not in CODEQL_BUNDLES: raise ContractError('unknown CodeQL bundle')
    return dict(lock['codeql'][bundle])


def codeql_text(value, maximum=4096):
    if type(value) is not str or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value) or any(c in value for c in ';`$|&<>'):
        raise ContractError('unsafe observation text')
    return value


def read_codeql_json(path, limit=16384):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit: raise ContractError('invalid bounded JSON file')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ContractError('duplicate JSON key')
            result[key] = value
        return result
    try:
        result = json.loads(path.read_text(), object_pairs_hook=unique)
        def check(value, depth=0):
            if depth > 32: raise ContractError('JSON depth exceeded')
            if type(value) is dict:
                for v in value.values(): check(v, depth + 1)
            elif type(value) is list:
                for v in value: check(v, depth + 1)
            elif type(value) not in (str, int, bool, type(None)): raise ContractError('noncontract JSON value')
        check(result)
    except (ValueError, RecursionError, UnicodeError) as exc: raise ContractError('invalid JSON') from exc
    return result


def codeql_write(path, value):
    data = (json.dumps(value, indent=2) + '\n').encode()
    if len(data) > 16384: raise ContractError('receipt budget')
    pending = path.with_suffix('.pending')
    with pending.open('xb') as stream: stream.write(data)
    pending.replace(path)


def codeql_host(bundle):
    return (bundle == 'linux64' and platform.system() == 'Linux' and platform.machine() == 'x86_64' and platform.libc_ver()[0] == 'glibc') or (bundle == 'osx64' and platform.system() == 'Darwin' and platform.machine() == 'x86_64')


def codeql_download(selected, path):
    # Separate exact bundle bounds, deadline and digest; no shared Python-limit increase.
    started = time.monotonic(); count = 0; h = hashlib.sha256()
    with open_download(selected['url']) as response, path.open('xb') as output:
        if response.status != 200: raise ContractError('CodeQL HTTP failure')
        length = response.headers.get('Content-Length')
        if length is not None and int(length) != selected['size']: raise ContractError('CodeQL declared size mismatch')
        while True:
            if time.monotonic() - started > 300: raise ContractError('CodeQL download deadline')
            chunk = response.read(1024 * 1024)
            if not chunk: break
            count += len(chunk)
            if count > selected['size'] or count > CODEQL_DOWNLOAD_LIMIT: raise ContractError('CodeQL download size exceeded')
            output.write(chunk); h.update(chunk)
    if count != selected['size'] or h.hexdigest() != selected['sha256']: raise ContractError('CodeQL download integrity mismatch')


def codeql_archive_hash(path, selected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != selected['size']: raise ContractError('CodeQL archive file mismatch')
    h = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''): h.update(data)
    if h.hexdigest() != selected['sha256']: raise ContractError('CodeQL archive digest mismatch')


class CodeQLReadBudget:
    def __init__(self, stream): self.stream = stream; self.count = 0; self.started = time.monotonic()
    def read(self, size):
        if size < 0 or size > 1024 * 1024: raise ContractError('oversized archive parser read')
        if time.monotonic() - self.started > 180: raise ContractError('archive read deadline')
        data = self.stream.read(size); self.count += len(data)
        if self.count > CODEQL_EXPANDED_LIMIT: raise ContractError('actual decompressed bytes exceeded')
        return data


def codeql_member_name(name):
    if type(name) is not str or not name or len(name) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in name) or '\\' in name or name.startswith('/'):
        raise ContractError('unsafe CodeQL member name')
    parts = name.rstrip('/').split('/')
    if parts[0] != 'codeql' or any(x in ('', '.', '..') for x in parts): raise ContractError('CodeQL member traversal/layout')
    return '/'.join(parts)


def codeql_link_target(name, link, kind):
    if not link or len(link) > 1024 or link.startswith('/') or '\\' in link or any(ord(c) < 32 for c in link): raise ContractError('invalid archive link')
    stack = list(PurePosixPath(name).parent.parts) if kind == 'symlink' else []
    for part in link.split('/'):
        if part == '..':
            if not stack: raise ContractError('outward archive link')
            stack.pop()
        elif part not in ('', '.'): stack.append(part)
    if not stack or stack[0] != 'codeql': raise ContractError('archive link escape')
    return '/'.join(stack)


def scan_codeql_archive(path):
    entries = {}; folded = set(); declared = 0; started = time.monotonic()
    with gzip.open(path, 'rb') as inflated:
        budget = CodeQLReadBudget(inflated)
        with tarfile.open(fileobj=budget, mode='r|') as tar:
            for member in tar:
                if time.monotonic() - started > 180 or len(entries) >= CODEQL_MEMBER_LIMIT: raise ContractError('CodeQL archive budget')
                name = codeql_member_name(member.name); collision = unicodedata.normalize('NFC', name).casefold()
                if collision in folded: raise ContractError('archive duplicate/normalization collision')
                folded.add(collision)
                if member.sparse or not (member.isfile() or member.isdir() or member.issym() or member.islnk()): raise ContractError('archive special/sparse member')
                if member.mode & 0o7000: raise ContractError('archive elevated file mode')
                declared += member.size
                if member.size < 0 or declared > CODEQL_EXPANDED_LIMIT: raise ContractError('declared archive bytes exceeded')
                kind = 'file' if member.isfile() else 'dir' if member.isdir() else 'symlink' if member.issym() else 'hardlink'
                row = {'kind': kind, 'mode': member.mode & 0o777, 'size': member.size}
                if kind == 'file':
                    h = hashlib.sha256(); count = 0
                    with tar.extractfile(member) as data:
                        while True:
                            chunk = data.read(64 * 1024)
                            if not chunk: break
                            count += len(chunk)
                            if count > member.size or time.monotonic() - started > 180: raise ContractError('member read budget')
                            h.update(chunk)
                    if count != member.size: raise ContractError('truncated archive member')
                    row['sha256'] = h.hexdigest()
                elif kind in ('symlink', 'hardlink'):
                    row['link'] = member.linkname; row['target'] = codeql_link_target(name, member.linkname, kind)
                entries[name] = row
        # Force gzip trailer and remaining decompressed bytes through the same bound.
        while budget.read(64 * 1024): pass
    for name in list(entries):
        for parent in PurePosixPath(name).parents:
            key = str(parent)
            if key == '.': continue
            if key in entries and entries[key]['kind'] != 'dir': raise ContractError('archive prefix collision')
            if key not in entries: entries[key] = {'kind': 'dir', 'mode': None, 'size': 0}
    def follow(name, seen):
        if name not in entries or name in seen: raise ContractError('missing/cyclic archive target')
        entry = entries[name]
        if entry['kind'] in ('symlink', 'hardlink'):
            return follow(entry['target'], seen | {name})
        return name
    for name, row in entries.items():
        if row['kind'] in ('symlink', 'hardlink'):
            final = follow(name, set())
            if row['kind'] == 'hardlink' and entries[final]['kind'] != 'file': raise ContractError('hardlink not a file')
            row['resolved'] = final
    result = {'schema_version': 1, 'entries': entries, 'actual_decompressed_bytes': budget.count}
    if len(entries) > CODEQL_MEMBER_LIMIT or len(json.dumps(result).encode()) > CODEQL_MANIFEST_LIMIT: raise ContractError('manifest budget')
    return result


def verify_codeql_installation(root, manifest):
    if root.is_symlink() or not root.is_dir(): raise ContractError('invalid installed bundle root')
    entries = manifest['entries']; actual = set(); total = 0; started = time.monotonic()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(directory)] + [Path(directory) / name for name in dirs + files]:
            name = 'codeql' + (('/' + str(path.relative_to(root))) if path != root else '')
            if name in actual: continue
            actual.add(name)
            if name not in entries: raise ContractError('unexpected installed bundle member')
            row = entries[name]; st = path.lstat()
            if time.monotonic() - started > 180: raise ContractError('installation verify deadline')
            if row['kind'] == 'symlink':
                if not stat.S_ISLNK(st.st_mode) or os.readlink(path) != row['link']: raise ContractError('installed link mismatch')
            elif row['kind'] == 'dir':
                if not stat.S_ISDIR(st.st_mode): raise ContractError('installed directory mismatch')
                if row['mode'] is not None and stat.S_IMODE(st.st_mode) != row['mode']: raise ContractError('installed directory mode changed')
            else:
                expected = entries[row['resolved']] if row['kind'] == 'hardlink' else row
                if not stat.S_ISREG(st.st_mode) or st.st_size != expected['size']: raise ContractError('installed file shape mismatch')
                h = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as f:
                    for chunk in iter(lambda: f.read(64 * 1024), b''):
                        total += len(chunk)
                        if total > CODEQL_EXPANDED_LIMIT or time.monotonic() - started > 180: raise ContractError('installed data budget')
                        h.update(chunk)
                if h.hexdigest() != expected['sha256']: raise ContractError('installed immutable member changed')
                if stat.S_IMODE(st.st_mode) != expected['mode']: raise ContractError('installed file mode changed')
                if row['kind'] == 'hardlink':
                    target_stat = (root.parent / row['resolved']).stat()
                    if (st.st_dev, st.st_ino) != (target_stat.st_dev, target_stat.st_ino): raise ContractError('installed hardlink shape changed')
    if actual != set(entries): raise ContractError('missing installed bundle member')


def codeql_worker(kind, source, output, manifest_path=None, bundle=None):
    try:
        if kind == 'download':
            codeql_download(codeql_select(load_lock(ROOT), bundle), source); result = {'download_verified': True}
        elif kind == 'scan': result = scan_codeql_archive(source)
        else:
            verify_codeql_installation(source, read_codeql_json(manifest_path, CODEQL_MANIFEST_LIMIT)); result = {'verified': True}
        data = json.dumps(result).encode()
        if len(data) > CODEQL_MANIFEST_LIMIT: raise ContractError('worker output budget')
        with output.open('xb') as f: f.write(data)
        return 0
    except Exception as exc:
        with output.open('xb') as f: f.write(json.dumps({'error_type': type(exc).__name__, 'error_code': str(exc) if isinstance(exc, ContractError) else 'worker_failure'}).encode())
        return 1


def run_codeql_worker(kind, source, output, manifest_path=None, bundle=None):
    command = [sys.executable, '-I', str(Path(__file__).resolve()), '--codeql-worker', kind, '--worker-source', str(source), '--worker-output', str(output)]
    if manifest_path: command += ['--worker-manifest', str(manifest_path)]
    if bundle: command += ['--bundle', bundle]
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=child_environment())
    try: code = child.wait(timeout=300 if kind == 'download' else 180)
    except KeyboardInterrupt:
        os.killpg(child.pid, signal.SIGKILL); child.wait(); raise
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL); child.wait(); raise ContractError('CodeQL worker deadline exceeded')
    if code != 0: raise ContractError('CodeQL worker rejected archive/installation')


def codeql_binding(bundle, language, output):
    if language not in ('actions', 'python'): raise ContractError('unsupported CodeQL language')
    lock = load_lock(ROOT); selected = codeql_select(lock, bundle)
    return {'target': target_identity(ROOT), 'workflow_digest': digest((ROOT / '.github/workflows/codeql.yml').read_bytes()),
            'lock_digest': digest((ROOT / '.github/delivery-toolchain.lock.json').read_bytes()),
            'bundle': bundle, 'artifact': selected, 'language': language, 'attempt': output.name,
            'run_id': codeql_text(os.environ.get('GITHUB_RUN_ID', 'local'), 128),
            'run_attempt': codeql_text(os.environ.get('GITHUB_RUN_ATTEMPT', 'local'), 128)}


def codeql_acquire(output, bundle, language, fetch_only):
    output.mkdir(mode=0o700)
    receipt = {'phase': 'acquire', 'start': now(), 'status': 'running', 'exit': None,
               'mode': 'fetch-only' if fetch_only else 'ci-consumer', 'authorizes_execution': False, 'policy_status': 'not_adopted',
               'runnable_here': codeql_host(bundle), 'host': {'system': platform.system(), 'machine': platform.machine()},
               'runtime': 'not_run' if codeql_host(bundle) else 'local_unavailable', 'analysis': 'not_run', 'authenticated_service_state': 'remote_pending'}
    rp = output / 'receipt-acquire.json'; codeql_write(rp, receipt)
    try:
        binding = codeql_binding(bundle, language, output); receipt['binding'] = binding
        if not codeql_host(bundle) and not fetch_only: raise ContractError('CodeQL host mismatch')
        codeql_write(rp, receipt)
        run_codeql_worker('download', output / 'bundle.partial', output / 'download-verification.json', bundle=bundle)
        run_codeql_worker('scan', output / 'bundle.partial', output / 'manifest.json')
        require_identity(binding, codeql_binding(bundle, language, output))
        (output / 'bundle.partial').rename(output / 'bundle.tar.gz')
        receipt.update(status='acquisition_verified', exit=0, manifest_sha256=digest((output / 'manifest.json').read_bytes()))
    except (Exception, KeyboardInterrupt) as exc:
        receipt.update(status='cancelled' if isinstance(exc, KeyboardInterrupt) else 'failure', exit=130 if isinstance(exc, KeyboardInterrupt) else 1, error_type=type(exc).__name__)
    finally: receipt['end'] = now(); codeql_write(rp, receipt)
    return receipt['exit']


def validate_codeql_phase_fields(record):
    if record.get('authorizes_execution') is not False or record.get('policy_status') != 'not_adopted' or record.get('authenticated_service_state') != 'remote_pending':
        raise ContractError('invalid phase authority fields')
    times = {}
    for field in ('start', 'end') + (('command_start', 'command_end') if record.get('phase') == 'probe' else ()):
        value = record.get(field); codeql_text(value, 64)
        try: parsed = datetime.fromisoformat(value)
        except ValueError as exc: raise ContractError('invalid phase timestamp') from exc
        if parsed.tzinfo is None: raise ContractError('phase timestamp missing timezone')
        times[field] = parsed
    if times['end'] < times['start']: raise ContractError('phase time reversed')
    if record.get('phase') == 'acquire':
        host = record.get('host')
        expected_host = {'system': platform.system(), 'machine': platform.machine()}
        if type(host) is not dict or set(host) != set(expected_host) or any(type(host[key]) is not str or host[key] != expected_host[key] for key in expected_host):
            raise ContractError('invalid acquisition host')
        if record.get('runnable_here') is not True or record.get('runtime') != 'not_run' or record.get('analysis') != 'not_run':
            raise ContractError('invalid acquisition runtime fields')
    elif record.get('phase') == 'probe':
        if record.get('version') != '2.26.0': raise ContractError('invalid probe version')
        codeql_installed_path(record.get('codeql_path'))
        if type(record['command']) is not list or record['command'] != [record['codeql_path'], 'version', '--format=json'] or type(record['command_exit']) is not int or record['command_exit'] != 0: raise ContractError('invalid command receipt')
        for item in record['command']: codeql_text(item)
        if not times['start'] <= times['command_start'] <= times['command_end'] <= times['end']: raise ContractError('command times outside phase')
    else: raise ContractError('unsupported consumed phase')
    for field in ('manifest_sha256',) if record['phase'] == 'acquire' else ('version_document_sha256', 'installation_verification_sha256'):
        if type(record.get(field)) is not str or not re.fullmatch('[a-f0-9]{64}', record[field]): raise ContractError('invalid phase digest')


def codeql_chain(output, bundle, language):
    if output.is_symlink(): raise ContractError('symlink attempt')
    record = read_codeql_json(output / 'receipt-acquire.json')
    if set(record) != {'phase', 'start', 'status', 'exit', 'mode', 'authorizes_execution', 'runnable_here', 'host', 'runtime', 'analysis', 'authenticated_service_state', 'binding', 'manifest_sha256', 'end', 'policy_status'} or record['phase'] != 'acquire' or record['status'] != 'acquisition_verified' or type(record['exit']) is not int or record['exit'] != 0 or record['mode'] != 'ci-consumer':
        raise ContractError('missing/failed/nonconsumer acquisition')
    validate_codeql_phase_fields(record)
    require_identity(canonical(record['binding']), canonical(codeql_binding(bundle, language, output)))
    if not codeql_host(bundle): raise ContractError('runtime host mismatch')
    codeql_archive_hash(output / 'bundle.tar.gz', record['binding']['artifact'])
    manifest_path = output / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > CODEQL_MANIFEST_LIMIT: raise ContractError('manifest file bounds')
    if digest(manifest_path.read_bytes()) != record['manifest_sha256']: raise ContractError('manifest changed')
    return record


class CodeQLCommandFailure(ContractError):
    def __init__(self, code, command, started):
        super().__init__('CodeQL version invocation failed')
        self.exit_code = code
        self.invocation = {'command': command, 'command_start': started, 'command_end': now(), 'command_exit': code}


def bounded_codeql_command(path, directory):
    command = [str(path), 'version', '--format=json']; started = now(); env = child_environment()
    for key in list(env):
        if key.startswith(('JAVA', 'JDK_', '_JAVA', 'CODEQL_')): env.pop(key)
    child = subprocess.Popen(command, env=env, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector(); selector.register(child.stdout, selectors.EVENT_READ); selector.register(child.stderr, selectors.EVENT_READ)
    outputs = {child.stdout: bytearray(), child.stderr: bytearray()}; deadline = time.monotonic() + 30
    try:
        while selector.get_map():
            if time.monotonic() > deadline: raise CodeQLCommandFailure(124, command, started)
            for key, _ in selector.select(.1):
                data = os.read(key.fileobj.fileno(), 16384)
                if not data: selector.unregister(key.fileobj); continue
                outputs[key.fileobj].extend(data)
                if len(outputs[key.fileobj]) > 65536: raise CodeQLCommandFailure(1, command, started)
        code = child.wait(timeout=max(.01, deadline - time.monotonic()))
        if code != 0: raise CodeQLCommandFailure(code, command, started)
        return json.loads(outputs[child.stdout])
    finally:
        selector.close()
        if child.poll() is None: os.killpg(child.pid, signal.SIGKILL); child.wait()
        child.stdout.close(); child.stderr.close()


def codeql_installed_path(codeql_path):
    codeql_text(codeql_path)
    path = Path(codeql_path)
    temporary = Path(os.environ.get('RUNNER_TEMP', '')).resolve()
    if not os.environ.get('RUNNER_TEMP') or not path.is_absolute() or path.name != 'codeql' or path.parent.name != 'codeql': raise ContractError('unexpected init output layout')
    if any(part in ('.', '..') for part in codeql_path.split('/')): raise ContractError('noncanonical init path')
    if not re.fullmatch('[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', path.parent.parent.name): raise ContractError('expected fresh init UUID root')
    try: path.relative_to(temporary)
    except ValueError as exc: raise ContractError('init path outside runner temp') from exc
    for ancestor in [path] + list(path.parents):
        if ancestor.is_symlink(): raise ContractError('symlink init path')
        if ancestor == temporary: break
    try: path.resolve(strict=True).relative_to(temporary)
    except (OSError, RuntimeError, ValueError) as exc: raise ContractError('resolved init path outside runner temp or missing') from exc
    if not path.is_file(): raise ContractError('missing CodeQL wrapper')
    return path


def codeql_probe(output, bundle, language, codeql_path, version, init_outcome):
    # This follows init's first execution; it binds installed immutable bytes before analyze.
    record = codeql_chain(output, bundle, language)
    for value in (codeql_path, version, init_outcome): codeql_text(value)
    if init_outcome != 'success' or version != '2.26.0': raise ContractError('init did not establish expected version')
    path = codeql_installed_path(codeql_path)
    run_codeql_worker('installed', path.parent, output / 'installed-verification.json', output / 'manifest.json')
    command_start = now()
    actual = bounded_codeql_command(path, output)
    command_end = now()
    if type(actual) is not dict or actual.get('version') != '2.26.0': raise ContractError('actual CodeQL version mismatch')
    require_identity(record['binding'], codeql_binding(bundle, language, output))
    return {'codeql_path': codeql_path, 'version': actual['version'], 'version_document_sha256': canonical(actual),
            'command': [str(path), 'version', '--format=json'], 'command_start': command_start, 'command_end': command_end, 'command_exit': 0,
            'installation_verification_sha256': digest((output / 'installed-verification.json').read_bytes()), 'binding': record['binding']}


def codeql_observations(outcome, sarif_id, summary):
    codeql_text(outcome, 32); codeql_text(sarif_id, 256)
    return {'analysis': 'observed_result' if outcome == 'success' and summary is not None else 'not_run' if outcome in ('', 'skipped') else 'unknown' if outcome not in ('failure', 'cancelled', 'timed_out') else outcome,
            'action_outcome': outcome, 'upload_observation': 'observed_request_id' if sarif_id else 'missing',
            'sarif_id': sarif_id or None, 'sarif_summary': summary, 'authenticated_service_state': 'remote_pending', 'authorizes_execution': False}


def codeql_sarif_summary(output, language):
    path = output / 'results' / (language + '.sarif')
    if not path.exists(): return None
    for ancestor in [path, path.parent]:
        if ancestor.is_symlink(): raise ContractError('symlink SARIF')
    data = read_codeql_json(path, 16 * 1024 * 1024)
    if type(data) is not dict or data.get('version') != '2.1.0' or type(data.get('runs')) is not list or not 1 <= len(data['runs']) <= 32: raise ContractError('invalid bounded SARIF')
    count = 0
    for run in data['runs']:
        if type(run) is not dict or type(run.get('results', [])) is not list: raise ContractError('invalid SARIF results')
        count += len(run.get('results', []))
        if count > 100000: raise ContractError('SARIF result budget')
    return {'sha256': digest(path.read_bytes()), 'runs': len(data['runs']), 'results': count}


def codeql_phase(args):
    output = args.output.absolute()
    if args.codeql_phase == 'acquire': return codeql_acquire(output, args.bundle, args.language, args.fetch_only)
    if args.fetch_only: raise ContractError('fetch-only cannot probe/observe')
    phase = args.codeql_phase; rp = output / ('receipt-' + phase + '.json')
    if rp.exists() or rp.is_symlink(): raise ContractError('phase replay forbidden')
    if output.is_symlink() or not output.is_dir(): raise ContractError('missing attempt directory')
    result = {'phase': phase, 'start': now(), 'status': 'running', 'exit': None, 'authorizes_execution': False, 'authenticated_service_state': 'remote_pending', 'policy_status': 'not_adopted'}
    codeql_write(rp, result)
    try:
        if phase == 'probe':
            result.update(codeql_probe(output, args.bundle, args.language, args.codeql_path, args.codeql_version, args.init_outcome))
            result['status'] = 'installation_verified'
        else:
            acquired = codeql_chain(output, args.bundle, args.language)
            probe = read_codeql_json(output / 'receipt-probe.json')
            if set(probe) != {'phase', 'start', 'status', 'exit', 'authorizes_execution', 'authenticated_service_state', 'codeql_path', 'version', 'version_document_sha256', 'installation_verification_sha256', 'binding', 'end', 'policy_status', 'command', 'command_start', 'command_end', 'command_exit'} or probe['phase'] != 'probe' or probe['status'] != 'installation_verified' or type(probe['exit']) is not int or probe['exit'] != 0 or args.init_outcome != 'success': raise ContractError('missing/failed init/probe chain')
            validate_codeql_phase_fields(probe)
            proof = read_codeql_json(output / 'installed-verification.json')
            if type(proof) is not dict or set(proof) != {'verified'} or proof['verified'] is not True: raise ContractError('invalid installation proof')
            if digest((output / 'installed-verification.json').read_bytes()) != probe['installation_verification_sha256']: raise ContractError('installation proof changed')
            require_identity(canonical(probe['binding']), canonical(acquired['binding']))
            observation = codeql_observations(args.analysis_outcome, args.sarif_id, codeql_sarif_summary(output, args.language))
            result.update(observation, binding=acquired['binding'], acquire_receipt_sha256=digest((output / 'receipt-acquire.json').read_bytes()), probe_receipt_sha256=digest((output / 'receipt-probe.json').read_bytes()))
            if observation['analysis'] != 'observed_result' or not args.sarif_id: raise ContractError('analysis/upload observation incomplete')
            result['status'] = 'observation_recorded'
        result['exit'] = 0
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status='cancelled' if isinstance(exc, KeyboardInterrupt) else 'failure', exit=130 if isinstance(exc, KeyboardInterrupt) else getattr(exc, 'exit_code', 1), error_type=type(exc).__name__)
        if isinstance(exc, CodeQLCommandFailure): result.update(exc.invocation)
    finally: result['end'] = now(); codeql_write(rp, result)
    return result['exit']



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard-index', type=int)
    parser.add_argument('--shard-count', type=int)
    parser.add_argument('--output', type=Path, help='new private attempt directory; never reused')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', choices=['focused', 'full'])
    group.add_argument('--codeql-phase', choices=['acquire', 'probe', 'observe'])
    group.add_argument('--codeql-worker', choices=['scan', 'installed', 'download'], help=argparse.SUPPRESS)
    parser.add_argument('--bundle', choices=['linux64', 'osx64'])
    parser.add_argument('--language', choices=['actions', 'python'])
    parser.add_argument('--fetch-only', action='store_true')
    parser.add_argument('--codeql-path', default='')
    parser.add_argument('--codeql-version', default='')
    parser.add_argument('--init-outcome', default='')
    parser.add_argument('--analysis-outcome', default='')
    parser.add_argument('--sarif-id', default='')
    parser.add_argument('--worker-source', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker-manifest', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    if args.codeql_worker:
        if not args.worker_source or not args.worker_output: parser.error('worker paths required')
        raise SystemExit(codeql_worker(args.codeql_worker, args.worker_source, args.worker_output, args.worker_manifest, args.bundle))
    if not args.output: parser.error('new private output required')
    if args.codeql_phase:
        if not args.bundle or not args.language: parser.error('fixed bundle and language required')
        raise SystemExit(codeql_phase(args))
    if args.bundle or args.language or args.fetch_only: parser.error('CodeQL arguments in Python mode')
    raise SystemExit(execute(args.output.absolute(), args.run, args.shard_index, args.shard_count))


if __name__ == '__main__': main()
