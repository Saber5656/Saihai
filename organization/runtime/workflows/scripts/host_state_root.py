#!/usr/bin/env python3
"""Host-owned state-root authority for the workflow frontdoor."""

from __future__ import annotations

import os
import pwd
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from directory_paths import EnvError as DirectoryPathError
from directory_paths import parse_env as parse_directory_catalog


class HostStateRootError(RuntimeError):
    """Raised when the host-owned state-root contract is not satisfied."""


@dataclass(frozen=True)
class PermissionFinding:
    path: str
    artifact_type: str
    actual_mode: str
    required_mode: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "artifact_type": self.artifact_type,
            "actual_mode": self.actual_mode,
            "required_mode": self.required_mode,
        }


def _path_chain(path: Path) -> Iterator[Path]:
    current = Path(path.anchor)
    yield current
    for part in path.parts[1:]:
        current = current / part
        yield current


def _safe_ancestor(metadata: os.stat_result, *, runtime_uid: int) -> bool:
    """Return whether an existing ancestor is safe for runtime authority.

    A root-owned sticky directory (for example ``/tmp``) is safe as an
    ancestor because an unprivileged peer cannot replace another owner's
    entries there.  Any other group/world-writable ancestor is rejected.
    """

    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        return bool(mode & stat.S_ISVTX) and metadata.st_uid == 0
    return metadata.st_uid in {0, runtime_uid}


def ensure_runtime_state_root(
    state_root: Path,
    *,
    runtime_uid: int | None = None,
) -> Path:
    """Create or verify one private runtime-user state root.

    Every missing directory is created with mode ``0700`` under umask 077.
    Existing components must be real directories owned by root or the runtime
    user and must not be replaceable by another local principal.  The final
    state root is always owned by the runtime user with exact mode ``0700``.
    """

    uid = os.getuid() if runtime_uid is None else runtime_uid
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise HostStateRootError("state_root_runtime_uid_invalid")
    supplied = state_root.expanduser()
    if not supplied.is_absolute() or any(part in {".", ".."} for part in supplied.parts):
        raise HostStateRootError("state_root_path_invalid")
    candidate = supplied.resolve(strict=False)
    if supplied != candidate:
        macos_var_alias = (
            str(supplied).startswith("/var/")
            and str(candidate) == "/private" + str(supplied)
        )
        if not macos_var_alias:
            raise HostStateRootError("state_root_symlink_redirection_forbidden")

    old_umask = os.umask(0o077)
    try:
        for component in _path_chain(candidate):
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                try:
                    component.mkdir(mode=0o700)
                except OSError as exc:
                    raise HostStateRootError("state_root_create_failed") from exc
                metadata = component.lstat()
            except OSError as exc:
                raise HostStateRootError("state_root_component_unavailable") from exc
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise HostStateRootError("state_root_component_not_real_directory")
            if component != candidate and not _safe_ancestor(metadata, runtime_uid=uid):
                raise HostStateRootError("state_root_ancestor_unsafe")
        metadata = candidate.lstat()
        if metadata.st_uid != uid:
            raise HostStateRootError("state_root_owner_mismatch")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise HostStateRootError("state_root_mode_must_be_0700")
    finally:
        os.umask(old_umask)
    return candidate.resolve(strict=True)


