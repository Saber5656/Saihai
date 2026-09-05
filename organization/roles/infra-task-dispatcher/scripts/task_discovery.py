"""Read-only Task Detail discovery; a discovery result never grants execution.

Accepted authorities live under 01-Projects (including 00_Archive): task.md,
copy variants, legacy TSK-*.md, or files explicitly typed task-detail/task-record.
task_id is the authoritative scalar; a conflicting conventional path is an
error. Legacy four-digit path titles are not part of their IDs; eight-digit
and PENDING IDs retain their suffixes. No files are renamed or removed.

waiting_human is only a display backed by these Task Detail scalar fields:
human_decision_id, human_decision_status=pending, human_decision_question,
human_decision_kind (requirements/scope/permission/cost/irreversible), and a
timezone-aware future human_decision_expires_at. Missing/unsupported evidence
produces state_unverified, not a question. These observations do not grant
permission or claim that a decision was delivered through a user interface.

Unknown/non-task types, including #138 incidental finding lists, remain
projections; their IDs, checkboxes and Issue links never become task authority.
Writer ACK, finding reconciliation and GitHub synchronization are separate
consumer contracts and cannot be inferred from this discovery result.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from pathlib import Path
import re
from typing import Any

TASK_ID_RE = re.compile(r"TSK-(?:\d{8}(?:-[A-Za-z0-9_]+)*|\d{4}|PENDING-[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)\Z")
TASK_TYPES = {"task-detail", "task-record", "task"}
STATES = {
    "inbox", "triage", "ready", "in_progress", "domain_review", "independent_review",
    "waiting_human", "blocked", "done", "archived", "deferred", "recorded",
    "dependency_deferred", "pending", "pending_sync", "waiting_runtime", "waiting_quality",
}
PASSIVE_STATES = {"deferred", "recorded", "dependency_deferred", "pending", "pending_sync"}
DECISION_KINDS = {"requirements", "scope", "permission", "cost", "irreversible"}


def parse_frontmatter(text: str) -> dict[str, str]:
    """Read top-level scalar metadata without interpreting YAML code or nested history.

    Authority fields use single-line scalars. Duplicate top-level keys or an
    unterminated header are ambiguous, never last-write-wins.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            return out
        if not line or line[0].isspace() or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key[:1] in {"'", '"'}:
            if len(key) < 2 or key[-1] != key[0] or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key[1:-1]):
                raise ValueError("unsupported_frontmatter_key")
            key = key[1:-1]
        if key in out:
            raise ValueError("duplicate_frontmatter_key")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.rfind(quote)
            if end == 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise ValueError("invalid_scalar_quote")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    raise ValueError("unterminated_frontmatter")


def path_identity(name: str) -> str:
    """Legacy four-digit paths contain a title; date/PENDING paths contain full IDs."""
    name = re.sub(r"(?: \d+| \([^)]*(?:copy|conflict)[^)]*\))$", "", name, flags=re.IGNORECASE)
    short = re.match(r"^(TSK-\d{4})(?:-|$)", name)
    if short:
        return short[1]
    return name if TASK_ID_RE.fullmatch(name) else ""


def frontmatter_claims(text: str) -> tuple[set[str], set[str]]:
    """Collect header claims for quarantine only, never to recover an authority.

    Parse each top-level scalar separately so duplicate keys cannot hide a
    typed copy or any of its conflicting identity claims behind a parse error.
    """
    types: set[str] = set()
    identities: set[str] = set()
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return types, identities
    for line in lines[1:]:
        if line == "---":
            break
        if not re.match(r"^(?:type|task_id|['\"](?:type|task_id)['\"])\s*:", line):
            continue
        try:
            scalar = parse_frontmatter("---\n" + line + "\n---")
        except ValueError:
            continue
        if "type" in scalar:
            types.add(scalar["type"])
        if TASK_ID_RE.fullmatch(scalar.get("task_id", "")):
            identities.add(scalar["task_id"])
    return types, identities


def live_human_decision(meta: dict[str, str], now: dt.datetime) -> bool:
    question = meta.get("human_decision_question", "")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", meta.get("human_decision_id", ""))
            or not question or question[0] in "|>&*![{" or question.lower() in {"null", "false", "true", "~"}
            or meta.get("human_decision_status") != "pending"
            or meta.get("human_decision_kind") not in DECISION_KINDS):
        return False
    try:
        expires = dt.datetime.fromisoformat(meta.get("human_decision_expires_at", "").replace("Z", "+00:00"))
        return expires.tzinfo is not None and expires > now
    except ValueError:
        return False


