#!/usr/bin/env python3
"""Pure contract validation for an isolated worker policy-domain boundary.

This module deliberately performs no I/O and does not provision or launch a
worker.  A future host-owned transport controller may call these validators
before it writes a read-only input image and after it captures the guest's
typed result channel.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import work_order_builder


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
INPUT_SCHEMA_PATH = (
    WORKFLOW_ROOT / "schemas" / "isolated-worker-input.schema.json"
)
RESULT_SCHEMA_PATH = (
    WORKFLOW_ROOT / "schemas" / "isolated-worker-result.schema.json"
)

MAX_WORK_ORDER_BYTES = 1 * 1024 * 1024
MAX_REPOSITORY_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_RESULT_CONTRACT_BYTES = 1 * 1024 * 1024
MAX_PATCH_BYTES = 16 * 1024 * 1024
MAX_RESULT_ENVELOPE_BYTES = 24 * 1024 * 1024
MAX_EXECUTION_WINDOW_SECONDS = 5 * 60
MAX_RESULT_NESTING_DEPTH = 64
VCS_CONTROL_PATH_COMPONENTS = frozenset(
    {
        ".git",
        ".gitmodules",
        ".hg",
        ".svn",
        ".bzr",
        "_darcs",
        ".pijul",
        ".fossil-settings",
        ".jj",
        ".sl",
        "cvs",
        "rcs",
        "sccs",
    }
)
INPUT_ARTIFACT_FIELDS = (
    "approved_work_order",
    "repository_snapshot",
    "result_contract",
)
INPUT_ARTIFACT_LIMITS = {
    "approved_work_order": MAX_WORK_ORDER_BYTES,
    "repository_snapshot": MAX_REPOSITORY_SNAPSHOT_BYTES,
    "result_contract": MAX_RESULT_CONTRACT_BYTES,
}
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "description",
        "enum",
        "items",
        "maximum",
        "minimum",
        "minItems",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)
SUPPORTED_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
FORBIDDEN_LOCATION_KEYS = frozenset(
    {
        "file_path",
        "file_uri",
        "host_path",
        "host_state_path",
        "host_state_root",
        "path",
        "state_path",
        "state_root",
        "uri",
        "vault_path",
        "vault_root",
        "write_target",
    }
)
_SCHEMA_CACHE: dict[Path, dict[str, Any]] = {}


class IsolatedWorkerTransportError(RuntimeError):
    """Stable fail-closed transport contract error."""

    def __init__(self, reason: str, errors: list[str] | None = None) -> None:
        self.reason = reason
        self.errors = tuple(errors or (reason,))
        super().__init__(reason)


def _load_schema(path: Path) -> dict[str, Any]:
    if path not in _SCHEMA_CACHE:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise IsolatedWorkerTransportError("transport_schema_invalid")
        _SCHEMA_CACHE[path] = value
    return _SCHEMA_CACHE[path]


def _schema_definition_errors(
    schema: Any, path: str = "$schema"
) -> list[str]:
    """Reject schema constructs this deliberately small evaluator cannot enforce."""

    if not isinstance(schema, dict):
        return [f"schema_definition:{path}:not_object"]
    errors: list[str] = []
    for keyword in schema:
        if keyword not in SUPPORTED_SCHEMA_KEYWORDS:
            errors.append(
                f"schema_definition:{path}.{keyword}:unsupported_keyword"
            )

    expected_type = schema.get("type")
    if expected_type is not None:
        declared_types = (
            expected_type if isinstance(expected_type, list) else [expected_type]
        )
        if not declared_types or any(
            item not in SUPPORTED_SCHEMA_TYPES for item in declared_types
        ):
            errors.append(f"schema_definition:{path}.type:invalid")

    for keyword in ("properties", "$defs"):
        children = schema.get(keyword)
        if children is None:
            continue
        if not isinstance(children, dict):
            errors.append(f"schema_definition:{path}.{keyword}:invalid")
            continue
        for name, child in children.items():
            errors.extend(
                _schema_definition_errors(child, f"{path}.{keyword}.{name}")
            )

    for keyword in ("allOf", "anyOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            errors.append(f"schema_definition:{path}.{keyword}:invalid")
            continue
        for index, branch in enumerate(branches):
            errors.extend(
                _schema_definition_errors(
                    branch, f"{path}.{keyword}[{index}]"
                )
            )

    if "items" in schema:
        errors.extend(
            _schema_definition_errors(schema["items"], f"{path}.items")
        )
    if "additionalProperties" in schema and not isinstance(
        schema["additionalProperties"], bool
    ):
        errors.append(f"schema_definition:{path}.additionalProperties:invalid")
    if "required" in schema and (
        not isinstance(schema["required"], list)
        or any(not isinstance(item, str) for item in schema["required"])
    ):
        errors.append(f"schema_definition:{path}.required:invalid")
    if "enum" in schema and not isinstance(schema["enum"], list):
        errors.append(f"schema_definition:{path}.enum:invalid")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            errors.append(f"schema_definition:{path}.pattern:invalid")
    for keyword in ("minimum", "maximum", "minItems"):
        if keyword in schema and (
            not isinstance(schema[keyword], (int, float))
            or isinstance(schema[keyword], bool)
        ):
            errors.append(f"schema_definition:{path}.{keyword}:invalid")
    for keyword in ("$schema", "$id", "$ref", "title", "description"):
        if keyword in schema and not isinstance(schema[keyword], str):
            errors.append(f"schema_definition:{path}.{keyword}:invalid")
    return errors


def _validate_schema_value(value: Any, schema: dict[str, Any]) -> list[str]:
    definition_errors = _schema_definition_errors(schema)
    if definition_errors:
        return definition_errors
    return work_order_builder._validate_schema_fragment(
        value, schema, "$", root_schema=schema
    )


def _schema_errors(value: Any, path: Path) -> list[str]:
    return _validate_schema_value(value, _load_schema(path))


def _forbidden_location_fields(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in FORBIDDEN_LOCATION_KEYS:
                errors.append(f"forbidden_location_field:{child_path}")
            errors.extend(_forbidden_location_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_forbidden_location_fields(child, f"{path}[{index}]"))
    return errors


def _relative_changed_path(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _WINDOWS_DRIVE_PATH_RE.match(value)
        or unicodedata.normalize("NFC", value) != value
    ):
        return False
    candidate = PurePosixPath(value)
    return (
        not candidate.is_absolute()
        and value != "."
        and all(part not in {"", ".", ".."} for part in candidate.parts)
        and all(
            part.casefold() not in VCS_CONTROL_PATH_COMPONENTS
            for part in candidate.parts
        )
        and candidate.as_posix() == value
    )


def _path_identity(value: str) -> str:
    """Conservative identity for case-insensitive host filesystem safety."""

    return unicodedata.normalize("NFC", value).casefold()


def _result_structure_errors(value: Any) -> list[str]:
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_RESULT_NESTING_DEPTH:
            return ["result_structure_too_deep"]
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return []


def _freshness_window_errors(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    timestamps: dict[str, datetime] = {}
    errors: list[str] = []
    for field in ("issued_at", "expires_at"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str) or not _UTC_TIMESTAMP_RE.fullmatch(
            timestamp
        ):
            errors.append(f"freshness_window_invalid:{field}")
            continue
        try:
            timestamps[field] = datetime.strptime(
                timestamp, "%Y-%m-%dT%H:%M:%SZ"
            )
        except ValueError:
            errors.append(f"freshness_window_invalid:{field}")
    if len(timestamps) == 2:
        duration = (
            timestamps["expires_at"] - timestamps["issued_at"]
        ).total_seconds()
        if duration <= 0:
            errors.append("freshness_window_invalid:ordering")
        elif duration > MAX_EXECUTION_WINDOW_SECONDS:
            errors.append("freshness_window_invalid:too_long")
    return errors


def _input_artifact_errors(
    value: Any, artifacts: Mapping[str, bytes] | None
) -> list[str]:
    if artifacts is None or not isinstance(value, dict):
        return []
    if not isinstance(artifacts, Mapping):
        return ["artifact_bytes_mapping_invalid"]

    errors: list[str] = []
    artifact_keys: set[str] = set()
    for artifact_id in artifacts:
        if not isinstance(artifact_id, str):
            errors.append("artifact_bytes_key_invalid")
        else:
            artifact_keys.add(artifact_id)
    expected_ids: set[str] = set()
    for field in INPUT_ARTIFACT_FIELDS:
        descriptor = value.get(field)
        if not isinstance(descriptor, dict):
            continue
        artifact_id = descriptor.get("artifact_id")
        if not isinstance(artifact_id, str):
            continue
        expected_ids.add(artifact_id)
        raw = artifacts.get(artifact_id)
        if not isinstance(raw, bytes):
            errors.append(f"artifact_bytes_missing_or_invalid:{artifact_id}")
            continue
        if len(raw) > INPUT_ARTIFACT_LIMITS[field]:
            errors.append(f"artifact_too_large:{artifact_id}")
        if descriptor.get("size_bytes") != len(raw):
            errors.append(f"artifact_size_mismatch:{artifact_id}")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if descriptor.get("sha256") != digest:
            errors.append(f"artifact_digest_mismatch:{artifact_id}")
    for artifact_id in sorted(artifact_keys - expected_ids):
        errors.append(f"unexpected_artifact_bytes:{artifact_id}")
    return errors


def validate_input_envelope(
    value: Any, *, artifacts: Mapping[str, bytes] | None = None
) -> dict[str, Any]:
    """Validate one host-originated, path-free worker input envelope."""

    errors = _schema_errors(value, INPUT_SCHEMA_PATH)
    errors.extend(_forbidden_location_fields(value))
    errors.extend(_freshness_window_errors(value))
    errors.extend(_input_artifact_errors(value, artifacts))
    if isinstance(value, dict):
        approved = value.get("approved_work_order")
        repository = value.get("repository_snapshot")
        result_contract = value.get("result_contract")
        descriptors = [approved, repository, result_contract]
        if all(isinstance(item, dict) for item in descriptors):
            artifact_ids = [str(item.get("artifact_id") or "") for item in descriptors]
            if len(set(artifact_ids)) != len(artifact_ids):
                errors.append("transport_artifact_id_reused")
            if approved.get("sha256") != value.get("work_order_digest"):
                errors.append("work_order_descriptor_digest_mismatch")
    if errors:
        raise IsolatedWorkerTransportError("isolated_worker_input_invalid", errors)
    return json.loads(json.dumps(value))


def _validate_patch(patch: Any, changed_paths: Any) -> list[str]:
    """Validate typed, complete regular-text-file operations and their path set."""

    if patch is None:
        return []
    if not isinstance(patch, dict):
        return ["patch_invalid"]
    errors: list[str] = []
    changes = patch.get("changes")
    if not isinstance(changes, list):
        return ["patch_changes_invalid"]

    patch_paths: list[str] = []
    total_content_bytes = 0
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            errors.append(f"file_change_invalid:$[{index}]")
            continue
        path = change.get("relative_path")
        if not _relative_changed_path(path):
            errors.append(f"file_change_path_invalid:$[{index}]")
        else:
            patch_paths.append(path)

        operation = change.get("operation")
        if operation not in {"create", "replace"}:
            continue
        encoded = change.get("content_base64")
        if not isinstance(encoded, str):
            errors.append(f"file_change_content_invalid:$[{index}]")
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            errors.append(f"file_change_content_invalid:$[{index}]")
            continue
        total_content_bytes += len(raw)
        if change.get("size_bytes") != len(raw):
            errors.append(f"file_change_size_mismatch:$[{index}]")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if change.get("sha256") != digest:
            errors.append(f"file_change_digest_mismatch:$[{index}]")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"file_change_binary_content_forbidden:$[{index}]")
        else:
            if "\x00" in decoded:
                errors.append(f"file_change_binary_content_forbidden:$[{index}]")

    if total_content_bytes > MAX_PATCH_BYTES:
        errors.append("patch_too_large")

    seen_patch_paths: set[str] = set()
    seen_patch_identities: set[str] = set()
    for path in patch_paths:
        if path in seen_patch_paths:
            errors.append(f"file_change_path_duplicate:{path}")
        elif _path_identity(path) in seen_patch_identities:
            errors.append(f"file_change_path_collision:{path}")
        seen_patch_paths.add(path)
        seen_patch_identities.add(_path_identity(path))

    if isinstance(changed_paths, list):
        declared_paths = {
            path for path in changed_paths if _relative_changed_path(path)
        }
        for path in sorted(declared_paths - seen_patch_paths):
            errors.append(f"changed_path_set_mismatch:missing_file_change:{path}")
        for path in sorted(seen_patch_paths - declared_paths):
            errors.append(f"changed_path_set_mismatch:undeclared_file_change:{path}")
    return errors


def validate_result_envelope(value: Any) -> dict[str, Any]:
    """Validate an inert worker result; no value is interpreted as a host path."""

    errors = _result_structure_errors(value)
    if errors:
        raise IsolatedWorkerTransportError("isolated_worker_result_invalid", errors)
    errors = _schema_errors(value, RESULT_SCHEMA_PATH)
    errors.extend(_forbidden_location_fields(value))
    errors.extend(_freshness_window_errors(value))
    if isinstance(value, dict):
        result = value.get("worker_result")
        patch = value.get("patch")
        if isinstance(result, dict):
            changed_paths = result.get("changed_paths")
            if isinstance(changed_paths, list):
                seen_changed_paths: set[str] = set()
                seen_changed_identities: set[str] = set()
                for index, changed_path in enumerate(changed_paths):
                    if not _relative_changed_path(changed_path):
                        errors.append(f"changed_path_invalid:$[{index}]")
                    elif changed_path in seen_changed_paths:
                        errors.append(f"changed_path_duplicate:{changed_path}")
                    elif _path_identity(changed_path) in seen_changed_identities:
                        errors.append(f"changed_path_collision:{changed_path}")
                    else:
                        seen_changed_paths.add(changed_path)
                        seen_changed_identities.add(_path_identity(changed_path))
                if changed_paths and patch is None:
                    errors.append("patch_required_for_changed_paths")
                if not changed_paths and patch is not None:
                    errors.append("patch_for_unchanged_result")
        errors.extend(
            _validate_patch(
                patch,
                result.get("changed_paths") if isinstance(result, dict) else None,
            )
        )
    if errors:
        raise IsolatedWorkerTransportError("isolated_worker_result_invalid", errors)
    return json.loads(json.dumps(value))


def validate_result_bytes(raw: bytes) -> dict[str, Any]:
    """Bound a captured untrusted result before decoding or parsing it."""

    if not isinstance(raw, bytes):
        raise IsolatedWorkerTransportError(
            "isolated_worker_result_malformed", ["result_bytes_invalid"]
        )
    if len(raw) > MAX_RESULT_ENVELOPE_BYTES:
        raise IsolatedWorkerTransportError(
            "isolated_worker_result_too_large", ["result_envelope_too_large"]
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise IsolatedWorkerTransportError(
            "isolated_worker_result_malformed", ["result_json_invalid"]
        ) from exc
    try:
        return validate_result_envelope(value)
    except RecursionError as exc:
        raise IsolatedWorkerTransportError(
            "isolated_worker_result_invalid", ["result_structure_too_deep"]
        ) from exc


def _trusted_clock_errors(value: dict[str, Any], observed_at: Any) -> list[str]:
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        return ["trusted_clock_invalid"]
    try:
        issued_at = datetime.strptime(
            value["issued_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        expires_at = datetime.strptime(
            value["expires_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return ["trusted_clock_window_invalid"]
    instant = observed_at.astimezone(timezone.utc)
    if instant < issued_at:
        return ["freshness_window_not_yet_valid"]
    if instant >= expires_at:
        return ["freshness_window_expired"]
    return []


def validate_exchange(
    input_envelope: Any,
    result_envelope: Any,
    *,
    result_received_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind one result and its trusted first-byte receipt time to one input."""

    incoming = validate_input_envelope(input_envelope)
    outgoing = validate_result_envelope(result_envelope)
    errors: list[str] = []
    for field in (
        "transfer_id",
        "execution_id",
        "issued_at",
        "expires_at",
        "task_id",
        "request_id",
        "run_id",
        "step_id",
        "work_order_digest",
    ):
        if outgoing.get(field) != incoming.get(field):
            errors.append(f"exchange_binding_mismatch:{field}")
    if outgoing.get("managed_worker_generation") != incoming.get(
        "managed_worker_generation"
    ):
        errors.append("exchange_binding_mismatch:managed_worker_generation")
    if outgoing.get("base_revision") != incoming.get("repository_snapshot", {}).get(
        "base_revision"
    ):
        errors.append("exchange_binding_mismatch:base_revision")
    errors.extend(_trusted_clock_errors(incoming, result_received_at))
    if errors:
        raise IsolatedWorkerTransportError("isolated_worker_exchange_invalid", errors)
    return incoming, outgoing
