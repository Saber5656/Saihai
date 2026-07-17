#!/usr/bin/env python3
"""Acceptance tests for active workflow template role resolution."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "organization/runtime/workflows/scripts/template_role_validator.py"


def load_validator_module():
    spec = importlib.util.spec_from_file_location("template_role_validator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


validator = load_validator_module()


def test_active_templates_resolve_all_roles() -> None:
    result = validator.validate_template_roles()
    assert result["decision"] == "ok", result["errors"]
    assert result["active_template_count"] == 6
    assert result["checked_step_count"] > 0


def test_unknown_role_fails_with_typed_step_errors() -> None:
    with tempfile.TemporaryDirectory() as raw_temp:
        repo_root = Path(raw_temp)
        template_path = repo_root / "organization/runtime/workflows/templates/unknown.yaml"
        template_path.parent.mkdir(parents=True)
        template_path.write_text(
            json.dumps(
                {
                    "workflow_id": "unknown_role_fixture",
                    "steps": [{"id": "implement", "role": "tech-does-not-exist"}],
                }
            ),
            encoding="utf-8",
        )
        workflow_registry_path = repo_root / "organization/runtime/workflows/registry.yaml"
        workflow_registry_path.write_text(
            json.dumps(
                {
                    "templates": [
                        {
                            "workflow_id": "unknown_role_fixture",
                            "status": "active",
                            "path": "organization/runtime/workflows/templates/unknown.yaml",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        roles_root = repo_root / "organization/roles"
        roles_root.mkdir(parents=True)
        role_registry_path = repo_root / "role-agent-registry.yaml"
        role_registry_path.write_text(json.dumps({"role_layers": {}}), encoding="utf-8")
        model_registry_path = repo_root / "model-registry.md"
        model_registry_path.write_text(
            "\n".join(
                [
                    "## Model Routing",
                    "",
                    "| agent_id | primary_model |",
                    "| --- | --- |",
                    "| known-role | test-model |",
                ]
            ),
            encoding="utf-8",
        )

        result = validator.validate_template_roles(
            repo_root=repo_root,
            workflow_registry_path=workflow_registry_path,
            roles_root=roles_root,
            role_agent_registry_path=role_registry_path,
            model_registry_path=model_registry_path,
        )

    assert result["decision"] == "error"
    resolution_errors = [
        error for error in result["errors"] if error["type"] == "template_role_resolution_error"
    ]
    assert {error["code"] for error in resolution_errors} == {
        "role_directory_missing",
        "role_registry_entry_missing",
        "model_registry_entry_missing",
    }
    for error in resolution_errors:
        assert error["template_file"] == "organization/runtime/workflows/templates/unknown.yaml"
        assert error["step_id"] == "implement"
        assert error["step_index"] == 0
        assert error["role_id"] == "tech-does-not-exist"


def main() -> None:
    tests = [
        test_active_templates_resolve_all_roles,
        test_unknown_role_fails_with_typed_step_errors,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
