#!/usr/bin/env python3
"""Static security contract for the repository-owned CodeQL setup."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
MODEL_PACK = ROOT / ".github" / "codeql" / "extensions" / "saihai-python-models"
CODEQL_ACTION_SHA = "99df26d4f13ea111d4ec1a7dddef6063f76b97e9"
CHECKOUT_ACTION_SHA = "34e114876b0b11c390a56381ad16ebd13914f8d5"


def test_advanced_codeql_contract() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required = (
        "push:\n    branches: [main]",
        "pull_request:\n    branches: [main]",
        "schedule:",
        "workflow_dispatch:",
        "contents: read",
        "security-events: write",
        "language: [actions, python]",
        f"actions/checkout@{CHECKOUT_ACTION_SHA}",
        f"github/codeql-action/init@{CODEQL_ACTION_SHA}",
        f"github/codeql-action/analyze@{CODEQL_ACTION_SHA}",
    )
    for marker in required:
        assert marker in workflow, marker

    forbidden = (
        "disable-default-queries",
        "query-filters",
        "paths-ignore",
        "continue-on-error",
        "security-events: read",
    )
    for marker in forbidden:
        assert marker not in workflow, marker


def test_local_model_pack_remains_discoverable() -> None:
    pack = (MODEL_PACK / "codeql-pack.yml").read_text(encoding="utf-8")
    model = (MODEL_PACK / "models" / "frontdoor.yaml").read_text(encoding="utf-8")

    assert "extensionTargets:" in pack
    assert "codeql/python-all" in pack
    assert "dataExtensions:" in pack
    assert "models/**/*.yaml" in pack
    assert "extensible: barrierModel" in model
    assert '"host_state_root"' in model
    assert "Member[configured_state_root].ReturnValue" in model
    assert '"safe_paths"' in model
    assert "Member[confined_state_path].ReturnValue" in model
    assert "path-injection" in model


if __name__ == "__main__":
    test_advanced_codeql_contract()
    test_local_model_pack_remains_discoverable()
    print("test_codeql_workflow: ok")
