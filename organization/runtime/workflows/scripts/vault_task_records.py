"""Host-owned, task-scoped Vault records; never a worker capability grant."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import sys
import os
from pathlib import Path
import re
import stat

_SOURCE = Path(__file__).resolve().parents[3] / 'roles/infra-task-dispatcher/scripts/task_discovery.py'
_SPEC = importlib.util.spec_from_file_location('saihai_canonical_task_discovery', _SOURCE)
discovery = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(discovery)
MAX_RECORD_BYTES = 2 * 1024 * 1024


class VaultTaskError(ValueError):
    def __init__(self, reason: str):
        self.reason_class = reason
        super().__init__(reason)


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def plain(value: object, limit: int = 600) -> str:
    """Render inert single-line text, including Obsidian/plugin metacharacters."""
    text = ' '.join(str(value if value is not None else '').split())[:limit]
    replacements = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '[': '&#91;',
                    ']': '&#93;', '`': '&#96;', '|': '&#124;', '{': '&#123;',
                    '}': '&#125;', '$': '&#36;', '\\': '&#92;'}
    return ''.join(replacements.get(c, c) for c in text)


def _relative(root: Path, path: Path) -> tuple[str, ...]:
    if not root.is_absolute() or not path.is_absolute() or '..' in path.parts:
        raise VaultTaskError('vault_task_path_unsafe')
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise VaultTaskError('vault_task_path_unsafe') from exc
    if len(parts) < 3 or parts[0] != '01-Projects' or any(p in {'.', '..', '.obsidian', '.git'} for p in parts):
        raise VaultTaskError('vault_task_path_unsafe')
    return parts


@contextmanager
def _record(root: Path, path: Path, *, writable: bool = False):
    """Anchor every component to open directory descriptors; never follow links."""
    parts = _relative(root, path)
    handles = []
    edges = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(directory)
        for part in parts[:-1]:
            parent = directory
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            edges.append((parent, part, directory))
            handles.append(directory)
        fd = os.open(parts[-1], (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        handles.append(fd)
        edges.append((directory, parts[-1], fd))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_RECORD_BYTES:
            raise VaultTaskError('vault_task_record_unsafe')
        if writable and not st.st_mode & 0o222:
            raise VaultTaskError('vault_task_record_not_writable')
        fcntl.flock(fd, fcntl.LOCK_EX if writable else fcntl.LOCK_SH)
        data = os.read(fd, MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            raise VaultTaskError('vault_task_record_too_large')
        yield fd, data
        for parent, name, handle in edges:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(handle)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise VaultTaskError('vault_task_record_changed')
    except PermissionError as exc:
        raise VaultTaskError('vault_task_record_not_writable' if writable else 'vault_task_record_unavailable') from exc
    except FileNotFoundError as exc:
        raise VaultTaskError('vault_task_record_missing') from exc
    except (OSError, UnicodeError) as exc:
        raise VaultTaskError('vault_task_record_unavailable') from exc
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _identity(task_id: str, path: Path, data: bytes) -> None:
    if not discovery.TASK_ID_RE.fullmatch(task_id):
        raise VaultTaskError('vault_task_id_invalid')
    try:
        meta = discovery.parse_frontmatter(data.decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise VaultTaskError('vault_task_record_invalid') from exc
    implied = discovery.path_identity(path.stem if path.name.startswith('TSK-') else path.parent.name)
    actual = meta.get('task_id', implied)
    if actual != task_id or (implied and implied != actual) or meta.get('type', 'task') not in discovery.TASK_TYPES:
        raise VaultTaskError('vault_task_identity_mismatch')


def resolve_task(root: Path, task_id: str, *, record_path: Path | None = None) -> dict:
    if not discovery.TASK_ID_RE.fullmatch(task_id):
        raise VaultTaskError('vault_task_id_invalid')
    if record_path is None:
        observed = discovery.discover_tasks(root)
        match = observed['tasks'].get(task_id)
        if not match:
            kind = 'vault_task_record_missing'
            if any(p.get('task_id') == task_id for p in observed['problems']):
                kind = 'vault_task_record_ambiguous'
            raise VaultTaskError(kind)
        record_path = match['path']
    with _record(root, record_path) as (_, data):
        _identity(task_id, record_path, data)
        return {'task_id': task_id, 'path': str(record_path), 'content_digest': digest(data),
                'convention': '03-Contexts/Policies/Task-File-Conventions.md'}


def scaffold(root: Path, task_id: str, *, project: str, brief: dict) -> dict:
    """Create the canonical folder task from a host-approved typed brief only."""
    if not discovery.TASK_ID_RE.fullmatch(task_id) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', project):
        raise VaultTaskError('vault_task_id_invalid')
    if not isinstance(brief, dict) or set(brief) != {'objective', 'scope', 'acceptance_criteria'} or any(
        not isinstance(brief[k], str) or not brief[k].strip() or len(brief[k]) > 4000 for k in brief
    ):
        raise VaultTaskError('vault_task_brief_invalid')
    observed = discovery.discover_tasks(root)
    if task_id in observed['tasks'] or any(p.get('task_id') == task_id for p in observed['problems']):
        raise VaultTaskError('vault_task_record_exists')
    path = root / '01-Projects' / project / task_id / 'task.md'
    handles = []
    edges = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(fd)
        for name in ('01-Projects', project, task_id):
            try:
                os.mkdir(name, mode=0o755, dir_fd=fd)
            except FileExistsError:
                pass
            parent = fd
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            edges.append((parent, name, fd))
            handles.append(fd)
        for parent, name, opened in edges:
            actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(opened)
            if (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino):
                raise VaultTaskError('vault_task_record_changed')
        record_fd = os.open('task.md', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        text = f'---\ntype: task\ntask_id: {task_id}\nstatus: ready\n---\n\n'
        text += '\n\n'.join('## ' + key + '\n\n' + plain(brief[key], 4000) for key in brief) + '\n'
        with os.fdopen(record_fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for parent, name, opened in edges:
            actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(opened)
            if (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino):
                raise VaultTaskError('vault_task_record_changed')
    except FileExistsError as exc:
        raise VaultTaskError('vault_task_record_exists') from exc
    except OSError as exc:
        raise VaultTaskError('vault_task_record_unavailable') from exc
    finally:
        for handle in reversed(handles):
            os.close(handle)
    return resolve_task(root, task_id, record_path=path)


def append_completion(root: Path, binding: dict, *, run_id: str, evidence: dict) -> dict:
    """Append once per immutable result. A failed write never means completion."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,191}', run_id):
        raise VaultTaskError('vault_run_id_invalid')
    task_id = str(binding.get('task_id', ''))
    path = Path(str(binding.get('path', '')))
    # Do not persist provider transcripts or arbitrary supplied markdown/links.
    keys = ('result', 'verification_decision', 'terminal_status', 'terminal_reason',
            'report_sha256', 'evidence_sha256', 'merge_commit', 'pr', 'validation')
    fields = {k: plain(evidence[k]) for k in keys if k in evidence}
    if evidence.get('attachments'):
        refs = checked_attachments(evidence['attachments'])
        serialized = json.dumps(refs, sort_keys=True)
        if len(serialized) > 8000:
            raise VaultTaskError('vault_attachment_limit')
        fields['attachments'] = plain(serialized, 8000)
    material = (task_id + '\n' + run_id + '\n' + repr(sorted(fields.items()))).encode()
    marker = '<!-- saihai-completion:' + hashlib.sha256(material).hexdigest() + ' -->'
    block = '\n' + marker + '\n## Saihai completion\n\n| Field | Value |\n|---|---|\n'
    block += '| Task | ' + plain(task_id) + ' |\n| Run | ' + plain(run_id) + ' |\n'
    block += ''.join('| ' + k + ' | ' + v + ' |\n' for k, v in fields.items())
    encoded = block.encode()
    with _record(root, path, writable=True) as (fd, data):
        _identity(task_id, path, data)
        repeated = marker.encode() in data
        if repeated and encoded not in data:
            raise VaultTaskError('vault_completion_receipt_conflict')
        if not repeated:
            if len(data) + len(encoded) > MAX_RECORD_BYTES:
                raise VaultTaskError('vault_task_record_too_large')
            os.lseek(fd, 0, os.SEEK_END)
            view = memoryview(encoded)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise VaultTaskError('vault_completion_write_failed')
                view = view[count:]
            os.fsync(fd)
            data += encoded
        return {'status': 'persisted', 'task_id': task_id, 'run_id': run_id,
                'path': str(path), 'block_digest': digest(encoded), 'content_digest': digest(data),
                'replayed': repeated, 'committed': False, 'published': False}


