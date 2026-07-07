#!/usr/bin/env python3
"""Regression tests for organization mode decisions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "configure_organization.py"


def classify(prompt: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), "classify", "--prompt", prompt],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def runtime_paths() -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), "runtime-paths"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def assert_decision(prompt: str, **expected) -> None:
    result = classify(prompt)
    for key, value in expected.items():
        actual = result.get(key)
        assert actual == value, f"{prompt}: {key} expected {value!r}, got {actual!r}"
    assert result["task_required"] is True
    assert result["hook_policy"]["mode"] == "observer"
    assert result["hook_policy"]["hard_block"] is False


def main() -> None:
    assert_decision(
        "最近の天気予報を調べる",
        mode="fast",
        clarification_required=True,
        main_agent_can_execute=True,
    )
    assert classify("最近の天気予報を調べる")["missing_information"] == ["location"]
    assert_decision(
        "レッドブルとモンスターどちらがエナジードリンクとして人気か",
        organization_state="enabled",
        mode="fast",
        role_dispatch_required=False,
        review_required="optional",
        main_agent_can_execute=True,
    )
    assert_decision(
        "COMMON-AGENTS.md の組織運用ルールを薄くしたい",
        organization_state="maintenance",
        mode="fast",
        organization_flow_enabled=False,
        main_agent_can_execute=True,
    )
    assert_decision(
        "認証ロジックを変更してテストとレビューまで実行して",
        organization_state="enabled",
        mode="strict",
        role_dispatch_required=True,
        review_required="required",
        main_agent_can_execute=False,
    )
    assert_decision(
        "React と Vue のどちらを採用すべきか設計判断して",
        organization_state="enabled",
        mode="strict",
        role_dispatch_required=True,
        review_required="required",
        main_agent_can_execute=False,
    )
    paths = runtime_paths()["runtime_paths"]
    assert paths["itb_builder"]["exists"] is True
    assert paths["itb_hooks"]["exists"] is True
    assert paths["itd_monitor"]["exists"] is True
    assert paths["workflow_selector"]["exists"] is True
    assert paths["workflow_frontdoor"]["exists"] is True
    assert paths["workflow_frontdoor_server"]["exists"] is True
    assert paths["saihai_cli"]["exists"] is True
    assert "organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py" in paths["itb_builder"]["path"]
    assert "organization/runtime/workflows/scripts/workflow_selector.py" in paths["workflow_selector"]["path"]
    assert "organization/runtime/workflows/scripts/frontdoor_orchestrator.py" in paths["workflow_frontdoor"]["path"]
    assert "organization/runtime/workflows/scripts/frontdoor_server.py" in paths["workflow_frontdoor_server"]["path"]
    assert "scripts/saihai.py" in paths["saihai_cli"]["path"]
    print(json.dumps({"result": "pass", "cases": 7}, ensure_ascii=False))


if __name__ == "__main__":
    main()
