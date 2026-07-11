#!/usr/bin/env python3
"""Final completion gate for terminal workflow runs and Vault evidence."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import report_gate
import run_lifecycle
import run_lock
import run_store
import work_order_builder


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
        "transitions": state_root / "transitions",
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"expected object json: {path}")
    return payload


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(parent.expanduser().resolve())
    except (OSError, ValueError):
        return False
    return True


def reason(reason_class: str, detail: str) -> dict[str, str]:
    return {"reason_class": reason_class, "detail": detail}


def _load_optional_json(path: Path, reasons: list[dict[str, str]], reason_class: str) -> dict[str, Any] | None:
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reasons.append(reason(reason_class, str(exc)))
        return None


def _snapshot_paths(state_root: Path, run_id: str) -> list[Path]:
    return sorted((state_paths(state_root)["work_orders"] / run_id).glob("*-snapshot-*.json"))


def _verify_snapshot(
    *,
    state_root: Path,
    run: dict[str, Any],
    work_order: dict[str, Any] | None,
    reasons: list[dict[str, str]],
    skipped: list[str],
) -> None:
    run_id = str(run.get("run_id") or "")
    all_snapshots = _snapshot_paths(state_root, run_id)
    if not all_snapshots:
        skipped.append("snapshot")
        return
    if work_order is None:
        reasons.append(reason("missing_work_order", "cannot verify snapshot without a work order"))
        return
    step_id = str(work_order.get("step_id") or run.get("current_step") or "")
    iteration = int(run.get("iteration") or 1)
    snapshot_path = work_order_builder.snapshot_path(state_root, run_id, step_id, iteration)
    if not snapshot_path.exists():
        reasons.append(reason("work_order_snapshot_mismatch", f"missing current snapshot: {snapshot_path}"))
        return
    snapshot = _load_optional_json(snapshot_path, reasons, "work_order_snapshot_mismatch")
    if snapshot is None:
        return
    expected_digest = work_order_builder.sha256_digest(work_order)
    if snapshot.get("work_order_digest") != expected_digest:
        reasons.append(
            reason(
                "work_order_snapshot_mismatch",
                f"expected {expected_digest}, found {snapshot.get('work_order_digest')!r}",
            )
        )


def _transition_artifact_complete(state_root: Path, run_id: str, skipped: list[str]) -> bool:
    directory = state_paths(state_root)["transitions"] / run_id
    if not directory.exists():
        skipped.append("transition")
        return True
    artifacts = sorted(directory.glob("*.json"))
    if not artifacts:
        skipped.append("transition")
        return True
    for path in artifacts:
        try:
            payload = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("outcome") == "report_valid" and payload.get("to_state") == "complete":
            return True
    return False


def _verify_evidence_digest(
    *,
    evidence: dict[str, Any] | None,
    report: dict[str, Any] | None,
    reasons: list[dict[str, str]],
    skipped: list[str],
) -> None:
    if evidence is None or report is None:
        return
    stdout_sha256 = evidence.get("stdout_sha256")
    provider_evidence = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
    transcript_raw = provider_evidence.get("transcript_path")
    if not stdout_sha256:
        skipped.append("digest")
        return
    if not isinstance(transcript_raw, str) or not transcript_raw:
        reasons.append(reason("digest_mismatch", "stdout_sha256 is present but transcript_path is missing"))
        return
    transcript_path = Path(transcript_raw).expanduser()
    if not transcript_path.exists():
        reasons.append(reason("digest_mismatch", f"transcript file missing: {transcript_path}"))
        return
    actual = file_sha256(transcript_path)
    if actual != stdout_sha256:
        reasons.append(reason("digest_mismatch", f"expected {stdout_sha256}, found {actual}"))


def _evidence_identity_errors(evidence: dict[str, Any], *, run: dict[str, Any], step_id: str) -> list[str]:
    errors: list[str] = []
    if evidence.get("evidence_version") != "1":
        errors.append("evidence_version must be '1'")
    for field in ("run_id", "step_id"):
        expected = str(run.get(field) if field != "step_id" else step_id)
        if str(evidence.get(field) or "") != expected:
            errors.append(f"evidence.{field} mismatch: expected {expected!r}")
    return errors


def vault_evidence(state_root: Path, run: dict[str, Any], report: dict[str, Any], evidence: dict[str, Any] | None) -> dict[str, Any]:
    provider_evidence = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
    report_path = Path(str(run.get("_verified_report_path") or "")).expanduser()
    evidence_path = Path(str(provider_evidence.get("evidence_path") or "")).expanduser()
    return {
        "vault_evidence_version": "1",
        "task_id": str(run.get("task_id") or ""),
        "run_id": str(run.get("run_id") or ""),
        "request_id": str(run.get("request_id") or ""),
        "workflow_id": str(run.get("workflow_id") or ""),
        "terminal_status": "complete",
        "terminal_reason": str((run.get("terminal") or {}).get("reason") or ""),
        "result": report.get("result"),
        "finding_count": len(report.get("findings") or []) if isinstance(report.get("findings"), list) else 0,
        "provider": evidence.get("provider") if isinstance(evidence, dict) else provider_evidence.get("provider"),
        "effective_model": evidence.get("effective_model") if isinstance(evidence, dict) else provider_evidence.get("effective_model"),
        "provider_session_id": evidence.get("provider_session_id")
        if isinstance(evidence, dict)
        else provider_evidence.get("provider_session_id"),
        "provider_request_id": evidence.get("provider_request_id") if isinstance(evidence, dict) else provider_evidence.get("request_id"),
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path) if report_path.exists() else "",
        "evidence_path": str(evidence_path),
        "evidence_sha256": file_sha256(evidence_path) if evidence_path.exists() else "",
        "verified_at": now_iso(),
        "verification_decision": "complete",
    }


def render_vault_evidence_markdown(block: dict[str, Any]) -> str:
    rows = [
        ("Task ID", block.get("task_id")),
        ("Run ID", block.get("run_id")),
        ("Workflow", block.get("workflow_id")),
        ("Result", block.get("result")),
        ("Terminal", f"{block.get('terminal_status')} / {block.get('terminal_reason')}"),
        ("Provider", block.get("provider")),
        ("Effective Model", block.get("effective_model")),
        ("Provider Session", block.get("provider_session_id")),
        ("Report", f"{block.get('report_path')} ({block.get('report_sha256')})"),
        ("Provider Evidence", f"{block.get('evidence_path')} ({block.get('evidence_sha256')})"),
        ("Verified At", block.get("verified_at")),
        ("Verification", block.get("verification_decision")),
    ]
    lines = ["## Workflow Run Evidence", "", "| Field | Value |", "|---|---|"]
    for field, value in rows:
        text = str(value or "").replace("|", "\\|")
        lines.append(f"| {field} | {text} |")
    return "\n".join(lines) + "\n"


def annotate_completion(
    state_root: Path,
    run: dict[str, Any],
    *,
    block: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    run["completion_verification"] = {
        "verified_at": block["verified_at"],
        "decision": "complete",
        "report_sha256": block["report_sha256"],
        "evidence_sha256": block["evidence_sha256"],
        "verifier": run_lifecycle.redacted_principal(principal),
    }
    run_store.store_run(state_root, run, expected_current_state="complete")
    return run


def verify_completion(
    state_root: Path,
    run_id: str,
    *,
    principal: dict[str, Any] | None = None,
    annotate: bool = True,
) -> dict[str, Any]:
    actor = principal or {
        "principal_type": "harness_runner",
        "principal_id": "local-harness",
        "authn_method": "local_cli",
    }
    run_store.validate_artifact_id(run_id, "run_id")
    reasons: list[dict[str, str]] = []
    skipped: list[str] = []
    report: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    work_order: dict[str, Any] | None = None

    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="verify_completion",
            run_id=run_id,
            principal=actor,
        ):
            try:
                run = run_store.load_run(state_root, run_id)
            except run_store.RunStoreError as exc:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "run_id": run_id,
                    "reasons": [reason("run_unloadable", f"{exc.reason_class}: {'; '.join(exc.errors)}")],
                    "skipped": [],
                }

            terminal = run.get("terminal") if isinstance(run.get("terminal"), dict) else {}
            if run.get("run_state") != "complete" or terminal.get("status") != "complete":
                reasons.append(
                    reason(
                        "run_not_terminal_complete",
                        f"run_state={run.get('run_state')!r}, terminal.status={terminal.get('status')!r}",
                    )
                )

            activation = run.get("activation") if isinstance(run.get("activation"), dict) else {}
            if (
                activation.get("activation_status") != "approved"
                or activation.get("activation_source") not in run_store.APPROVED_ACTIVATION_SOURCES
            ):
                reasons.append(reason("activation_not_approved", "activation must be approved from a legal source"))

            step_id = str(run.get("current_step") or "")
            work_order_file = state_paths(state_root)["work_orders"] / run_id / f"{step_id}.json"
            if work_order_file.exists():
                work_order = _load_optional_json(work_order_file, reasons, "missing_work_order")
            else:
                reasons.append(reason("missing_work_order", f"missing work order: {work_order_file}"))
            _verify_snapshot(state_root=state_root, run=run, work_order=work_order, reasons=reasons, skipped=skipped)

            if work_order is not None:
                report_file = Path(str(work_order.get("report_path") or "")).expanduser()
            else:
                report_file = state_paths(state_root)["reports"] / run_id / f"{step_id}-external-review-report.json"
            if not report_file.exists():
                reasons.append(reason("missing_typed_report", f"missing typed report: {report_file}"))
                if work_order is not None:
                    canonical_evidence = report_gate.provider_evidence_path(
                        state_root,
                        run_id,
                        str(work_order.get("step_id") or step_id),
                    )
                    if not canonical_evidence.exists():
                        reasons.append(
                            reason("missing_provider_evidence", f"missing provider evidence: {canonical_evidence}")
                        )
            else:
                report = _load_optional_json(report_file, reasons, "invalid_typed_report")

            if report is not None and work_order is not None:
                outcome, errors = report_gate.classify_report_outcome(
                    report,
                    run=run,
                    work_order=work_order,
                    state_root=state_root,
                )
                if outcome != "report_valid":
                    reasons.append(reason("invalid_typed_report", f"{outcome}: {'; '.join(errors)}"))
                for field in ("run_id", "request_id", "workflow_id"):
                    if str(report.get(field) or "") != str(run.get(field) or ""):
                        reasons.append(reason("report_identity_mismatch", f"{field} mismatch"))
                if str(report.get("step_id") or "") != str(work_order.get("step_id") or ""):
                    reasons.append(reason("report_identity_mismatch", "step_id mismatch"))

                provider = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
                evidence_raw = provider.get("evidence_path")
                transcript_raw = provider.get("transcript_path")
                for label, raw in (("evidence_path", evidence_raw), ("transcript_path", transcript_raw)):
                    path = Path(str(raw or "")).expanduser()
                    if raw and not path_is_within(path, state_root):
                        reasons.append(reason("evidence_path_escape", f"{label} escapes state root: {path}"))
                evidence_path = Path(str(evidence_raw or "")).expanduser()
                if not evidence_raw or not evidence_path.exists():
                    reasons.append(reason("missing_provider_evidence", f"missing provider evidence: {evidence_path}"))
                else:
                    evidence = _load_optional_json(evidence_path, reasons, "missing_provider_evidence")
                    if evidence is not None:
                        for item in _evidence_identity_errors(evidence, run=run, step_id=str(work_order.get("step_id") or "")):
                            reasons.append(reason("missing_provider_evidence", item))
                    _verify_evidence_digest(evidence=evidence, report=report, reasons=reasons, skipped=skipped)

            if not _transition_artifact_complete(state_root, run_id, skipped):
                reasons.append(reason("missing_transition_artifact", "no report_valid transition artifact found"))

            if reasons:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "run_id": run_id,
                    "task_id": str(run.get("task_id") or ""),
                    "workflow_id": str(run.get("workflow_id") or ""),
                    "reasons": reasons,
                    "skipped": sorted(set(skipped)),
                }

            assert report is not None
            block = vault_evidence(state_root, {**run, "_verified_report_path": str(report_file)}, report, evidence)
            if annotate:
                run = annotate_completion(state_root, run, block=block, principal=actor)
            return {
                "schema_version": 1,
                "decision": "complete",
                "run_id": run_id,
                "task_id": str(run.get("task_id") or ""),
                "workflow_id": str(run.get("workflow_id") or ""),
                "reasons": [],
                "skipped": sorted(set(skipped)),
                "evidence": block,
                "workflow_run": run,
            }
    except run_lock.LockContentionError:
        raise
