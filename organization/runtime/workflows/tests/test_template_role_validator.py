#!/usr/bin/env python3
"""Acceptance tests for active workflow template role resolution."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "organization/runtime/workflows/scripts/template_role_validator.py"
VALIDATE_ALL_SCRIPT = ROOT / "scripts/validate_all.py"
sys.path.insert(0, str(ROOT))
import directory_paths  # noqa: E402


def load_validator_module():
    spec = importlib.util.spec_from_file_location("template_role_validator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


validator = load_validator_module()


def load_validate_all_module():
    spec = importlib.util.spec_from_file_location("validate_all", VALIDATE_ALL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with patch.object(directory_paths, "load_environment", return_value={}):
        spec.loader.exec_module(module)
    return module


validate_all = load_validate_all_module()


def write_fixture(
    repo_root: Path,
    *,
    role_id: str,
    model_status: str | None = "active",
    include_status_column: bool = True,
) -> tuple[Path, Path, Path, Path]:
    template_path = repo_root / "organization/runtime/workflows/templates/fixture.yaml"
    template_path.parent.mkdir(parents=True)
    template_path.write_text(
        json.dumps(
            {
                "workflow_id": "template_role_fixture",
                "steps": [{"id": "implement", "role": role_id}],
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
                        "workflow_id": "template_role_fixture",
                        "status": "active",
                        "path": "organization/runtime/workflows/templates/fixture.yaml",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    roles_root = repo_root / "organization/roles"
    (roles_root / role_id).mkdir(parents=True)
    role_registry_path = repo_root / "role-agent-registry.yaml"
    role_registry_path.write_text(
        json.dumps({"role_layers": {role_id: "worker"}}), encoding="utf-8"
    )
    model_registry_path = repo_root / "model-registry.md"
    headers = ["agent_id"]
    values = [role_id]
    if include_status_column:
        headers.append("status")
        values.append("" if model_status is None else model_status)
    headers.append("primary_model")
    values.append("test-model")
    model_registry_path.write_text(
        "\n".join(
            [
                "## Model Routing",
                "",
                "| " + " | ".join(headers) + " |",
                "| " + " | ".join("---" for _ in headers) + " |",
                "| " + " | ".join(values) + " |",
            ]
        ),
        encoding="utf-8",
    )
    return workflow_registry_path, roles_root, role_registry_path, model_registry_path


def write_unknown_role_fixture(repo_root: Path) -> tuple[Path, Path, Path, Path]:
    paths = write_fixture(repo_root, role_id="tech-does-not-exist")
    paths[1].joinpath("tech-does-not-exist").rmdir()
    paths[2].write_text(json.dumps({"role_layers": {}}), encoding="utf-8")
    paths[3].write_text(
        "\n".join(
            [
                "## Model Routing",
                "",
                "| agent_id | status | primary_model |",
                "| --- | --- | --- |",
                "| known-role | active | test-model |",
            ]
        ),
        encoding="utf-8",
    )
    return paths


def test_active_templates_resolve_all_roles() -> None:
    result = validator.validate_template_roles()
    assert result["decision"] == "ok", result["errors"]
    assert result["active_template_count"] == 6
    assert result["checked_step_count"] == 21


def test_unknown_role_fails_with_typed_step_errors() -> None:
    with tempfile.TemporaryDirectory() as raw_temp:
        repo_root = Path(raw_temp)
        paths = write_unknown_role_fixture(repo_root)

        result = validator.validate_template_roles(
            repo_root=repo_root,
            workflow_registry_path=paths[0],
            roles_root=paths[1],
            role_agent_registry_path=paths[2],
            model_registry_path=paths[3],
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
        assert error["template_file"] == "organization/runtime/workflows/templates/fixture.yaml"
        assert error["step_id"] == "implement"
        assert error["step_index"] == 0
        assert error["role_id"] == "tech-does-not-exist"


def test_non_active_model_registry_role_fails_with_typed_error() -> None:
    for model_status in ("reference", "deprecated"):
        with tempfile.TemporaryDirectory() as raw_temp:
            repo_root = Path(raw_temp)
            paths = write_fixture(
                repo_root, role_id="non-active-role", model_status=model_status
            )
            result = validator.validate_template_roles(
                repo_root=repo_root,
                workflow_registry_path=paths[0],
                roles_root=paths[1],
                role_agent_registry_path=paths[2],
                model_registry_path=paths[3],
            )

        assert result["decision"] == "error"
        assert [error["code"] for error in result["errors"]] == [
            "model_registry_entry_not_active"
        ]
        assert result["errors"][0]["type"] == "template_role_resolution_error"
        assert result["errors"][0]["role_id"] == "non-active-role"
        assert model_status in result["errors"][0]["detail"]


def test_missing_or_invalid_model_registry_status_fails_closed() -> None:
    cases = [
        (False, "active", "model_registry_status_missing"),
        (True, None, "model_registry_status_missing"),
        (True, "selected", "model_registry_status_invalid"),
    ]
    for include_status_column, model_status, expected_code in cases:
        with tempfile.TemporaryDirectory() as raw_temp:
            repo_root = Path(raw_temp)
            paths = write_fixture(
                repo_root,
                role_id="fixture-role",
                model_status=model_status,
                include_status_column=include_status_column,
            )
            result = validator.validate_template_roles(
                repo_root=repo_root,
                workflow_registry_path=paths[0],
                roles_root=paths[1],
                role_agent_registry_path=paths[2],
                model_registry_path=paths[3],
            )

        assert result["decision"] == "error"
        source_errors = [
            error
            for error in result["errors"]
            if error["type"] == "template_role_validation_source_error"
        ]
        assert [error["code"] for error in source_errors] == [expected_code]


def test_cli_and_contract_runner_propagate_unknown_role_failure() -> None:
    with tempfile.TemporaryDirectory() as raw_temp:
        repo_root = Path(raw_temp)
        paths = write_unknown_role_fixture(repo_root)
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo_root),
            "--workflow-registry",
            str(paths[0]),
            "--roles-root",
            str(paths[1]),
            "--role-agent-registry",
            str(paths[2]),
            "--model-registry",
            str(paths[3]),
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(completed.stdout)
        contract_result = validate_all.run_contract(command)

    assert completed.returncode != 0
    assert payload["decision"] == "error"
    resolution_errors = [
        error for error in payload["errors"] if error["type"] == "template_role_resolution_error"
    ]
    assert {error["code"] for error in resolution_errors} == {
        "role_directory_missing",
        "role_registry_entry_missing",
        "model_registry_entry_missing",
    }
    assert contract_result["result"] == "fail"
    assert contract_result["detail"] == f"exit:{completed.returncode}"


def main() -> None:
    tests = [
        test_active_templates_resolve_all_roles,
        test_unknown_role_fails_with_typed_step_errors,
        test_non_active_model_registry_role_fails_with_typed_error,
        test_missing_or_invalid_model_registry_status_fails_closed,
        test_cli_and_contract_runner_propagate_unknown_role_failure,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
