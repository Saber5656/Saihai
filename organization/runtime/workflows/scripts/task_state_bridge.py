#!/usr/bin/env python3
"""Derived task/session views for orchestrator workflow-run state."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import run_store

DEFAULT_ITB_STATE_ROOTS = (
    Path.home() / ".claude" / "state" / "itb",
    Path.home() / ".codex" / "state" / "itb",
)
ITB_STATE_ROOTS_ENV = "SAIHAI_ITB_STATE_ROOTS"
RUN_VIEW_VERSION = "1"
SESSION_INDEX_VERSION = "1"
SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RESERVED_RUN_ARTIFACT_RE = re.compile(r"(?:\.error|\.corrupt-\d+)\.json$")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def mtime_iso(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(path.stat().st_mtime))
    except OSError:
        return ""


def safe_session_id(value: str) -> str:
    allowed = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    compact = "".join(allowed).strip(".-")
    return compact[:96] or "anonymous"


def default_itb_state_roots() -> list[Path]:
    raw = os.environ.get(ITB_STATE_ROOTS_ENV, "")
    if raw:
        return [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]
    return [path.expanduser() for path in DEFAULT_ITB_STATE_ROOTS]


def run_files(state_root: Path) -> list[Path]:
    runs_dir = state_root / "runs"
    if not runs_dir.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(runs_dir.iterdir()):
        if path.name.startswith(".") or not path.name.endswith(".json"):
            continue
        if RESERVED_RUN_ARTIFACT_RE.search(path.name):
            continue
        out.append(path)
    return out


def run_id_from_path(path: Path) -> str:
    return path.name[:-5] if path.name.endswith(".json") else path.stem


def load_run_lenient(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {
            "run_id": run_id_from_path(path),
            "view_error": "corrupt_json",
        }
    if not isinstance(payload, dict):
        return {
            "run_id": run_id_from_path(path),
            "view_error": "invalid_run_json",
        }
    return payload


def state_path(state_root: Path, name: str) -> Path:
    return state_root / name


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return state_path(state_root, "work-orders") / run_id / f"{step_id}.json"


def report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return state_path(state_root, "reports") / run_id / f"{step_id}-external-review-report.json"


def provider_evidence_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return state_path(state_root, "provider-evidence") / run_id / f"{step_id}-provider-evidence.json"


def existing_path_text(path: Path) -> str:
    return str(path) if path.exists() else ""


def requester_field(run: dict[str, Any], field: str) -> str:
    requester = run.get("requester")
    if not isinstance(requester, dict):
        return ""
    return str(requester.get(field) or "")


def terminal_view(run: dict[str, Any]) -> dict[str, Any]:
    terminal = run.get("terminal")
    if not isinstance(terminal, dict):
        return {"status": None, "reason": None}
    return {
        "status": terminal.get("status"),
        "reason": terminal.get("reason"),
    }


def last_transition_at(run: dict[str, Any]) -> str:
    transitions = run.get("transitions")
    if isinstance(transitions, list):
        for transition in reversed(transitions):
            if isinstance(transition, dict) and transition.get("occurred_at"):
                return str(transition["occurred_at"])

    history = run.get("step_history")
    if isinstance(history, list):
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            for field in ("occurred_at", "checked_at", "completed_at", "queued_at", "started_at"):
                if item.get(field):
                    return str(item[field])

    provenance = run.get("transition_provenance")
    if isinstance(provenance, list):
        for item in reversed(provenance):
            if not isinstance(item, dict):
                continue
            signature = item.get("signature")
            if isinstance(signature, dict) and signature.get("signed_at"):
                return str(signature["signed_at"])
    return ""


def current_step_id(run: dict[str, Any]) -> str:
    return str(run.get("current_step") or "")


def run_task_view(
    run: dict[str, Any],
    *,
    state_root: Path | None = None,
    run_path: Path | None = None,
) -> dict[str, Any]:
    run_id = str(run.get("run_id") or "")
    step_id = current_step_id(run)
    report = ""
    evidence = ""
    if state_root is not None and run_id and step_id:
        report = existing_path_text(report_path(state_root, run_id, step_id))
        evidence = existing_path_text(provider_evidence_path(state_root, run_id, step_id))
    return {
        "view_version": RUN_VIEW_VERSION,
        "run_id": run_id,
        "task_id": str(run.get("task_id") or ""),
        "request_id": str(run.get("request_id") or ""),
        "workflow_id": str(run.get("workflow_id") or ""),
        "run_state": str(run.get("run_state") or ""),
        "goal_state": str(run.get("goal_state") or ""),
        "terminal": terminal_view(run),
        "current_step": step_id,
        "iteration": run.get("iteration"),
        "chat_session_id": requester_field(run, "chat_session_id"),
        "frontdoor": requester_field(run, "frontdoor"),
        "report_path": report,
        "evidence_path": evidence,
        "last_transition_at": last_transition_at(run),
        "updated_at": mtime_iso(run_path),
    }


def runs_for_task(state_root: Path, task_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in run_files(state_root):
        run = load_run_lenient(path)
        if run.get("view_error"):
            rows.append(run)
            continue
        if str(run.get("task_id") or "") != task_id:
            continue
        rows.append(run_task_view(run, state_root=state_root, run_path=path))
    return sorted(rows, key=lambda item: str(item.get("run_id") or ""))


def runs_for_session(state_root: Path, chat_session_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in run_files(state_root):
        run = load_run_lenient(path)
        if run.get("view_error"):
            continue
        if requester_field(run, "chat_session_id") != chat_session_id:
            continue
        rows.append(run_task_view(run, state_root=state_root, run_path=path))
    return sorted(rows, key=lambda item: str(item.get("run_id") or ""))


def raw_runs_for_task(state_root: Path, task_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in run_files(state_root):
        run = load_run_lenient(path)
        if run.get("view_error"):
            continue
        if str(run.get("task_id") or "") == task_id:
            rows.append(run)
    return sorted(rows, key=lambda item: str(item.get("run_id") or ""))


def step_ids_for_run(run: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    history = run.get("step_history")
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("step_id") or "")
            if step_id and step_id not in seen:
                seen.add(step_id)
                out.append(step_id)
    current = current_step_id(run)
    if current and current not in seen:
        out.append(current)
    return out


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def message_status(run_state: str) -> str:
    if run_state in {"waiting_provider", "validating", "remediating"}:
        return "processing"
    if run_state == "complete":
        return "done"
    if run_state in {"failed", "aborted"}:
        return "failed"
    return "pending"


def report_status(run_state: str) -> str:
    if run_state == "complete":
        return "done"
    if run_state in {"failed", "aborted"}:
        return "failed"
    return "pending"


def queue_evidence_view(state_root: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = str(run.get("run_id") or "")
    if not run_id:
        return []
    run_state = str(run.get("run_state") or "")
    rows: list[dict[str, Any]] = []
    for step_id in step_ids_for_run(run):
        order_path = work_order_path(state_root, run_id, step_id)
        work_order = read_json_object(order_path) if order_path.exists() else {}
        report = report_path(state_root, run_id, step_id)
        evidence = provider_evidence_path(state_root, run_id, step_id)
        rows.append(
            {
                "from_role": "frontdoor",
                "to_role": str(work_order.get("to_role") or ""),
                "message_id": f"wo-{run_id}-{step_id}",
                "inbox_path": "",
                "payload_path": existing_path_text(order_path),
                "report_path": existing_path_text(report),
                "message_status": message_status(run_state),
                "report_status": report_status(run_state),
                "provider_evidence": existing_path_text(evidence),
                "notes": "orchestrator workflow run (derived view)",
            }
        )
    return rows


def task_view_payload(state_root: Path, task_id: str) -> dict[str, Any]:
    runs = runs_for_task(state_root, task_id)
    queue_evidence: list[dict[str, Any]] = []
    for run in raw_runs_for_task(state_root, task_id):
        queue_evidence.extend(queue_evidence_view(state_root, run))
    return {
        "schema_version": 1,
        "decision": "ok",
        "task_id": task_id,
        "runs": runs,
        "queue_evidence": queue_evidence,
    }


def find_session_dir(chat_session_id: str, itb_state_roots: list[Path]) -> Path | None:
    if not chat_session_id:
        return None
    session_name = safe_session_id(chat_session_id)
    if not SAFE_SESSION_ID_RE.fullmatch(session_name):
        return None
    for root in itb_state_roots:
        session_dir = root.expanduser() / session_name
        if session_dir.is_dir():
            return session_dir
    return None


def record_run_link(
    orch_state_root: Path,
    run: dict[str, Any],
    itb_state_roots: list[Path] | None = None,
) -> Path | None:
    chat_session_id = requester_field(run, "chat_session_id")
    roots = itb_state_roots if itb_state_roots is not None else default_itb_state_roots()
    session_dir = find_session_dir(chat_session_id, roots)
    if session_dir is None:
        return None

    index_path = session_dir / "orchestrator-runs.json"
    payload = {
        "orchestrator_runs_version": SESSION_INDEX_VERSION,
        "orchestrator_state_root": str(orch_state_root.expanduser().resolve()),
        "updated_at": now_iso(),
        "runs": runs_for_session(orch_state_root, chat_session_id),
    }
    run_store.atomic_write_json(index_path, payload)
    return index_path
