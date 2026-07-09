#!/usr/bin/env python3
"""Host-owned frontdoor and P0 harness for deterministic workflow control."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_store
import run_lock
import run_lifecycle
import task_state_bridge
import work_order_builder
import workflow_selector

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_STATE_ROOT = Path.home() / ".codex" / "state" / "itb" / "frontdoor-orchestrator"
BRIDGE_PRINCIPAL_TYPE = "main_agent_bridge"
HTTP_CHANNEL_PRINCIPALS = {
    "bridge": (BRIDGE_PRINCIPAL_TYPE, "http-bridge", "local_http_channel"),
    "operator": ("manual_operator", "http-operator", "local_http_channel"),
    "human_ui": ("human_operator", "human-ui", "local_http_channel"),
    "harness": ("harness_runner", "local-harness", "local_http_channel"),
}
EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
}
WORKFLOW_DEFINITION_PRINCIPAL_TYPES = {"human_deploy_review"}
BRIDGE_REQUEST_KINDS = {"external_review_request", "orchestrator_status_request"}
BRIDGE_ALLOWED_ACTIONS = ["submit_request", "read_projection", "ack_output"]
BRIDGE_SUBMIT_ALLOWED_FIELDS = {
    "task_id",
    "request_id",
    "request_kind",
    "prompt",
    "refs",
    "allowed_paths",
    "expires_at",
    "frontdoor",
    "chat_session_id",
    "idempotency_key",
}
BRIDGE_FORBIDDEN_FIELDS = {
    "classification",
    "workflow_selection",
    "activation",
    "approved_activation",
    "human_action_id",
    "run_id",
    "workflow_id",
    "initial_step",
    "steps",
    "gates",
    "max_steps",
    "template",
    "work_order",
    "adapter_request",
    "report_path",
    "evidence_path",
    "transcript_path",
    "token",
    "api_key",
    "secret",
    "credential",
    "authorization",
    "principal_type",
    "principal_id",
    "authn_method",
}
MAX_APPROVAL_FAILURES = 3
MAX_CONTEXT_REF_COUNT = 50
MAX_ALLOWED_PATH_COUNT = 50
MAX_CONTEXT_REF_FILE_BYTES = 1_000_000
MAX_CONTEXT_REF_TOTAL_BYTES = 5_000_000
# canonical copy lives in run_store.py
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
REF_DENYLIST_NAMES = {
    ".git",
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
}
REF_DENYLIST_PATTERNS = (
    ".env*",
    "id_rsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*private_key*",
    "*private-key*",
    "*deploy_key*",
    "*deploy-key*",
    "*secret_key*",
    "*secret-key*",
    "*auth_key*",
    "*auth-key*",
    "*credential*",
    "*credentials*",
    "*secret*",
    "*token*",
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def load_json_path(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_json_arg(raw: str) -> Any:
    return workflow_selector.load_json_arg(raw)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    run_store.atomic_write_json(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = load_json_path(path)
    except OSError as exc:
        raise FrontdoorError(f"missing file: {path}") from exc
    if not isinstance(data, dict):
        raise FrontdoorError(f"expected object json: {path}")
    return data


class FrontdoorError(RuntimeError):
    pass


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def safe_id(value: str) -> str:
    allowed = [char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    compact = "".join(allowed).strip(".-")
    return compact[:96] or "anonymous"


def validate_artifact_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise FrontdoorError(
            f"{label} must match {SAFE_ID_RE.pattern} and cannot contain path separators"
        )
    if "/" in value or "\\" in value or value in {".", ".."} or ".." in value.split("."):
        raise FrontdoorError(f"{label} cannot contain path traversal segments")
    return value


def make_principal(
    principal_type: str,
    principal_id: str,
    *,
    authn_method: str = "local_cli",
) -> dict[str, str]:
    return {
        "principal_type": principal_type,
        "principal_id": principal_id,
        "authn_method": authn_method,
    }


def bridge_principal(frontdoor: str, chat_session_id: str = "") -> dict[str, str]:
    bridge_id = f"{frontdoor}:{chat_session_id or 'unknown'}"
    return make_principal(BRIDGE_PRINCIPAL_TYPE, bridge_id, authn_method="codex_app_bridge")


def default_manual_principal() -> dict[str, str]:
    return make_principal("manual_operator", "manual-cli", authn_method="local_cli")


def redacted_principal(principal: dict[str, Any]) -> dict[str, str]:
    return {
        "principal_type": str(principal.get("principal_type") or "unknown"),
        "principal_id": str(principal.get("principal_id") or "unknown"),
        "authn_method": str(principal.get("authn_method") or "unknown"),
    }


def signing_key_path(state_root: Path, principal: dict[str, Any]) -> Path:
    principal_id = str(principal.get("principal_id") or "anonymous")
    digest = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:24]
    return state_paths(state_root)["signing_keys"] / f"{safe_id(str(principal.get('principal_type') or 'principal'))}-{digest}.key"


def principal_key(state_root: Path, principal: dict[str, Any]) -> bytes:
    path = signing_key_path(state_root, principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(32) + "\n")
    return read_private_file_text(path, label="principal signing key").encode("utf-8")


def sign_transition(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> dict[str, str]:
    material = {
        "principal": redacted_principal(principal),
        "transition": transition,
        "subject": subject,
    }
    signature = hmac.new(principal_key(state_root, principal), canonical_json(material), hashlib.sha256).hexdigest()
    return {
        "algorithm": "sha256-local-principal-key",
        "signature": "sha256:" + signature,
        "signed_at": now_iso(),
    }


def append_audit_event(
    *,
    state_root: Path,
    event_type: str,
    principal: dict[str, Any],
    subject: dict[str, Any],
    outcome: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "audit_event_version": "1",
        "event_id": "evt-" + stable_digest(
            {
                "event_type": event_type,
                "principal": redacted_principal(principal),
                "subject": subject,
                "created_at": time.time_ns(),
            }
        )[:20],
        "created_at": now_iso(),
        "event_type": event_type,
        "principal": redacted_principal(principal),
        "subject": subject,
        "outcome": outcome,
        "details": details or {},
    }
    path = state_paths(state_root)["audit"] / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def assert_allowed_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    allowed_types: set[str],
    transition: str,
    subject: dict[str, Any],
    blocked_reason: str,
) -> dict[str, str]:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type not in allowed_types:
        append_audit_event(
            state_root=state_root,
            event_type=transition,
            principal=principal,
            subject=subject,
            outcome="blocked",
            details={"reason": blocked_reason, "principal_type": principal_type},
        )
        raise FrontdoorError(f"{blocked_reason}: {principal_type}")
    return sign_transition(
        state_root=state_root,
        principal=principal,
        transition=transition,
        subject=subject,
    )


def execution_principal_blocked_reason(principal: dict[str, Any]) -> str:
    return (
        "bridge principal cannot perform execution transition"
        if principal.get("principal_type") == BRIDGE_PRINCIPAL_TYPE
        else "unsupported execution principal"
    )


def assert_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> dict[str, str]:
    return assert_allowed_principal(
        state_root=state_root,
        principal=principal,
        allowed_types=EXECUTION_PRINCIPAL_TYPES,
        transition=transition,
        subject=subject,
        blocked_reason=execution_principal_blocked_reason(principal),
    )


def precheck_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    transition: str,
    subject: dict[str, Any],
) -> None:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type in EXECUTION_PRINCIPAL_TYPES:
        return
    blocked_reason = execution_principal_blocked_reason(principal)
    append_audit_event(
        state_root=state_root,
        event_type=transition,
        principal=principal,
        subject=subject,
        outcome="blocked",
        details={"reason": blocked_reason, "principal_type": principal_type},
    )
    raise FrontdoorError(f"{blocked_reason}: {principal_type}")


def assert_workflow_definition_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    subject: dict[str, Any],
) -> dict[str, str]:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type not in WORKFLOW_DEFINITION_PRINCIPAL_TYPES:
        append_audit_event(
            state_root=state_root,
            event_type="workflow_definition_change",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details={"reason": "workflow_definitions_are_human_deploy_only"},
        )
        raise FrontdoorError("workflow definition changes require human-owned deploy/review path")
    return sign_transition(
        state_root=state_root,
        principal=principal,
        transition="workflow_definition_change",
        subject=subject,
    )


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "requests": state_root / "requests",
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "adapter_requests": state_root / "adapter-requests",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
        "audit": state_root / "audit",
        "idempotency": state_root / "idempotency",
        "acks": state_root / "acks",
        "signing_keys": state_root / "principal-keys",
        "channel_tokens": state_root / "channel-tokens",
    }


def record_run_link_status(state_root: Path, run: dict[str, Any]) -> str:
    try:
        path = task_state_bridge.record_run_link(state_root, run)
    except Exception as exc:  # defensive isolation: view refresh must not fail transitions
        return f"error:{type(exc).__name__}:{exc}"
    if path is None:
        return "skipped:no_session"
    return f"linked:{path}"


def task_view(
    *,
    state_root: Path,
    task_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(task_id, "task_id")
    actor = principal or default_manual_principal()
    payload = task_state_bridge.task_view_payload(state_root, task_id)
    append_audit_event(
        state_root=state_root,
        event_type="task_view",
        principal=actor,
        subject={"task_id": task_id},
        outcome="ok",
        details={
            "run_count": len(payload.get("runs") or []),
            "queue_evidence_count": len(payload.get("queue_evidence") or []),
        },
    )
    return payload


def request_path(state_root: Path, request_id: str) -> Path:
    return state_paths(state_root)["requests"] / f"{validate_artifact_id(request_id, 'request_id')}.json"


def run_path(state_root: Path, run_id: str) -> Path:
    return state_paths(state_root)["runs"] / f"{validate_artifact_id(run_id, 'run_id')}.json"


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["work_orders"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}.json"
    )


def report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["reports"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-external-review-report.json"
    )


def provider_evidence_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-provider-evidence.json"
    )


def provider_transcript_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return (
        state_paths(state_root)["provider_evidence"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-claude-transcript.json"
    )


def adapter_request_path(state_root: Path, run_id: str, step_id: str, adapter_id: str) -> Path:
    return (
        state_paths(state_root)["adapter_requests"]
        / validate_artifact_id(run_id, "run_id")
        / f"{validate_artifact_id(step_id, 'step_id')}-{validate_artifact_id(adapter_id, 'adapter_id')}.json"
    )


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def channel_token_path(state_root: Path, channel: str) -> Path:
    if channel not in HTTP_CHANNEL_PRINCIPALS:
        raise FrontdoorError(f"unsupported channel: {channel}")
    return state_paths(state_root)["channel_tokens"] / f"{channel}.token"


def ensure_private_file(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise FrontdoorError(f"{label} must not be a symlink: {path}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        path.chmod(0o600)
        mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise FrontdoorError(f"{label} must have 0600 permissions: {path}")


def read_private_file_text(path: Path, *, label: str) -> str:
    ensure_private_file(path, label=label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FrontdoorError(f"{label} cannot be opened safely: {path}") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def channel_token(state_root: Path, channel: str) -> str:
    path = channel_token_path(state_root, channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")
    return read_private_file_text(path, label="channel token")


def principal_from_authenticated_channel(state_root: Path, channel: str, token: str) -> dict[str, str]:
    if channel not in HTTP_CHANNEL_PRINCIPALS:
        raise FrontdoorError(f"unsupported channel: {channel}")
    if not token:
        raise FrontdoorError("missing orchestrator channel token")
    expected = channel_token(state_root, channel)
    if not hmac.compare_digest(token, expected):
        raise FrontdoorError("invalid orchestrator channel token")
    principal_type, principal_id, authn_method = HTTP_CHANNEL_PRINCIPALS[channel]
    return make_principal(principal_type, principal_id, authn_method=authn_method)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _denylisted_ref_part(relative_path: Path) -> str | None:
    for part in relative_path.parts:
        lowered = part.lower()
        if lowered in REF_DENYLIST_NAMES:
            return part
        if any(fnmatch.fnmatch(lowered, pattern) for pattern in REF_DENYLIST_PATTERNS):
            return part
    return None


def _resolve_repo_path(raw: str, *, ref_root: Path, label: str) -> tuple[Path, Path]:
    if not isinstance(raw, str) or not raw.strip():
        raise FrontdoorError(f"{label} must be a non-empty string")
    if "\x00" in raw:
        raise FrontdoorError(f"{label} cannot contain NUL bytes")
    root = ref_root.expanduser().resolve()
    candidate = Path(raw).expanduser()
    path = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FrontdoorError(f"{label} does not exist: {raw}") from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise FrontdoorError(f"{label} outside approved ref root: {raw}") from exc
    denied_part = _denylisted_ref_part(relative)
    if denied_part:
        raise FrontdoorError(f"{label} denylisted path component: {denied_part}")
    return resolved, relative


def resolve_context_refs(refs: list[str], *, ref_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    if len(refs) > MAX_CONTEXT_REF_COUNT:
        raise FrontdoorError(f"too many context refs: {len(refs)} > {MAX_CONTEXT_REF_COUNT}")
    resolved_refs: list[dict[str, Any]] = []
    total_size = 0
    root = ref_root.expanduser().resolve()
    for raw in refs:
        resolved, relative = _resolve_repo_path(raw, ref_root=root, label="context ref")
        if not resolved.is_file():
            raise FrontdoorError(f"context ref must be a file: {raw}")
        size = resolved.stat().st_size
        if size > MAX_CONTEXT_REF_FILE_BYTES:
            raise FrontdoorError(
                f"context ref exceeds file size cap: {relative.as_posix()} > {MAX_CONTEXT_REF_FILE_BYTES}"
            )
        total_size += size
        if total_size > MAX_CONTEXT_REF_TOTAL_BYTES:
            raise FrontdoorError(f"context refs exceed total size cap: {total_size} > {MAX_CONTEXT_REF_TOTAL_BYTES}")
        resolved_refs.append(
            {
                "type": "repo_file",
                "original": raw,
                "path": relative.as_posix(),
                "size_bytes": size,
                "digest": file_sha256(resolved),
            }
        )
    return resolved_refs


def resolve_allowed_paths(paths: list[str], *, ref_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    if len(paths) > MAX_ALLOWED_PATH_COUNT:
        raise FrontdoorError(f"too many allowed paths: {len(paths)} > {MAX_ALLOWED_PATH_COUNT}")
    resolved_paths: list[dict[str, Any]] = []
    root = ref_root.expanduser().resolve()
    for raw in paths:
        resolved, relative = _resolve_repo_path(raw, ref_root=root, label="allowed path")
        resolved_paths.append(
            {
                "type": "repo_dir" if resolved.is_dir() else "repo_file",
                "original": raw,
                "path": relative.as_posix(),
            }
        )
    return resolved_paths


def resolved_ref_paths(resolved_refs: list[dict[str, Any]]) -> list[str]:
    return [str(item["path"]) for item in resolved_refs]


def bounded_context(
    refs: list[str],
    allowed_paths: list[str],
    *,
    require_refs: bool = False,
) -> dict[str, Any]:
    if require_refs and not refs:
        raise FrontdoorError("refs must be non-empty")
    resolved_refs = resolve_context_refs(refs) if refs else []
    resolved_allowed_paths = resolve_allowed_paths(allowed_paths) if allowed_paths else []
    return {
        "requested_context_refs": list(refs),
        "context_refs": resolved_ref_paths(resolved_refs),
        "resolved_context_refs": resolved_refs,
        "requested_allowed_paths": list(allowed_paths),
        "allowed_paths": resolved_ref_paths(resolved_allowed_paths),
        "resolved_allowed_paths": resolved_allowed_paths,
    }


def approval_ref_summaries(resolved_refs: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in resolved_refs:
        if not isinstance(item, dict):
            continue
        summary = {
            "type": str(item.get("type") or "repo_file"),
            "path": str(item.get("path") or ""),
        }
        if "size_bytes" in item:
            summary["size_bytes"] = item["size_bytes"]
        if "digest" in item:
            summary["digest"] = item["digest"]
        summaries.append(summary)
    return summaries


def approval_allowed_path_summaries(resolved_paths: list[Any]) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for item in resolved_paths:
        if not isinstance(item, dict):
            continue
        summaries.append(
            {
                "type": str(item.get("type") or "repo_path"),
                "path": str(item.get("path") or ""),
            }
        )
    return summaries


def ref_integrity_view(refs: list[Any]) -> list[dict[str, Any]]:
    view: list[dict[str, Any]] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        view.append(
            {
                "path": str(item.get("path") or ""),
                "size_bytes": item.get("size_bytes"),
                "digest": str(item.get("digest") or ""),
            }
        )
    return view


def verified_context_refs_for_work_order(request_record: dict[str, Any]) -> list[dict[str, Any]]:
    approved_refs = request_record.get("resolved_context_refs")
    requested_refs = list(request_record.get("requested_context_refs") or request_record.get("context_refs") or [])
    current_refs = resolve_context_refs(requested_refs) if requested_refs else []
    if isinstance(approved_refs, list) and approved_refs:
        if ref_integrity_view(current_refs) != ref_integrity_view(approved_refs):
            raise FrontdoorError("context refs changed after approval")
    return current_refs


def stable_run_id(request_id: str, workflow_id: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{workflow_id}".encode("utf-8")).hexdigest()[:12]
    return f"run-{digest}"


def policy_digest(envelope: dict[str, Any]) -> str:
    policy_material = {
        "policy": envelope.get("policy"),
        "activation_scope": envelope.get("activation_scope"),
        "context_scope": envelope.get("context_scope"),
        "workflow_selection": envelope.get("workflow_selection"),
    }
    encoded = json.dumps(policy_material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sanitize_activation_for_run(envelope: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "activation_version",
        "activation_source",
        "activation_status",
        "approved_by",
        "approved_at",
        "workflow_selection",
        "classification_provenance",
        "context_scope",
        "activation_scope",
        "next_action",
    }
    return {key: envelope[key] for key in allowed if key in envelope}


def load_registry() -> dict[str, Any]:
    return workflow_selector.load_registry()


def load_template(workflow_id: str) -> dict[str, Any]:
    registry = load_registry()
    for entry in registry.get("templates", []):
        if entry.get("workflow_id") != workflow_id:
            continue
        path = REPO_ROOT / entry["path"]
        return read_json(path)
    raise FrontdoorError(f"active workflow template not found: {workflow_id}")


def proposed_request(
    *,
    state_root: Path,
    task_id: str,
    request_id: str,
    user_prompt: str,
    refs: list[str],
    classification: dict[str, Any] | None,
    allowed_paths: list[str],
    expires_at: str,
    frontdoor: str,
    chat_session_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    validate_artifact_id(task_id, "task_id")
    actor = principal or default_manual_principal()
    bounded = bounded_context(refs, allowed_paths)
    path = request_path(state_root, request_id)
    existing_record = read_json(path) if path.exists() else None
    if existing_record is not None:
        if classification is None:
            if existing_record.get("status") == "waiting_human" and isinstance(existing_record.get("proposal"), dict):
                append_audit_event(
                    state_root=state_root,
                    event_type="request_waiting_human",
                    principal=actor,
                    subject={"request_id": request_id, "task_id": task_id},
                    outcome="replayed",
                    details={"reason": "request_id_already_waiting_human"},
                )
                return existing_record["proposal"]
            append_audit_event(
                state_root=state_root,
                event_type="request_waiting_human",
                principal=actor,
                subject={"request_id": request_id, "task_id": task_id},
                outcome="blocked",
                details={"reason": "request_id_conflict"},
            )
            raise FrontdoorError("request_id conflict for propose")
        if existing_record.get("status") != "waiting_human" or existing_record.get("approved_activation"):
            append_audit_event(
                state_root=state_root,
                event_type="request_proposed",
                principal=actor,
                subject={"request_id": request_id, "task_id": task_id},
                outcome="blocked",
                details={
                    "reason": "request_id_conflict",
                    "existing_status": existing_record.get("status"),
                },
            )
            raise FrontdoorError("request_id conflict for propose")
        immutable_mismatches = []
        expected = {
            "task_id": task_id,
            "user_prompt": user_prompt,
            "context_refs": bounded["context_refs"],
            "allowed_paths": bounded["allowed_paths"],
            "expires_at": expires_at,
        }
        for key, value in expected.items():
            if existing_record.get(key) != value:
                immutable_mismatches.append(key)
        if immutable_mismatches:
            append_audit_event(
                state_root=state_root,
                event_type="request_proposed",
                principal=actor,
                subject={"request_id": request_id, "task_id": task_id},
                outcome="blocked",
                details={
                    "reason": "request_id_conflict",
                    "immutable_mismatches": immutable_mismatches,
                },
            )
            raise FrontdoorError("request_id conflict for propose")
    if classification is None:
        payload = {
            "schema_version": 1,
            "decision": "waiting_human",
            "request_status": "waiting_human",
            "reason": "typed_classification_required",
            "task_id": task_id,
            "request_id": request_id,
            "next_action": "ask_human",
        }
        record = {
            "request_version": "1",
            "task_id": task_id,
            "request_id": request_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "user_prompt": user_prompt,
            **bounded,
            "expires_at": expires_at,
            "classification": None,
            "requester": requester(frontdoor, chat_session_id),
            "status": "waiting_human",
            "proposal": payload,
        }
        write_json(path, record)
        append_audit_event(
            state_root=state_root,
            event_type="request_waiting_human",
            principal=actor,
            subject={"request_id": request_id, "task_id": task_id},
            outcome="ok",
            details={"reason": "typed_classification_required"},
        )
        return payload

    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source="frontdoor_prompt",
        task_id=task_id,
        request_id=request_id,
        refs=list(bounded["context_refs"]),
        allowed_paths=list(bounded["allowed_paths"]),
        expires_at=expires_at,
    )
    if existing_record is None:
        record = {
            "request_version": "1",
            "task_id": task_id,
            "request_id": request_id,
            "created_at": now_iso(),
            "user_prompt": user_prompt,
            **bounded,
            "expires_at": expires_at,
            "requester": requester(frontdoor, chat_session_id),
        }
    else:
        record = dict(existing_record)
    record.update(
        {
            "updated_at": now_iso(),
            "classification": classification,
            "status": envelope["activation_status"],
            "proposal": envelope,
        }
    )
    attach_approval_summary(record)
    write_json(path, record)
    append_audit_event(
        state_root=state_root,
        event_type="request_proposed",
        principal=actor,
        subject={"request_id": request_id, "task_id": task_id},
        outcome="ok" if envelope["activation_status"] != "blocked" else "blocked",
        details={
            "request_status": envelope["activation_status"],
            "workflow_selection": envelope.get("workflow_selection"),
        },
    )
    return {
        "schema_version": 1,
        "decision": "blocked" if envelope["activation_status"] == "blocked" else "ok",
        "request_status": envelope["activation_status"],
        "request_path": str(path),
        "activation": envelope,
        "approval": record.get("approval"),
    }


def requester(frontdoor: str, chat_session_id: str = "") -> dict[str, str]:
    payload = {"frontdoor": frontdoor}
    if chat_session_id:
        payload["chat_session_id"] = chat_session_id
    return payload


def approval_action_id(record: dict[str, Any]) -> str:
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    material = {
        "task_id": record.get("task_id"),
        "request_id": record.get("request_id"),
        "workflow_selection": proposal.get("workflow_selection"),
        "classification_provenance": proposal.get("classification_provenance"),
        "context_refs": record.get("context_refs") or [],
        "resolved_context_refs": record.get("resolved_context_refs") or [],
        "allowed_paths": record.get("allowed_paths") or [],
        "expires_at": record.get("expires_at"),
    }
    return "approve-" + stable_digest(material)[:20]


def approval_summary(record: dict[str, Any]) -> dict[str, Any]:
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    workflow_selection = proposal.get("workflow_selection") if isinstance(proposal.get("workflow_selection"), dict) else {}
    activation_scope = proposal.get("activation_scope") if isinstance(proposal.get("activation_scope"), dict) else {}
    allowed_ops = activation_scope.get("allowed_ops") if isinstance(activation_scope.get("allowed_ops"), dict) else {}
    denied_ops = sorted(op for op, allowed in allowed_ops.items() if allowed is False)
    return {
        "approval_view_version": "1",
        "source": "orchestrator_structured_state",
        "main_agent_prose_used": False,
        "human_action_id": approval_action_id(record),
        "rate_limit": {
            "max_failed_attempts": MAX_APPROVAL_FAILURES,
            "failed_attempts": int((record.get("approval_rate_limit") or {}).get("failed_attempts") or 0),
        },
        "what_will_execute": {
            "workflow_id": workflow_selection.get("workflow_id"),
            "initial_step": workflow_selection.get("initial_step"),
            "permission_mode": "readonly",
            "step_budget": activation_scope.get("step_budget"),
            "context_refs": list(record.get("context_refs") or []),
            "resolved_context_refs": approval_ref_summaries(list(record.get("resolved_context_refs") or [])),
            "allowed_paths": list(record.get("allowed_paths") or []),
            "resolved_allowed_paths": approval_allowed_path_summaries(
                list(record.get("resolved_allowed_paths") or [])
            ),
            "denied_ops": denied_ops,
            "provider_adapter": "claude_headless_p0",
            "ref_boundary": {
                "workspace_root": str(REPO_ROOT),
                "max_ref_count": MAX_CONTEXT_REF_COUNT,
                "max_ref_file_bytes": MAX_CONTEXT_REF_FILE_BYTES,
                "max_ref_total_bytes": MAX_CONTEXT_REF_TOTAL_BYTES,
            },
        },
        "classification_provenance": proposal.get("classification_provenance"),
        "next_action": proposal.get("next_action"),
    }


def attach_approval_summary(record: dict[str, Any]) -> None:
    proposal = record.get("proposal")
    if isinstance(proposal, dict) and proposal.get("activation_status") == "proposed":
        record.setdefault("approval_rate_limit", {"failed_attempts": 0})
        record["approval"] = approval_summary(record)


def idempotency_path(state_root: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return state_paths(state_root)["idempotency"] / f"key-{digest}.json"


def request_digest(payload: dict[str, Any]) -> str:
    material = {
        "task_id": payload.get("task_id"),
        "request_id": payload.get("request_id"),
        "request_kind": payload.get("request_kind"),
        "prompt": payload.get("prompt") or "",
        "refs": list(payload.get("refs") or []),
        "allowed_paths": list(payload.get("allowed_paths") or []),
        "expires_at": payload.get("expires_at") or "run_terminal",
        "frontdoor": payload.get("frontdoor") or "codex",
        "chat_session_id": payload.get("chat_session_id") or "",
    }
    return "sha256:" + stable_digest(material)


def bridge_audit_details(
    *,
    frontdoor: str,
    chat_session_id: str = "",
    peer: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"requester": requester(frontdoor, chat_session_id)}
    if peer:
        details["peer"] = {str(key): str(value) for key, value in peer.items()}
    if extra:
        details.update(extra)
    return details


def validate_bridge_submit_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    extra = sorted(set(payload) - BRIDGE_SUBMIT_ALLOWED_FIELDS)
    if extra:
        errors.append("unexpected_fields:" + ",".join(extra))
    forbidden = sorted(set(payload) & BRIDGE_FORBIDDEN_FIELDS)
    if forbidden:
        errors.append("forbidden_fields:" + ",".join(forbidden))
    for field in ("task_id", "request_id", "request_kind", "prompt", "idempotency_key"):
        if not isinstance(payload.get(field), str) or not str(payload.get(field)).strip():
            errors.append(f"{field} must be non-empty string")
    for field in ("task_id", "request_id"):
        value = payload.get(field)
        if isinstance(value, str):
            try:
                validate_artifact_id(value, field)
            except FrontdoorError as exc:
                errors.append(str(exc))
    if payload.get("request_kind") not in BRIDGE_REQUEST_KINDS:
        errors.append(f"request_kind unsupported:{payload.get('request_kind')!r}")
    if "refs" not in payload:
        errors.append("refs is required")
    for field in ("refs", "allowed_paths"):
        value = payload.get(field) or []
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            errors.append(f"{field} must be a list of non-empty strings")
        if field == "refs" and isinstance(value, list) and not value:
            errors.append("refs must be non-empty")
    for key, value in payload.items():
        if any(secret_word in key.lower() for secret_word in ("token", "secret", "api_key", "authorization")):
            errors.append(f"forbidden_secret_field:{key}")
        if isinstance(value, str) and any(marker in value.lower() for marker in ("authorization:", "bearer ", "api_key=", "private key")):
            errors.append(f"forbidden_secret_material:{key}")
    return errors


def bridge_submit_request(
    *,
    state_root: Path,
    payload: dict[str, Any],
    principal: dict[str, Any] | None = None,
    peer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors = validate_bridge_submit_payload(payload)
    frontdoor_name = str(payload.get("frontdoor") or "codex")
    chat_session_id = str(payload.get("chat_session_id") or "")
    principal = principal or bridge_principal(frontdoor_name, chat_session_id)
    subject = {
        "request_id": str(payload.get("request_id") or ""),
        "task_id": str(payload.get("task_id") or ""),
    }
    if errors:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_submit_request",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"errors": errors},
            ),
        )
        raise FrontdoorError("invalid bridge submit_request: " + "; ".join(errors))

    digest = request_digest(payload)
    idempotency_key = str(payload["idempotency_key"])
    idempotency_file = idempotency_path(state_root, idempotency_key)
    if idempotency_file.exists():
        existing = read_json(idempotency_file)
        if existing.get("request_digest") != digest:
            append_audit_event(
                state_root=state_root,
                event_type="bridge_submit_request",
                principal=principal,
                subject=subject,
                outcome="blocked",
                details=bridge_audit_details(
                    frontdoor=frontdoor_name,
                    chat_session_id=chat_session_id,
                    peer=peer,
                    extra={"reason": "idempotency_conflict"},
                ),
            )
            raise FrontdoorError("idempotency conflict for bridge submit_request")
        projection = bridge_read_projection(
            state_root=state_root,
            request_id=str(existing["request_id"]),
            frontdoor=frontdoor_name,
            chat_session_id=chat_session_id,
            principal=principal,
            peer=peer,
        )
        projection["replayed"] = True
        append_audit_event(
            state_root=state_root,
            event_type="bridge_submit_request",
            principal=principal,
            subject=subject,
            outcome="replayed",
            details=bridge_audit_details(
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"idempotency_key": idempotency_key},
            ),
        )
        return projection

    path = request_path(state_root, str(payload["request_id"]))
    if path.exists():
        existing = read_json(path)
        if existing.get("request_digest") != digest:
            append_audit_event(
                state_root=state_root,
                event_type="bridge_submit_request",
                principal=principal,
                subject=subject,
                outcome="blocked",
                details=bridge_audit_details(
                    frontdoor=frontdoor_name,
                    chat_session_id=chat_session_id,
                    peer=peer,
                    extra={"reason": "request_id_conflict"},
                ),
            )
            raise FrontdoorError("request_id conflict for bridge submit_request")
        return bridge_read_projection(
            state_root=state_root,
            request_id=str(payload["request_id"]),
            frontdoor=frontdoor_name,
            chat_session_id=chat_session_id,
            principal=principal,
            peer=peer,
        )

    try:
        bounded = bounded_context(
            list(payload.get("refs") or []),
            list(payload.get("allowed_paths") or []),
            require_refs=True,
        )
    except FrontdoorError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_submit_request",
            principal=principal,
            subject=subject,
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor_name,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={"reason": str(exc)},
            ),
        )
        raise

    now = now_iso()
    record = {
        "request_version": "1",
        "task_id": str(payload["task_id"]),
        "request_id": str(payload["request_id"]),
        "request_kind": str(payload["request_kind"]),
        "created_at": now,
        "updated_at": now,
        "user_prompt": str(payload.get("prompt") or ""),
        "request_digest": digest,
        **bounded,
        "expires_at": str(payload.get("expires_at") or "run_terminal"),
        "classification": None,
        "requester": requester(frontdoor_name, chat_session_id),
        "principal": redacted_principal(principal),
        "status": "waiting_human",
        "proposal": {
            "schema_version": 1,
            "decision": "waiting_human",
            "request_status": "waiting_human",
            "reason": "typed_classification_required_from_non_bridge_principal",
            "task_id": str(payload["task_id"]),
            "request_id": str(payload["request_id"]),
            "next_action": "ask_human",
        },
        "bridge_contract": {
            "allowed_actions": BRIDGE_ALLOWED_ACTIONS,
            "forbidden_actions": [
                "classify",
                "approve",
                "create_run",
                "drain",
                "prepare_provider",
                "validate_report",
                "workflow_definition_change",
            ],
        },
    }
    write_json(path, record)
    write_json(
        idempotency_file,
        {
            "idempotency_version": "1",
            "idempotency_key": idempotency_key,
            "request_id": str(payload["request_id"]),
            "request_digest": digest,
            "created_at": now,
        },
    )
    append_audit_event(
        state_root=state_root,
        event_type="bridge_submit_request",
        principal=principal,
        subject=subject,
        outcome="ok",
        details=bridge_audit_details(
            frontdoor=frontdoor_name,
            chat_session_id=chat_session_id,
            peer=peer,
            extra={"request_digest": digest},
        ),
    )
    return bridge_read_projection(
        state_root=state_root,
        request_id=str(payload["request_id"]),
        frontdoor=frontdoor_name,
        chat_session_id=chat_session_id,
        principal=principal,
        peer=peer,
    )


def redacted_ref_labels(refs: list[Any]) -> list[str]:
    labels: list[str] = []
    for ref in refs:
        value = str(ref)
        if value.startswith("/") or ":" in Path(value).anchor:
            labels.append("redacted:absolute-path")
        else:
            labels.append(value)
    return labels


def redacted_approval_summary(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    what = summary.get("what_will_execute") if isinstance(summary.get("what_will_execute"), dict) else {}
    return {
        "approval_view_version": summary.get("approval_view_version"),
        "source": summary.get("source"),
        "main_agent_prose_used": False,
        "human_action_id": summary.get("human_action_id"),
        "rate_limit": summary.get("rate_limit"),
        "what_will_execute": {
            "workflow_id": what.get("workflow_id"),
            "initial_step": what.get("initial_step"),
            "permission_mode": what.get("permission_mode"),
            "step_budget": what.get("step_budget"),
            "context_ref_count": len(what.get("context_refs") or []),
            "context_refs_digest": "sha256:" + stable_digest(what.get("context_refs") or []),
            "allowed_paths_digest": "sha256:" + stable_digest(what.get("allowed_paths") or []),
            "denied_ops": list(what.get("denied_ops") or []),
            "provider_adapter": what.get("provider_adapter"),
        },
        "classification_provenance": summary.get("classification_provenance"),
        "next_action": summary.get("next_action"),
    }


def build_bridge_projection(
    *,
    state_root: Path,
    request_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = read_json(request_path(state_root, request_id))
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    return {
        "schema_version": 1,
        "decision": "ok",
        "projection_version": "1",
        "safe_for_principal": redacted_principal(principal),
        "request_id": record.get("request_id"),
        "task_id": record.get("task_id"),
        "request_kind": record.get("request_kind"),
        "request_status": record.get("status"),
        "orchestrator_decision": proposal.get("decision"),
        "reason": proposal.get("reason") or proposal.get("approval_required_reason"),
        "next_action": proposal.get("next_action"),
        "next_allowed_bridge_actions": BRIDGE_ALLOWED_ACTIONS,
        "context": {
            "ref_count": len(record.get("context_refs") or []),
            "ref_labels": redacted_ref_labels(list(record.get("context_refs") or [])),
            "refs_digest": "sha256:" + stable_digest(record.get("context_refs") or []),
        },
        "approval": redacted_approval_summary(record.get("approval")),
        "redacted_fields": [
            "user_prompt",
            "request_path",
            "run_path",
            "work_order_path",
            "adapter_request_path",
            "report_path",
            "evidence_path",
            "transcript_path",
            "provider_session_id",
            "principal_keys",
        ],
        "transition_effect": "none",
    }, record


def bridge_projection_digest(projection: dict[str, Any]) -> str:
    material = {key: value for key, value in projection.items() if key != "projection_digest"}
    return "sha256:" + stable_digest(material)


def bridge_read_projection(
    *,
    state_root: Path,
    request_id: str,
    frontdoor: str,
    chat_session_id: str,
    principal: dict[str, Any] | None = None,
    peer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    principal = principal or bridge_principal(frontdoor, chat_session_id)
    projection, record = build_bridge_projection(
        state_root=state_root,
        request_id=request_id,
        principal=principal,
    )
    digest = bridge_projection_digest(projection)
    append_audit_event(
        state_root=state_root,
        event_type="bridge_read_projection",
        principal=principal,
        subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
        outcome="ok",
        details=bridge_audit_details(
            frontdoor=frontdoor,
            chat_session_id=chat_session_id,
            peer=peer,
            extra={"projection_digest": digest},
        ),
    )
    projection["projection_digest"] = digest
    return projection


def bridge_ack_output(
    *,
    state_root: Path,
    request_id: str,
    projection_digest: str,
    frontdoor: str,
    chat_session_id: str,
    principal: dict[str, Any] | None = None,
    peer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    principal = principal or bridge_principal(frontdoor, chat_session_id)
    projection, before = build_bridge_projection(
        state_root=state_root,
        request_id=request_id,
        principal=principal,
    )
    expected_projection_digest = bridge_projection_digest(projection)
    ack_verified = hmac.compare_digest(projection_digest, expected_projection_digest)
    if not ack_verified:
        append_audit_event(
            state_root=state_root,
            event_type="bridge_ack_output",
            principal=principal,
            subject={"request_id": request_id, "task_id": str(before.get("task_id") or "")},
            outcome="blocked",
            details=bridge_audit_details(
                frontdoor=frontdoor,
                chat_session_id=chat_session_id,
                peer=peer,
                extra={
                    "transition_effect": "none",
                    "ack_verified": False,
                    "supplied_projection_digest": projection_digest,
                    "expected_projection_digest": expected_projection_digest,
                },
            ),
        )
        raise FrontdoorError("projection digest mismatch for bridge ack_output")
    ack = {
        "ack_version": "1",
        "request_id": request_id,
        "projection_digest": projection_digest,
        "expected_projection_digest": expected_projection_digest,
        "ack_verified": True,
        "principal": redacted_principal(principal),
        "acked_at": now_iso(),
        "transition_effect": "none",
    }
    ack_path = state_paths(state_root)["acks"] / f"{safe_id(request_id)}-{stable_digest(ack)[:12]}.json"
    write_json(ack_path, ack)
    after = read_json(request_path(state_root, request_id))
    append_audit_event(
        state_root=state_root,
        event_type="bridge_ack_output",
        principal=principal,
        subject={"request_id": request_id, "task_id": str(before.get("task_id") or "")},
        outcome="ok",
        details=bridge_audit_details(
            frontdoor=frontdoor,
            chat_session_id=chat_session_id,
            peer=peer,
            extra={
                "transition_effect": "none",
                "ack_verified": True,
                "projection_digest": projection_digest,
                "expected_projection_digest": expected_projection_digest,
                "request_digest_before": "sha256:" + stable_digest(before),
                "request_digest_after": "sha256:" + stable_digest(after),
            },
        ),
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "ack_path": str(ack_path),
        "ack_verified": True,
        "expected_projection_digest": expected_projection_digest,
        "transition_effect": "none",
        "request_status": after.get("status"),
    }


def principal_from_cli(principal_type: str, principal_id: str, authn_method: str) -> dict[str, str]:
    return make_principal(
        principal_type or "manual_operator",
        principal_id or "manual-cli",
        authn_method=authn_method or "local_cli",
    )


def approve_request(
    *,
    state_root: Path,
    request_id: str,
    human_action_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    actor = principal or make_principal("human_operator", "human-ui", authn_method="local_ui")
    path = request_path(state_root, request_id)
    record = read_json(path)
    signature = assert_allowed_principal(
        state_root=state_root,
        principal=actor,
        allowed_types={"human_operator", "manual_operator", "orchestrator_start"},
        transition="approve_request",
        subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
        blocked_reason="unsupported approval principal",
    )
    classification = record.get("classification")
    if not isinstance(classification, dict):
        raise FrontdoorError("typed classification is required before approval")
    record.setdefault("requested_context_refs", list(record.get("context_refs") or []))
    record.setdefault("requested_allowed_paths", list(record.get("allowed_paths") or []))
    requested_refs = list(record.get("requested_context_refs") or record.get("context_refs") or [])
    requested_allowed_paths = list(record.get("requested_allowed_paths") or record.get("allowed_paths") or [])
    refreshed = bounded_context(
        requested_refs,
        requested_allowed_paths,
    )
    for key in (
        "requested_context_refs",
        "requested_allowed_paths",
        "context_refs",
        "resolved_context_refs",
        "allowed_paths",
        "resolved_allowed_paths",
    ):
        record[key] = refreshed[key]
    attach_approval_summary(record)
    expected_action_id = approval_action_id(record)
    if human_action_id != expected_action_id:
        rate = record.setdefault("approval_rate_limit", {"failed_attempts": 0})
        rate["failed_attempts"] = int(rate.get("failed_attempts") or 0) + 1
        record["updated_at"] = now_iso()
        record["approval"] = approval_summary(record)
        write_json(path, record)
        append_audit_event(
            state_root=state_root,
            event_type="approve_request",
            principal=actor,
            subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
            outcome="blocked",
            details={"reason": "approval_challenge_mismatch", "failed_attempts": rate["failed_attempts"]},
        )
        raise FrontdoorError("approval challenge mismatch")
    failed_attempts = int((record.get("approval_rate_limit") or {}).get("failed_attempts") or 0)
    if failed_attempts >= MAX_APPROVAL_FAILURES:
        append_audit_event(
            state_root=state_root,
            event_type="approve_request",
            principal=actor,
            subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
            outcome="blocked",
            details={"reason": "approval_rate_limited", "failed_attempts": failed_attempts},
        )
        raise FrontdoorError("approval challenge rate limit exceeded")
    envelope = workflow_selector.activation_envelope(
        classification,
        activation_source="human_ui",
        task_id=record["task_id"],
        request_id=record["request_id"],
        refs=list(record.get("context_refs") or []),
        allowed_paths=list(record.get("allowed_paths") or []),
        expires_at=str(record.get("expires_at") or "run_terminal"),
    )
    record["updated_at"] = now_iso()
    record["status"] = envelope["activation_status"]
    record["human_action_id"] = human_action_id
    record["approved_activation"] = envelope
    record["approval_record"] = {
        "approval_record_version": "1",
        "human_action_id": human_action_id,
        "approved_at": now_iso(),
        "approved_by_principal": redacted_principal(actor),
        "proposal_digest": "sha256:" + stable_digest(record.get("proposal") or {}),
        "request_digest": record.get("request_digest") or "sha256:" + stable_digest(
            {
                "task_id": record.get("task_id"),
                "request_id": record.get("request_id"),
                "user_prompt": record.get("user_prompt"),
                "context_refs": record.get("context_refs") or [],
                "resolved_context_refs": record.get("resolved_context_refs") or [],
                "allowed_paths": record.get("allowed_paths") or [],
            }
        ),
        "refs_digest": "sha256:" + stable_digest(record.get("resolved_context_refs") or record.get("context_refs") or []),
        "display_digest": "sha256:" + stable_digest(record.get("approval") or {}),
        "signature": signature,
    }
    write_json(path, record)
    append_audit_event(
        state_root=state_root,
        event_type="approve_request",
        principal=actor,
        subject={"request_id": request_id, "task_id": str(record.get("task_id") or "")},
        outcome="ok" if envelope["activation_status"] == "approved" else "blocked",
        details={"request_status": envelope["activation_status"]},
    )
    return {
        "schema_version": 1,
        "decision": "ok" if envelope["activation_status"] == "approved" else "blocked",
        "request_status": envelope["activation_status"],
        "request_path": str(path),
        "activation": envelope,
        "approval_record": record["approval_record"],
    }


def create_run(
    *,
    state_root: Path,
    request_id: str,
    run_id: str,
    resume_policy: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(request_id, "request_id")
    if run_id:
        validate_artifact_id(run_id, "run_id")
    if resume_policy not in {"manual", "daemon_future"}:
        raise FrontdoorError("resume_policy unsupported")
    actor = principal or default_manual_principal()

    record = read_json(request_path(state_root, request_id))
    envelope = record.get("approved_activation")
    if not isinstance(envelope, dict) or envelope.get("activation_status") != "approved":
        raise FrontdoorError("approved activation envelope required")
    selection = envelope.get("workflow_selection") or {}
    workflow_id = selection.get("workflow_id")
    initial_step = selection.get("initial_step")
    if not workflow_id or not initial_step:
        raise FrontdoorError("approved activation must contain selected workflow and initial step")

    template = load_template(str(workflow_id))
    effective_run_id = run_id or stable_run_id(request_id, str(workflow_id))
    signature = assert_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="create_run",
        subject={"request_id": request_id, "run_id": effective_run_id},
    )
    subject = {"request_id": request_id, "run_id": effective_run_id}
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="create_run",
            run_id=effective_run_id,
            principal=actor,
        ):
            record = read_json(request_path(state_root, request_id))
            bound_run_id = str(record.get("run_id") or "")
            if bound_run_id and bound_run_id != effective_run_id:
                raise FrontdoorError("request_id is already bound to a different run_id")
            path = run_store.run_path(state_root, effective_run_id)
            if path.exists():
                existing = run_store.load_run(state_root, effective_run_id)
                if existing.get("request_id") != request_id:
                    raise FrontdoorError("run_id conflict for different request")
                if not bound_run_id:
                    record["run_id"] = effective_run_id
                    record["updated_at"] = now_iso()
                    write_json(request_path(state_root, request_id), record)
                link_status = record_run_link_status(state_root, existing)
                append_audit_event(
                    state_root=state_root,
                    event_type="create_run",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={"created": False, "run_link": link_status},
                )
                return {
                    "schema_version": 1,
                    "decision": "ok",
                    "created": False,
                    "run_path": str(path),
                    "workflow_run": existing,
                }

            run = {
                "run_version": "1",
                "run_id": effective_run_id,
                "task_id": record["task_id"],
                "request_id": request_id,
                "workflow_id": workflow_id,
                "goal_state": "approved",
                "run_state": "created",
                "current_step": initial_step,
                "iteration": 1,
                "max_steps": int(template.get("max_steps") or 1),
                "step_history": [],
                "activation": sanitize_activation_for_run(envelope),
                "terminal": {"status": None, "reason": None},
                "requester": record.get("requester") or requester("manual"),
                "scheduling": {
                    "scheduler_mode": "invocation-drain",
                    "concurrency_group": "global",
                    "state_persistence": "durable_state",
                    "lock_policy": "global_advisory_lock",
                    "concurrency": 1,
                    "resume_policy": resume_policy,
                },
                "context_sharing": {
                    "shared_run_state": "typed_durable_state",
                    "step_local_snapshot": "immutable_step_attempt_snapshot",
                    "provider_transcript": "confined_evidence_path_only",
                },
                "transitions": [],
                "transition_provenance": [
                    {
                        "transition": "create_run",
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                ],
            }
            path = run_store.store_run(state_root, run)
            record["run_id"] = effective_run_id
            record["updated_at"] = now_iso()
            write_json(request_path(state_root, request_id), record)
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="create_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise

    link_status = record_run_link_status(state_root, run)
    append_audit_event(
        state_root=state_root,
        event_type="create_run",
        principal=actor,
        subject=subject,
        outcome="ok",
        details={"created": True, "run_link": link_status},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "created": True,
        "run_path": str(path),
        "workflow_run": run,
    }


def drain_run(
    *,
    state_root: Path,
    run_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    path = run_store.run_path(state_root, run_id)
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="drain_run",
        subject=subject,
    )
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="drain_run",
            run_id=run_id,
            principal=actor,
        ):
            run = run_store.load_run(state_root, run_id)
            subject = {"run_id": run_id, "request_id": str(run.get("request_id") or "")}
            signature = assert_execution_principal(
                state_root=state_root,
                principal=actor,
                transition="drain_run",
                subject=subject,
            )
            if run.get("run_state") not in {"created", "step_queued"}:
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="drain_run",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={
                        "reason": "run_state_not_queueable",
                        "run_state": run.get("run_state"),
                        "run_link": link_status,
                    },
                )
                return {
                    "schema_version": 1,
                    "decision": "ok",
                    "drained": False,
                    "reason": "run_state_not_queueable",
                    "workflow_run": run,
                }

            run_lock.assert_p0_concurrency(state_root, target_run_id=run_id)
            workflow_id = str(run.get("workflow_id") or "")
            current_step_id = str(run.get("current_step") or "")
            errors: list[str] = []
            try:
                template = load_template(workflow_id)
            except FrontdoorError:
                template = {}
                errors.append(f"template_not_active:{workflow_id}")
            step = (
                next((item for item in template.get("steps", []) if item.get("id") == current_step_id), None)
                if not errors
                else None
            )
            if not errors and not isinstance(step, dict):
                errors.append(f"step_not_in_template:{current_step_id}")

            order_step_id = str((step or {}).get("id") or current_step_id)
            order_path = work_order_path(state_root, run_id, order_step_id)
            snapshot_path: Path | None = None
            work_order: dict[str, Any] = {}
            drained = False
            if not errors:
                order_exists = order_path.exists()
                if order_exists:
                    work_order = read_json(order_path)
                else:
                    request_record = read_json(request_path(state_root, str(run["request_id"])))
                    try:
                        work_order = build_work_order(
                            state_root=state_root,
                            run=run,
                            request_record=request_record,
                            template=template,
                            step=step,
                            issuer_principal=actor,
                        )
                    except FrontdoorError as exc:
                        errors.append(str(exc))
                if not errors:
                    errors.extend(
                        work_order_builder.validate_work_order(
                            work_order,
                            template=template,
                            step=step,
                            state_root=state_root,
                        )
                    )
                if not errors and not order_exists:
                    write_json(order_path, work_order)
                    drained = True
                if not errors:
                    try:
                        snapshot_path = work_order_builder.freeze_step_snapshot(
                            state_root,
                            work_order,
                            iteration=int(run.get("iteration") or 1),
                        )
                    except work_order_builder.WorkOrderError as exc:
                        errors.append(str(exc))

            if errors:
                transition = run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="waiting_human",
                    reason_class="work_order_invalid",
                    transition="drain_run",
                    principal=actor,
                    artifact_refs=[str(order_path)] if order_path.exists() else [],
                    run=run,
                )
                path = run_store.run_path(state_root, run_id)
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="drain_run",
                    principal=actor,
                    subject=subject,
                    outcome="blocked",
                    details={"reason": "work_order_invalid", "errors": errors, "run_link": link_status},
                )
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "work_order_invalid",
                    "errors": errors,
                    "transition": transition,
                    "run_path": str(path),
                    "workflow_run": run,
                }

            if run["run_state"] == "created":
                run["step_history"].append(
                    {
                        "step_id": step["id"],
                        "status": "queued",
                        "queued_at": now_iso(),
                        "work_order_path": str(order_path),
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                )
                run.setdefault("transition_provenance", []).append(
                    {
                        "transition": "drain_run",
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                )
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="step_queued",
                    reason_class="step_queued",
                    transition="drain_run",
                    principal=actor,
                    artifact_refs=[str(order_path), str(snapshot_path)] if snapshot_path else [str(order_path)],
                    run=run,
                )
                path = run_store.run_path(state_root, run_id)
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="drain_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise

    link_status = record_run_link_status(state_root, run)
    append_audit_event(
        state_root=state_root,
        event_type="drain_run",
        principal=actor,
        subject=subject,
        outcome="ok" if drained else "replayed",
        details={"drained": drained, "run_link": link_status},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "drained": drained,
        "run_path": str(path),
        "work_order_path": str(order_path),
        "step_snapshot_path": str(snapshot_path) if snapshot_path else None,
        "workflow_run": run,
        "work_order": work_order,
    }


def resume_run(
    *,
    state_root: Path,
    run_id: str,
    requeue: bool = False,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="resume_run",
        subject=subject,
    )
    try:
        payload = run_lifecycle.resume_run(
            state_root,
            run_id,
            principal=actor,
            requeue=requeue,
        )
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="resume_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise
    except run_lifecycle.LifecycleError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="resume_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "errors": exc.errors},
        )
        raise

    workflow_run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
    subject = {"run_id": run_id, "request_id": str(workflow_run.get("request_id") or "")}
    outcome = "ok"
    if payload.get("decision") == "blocked":
        outcome = "blocked"
    elif payload.get("reason") == "terminal_run_already_set":
        outcome = "replayed"
    append_audit_event(
        state_root=state_root,
        event_type="resume_run",
        principal=actor,
        subject=subject,
        outcome=outcome,
        details={
            "resumed": bool(payload.get("resumed")),
            "reason": payload.get("reason"),
            "next_action": payload.get("next_action"),
            "requeue": requeue,
        },
    )
    return payload


def abort_run(
    *,
    state_root: Path,
    run_id: str,
    reason: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="abort_run",
        subject=subject,
    )
    try:
        payload = run_lifecycle.abort_run(
            state_root,
            run_id,
            reason=reason,
            principal=actor,
        )
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="abort_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise
    except run_lifecycle.LifecycleError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="abort_run",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "errors": exc.errors},
        )
        raise

    workflow_run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
    subject = {"run_id": run_id, "request_id": str(workflow_run.get("request_id") or "")}
    append_audit_event(
        state_root=state_root,
        event_type="abort_run",
        principal=actor,
        subject=subject,
        outcome="ok" if payload.get("aborted") else "replayed",
        details={
            "aborted": bool(payload.get("aborted")),
            "reason": payload.get("reason"),
        },
    )
    return payload


def claude_headless_capability() -> dict[str, Any]:
    return {
        "adapter_contract_version": "1",
        "provider_adapter_id": "claude_headless_p0",
        "transport": "headless_cli",
        "sync_mode": "sync",
        "context_freshness": "fresh_process",
        "concurrency_unit": "process",
        "permission_enforcement": "harness",
        "supports_structured_output": True,
        "requires_marker": False,
        "reset_strategy": "new_session",
        "report_authority": "typed_report_and_evidence_file",
    }


def prepare_claude_adapter(
    *,
    state_root: Path,
    run_id: str,
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or default_manual_principal()
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="prepare_claude_adapter",
        subject={"run_id": run_id},
    )
    run = run_store.load_run(state_root, run_id)
    signature = assert_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="prepare_claude_adapter",
        subject={"run_id": run_id, "request_id": str(run.get("request_id") or "")},
    )
    step_id = str(run["current_step"])
    order_path = work_order_path(state_root, run_id, step_id)
    work_order = read_json(order_path)
    errors = validate_work_order_for_adapter(work_order)
    if errors:
        return {
            "schema_version": 1,
            "decision": "blocked",
            "reason": "work_order_not_adapter_safe",
            "errors": errors,
        }

    capability = claude_headless_capability()
    evidence_path = provider_evidence_path(state_root, run_id, step_id)
    transcript_path = provider_transcript_path(state_root, run_id, step_id)
    prompt = bounded_claude_prompt(work_order, evidence_path=evidence_path, transcript_path=transcript_path)
    adapter_request = {
        "adapter_request_version": "1",
        "adapter": capability,
        "run_id": run_id,
        "request_id": run["request_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "work_order_path": str(order_path),
        "report_path": work_order["report_path"],
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "prompt": prompt,
        "authority": {
            "provider_may_write": ["typed_report_file", "normalized_provider_evidence_file"],
            "provider_must_not": ["select_workflow", "approve_activation", "mutate_run_state", "edit_repo", "commit", "push"],
            "issued_by_principal": redacted_principal(actor),
            "prepare_signature": signature,
            "work_order_signature": work_order.get("work_order_authority", {}).get("signature"),
        },
    }
    path = adapter_request_path(state_root, run_id, step_id, capability["provider_adapter_id"])
    write_json(path, adapter_request)
    append_audit_event(
        state_root=state_root,
        event_type="prepare_claude_adapter",
        principal=actor,
        subject={"run_id": run_id, "request_id": str(run.get("request_id") or "")},
        outcome="ok",
        details={"adapter_request_path": str(path)},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "adapter_request_path": str(path),
        "adapter_request": adapter_request,
    }


def validate_work_order_for_adapter(work_order: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if work_order.get("workflow_id") != "single_step_external_review":
        errors.append("only single_step_external_review is supported")
    if work_order.get("step_id") != "review":
        errors.append("only review step is supported")
    if work_order.get("permission_mode") != "readonly":
        errors.append("permission_mode must be readonly")
    allowed_ops = ((work_order.get("activation_scope") or {}).get("allowed_ops") or {})
    for op in ("edit", "commit", "push", "network"):
        if allowed_ops.get(op) is not False:
            errors.append(f"activation_scope.allowed_ops.{op} must be false")
    context_scope = work_order.get("context_scope") or {}
    if context_scope.get("raw_transcript_sharing") != "forbidden":
        errors.append("raw transcript sharing must be forbidden")
    if not work_order.get("context_refs"):
        errors.append("context_refs must be non-empty")
    authority = work_order.get("work_order_authority")
    if not isinstance(authority, dict):
        errors.append("work_order_authority must be object")
    else:
        issuer = authority.get("issuer_principal")
        if not isinstance(issuer, dict):
            errors.append("work_order_authority.issuer_principal must be object")
        elif issuer.get("principal_type") == BRIDGE_PRINCIPAL_TYPE:
            errors.append("bridge principal cannot issue work orders")
        signature = authority.get("signature")
        if not isinstance(signature, dict) or not str(signature.get("signature", "")).startswith("sha256:"):
            errors.append("work_order_authority.signature must be present")
        runner_claim = authority.get("runner_claim")
        if not isinstance(runner_claim, dict) or runner_claim.get("claim_state") not in {"unclaimed", "claimed"}:
            errors.append("work_order_authority.runner_claim must be present")
    return errors


def bounded_claude_prompt(work_order: dict[str, Any], *, evidence_path: Path, transcript_path: Path) -> str:
    refs = "\n".join(f"- {item.get('value', item)}" for item in work_order.get("context_refs", []))
    return "\n".join(
        [
            "You are the bounded reviewer for a deterministic P0 orchestrator work order.",
            "Do not select workflows, approve activation, mutate run state, edit files, commit, push, or publish.",
            "Use only the bounded context refs listed below. Do not request or infer raw transcript sharing.",
            "",
            f"task_id: {work_order['task_id']}",
            f"request_id: {work_order['request_id']}",
            f"run_id: {work_order['run_id']}",
            f"workflow_id: {work_order['workflow_id']}",
            f"step_id: {work_order['step_id']}",
            "",
            "Context refs:",
            refs,
            "",
            "Instruction:",
            work_order["instruction"],
            "",
            "Return only an External Review Report JSON object matching",
            "organization/runtime/workflows/schemas/external-review-report.schema.json.",
            f"Set provider_evidence.evidence_path to: {evidence_path}",
            f"Set provider_evidence.transcript_path to: {transcript_path}",
            f"The canonical report path is: {work_order['report_path']}",
        ]
    )


def validate_report(
    *,
    state_root: Path,
    run_id: str,
    report_path_arg: str = "",
    principal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact_id(run_id, "run_id")
    actor = principal or make_principal("harness_runner", "local-harness", authn_method="local_cli")
    run_file = run_store.run_path(state_root, run_id)
    subject = {"run_id": run_id}
    precheck_execution_principal(
        state_root=state_root,
        principal=actor,
        transition="validate_report",
        subject=subject,
    )
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="validate_report",
            run_id=run_id,
            principal=actor,
        ):
            run = run_store.load_run(state_root, run_id)
            run_state = str(run.get("run_state") or "")
            subject = {"run_id": run_id, "request_id": str(run.get("request_id") or "")}
            signature = assert_execution_principal(
                state_root=state_root,
                principal=actor,
                transition="validate_report",
                subject=subject,
            )
            if run_state in run_lifecycle.TERMINAL_RUN_STATES:
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="validate_report",
                    principal=actor,
                    subject=subject,
                    outcome="replayed",
                    details={
                        "reason": "terminal_run_already_set",
                        "run_state": run.get("run_state"),
                        "run_link": link_status,
                    },
                )
                return {
                    "schema_version": 1,
                    "decision": "ok",
                    "validated": False,
                    "reason": "terminal_run_already_set",
                    "run_path": str(run_file),
                    "workflow_run": run,
                }
            step_id = str(run["current_step"])
            work_order = read_json(work_order_path(state_root, run_id, step_id))
            canonical_report_path = Path(str(work_order["report_path"])).expanduser()
            path = Path(report_path_arg).expanduser() if report_path_arg else canonical_report_path
            if not path_is_within(path, state_paths(state_root)["reports"]):
                raise FrontdoorError("report path must stay under orchestrator state reports directory")
            if path.resolve() != canonical_report_path.resolve():
                raise FrontdoorError("report path must match canonical work order report path")

            report = read_json(path)

            if run_state == "step_queued":
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="waiting_provider",
                    reason_class="manual_provider_execution_assumed",
                    transition="validate_report",
                    principal=actor,
                    artifact_refs=[str(work_order_path(state_root, run_id, step_id)), str(path)],
                    run=run,
                )
                run_state = "waiting_provider"
            if run_state == "waiting_provider":
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="validating",
                    reason_class="report_received",
                    transition="validate_report",
                    principal=actor,
                    artifact_refs=[str(path)],
                    run=run,
                )
                run_state = "validating"

            errors = validate_external_review_report(report, run=run, work_order=work_order, state_root=state_root)
            if errors:
                run["step_history"].append(
                    {
                        "step_id": step_id,
                        "status": "blocked",
                        "checked_at": now_iso(),
                        "report_path": str(path),
                        "errors": errors,
                        "principal": redacted_principal(actor),
                        "signature": signature,
                    }
                )
                run.setdefault("transition_provenance", []).append(
                    {
                        "transition": "validate_report",
                        "principal": redacted_principal(actor),
                        "signature": signature,
                        "result": "blocked",
                    }
                )
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="failed",
                    reason_class="invalid_report",
                    transition="validate_report",
                    principal=actor,
                    artifact_refs=[str(path)],
                    terminal_status="blocked",
                    terminal_reason="invalid_report",
                    run=run,
                )
                run_file = run_store.run_path(state_root, run_id)
                link_status = record_run_link_status(state_root, run)
                append_audit_event(
                    state_root=state_root,
                    event_type="validate_report",
                    principal=actor,
                    subject=subject,
                    outcome="blocked",
                    details={"reason": "invalid_report", "errors": errors, "run_link": link_status},
                )
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "invalid_report",
                    "errors": errors,
                    "workflow_run": run,
                }

            result = report["result"]
            if result in {"pass", "findings"}:
                to_state = "complete"
                terminal_status = "complete"
                terminal_reason = "report_valid"
                reason_class = "report_valid"
            else:
                to_state = "failed"
                terminal_status = "blocked"
                terminal_reason = f"provider_report_{result}"
                reason_class = terminal_reason

            run["step_history"].append(
                {
                    "step_id": step_id,
                    "status": terminal_status,
                    "checked_at": now_iso(),
                    "report_path": str(path),
                    "result": result,
                    "principal": redacted_principal(actor),
                    "signature": signature,
                }
            )
            run.setdefault("transition_provenance", []).append(
                {
                    "transition": "validate_report",
                    "principal": redacted_principal(actor),
                    "signature": signature,
                    "result": terminal_status,
                }
            )
            run_lifecycle.transition_run(
                state_root,
                run_id,
                to_state=to_state,
                reason_class=reason_class,
                transition="validate_report",
                principal=actor,
                artifact_refs=[str(path)],
                terminal_status=terminal_status,
                terminal_reason=terminal_reason,
                run=run,
            )
            run_file = run_store.run_path(state_root, run_id)
    except run_lock.LockContentionError as exc:
        append_audit_event(
            state_root=state_root,
            event_type="validate_report",
            principal=actor,
            subject=subject,
            outcome="blocked",
            details={"reason": exc.reason_class, "owner": exc.owner},
        )
        raise

    link_status = record_run_link_status(state_root, run)
    append_audit_event(
        state_root=state_root,
        event_type="validate_report",
        principal=actor,
        subject=subject,
        outcome="ok" if terminal_status == "complete" else "blocked",
        details={"report_status": terminal_status, "result": result, "run_link": link_status},
    )
    return {
        "schema_version": 1,
        "decision": "ok",
        "report_status": terminal_status,
        "run_path": str(run_file),
        "workflow_run": run,
        "report": report,
    }


def validate_external_review_report(
    report: dict[str, Any],
    *,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    errors: list[str] = []
    required = {
        "report_version",
        "report_id",
        "request_id",
        "run_id",
        "workflow_id",
        "step_id",
        "result",
        "summary",
        "provider_evidence",
        "findings",
        "authority",
    }
    allowed = required | {"recommendations"}
    missing = sorted(required - set(report))
    if missing:
        errors.append("missing_required_fields:" + ",".join(missing))
    extra = sorted(set(report) - allowed)
    if extra:
        errors.append("unexpected_fields:" + ",".join(extra))
    if report.get("report_version") != "1":
        errors.append("report_version must be '1'")
    for field in ("request_id", "run_id", "workflow_id", "step_id"):
        expected = str(run.get(field) if field != "step_id" else work_order.get("step_id"))
        if str(report.get(field)) != expected:
            errors.append(f"{field} mismatch: expected {expected!r}")
    if report.get("workflow_id") != "single_step_external_review":
        errors.append("workflow_id must be single_step_external_review")
    if report.get("step_id") != "review":
        errors.append("step_id must be review")
    if report.get("result") not in {"pass", "findings", "blocked", "invalid"}:
        errors.append("result unsupported")
    if not isinstance(report.get("summary"), str) or not report.get("summary"):
        errors.append("summary must be non-empty string")

    errors.extend(validate_provider_evidence(report.get("provider_evidence"), run, work_order, state_root))
    errors.extend(validate_findings(report.get("findings"), report.get("result")))
    errors.extend(validate_authority(report.get("authority")))
    return errors


def validate_provider_evidence(
    value: Any,
    run: dict[str, Any],
    work_order: dict[str, Any],
    state_root: Path,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["provider_evidence must be object"]
    required = {
        "provider",
        "effective_model",
        "request_id",
        "provider_session_id",
        "transcript_path",
        "evidence_path",
    }
    missing = sorted(required - set(value))
    if missing:
        errors.append("provider_evidence missing:" + ",".join(missing))
    extra = sorted(set(value) - required)
    if extra:
        errors.append("provider_evidence unexpected:" + ",".join(extra))
    if str(value.get("request_id")) != str(run.get("request_id")):
        errors.append("provider_evidence.request_id mismatch")
    for field in ("provider", "effective_model", "provider_session_id", "transcript_path", "evidence_path"):
        if not isinstance(value.get(field), str) or not value.get(field):
            errors.append(f"provider_evidence.{field} must be non-empty string")
    expected_paths = {
        "transcript_path": provider_transcript_path(
            state_root,
            str(run.get("run_id") or ""),
            str(work_order.get("step_id") or ""),
        ),
        "evidence_path": provider_evidence_path(
            state_root,
            str(run.get("run_id") or ""),
            str(work_order.get("step_id") or ""),
        ),
    }
    for field in ("transcript_path", "evidence_path"):
        path = Path(str(value.get(field, ""))).expanduser()
        if value.get(field) and not path.exists():
            errors.append(f"provider_evidence.{field} does not exist: {path}")
        if value.get(field) and not path_is_within(path, state_paths(state_root)["provider_evidence"]):
            errors.append(f"provider_evidence.{field} must stay under provider evidence state directory")
        if value.get(field) and path.resolve() != expected_paths[field].resolve():
            errors.append(f"provider_evidence.{field} must match current run evidence path")
    return errors


def validate_findings(value: Any, result: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        return ["findings must be array"]
    if result == "findings" and not value:
        errors.append("findings result requires at least one finding")
    required = {"finding_id", "severity", "status", "summary", "evidence_refs"}
    allowed = required
    for index, finding in enumerate(value):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] must be object")
            continue
        missing = sorted(required - set(finding))
        if missing:
            errors.append(f"findings[{index}] missing:" + ",".join(missing))
        extra = sorted(set(finding) - allowed)
        if extra:
            errors.append(f"findings[{index}] unexpected:" + ",".join(extra))
        if finding.get("severity") not in {"critical", "high", "medium", "low", "info"}:
            errors.append(f"findings[{index}].severity unsupported")
        if finding.get("status") not in {"open", "closed", "waived", "informational"}:
            errors.append(f"findings[{index}].status unsupported")
        if not isinstance(finding.get("finding_id"), str) or not finding.get("finding_id"):
            errors.append(f"findings[{index}].finding_id must be non-empty string")
        if not isinstance(finding.get("summary"), str) or not finding.get("summary"):
            errors.append(f"findings[{index}].summary must be non-empty string")
        if not isinstance(finding.get("evidence_refs"), list):
            errors.append(f"findings[{index}].evidence_refs must be array")
        elif any(not isinstance(item, str) or not item for item in finding["evidence_refs"]):
            errors.append(f"findings[{index}].evidence_refs entries must be non-empty strings")
    return errors


def validate_authority(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["authority must be object"]
    errors: list[str] = []
    expected = {
        "canonical_result": "typed_report_file",
        "stdout_is_signal_only": True,
        "raw_transcript_shared": False,
    }
    missing = sorted(set(expected) - set(value))
    if missing:
        errors.append("authority missing:" + ",".join(missing))
    extra = sorted(set(value) - set(expected))
    if extra:
        errors.append("authority unexpected:" + ",".join(extra))
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            errors.append(f"authority.{key} must be {expected_value!r}")
    return errors


def build_work_order(
    *,
    state_root: Path,
    run: dict[str, Any],
    request_record: dict[str, Any],
    template: dict[str, Any],
    step: dict[str, Any],
    issuer_principal: dict[str, Any],
) -> dict[str, Any]:
    resolved_refs = verified_context_refs_for_work_order(request_record)
    if not isinstance(resolved_refs, list) or not resolved_refs:
        refs = run["activation"]["context_scope"]["refs"]
        resolved_refs = [{"type": "repo_file", "value": ref, "path": ref} for ref in refs]
    step_id = str(step["id"])
    authority_subject = {
        "request_id": str(run["request_id"]),
        "run_id": str(run["run_id"]),
        "workflow_id": str(run["workflow_id"]),
        "step_id": step_id,
    }
    return work_order_builder.build_work_order(
        run=run,
        request_record=request_record,
        template=template,
        step=step,
        issuer_principal_redacted=redacted_principal(issuer_principal),
        resolved_refs=resolved_refs,
        policy_digest_value=policy_digest(request_record["approved_activation"]),
        signature=sign_transition(
            state_root=state_root,
            principal=issuer_principal,
            transition="issue_work_order",
            subject=authority_subject,
        ),
        report_path_value=str(report_path(state_root, str(run["run_id"]), step_id)),
    )


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Host-owned P0 frontdoor orchestrator")
    parser.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)

    propose = sub.add_parser("propose")
    propose.add_argument("--task-id", required=True)
    propose.add_argument("--request-id", required=True)
    propose.add_argument("--prompt", default="")
    propose.add_argument("--classification", default="")
    propose.add_argument("--ref", action="append", default=[])
    propose.add_argument("--allowed-path", action="append", default=[])
    propose.add_argument("--expires-at", default="run_terminal")
    propose.add_argument("--frontdoor", choices=["codex", "claude", "manual"], default="codex")
    propose.add_argument("--chat-session-id", default="")
    propose.add_argument("--principal-type", default="manual_operator")
    propose.add_argument("--principal-id", default="manual-cli")
    propose.add_argument("--authn-method", default="local_cli")

    approve = sub.add_parser("approve")
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--human-action-id", required=True)
    approve.add_argument("--principal-type", default="human_operator")
    approve.add_argument("--principal-id", default="human-ui")
    approve.add_argument("--authn-method", default="local_ui")

    create = sub.add_parser("create-run")
    create.add_argument("--request-id", required=True)
    create.add_argument("--run-id", default="")
    create.add_argument("--resume-policy", choices=["manual", "daemon_future"], default="manual")
    create.add_argument("--principal-type", default="manual_operator")
    create.add_argument("--principal-id", default="manual-cli")
    create.add_argument("--authn-method", default="local_cli")

    drain = sub.add_parser("drain")
    drain.add_argument("--run-id", required=True)
    drain.add_argument("--principal-type", default="manual_operator")
    drain.add_argument("--principal-id", default="manual-cli")
    drain.add_argument("--authn-method", default="local_cli")

    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--requeue", action="store_true")
    resume.add_argument("--principal-type", default="manual_operator")
    resume.add_argument("--principal-id", default="manual-cli")
    resume.add_argument("--authn-method", default="local_cli")

    abort = sub.add_parser("abort")
    abort.add_argument("--run-id", required=True)
    abort.add_argument("--reason", required=True)
    abort.add_argument("--principal-type", default="manual_operator")
    abort.add_argument("--principal-id", default="manual-cli")
    abort.add_argument("--authn-method", default="local_cli")

    sub.add_parser("adapter-capability")

    adapter = sub.add_parser("prepare-claude-adapter")
    adapter.add_argument("--run-id", required=True)
    adapter.add_argument("--principal-type", default="manual_operator")
    adapter.add_argument("--principal-id", default="manual-cli")
    adapter.add_argument("--authn-method", default="local_cli")

    report = sub.add_parser("validate-report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--report-path", default="")
    report.add_argument("--principal-type", default="harness_runner")
    report.add_argument("--principal-id", default="local-harness")
    report.add_argument("--authn-method", default="local_cli")

    task = sub.add_parser("task-view")
    task.add_argument("--task-id", required=True)
    task.add_argument("--format", choices=["json"], default="json")
    task.add_argument("--principal-type", default="manual_operator")
    task.add_argument("--principal-id", default="manual-cli")
    task.add_argument("--authn-method", default="local_cli")

    sub.add_parser("lock-status")

    bridge_submit = sub.add_parser("bridge-submit-request")
    bridge_submit.add_argument("--task-id", required=True)
    bridge_submit.add_argument("--request-id", required=True)
    bridge_submit.add_argument("--request-kind", choices=sorted(BRIDGE_REQUEST_KINDS), required=True)
    bridge_submit.add_argument("--prompt", default="")
    bridge_submit.add_argument("--ref", action="append", default=[])
    bridge_submit.add_argument("--allowed-path", action="append", default=[])
    bridge_submit.add_argument("--expires-at", default="run_terminal")
    bridge_submit.add_argument("--frontdoor", choices=["codex", "claude", "manual"], default="codex")
    bridge_submit.add_argument("--chat-session-id", default="")
    bridge_submit.add_argument("--idempotency-key", required=True)

    bridge_projection = sub.add_parser("bridge-read-projection")
    bridge_projection.add_argument("--request-id", required=True)
    bridge_projection.add_argument("--frontdoor", choices=["codex", "claude", "manual"], default="codex")
    bridge_projection.add_argument("--chat-session-id", default="")

    bridge_ack = sub.add_parser("bridge-ack-output")
    bridge_ack.add_argument("--request-id", required=True)
    bridge_ack.add_argument("--projection-digest", required=True)
    bridge_ack.add_argument("--frontdoor", choices=["codex", "claude", "manual"], default="codex")
    bridge_ack.add_argument("--chat-session-id", default="")

    channel = sub.add_parser("channel-token")
    channel.add_argument("--channel", choices=sorted(HTTP_CHANNEL_PRINCIPALS), required=True)
    return parser


def main() -> None:
    args = parser().parse_args()
    state_root = Path(args.state_root).expanduser()
    try:
        if args.command == "propose":
            classification = load_json_arg(args.classification) if args.classification else None
            payload = proposed_request(
                state_root=state_root,
                task_id=args.task_id,
                request_id=args.request_id,
                user_prompt=args.prompt,
                refs=args.ref,
                classification=classification,
                allowed_paths=args.allowed_path,
                expires_at=args.expires_at,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "approve":
            payload = approve_request(
                state_root=state_root,
                request_id=args.request_id,
                human_action_id=args.human_action_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "create-run":
            payload = create_run(
                state_root=state_root,
                request_id=args.request_id,
                run_id=args.run_id,
                resume_policy=args.resume_policy,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "drain":
            payload = drain_run(
                state_root=state_root,
                run_id=args.run_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "resume":
            payload = resume_run(
                state_root=state_root,
                run_id=args.run_id,
                requeue=args.requeue,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "abort":
            payload = abort_run(
                state_root=state_root,
                run_id=args.run_id,
                reason=args.reason,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "adapter-capability":
            payload = {
                "schema_version": 1,
                "decision": "ok",
                "adapter": claude_headless_capability(),
            }
        elif args.command == "prepare-claude-adapter":
            payload = prepare_claude_adapter(
                state_root=state_root,
                run_id=args.run_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "validate-report":
            payload = validate_report(
                state_root=state_root,
                run_id=args.run_id,
                report_path_arg=args.report_path,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "task-view":
            payload = task_view(
                state_root=state_root,
                task_id=args.task_id,
                principal=principal_from_cli(args.principal_type, args.principal_id, args.authn_method),
            )
        elif args.command == "lock-status":
            payload = run_lock.inspect_global_lock(state_root)
        elif args.command == "bridge-submit-request":
            payload = bridge_submit_request(
                state_root=state_root,
                payload={
                    "task_id": args.task_id,
                    "request_id": args.request_id,
                    "request_kind": args.request_kind,
                    "prompt": args.prompt,
                    "refs": args.ref,
                    "allowed_paths": args.allowed_path,
                    "expires_at": args.expires_at,
                    "frontdoor": args.frontdoor,
                    "chat_session_id": args.chat_session_id,
                    "idempotency_key": args.idempotency_key,
                },
            )
        elif args.command == "bridge-read-projection":
            payload = bridge_read_projection(
                state_root=state_root,
                request_id=args.request_id,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
            )
        elif args.command == "bridge-ack-output":
            payload = bridge_ack_output(
                state_root=state_root,
                request_id=args.request_id,
                projection_digest=args.projection_digest,
                frontdoor=args.frontdoor,
                chat_session_id=args.chat_session_id,
            )
        elif args.command == "channel-token":
            token = channel_token(state_root, args.channel)
            payload = {
                "schema_version": 1,
                "decision": "ok",
                "channel": args.channel,
                "token_path": str(channel_token_path(state_root, args.channel)),
                "token": token,
            }
        else:
            raise FrontdoorError(f"unsupported command: {args.command}")
    except run_store.RunStoreError as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "errors": exc.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except run_lock.LockContentionError as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "owner": exc.owner,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except run_lifecycle.LifecycleError as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "errors": exc.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    except FrontdoorError as exc:
        payload = {"schema_version": 1, "decision": "blocked", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("decision") == "blocked" or payload.get("request_status") == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