def discover_tasks(agents_vault: Path, *, now: dt.datetime | None = None,
                   excluded_paths: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Return unique authorities, ambiguous inputs and non-authoritative projections.

    No copy wins, even when its bytes match. Archive location never rewrites the
    authoritative status. Symlink inputs are not followed. Results and metadata
    are observations only, not writer acknowledgements or dispatch requests.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    project_root = agents_vault / "01-Projects"
    grouped: dict[str, list[dict[str, Any]]] = {}
    invalid_claims: set[str] = set()
    result: dict[str, Any] = {"tasks": {}, "problems": [], "projections": [], "inputs": {}, "complete": True}

    def problem(kind: str, paths: list[Path], reason: str, task_id: str = "") -> None:
        result["problems"].append({"event_type": kind, "paths": paths, "reason": reason, "task_id": task_id})

    def traversal_error(error: OSError) -> None:
        result["complete"] = False
        problem("task_discovery_incomplete", [Path(error.filename) if error.filename else project_root],
                type(error).__name__)

    if project_root.is_symlink():
        problem("task_identity_invalid", [project_root], "symlink_project_root")
        return result
    if not project_root.exists():
        return result
    for directory, dirs, files in os.walk(project_root, followlinks=False, onerror=traversal_error):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".obsidian"} and not (base / d).is_symlink())
        for filename in sorted(files):
            path = base / filename
            if path in excluded_paths or path.suffix.lower() != ".md":
                continue
            conventional = filename.startswith("TSK-") or bool(re.fullmatch(r"task(?:[ ._-].*)?\.md", filename))
            path_name = path.stem if filename.startswith("TSK-") else path.parent.name
            implied_id = path_identity(path_name)
            if path.is_symlink():
                if conventional:
                    problem("task_identity_invalid", [path], "symlink_task")
                    invalid_claims.add(implied_id)
                continue
            raw = b""
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                meta = parse_frontmatter(text)
            except OSError as exc:
                traversal_error(exc)
                continue
            except (UnicodeError, ValueError) as exc:
                claimed_types, claimed_ids = frontmatter_claims(raw.decode("utf-8", errors="replace"))
                if conventional or claimed_types & TASK_TYPES:
                    problem("task_identity_invalid", [path], type(exc).__name__)
                    invalid_claims.add(implied_id)
                    invalid_claims.update(claimed_ids)
                    result["inputs"][str(path.relative_to(agents_vault))] = hashlib.sha256(raw).hexdigest()
                continue
            record_type = meta.get("type", "")
            if record_type and record_type not in TASK_TYPES:
                # Monitor reports must not feed back into discovery or snapshots.
                if record_type != "itd-monitoring-report" and (conventional or meta.get("task_id")):
                    result["projections"].append({"path": path, "meta": meta})
                    result["inputs"][str(path.relative_to(agents_vault))] = hashlib.sha256(raw).hexdigest()
                continue
            if not conventional and record_type not in TASK_TYPES:
                continue
            result["inputs"][str(path.relative_to(agents_vault))] = hashlib.sha256(raw).hexdigest()
            task_id = meta.get("task_id", implied_id)
            if (not TASK_ID_RE.fullmatch(task_id) or (implied_id and implied_id != task_id)
                    or (path_name.startswith("TSK-") and not implied_id)):
                problem("task_identity_invalid", [path], "missing_invalid_or_conflicting_identity", task_id)
                invalid_claims.update((implied_id, task_id))
                continue
            recorded_status = meta.get("status", "")
            status = recorded_status if recorded_status in STATES else "state_unverified"
            if status == "waiting_human" and not live_human_decision(meta, now):
                status = "state_unverified"
            record = {"task_id": task_id, "path": path, "meta": meta, "text": text,
                      "recorded_status": recorded_status, "status": status,
                      "archived": "00_Archive" in path.relative_to(project_root).parts}
            grouped.setdefault(task_id, []).append(record)
    for task_id, records in sorted(grouped.items()):
        if len(records) != 1 or task_id in invalid_claims:
            reason = "invalid_conflicting_copy" if task_id in invalid_claims else "multiple_task_details"
            problem("task_identity_ambiguous", [r["path"] for r in records], reason, task_id)
        elif result["complete"]:
            result["tasks"][task_id] = records[0]
    return result
