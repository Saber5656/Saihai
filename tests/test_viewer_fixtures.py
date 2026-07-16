#!/usr/bin/env python3
"""Viewer/API contract tests over generated workflow-run artifact fixtures."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import tempfile
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "server.py"
GENERATOR_PATH = ROOT / "organization" / "runtime" / "workflows" / "tests" / "make_run_fixtures.py"
FIXTURE_ROOT = GENERATOR_PATH.parent / "fixtures" / "run-artifacts"
TRANSCRIPT_MARKER = "__FIXTURE_TRANSCRIPT__"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = load_module(SERVER_PATH, "saihai_fixture_server")
GENERATOR = load_module(GENERATOR_PATH, "saihai_fixture_generator")


def _replace_placeholders(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "corrupt-run.json":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        text = text.replace("__STATE_ROOT__", str(root)).replace("__REPO_ROOT__", str(ROOT))
        path.write_text(text, encoding="utf-8")


@contextmanager
def loaded_fixture(name: str):
    with tempfile.TemporaryDirectory(prefix=f"saihai-viewer-{name}-") as raw_tmp:
        root = Path(raw_tmp) / name
        shutil.copytree(FIXTURE_ROOT / name, root)
        _replace_placeholders(root)
        lock_dir = root / "locks" / "global-advisory.lock.d"
        if lock_dir.exists():
            os.utime(lock_dir, (1, 1))
        previous = os.environ.get("SAIHAI_ORCH_STATE_ROOT")
        os.environ["SAIHAI_ORCH_STATE_ROOT"] = str(root)
        try:
            yield root
        finally:
            if previous is None:
                os.environ.pop("SAIHAI_ORCH_STATE_ROOT", None)
            else:
                os.environ["SAIHAI_ORCH_STATE_ROOT"] = previous


def _single_run(name: str) -> tuple[Path, dict]:
    root = FIXTURE_ROOT / name / "runs"
    paths = [path for path in root.glob("*.json") if path.name != "corrupt-run.json"]
    assert len(paths) == 1, (name, paths)
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


@contextmanager
def merged_fixtures(names: tuple[str, ...]):
    with tempfile.TemporaryDirectory(prefix="saihai-viewer-merged-") as raw_tmp:
        root = Path(raw_tmp) / "merged"
        root.mkdir()
        for name in names:
            shutil.copytree(FIXTURE_ROOT / name, root, dirs_exist_ok=True)
        _replace_placeholders(root)
        previous = os.environ.get("SAIHAI_ORCH_STATE_ROOT")
        os.environ["SAIHAI_ORCH_STATE_ROOT"] = str(root)
        try:
            yield root
        finally:
            if previous is None:
                os.environ.pop("SAIHAI_ORCH_STATE_ROOT", None)
            else:
                os.environ["SAIHAI_ORCH_STATE_ROOT"] = previous


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_ref_path(value: str, state_root: Path) -> Path:
    prefix = "__STATE_ROOT__/"
    return state_root / value[len(prefix) :] if value.startswith(prefix) else Path(value)


def _assert_integrity_refs(value, state_root: Path) -> None:
    if isinstance(value, dict):
        for prefix in ("report", "evidence"):
            path_value = value.get(f"{prefix}_path")
            digest = value.get(f"{prefix}_sha256")
            artifact = _fixture_ref_path(path_value, state_root) if isinstance(path_value, str) else None
            if artifact and isinstance(digest, str) and artifact.is_file():
                assert digest == _sha256(artifact), (prefix, path_value)
        transcript = value.get("transcript_path")
        transcript_digest = value.get("transcript_sha256")
        transcript_path = _fixture_ref_path(transcript, state_root) if isinstance(transcript, str) else None
        if transcript_path and isinstance(transcript_digest, str) and transcript_path.is_file():
            assert transcript_digest == _sha256(transcript_path), transcript
        for item in value.values():
            _assert_integrity_refs(item, state_root)
    elif isinstance(value, list):
        for item in value:
            _assert_integrity_refs(item, state_root)


def test_fixture_manifests_are_offline_and_complete() -> None:
    assert set(GENERATOR.SCENARIOS) == {path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir()}
    for name in GENERATOR.SCENARIOS:
        manifest = json.loads((FIXTURE_ROOT / name / "fixture-manifest.json").read_text(encoding="utf-8"))
        assert manifest["scenario"] == name
        assert manifest["live_provider_used"] is False
        if name == "empty":
            assert not (FIXTURE_ROOT / name / "runs").exists()
        else:
            _single_run(name)


def test_empty_fixture_returns_no_runs() -> None:
    with loaded_fixture("empty"):
        payload, code = SERVER.api_workflow_runs()
    assert code == 200
    assert payload["workflow_runs"] == []


def test_detail_complete_pass() -> None:
    with loaded_fixture("complete-pass"):
        payload, code = SERVER.api_workflow_run("run-complete-pass")
    assert code == 200
    detail = payload["workflow_run"]
    assert detail["run"]["run_state"] == "complete"
    assert detail["run"]["terminal"] == {"status": "complete", "reason": "report_valid"}
    assert detail["report"]["result"] == "pass"
    assert detail["evidence"]["effective_model"]
    assert detail["transitions"]


def test_detail_findings_data() -> None:
    with loaded_fixture("complete-findings"):
        payload, code = SERVER.api_workflow_run("run-complete-findings")
    assert code == 200
    report = payload["workflow_run"]["report"]
    assert report["result"] == "findings"
    assert len(report["findings"]) == 2
    assert all({"severity", "status", "summary"} <= set(finding) for finding in report["findings"])


def test_detail_waiting_human_timeout() -> None:
    with loaded_fixture("waiting-human-timeout"):
        payload, code = SERVER.api_workflow_run("run-waiting-human-timeout")
    assert code == 200
    detail = payload["workflow_run"]
    assert detail["run"]["run_state"] == "waiting_human"
    assert detail["run"]["terminal"] == {"status": None, "reason": None}
    assert detail["report"] is None
    assert detail["evidence"]["outcome"] == "provider_timeout"


def test_detail_failed_invalid_report() -> None:
    with loaded_fixture("failed-invalid-report"):
        payload, code = SERVER.api_workflow_run("run-failed-invalid-report")
    assert code == 200
    detail = payload["workflow_run"]
    assert detail["run"]["run_state"] == "failed"
    assert detail["run"]["terminal"] == {"status": "blocked", "reason": "invalid_report"}
    assert detail["report"]["result"] == "invalid"
    assert detail["rejections"]
    assert detail["rejections"][0]["outcome"] == "report_invalid"
    assert "result invalid" in detail["rejections"][0]["errors"]


def test_corrupt_fixture_preserves_healthy_row() -> None:
    with loaded_fixture("corrupt"):
        payload, code = SERVER.api_workflow_runs()
    assert code == 200
    rows = {row["run_id"]: row for row in payload["workflow_runs"]}
    assert rows["corrupt-run"]["view_error"] == "corrupt_json"
    assert rows["corrupt-run"]["derived_status"] == "invalid"
    assert rows["run-corrupt"]["run_state"] == "complete"


def test_stale_lock_fixture() -> None:
    with loaded_fixture("stale-lock"):
        payload, code = SERVER.api_workflow_lock()
    assert code == 200
    assert len(payload["locks"]) == 1
    lock = payload["locks"][0]
    assert lock["locked"] is True
    assert lock["stale"] is True
    assert lock["stale_reason"].startswith("lock_owner_")


def test_traversal_fixture_is_not_addressable_by_path() -> None:
    with loaded_fixture("traversal"):
        payload, code = SERVER.api_workflow_run("../outside-root-secret")
        encoded_payload, encoded_code = SERVER.api_workflow_run("..%2Foutside-root-secret")
        list_payload, list_code = SERVER.api_workflow_runs()
    assert code == encoded_code == 400
    assert payload == encoded_payload == {"error": "invalid_run_id"}
    assert list_code == 200
    assert "__TRAVERSAL_SECRET__" not in json.dumps(list_payload)


def test_combined_list_states_and_reasons() -> None:
    names = tuple(name for name in GENERATOR.SCENARIOS if name not in {"empty"})
    with merged_fixtures(names):
        payload, code = SERVER.api_workflow_runs()
    assert code == 200
    rows = {row["run_id"]: row for row in payload["workflow_runs"]}
    expected = {
        "run-complete-pass": ("complete", "report_valid"),
        "run-complete-findings": ("complete", "report_valid"),
        "run-waiting-human-timeout": ("waiting_human", None),
        "run-failed-invalid-report": ("failed", "invalid_report"),
        "run-corrupt": ("complete", "report_valid"),
        "run-stale-lock": ("complete", "report_valid"),
        "run-traversal": ("complete", "report_valid"),
    }
    assert set(expected) <= set(rows)
    for run_id, (state, reason) in expected.items():
        assert rows[run_id]["run_state"] == state
        assert rows[run_id]["terminal"]["reason"] == reason
    assert rows["corrupt-run"]["derived_status"] == "invalid"


def test_html_contract_markers_present() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for marker in [
        'data-panel="workflow-runs"',
        'data-role="run-detail"',
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
        assert marker in html, marker


def test_http_root_serves_viewer_contract() -> None:
    httpd = SERVER.ThreadingHTTPServer(("127.0.0.1", 0), SERVER.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/", timeout=5) as response:
            assert response.status == 200
            html = response.read().decode("utf-8")
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
    assert 'data-panel="workflow-runs"' in html
    assert 'data-role="run-detail"' in html


def test_transcript_marker_never_reaches_api() -> None:
    for name in GENERATOR.SCENARIOS:
        with loaded_fixture(name):
            list_payload, list_code = SERVER.api_workflow_runs()
            if name == "empty":
                detail_payload, detail_code = {"workflow_run": None}, 200
            else:
                _, run = _single_run(name)
                detail_payload, detail_code = SERVER.api_workflow_run(run["run_id"])
            lock_payload, lock_code = SERVER.api_workflow_lock()
        assert list_code == detail_code == lock_code == 200
        serialized = json.dumps([list_payload, detail_payload, lock_payload], ensure_ascii=False)
        assert TRANSCRIPT_MARKER not in serialized, name


def test_all_fixtures_are_fresh() -> None:
    previous_umask = os.umask(0o077)
    try:
        for name in GENERATOR.SCENARIOS:
            passed, diff = GENERATOR.check_fixture(name)
            assert passed, f"{name}\n{diff}"
    finally:
        os.umask(previous_umask)


def test_normalized_content_digests_match_artifacts() -> None:
    normalized_evidence_count = 0
    for name in GENERATOR.SCENARIOS:
        root = FIXTURE_ROOT / name
        for path in sorted(root.rglob("*.json")):
            if path.name == "corrupt-run.json":
                continue
            _assert_integrity_refs(json.loads(path.read_text(encoding="utf-8")), root)
        for path in sorted(root.rglob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    _assert_integrity_refs(json.loads(line), root)
        for evidence_path in root.glob("provider-evidence/*/*-provider-evidence.json"):
            normalized_evidence_count += 1
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            transcript = _fixture_ref_path(evidence["transcript_path"], root)
            assert evidence["transcript_sha256"] == _sha256(transcript)
    assert normalized_evidence_count == 7


def main() -> None:
    tests = [
        test_fixture_manifests_are_offline_and_complete,
        test_empty_fixture_returns_no_runs,
        test_detail_complete_pass,
        test_detail_findings_data,
        test_detail_waiting_human_timeout,
        test_detail_failed_invalid_report,
        test_corrupt_fixture_preserves_healthy_row,
        test_stale_lock_fixture,
        test_traversal_fixture_is_not_addressable_by_path,
        test_combined_list_states_and_reasons,
        test_html_contract_markers_present,
        test_http_root_serves_viewer_contract,
        test_transcript_marker_never_reaches_api,
        test_all_fixtures_are_fresh,
        test_normalized_content_digests_match_artifacts,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
