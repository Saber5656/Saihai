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


def safe_relative_component(value: object, *, label: str) -> str:
    """Return one portable relative-path component without narrowing its alphabet."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or os.path.basename(value) != value
    ):
        raise SafePathError(f"unsafe_{label}")
    return value


def _candidate_text(candidate: object, *, label: str) -> str:
    if not isinstance(candidate, (str, Path)):
        raise SafePathError(f"unsafe_{label}")
    raw = str(candidate)
    if not raw or "\x00" in raw:
        raise SafePathError(f"unsafe_{label}")
    if raw == "~":
        return str(Path.home())
    if raw.startswith("~/"):
        raw = str(Path.home()) + raw[1:]
    if raw.startswith("~"):
        raise SafePathError(f"unsafe_{label}")
    if raw == "/var" or raw.startswith("/var/"):
        return "/private" + raw
    return raw


def _relative_parts_below_root(
    raw: str,
    *,
    roots: Iterable[Path],
    label: str,
    strict_components: bool,
) -> list[str]:
    relative = raw
    if raw.startswith("/"):
        relative = ""
        for root in roots:
            root_text = str(root)
            if raw == root_text:
                break
            prefix = root_text.rstrip("/") + "/"
            if raw.startswith(prefix):
                relative = raw[len(prefix) :]
                break
        else:
            raise SafePathError(f"{label}_outside_trusted_root")
    if not relative:
        return []
    raw_parts = [part for part in relative.split("/") if part != "."]
    if not raw_parts:
        return []
    validator = safe_component if strict_components else safe_relative_component
    return [
        validator(part, label=f"{label}_component_{index}")
        for index, part in enumerate(raw_parts)
    ]


def confined_trusted_root_path(
    trusted_root: Path,
    candidate: object,
    *,
    label: str,
    strict: bool = False,
) -> Path:
    """Resolve a candidate below a host-trusted root and reject any escape.

    ``trusted_root`` is part of the trust boundary: callers must derive it from
    host configuration rather than the request that supplied ``candidate``.
    """

    raw = _candidate_text(candidate, label=label)
    root = trusted_root.expanduser().resolve(strict=True)
    parts = _relative_parts_below_root(
        raw,
        roots=(root,),
        label=label,
        strict_components=False,
    )
    requested = root.joinpath(*parts)
    resolved = requested.resolve(strict=strict)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SafePathError(f"{label}_outside_trusted_root") from exc
    return resolved


def exact_allowlisted_path(
    candidate: object,
    *,
    allowed_paths: Iterable[Path],
    label: str,
) -> Path:
    """Return a host-derived allowlist member exactly matching a candidate.

    Returning the matched allowlist value, rather than the request-derived
    value, keeps the authority for the resulting path on the host side.
    """

    requested = _candidate_text(candidate, label=label)
    for allowed_path in allowed_paths:
        allowed = allowed_path.expanduser().resolve(strict=False)
        allowed_text = str(allowed)
        accepted = {allowed_text}
        if allowed_text.startswith("/private/var/"):
            accepted.add(allowed_text.removeprefix("/private"))
        if requested in accepted:
            return allowed
    raise SafePathError(f"{label}_not_allowlisted")


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
    candidate: object,
    *,
    namespaces: Iterable[str],
) -> Path:
    """Validate an already-derived path and reject symlinks in its state-root chain."""

    allowed = frozenset(namespaces)
    if not allowed or not allowed.issubset(STATE_NAMESPACES):
        raise SafePathError("unsafe_state_namespace")
    configured_root = state_root.expanduser().absolute()
    root = configured_root.resolve(strict=False)
    raw = _candidate_text(candidate, label="state_artifact_path")
    try:
        parts = _relative_parts_below_root(
            raw,
            roots=(root, configured_root),
            label="path",
            strict_components=True,
        )
    except SafePathError as exc:
        if str(exc) == "path_outside_trusted_root":
            raise SafePathError("state_artifact_path_escape") from exc
        raise
    if not parts or parts[0] not in allowed:
        raise SafePathError("state_artifact_namespace_mismatch")
    current = root
    for part in parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SafePathError("state_artifact_path_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SafePathError("state_artifact_symlink")
    return current
