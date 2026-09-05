#!/usr/bin/env python3
"""Static security contract for the repository-owned CodeQL setup."""

from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "organization/runtime/workflows/scripts"))
from delivery_workflow_inventory import parse_workflow


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
    model = (MODEL_PACK / "models" / "frontdoor.yml").read_text(encoding="utf-8")

    assert "extensionTargets:" in pack
    assert "codeql/python-all" in pack
    assert "dataExtensions:" in pack
    assert "models/**/*.yml" in pack
    assert "extensible: barrierModel" in model
    assert '"host_state_root"' in model
    assert "Member[configured_state_root].ReturnValue" in model
    assert (
        '["host_state_root", "Member[resolve_configured_state_root].ReturnValue", "path-injection"]'
        in model
    )
    assert '"safe_paths"' in model
    assert "Member[confined_state_path].ReturnValue" in model
    assert "path-injection" in model


def test_actual_codeql_consumer_order_and_observation_boundaries() -> None:
    workflow = parse_workflow(WORKFLOW.read_text())
    assert set(workflow['on']) == {'push', 'pull_request', 'schedule', 'workflow_dispatch', 'merge_group'}
    assert workflow['permissions'] == {'contents': 'read'}
    job = workflow['jobs']['analyze']
    assert job['permissions'] == {'contents': 'read', 'security-events': 'write'}
    assert job['strategy']['matrix']['language'] == ['actions', 'python']
    steps = job['steps']
    acquire = next(i for i, x in enumerate(steps) if '--codeql-phase acquire' in x.get('run', ''))
    init = next(i for i, x in enumerate(steps) if x.get('id') == 'codeql-init')
    probe = next(i for i, x in enumerate(steps) if '--codeql-phase probe' in x.get('run', ''))
    analyze = next(i for i, x in enumerate(steps) if x.get('id') == 'codeql-analysis')
    observe = next(i for i, x in enumerate(steps) if '--codeql-phase observe' in x.get('run', ''))
    assert acquire < init < probe < analyze < observe
    assert steps[init]['with']['tools'].endswith('/bundle.tar.gz')
    assert steps[init]['with']['trap-caching'] == 'false'
    assert steps[init]['with']['dependency-caching'] == 'false'
    assert steps[analyze]['with'].get('upload', 'always') == 'always'
    assert steps[analyze]['with'].get('wait-for-processing', True) is True
    assert steps[analyze]['with'].get('upload-database', True) is True
    assert steps[observe]['if'] == 'always()'
    for step in steps:
        assert '${{ steps.' not in step.get('run', '')
        assert 'continue-on-error' not in step
    assert steps[probe]['env']['CODEQL_PATH'] == '${{ steps.codeql-init.outputs.codeql-path }}'
    assert '--codeql-path "$CODEQL_PATH"' in steps[probe]['run']
    artifact = steps[-1]
    assert artifact['uses'] == 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02'
    assert artifact['with']['retention-days'] == 14
    assert [line.rsplit('/', 1)[-1] for line in artifact['with']['path'].splitlines()] == ['receipt-acquire.json', 'receipt-probe.json', 'receipt-observe.json']
    assert 'matrix.language' in artifact['with']['name']


if __name__ == "__main__":
    test_advanced_codeql_contract()
    test_local_model_pack_remains_discoverable()
    test_actual_codeql_consumer_order_and_observation_boundaries()
    print("test_codeql_workflow: ok (3 direct tests)")
