#!/usr/bin/env python3
"""ITB Organization Status Viewer - read-only local dashboard.

Visualizes an ITB Organization Instance from typed state, queue inboxes, and
provider reports. The viewer never starts agents, never calls providers, and
never mutates live Claude/Codex configuration.

Usage:  python3 server.py [--port 8765]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

SESSION_ID_RE = re.compile(r"^[0-9a-zA-Z_.:-]{3,128}$")
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
    r"(組織|organization|Saihai|Agent-Org-Viewer|configure-organization|COMMON-AGENTS|\bHook\b|\bITB\b|gate-prompt-formatter|infra-team-bootstrap)",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="ITB Organization Status Viewer")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Saihai: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
