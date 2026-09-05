#!/usr/bin/env python3
"""Execute the fixed delivery suite with a verified, private native CPython.

Bootstrap Python only acquires and verifies the selected artifact. No runtime,
URL, command, credential, cache or existing-environment fallback is accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
    if type(lock) is not dict or set(lock) != {'schema_version', 'python', 'dependency_lock', 'cache', 'policy_status'}:
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


def safe_extract(archive, destination):
    if not callable(getattr(tarfile, 'data_filter', None)):
        raise ContractError('safe extraction filter unavailable')
    destination.mkdir(mode=0o700)  # Must be a new private directory, never reused.
    start = time.monotonic(); members = {}; link_targets = {}; names_folded = set(); total = 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            if len(members) >= MAX_MEMBERS or time.monotonic() - start > 120: raise ContractError('archive member/time budget')
            name = member_name(member.name)
            if name.casefold() in names_folded: raise ContractError('duplicate/colliding archive entry')
            names_folded.add(name.casefold())
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


def execute(output, run):
    output.mkdir(mode=0o700)  # Attempt directory must not already exist.
    receipt = {'schema_version': 1, 'attempt': output.name, 'start': now(), 'status': 'running',
               'bootstrap': {'executable': sys.executable, 'version': platform.python_version()},
               'host': {'system': platform.system(), 'machine': platform.machine(), 'release': platform.release(),
                        'ci_image': {k: os.environ.get(k) for k in ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH')}},
               'stages': [], 'authorizes_execution': False,
               'other_platforms': 'not_run', 'codeql': 'local_unavailable/remote_pending', 'policy': 'not_adopted'}
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
            full_log = run_stage('full', [str(python), '-B', 'scripts/validate_all.py'], output, receipt, env, 900)
            result = json.loads(full_log.read_text())
            if result.get('result') != 'pass' or not result.get('suites'): raise ContractError('invalid full validation result')
            # Deliberate allowlist: no raw output tails or arbitrary logs in uploaded JSON.
            public = {k: result[k] for k in ('schema_version', 'result', 'compiled')}
            public['suites'] = [{k: row[k] for k in ('path', 'result', 'cases')} for row in result['suites']]
            public['contracts'] = [{k: row[k] for k in ('command', 'result')} for row in result['contracts']]
            (output / 'validation.json').write_text(json.dumps(public, indent=2) + '\n')
        receipt['target_after'] = target_identity(ROOT); require_identity(receipt['target_before'], receipt['target_after'])
        if receipt['dependency_lock_digest'] != digest((ROOT / lock['dependency_lock']['path']).read_bytes()): raise ContractError('dependency lock changed during run')
        receipt.update(status='success', exit=0)
    except KeyboardInterrupt:
        receipt.update(status='cancelled', exit=130)
    except Exception as exc:
        receipt.update(status='failure', exit=receipt['stages'][-1].get('exit', 1) or 1 if receipt['stages'] else 1,
                       error_type=type(exc).__name__, error=str(exc)[:500])
    finally:
        receipt['end'] = now(); save_receipt(output, receipt)
    return receipt['exit']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='new private attempt directory; never reused')
    parser.add_argument('--run', choices=['focused', 'full'], required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(execute(args.output.absolute(), args.run))


if __name__ == '__main__': main()
