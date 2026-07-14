#!/usr/bin/env python3
"""ITB Organization Status Viewer - read-only local dashboard.

Visualizes an ITB Organization Instance from typed state, queue inboxes, and
provider reports. The viewer never starts agents, never calls providers, and
never mutates live Claude/Codex configuration.

Usage:  python3 server.py [--port 8765]
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SAIHAI_CHECKOUT_ROOT = Path(__file__).resolve().parent
if str(SAIHAI_CHECKOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(SAIHAI_CHECKOUT_ROOT))
from directory_paths import load_environment

ENV_DIAGNOSTICS = load_environment(checkout_root=SAIHAI_CHECKOUT_ROOT, require_catalog=True)

HOME = Path.home()
STATE_ROOTS = [
    ("claude", HOME / ".claude" / "state" / "itb"),
    ("codex", HOME / ".codex" / "state" / "itb"),
]
TRANSCRIPT_ROOT = HOME / ".claude" / "projects"
STATIC_DIR = Path(__file__).resolve().parent / "static"
ORG_DIR = Path(__file__).resolve().parent / "organization"
ROLE_REGISTRY = ORG_DIR / "runtime" / "infra-team-bootstrap" / "config" / "role-agent-registry.yaml"
MODEL_REGISTRY = ORG_DIR / "runtime" / "model-registry.md"
WORKFLOW_SCRIPTS_DIR = ORG_DIR / "runtime" / "workflows" / "scripts"
ORCH_STATE_ROOTS = [
    ("claude", HOME / ".claude" / "state" / "itb" / "frontdoor-orchestrator"),
    ("codex", HOME / ".codex" / "state" / "itb" / "frontdoor-orchestrator"),
]

SESSION_ID_RE = re.compile(r"^[0-9a-zA-Z_.:-]{3,128}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RUN_STATES = {
    "created",
    "step_queued",
    "waiting_provider",
    "validating",
    "waiting_human",
    "remediating",
    "complete",
    "failed",
    "aborted",
}
MAX_RUN_FILE_BYTES = 1_000_000
MAX_RUN_DISCOVERY_FILES = 500
MAX_RUN_SCAN_ENTRIES = 2_000
MAX_DETAIL_ARTIFACT_FILES = 200
MAX_DETAIL_ARTIFACT_SCAN_ENTRIES = 1_000
MAX_DETAIL_ARTIFACT_BYTES = 5_000_000
DENIED_ARTIFACT_KEYS = {
    "prompt",
    "provider_transcript",
    "raw_prompt",
    "raw_transcript",
    "raw_transcript_text",
    "stdout",
    "tmux_pane_output",
    "transcript",
    "transcript_content",
    "pane_output",
}
RUN_RECORD_VIEW_KEYS = {
    "run_version",
    "run_id",
    "task_id",
    "request_id",
    "workflow_id",
    "goal_state",
    "run_state",
    "current_step",
    "iteration",
    "max_steps",
    "activation",
    "terminal",
    "requester",
    "scheduling",
    "context_sharing",
    "completion_verification",
}
LOCK_OWNER_VIEW_KEYS = {
    "lock_version",
    "lock_type",
    "pid",
    "hostname",
    "process_start_token",
    "created_at",
    "stale_after_seconds",
    "operation",
    "run_id",
    "principal_type",
}
LOCK_INFO_VIEW_KEYS = {
    "schema_version",
    "decision",
    "error",
    "locked",
    "stale",
    "stale_reason",
    "lock_path",
}
TEAM_ORDER = ["gate", "tech", "contents", "business", "infra"]
TEAM_LABELS = {
    "gate": "Gate",
    "tech": "Engineering",
    "contents": "Contents",
    "business": "Business",
    "infra": "Infrastructure",
}

FAST_HINT_RE = re.compile(r"(教えて|確認して|調べる|比較|どちら|人気|売上|シェア|どこ|何|短く|軽く|一言)", re.I)
STRICT_HINT_RE = re.compile(
    r"(修正|実装|変更|追加|削除|commit|push|PR|レビュー|security|権限|policy|モデル|hook|本番|検証|テスト|複数|認証|設計|採用|選定|判断|worktree|Vault)",
    re.I,
)
MAINTENANCE_HINT_RE = re.compile(
    r"(組織|organization|Sahai|Saihai|Agent-Teams-Viewer|Agent-Org-Viewer|configure-organization|COMMON-AGENTS|\bHook\b|\bITB\b|gate-prompt-formatter|infra-team-bootstrap)",
    re.I,
)
WEATHER_RE = re.compile(r"(天気|天気予報|weather|forecast)", re.I)
LOCATION_HINT_RE = re.compile(
    r"(東京|大阪|京都|名古屋|福岡|札幌|沖縄|横浜|神戸|Seattle|San Francisco|New York|Los Angeles|Tokyo|Osaka|市|区|県|府|都|州)",
    re.I,
)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_json(path: Path):
    text = read_text(path)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _load_workflow_module(module_name: str, filename: str):
    path = WORKFLOW_SCRIPTS_DIR / filename
    if not path.is_file():
        return None
    scripts_dir = str(WORKFLOW_SCRIPTS_DIR)
    inserted = False
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
        inserted = True
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None
    finally:
        if inserted:
            try:
                sys.path.remove(scripts_dir)
            except ValueError:
                pass


@functools.lru_cache(maxsize=1)
def _task_state_bridge():
    return _load_workflow_module("saihai_task_state_bridge", "task_state_bridge.py")


def _run_lock():
    return _load_workflow_module("saihai_run_lock", "run_lock.py")


def truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def org_json(name: str, default):
    return read_json(ORG_DIR / name) or default


def jsonish_file(path: Path):
    data = read_json(path)
    if data is not None:
        return data
    raw = read_text(path)
    if not raw:
        return None
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        key = key.strip().strip("-").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out or {"raw": raw}


def orch_roots() -> list[tuple[str, Path]]:
    env_root = (
        os.environ.get("SAIHAI_ORCH_STATE_ROOT")
        or os.environ.get("SAHAI_ORCH_STATE_ROOT")
        or ""
    ).strip()
    candidates: list[tuple[str, Path]]
    if env_root:
        candidates = [("env", Path(env_root).expanduser())]
    else:
        candidates = [(runtime, path.expanduser()) for runtime, path in ORCH_STATE_ROOTS]
    return [(runtime, root) for runtime, root in candidates if root.exists()]


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _safe_artifact_id(value: str) -> bool:
    return bool(RUN_ID_RE.fullmatch(str(value or "")))


def _artifact_key_is_denied(key: object) -> bool:
    raw_key = str(key or "").strip()
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw_key).replace("-", "_").lower()
    if normalized == "transcript_path":
        return False
    sensitive_tokens = {"prompt", "transcript"}
    return normalized in DENIED_ARTIFACT_KEYS or bool(sensitive_tokens.intersection(normalized.split("_")))


def _redact_artifact(value):
    if isinstance(value, dict):
        return {key: _redact_artifact(item) for key, item in value.items() if not _artifact_key_is_denied(key)}
    if isinstance(value, list):
        return [_redact_artifact(item) for item in value]
    return value


def _project_run_record(run_record: dict) -> dict:
    return _redact_artifact({key: run_record[key] for key in RUN_RECORD_VIEW_KEYS if key in run_record})


def _project_lock_info(info: dict) -> dict:
    projected = {key: info[key] for key in LOCK_INFO_VIEW_KEYS if key in info}
    owner = info.get("owner")
    if isinstance(owner, dict):
        projected["owner"] = _redact_artifact(
            {key: owner[key] for key in LOCK_OWNER_VIEW_KEYS if key in owner}
        ) or None
    else:
        projected["owner"] = None
    return projected


def _read_json_limited(path: Path, *, root: Path | None = None, max_bytes: int | None = None):
    if root is not None and not _path_is_within(path, root):
        return {"view_error": "outside_root"}
    try:
        if path.stat().st_size > (MAX_RUN_FILE_BYTES if max_bytes is None else max_bytes):
            return {"view_error": "oversize"}
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError):
        return None
    except ValueError:
        return {"view_error": "corrupt_json"}
    except RecursionError:
        return {"view_error": "too_deep"}
    try:
        return _redact_artifact(payload) if isinstance(payload, (dict, list)) else payload
    except RecursionError:
        return {"view_error": "too_deep"}


def mtime_iso(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    except OSError:
        return ""


def _fallback_thin_view(run: dict, *, state_root: Path | None = None, run_path: Path | None = None) -> dict:
    run_id = str(run.get("run_id") or "")
    step_id = str(run.get("current_step") or "")
    transitions = run.get("transitions") if isinstance(run.get("transitions"), list) else []
    last_transition = next(
        (
            str(item.get("occurred_at"))
            for item in reversed(transitions)
            if isinstance(item, dict) and item.get("occurred_at")
        ),
        "",
    )
    requester = run.get("requester") if isinstance(run.get("requester"), dict) else {}
    terminal = run.get("terminal") if isinstance(run.get("terminal"), dict) else {}
    report = ""
    evidence = ""
    if state_root is not None and run_id and step_id:
        report_path = state_root / "reports" / run_id / f"{step_id}-external-review-report.json"
        evidence_path = state_root / "provider-evidence" / run_id / f"{step_id}-provider-evidence.json"
        report = str(report_path) if report_path.exists() else ""
        evidence = str(evidence_path) if evidence_path.exists() else ""
    return {
        "view_version": "fallback",
        "run_id": run_id,
        "task_id": str(run.get("task_id") or ""),
        "request_id": str(run.get("request_id") or ""),
        "workflow_id": str(run.get("workflow_id") or ""),
        "run_state": str(run.get("run_state") or ""),
        "goal_state": str(run.get("goal_state") or ""),
        "terminal": {"status": terminal.get("status"), "reason": terminal.get("reason")},
        "current_step": step_id,
        "iteration": run.get("iteration"),
        "chat_session_id": str(requester.get("chat_session_id") or ""),
        "frontdoor": str(requester.get("frontdoor") or ""),
        "report_path": report,
        "evidence_path": evidence,
        "last_transition_at": last_transition,
        "updated_at": mtime_iso(run_path),
    }


def _run_activation_status(run: dict) -> str:
    activation = run.get("activation")
    if isinstance(activation, dict):
        return str(activation.get("activation_status") or "")
    return ""


def _derived_run_status(row: dict) -> str:
    if row.get("view_error"):
        return "invalid"
    state = str(row.get("run_state") or "")
    if state == "complete":
        return "complete"
    if state in {"failed", "aborted"}:
        return "terminal"
    if state == "waiting_human":
        return "blocked"
    if state in {"waiting_provider", "validating", "remediating"}:
        return "working"
    if state in {"created", "step_queued"}:
        return "active"
    return "unknown"


def load_thin_run(path: Path, *, state_root: Path, run_path: Path | None = None) -> dict:
    payload = _read_json_limited(path, root=state_root)
    if not isinstance(payload, dict):
        row = {"run_id": path.stem, "view_error": "invalid_run_json"}
        return {**row, "activation_status": "", "derived_status": _derived_run_status(row)}
    if payload.get("view_error"):
        row = {"run_id": path.stem, "view_error": payload["view_error"]}
        return {**row, "activation_status": "", "derived_status": _derived_run_status(row)}
    bridge = _task_state_bridge()
    if bridge is not None and hasattr(bridge, "run_task_view"):
        try:
            row = bridge.run_task_view(payload, state_root=state_root, run_path=run_path or path)
            return {
                **row,
                "activation_status": _run_activation_status(payload),
                "derived_status": _derived_run_status(row),
            }
        except Exception:
            pass
    row = _fallback_thin_view(payload, state_root=state_root, run_path=run_path or path)
    return {
        **row,
        "activation_status": _run_activation_status(payload),
        "derived_status": _derived_run_status(row),
    }


def _run_files_with_metadata(state_root: Path) -> tuple[list[Path], bool]:
    runs_dir = state_root / "runs"
    if not runs_dir.is_dir():
        return [], False
    out: list[Path] = []
    try:
        for scanned, path in enumerate(runs_dir.iterdir(), start=1):
            if scanned > MAX_RUN_SCAN_ENTRIES:
                return sorted(out[:MAX_RUN_DISCOVERY_FILES]), True
            name = path.name
            if name.startswith(".") or not name.endswith(".json"):
                continue
            if ".error." in name or ".corrupt-" in name:
                continue
            out.append(path)
            if len(out) > MAX_RUN_DISCOVERY_FILES:
                return sorted(out[:MAX_RUN_DISCOVERY_FILES]), True
    except OSError:
        return sorted(out[:MAX_RUN_DISCOVERY_FILES]), True
    return sorted(out), False


def _run_files(state_root: Path) -> list[Path]:
    paths, _ = _run_files_with_metadata(state_root)
    return paths


def _iso_to_epoch(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    return None


def _stale_seconds(value: str) -> int | None:
    epoch = _iso_to_epoch(value)
    if epoch is None:
        return None
    return max(0, int(time.time() - epoch))


def workflow_run_inventory(
    task_id: str = "",
    session_id: str = "",
    state: str = "",
) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    truncated = False
    for runtime, root in orch_roots():
        paths, root_truncated = _run_files_with_metadata(root)
        truncated = truncated or root_truncated
        for path in paths:
            row = load_thin_run(path, state_root=root, run_path=path)
            if task_id and str(row.get("task_id") or "") != task_id:
                continue
            if session_id and str(row.get("chat_session_id") or "") != session_id:
                continue
            if state and str(row.get("run_state") or "") != state:
                continue
            row = {
                **row,
                "runtime": runtime,
                "orch_root": str(root),
                "stale_seconds": _stale_seconds(str(row.get("last_transition_at") or "")),
            }
            rows.append(row)
    rows.sort(key=lambda item: (str(item.get("last_transition_at") or ""), str(item.get("run_id") or "")), reverse=True)
    return rows, truncated


def workflow_runs(task_id: str = "", session_id: str = "", state: str = "") -> list[dict]:
    rows, _ = workflow_run_inventory(task_id=task_id, session_id=session_id, state=state)
    return rows


def _read_artifact(path: Path, root: Path):
    if not path.exists() or not _path_is_within(path, root):
        return None
    payload = _read_json_limited(path, root=root)
    return payload if isinstance(payload, (dict, list)) else None


def _artifact_list_with_metadata(directory: Path, pattern: str, root: Path) -> tuple[list, dict]:
    if not directory.is_dir() or not _path_is_within(directory, root):
        return [], {"truncated": False, "files_returned": 0, "bytes_read": 0}
    candidates: list[Path] = []
    truncated = False
    try:
        for scanned, path in enumerate(directory.iterdir(), start=1):
            if scanned > MAX_DETAIL_ARTIFACT_SCAN_ENTRIES:
                truncated = True
                break
            if fnmatch.fnmatch(path.name, pattern):
                candidates.append(path)
    except OSError:
        truncated = True

    out: list = []
    bytes_read = 0
    for path in sorted(candidates):
        if not path.is_file() or not _path_is_within(path, root):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if len(out) >= MAX_DETAIL_ARTIFACT_FILES or bytes_read + size > MAX_DETAIL_ARTIFACT_BYTES:
            truncated = True
            break
        payload = _read_json_limited(path, root=root)
        bytes_read += size
        if isinstance(payload, (dict, list)):
            out.append(payload)
    return out, {
        "truncated": truncated,
        "files_returned": len(out),
        "bytes_read": bytes_read,
    }


def _artifact_list(directory: Path, pattern: str, root: Path) -> list:
    artifacts, _ = _artifact_list_with_metadata(directory, pattern, root)
    return artifacts


def _lock_info(runtime: str, root: Path) -> dict:
    run_lock = _run_lock()
    if run_lock is not None and hasattr(run_lock, "inspect_global_lock"):
        try:
            info = run_lock.inspect_global_lock(root)
        except Exception as exc:
            info = {"decision": "error", "error": str(exc), "locked": False, "stale": False, "owner": None}
    else:
        lock_path = root / "locks" / "global-advisory.lock.d"
        info = {"decision": "ok", "locked": lock_path.exists(), "stale": False, "owner": None, "lock_path": str(lock_path)}
    return {"runtime": runtime, "orch_root": str(root), **_project_lock_info(info)}


def workflow_run_detail(
    run_id: str,
    session_id: str = "",
    runtime_filter: str = "",
    orch_root_filter: str = "",
) -> dict | None:
    for runtime, root in orch_roots():
        if runtime_filter and runtime != runtime_filter:
            continue
        if orch_root_filter and str(root) != orch_root_filter:
            continue
        run_path = root / "runs" / f"{run_id}.json"
        if not run_path.is_file() or not _path_is_within(run_path, root):
            continue
        run_record = _read_json_limited(run_path, root=root)
        if not isinstance(run_record, dict):
            run_record = {"run_id": run_id, "view_error": "invalid_run_json"}
        thin = load_thin_run(run_path, state_root=root, run_path=run_path)
        if session_id and str(thin.get("chat_session_id") or "") != session_id:
            continue
        step_id = str(run_record.get("current_step") or thin.get("current_step") or "")
        safe_step = _safe_artifact_id(step_id)
        work_order = None
        report = None
        evidence = None
        if safe_step:
            work_order = _read_artifact(root / "work-orders" / run_id / f"{step_id}.json", root)
            report = _read_artifact(root / "reports" / run_id / f"{step_id}-external-review-report.json", root)
            evidence = _read_artifact(root / "provider-evidence" / run_id / f"{step_id}-provider-evidence.json", root)
        transitions, transition_meta = _artifact_list_with_metadata(
            root / "transitions" / run_id,
            "*.json",
            root,
        )
        rejections, rejection_meta = _artifact_list_with_metadata(
            root / "reports" / run_id,
            "*-rejection-*.json",
            root,
        )
        return {
            "runtime": runtime,
            "orch_root": str(root),
            "run": thin,
            "run_record": _project_run_record(run_record),
            "work_order": work_order,
            "report": report,
            "evidence": evidence,
            "transitions": transitions,
            "rejections": rejections,
            "artifact_truncation": {
                "transitions": transition_meta,
                "rejections": rejection_meta,
            },
            "lock": _lock_info(runtime, root),
        }
    return None


def _validate_optional_id(value: str, error_name: str) -> dict | None:
    if value and not RUN_ID_RE.fullmatch(value):
        return {"error": error_name}
    return None


def _validate_optional_session_id(value: str) -> dict | None:
    if value and not SESSION_ID_RE.fullmatch(value):
        return {"error": "invalid_session_id"}
    return None


def _validate_root_filters(runtime: str = "", orch_root: str = "") -> tuple[dict | None, list[tuple[str, Path]]]:
    roots = orch_roots()
    if runtime and runtime not in {candidate_runtime for candidate_runtime, _ in roots}:
        return {"error": "invalid_runtime"}, roots
    if orch_root and orch_root not in {str(candidate_root) for _, candidate_root in roots}:
        return {"error": "invalid_orch_root"}, roots
    return None, roots


def api_workflow_runs(task_id: str = "", session_id: str = "", state: str = "") -> tuple[dict, int]:
    for value, error_name in ((task_id, "invalid_task_id"), (state, "invalid_run_state")):
        error = _validate_optional_id(value, error_name)
        if error:
            return error, 400
    error = _validate_optional_session_id(session_id)
    if error:
        return error, 400
    if state and state not in RUN_STATES:
        return {"error": "invalid_run_state"}, 400
    roots = orch_roots()
    rows, truncated = workflow_run_inventory(task_id=task_id, session_id=session_id, state=state)
    return {
        "workflow_runs": rows,
        "roots": [str(root) for _, root in roots],
        "truncated": truncated,
        "generated_at": time.time(),
    }, 200


def api_workflow_run(
    run_id: str,
    session_id: str = "",
    runtime: str = "",
    orch_root: str = "",
) -> tuple[dict, int]:
    if not RUN_ID_RE.fullmatch(str(run_id or "")):
        return {"error": "invalid_run_id"}, 400
    error = _validate_optional_session_id(session_id)
    if error:
        return error, 400
    error, _ = _validate_root_filters(runtime, orch_root)
    if error:
        return error, 400
    detail = workflow_run_detail(
        run_id,
        session_id=session_id,
        runtime_filter=runtime,
        orch_root_filter=orch_root,
    )
    if detail is None:
        return {"error": "run_not_found"}, 404
    return {"workflow_run": detail, "generated_at": time.time()}, 200


def api_workflow_lock() -> tuple[dict, int]:
    return {
        "locks": [_lock_info(runtime, root) for runtime, root in orch_roots()],
        "generated_at": time.time(),
    }, 200


def read_inbox(path: Path) -> list[dict]:
    data = jsonish_file(path)
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
        if data.get("status") or data.get("message_id"):
            return [data]
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    raw = read_text(path)
    messages = []
    for match in re.finditer(r"status:\s*\"?([\w-]+)\"?", raw):
        messages.append({"status": match.group(1)})
    return messages


def session_metadata(session_dir: Path) -> dict:
    data = read_json(session_dir / "active-execution-context.json")
    return data if isinstance(data, dict) else {}


def session_dirs() -> list[tuple[str, Path]]:
    out = []
    for runtime, root in STATE_ROOTS:
        if not root.is_dir():
            continue
        for session_dir in sorted(root.iterdir()):
            if not session_dir.is_dir():
                continue
            has_headless_state = any(
                (session_dir / name).exists()
                for name in ("active-execution-context.json", "active-task.json", "queue")
            )
            if has_headless_state:
                out.append((runtime, session_dir))
    return out


def find_session_dir(session_id: str) -> tuple[str, Path] | None:
    if not SESSION_ID_RE.match(session_id):
        return None
    for runtime, root in STATE_ROOTS:
        session_dir = root / session_id
        if session_dir.is_dir():
            return runtime, session_dir
    return None


def chat_name_for_session(session_id: str, cwd: str) -> str:
    for path in TRANSCRIPT_ROOT.glob(f"*/{session_id}.jsonl"):
        name = _scan_transcript_name(path)
        if name:
            return name
    if cwd:
        return Path(cwd).name + " session"
    return session_id[:8]


def _scan_transcript_name(path: Path) -> str:
    summary = ""
    first_user = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                if index > 400 and (summary or first_user):
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "summary" and rec.get("summary"):
                    summary = str(rec["summary"])
                elif not first_user and rec.get("type") == "user":
                    msg = rec.get("message") or {}
                    content = msg.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = str(part.get("text", ""))
                                break
                    text = text.strip()
                    if text and not text.startswith("<"):
                        first_user = text
    except OSError:
        return ""
    name = re.sub(r"\s+", " ", summary or first_user).strip()
    return name[:60]


def context_path_from_pointer(pointer: dict, session_dir: Path) -> str:
    active = pointer.get("active_execution_context")
    if isinstance(active, dict):
        for key in ("path", "context_path", "contextPath", "execution_context_path", "executionContextPath"):
            value = str(active.get(key) or "").strip()
            if value:
                return value
    for key in (
        "active_execution_context_path",
        "activeExecutionContextPath",
        "execution_context_path",
        "executionContextPath",
        "context_path",
        "contextPath",
    ):
        value = str(pointer.get(key) or "").strip()
        if value:
            return value
    default_context = session_dir / "execution_context.json"
    return str(default_context) if default_context.is_file() else ""


def read_execution_context(pointer: dict, session_dir: Path) -> tuple[str, dict]:
    raw_path = context_path_from_pointer(pointer, session_dir)
    if not raw_path:
        return "", {}
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = session_dir / path
    context = read_json(path)
    return str(path), context if isinstance(context, dict) else {}


def markdown_table_rows(path: Path) -> list[dict[str, str]]:
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not headers:
            if "agent_id" in cells:
                headers = cells
            continue
        if cells and all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def active_model_rows() -> list[dict[str, str]]:
    return [row for row in markdown_table_rows(MODEL_REGISTRY) if row.get("status") == "active"]


def load_role_registry() -> list[dict]:
    registry = read_json(ROLE_REGISTRY) or {}
    agents = registry.get("agents") if isinstance(registry, dict) else {}
    role_layers = registry.get("role_layers") if isinstance(registry, dict) else {}
    defaults = registry.get("defaults") if isinstance(registry, dict) else {}
    models = {row.get("agent_id", ""): row for row in active_model_rows()}
    rows = []
    if not isinstance(agents, dict) or not models:
        return rows
    for role_id, model_row in sorted(models.items()):
        if not role_id:
            continue
        config = agents.get(role_id, {})
        config = config if isinstance(config, dict) else {}
        model_ref = config.get("model_registry_ref", role_id)
        model_source = models.get(model_ref, model_row)
        queue_consumer = truthy(config.get("queue_consumer", defaults.get("queue_consumer", False)))
        rows.append(
            {
                "role_id": role_id,
                "team": model_row.get("team") or role_team(role_id),
                "role_layer": config.get("role_layer") or role_layers.get(role_id, defaults.get("role_layer", "")),
                "model_registry_ref": model_ref,
                "provider": model_source.get("provider", ""),
                "intended_model": model_source.get("primary_model", ""),
                "execution_mode": model_source.get("execution_mode", ""),
                "queue_consumer": queue_consumer,
                "allowed_tools": config.get("allowed_tools", defaults.get("allowed_tools", [])),
            }
        )
    return rows


def role_team(role_id: str) -> str:
    if role_id.startswith("tech-"):
        return "tech"
    if role_id.startswith("contents-"):
        return "contents"
    if role_id.startswith("business-"):
        return "business"
    if role_id.startswith("infra-") or role_id in {"git-publisher"}:
        return "infra"
    return "gate"


def latest_report(session_dir: Path, role_id: str) -> tuple[Path | None, dict]:
    report_root = session_dir / "queue" / "reports" / role_id
    if not report_root.is_dir():
        return None, {}
    candidates = [path for path in report_root.rglob("*") if path.is_file() and path.suffix in {".json", ".yaml", ".yml"}]
    if not candidates:
        return None, {}
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    data = jsonish_file(newest)
    return newest, data if isinstance(data, dict) else {"raw": read_text(newest)}


def role_status(role: dict, inbox: list[dict], report_path: Path | None, report: dict) -> dict:
    statuses = {str(message.get("status", "")).lower() for message in inbox}
    report_status = str(report.get("status") or report.get("result") or "").lower()
    report_age = None
    if report_path and report_path.exists():
        report_age = max(0, int(time.time() - report_path.stat().st_mtime))
    if "processing" in statuses or report_status in {"processing", "running", "invoked"}:
        status = "processing"
    elif "pending" in statuses:
        status = "pending"
    elif report_age is not None and report_age <= 120:
        status = "working"
    elif role.get("queue_consumer"):
        status = "ready"
    else:
        status = "deferred"
    return {
        "status": status,
        "busy": status in {"working", "processing"},
        "report_age_sec": report_age,
        "inbox_pending": sum(1 for message in inbox if str(message.get("status", "")).lower() == "pending"),
        "inbox_processing": sum(1 for message in inbox if str(message.get("status", "")).lower() == "processing"),
        "latest_report_path": str(report_path) if report_path else "",
        "latest_report_status": report_status,
    }


def api_sessions() -> dict:
    sessions = []
    for runtime, session_dir in session_dirs():
        metadata = session_metadata(session_dir)
        session_id = str(metadata.get("session_id") or session_dir.name)
        active_task = read_json(session_dir / "active-task.json") or {}
        context_path, context = read_execution_context(metadata, session_dir)
        started_at = str(metadata.get("started_at") or "")
        sessions.append(
            {
                "session_id": session_id,
                "runtime": metadata.get("runtime") or runtime,
                "organization_instance_id": context.get("organization_instance_id", ""),
                "live": bool(metadata),
                "cwd": metadata.get("cwd", ""),
                "started_at": started_at,
                "chat_name": chat_name_for_session(session_id, str(metadata.get("cwd", ""))),
                "active_task_id": active_task.get("task_id") or context.get("task_id", ""),
                "flow_phase": active_task.get("flow_phase") or context.get("phase", ""),
                "execution_context_path": context_path,
            }
        )
    sessions.sort(key=lambda item: item.get("started_at", ""), reverse=True)
    return {"sessions": sessions, "generated_at": time.time()}


def api_org(session_id: str) -> dict:
    found = find_session_dir(session_id)
    if not found:
        return {"error": f"unknown session: {session_id}"}
    runtime, session_dir = found
    metadata = session_metadata(session_dir)
    active_task = read_json(session_dir / "active-task.json") or {}
    context_path, context = read_execution_context(metadata, session_dir)
    roles = load_role_registry()

    teams: dict[str, list[dict]] = {}
    busy_count = 0
    for role in roles:
        role_id = role["role_id"]
        inbox = read_inbox(session_dir / "queue" / "inbox" / f"{role_id}.yaml")
        report_path, report = latest_report(session_dir, role_id)
        status = role_status(role, inbox, report_path, report)
        if status["busy"]:
            busy_count += 1
        teams.setdefault(role["team"], []).append(
            {
                **role,
                **status,
                "active_for_task": active_task.get("task_id") or context.get("task_id", ""),
            }
        )

    ordered = []
    for team in TEAM_ORDER + sorted(set(teams) - set(TEAM_ORDER)):
        if team in teams:
            members = sorted(teams[team], key=lambda item: (not item["busy"], item["role_id"]))
            ordered.append({"team": team, "label": TEAM_LABELS.get(team, team), "agents": members})

    return {
        "session_id": session_id,
        "runtime": metadata.get("runtime") or runtime,
        "organization_instance_id": context.get("organization_instance_id", ""),
        "live": bool(metadata),
        "execution_context_path": context_path,
        "teams": ordered,
        "busy_count": busy_count,
        "agent_count": len(roles),
        "active_task": {
            "task_id": active_task.get("task_id") or context.get("task_id", ""),
            "flow_phase": active_task.get("flow_phase") or context.get("phase", ""),
            "owner_role": active_task.get("owner_role") or context.get("owner_role", ""),
            "last_gate": active_task.get("last_gate") or context.get("last_gate", ""),
        },
        "generated_at": time.time(),
    }


def api_role(session_id: str, role_id: str) -> dict:
    found = find_session_dir(session_id)
    if not found:
        return {"error": f"unknown session: {session_id}"}
    _, session_dir = found
    role = next((item for item in load_role_registry() if item["role_id"] == role_id), None)
    if role is None:
        return {"error": f"unknown role: {role_id}"}
    inbox = read_inbox(session_dir / "queue" / "inbox" / f"{role_id}.yaml")
    report_path, report = latest_report(session_dir, role_id)
    detail = {
        "role": role,
        "inbox": inbox,
        "latest_report_path": str(report_path) if report_path else "",
        "latest_report": report,
    }
    return {
        "role_id": role_id,
        "agent": role,
        "inbox": [
            {
                "message_id": message.get("message_id", ""),
                "status": message.get("status", ""),
                "task_id": message.get("task_id", ""),
                "from_role": message.get("from_role", ""),
                "created_at": message.get("created_at", ""),
            }
            for message in inbox
        ],
        "latest_report_path": str(report_path) if report_path else "",
        "latest_report": report,
        "detail": json.dumps(detail, ensure_ascii=False, indent=2),
        "generated_at": time.time(),
    }


def api_config() -> dict:
    settings = org_json("settings.json", {})
    roles = org_json("role-index.json", {"roles": []}).get("roles") or []
    policies = org_json("policy-index.json", {"policies": []}).get("policies") or []
    teams: dict[str, int] = {}
    for role in roles:
        team = role.get("team") or "unknown"
        teams[team] = teams.get(team, 0) + 1
    return {
        "settings": settings,
        "role_count": len(roles),
        "policy_count": len(policies),
        "teams": teams,
        "policies": [
            {
                "name": Path(str(item.get("path", ""))).name,
                "sha1": item.get("sha1", ""),
                "bytes": item.get("bytes", 0),
                "source": item.get("source", ""),
            }
            for item in policies
        ],
        "generated_at": time.time(),
    }


def api_decide(prompt: str, requested_mode: str = "", organization_state: str = "") -> dict:
    settings = org_json("settings.json", {})
    control = settings.get("control") or {}
    hook_policy = settings.get("hook_policy") or {}
    modes = settings.get("modes") or {}
    maintenance = settings.get("maintenance") or {}

    state = organization_state or control.get("state") or "enabled"
    prompt = prompt.strip()
    prompt_is_maintenance = bool(MAINTENANCE_HINT_RE.search(prompt))
    missing_information = []
    clarification_required = False
    if WEATHER_RE.search(prompt) and not LOCATION_HINT_RE.search(prompt):
        missing_information.append("location")
        clarification_required = True

    if state == "disabled":
        mode = "fast"
        reason = "organization disabled by configuration"
        flow_enabled = False
        main_agent_can_execute = True
    elif state == "maintenance" or prompt_is_maintenance:
        state = "maintenance"
        mode = "fast"
        reason = "organization maintenance or organization-system prompt"
        flow_enabled = bool(maintenance.get("organization_flow_enabled", False))
        main_agent_can_execute = bool(maintenance.get("main_agent_can_execute", True))
    elif requested_mode in {"fast", "strict"}:
        mode = requested_mode
        reason = f"mode explicitly requested: {requested_mode}"
        flow_enabled = True
        main_agent_can_execute = bool((modes.get(mode) or {}).get("main_agent_can_execute"))
    elif len(prompt) <= 120 and FAST_HINT_RE.search(prompt) and not STRICT_HINT_RE.search(prompt):
        mode = "fast"
        reason = "small low-risk prompt matched fast heuristics"
        flow_enabled = True
        main_agent_can_execute = True
    else:
        mode = "strict"
        reason = "default strict mode for non-trivial work"
        flow_enabled = True
        main_agent_can_execute = False

    mode_config = modes.get(mode) or {}
    return {
        "schema_version": 1,
        "decision": "ok",
        "organization_state": state,
        "organization_flow_enabled": flow_enabled,
        "mode": mode,
        "reason": reason,
        "task_required": True,
        "task_policy": control.get("task_policy", "all_work_must_have_task_record"),
        "main_agent_can_execute": main_agent_can_execute,
        "role_dispatch_required": bool(mode_config.get("role_dispatch_required", False)) and flow_enabled,
        "review_required": mode_config.get("review_required", "optional" if mode == "fast" else "required"),
        "vault_update_required": True,
        "missing_information": missing_information,
        "clarification_required": clarification_required,
        "hook_policy": {
            "mode": hook_policy.get("mode", "observer"),
            "hard_block": bool(hook_policy.get("hard_block", False)),
        },
        "performance_target_seconds": mode_config.get("performance_target_seconds"),
        "generated_at": time.time(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ITBOrgViewer/2.0"

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        path = (STATIC_DIR / rel).resolve()
        if not path.is_relative_to(STATIC_DIR) or not path.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            self.send_error(403, "forbidden host")
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        query = {key: value[0] for key, value in qs.items() if value}
        try:
            if url.path == "/" or url.path == "/index.html":
                self._send_static("index.html")
            elif url.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif url.path == "/api/sessions":
                self._send_json(api_sessions())
            elif url.path == "/api/org":
                self._send_json(api_org(query.get("session", "")))
            elif url.path == "/api/role":
                self._send_json(api_role(query.get("session", ""), query.get("role", "")))
            elif url.path == "/api/config":
                self._send_json(api_config())
            elif url.path == "/api/decide":
                self._send_json(api_decide(query.get("prompt", ""), query.get("mode", ""), query.get("state", "")))
            elif url.path == "/api/workflow-runs":
                payload, code = api_workflow_runs(
                    query.get("task", ""),
                    query.get("session", ""),
                    query.get("state", ""),
                )
                self._send_json(payload, code)
            elif url.path == "/api/workflow-run":
                payload, code = api_workflow_run(
                    query.get("run", ""),
                    query.get("session", ""),
                    query.get("runtime", ""),
                    query.get("orch_root", ""),
                )
                self._send_json(payload, code)
            elif url.path == "/api/workflow-lock":
                payload, code = api_workflow_lock()
                self._send_json(payload, code)
            elif url.path.startswith("/static/"):
                self._send_static(url.path[len("/static/"):])
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception:
            try:
                self._send_json({"error": "internal server error"}, 500)
            except Exception:
                pass

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/api/workflow-runs", "/api/workflow-run", "/api/workflow-lock"}:
            self._send_json({"error": "method_not_allowed"}, 405)
            return
        self.send_error(404)


def main() -> None:
    parser = argparse.ArgumentParser(description="ITB Organization Status Viewer")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Sahai: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
