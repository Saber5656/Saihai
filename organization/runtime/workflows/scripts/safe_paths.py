#!/usr/bin/env python3
"""Fail-closed constructors for artifacts confined below a workflow state root."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable

SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
STATE_NAMESPACES = frozenset(
    {
        "adapter-requests",
        "audit",
        "provider-evidence",
        "reports",
        "runs",
        "work-orders",
    }
)


class SafePathError(ValueError):
    """A state artifact path escaped its approved root or used an unsafe component."""


def safe_component(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT_RE.fullmatch(value):
        raise SafePathError(f"unsafe_{label}")
    if value in {".", ".."} or os.path.basename(value) != value:
        raise SafePathError(f"unsafe_{label}")
    return value


def state_artifact_path(
    state_root: Path,
    namespace: str,
    *components: str,
) -> Path:
    """Construct a canonical path from allowlisted single path components."""

    if namespace not in STATE_NAMESPACES:
        raise SafePathError("unsafe_state_namespace")
    root = state_root.expanduser().resolve(strict=False)
    if not root.is_absolute():
        raise SafePathError("unsafe_state_root")
    base = root / namespace
    safe_components = [
        safe_component(component, label=f"path_component_{index}")
        for index, component in enumerate(components)
    ]
    candidate = base.joinpath(*safe_components)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise SafePathError("state_artifact_path_escape") from exc
    return confined_state_path(root, candidate, namespaces={namespace})


def confined_state_path(
    state_root: Path,
    candidate: Path,
    *,
    namespaces: Iterable[str],
) -> Path:
    """Validate an already-derived path and reject symlinks in its state-root chain."""

    allowed = frozenset(namespaces)
    if not allowed or not allowed.issubset(STATE_NAMESPACES):
        raise SafePathError("unsafe_state_namespace")
    configured_root = state_root.expanduser().absolute()
    root = configured_root.resolve(strict=False)
    absolute = candidate
    if not absolute.is_absolute():
        absolute = root / absolute
    else:
        try:
            configured_relative = absolute.relative_to(configured_root)
        except ValueError:
            pass
        else:
            absolute = root / configured_relative
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise SafePathError("state_artifact_path_escape") from exc
    if not relative.parts or relative.parts[0] not in allowed:
        raise SafePathError("state_artifact_namespace_mismatch")
    for index, part in enumerate(relative.parts):
        safe_component(part, label=f"path_component_{index}")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SafePathError("state_artifact_path_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SafePathError("state_artifact_symlink")
    resolved = absolute.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SafePathError("state_artifact_path_escape") from exc
    return resolved
