#!/usr/bin/env python3
"""HTTP wrapper for the host-owned P0 frontdoor orchestrator."""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import frontdoor_orchestrator as frontdoor

RUN_DRAIN_RE = re.compile(r"^/orchestrator/runs/([^/]+)/drain$")
RUN_READ_RE = re.compile(r"^/orchestrator/runs/([^/]+)$")
REQUEST_READ_RE = re.compile(r"^/frontdoor/requests/([^/]+)$")

INDEX_HTML = """<!doctype html>
<html lang="en" data-frontdoor-ui="p0">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>P0 Frontdoor</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #171a1f;
      --muted: #647082;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-2: #7c3aed;
      --danger: #b42318;
      --code: #111827;
      --code-text: #f9fafb;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111418;
        --panel: #181d23;
        --text: #e8edf3;
        --muted: #9aa7b8;
        --line: #2b333d;
        --accent: #2dd4bf;
        --accent-2: #a78bfa;
        --danger: #fb7185;
        --code: #07090c;
        --code-text: #e8edf3;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 18px 24px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
    }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 480px) minmax(0, 1fr);
      gap: 16px;
      padding: 16px;
      max-width: 1440px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-width: 0;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }
    h2 {
      margin: 0;
      font-size: 14px;
      font-weight: 650;
    }
    form {
      display: grid;
      gap: 12px;
      padding: 14px;
    }
    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: transparent;
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
      font-size: 13px;
      line-height: 1.35;
    }
    textarea {
      min-height: 86px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .grid-two {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      padding: 14px;
      border-top: 1px solid var(--line);
    }
    button {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: transparent;
      color: var(--text);
      font: inherit;
      font-size: 13px;
      font-weight: 650;
      cursor: pointer;
    }
    button.primary {
      border-color: color-mix(in srgb, var(--accent) 70%, var(--line));
      background: color-mix(in srgb, var(--accent) 14%, transparent);
    }
    button.secondary {
      border-color: color-mix(in srgb, var(--accent-2) 55%, var(--line));
      background: color-mix(in srgb, var(--accent-2) 10%, transparent);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    .status {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    pre {
      margin: 0;
      min-height: 520px;
      overflow: auto;
      padding: 14px;
      background: var(--code);
      color: var(--code-text);
      border-radius: 0 0 8px 8px;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      .actions { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <h1>P0 Frontdoor</h1>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>Request</h2>
        <span id="status" class="status">idle</span>
      </div>
      <form id="frontdoor-form">
        <div class="grid-two">
          <label>Task ID
            <input id="task-id" value="TSK-ui">
          </label>
          <label>Request ID
            <input id="request-id" value="req-ui">
          </label>
        </div>
        <div class="grid-two">
          <label>Run ID
            <input id="run-id" value="run-ui">
          </label>
          <label>Report Path
            <input id="report-path" placeholder="optional">
          </label>
        </div>
        <label>Prompt
          <textarea id="prompt">Run a bounded readonly external review.</textarea>
        </label>
        <label>Context Refs
          <textarea id="refs">organization/runtime/workflows/README.md</textarea>
        </label>
        <label>Allowed Paths
          <textarea id="allowed-paths">organization/runtime/workflows</textarea>
        </label>
        <label>Typed Classification
          <textarea id="classification">{"classification_version":"1","task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}</textarea>
        </label>
      </form>
      <div class="actions">
        <button class="primary" data-action="propose">Propose</button>
        <button class="primary" data-action="approve">Approve</button>
        <button class="secondary" data-action="create">Create Run</button>
        <button data-action="drain">Drain</button>
        <button data-action="prepare">Prepare</button>
        <button data-action="validate">Validate</button>
        <button data-action="read-request">Read Request</button>
        <button data-action="read-run">Read Run</button>
        <button data-action="health">Health</button>
      </div>
    </section>
    <section>
      <div class="section-head">
        <h2>State</h2>
        <span id="endpoint" class="status">none</span>
      </div>
      <pre id="output">{}</pre>
    </section>
  </main>
  <script>
    const byId = (id) => document.getElementById(id);
    const fields = {
      taskId: byId("task-id"),
      requestId: byId("request-id"),
      runId: byId("run-id"),
      reportPath: byId("report-path"),
      prompt: byId("prompt"),
      refs: byId("refs"),
      allowedPaths: byId("allowed-paths"),
      classification: byId("classification"),
    };
    const output = byId("output");
    const statusEl = byId("status");
    const endpointEl = byId("endpoint");

    function lines(value) {
      return value.split(/\\n|,/).map((item) => item.trim()).filter(Boolean);
    }

    function classification() {
      return JSON.parse(fields.classification.value);
    }

    function setBusy(isBusy) {
      statusEl.textContent = isBusy ? "running" : "idle";
      document.querySelectorAll("button").forEach((button) => {
        button.disabled = isBusy;
      });
    }

    async function call(method, endpoint, body) {
      setBusy(true);
      endpointEl.textContent = endpoint;
      try {
        const options = { method, headers: { "Content-Type": "application/json" } };
        if (body !== undefined) {
          options.body = JSON.stringify(body);
        }
        const response = await fetch(endpoint, options);
        const payload = await response.json();
        output.textContent = JSON.stringify(payload, null, 2);
        statusEl.textContent = response.ok ? "ok" : "blocked";
      } catch (error) {
        output.textContent = JSON.stringify({ decision: "blocked", reason: String(error) }, null, 2);
        statusEl.textContent = "error";
      } finally {
        document.querySelectorAll("button").forEach((button) => {
          button.disabled = false;
        });
      }
    }

    const actions = {
      health: () => call("GET", "/healthz"),
      propose: () => call("POST", "/frontdoor/propose", {
        task_id: fields.taskId.value,
        request_id: fields.requestId.value,
        prompt: fields.prompt.value,
        refs: lines(fields.refs.value),
        allowed_paths: lines(fields.allowedPaths.value),
        classification: classification(),
        frontdoor: "codex",
        chat_session_id: "frontdoor-ui",
      }),
      approve: () => call("POST", "/frontdoor/approve", {
        request_id: fields.requestId.value,
        human_action_id: `ui-${Date.now()}`,
      }),
      create: () => call("POST", "/orchestrator/runs", {
        request_id: fields.requestId.value,
        run_id: fields.runId.value,
      }),
      drain: () => call("POST", `/orchestrator/runs/${encodeURIComponent(fields.runId.value)}/drain`, {}),
      prepare: () => call("POST", "/provider/claude/prepare", { run_id: fields.runId.value }),
      validate: () => call("POST", "/provider/reports/validate", {
        run_id: fields.runId.value,
        report_path: fields.reportPath.value,
      }),
      "read-request": () => call("GET", `/frontdoor/requests/${encodeURIComponent(fields.requestId.value)}`),
      "read-run": () => call("GET", `/orchestrator/runs/${encodeURIComponent(fields.runId.value)}`),
    };

    document.querySelector(".actions").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      event.preventDefault();
      actions[button.dataset.action]();
    });
  </script>
</body>
</html>
"""


class FrontdoorServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, *, state_root: Path):
        super().__init__(server_address, handler_class)
        self.state_root = state_root


class Handler(BaseHTTPRequestHandler):
    server_version = "P0Frontdoor/1.0"

    def log_message(self, format, *args):  # noqa: A002
        return

    @property
    def state_root(self) -> Path:
        return self.server.state_root  # type: ignore[attr-defined]

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise frontdoor.FrontdoorError("invalid json body") from exc
        if not isinstance(data, dict):
            raise frontdoor.FrontdoorError("json body must be object")
        return data

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            if self.path in {"/", "/index.html"}:
                self._send_html(INDEX_HTML)
                return
            if self.path == "/healthz":
                self._send_json({"schema_version": 1, "decision": "ok"})
                return
            request_match = REQUEST_READ_RE.match(self.path)
            if request_match:
                record = frontdoor.read_json(frontdoor.request_path(self.state_root, request_match.group(1)))
                self._send_json({"schema_version": 1, "decision": "ok", "request": record})
                return
            run_match = RUN_READ_RE.match(self.path)
            if run_match:
                run = frontdoor.read_json(frontdoor.run_path(self.state_root, run_match.group(1)))
                self._send_json({"schema_version": 1, "decision": "ok", "workflow_run": run})
                return
            self._send_json({"schema_version": 1, "decision": "blocked", "reason": "not_found"}, 404)
        except frontdoor.FrontdoorError as exc:
            self._send_json({"schema_version": 1, "decision": "blocked", "reason": str(exc)}, 400)

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            if self.path == "/frontdoor/propose":
                payload = frontdoor.proposed_request(
                    state_root=self.state_root,
                    task_id=str(body["task_id"]),
                    request_id=str(body["request_id"]),
                    user_prompt=str(body.get("prompt") or ""),
                    refs=list(body.get("refs") or []),
                    classification=body.get("classification") if isinstance(body.get("classification"), dict) else None,
                    allowed_paths=list(body.get("allowed_paths") or []),
                    expires_at=str(body.get("expires_at") or "run_terminal"),
                    frontdoor=str(body.get("frontdoor") or "codex"),
                    chat_session_id=str(body.get("chat_session_id") or ""),
                )
                self._send_json(payload)
                return
            if self.path == "/frontdoor/approve":
                payload = frontdoor.approve_request(
                    state_root=self.state_root,
                    request_id=str(body["request_id"]),
                    human_action_id=str(body["human_action_id"]),
                )
                status = 200 if payload.get("decision") == "ok" else 400
                self._send_json(payload, status)
                return
            if self.path == "/orchestrator/runs":
                payload = frontdoor.create_run(
                    state_root=self.state_root,
                    request_id=str(body["request_id"]),
                    run_id=str(body.get("run_id") or ""),
                    resume_policy=str(body.get("resume_policy") or "manual"),
                )
                self._send_json(payload)
                return
            drain_match = RUN_DRAIN_RE.match(self.path)
            if drain_match:
                payload = frontdoor.drain_run(state_root=self.state_root, run_id=drain_match.group(1))
                self._send_json(payload)
                return
            if self.path == "/provider/claude/prepare":
                payload = frontdoor.prepare_claude_adapter(
                    state_root=self.state_root,
                    run_id=str(body["run_id"]),
                )
                status = 200 if payload.get("decision") == "ok" else 400
                self._send_json(payload, status)
                return
            if self.path == "/provider/reports/validate":
                payload = frontdoor.validate_report(
                    state_root=self.state_root,
                    run_id=str(body["run_id"]),
                    report_path_arg=str(body.get("report_path") or ""),
                )
                status = 200 if payload.get("decision") == "ok" else 400
                self._send_json(payload, status)
                return
            self._send_json({"schema_version": 1, "decision": "blocked", "reason": "not_found"}, 404)
        except KeyError as exc:
            self._send_json(
                {"schema_version": 1, "decision": "blocked", "reason": f"missing field: {exc.args[0]}"},
                400,
            )
        except frontdoor.FrontdoorError as exc:
            self._send_json({"schema_version": 1, "decision": "blocked", "reason": str(exc)}, 400)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run P0 frontdoor HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--state-root", default=str(frontdoor.DEFAULT_STATE_ROOT))
    args = parser.parse_args()

    server = FrontdoorServer(
        (args.host, args.port),
        Handler,
        state_root=Path(args.state_root).expanduser(),
    )
    print(f"P0 frontdoor API: http://{args.host}:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
