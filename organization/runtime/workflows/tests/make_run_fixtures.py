#!/usr/bin/env python3
"""Generate deterministic workflow-run artifact fixtures from the offline harness."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parents[3]
FIXTURE_ROOT = TEST_DIR / "fixtures" / "run-artifacts"
FIXED_TIMESTAMP = "2026-01-01T00:00:00+0000"
TRANSCRIPT_MARKER = "__FIXTURE_TRANSCRIPT__"
SCENARIOS = (
    "empty",
    "complete-pass",
    "complete-findings",
    "waiting-human-timeout",
    "failed-invalid-report",
    "corrupt",
    "stale-lock",
    "traversal",
)
TIMESTAMP_KEYS = {
    "approved_at",
    "checked_at",
    "claimed_at",
    "closed_at",
    "created_at",
    "issued_at",
    "occurred_at",
    "queued_at",
    "signed_at",
    "started_at",
    "updated_at",
    "written_at",
}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
HARNESS_DIR_RE = re.compile(r"\.tmp/e2e-harness-\d+")

sys.path.insert(0, str(TEST_DIR))
from e2e_harness import OrchestratorHarness  # noqa: E402


def _stable_digest(path: tuple[str, ...]) -> str:
    material = "/".join(path).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _normalize_string(value: str, *, state_root: Path, path: tuple[str, ...]) -> str:
    normalized = value
    state_aliases = {str(state_root), str(state_root.absolute()), str(state_root.resolve())}
    repo_aliases = {str(REPO_ROOT), str(REPO_ROOT.absolute()), str(REPO_ROOT.resolve())}
    for alias in sorted(state_aliases, key=len, reverse=True):
        normalized = normalized.replace(alias, "__STATE_ROOT__")
    for alias in sorted(repo_aliases, key=len, reverse=True):
        normalized = normalized.replace(alias, "__REPO_ROOT__")
    normalized = HARNESS_DIR_RE.sub(".tmp/e2e-harness-fixture", normalized)
    if SHA256_RE.fullmatch(normalized):
        return _stable_digest(path)
    if normalized.startswith("approve-") and re.fullmatch(r"approve-[0-9a-f]+", normalized):
        return "approve-fixture"
    if re.fullmatch(r"evt-[0-9a-f]+", normalized):
        return "evt-" + hashlib.sha256("/".join(path).encode("utf-8")).hexdigest()[:20]
    if re.fullmatch(r"provider-[0-9a-f]+", normalized):
        return "provider-fixture"
    return normalized


def normalize_value(value: Any, *, state_root: Path, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            item_path = (*path, key)
            item = value[key]
            if isinstance(item, str) and (key in TIMESTAMP_KEYS or key.endswith("_at")) and ISO_TIMESTAMP_RE.match(item):
                normalized[key] = FIXED_TIMESTAMP
            elif key in {"duration_ms", "duration_seconds", "generated_at"} and isinstance(item, (int, float)):
                normalized[key] = 0
            elif key == "pid" and isinstance(item, int):
                normalized[key] = 1
            else:
                normalized[key] = normalize_value(item, state_root=state_root, path=item_path)
        return normalized
    if isinstance(value, list):
        return [normalize_value(item, state_root=state_root, path=(*path, str(index))) for index, item in enumerate(value)]
    if isinstance(value, str):
        return _normalize_string(value, state_root=state_root, path=path)
    return value


def normalize_tree(state_root: Path) -> None:
    # Runtime signing keys are host trust material, never portable test data.
    shutil.rmtree(state_root / "principal-keys", ignore_errors=True)
    for path in sorted(state_root.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        if path.name == "corrupt-run.json":
            continue
        if path.suffix == ".jsonl":
            records = []
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                if line.strip():
                    records.append(normalize_value(json.loads(line), state_root=state_root, path=(path.name, str(index))))
            path.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records), encoding="utf-8")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if path.name.endswith("-provider-transcript.json"):
                payload = {
                    "transcript_signal_version": "1",
                    "written_at": FIXED_TIMESTAMP,
                    "payload": {"outcome": "fixture_signal"},
                    "raw_content_policy": "signal_only_not_shared",
                }
            else:
                raise
        normalized = normalize_value(payload, state_root=state_root, path=(path.name,))
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for transcript in state_root.glob("provider-evidence/*/*-provider-transcript.json"):
        payload = json.loads(transcript.read_text(encoding="utf-8"))
        payload["fixture_marker"] = TRANSCRIPT_MARKER
        transcript.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    refresh_content_digests(state_root)

    forbidden = [path for path in state_root.rglob("*") if path.is_file() and path.suffix in {".key", ".pem", ".p12"}]
    if forbidden:
        raise RuntimeError(f"fixture tree contains host trust material: {forbidden}")


def _placeholder_path(value: Any, state_root: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    prefix = "__STATE_ROOT__/"
    if value.startswith(prefix):
        return state_root / value[len(prefix) :]
    return None


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_payload_files(state_root: Path, transform: Any) -> None:
    for file_path in sorted(state_root.rglob("*")):
        if not file_path.is_file() or file_path.suffix not in {".json", ".jsonl"}:
            continue
        if file_path.name == "corrupt-run.json":
            continue
        if file_path.suffix == ".jsonl":
            records = [json.loads(line) for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            transformed = [transform(record, state_root) for record in records]
            file_path.write_text(
                "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in transformed),
                encoding="utf-8",
            )
        else:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            payload = transform(payload, state_root)
            file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _refresh_transcript_digest(value: Any, state_root: Path) -> Any:
    if isinstance(value, dict):
        refreshed = {key: _refresh_transcript_digest(item, state_root) for key, item in value.items()}
        transcript = _placeholder_path(refreshed.get("transcript_path"), state_root)
        is_normalized_evidence = refreshed.get("evidence_version") == "1" and "provider_adapter_id" in refreshed
        if transcript and transcript.is_file() and ("stdout_sha256" in refreshed or is_normalized_evidence):
            refreshed["stdout_sha256"] = _file_sha256(transcript)
        return refreshed
    if isinstance(value, list):
        return [_refresh_transcript_digest(item, state_root) for item in value]
    return value


def _refresh_artifact_digests(value: Any, state_root: Path) -> Any:
    if isinstance(value, dict):
        refreshed = {key: _refresh_artifact_digests(item, state_root) for key, item in value.items()}
        for prefix in ("report", "evidence"):
            artifact = _placeholder_path(refreshed.get(f"{prefix}_path"), state_root)
            digest_key = f"{prefix}_sha256"
            if artifact and artifact.is_file() and digest_key in refreshed:
                refreshed[digest_key] = _file_sha256(artifact)
        return refreshed
    if isinstance(value, list):
        return [_refresh_artifact_digests(item, state_root) for item in value]
    return value


def refresh_content_digests(state_root: Path) -> None:
    # Evidence bytes include the normalized transcript digest, so refresh that first.
    _rewrite_payload_files(state_root, _refresh_transcript_digest)
    _rewrite_payload_files(state_root, _refresh_artifact_digests)


def _prepare_run(harness: OrchestratorHarness, scenario: str) -> str:
    request_id = f"req-{scenario}"
    run_id = f"run-{scenario}"
    harness.propose(task_id=f"TSK-{scenario}", request_id=request_id)
    harness.approve(request_id)
    harness.create_run(request_id, run_id)
    harness.drain(run_id)
    return run_id


def _run_provider(harness: OrchestratorHarness, run_id: str, mode: str) -> dict[str, Any]:
    runner = harness.optional_modules["provider_runner"]
    return runner.run_provider_step(
        state_root=harness.state_root,
        run_id=run_id,
        adapter="fake_pass",
        fake_provider_mode=mode,
    )


def build_scenario(scenario: str, state_root: Path) -> None:
    if scenario == "empty":
        normalize_tree(state_root)
        _write_manifest(scenario, state_root)
        return
    with OrchestratorHarness(state_root) as harness:
        run_id = _prepare_run(harness, scenario)
        if scenario in {"complete-pass", "corrupt", "stale-lock", "traversal"}:
            result = _run_provider(harness, run_id, "success")
            if result.get("decision") != "ok":
                raise RuntimeError(f"{scenario} provider flow failed: {result}")
        elif scenario == "complete-findings":
            result = _run_provider(harness, run_id, "findings")
            if result.get("decision") != "ok":
                raise RuntimeError(f"findings provider flow failed: {result}")
        elif scenario == "waiting-human-timeout":
            result = _run_provider(harness, run_id, "timeout")
            if result.get("reason") != "provider_timeout":
                raise RuntimeError(f"timeout provider flow did not block as expected: {result}")
        elif scenario == "failed-invalid-report":
            report_path = harness.place_report(run_id, result="invalid")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            runner = harness.optional_modules["provider_runner"]
            request_path = state_root / "adapter-requests" / run_id / "review-claude_headless_p0.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            adapter = runner.load_provider_adapters()["claude_headless_p0"]
            evidence = runner.normalized_evidence(
                request=request,
                adapter=adapter,
                report=report,
                outcome="ok",
                details={"duration_ms": 0},
            )
            Path(request["evidence_path"]).write_text(
                json.dumps(evidence, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            result = harness.frontdoor.validate_report(
                state_root=state_root,
                run_id=run_id,
                report_path_arg=str(report_path),
            )
            if result.get("reason") != "invalid_report":
                raise RuntimeError(f"invalid report did not fail as expected: {result}")
        else:
            raise ValueError(f"unknown scenario: {scenario}")

    if scenario == "complete-findings":
        report_path = state_root / "reports" / run_id / "review-external-review-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["findings"].append(
            {
                "finding_id": "F-2",
                "severity": "medium",
                "status": "open",
                "summary": "Second deterministic fixture finding.",
                "evidence_refs": ["organization/runtime/workflows/README.md"],
            }
        )
        report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")

    if scenario == "corrupt":
        corrupt = state_root / "runs" / "corrupt-run.json"
        corrupt.write_text('{"run_id":"corrupt-run",', encoding="utf-8")
    if scenario == "stale-lock":
        lock_dir = state_root / "locks" / "global-advisory.lock.d"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "owner.json").write_text(
            json.dumps(
                {
                    "lock_version": "1",
                    "lock_type": "workflow-run-global",
                    "pid": 2147483647,
                    "hostname": "fixture.invalid",
                    "process_start_token": "fixture-dead-process",
                    "owner_nonce": "fixture-stale-lock",
                    "created_at": FIXED_TIMESTAMP,
                    "stale_after_seconds": 300,
                    "operation": "run_provider",
                    "run_id": run_id,
                    "principal_type": "harness_runner",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if scenario == "traversal":
        (state_root / "outside-root-secret.json").write_text(
            json.dumps({"marker": "__TRAVERSAL_SECRET__"}) + "\n",
            encoding="utf-8",
        )

    normalize_tree(state_root)

    _write_manifest(scenario, state_root)


def _write_manifest(scenario: str, state_root: Path) -> None:
    manifest = {
        "fixture_version": 1,
        "scenario": scenario,
        "generated_by": "organization/runtime/workflows/tests/make_run_fixtures.py",
        "generated_at": FIXED_TIMESTAMP,
        "live_provider_used": False,
        "state_root_placeholder": "__STATE_ROOT__",
    }
    (state_root / "fixture-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate_fixture(scenario: str, destination: Path) -> None:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown fixture set: {scenario}")
    if os.environ.get("SAIHAI_ALLOW_LIVE_PROVIDERS"):
        raise RuntimeError("refusing to generate fixtures while SAIHAI_ALLOW_LIVE_PROVIDERS is set")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    build_scenario(scenario, destination)


def directory_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def check_fixture(scenario: str) -> tuple[bool, str]:
    expected_root = FIXTURE_ROOT / scenario
    with tempfile.TemporaryDirectory(prefix=f"saihai-fixture-{scenario}-") as raw_tmp:
        actual_root = Path(raw_tmp) / scenario
        generate_fixture(scenario, actual_root)
        expected = directory_snapshot(expected_root) if expected_root.exists() else {}
        actual = directory_snapshot(actual_root)
    if expected == actual:
        return True, ""
    lines: list[str] = []
    for relative in sorted(set(expected) | set(actual)):
        if expected.get(relative) == actual.get(relative):
            continue
        before = expected.get(relative, b"").decode("utf-8", errors="replace").splitlines()
        after = actual.get(relative, b"").decode("utf-8", errors="replace").splitlines()
        lines.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"checked-in/{scenario}/{relative}",
                tofile=f"generated/{scenario}/{relative}",
                lineterm="",
            )
        )
    return False, "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", choices=SCENARIOS)
    group.add_argument("--all", action="store_true")
    group.add_argument("--check", choices=SCENARIOS)
    args = parser.parse_args()

    if args.check:
        passed, diff = check_fixture(args.check)
        print(json.dumps({"result": "pass" if passed else "fail", "fixture": args.check}, ensure_ascii=False))
        if diff:
            print(diff)
        raise SystemExit(0 if passed else 1)

    selected = SCENARIOS if args.all else (args.set,)
    for scenario in selected:
        generate_fixture(scenario, FIXTURE_ROOT / scenario)
    print(json.dumps({"result": "pass", "fixtures": list(selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