def canonical_root() -> Path:
    # A fresh catalog load with empty environment; no caller-supplied path grant.
    checkout = Path(__file__).resolve().parents[4]
    if str(checkout) not in sys.path:
        sys.path.insert(0, str(checkout))
    import directory_paths
    env = {}
    try:
        result = directory_paths.load_environment(checkout_root=Path.home() / 'dev/Saihai', environ=env, require_catalog=True)
        if result['status'] != 'loaded':
            raise VaultTaskError('vault_catalog_unavailable')
        return Path(env['AGENTS_VAULT_ROOT'])
    except (ValueError, OSError, KeyError) as exc:
        raise VaultTaskError('vault_catalog_unavailable') from exc


def bind_task(task_id: str, *, authority_ref: str = '') -> dict:
    root = canonical_root()
    explicit = Path(authority_ref.split('#', 1)[0]) if authority_ref else None
    if explicit is not None and not explicit.is_absolute():
        explicit = None  # A provenance label is not a path or a separate grant.
    return resolve_task(root, task_id, record_path=explicit)


def checked_attachments(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or len(items) > 8:
        raise VaultTaskError('vault_attachment_limit')
    results = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'path', 'digest'}:
            raise VaultTaskError('vault_attachment_invalid')
        if not isinstance(item['path'], str) or not isinstance(item['digest'], str):
            raise VaultTaskError('vault_attachment_invalid')
        path = Path(item['path'])
        if not path.is_absolute() or '..' in path.parts:
            raise VaultTaskError('vault_attachment_invalid')
        handles = []
        try:
            fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); handles.append(fd)
            for name in path.parts[1:-1]:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); handles.append(fd)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); handles.append(fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_RECORD_BYTES:
                raise VaultTaskError('vault_attachment_invalid')
            data = os.read(fd, MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES or digest(data) != item['digest']:
                raise VaultTaskError('vault_attachment_digest_mismatch')
        except OSError as exc:
            raise VaultTaskError('vault_attachment_unavailable') from exc
        finally:
            for handle in reversed(handles): os.close(handle)
        results.append({'path': str(path), 'digest': item['digest']})
    return results


def persist_completion(binding: dict, *, run_id: str, evidence: dict, attachments: list[dict]) -> dict:
    refs = checked_attachments(attachments)
    return append_completion(canonical_root(), binding, run_id=run_id,
                             evidence=dict(evidence, attachments=refs))
