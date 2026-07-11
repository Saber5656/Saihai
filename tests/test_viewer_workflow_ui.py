#!/usr/bin/env python3
"""Static contract checks for the workflow-run viewer panel."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"


def read_index() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_workflow_panel_contract_markers() -> None:
    html = read_index()
    required = [
        'id="workflow-runs" data-panel="workflow-runs"',
        'id="wf-state-filter"',
        'id="wf-session-only"',
        'id="wf-refresh"',
        'id="wf-lock-banner" data-role="lock-banner"',
        'id="wf-run-table"',
        'id="wf-run-detail" data-role="run-detail"',
        '<option value="remediating">remediating</option>',
        "const WF_STUCK_SECONDS = 1800;",
        "fetchJSON(`/api/workflow-runs${workflowQuery()}`)",
        'fetchJSON("/api/workflow-lock")',
        "fetchJSON(`/api/workflow-run?",
        "function clearRunDetail()",
        "function workflowFilterChanged()",
        '$("wf-state-filter").addEventListener("change", workflowFilterChanged);',
        '$("wf-session-only").addEventListener("change", workflowFilterChanged);',
    ]
    for marker in required:
        assert marker in html, marker


def test_empty_sessions_still_start_workflow_polling() -> None:
    html = read_index()
    empty_sessions_block = re.search(
        r"if \(data\.sessions\.length === 0\) \{(?P<body>.*?)\n  \}",
        html,
        re.S,
    )
    assert empty_sessions_block, "empty sessions block"
    body = empty_sessions_block.group("body")
    assert "currentSession = null;" in body
    assert "startWorkflowPolling();" in body
    assert body.index("startWorkflowPolling();") < body.index("return;")


def test_workflow_badge_classes_are_stable() -> None:
    html = read_index()
    for css_class in [
        "wf-badge-created",
        "wf-badge-queued",
        "wf-badge-provider",
        "wf-badge-validating",
        "wf-badge-human",
        "wf-badge-complete",
        "wf-badge-failed",
        "wf-badge-aborted",
        "wf-stuck",
    ]:
        assert css_class in html, css_class


def test_workflow_detail_sections_are_stable() -> None:
    html = read_index()
    for section in [
        "summary",
        "terminal",
        "work-order",
        "report",
        "evidence",
        "transitions",
        "rejections",
    ]:
        assert f'detailSection("{section}"' in html, section


def test_viewer_does_not_add_workflow_mutations() -> None:
    html = read_index()
    script = re.search(r"<script>(.*)</script>", html, re.S)
    assert script, "script block"
    js = script.group(1)
    forbidden = [
        "method: \"POST\"",
        "method:'POST'",
        "/frontdoor/approve",
        "/orchestrator/runs/",
        "/provider/",
    ]
    for marker in forbidden:
        assert marker not in js, marker


if __name__ == "__main__":
    test_workflow_panel_contract_markers()
    test_empty_sessions_still_start_workflow_polling()
    test_workflow_badge_classes_are_stable()
    test_workflow_detail_sections_are_stable()
    test_viewer_does_not_add_workflow_mutations()
    print('{"result":"pass","cases":5}')
