#!/usr/bin/env python3
"""ITD organization-quality monitor.

Runtime invariant: the default run may write only the ITD report file.
All other files are read-only inputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def first_env_path(names: list[str], default: Path) -> Path:
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return default.expanduser()


AGENTS_VAULT = env_path(
    "AGENTS_VAULT_ROOT",
    Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Agents-Vault",
)
USER_VAULT = first_env_path(
    ["USER_VAULT_ROOT", "YASU_VAULT_ROOT"],
    Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Personal Vault",
)
YASU_VAULT = USER_VAULT
SKILLS_REPO = env_path("SKILLS_REPO_ROOT", Path.home() / "skills-repo")
DOTFILES = env_path("DOTFILES_ROOT", Path.home() / "dotfiles")

DEFAULT_REPORT = AGENTS_VAULT / "03-Contexts/Reports/ITD-Monitoring-Report.md"
DEFAULT_ROOTS = [AGENTS_VAULT, YASU_VAULT, SKILLS_REPO, DOTFILES]
SNAPSHOT_RE = re.compile(r"<!-- ITD_SNAPSHOT (.*?) -->", re.DOTALL)
JST = dt.timezone(dt.timedelta(hours=9), "JST")


def run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rel_or_abs(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def is_report_path(path_text: str, root: Path, report: Path) -> bool:
    candidates = {path_text.strip(), path_text.strip().strip('"')}
    report_rel = rel_or_abs(report, root)
    return report_rel in candidates or str(report) in candidates


def git_snapshot(root: Path, report: Path) -> dict[str, Any]:
    if not root.exists():
        return {"root": str(root), "exists": False, "git": False}

    code, top, _ = run(["git", "rev-parse", "--show-toplevel"], root)
    if code != 0:
        return {"root": str(root), "exists": True, "git": False}

    repo = Path(top)
    _, head, _ = run(["git", "rev-parse", "HEAD"], repo)
    _, branch, _ = run(["git", "branch", "--show-current"], repo)
    _, status, _ = run(["git", "status", "--porcelain=v1", "-uall"], repo)

    lines = []
    for line in status.splitlines():
        if not line:
            continue
        path_text = line[3:] if len(line) > 3 else line
        if is_report_path(path_text, repo, report):
            continue
        lines.append(line)

    normalized = "\n".join(sorted(lines))
    return {
        "root": str(repo),
        "exists": True,
        "git": True,
        "branch": branch,
        "head": head,
        "status_count": len(lines),
        "status_digest": sha256_text(normalized),
        "status_lines": lines,
    }


def build_snapshot(roots: list[Path], report: Path) -> dict[str, Any]:
    repos: dict[str, Any] = {}
    seen: set[str] = set()
    for root in roots:
        snap = git_snapshot(root, report)
        key = snap.get("root", str(root))
        if key in seen:
            continue
        seen.add(key)
        repos[key] = {k: v for k, v in snap.items() if k != "status_lines"}
    digest = sha256_text(json.dumps(repos, sort_keys=True, ensure_ascii=False))
    return {"version": 1, "digest": digest, "repos": repos}


def read_previous_snapshot(report: Path) -> dict[str, Any] | None:
    if not report.exists():
        return None
    text = report.read_text(encoding="utf-8")
    matches = SNAPSHOT_RE.findall(text)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def task_files(agents_vault: Path) -> list[Path]:
    project_root = agents_vault / "01-Projects"
    if not project_root.exists():
        return []
    return sorted(project_root.rglob("TSK-*.md"))


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip().strip('"')
    return out


def parse_task_index(agents_vault: Path) -> dict[str, str]:
    path = agents_vault / "00-Inbox&Tasks/Task-Index.md"
    if not path.exists():
        return {}
    index: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| TSK-"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 5:
            index[cells[0]] = cells[4]
    return index


def parse_kanban(agents_vault: Path) -> dict[str, str]:
    path = agents_vault / "00-Inbox&Tasks/Kanban.md"
    if not path.exists():
        return {}
    current = ""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line.removeprefix("## ").strip()
            continue
        for task_id in re.findall(r"TSK-\d{4}", line):
            out[task_id] = current
    return out


def kanban_section_for(status: str) -> str:
    return {
        "inbox": "Inbox",
        "triage": "Inbox",
        "ready": "Ready",
        "in_progress": "In Progress",
        "domain_review": "Review",
        "independent_review": "Review",
        "waiting_human": "Waiting Human",
        "blocked": "Waiting Human",
        "done": "Done",
        "archived": "Done",
    }.get(status, "")


def finding(event_type: str, severity: str, title: str, evidence: str, affected: list[str]) -> dict[str, Any]:
    fingerprint = sha256_text(
        json.dumps(
            {
                "event_type": event_type,
                "severity": severity,
                "title": title,
                "evidence": evidence,
                "affected": affected,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )[:16]
    return {
        "finding_id": f"{event_type}:{fingerprint}",
        "severity": severity,
        "event_type": event_type,
        "title": title,
        "evidence": evidence,
        "affected_paths": affected,
        "recommended_owner": "infra-task-dispatcher",
        "recommended_gtc_request": f"{title} を確認し、必要なら是正タスクを作成する。",
        "do_not_auto_fix": True,
    }


def collect_git_findings(roots: list[Path], report: Path) -> list[dict[str, Any]]:
    findings = []
    seen: set[str] = set()
    for root in roots:
        snap = git_snapshot(root, report)
        repo = snap.get("root", str(root))
        if repo in seen:
            continue
        seen.add(repo)
        lines = snap.get("status_lines", [])
        if not lines:
            continue
        sample = "; ".join(lines[:8])
        severity = "P0" if "Agents-Vault" in repo or "skills-repo" in repo else "P1"
        findings.append(
            finding(
                "git_dirty_detected",
                severity,
                f"Git dirty state detected in {repo}",
                f"{len(lines)} status entries. sample: {sample}",
                [repo],
            )
        )
    return findings


def collect_gate_findings(agents_vault: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    index = parse_task_index(agents_vault)
    kanban = parse_kanban(agents_vault)

    for path in task_files(agents_vault):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        task_id = meta.get("task_id") or path.name.split("-", 2)[0] + "-" + path.name.split("-", 2)[1]
        status = meta.get("status", "")
        rel = rel_or_abs(path, agents_vault)

        if status and index.get(task_id) and index[task_id] != status:
            findings.append(
                finding(
                    "kanban_desync",
                    "P0",
                    f"Task Index status differs for {task_id}",
                    f"Task Detail status={status}, Task Index status={index[task_id]}",
                    [rel, "00-Inbox&Tasks/Task-Index.md"],
                )
            )

        expected_section = kanban_section_for(status)
        if expected_section and kanban.get(task_id) and kanban[task_id] != expected_section:
            findings.append(
                finding(
                    "kanban_desync",
                    "P0",
                    f"Kanban section differs for {task_id}",
                    f"Task Detail status={status}, expected Kanban={expected_section}, actual={kanban[task_id]}",
                    [rel, "00-Inbox&Tasks/Kanban.md"],
                )
            )

        if status not in {"done", "archived"}:
            required = [
                "gate_intake_envelope_created",
                "task_detail_created_or_updated",
                "task_index_synced",
                "kanban_synced",
                "project_manager_handoff_created",
                "review_line_defined",
            ]
            missing = [item for item in required if item not in text]
            if "Project Manager Handoff" not in text:
                missing.append("Project Manager Handoff")
            if missing:
                findings.append(
                    finding(
                        "gate_preflight_missing",
                        "P1",
                        f"Preflight evidence missing for {task_id}",
                        "missing: " + ", ".join(sorted(set(missing))),
                        [rel],
                    )
                )

        if status in {"domain_review", "independent_review", "waiting_human", "in_progress"}:
            last_updated = meta.get("last_updated", "")
            try:
                updated = dt.date.fromisoformat(last_updated)
                age = (dt.datetime.now(JST).date() - updated).days
            except ValueError:
                age = 999
            if age >= 3:
                findings.append(
                    finding(
                        "review_stalled",
                        "P1",
                        f"Task appears stalled: {task_id}",
                        f"status={status}, last_updated={last_updated or 'unknown'}, age_days={age}",
                        [rel],
                    )
                )

        if status == "done":
            missing_done = []
            for section in ["## Reviews", "## Deliverables", "## Vault Updates"]:
                if section not in text:
                    missing_done.append(section)
            if re.search(r"pending|TODO|未完了", text, re.IGNORECASE):
                missing_done.append("pending marker")
            if missing_done:
                findings.append(
                    finding(
                        "vault_update_missing",
                        "P2",
                        f"Done task has incomplete completion evidence: {task_id}",
                        "missing_or_pending: " + ", ".join(missing_done),
                        [rel],
                    )
                )

    for task_id, idx_status in index.items():
        if task_id not in kanban:
            findings.append(
                finding(
                    "kanban_desync",
                    "P0",
                    f"Task Index entry missing from Kanban: {task_id}",
                    f"Task Index status={idx_status}",
                    ["00-Inbox&Tasks/Task-Index.md", "00-Inbox&Tasks/Kanban.md"],
                )
            )

    return findings


def render_report(run_id: str, started_at: str, snapshot: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    severity_counts: dict[str, int] = {}
    for item in findings:
        severity_counts[item["severity"]] = severity_counts.get(item["severity"], 0) + 1

    lines = [
        "",
        f"## Run {run_id}",
        "",
        f"- started_at: {started_at}",
        "- changed_since_last_run: true",
        f"- findings_count: {len(findings)}",
        f"- severity_counts: `{json.dumps(severity_counts, ensure_ascii=False, sort_keys=True)}`",
        "",
        "### Snapshot",
        "",
        "```json",
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "### Findings",
        "",
    ]
    if not findings:
        lines.append("- No findings.")
    else:
        for item in findings:
            lines.extend(
                [
                    f"- **{item['severity']} {item['finding_id']}** {item['title']}",
                    f"  - event_type: `{item['event_type']}`",
                    f"  - evidence: {item['evidence']}",
                    "  - affected_paths: " + ", ".join(f"`{p}`" for p in item["affected_paths"]),
                    f"  - recommended_owner: `{item['recommended_owner']}`",
                    f"  - recommended_gtc_request: {item['recommended_gtc_request']}",
                    "  - do_not_auto_fix: true",
                ]
            )

    lines.extend(
        [
            "",
            "### GTC Candidates",
            "",
        ]
    )
    candidates = [item for item in findings if item["severity"] in {"P0", "P1"}]
    if not candidates:
        lines.append("- No P0/P1 candidates.")
    else:
        for item in candidates:
            lines.append(f"- `{item['finding_id']}`: {item['recommended_gtc_request']}")

    lines.extend(
        [
            "",
            "### Skipped Items",
            "",
            "- ITD report file itself is excluded from snapshots to avoid self-triggering.",
            "- ITD runtime does not auto-fix, auto-create GTC tasks, or auto-commit.",
            "",
            "### Completion Note",
            "",
            "ITD monitoring run completed. This run only appends to the ITD report.",
            "",
            f"<!-- ITD_SNAPSHOT {json.dumps(snapshot, ensure_ascii=False, sort_keys=True)} -->",
            "",
        ]
    )
    return "\n".join(lines)


def ensure_report_header(report: Path) -> str:
    if report.exists():
        return report.read_text(encoding="utf-8")
    return "\n".join(
        [
            "---",
            "type: itd-monitoring-report",
            "owner: infra-task-dispatcher",
            "status: active",
            "---",
            "",
            "# ITD Monitoring Report",
            "",
            "This file is append-only for ITD runtime.",
            "No agent other than ITD runtime may update this file.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ITD organization-quality monitor.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="write even if snapshot is unchanged")
    parser.add_argument("--max-findings", type=int, default=80)
    args = parser.parse_args()

    report = args.report.expanduser()
    roots = [p.expanduser() for p in args.roots] if args.roots else DEFAULT_ROOTS
    now = dt.datetime.now(JST)
    started_at = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    run_id = now.strftime("%Y%m%dT%H%M%S%z")

    previous = read_previous_snapshot(report)
    snapshot = build_snapshot(roots, report)

    if previous and previous.get("digest") == snapshot.get("digest") and not args.force:
        if args.dry_run:
            print("unchanged: no report write")
        return 0

    findings = collect_git_findings(roots, report)
    if AGENTS_VAULT in roots or any(str(root) == str(AGENTS_VAULT) for root in roots):
        findings.extend(collect_gate_findings(AGENTS_VAULT))
    findings = findings[: args.max_findings]

    report_text = render_report(run_id, started_at, snapshot, findings)
    if args.dry_run:
        print(report_text)
        return 0

    report.parent.mkdir(parents=True, exist_ok=True)
    current = ensure_report_header(report)
    report.write_text(current.rstrip() + "\n" + report_text, encoding="utf-8")
    print(f"wrote {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
