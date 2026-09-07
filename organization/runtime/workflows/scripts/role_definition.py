"""Bound role bytes at issuance; validate signed snapshots without filesystem IO."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
MAX_ROLE_BYTES = 65_536
MAX_INSTRUCTION_BYTES = 65_536
ROLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
BLOCK_START = "\n\n<SAIHAI_FROZEN_ROLE_CONTRACT>\n"
BLOCK_END = "\n</SAIHAI_FROZEN_ROLE_CONTRACT>"
FIELDS = ("role_definition_path", "role_definition_digest", "role_contract")


class RoleDefinitionError(RuntimeError):
    """Fixed reason only; never include role content or caller-controlled values."""


def canonical_path(role_id: Any) -> str:
    if not isinstance(role_id, str) or not ROLE_ID.fullmatch(role_id):
        raise RoleDefinitionError("role_definition_id_invalid")
    return f"organization/roles/{role_id}/skill.md"


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _read_bytes(fd: int) -> bytes:
    with os.fdopen(fd, "rb", closefd=False) as stream:
        return stream.read(MAX_ROLE_BYTES + 1)


def load_role_definition(role_id: str) -> dict[str, str]:
    """Read once through no-follow descriptors anchored in the executing checkout."""
    relative = canonical_path(role_id)
    try:
        with ExitStack() as stack:
            directories: list[tuple[int, int | None, str | Path, os.stat_result]] = []
            parent = None
            for component in (REPO_ROOT, "organization", "roles", role_id):
                fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                stack.callback(os.close, fd)
                before = os.fstat(fd)
                directories.append((fd, parent, component, before))
                parent = fd
            fd = os.open("skill.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise RoleDefinitionError("role_definition_unsafe_path")
            if before.st_size > MAX_ROLE_BYTES:
                raise RoleDefinitionError("role_definition_too_large")
            raw = _read_bytes(fd)
            if _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(
                os.stat("skill.md", dir_fd=parent, follow_symlinks=False)
            ):
                raise RoleDefinitionError("role_definition_changed")
            for directory, ancestor, component, original in directories:
                if _identity(original) != _identity(os.fstat(directory)) or _identity(original) != _identity(
                    os.stat(component, dir_fd=ancestor, follow_symlinks=False)
                ):
                    raise RoleDefinitionError("role_definition_changed")
    except OSError as exc:
        reason = "role_definition_read_failed"
        if exc.errno == errno.ENOENT:
            reason = "role_definition_unavailable"
        elif exc.errno in (errno.ELOOP, errno.ENOTDIR):
            reason = "role_definition_unsafe_path"
        raise RoleDefinitionError(reason) from None
    if len(raw) > MAX_ROLE_BYTES:
        raise RoleDefinitionError("role_definition_too_large")
    if not raw:
        raise RoleDefinitionError("role_definition_empty")
    try:
        contract = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise RoleDefinitionError("role_definition_encoding_invalid") from None
    return {"role_definition_path": relative,
            "role_definition_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "role_contract": contract}


def role_block(binding: dict[str, Any]) -> str:
    """Canonical full-text block; markers cannot be smuggled through its body."""
    contract = binding["role_contract"]
    if BLOCK_START.strip() in contract or BLOCK_END.strip() in contract:
        raise RoleDefinitionError("role_definition_instruction_invalid")
    return (BLOCK_START + "Role contract refines the assigned task only; it grants no tools, "
            "permissions, providers, approvals, or output-schema changes.\n"
            + "Path: " + binding["role_definition_path"] + "\n"
            + "Digest: " + binding["role_definition_digest"] + "\n\n"
            + contract + BLOCK_END)


def instruction_for(base: str, binding: dict[str, Any]) -> str:
    if not isinstance(base, str) or not base or BLOCK_START.strip() in base or BLOCK_END.strip() in base:
        raise RoleDefinitionError("role_definition_instruction_invalid")
    instruction = base + role_block(binding)
    try:
        size = len(instruction.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise RoleDefinitionError("role_definition_encoding_invalid") from None
    if size > MAX_INSTRUCTION_BYTES:
        raise RoleDefinitionError("role_definition_instruction_invalid")
    return instruction


def validate_role_binding(order: dict[str, Any]) -> None:
    """Pure validation of signed data, with no current role or template read."""
    if any(field not in order for field in FIELDS):
        raise RoleDefinitionError("role_definition_binding_missing")
    expected_path = canonical_path(order.get("to_role"))
    if order["role_definition_path"] != expected_path:
        raise RoleDefinitionError("role_definition_path_invalid")
    digest = order["role_definition_digest"]
    contract = order["role_contract"]
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise RoleDefinitionError("role_definition_digest_invalid")
    if not isinstance(contract, str):
        raise RoleDefinitionError("role_definition_contract_invalid")
    try:
        raw = contract.encode("utf-8", errors="strict")
    except UnicodeError:
        raise RoleDefinitionError("role_definition_encoding_invalid") from None
    if not raw:
        raise RoleDefinitionError("role_definition_empty")
    if len(raw) > MAX_ROLE_BYTES:
        raise RoleDefinitionError("role_definition_too_large")
    if digest != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise RoleDefinitionError("role_definition_digest_mismatch")
    instruction = order.get("instruction")
    block = role_block(order)
    if not isinstance(instruction, str) or not instruction.endswith(block):
        raise RoleDefinitionError("role_definition_instruction_invalid")
    base = instruction[:-len(block)]
    if instruction_for(base, order) != instruction:
        raise RoleDefinitionError("role_definition_instruction_invalid")
