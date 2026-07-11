#!/usr/bin/env python3
"""Canonical normalized provider-evidence schema validation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas/provider-evidence.schema.json"
FORBIDDEN_RAW_CONTENT_KEYS = {
    "pane-output",
    "pane_output",
    "prompt",
    "provider_output",
    "raw_prompt",
    "raw_provider_output",
    "raw_transcript",
    "raw_transcript_text",
    "stderr",
    "stderr_text",
    "stdout",
    "stdout_text",
    "transcript_content",
}

_SCHEMA_CACHE: dict[str, Any] | None = None


def provider_evidence_schema() -> dict[str, Any]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with SCHEMA_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"expected object schema: {SCHEMA_PATH}")
        _SCHEMA_CACHE = payload
    return _SCHEMA_CACHE


def _type_matches(value: Any, expected: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return True


def _child_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent != "$" else f"$.{key}"


def _schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _type_matches(value, expected_type):
        return [f"schema:{path}:type"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"schema:{path}:const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"schema:{path}:enum")
    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"schema:{path}:min_length")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            errors.append(f"schema:{path}:pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, int | float) and value < minimum:
            errors.append(f"schema:{path}:minimum")
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for field in required:
                if field not in value:
                    errors.append(f"schema:{_child_path(path, str(field))}:required")
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(_schema_errors(value[key], child_schema, _child_path(path, str(key))))
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                errors.append(f"schema:{_child_path(path, str(key))}:additional_property")
    return errors


def _raw_content_errors(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = _child_path(path, str(key))
            if key in FORBIDDEN_RAW_CONTENT_KEYS:
                errors.append(f"forbidden_raw_provider_field:{child_path}")
            errors.extend(_raw_content_errors(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_raw_content_errors(child, f"{path}[{index}]"))
    return errors


def validate_provider_evidence_schema(value: Any) -> list[str]:
    errors = _schema_errors(value, provider_evidence_schema())
    errors.extend(_raw_content_errors(value))
    return list(dict.fromkeys(errors))
