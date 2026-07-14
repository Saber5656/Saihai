#!/usr/bin/env python3
"""Host-owned state-root authority for the workflow frontdoor."""

from __future__ import annotations

import os
import pwd
import re
import stat
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from directory_paths import EnvError as DirectoryPathError
from directory_paths import parse_env as parse_directory_catalog


class HostStateRootError(RuntimeError):
    """Raised when the host-owned state-root contract is not satisfied."""


def host_home_directory() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise HostStateRootError("host_account_home_unavailable") from exc
    if not home.is_absolute():
        raise HostStateRootError("host_account_home_invalid")
    return home.resolve()


HOST_HOME = host_home_directory()
DEFAULT_STATE_ROOT = HOST_HOME / ".codex" / "state" / "itb" / "frontdoor-orchestrator"
MANAGED_PRIMARY_CHECKOUT_ROOT = HOST_HOME / "dev" / "Saihai"
SAFE_HOST_STATE_ROOT_RE = re.compile(r"^/(?:[\w .,'@%+=:~&()-]+/)*[\w .,'@%+=:~&()-]+/?$")


def validate_host_state_root_value(raw: str) -> str:
    configured = raw.strip()
    if not SAFE_HOST_STATE_ROOT_RE.fullmatch(configured):
        raise DirectoryPathError("state_root_must_be_validated_absolute_host_path")
    if any(part in {".", ".."} for part in configured.split("/")):
        raise DirectoryPathError("state_root_traversal_forbidden")
    return configured


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


PRIMARY_DIRECTORY_CATALOG_PATH, DIRECTORY_CATALOG, DIRECTORY_ENV_DIAGNOSTICS = load_primary_directory_catalog(REPO_ROOT)


def configured_state_root() -> Path:
    configured = DIRECTORY_CATALOG.get("SAIHAI_ORCH_STATE_ROOT", "").strip()
    if configured:
        try:
            root = Path(validate_host_state_root_value(configured))
        except DirectoryPathError as exc:
            raise HostStateRootError("configured state root must be a validated absolute host path") from exc
    else:
        root = DEFAULT_STATE_ROOT
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise HostStateRootError("configured state root must be a non-symlink directory")
    return root.resolve(strict=False)