def _permission_mode_text(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def _permission_scan(
    root_fd: int,
    *,
    expected_uid: int,
    apply: bool,
    max_entries: int,
) -> tuple[list[PermissionFinding], int, int, int]:
    findings: list[PermissionFinding] = []
    directory_count = 0
    file_count = 0
    repaired_count = 0
    entry_count = 0

    def walk(directory_fd: int, relative: str) -> None:
        nonlocal directory_count, file_count, repaired_count, entry_count
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != expected_uid:
            raise HostStateRootError("state_permission_directory_owner_invalid")
        directory_count += 1
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            findings.append(
                PermissionFinding(
                    path=relative,
                    artifact_type="directory",
                    actual_mode=_permission_mode_text(metadata.st_mode),
                    required_mode="0700",
                )
            )

        try:
            names = sorted(entry.name for entry in os.scandir(directory_fd))
        except OSError as exc:
            raise HostStateRootError("state_permission_scan_failed") from exc
        for name in names:
            entry_count += 1
            if entry_count > max_entries:
                raise HostStateRootError("state_permission_scan_limit_exceeded")
            if not name or name in {".", ".."} or os.path.basename(name) != name:
                raise HostStateRootError("state_permission_entry_name_invalid")
            child_relative = name if relative == "." else f"{relative}/{name}"
            try:
                initial = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise HostStateRootError("state_permission_entry_unavailable") from exc
            if stat.S_ISLNK(initial.st_mode):
                raise HostStateRootError("state_permission_symlink_forbidden")
            if stat.S_ISDIR(initial.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise HostStateRootError("state_permission_directory_open_failed") from exc
                try:
                    opened = os.fstat(child_fd)
                    if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
                        raise HostStateRootError("state_permission_entry_raced")
                    walk(child_fd, child_relative)
                    if apply and stat.S_IMODE(opened.st_mode) != 0o700:
                        os.fchmod(child_fd, 0o700)
                        repaired_count += 1
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(initial.st_mode):
                raise HostStateRootError("state_permission_special_file_forbidden")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                child_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise HostStateRootError("state_permission_file_open_failed") from exc
            try:
                opened = os.fstat(child_fd)
                if (
                    (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != expected_uid
                    or opened.st_nlink != 1
                ):
                    raise HostStateRootError("state_permission_file_identity_invalid")
                file_count += 1
                if stat.S_IMODE(opened.st_mode) != 0o600:
                    findings.append(
                        PermissionFinding(
                            path=child_relative,
                            artifact_type="file",
                            actual_mode=_permission_mode_text(opened.st_mode),
                            required_mode="0600",
                        )
                    )
                    if apply:
                        os.fchmod(child_fd, 0o600)
                        repaired_count += 1
            finally:
                os.close(child_fd)

        if apply and relative == "." and stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(directory_fd, 0o700)
            repaired_count += 1

    walk(root_fd, ".")
    return findings, directory_count, file_count, repaired_count


def audit_or_repair_state_permissions(
    state_root: Path,
    *,
    apply: bool = False,
    expected_uid: int | None = None,
    max_entries: int = 200_000,
) -> dict[str, Any]:
    """Audit or explicitly repair legacy private-state modes.

    The scanner never follows links, rejects owner drift, hard-linked files,
    sockets/FIFOs/devices, and performs a complete non-mutating pass before an
    apply pass.  The caller must persist the returned digest-bearing report as
    operator evidence.
    """

    supplied = state_root.expanduser()
    if not supplied.is_absolute() or any(part in {".", ".."} for part in supplied.parts):
        raise HostStateRootError("state_permission_root_invalid")
    resolved = supplied.resolve(strict=True)
    if resolved != supplied:
        macos_var_alias = str(supplied).startswith("/var/") and str(resolved) == "/private" + str(supplied)
        if not macos_var_alias:
            raise HostStateRootError("state_permission_root_redirected")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(resolved, flags)
    except OSError as exc:
        raise HostStateRootError("state_permission_root_open_failed") from exc
    try:
        root_metadata = os.fstat(root_fd)
        owner_uid = root_metadata.st_uid if expected_uid is None else expected_uid
        if os.geteuid() not in {0, owner_uid}:
            raise HostStateRootError("state_permission_operator_not_authorized")
        if root_metadata.st_uid != owner_uid or not stat.S_ISDIR(root_metadata.st_mode):
            raise HostStateRootError("state_permission_root_owner_invalid")
        findings, directories, files, _unused = _permission_scan(
            root_fd,
            expected_uid=owner_uid,
            apply=False,
            max_entries=max_entries,
        )
        repaired_count = 0
        if apply and findings:
            second_findings, second_directories, second_files, repaired_count = _permission_scan(
                root_fd,
                expected_uid=owner_uid,
                apply=True,
                max_entries=max_entries,
            )
            if second_findings != findings or second_directories != directories or second_files != files:
                raise HostStateRootError("state_permission_tree_changed_during_repair")
            remaining, _directories, _files, _repairs = _permission_scan(
                root_fd,
                expected_uid=owner_uid,
                apply=False,
                max_entries=max_entries,
            )
            if remaining:
                raise HostStateRootError("state_permission_repair_incomplete")
        return {
            "state_root": str(resolved),
            "expected_uid": owner_uid,
            "mode": "apply" if apply else "dry_run",
            "decision": (
                "repaired"
                if apply and findings
                else "already_private"
                if not findings
                else "repair_required"
            ),
            "directory_count": directories,
            "file_count": files,
            "finding_count": len(findings),
            "repaired_count": repaired_count,
            "findings": [finding.as_dict() for finding in findings],
        }
    finally:
        os.close(root_fd)


TRUSTED_GIT_EXECUTABLE = Path("/usr/bin/git")
GIT_FIXED_CONFIG = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "credential.helper=",
)


def _drop_to_checkout_owner(owner: pwd.struct_passwd) -> Any:
    def drop() -> None:
        os.initgroups(owner.pw_name, owner.pw_gid)
        os.setgid(owner.pw_gid)
        os.setuid(owner.pw_uid)

    return drop


def run_git_as_checkout_owner(
    repo_root: Path,
    args: list[str] | tuple[str, ...],
    *,
    timeout: float = 30.0,
    text: bool = False,
    capture_output: bool = True,
    input: str | bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run absolute Git without ambient config and never as root on user data."""

    checkout = repo_root.expanduser().resolve(strict=True)
    try:
        metadata = checkout.lstat()
        owner = pwd.getpwuid(metadata.st_uid)
        git_metadata = TRUSTED_GIT_EXECUTABLE.lstat()
    except (KeyError, OSError) as exc:
        raise HostStateRootError("checkout_git_identity_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise HostStateRootError("checkout_git_root_invalid")
    if (
        not stat.S_ISREG(git_metadata.st_mode)
        or git_metadata.st_uid != 0
        or git_metadata.st_mode & 0o022
    ):
        raise HostStateRootError("trusted_git_executable_invalid")
    if os.geteuid() not in {0, owner.pw_uid}:
        raise HostStateRootError("checkout_git_runtime_user_mismatch")

    command = [str(TRUSTED_GIT_EXECUTABLE)]
    for item in GIT_FIXED_CONFIG:
        command.extend(["-c", item])
    command.extend(["-C", str(checkout), *list(args)])
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": owner.pw_dir,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "XDG_CONFIG_HOME": "/dev/null",
    }
    preexec = (
        _drop_to_checkout_owner(owner)
        if os.geteuid() == 0 and owner.pw_uid != 0
        else None
    )
    return subprocess.run(
        command,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        check=False,
        env=environment,
        input=input,
        preexec_fn=preexec,
    )


def ensure_checkout_owner_subdirectory(
    repo_root: Path,
    parent: Path,
    name: str,
) -> Path:
    """Create one private direct child owned by the checkout owner.

    The configured parent must already be a real ``0700`` directory owned by
    the checkout owner.  This keeps a privileged host executor from silently
    creating root-owned worktree paths or traversing a caller-replaceable
    ancestor.
    """

    checkout = repo_root.expanduser().resolve(strict=True)
    supplied_parent = parent.expanduser()
    if (
        not supplied_parent.is_absolute()
        or supplied_parent.resolve(strict=True) != supplied_parent
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name)
    ):
        raise HostStateRootError("checkout_subdirectory_path_invalid")
    try:
        checkout_metadata = checkout.lstat()
        owner = pwd.getpwuid(checkout_metadata.st_uid)
        parent_metadata = supplied_parent.lstat()
    except (KeyError, OSError) as exc:
        raise HostStateRootError("checkout_subdirectory_identity_unavailable") from exc
    if os.geteuid() not in {0, owner.pw_uid}:
        raise HostStateRootError("checkout_subdirectory_runtime_user_mismatch")
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != owner.pw_uid
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise HostStateRootError("checkout_subdirectory_parent_untrusted")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(supplied_parent, flags)
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise HostStateRootError("checkout_subdirectory_parent_raced")
        try:
            os.mkdir(name, 0o700, dir_fd=descriptor)
            if os.geteuid() == 0 and owner.pw_uid != 0:
                os.chown(
                    name,
                    owner.pw_uid,
                    owner.pw_gid,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
        except FileExistsError:
            pass
        except OSError as exc:
            raise HostStateRootError("checkout_subdirectory_create_failed") from exc
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != owner.pw_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise HostStateRootError("checkout_subdirectory_untrusted")
    finally:
        os.close(descriptor)
    return supplied_parent / name


def host_home_directory() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise HostStateRootError("host_account_home_unavailable") from exc
    if not home.is_absolute():
        raise HostStateRootError("host_account_home_invalid")
    return home.resolve()


SAFE_HOST_STATE_ROOT_RE = re.compile(r"^/(?:[\w .,'@%+=:~&()-]+/)*[\w .,'@%+=:~&()-]+/?$")
HOST_HOME = host_home_directory()
DEFAULT_STATE_ROOT = HOST_HOME / ".codex" / "state" / "itb" / "frontdoor-orchestrator"
MANAGED_PRIMARY_CHECKOUT_ROOT = HOST_HOME / "dev" / "Saihai"

# There is deliberately no environment override for state-root authority.
# A fixed deployed bridge receives its root as a root-wrapper CLI argument;
# any legacy-looking deployed variable is treated as an attempted bypass.
_FORBIDDEN_DEPLOYED_ENV = tuple(
    name
    for name in (
        "SAIHAI_DEPLOYED_STATE_ROOT",
        "SAIHAI_DEPLOYED_MANAGED_PRIMARY",
        "SAIHAI_DEPLOYED_STATE_ROOT_CATALOG",
        "SAIHAI_DEPLOYED_STATE_ROOT_CATALOG_STATUS",
        "SAIHAI_DEPLOYED_STATE_ROOT_CATALOG_SHA256",
    )
    if name in os.environ
)
if _FORBIDDEN_DEPLOYED_ENV:
    raise HostStateRootError("deployed state-root environment override forbidden")


def validate_host_state_root_value(raw: str) -> str:
    configured = raw.strip()
    if not SAFE_HOST_STATE_ROOT_RE.fullmatch(configured):
        raise DirectoryPathError("state_root_must_be_validated_absolute_host_path")
    if any(part in {".", ".."} for part in configured.split("/")):
        raise DirectoryPathError("state_root_traversal_forbidden")
    return configured


def _canonical_host_state_root_path(raw: str) -> Path:
    """Return one exact, non-redirected host state-root path.

    The directory catalog is an identity contract, not a path hint.  Reject
    textual normalization (including trailing separators) and any realpath
    change so neither a final-component nor ancestor symlink can select a
    different directory.
    """

    try:
        validated = validate_host_state_root_value(raw)
    except DirectoryPathError as exc:
        raise HostStateRootError(
            "configured state root must be a validated absolute host path"
        ) from exc
    candidate = Path(validated)
    if raw != validated or str(candidate) != validated:
        raise HostStateRootError("state_root_path_not_canonical")
    resolved = candidate.resolve(strict=False)
    if resolved != candidate:
        raise HostStateRootError("state_root_symlink_redirection_forbidden")
    return candidate


def load_primary_directory_catalog(repo_root: Path) -> tuple[Path, dict[str, str], dict[str, object]]:
    checkout = repo_root.resolve()
    primary_root = MANAGED_PRIMARY_CHECKOUT_ROOT.resolve()
    git_admin = checkout / ".git"
    try:
        git_admin_stat = git_admin.lstat()
        primary_git_stat = (primary_root / ".git").lstat()
    except OSError as exc:
        raise HostStateRootError("directory_path_environment_invalid:checkout_identity_invalid") from exc
    if not stat.S_ISDIR(primary_git_stat.st_mode):
        raise HostStateRootError("directory_path_environment_invalid:managed_primary_checkout_unavailable")
    if checkout == primary_root:
        if not stat.S_ISDIR(git_admin_stat.st_mode):
            raise HostStateRootError("directory_path_environment_invalid:checkout_identity_invalid")
    elif not stat.S_ISREG(git_admin_stat.st_mode):
        raise HostStateRootError("directory_path_environment_invalid:checkout_identity_invalid")

    catalog_path = primary_root / "directory-path.env"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(catalog_path, flags)
    except FileNotFoundError:
        return catalog_path, {}, {"status": "not_configured", "loaded_keys": ()}
    except OSError as exc:
        raise HostStateRootError("directory_path_environment_invalid:catalog_must_be_regular_file") from exc
    try:
        catalog_stat = os.fstat(fd)
        if not stat.S_ISREG(catalog_stat.st_mode):
            raise DirectoryPathError("catalog_must_be_regular_file")
        if catalog_stat.st_uid != os.getuid():
            raise DirectoryPathError("catalog_owner_mismatch")
        if stat.S_IMODE(catalog_stat.st_mode) != 0o600:
            raise DirectoryPathError("catalog_mode_must_be_0600")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            parsed = parse_directory_catalog(handle.read())
        configured = parsed.get("SAIHAI_ORCH_STATE_ROOT") or parsed.get("SAHAI_ORCH_STATE_ROOT") or ""
        catalog = {"SAIHAI_ORCH_STATE_ROOT": validate_host_state_root_value(configured)} if configured else {}
    except (DirectoryPathError, OSError, UnicodeError) as exc:
        raise HostStateRootError(f"directory_path_environment_invalid:{exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return catalog_path, catalog, {"status": "loaded", "loaded_keys": tuple(sorted(catalog))}


# Keep import side-effect free so immutable runtime copies without a fake .git
# marker can import checkout helpers used by bridge/canary/attester modules.
# The ordinary full frontdoor resolves and validates the primary catalog only
# when it actually requests configured state-root authority.
PRIMARY_DIRECTORY_CATALOG_PATH = MANAGED_PRIMARY_CHECKOUT_ROOT / "directory-path.env"
DIRECTORY_CATALOG: dict[str, str] = {}
DIRECTORY_ENV_DIAGNOSTICS: dict[str, object] = {
    "status": "lazy_not_loaded",
    "loaded_keys": (),
}


def resolve_configured_state_root(
    requested: str | Path | None = None,
    *,
    runtime_uid: int | None = None,
) -> Path:
    """Resolve and verify the single configured state-root identity.

    ``requested`` is an optional host-pinned copy of the configured value.  It
    must be byte-for-byte identical; resolving two different spellings to the
    same inode is deliberately insufficient.  Both the operator CLI and the
    deployed bridge call this resolver so they share rejection reasons and
    owner/type/mode checks.
    """

    configured = DIRECTORY_CATALOG.get("SAIHAI_ORCH_STATE_ROOT", "")
    if not configured:
        _catalog_path, loaded, _diagnostics = load_primary_directory_catalog(REPO_ROOT)
        configured = loaded.get("SAIHAI_ORCH_STATE_ROOT", "")
    configured_value = configured if configured else str(DEFAULT_STATE_ROOT)
    root = _canonical_host_state_root_path(configured_value)
    if requested not in (None, "") and os.fspath(requested) != configured_value:
        raise HostStateRootError("state_root_not_configured")
    return ensure_runtime_state_root(root, runtime_uid=runtime_uid)


def configured_state_root() -> Path:
    return resolve_configured_state_root()
