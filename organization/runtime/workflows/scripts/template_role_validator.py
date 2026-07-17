#!/usr/bin/env python3
"""Validate active workflow template roles against organization registries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_REGISTRY = REPO_ROOT / "organization/runtime/workflows/registry.yaml"
ROLES_ROOT = REPO_ROOT / "organization/roles"
ROLE_AGENT_REGISTRY = (
    REPO_ROOT / "organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml"
)
MODEL_REGISTRY = (
    REPO_ROOT / "organization/runtime/infra-team-bootstrap/references/model-registry.md"
)


def relative_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def source_error(path: Path, repo_root: Path, code: str, detail: str) -> dict[str, Any]:
    return {
        "type": "template_role_validation_source_error",
        "code": code,
        "path": relative_path(path, repo_root),
        "detail": detail,
    }


def load_json_object(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("top-level value must be an object")
    return parsed


def markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_model_roles(path: Path) -> set[str]:
    """Return agent ids from the model registry's Model Routing table."""
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = -1
    header: list[str] = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = markdown_cells(line)
        if "agent_id" in cells and "primary_model" in cells:
            header_index = index
            header = cells
            break
    if header_index < 0:
        raise ValueError("Model Routing table header is missing")

    agent_index = header.index("agent_id")
    roles: set[str] = set()
    for line in lines[header_index + 1 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = markdown_cells(line)
        if cells and all(cell.replace(":", "").strip("-") == "" for cell in cells):
            continue
        if len(cells) != len(header):
            raise ValueError(
                f"Model Routing row has {len(cells)} columns; expected {len(header)}: {line}"
            )
        role_id = cells[agent_index]
        if not role_id:
            raise ValueError("Model Routing row has an empty agent_id")
        if role_id in roles:
            raise ValueError(f"Model Routing has duplicate agent_id: {role_id}")
        roles.add(role_id)
    if not roles:
        raise ValueError("Model Routing table has no role entries")
    return roles


def role_resolution_error(
    *,
    code: str,
    template_path: Path,
    repo_root: Path,
    step_id: Any,
    step_index: int,
    role_id: Any,
    detail: str,
) -> dict[str, Any]:
    return {
        "type": "template_role_resolution_error",
        "code": code,
        "template_file": relative_path(template_path, repo_root),
        "step_id": step_id,
        "step_index": step_index,
        "role_id": role_id,
        "detail": detail,
    }


def validate_template_roles(
    *,
    repo_root: Path = REPO_ROOT,
    workflow_registry_path: Path = WORKFLOW_REGISTRY,
    roles_root: Path = ROLES_ROOT,
    role_agent_registry_path: Path = ROLE_AGENT_REGISTRY,
    model_registry_path: Path = MODEL_REGISTRY,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []

    try:
        workflow_registry = load_json_object(workflow_registry_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(
            source_error(workflow_registry_path, repo_root, "workflow_registry_invalid", str(exc))
        )
        workflow_registry = {}

    try:
        role_agent_registry = load_json_object(role_agent_registry_path)
        role_layers = role_agent_registry.get("role_layers")
        if not isinstance(role_layers, dict):
            raise ValueError("role_layers must be an object")
        registered_roles = set(role_layers)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(
            source_error(role_agent_registry_path, repo_root, "role_registry_invalid", str(exc))
        )
        registered_roles = set()

    try:
        model_roles = load_model_roles(model_registry_path)
    except (OSError, ValueError) as exc:
        errors.append(
            source_error(model_registry_path, repo_root, "model_registry_invalid", str(exc))
        )
        model_roles = set()

    if not roles_root.is_dir():
        errors.append(source_error(roles_root, repo_root, "roles_root_missing", "not a directory"))

    templates = workflow_registry.get("templates")
    if not isinstance(templates, list):
        errors.append(
            source_error(
                workflow_registry_path,
                repo_root,
                "workflow_registry_templates_invalid",
                "templates must be an array",
            )
        )
        templates = []
    active_entries = [
        entry for entry in templates if isinstance(entry, dict) and entry.get("status") == "active"
    ]
    if not active_entries:
        errors.append(
            source_error(
                workflow_registry_path,
                repo_root,
                "active_templates_missing",
                "no templates with status active",
            )
        )

    checked_steps = 0
    for entry in active_entries:
        template_ref = entry.get("path")
        if not isinstance(template_ref, str) or not template_ref:
            errors.append(
                source_error(
                    workflow_registry_path,
                    repo_root,
                    "active_template_path_invalid",
                    f"workflow {entry.get('workflow_id')!r} has no valid path",
                )
            )
            continue
        template_path = repo_root / template_ref
        try:
            template = load_json_object(template_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(source_error(template_path, repo_root, "template_invalid", str(exc)))
            continue
        steps = template.get("steps")
        if not isinstance(steps, list):
            errors.append(source_error(template_path, repo_root, "template_steps_invalid", "steps must be an array"))
            continue
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(
                    role_resolution_error(
                        code="template_step_invalid",
                        template_path=template_path,
                        repo_root=repo_root,
                        step_id=None,
                        step_index=step_index,
                        role_id=None,
                        detail="step must be an object",
                    )
                )
                continue
            checked_steps += 1
            step_id = step.get("id")
            role_id = step.get("role")
            if not isinstance(role_id, str) or not role_id:
                errors.append(
                    role_resolution_error(
                        code="template_step_role_invalid",
                        template_path=template_path,
                        repo_root=repo_root,
                        step_id=step_id,
                        step_index=step_index,
                        role_id=role_id,
                        detail="steps[].role must be a non-empty string",
                    )
                )
                continue
            if not (roles_root / role_id).is_dir():
                errors.append(
                    role_resolution_error(
                        code="role_directory_missing",
                        template_path=template_path,
                        repo_root=repo_root,
                        step_id=step_id,
                        step_index=step_index,
                        role_id=role_id,
                        detail=f"organization/roles/{role_id}/ is missing",
                    )
                )
            if role_id not in registered_roles:
                errors.append(
                    role_resolution_error(
                        code="role_registry_entry_missing",
                        template_path=template_path,
                        repo_root=repo_root,
                        step_id=step_id,
                        step_index=step_index,
                        role_id=role_id,
                        detail="role id is absent from role-agent-registry role_layers",
                    )
                )
            if role_id not in model_roles:
                errors.append(
                    role_resolution_error(
                        code="model_registry_entry_missing",
                        template_path=template_path,
                        repo_root=repo_root,
                        step_id=step_id,
                        step_index=step_index,
                        role_id=role_id,
                        detail="role id is absent from model-registry Model Routing",
                    )
                )

    return {
        "schema_version": 1,
        "decision": "error" if errors else "ok",
        "validator": "active_template_role_resolution",
        "active_template_count": len(active_entries),
        "checked_step_count": checked_steps,
        "errors": errors,
    }


def main() -> None:
    result = validate_template_roles()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["decision"] == "ok" else 1)


if __name__ == "__main__":
    main()
