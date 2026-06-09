#!/usr/bin/env python3
"""ITB Organization Status Viewer - read-only local dashboard.

Visualizes which agents of an ITB Organization Instance (one per chat
session) are currently working, based on:
  - ITB state dirs:  ~/.claude/hooks/state/itb/<session_id>/
                     ~/.codex/state/itb/<session_id>/   (best effort)
  - tmux sessions:   itb-org-<org_instance_id>  (window per role)
  - queue inboxes:   <state>/<session>/queue/inbox/<role>.yaml

Strictly read-only: only `tmux list-*` / `capture-pane` are used,
never `send-keys` / `paste-buffer`. Binds to 127.0.0.1 only.

Usage:  python3 server.py [--port 8765]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOME = Path.home()
STATE_ROOTS = [
    ("claude", HOME / ".claude" / "hooks" / "state" / "itb"),
    ("codex", HOME / ".codex" / "state" / "itb"),
]
TRANSCRIPT_ROOT = HOME / ".claude" / "projects"
STATIC_DIR = Path(__file__).resolve().parent / "static"

SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
ACTIVE_WINDOW_SECONDS = 20  # tmux window activity within this -> working
PANE_HISTORY_LINES = 2000

TEAM_ORDER = ["gate", "tech", "contents", "business", "infra"]
TEAM_LABELS = {
    "gate": "Gate",
    "tech": "Engineering",
    "contents": "Contents",
    "business": "Business",
    "infra": "Infrastructure",
}


# ---------------------------------------------------------------- helpers

def run_tmux(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=timeout
        )
        return cp.returncode, cp.stdout, cp.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 1, "", str(exc)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_inbox(path: Path) -> list[dict]:
    """Inbox files are JSON-compatible YAML. Parse tolerantly."""
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    data = None
    try:
        data = json.loads(raw)
    except ValueError:
        # naive fallback: count "status:" lines
        msgs = []
        for m in re.finditer(r"status:\s*\"?(\w+)\"?", raw):
            msgs.append({"status": m.group(1)})
        return msgs
    if isinstance(data, dict):
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return [m for m in msgs if isinstance(m, dict)]
    return []


def session_dirs() -> list[tuple[str, Path]]:
    out = []
    for runtime, root in STATE_ROOTS:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "bootstrap.json").is_file():
                out.append((runtime, d))
    return out


def tmux_live_sessions() -> set[str]:
    rc, out, _ = run_tmux(["list-sessions", "-F", "#{session_name}"])
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def tmux_window_state(tmux_session: str) -> dict[str, dict]:
    """Map window_name -> {activity_epoch, command}."""
    rc, out, _ = run_tmux(
        [
            "list-panes",
            "-s",
            "-t",
            tmux_session,
            "-F",
            "#{window_name}\t#{window_activity}\t#{pane_current_command}",
        ]
    )
    state: dict[str, dict] = {}
    if rc != 0:
        return state
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, activity, cmd = parts[0], parts[1], parts[2]
        try:
            epoch = int(activity)
        except ValueError:
            epoch = 0
        prev = state.get(name)
        if prev is None or epoch > prev["activity"]:
            state[name] = {"activity": epoch, "command": cmd}
    return state


def chat_name_for_session(session_id: str, cwd: str) -> str:
    """Best-effort chat display name from Claude transcript."""
    candidates = list(TRANSCRIPT_ROOT.glob(f"*/{session_id}.jsonl"))
    for path in candidates:
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
            for i, line in enumerate(fh):
                if i > 400 and (summary or first_user):
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
    name = summary or first_user
    name = re.sub(r"\s+", " ", name).strip()
    return name[:60]


# ---------------------------------------------------------------- status

def agent_status(agent: dict, win: dict | None, inbox_msgs: list[dict], now: float,
                 session_live: bool) -> dict:
    """Combine tmux activity x queue state into one display status."""
    statuses = {str(m.get("status", "")).lower() for m in inbox_msgs}
    activity = win["activity"] if win else 0
    age = now - activity if activity else None

    if not session_live:
        status = "offline"
    elif win is None:
        # registered but no tmux window (lazy_activation or not launched)
        status = "lazy" if agent.get("process_status") != "process_ready" else "offline"
    elif age is not None and age <= ACTIVE_WINDOW_SECONDS:
        status = "working"
    elif "processing" in statuses:
        status = "processing"
    elif "pending" in statuses:
        status = "pending"
    else:
        status = "ready"

    return {
        "status": status,
        "busy": status in ("working", "processing"),
        "last_activity_epoch": activity or None,
        "last_activity_age_sec": int(age) if age is not None else None,
        "pane_command": win["command"] if win else None,
        "inbox_pending": sum(1 for m in inbox_msgs
                             if str(m.get("status", "")).lower() == "pending"),
        "inbox_processing": sum(1 for m in inbox_msgs
                                if str(m.get("status", "")).lower() == "processing"),
    }


# ---------------------------------------------------------------- api

def find_session_dir(session_id: str) -> tuple[str, Path] | None:
    if not SESSION_ID_RE.match(session_id):
        return None
    for runtime, root in STATE_ROOTS:
        d = root / session_id
        if d.is_dir() and (d / "bootstrap.json").is_file():
            return runtime, d
    return None


def api_sessions() -> dict:
    live = tmux_live_sessions()
    sessions = []
    for runtime, d in session_dirs():
        boot = read_json(d / "bootstrap.json") or {}
        session_id = boot.get("session_id") or d.name
        tmux_session = boot.get("tmux_session") or ""
        active_task = read_json(d / "active-task.json") or {}
        sessions.append({
            "session_id": session_id,
            "runtime": runtime,
            "organization_instance_id": boot.get("organization_instance_id", ""),
            "tmux_session": tmux_session,
            "live": tmux_session in live,
            "cwd": boot.get("cwd", ""),
            "created_at": boot.get("created_at", ""),
            "chat_name": chat_name_for_session(session_id, boot.get("cwd", "")),
            "active_task_id": active_task.get("task_id", ""),
            "flow_phase": active_task.get("flow_phase", ""),
        })
    sessions.sort(key=lambda s: (not s["live"], s["created_at"]), reverse=False)
    sessions.sort(key=lambda s: s["created_at"], reverse=True)
    sessions.sort(key=lambda s: not s["live"])
    return {"sessions": sessions, "generated_at": time.time()}


def api_org(session_id: str) -> dict:
    found = find_session_dir(session_id)
    if not found:
        return {"error": f"unknown session: {session_id}"}
    runtime, d = found
    boot = read_json(d / "bootstrap.json") or {}
    roster = read_json(d / "roster.json") or []
    active_task = read_json(d / "active-task.json") or {}
    tmux_session = boot.get("tmux_session", "")
    live = tmux_session in tmux_live_sessions()
    windows = tmux_window_state(tmux_session) if live else {}
    now = time.time()

    teams: dict[str, list[dict]] = {}
    busy_count = 0
    for agent in roster:
        if not isinstance(agent, dict):
            continue
        role = agent.get("agent_id", "")
        team = agent.get("team") or "infra"
        window_name = agent.get("tmux_window") or role
        win = windows.get(window_name)
        inbox_msgs = read_inbox(d / "queue" / "inbox" / f"{role}.yaml")
        st = agent_status(agent, win, inbox_msgs, now, live)
        if st["busy"]:
            busy_count += 1
        teams.setdefault(team, []).append({
            "role_id": role,
            "team": team,
            "intended_model": agent.get("intended_model", ""),
            "provider": agent.get("provider", ""),
            "startup_profile": agent.get("startup_profile", ""),
            "process_status": agent.get("process_status", ""),
            "activation_status": agent.get("activation_status", ""),
            "always_active": bool(agent.get("always_active")),
            "active_for_task": agent.get("active_for_task", ""),
            "tmux_target": agent.get("tmux_target", ""),
            **st,
        })

    ordered = []
    for team in TEAM_ORDER + sorted(set(teams) - set(TEAM_ORDER)):
        if team in teams:
            members = sorted(teams[team], key=lambda a: (not a["busy"], a["role_id"]))
            ordered.append({
                "team": team,
                "label": TEAM_LABELS.get(team, team),
                "agents": members,
            })

    return {
        "session_id": session_id,
        "runtime": runtime,
        "organization_instance_id": boot.get("organization_instance_id", ""),
        "tmux_session": tmux_session,
        "live": live,
        "teams": ordered,
        "busy_count": busy_count,
        "agent_count": len(roster),
        "active_task": {
            "task_id": active_task.get("task_id", ""),
            "flow_phase": active_task.get("flow_phase", ""),
            "owner_role": active_task.get("owner_role", ""),
            "last_gate": active_task.get("last_gate", ""),
        },
        "active_window_seconds": ACTIVE_WINDOW_SECONDS,
        "generated_at": now,
    }


def api_pane(session_id: str, role: str) -> dict:
    found = find_session_dir(session_id)
    if not found:
        return {"error": f"unknown session: {session_id}"}
    _, d = found
    roster = read_json(d / "roster.json") or []
    agent = next(
        (a for a in roster if isinstance(a, dict) and a.get("agent_id") == role),
        None,
    )
    if agent is None:
        return {"error": f"unknown role: {role}"}

    boot = read_json(d / "bootstrap.json") or {}
    tmux_session = boot.get("tmux_session", "")
    target = agent.get("tmux_target") or (
        f"{tmux_session}:{agent.get('tmux_window') or role}.0"
    )
    content, err = "", ""
    if tmux_session in tmux_live_sessions():
        rc, out, stderr = run_tmux(
            ["capture-pane", "-p", "-S", f"-{PANE_HISTORY_LINES}", "-t", target],
            timeout=8.0,
        )
        if rc == 0:
            content = out
        else:
            err = stderr.strip() or "capture-pane failed"
    else:
        err = "tmux session is not running"

    inbox_msgs = read_inbox(d / "queue" / "inbox" / f"{role}.yaml")
    return {
        "role_id": role,
        "tmux_target": target,
        "content": content,
        "error": err,
        "inbox": [
            {
                "message_id": m.get("message_id", ""),
                "status": m.get("status", ""),
                "task_id": m.get("task_id", ""),
                "from_role": m.get("from_role", ""),
                "created_at": m.get("created_at", ""),
            }
            for m in inbox_msgs
        ],
        "agent": {
            "team": agent.get("team", ""),
            "intended_model": agent.get("intended_model", ""),
            "provider": agent.get("provider", ""),
            "startup_profile": agent.get("startup_profile", ""),
            "process_status": agent.get("process_status", ""),
            "activation_status": agent.get("activation_status", ""),
            "active_for_task": agent.get("active_for_task", ""),
            "last_seen_at": agent.get("last_seen_at", ""),
            "notes": agent.get("notes", ""),
        },
        "generated_at": time.time(),
    }


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "ITBOrgViewer/1.0"

    def log_message(self, format, *args):  # noqa: A002  quiet
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
        if not str(path).startswith(str(STATIC_DIR)) or not path.is_file():
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
        # DNS rebinding hardening: only accept local Host headers.
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            self.send_error(403, "forbidden host")
            return
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        q = {k: v[0] for k, v in qs.items() if v}
        try:
            if url.path == "/" or url.path == "/index.html":
                self._send_static("index.html")
            elif url.path == "/api/sessions":
                self._send_json(api_sessions())
            elif url.path == "/api/org":
                self._send_json(api_org(q.get("session", "")))
            elif url.path == "/api/pane":
                self._send_json(api_pane(q.get("session", ""), q.get("role", "")))
            elif url.path.startswith("/static/"):
                self._send_static(url.path[len("/static/"):])
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # keep server alive, report error
            try:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="ITB Organization Status Viewer")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ITB Org Viewer: http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
