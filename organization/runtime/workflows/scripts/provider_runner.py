#!/usr/bin/env python3
"""Headless provider adapter runner with normalized evidence artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import provider_adapters
import report_gate
import run_lifecycle
import run_lock
import run_store
import safe_paths
import scoped_worker_executor
import workflow_selector

BRIDGE_PRINCIPAL_TYPE = "main_agent_bridge"
EXECUTION_PRINCIPAL_TYPES = {
    "human_operator",
    "manual_operator",
    "harness_runner",
    "orchestrator_start",
}

DEFAULT_ADAPTER_ID = "claude_headless_p0"
REPO_ROOT = Path(__file__).resolve().parents[4]
LIVE_ENV_FLAG = "SAIHAI_ALLOW_LIVE_PROVIDERS"
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 1800
MAX_PROVIDER_TIMEOUT_SECONDS = 86400
DEFAULT_PROVIDER_LEASE_SECONDS = 90
DEFAULT_PROVIDER_HEARTBEAT_SECONDS = 30
DEFAULT_MAX_AUTO_RETRIES = 5
PROVIDER_MODEL_MISMATCH = "provider_model_mismatch"
MAX_LIVE_CONTEXT_REFS = 20
MAX_LIVE_CONTEXT_FILE_BYTES = 256_000
MAX_LIVE_CONTEXT_TOTAL_BYTES = 1_000_000
LIVE_ADAPTERS = {
    "claude_headless_p0": provider_adapters.invoke_claude_cli,
    "codex_cli_openai_p0": provider_adapters.invoke_codex_exec,
}
PROVIDER_TARGETS = {
    "claude_headless",
    "codex_cli_openai",
    "hermes_agent",
    "cursor_cli",
    "grok_build_cli",
}


class ProviderRunnerError(RuntimeError):
    """Stable provider runner failure."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def state_paths(state_root: Path) -> dict[str, Path]:
    return {
        "runs": state_root / "runs",
        "work_orders": state_root / "work-orders",
        "provider_evidence": state_root / "provider-evidence",
        "reports": state_root / "reports",
        "adapter_requests": state_root / "adapter-requests",
        "provider_attempts": state_root / "provider-evidence",
        "audit": state_root / "audit",
    }


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    if len(raw) >= 5 and raw[-5] in {"+", "-"} and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def validate_provider_timeout(timeout_seconds: int) -> int:
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds < 1
        or timeout_seconds > MAX_PROVIDER_TIMEOUT_SECONDS
    ):
        raise ProviderRunnerError(
            f"timeout_seconds must be between 1 and {MAX_PROVIDER_TIMEOUT_SECONDS}"
        )
    return timeout_seconds


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderRunnerError(f"unreadable json: {path}") from exc
    if not isinstance(payload, dict):
        raise ProviderRunnerError(f"expected object json: {path}")
    return payload


def load_verified_context_snapshot(context_refs: Any) -> list[dict[str, Any]]:
    if not isinstance(context_refs, list) or not context_refs:
        raise ProviderRunnerError("context_snapshot_missing")
    if len(context_refs) > MAX_LIVE_CONTEXT_REFS:
        raise ProviderRunnerError("context_snapshot_ref_limit")
    snapshots: list[dict[str, Any]] = []
    total_bytes = 0
    root = REPO_ROOT.resolve()
    for item in context_refs:
        if not isinstance(item, dict) or item.get("type") != "repo_file":
            raise ProviderRunnerError("context_snapshot_ref_invalid")
        raw_path = str(item.get("value") or "")
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise ProviderRunnerError("context_snapshot_path_invalid")
        candidate = root / relative
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
            resolved_parent.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ProviderRunnerError("context_snapshot_path_invalid") from exc
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(candidate, flags)
        except OSError as exc:
            raise ProviderRunnerError("context_snapshot_unavailable") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProviderRunnerError("context_snapshot_not_regular")
            expected_size = item.get("size_bytes")
            if not isinstance(expected_size, int) or isinstance(expected_size, bool):
                raise ProviderRunnerError("context_snapshot_size_missing")
            if expected_size > MAX_LIVE_CONTEXT_FILE_BYTES:
                raise ProviderRunnerError("context_snapshot_file_limit")
            content = b""
            while len(content) <= MAX_LIVE_CONTEXT_FILE_BYTES:
                chunk = os.read(fd, min(64 * 1024, MAX_LIVE_CONTEXT_FILE_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content += chunk
        finally:
            os.close(fd)
        if len(content) != expected_size or metadata.st_size != expected_size:
            raise ProviderRunnerError("context_snapshot_size_mismatch")
        expected_digest = str(item.get("digest") or "")
        actual_digest = sha256_bytes(content)
        if expected_digest != actual_digest:
            raise ProviderRunnerError("context_snapshot_digest_mismatch")
        total_bytes += len(content)
        if total_bytes > MAX_LIVE_CONTEXT_TOTAL_BYTES:
            raise ProviderRunnerError("context_snapshot_total_limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderRunnerError("context_snapshot_not_utf8") from exc
        snapshots.append(
            {
                "path": relative.as_posix(),
                "size_bytes": len(content),
                "sha256": actual_digest,
                "content": text,
            }
        )
    return snapshots


PRIVATE_STATE_NAMESPACES = {"adapter-requests", "provider-evidence"}


def runner_state_artifact_path(
    state_root: Path,
    namespace: str,
    *components: str,
) -> Path:
    try:
        return safe_paths.state_artifact_path(state_root, namespace, *components)
    except safe_paths.SafePathError as exc:
        raise ProviderRunnerError(str(exc)) from exc


def confined_private_path(state_root: Path, path: Path) -> Path:
    try:
        return safe_paths.confined_state_path(
            state_root,
            path,
            namespaces=PRIVATE_STATE_NAMESPACES,
        )
    except safe_paths.SafePathError as exc:
        raise ProviderRunnerError(str(exc)) from exc


def secure_directory(state_root: Path, path: Path) -> None:
    path = confined_private_path(state_root, path)
    parent = path.parent
    if parent == path:
        raise ProviderRunnerError("private_artifact_parent_invalid")
    try:
        run_store.ensure_private_directory(parent)
    except run_store.RunStoreError as exc:
        raise ProviderRunnerError("private_artifact_parent_unsafe") from exc
    if path.exists():
        if path.is_symlink():
            raise ProviderRunnerError("private_artifact_directory_symlink")
        metadata = path.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ProviderRunnerError("private_artifact_directory_unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ProviderRunnerError("private_artifact_directory_mode")
            os.chmod(path, 0o700)
        return
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def private_atomic_write_json(state_root: Path, path: Path, payload: Any) -> None:
    path = confined_private_path(state_root, path)
    secure_directory(state_root, path.parent)
    if path.exists():
        if path.is_symlink():
            raise ProviderRunnerError("private_artifact_symlink")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ProviderRunnerError("private_artifact_unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ProviderRunnerError("private_artifact_mode")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    metadata = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ProviderRunnerError("private_artifact_post_write_validation_failed")


def secure_artifact_tree(state_root: Path, path: Path, root_name: str) -> None:
    if root_name not in PRIVATE_STATE_NAMESPACES:
        raise ProviderRunnerError("private_artifact_root_missing")
    path = confined_private_path(state_root, path)
    ancestors = list(path.parents)
    roots = [candidate for candidate in ancestors if candidate.name == root_name]
    if not roots:
        raise ProviderRunnerError("private_artifact_root_missing")
    root = roots[0]
    chain = [root]
    relative = path.parent.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        chain.append(current)
    for directory in chain:
        secure_directory(state_root, directory)


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
                "principal": run_lifecycle.redacted_principal(principal),
                "subject": subject,
                "created_at": time.time_ns(),
            }
        )[:20],
        "created_at": now_iso(),
        "event_type": event_type,
        "principal": run_lifecycle.redacted_principal(principal),
        "subject": subject,
        "outcome": outcome,
        "details": details or {},
    }
    path = state_paths(state_root)["audit"] / "events.jsonl"
    run_store.append_json_line(path, event)
    return event


def precheck_execution_principal(
    *,
    state_root: Path,
    principal: dict[str, Any],
    subject: dict[str, Any],
) -> None:
    principal_type = str(principal.get("principal_type") or "")
    if principal_type in EXECUTION_PRINCIPAL_TYPES:
        return
    reason = (
        "bridge principal cannot perform execution transition"
        if principal_type == BRIDGE_PRINCIPAL_TYPE
        else "unsupported execution principal"
    )
    append_audit_event(
        state_root=state_root,
        event_type="run_provider",
        principal=principal,
        subject=subject,
        outcome="blocked",
        details={"reason": reason, "principal_type": principal_type},
    )
    raise ProviderRunnerError(f"{reason}: {principal_type}")


def work_order_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return runner_state_artifact_path(
        state_root,
        "work-orders",
        run_store.validate_artifact_id(run_id, "run_id"),
        f"{run_store.validate_artifact_id(step_id, 'step_id')}.json",
    )


def provider_evidence_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return runner_state_artifact_path(
        state_root,
        "provider-evidence",
        run_store.validate_artifact_id(run_id, "run_id"),
        f"{run_store.validate_artifact_id(step_id, 'step_id')}-provider-evidence.json",
    )


def provider_transcript_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return runner_state_artifact_path(
        state_root,
        "provider-evidence",
        run_store.validate_artifact_id(run_id, "run_id"),
        f"{run_store.validate_artifact_id(step_id, 'step_id')}-provider-transcript.json",
    )


def provider_report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return runner_state_artifact_path(
        state_root,
        "reports",
        run_store.validate_artifact_id(run_id, "run_id"),
        f"{run_store.validate_artifact_id(step_id, 'step_id')}-external-review-report.json",
    )


def adapter_request_path(state_root: Path, run_id: str, step_id: str, adapter_id: str) -> Path:
    return runner_state_artifact_path(
        state_root,
        "adapter-requests",
        run_store.validate_artifact_id(run_id, "run_id"),
        f"{run_store.validate_artifact_id(step_id, 'step_id')}-{run_store.validate_artifact_id(adapter_id, 'adapter_id')}.json",
    )


def verified_request_artifact_paths(
    *,
    state_root: Path,
    run_id: str,
    step_id: str,
    request: dict[str, Any],
) -> tuple[Path, Path, Path]:
    """Recompute canonical runner-owned paths instead of trusting request strings."""

    report_path = provider_report_path(state_root, run_id, step_id)
    evidence_path = provider_evidence_path(state_root, run_id, step_id)
    transcript_path = provider_transcript_path(state_root, run_id, step_id)
    expected = {
        "report_path": report_path,
        "evidence_path": evidence_path,
        "transcript_path": transcript_path,
    }
    for field, canonical in expected.items():
        if str(request.get(field) or "") != str(canonical):
            raise ProviderRunnerError(f"provider_{field.removesuffix('_path')}_path_mismatch")
    return report_path, evidence_path, transcript_path


def load_provider_adapters(registry: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    registry = registry or workflow_selector.load_registry()
    adapters = registry.get("provider_adapters")
    if not isinstance(adapters, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in adapters:
        if not isinstance(item, dict):
            continue
        adapter_id = str(item.get("provider_adapter_id") or "")
        if adapter_id:
            result[adapter_id] = item
    return result


def validate_adapter_descriptor(adapter: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "adapter_contract_version",
        "provider_adapter_id",
        "provider_target",
        "transport",
        "sync_mode",
        "context_freshness",
        "concurrency_unit",
        "permission_enforcement",
        "supports_structured_output",
        "requires_marker",
        "reset_strategy",
        "report_authority",
        "default_model",
    }
    missing = sorted(required - set(adapter))
    if missing:
        errors.append("adapter missing:" + ",".join(missing))
    if adapter.get("adapter_contract_version") != "1":
        errors.append("adapter_contract_version must be '1'")
    if adapter.get("provider_target") not in PROVIDER_TARGETS:
        errors.append("provider_target unsupported")
    if adapter.get("transport") != "headless_cli":
        errors.append("only headless_cli transport is runnable in this runner")
    if adapter.get("permission_enforcement") != "harness":
        errors.append("permission_enforcement must be harness")
    if adapter.get("report_authority") != "typed_report_and_evidence_file":
        errors.append("report_authority must be typed_report_and_evidence_file")
    if adapter.get("supports_structured_output") is not True:
        errors.append("supports_structured_output must be true")
    if not isinstance(adapter.get("default_model"), str) or not adapter.get("default_model"):
        errors.append("default_model must be non-empty string")
    if adapter.get("provider_adapter_id") == "claude_headless_p0" and adapter.get(
        "command_argv"
    ) != provider_adapters.claude_argv_template(str(adapter.get("default_model") or "")):
        errors.append("claude command_argv must match the registry-pinned model template")
    return errors


def validate_work_order_for_runner(
    work_order: dict[str, Any],
    *,
    state_root: Path | None = None,
    run: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if work_order.get("workflow_id") != "single_step_external_review":
        errors.append("only single_step_external_review is supported")
    if work_order.get("step_id") != "review":
        errors.append("only review step is supported")
    if work_order.get("permission_mode") != "readonly":
        errors.append("permission_mode must be readonly")
    if work_order.get("external_provider_allowed") is not True:
        errors.append("external_provider_allowed must be true")
    if not isinstance(work_order.get("intended_model"), str) or not work_order.get("intended_model"):
        errors.append("intended_model must be non-empty string")
    allowed_ops = ((work_order.get("activation_scope") or {}).get("allowed_ops") or {})
    for op in ("edit", "commit", "push", "network"):
        if allowed_ops.get(op) is not False:
            errors.append(f"activation_scope.allowed_ops.{op} must be false")
    if (work_order.get("context_scope") or {}).get("raw_transcript_sharing") != "forbidden":
        errors.append("raw transcript sharing must be forbidden")
    authority = work_order.get("work_order_authority")
    if not isinstance(authority, dict):
        errors.append("work_order_authority must be object")
    elif (authority.get("issuer_principal") or {}).get("principal_type") == BRIDGE_PRINCIPAL_TYPE:
        errors.append("bridge principal cannot issue work orders")
    if state_root is not None and run is not None:
        raw_report_path = work_order.get("report_path")
        if not isinstance(raw_report_path, str) or not raw_report_path:
            errors.append("report_path must be non-empty string")
        else:
            report_path = Path(raw_report_path).expanduser()
            canonical_report_path = report_gate.report_path(
                state_root,
                str(run.get("run_id") or ""),
                str(work_order.get("step_id") or ""),
            )
            if not report_gate.path_is_within(report_path, state_paths(state_root)["reports"]):
                errors.append("report_path must stay under reports")
            if report_path.resolve() != canonical_report_path.resolve():
                errors.append("report_path must match current run report path")
    return errors


def runner_claim_is_live(work_order: dict[str, Any]) -> bool:
    return run_lifecycle.provider_claim_is_live({}, work_order)


def adapter_request(
    *,
    state_root: Path,
    run: dict[str, Any],
    work_order: dict[str, Any],
    adapter: dict[str, Any],
    principal: dict[str, Any],
    work_order_digest: str = "",
    snapshot_path: Path | None = None,
    attempt_id: str = "",
    lease_id: str = "",
    timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    execution_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = str(run["run_id"])
    step_id = str(work_order["step_id"])
    evidence_path = provider_evidence_path(state_root, run_id, step_id)
    transcript_path = provider_transcript_path(state_root, run_id, step_id)
    report_path = provider_report_path(state_root, run_id, step_id)
    context_snapshot = load_verified_context_snapshot(work_order.get("context_refs"))
    context_snapshot_digest = "sha256:" + stable_digest(context_snapshot)
    context_json = json.dumps(context_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    context_bytes = context_json.encode("utf-8")
    if len(context_bytes) > provider_adapters.MAX_CONTEXT_BYTES:
        raise ProviderRunnerError("context_snapshot_serialized_limit")
    intended_model = str(work_order.get("intended_model") or "")
    if not intended_model:
        raise ProviderRunnerError("intended_model_missing")
    request = {
        "adapter_request_version": "1",
        "adapter": adapter,
        "run_id": run_id,
        "request_id": run["request_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "intended_model": intended_model,
        "work_order_path": str(work_order_path(state_root, run_id, step_id)),
        "report_path": str(report_path),
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "instruction": work_order["instruction"],
        "context_refs": work_order.get("context_refs", []),
        "approved_context": context_snapshot,
        "context_snapshot": {
            "content": context_json,
            "byte_length": len(context_bytes),
            "sha256": hashlib.sha256(context_bytes).hexdigest(),
        },
        "context_snapshot_digest": context_snapshot_digest,
        "execution_binding": execution_binding or {},
        "work_order_digest": work_order_digest,
        "work_order_snapshot_path": str(snapshot_path) if snapshot_path else "",
        "attempt_id": attempt_id,
        "lease_id": lease_id,
        "timeout_seconds": timeout_seconds,
        "authority": {
            "provider_may_write": [],
            "runner_writes": ["typed_report_file", "normalized_provider_evidence_file", "provider_transcript_file"],
            "provider_must_not": [
                "select_workflow",
                "approve_activation",
                "mutate_run_state",
                "edit_repo",
                "commit",
                "push",
                "publish",
                "copy_raw_transcript_to_run_state",
            ],
            "issued_by_principal": run_lifecycle.redacted_principal(principal),
            "work_order_signature": (work_order.get("work_order_authority") or {}).get("signature"),
        },
    }
    request["adapter_request_digest"] = "sha256:" + stable_digest(request)
    return request


def provider_attempt_paths(state_root: Path, run_id: str, attempt_id: str) -> tuple[Path, Path]:
    safe_run = run_store.validate_artifact_id(run_id, "run_id")
    safe_attempt = run_store.validate_artifact_id(attempt_id, "attempt_id")
    return (
        runner_state_artifact_path(
            state_root,
            "provider-evidence",
            safe_run,
            "attempts",
            f"{safe_attempt}-result.json",
        ),
        runner_state_artifact_path(
            state_root,
            "provider-evidence",
            safe_run,
            "attempts",
            f"{safe_attempt}-transcript.json",
        ),
    )


def read_private_json(
    state_root: Path,
    path: Path,
    *,
    artifact_kind: str,
) -> dict[str, Any]:
    path = confined_private_path(state_root, path)
    if path.is_symlink():
        raise ProviderRunnerError(f"{artifact_kind}_symlink")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ProviderRunnerError(f"{artifact_kind}_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ProviderRunnerError(f"{artifact_kind}_unsafe")
    return read_json(path)


def recover_completed_provider_attempt(
    *,
    state_root: Path,
    run: dict[str, Any],
    adapter: dict[str, Any],
) -> dict[str, Any] | None:
    """Promote an owner-only completed-attempt journal without invoking the provider again."""

    execution = run.get("provider_execution")
    if not isinstance(execution, dict):
        return None
    recorded_adapter_id = str(execution.get("adapter_id") or "")
    if adapter.get("provider_adapter_id") != recorded_adapter_id:
        raise ProviderRunnerError("provider_adapter_replay_mismatch")
    attempt_id = str(execution.get("attempt_id") or "")
    lease = execution.get("lease") if isinstance(execution.get("lease"), dict) else {}
    lease_id = str(lease.get("lease_id") or "")
    if not attempt_id or not lease_id:
        return None
    result_path, attempt_transcript_path = provider_attempt_paths(
        state_root, str(run["run_id"]), attempt_id
    )
    if not result_path.exists():
        return None
    journal = read_private_json(
        state_root, result_path, artifact_kind="provider_attempt_result"
    )
    if journal.get("abandoned") is True:
        return None
    expected_bindings = {
        "attempt_id": attempt_id,
        "lease_id": lease_id,
        "work_order_digest": execution.get("work_order_digest"),
        "adapter_request_digest": execution.get("adapter_request_digest"),
        "context_snapshot_digest": execution.get("context_snapshot_digest"),
        "adapter_id": execution.get("adapter_id"),
    }
    if any(journal.get(key) != value for key, value in expected_bindings.items()):
        raise ProviderRunnerError("provider_attempt_result_binding_mismatch")
    outcome = str(journal.get("outcome") or "")
    report = journal.get("report")
    details = journal.get("details")
    if not outcome or not isinstance(details, dict):
        raise ProviderRunnerError("provider_attempt_result_invalid")
    if outcome == "ok" and not isinstance(report, dict):
        raise ProviderRunnerError("provider_attempt_result_invalid")
    if outcome != "ok":
        report = None
    if Path(str(journal.get("transcript_path") or "")) != attempt_transcript_path:
        raise ProviderRunnerError("provider_attempt_transcript_path_mismatch")
    transcript_payload = read_private_json(
        state_root,
        attempt_transcript_path,
        artifact_kind="provider_attempt_transcript",
    )
    if journal.get("transcript_sha256") != file_sha256(attempt_transcript_path):
        raise ProviderRunnerError("provider_attempt_transcript_digest_mismatch")

    step_id = str(execution.get("step_id") or "")
    request_path = adapter_request_path(
        state_root, str(run["run_id"]), step_id, str(adapter["provider_adapter_id"])
    )
    request = read_private_json(state_root, request_path, artifact_kind="adapter_request")
    request_copy = dict(request)
    supplied_digest = str(request_copy.pop("adapter_request_digest", ""))
    if (
        supplied_digest != execution.get("adapter_request_digest")
        or supplied_digest != "sha256:" + stable_digest(request_copy)
        or request.get("attempt_id") != attempt_id
        or request.get("lease_id") != lease_id
        or request.get("work_order_digest") != execution.get("work_order_digest")
        or request.get("context_snapshot_digest") != execution.get("context_snapshot_digest")
    ):
        raise ProviderRunnerError("adapter_request_digest_mismatch")

    report_path, evidence_path, transcript_path = verified_request_artifact_paths(
        state_root=state_root,
        run_id=str(run["run_id"]),
        step_id=step_id,
        request=request,
    )
    private_atomic_write_json(state_root, transcript_path, transcript_payload)
    recovered_details = dict(details)
    recovered_details["transcript_sha256"] = file_sha256(transcript_path)
    if transcript_payload.get("provider_transcript_version") == "1":
        recovered_details["stdout_sha256"] = transcript_payload.get("stdout_sha256")
        recovered_details["stderr_sha256"] = transcript_payload.get("stderr_sha256")
    evidence = normalized_evidence(
        request=request,
        adapter=adapter,
        report=report,
        outcome=outcome,
        details=recovered_details,
    )
    private_atomic_write_json(state_root, evidence_path, evidence)
    if outcome == "ok":
        run_store.ensure_private_directory(report_path.parent)
        run_store.atomic_write_json(report_path, report)
        execution["phase"] = "result_ready"
    execution["last_outcome"] = {
        "reason_class": recovered_details.get("reason") or outcome,
        "recorded_at": now_iso(),
        "attempt_result_path": str(result_path),
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "recovered_from_journal": True,
    }
    return {
        "attempt_id": attempt_id,
        "outcome": outcome,
        "details": recovered_details,
        "attempt_result_path": result_path,
        "request": request,
        "request_path": request_path,
        "report_path": report_path,
        "evidence_path": evidence_path,
        "transcript_path": transcript_path,
        "evidence": evidence,
    }


def execution_lease_valid(execution: Any, *, lease_id: str = "", attempt_id: str = "") -> bool:
    if not isinstance(execution, dict):
        return False
    lease = execution.get("lease")
    if not isinstance(lease, dict):
        return False
    if lease_id and lease.get("lease_id") != lease_id:
        return False
    if attempt_id and execution.get("attempt_id") != attempt_id:
        return False
    expires = parse_timestamp(lease.get("lease_expires_at"))
    return expires is not None and expires > datetime.now(timezone.utc)


def build_provider_execution(
    *,
    run: dict[str, Any],
    adapter_id: str,
    work_order_digest: str,
    request: dict[str, Any],
    attempt_id: str,
    lease_id: str,
    timeout_seconds: int,
    principal: dict[str, Any],
) -> dict[str, Any]:
    previous = run.get("provider_execution") if isinstance(run.get("provider_execution"), dict) else {}
    retry = previous.get("retry") if isinstance(previous.get("retry"), dict) else {}
    attempt_number = int(previous.get("attempt_number") or 0) + 1
    claimed_at = now_iso()
    return {
        "execution_version": "1",
        "step_id": request["step_id"],
        "adapter_id": adapter_id,
        "work_order_digest": work_order_digest,
        "adapter_request_digest": request["adapter_request_digest"],
        "context_snapshot_digest": request["context_snapshot_digest"],
        "phase": "claimed",
        "attempt_number": attempt_number,
        "attempt_id": attempt_id,
        "timeout_seconds": timeout_seconds,
        "lease": {
            "lease_id": lease_id,
            "claimed_by": run_lifecycle.redacted_principal(principal),
            "claimed_at": claimed_at,
            "last_heartbeat_at": claimed_at,
            "lease_expires_at": future_iso(DEFAULT_PROVIDER_LEASE_SECONDS),
        },
        "retry": {
            "last_failure_fingerprint": retry.get("last_failure_fingerprint"),
            "consecutive_failures": int(retry.get("consecutive_failures") or 0),
            "auto_retries_used": int(retry.get("auto_retries_used") or 0),
            "max_auto_retries": DEFAULT_MAX_AUTO_RETRIES,
        },
        "last_outcome": previous.get("last_outcome"),
    }


def authorize_provider_dispatch(
    *,
    state_root: Path,
    run_id: str,
    request: dict[str, Any],
    attempt_id: str,
    lease_id: str,
    principal: dict[str, Any],
) -> None:
    with run_lock.hold_global_lock(
        state_root,
        operation="authorize_provider_dispatch",
        run_id=run_id,
        principal=principal,
    ):
        run = run_store.load_run(state_root, run_id)
        execution = run.get("provider_execution")
        if run.get("run_state") != "waiting_provider" or not execution_lease_valid(
            execution, lease_id=lease_id, attempt_id=attempt_id
        ):
            raise ProviderRunnerError("provider_lease_lost")
        if not isinstance(execution, dict):
            raise ProviderRunnerError("provider_execution_missing")
        if execution.get("adapter_request_digest") != request.get("adapter_request_digest"):
            raise ProviderRunnerError("adapter_request_digest_mismatch")
        request_copy = dict(request)
        supplied_digest = str(request_copy.pop("adapter_request_digest", ""))
        if supplied_digest != "sha256:" + stable_digest(request_copy):
            raise ProviderRunnerError("adapter_request_digest_mismatch")
        current_context = load_verified_context_snapshot(request.get("context_refs"))
        if "sha256:" + stable_digest(current_context) != execution.get("context_snapshot_digest"):
            raise ProviderRunnerError("context_snapshot_digest_mismatch")
        frozen_binding = request.get("execution_binding")
        if isinstance(frozen_binding, dict) and frozen_binding:
            try:
                current_binding = provider_adapters.resolve_execution_binding(str(execution["adapter_id"]))
            except provider_adapters.AdapterConfigurationError as exc:
                raise ProviderRunnerError("binary_binding_invalid") from exc
            if current_binding != frozen_binding:
                raise ProviderRunnerError("binary_binding_invalid")
        scoped_worker_executor.verify_frozen_work_order(
            state_root,
            run_id=run_id,
            step_id=str(request["step_id"]),
            expected_run_states={"waiting_provider"},
            expected_iteration=int(run["iteration"]),
            expected_work_order_digest=str(execution["work_order_digest"]),
        )
        execution["phase"] = "invoking"
        execution["lease"]["last_heartbeat_at"] = now_iso()
        execution["lease"]["lease_expires_at"] = future_iso(DEFAULT_PROVIDER_LEASE_SECONDS)
        run_store.store_run(state_root, run, expected_current_state="waiting_provider")


def renew_provider_lease(
    *,
    state_root: Path,
    run_id: str,
    attempt_id: str,
    lease_id: str,
    principal: dict[str, Any],
) -> bool:
    try:
        with run_lock.hold_global_lock(
            state_root,
            operation="renew_provider_lease",
            run_id=run_id,
            principal=principal,
        ):
            run = run_store.load_run(state_root, run_id)
            execution = run.get("provider_execution")
            if run.get("run_state") != "waiting_provider" or not execution_lease_valid(
                execution, lease_id=lease_id, attempt_id=attempt_id
            ):
                return False
            execution["lease"]["last_heartbeat_at"] = now_iso()
            execution["lease"]["lease_expires_at"] = future_iso(DEFAULT_PROVIDER_LEASE_SECONDS)
            run_store.store_run(state_root, run, expected_current_state="waiting_provider")
            return True
    except (run_lock.LockContentionError, run_store.RunStoreError):
        return False


def fake_provider_report(
    *,
    request: dict[str, Any],
    adapter: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    result = "pass"
    findings: list[dict[str, Any]] = []
    if mode == "findings":
        result = "findings"
        findings = [
            {
                "finding_id": "F-1",
                "severity": "low",
                "status": "open",
                "summary": "Fake provider finding.",
                "evidence_refs": ["organization/runtime/workflows/README.md"],
            }
        ]
    elif mode == "blocked":
        result = "blocked"
    provider = str(adapter.get("provider_target") or adapter.get("provider_adapter_id"))
    effective_model = str(request.get("intended_model") or "")
    if mode == "model_mismatch":
        effective_model = "claude-model-mismatch"
    provider_evidence = {
        "provider": provider,
        "intended_model": str(request.get("intended_model") or ""),
        "effective_model": effective_model,
        "request_id": request["request_id"],
        "provider_session_id": f"fake-session-{request['run_id']}",
        "transcript_path": request["transcript_path"],
        "evidence_path": request["evidence_path"],
    }
    if mode == "missing_effective_model":
        provider_evidence.pop("effective_model")
    return {
        "report_version": "1",
        "report_id": f"report-{request['run_id']}",
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "workflow_id": request["workflow_id"],
        "step_id": request["step_id"],
        "result": result,
        "summary": "Fake provider completed the bounded work order.",
        "provider_evidence": provider_evidence,
        "findings": findings,
        "authority": {
            "canonical_result": "typed_report_file",
            "stdout_is_signal_only": True,
            "raw_transcript_shared": False,
        },
    }


def execute_provider(
    *,
    request: dict[str, Any],
    adapter: dict[str, Any],
    timeout_seconds: int,
    fake_provider_mode: str,
    live: bool = False,
    heartbeat: Any = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    started = time.monotonic()
    if fake_provider_mode:
        if fake_provider_mode == "timeout":
            time.sleep(0.001)
            return "provider_timeout", None, {"timed_out": True, "duration_ms": int((time.monotonic() - started) * 1000)}
        if fake_provider_mode == "nonzero":
            return "provider_nonzero_exit", None, {"exit_code": 42, "duration_ms": int((time.monotonic() - started) * 1000)}
        if fake_provider_mode == "malformed":
            return "provider_malformed_output", None, {"stdout_sha256": "sha256:" + hashlib.sha256(b"not json").hexdigest()}
        if fake_provider_mode == "malformed_binary":
            return parse_provider_stdout(b"\xff")
        if fake_provider_mode == "unavailable":
            return "provider_unavailable", None, {"reason": "fake_provider_unavailable"}
        report = fake_provider_report(request=request, adapter=adapter, mode=fake_provider_mode)
        report_evidence = report.get("provider_evidence")
        details = {"duration_ms": int((time.monotonic() - started) * 1000)}
        if isinstance(report_evidence, dict):
            effective_model = report_evidence.get("effective_model")
            if isinstance(effective_model, str) and effective_model:
                details["effective_model"] = effective_model
        return "ok", report, details

    adapter_id = str(adapter.get("provider_adapter_id") or "")
    if adapter_id not in LIVE_ADAPTERS:
        return "provider_unavailable", None, {"reason": "live_adapter_not_supported"}
    if not live:
        return "provider_unavailable", None, {"reason": "live_execution_not_enabled"}
    if os.environ.get(LIVE_ENV_FLAG) != "1":
        return "provider_unavailable", None, {"reason": "live_env_guard_missing"}

    try:
        invocation = LIVE_ADAPTERS[adapter_id](
            request,
            timeout_seconds=timeout_seconds,
            heartbeat=heartbeat,
        )
    except OSError as exc:
        invocation = {
            "status": "unavailable",
            "reason": "provider_os_error",
            "exit_code": None,
            "stdout": b"",
            "stderr": str(exc).encode("utf-8", errors="replace"),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    status_map = {
        "ok": "ok",
        "timeout": "provider_timeout",
        "nonzero_exit": "provider_nonzero_exit",
        "malformed_output": "provider_malformed_output",
        "unavailable": "provider_unavailable",
    }
    outcome = status_map.get(str(invocation.get("status") or ""), "provider_malformed_output")
    evidence_fields = invocation.get("evidence_fields")
    details = {
        "reason": invocation.get("reason") or outcome,
        "duration_ms": int(invocation.get("duration_ms") or 0),
        "exit_code": invocation.get("exit_code"),
        "timed_out": outcome == "provider_timeout",
        "_raw_stdout": invocation.get("stdout") or b"",
        "_raw_stderr": invocation.get("stderr") or b"",
        "_live": True,
    }
    if isinstance(evidence_fields, dict):
        for field in (
            "provider",
            "effective_model",
            "provider_request_id",
            "provider_session_id",
            "usage",
        ):
            if evidence_fields.get(field) not in (None, "", {}):
                details[field] = evidence_fields[field]
    report = invocation.get("report") if isinstance(invocation.get("report"), dict) else None
    if outcome == "ok" and report is not None:
        report = bind_live_report(report=report, request=request, adapter=adapter, details=details)
    return outcome, report, details


def bind_live_report(
    *,
    report: dict[str, Any],
    request: dict[str, Any],
    adapter: dict[str, Any],
    details: dict[str, Any],
) -> dict[str, Any]:
    bound = dict(report)
    supplied = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
    bound["provider_evidence"] = {
        "provider": details.get("provider") or supplied.get("provider") or adapter["provider_target"],
        "intended_model": request.get("intended_model") or "",
        "effective_model": details.get("effective_model") or "",
        "request_id": request["request_id"],
        "provider_session_id": details.get("provider_session_id") or supplied.get("provider_session_id") or f"session-{request['run_id']}",
        "transcript_path": request["transcript_path"],
        "evidence_path": request["evidence_path"],
    }
    return bound


def parse_provider_stdout(stdout: bytes | str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    raw = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
    stdout_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "provider_malformed_output", None, {"stdout_sha256": stdout_sha256}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "provider_malformed_output", None, {"stdout_sha256": stdout_sha256}
    if not isinstance(payload, dict):
        return "provider_malformed_output", None, {"reason": "stdout_json_not_object"}
    return "ok", payload, {"stdout_sha256": stdout_sha256}


def parsed_effective_model(details: dict[str, Any]) -> str:
    candidate = details.get("effective_model")
    if isinstance(candidate, str) and candidate:
        return candidate
    return ""


def normalized_evidence(
    *,
    request: dict[str, Any],
    adapter: dict[str, Any],
    report: dict[str, Any] | None,
    outcome: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    report_evidence = report.get("provider_evidence") if isinstance(report, dict) else {}
    if not isinstance(report_evidence, dict):
        report_evidence = {}
    evidence = {
        "evidence_version": "1",
        "provider_adapter_id": adapter["provider_adapter_id"],
        "provider_target": adapter["provider_target"],
        "provider": details.get("provider") or report_evidence.get("provider") or adapter["provider_target"],
        "intended_model": request.get("intended_model") or "unknown",
        "effective_model": details.get("effective_model") or report_evidence.get("effective_model") or "unknown",
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "workflow_id": request["workflow_id"],
        "step_id": request["step_id"],
        "provider_request_id": details.get("provider_request_id") or f"provider-{stable_digest(request)[:16]}",
        "provider_session_id": details.get("provider_session_id") or report_evidence.get("provider_session_id") or f"session-{request['run_id']}",
        "transcript_path": request["transcript_path"],
        "evidence_path": request["evidence_path"],
        "duration_ms": int(details.get("duration_ms") or 0),
        "usage": details.get("usage") if isinstance(details.get("usage"), dict) else {},
        "outcome": outcome,
        "reason_class": details.get("reason") or outcome,
        "transport": adapter["transport"],
        "bridge_pattern": adapter.get("bridge_pattern"),
        "surface_metadata": adapter.get("surface_metadata") if isinstance(adapter.get("surface_metadata"), dict) else {},
        "stdout_sha256": details.get("stdout_sha256"),
        "stderr_sha256": details.get("stderr_sha256"),
        "transcript_sha256": details.get("transcript_sha256"),
        "attempt_id": request.get("attempt_id"),
        "exit_code": details.get("exit_code"),
        "timed_out": bool(details.get("timed_out", False)),
        "raw_transcript_policy": "signal_only_not_shared",
    }
    return {key: value for key, value in evidence.items() if value is not None}


def write_signal_transcript(state_root: Path, path: Path, payload: dict[str, Any]) -> None:
    secure_artifact_tree(state_root, path, "provider-evidence")
    private_atomic_write_json(
        state_root,
        path,
        {
            "transcript_signal_version": "1",
            "written_at": now_iso(),
            "payload": payload,
            "raw_content_policy": "signal_only_not_shared",
        },
    )


def write_live_transcript(
    state_root: Path,
    path: Path,
    *,
    stdout: bytes,
    stderr: bytes,
    outcome: str,
    exit_code: Any,
) -> dict[str, Any]:
    secure_artifact_tree(state_root, path, "provider-evidence")
    payload = {
        "provider_transcript_version": "1",
        "written_at": now_iso(),
        "outcome": outcome,
        "exit_code": exit_code,
        "encoding": "base64",
        "stdout_size_bytes": len(stdout),
        "stderr_size_bytes": len(stderr),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "raw_content_policy": "confined_provider_evidence_path_only",
    }
    private_atomic_write_json(
        state_root,
        path,
        payload,
    )
    return payload


def transition_failure(
    *,
    state_root: Path,
    run_id: str,
    run: dict[str, Any],
    reason_class: str,
    principal: dict[str, Any],
    artifact_refs: list[str],
) -> dict[str, Any]:
    current = str(run.get("run_state") or "")
    recoverable_reasons = {
        "provider_unavailable",
        "provider_timeout",
        "provider_nonzero_exit",
        "report_not_written",
    }
    if current == "step_queued":
        target = "waiting_human" if reason_class in recoverable_reasons else "failed"
    elif current == "waiting_provider":
        target = "waiting_human" if reason_class in recoverable_reasons else "failed"
    else:
        target = "failed"
    terminal_status = "blocked" if target == "failed" else None
    transition = run_lifecycle.transition_run(
        state_root,
        run_id,
        to_state=target,
        reason_class=reason_class,
        transition="run_provider",
        principal=principal,
        artifact_refs=artifact_refs,
        terminal_status=terminal_status,
        terminal_reason=reason_class if terminal_status else None,
        run=run,
    )
    return transition


def run_provider(
    *,
    state_root: Path,
    run_id: str,
    adapter_id: str = DEFAULT_ADAPTER_ID,
    timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    fake_provider_mode: str = "",
    live: bool = False,
    principal: dict[str, Any],
) -> dict[str, Any]:
    timeout_seconds = validate_provider_timeout(timeout_seconds)
    run_store.validate_artifact_id(run_id, "run_id")
    subject = {"run_id": run_id, "adapter_id": adapter_id}
    precheck_execution_principal(state_root=state_root, principal=principal, subject=subject)
    adapters = load_provider_adapters()
    adapter = adapters.get(adapter_id)
    if adapter is None:
        raise ProviderRunnerError(f"provider adapter not found: {adapter_id}")
    adapter_errors = validate_adapter_descriptor(adapter)
    if adapter_errors:
        return {"schema_version": 1, "decision": "blocked", "reason": "adapter_descriptor_invalid", "errors": adapter_errors}

    recovered_result: dict[str, Any] | None = None
    while True:
        with run_lock.hold_global_lock(
            state_root,
            operation="claim_provider_attempt",
            run_id=run_id,
            principal=principal,
        ):
            run = run_store.load_run(state_root, run_id)
            run_state = str(run.get("run_state") or "")
            if run_state == "waiting_provider":
                claimed_step_id = str(run.get("current_step") or "")
                claimed_order_path = work_order_path(state_root, run_id, claimed_step_id)
                try:
                    claimed_work_order = read_json(claimed_order_path)
                except ProviderRunnerError:
                    claimed_work_order = {}
                if runner_claim_is_live(claimed_work_order):
                    return {
                        "schema_version": 1,
                        "decision": "blocked",
                        "reason": "provider_in_flight",
                        "run_state": run_state,
                        "workflow_run": run,
                    }
                execution = run.get("provider_execution")
                recorded_adapter_id = (
                    str(execution.get("adapter_id") or "")
                    if isinstance(execution, dict)
                    else ""
                )
                if recorded_adapter_id:
                    recorded_adapter = adapters.get(recorded_adapter_id)
                    if recorded_adapter is None:
                        return {
                            "schema_version": 1,
                            "decision": "blocked",
                            "reason": "recorded_provider_adapter_unavailable",
                            "workflow_run": run,
                        }
                    adapter_id = recorded_adapter_id
                    adapter = recorded_adapter
                    subject["adapter_id"] = recorded_adapter_id
                execution_phase = execution.get("phase") if isinstance(execution, dict) else ""
                if execution_phase == "result_ready" or not execution_lease_valid(execution):
                    try:
                        recovered_result = recover_completed_provider_attempt(
                            state_root=state_root,
                            run=run,
                            adapter=adapter,
                        )
                    except ProviderRunnerError as exc:
                        return {
                            "schema_version": 1,
                            "decision": "blocked",
                            "reason": str(exc),
                            "workflow_run": run,
                        }
                    if recovered_result is not None:
                        attempt_id = str(recovered_result["attempt_id"])
                        request = recovered_result["request"]
                        request_path = recovered_result["request_path"]
                        report_path = recovered_result["report_path"]
                        evidence_path = recovered_result["evidence_path"]
                        transcript_path = recovered_result["transcript_path"]
                        evidence = recovered_result["evidence"]
                        recovered_outcome = str(recovered_result["outcome"])
                        if recovered_outcome == "ok":
                            run_store.store_run(
                                state_root, run, expected_current_state="waiting_provider"
                            )
                            break
                        details = recovered_result["details"]
                        reason_code = str(details.get("reason") or recovered_outcome)
                        fingerprint = "sha256:" + stable_digest(
                            {
                                "adapter_id": adapter_id,
                                "outcome": recovered_outcome,
                                "reason_code": reason_code,
                            }
                        )
                        retry_state = execution["retry"]
                        if retry_state.get("last_failure_fingerprint") == fingerprint:
                            retry_state["consecutive_failures"] = int(
                                retry_state.get("consecutive_failures") or 0
                            ) + 1
                        else:
                            retry_state["last_failure_fingerprint"] = fingerprint
                            retry_state["consecutive_failures"] = 1
                            retry_state["auto_retries_used"] = 0
                        non_retryable = reason_code in {
                            "auth_required",
                            "auth_or_quota",
                            "invalid_configuration",
                            "binary_binding_invalid",
                            "provider_authority_not_enforceable",
                            "live_execution_not_enabled",
                            "live_env_guard_missing",
                            "lease_lost",
                            PROVIDER_MODEL_MISMATCH,
                        }
                        retry = not non_retryable and int(
                            retry_state["auto_retries_used"]
                        ) < int(retry_state["max_auto_retries"])
                        artifacts = [
                            str(recovered_result["attempt_result_path"]),
                            str(evidence_path),
                            str(transcript_path),
                        ]
                        if retry:
                            retry_state["auto_retries_used"] = int(
                                retry_state["auto_retries_used"]
                            ) + 1
                            execution["phase"] = "retry_scheduled"
                            run_lifecycle.transition_run(
                                state_root,
                                run_id,
                                to_state="step_queued",
                                reason_class="provider_retry_scheduled",
                                transition="retry_provider",
                                principal=principal,
                                artifact_refs=artifacts,
                                run=run,
                            )
                            recovered_result = None
                            continue
                        execution["phase"] = "human_gate"
                        transition = run_lifecycle.transition_run(
                            state_root,
                            run_id,
                            to_state="waiting_human",
                            reason_class=recovered_outcome,
                            transition="run_provider",
                            principal=principal,
                            artifact_refs=artifacts,
                            run=run,
                        )
                        return {
                            "schema_version": 1,
                            "decision": "blocked",
                            "reason": recovered_outcome,
                            "adapter_request_path": str(request_path),
                            "attempt_result_path": str(
                                recovered_result["attempt_result_path"]
                            ),
                            "evidence_path": str(evidence_path),
                            "transcript_path": str(transcript_path),
                            "transition": transition,
                            "workflow_run": run,
                        }
                if execution_lease_valid(execution):
                    return {
                        "schema_version": 1,
                        "decision": "blocked",
                        "reason": "provider_in_flight",
                        "workflow_run": run,
                    }
                retry_allowed, journal_path = run_lifecycle.account_expired_provider_attempt(
                    state_root, run
                )
                if not retry_allowed:
                    transition = run_lifecycle.transition_run(
                        state_root,
                        run_id,
                        to_state="waiting_human",
                        reason_class="provider_retry_exhausted",
                        transition="retry_provider",
                        principal=principal,
                        artifact_refs=[str(journal_path)] if journal_path else [],
                        run=run,
                    )
                    return {
                        "schema_version": 1,
                        "decision": "blocked",
                        "reason": "provider_retry_exhausted",
                        "transition": transition,
                        "workflow_run": run,
                    }
                run_lifecycle.transition_run(
                    state_root,
                    run_id,
                    to_state="step_queued",
                    reason_class="provider_lease_expired",
                    transition="retry_provider",
                    principal=principal,
                    artifact_refs=[str(journal_path)] if journal_path else [],
                    run=run,
                )
                run_state = "step_queued"
            if run_state != "step_queued":
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "run_not_runnable",
                    "run_state": run_state,
                    "workflow_run": run,
                }
            step_id = str(run.get("current_step") or "")
            order_path = work_order_path(state_root, run_id, step_id)
            try:
                claimed_work_order = read_json(order_path)
            except ProviderRunnerError:
                claimed_work_order = {}
            if runner_claim_is_live(claimed_work_order):
                append_audit_event(
                    state_root=state_root,
                    event_type="run_provider",
                    principal=principal,
                    subject=subject,
                    outcome="blocked",
                    details={"reason": "provider_in_flight", "run_state": run_state},
                )
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "provider_in_flight",
                    "run_state": run_state,
                    "workflow_run": run,
                }
            try:
                work_order, order_digest, snapshot_path = scoped_worker_executor.verify_frozen_work_order(
                    state_root,
                    run_id=run_id,
                    step_id=step_id,
                    expected_run_states={"step_queued"},
                    expected_iteration=int(run["iteration"]),
                )
            except scoped_worker_executor.ScopedWorkerError as exc:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": exc.reason_class,
                    "workflow_run": run,
                }
            work_order_errors = validate_work_order_for_runner(work_order, state_root=state_root, run=run)
            if work_order_errors:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "work_order_not_provider_safe",
                    "errors": work_order_errors,
                }
            execution_binding: dict[str, Any] = {}
            if live and os.environ.get(LIVE_ENV_FLAG) == "1":
                try:
                    execution_binding = provider_adapters.resolve_execution_binding(adapter_id)
                except provider_adapters.AdapterConfigurationError as exc:
                    run["provider_execution"] = {
                        "execution_version": "1",
                        "step_id": step_id,
                        "adapter_id": adapter_id,
                        "work_order_digest": order_digest,
                        "adapter_request_digest": "sha256:" + "0" * 64,
                        "context_snapshot_digest": "sha256:" + "0" * 64,
                        "phase": "human_gate",
                        "attempt_number": 1,
                        "attempt_id": "provider-attempt-configuration",
                        "timeout_seconds": timeout_seconds,
                        "lease": {
                            "lease_id": "provider-lease-configuration",
                            "claimed_by": run_lifecycle.redacted_principal(principal),
                            "claimed_at": now_iso(),
                            "last_heartbeat_at": now_iso(),
                            "lease_expires_at": future_iso(1),
                        },
                        "retry": {
                            "last_failure_fingerprint": None,
                            "consecutive_failures": 1,
                            "auto_retries_used": 0,
                            "max_auto_retries": DEFAULT_MAX_AUTO_RETRIES,
                        },
                        "last_outcome": {"reason_class": str(exc), "recorded_at": now_iso()},
                    }
                    transition = run_lifecycle.transition_run(
                        state_root,
                        run_id,
                        to_state="waiting_human",
                        reason_class="provider_configuration_invalid",
                        transition="run_provider",
                        principal=principal,
                        run=run,
                    )
                    return {
                        "schema_version": 1,
                        "decision": "blocked",
                        "reason": "provider_configuration_invalid",
                        "configuration_reason": str(exc),
                        "transition": transition,
                        "workflow_run": run,
                    }
            if fake_provider_mode:
                previous_execution = (
                    run.get("provider_execution")
                    if isinstance(run.get("provider_execution"), dict)
                    else {}
                )
                attempt_number = int(previous_execution.get("attempt_number") or 0) + 1
                fake_binding = {
                    "run_id": run_id,
                    "step_id": step_id,
                    "adapter_id": adapter_id,
                    "attempt_number": attempt_number,
                }
                attempt_id = "provider-attempt-" + stable_digest(
                    {**fake_binding, "binding": "attempt"}
                )[:32]
                lease_id = "provider-lease-" + stable_digest(
                    {**fake_binding, "binding": "lease"}
                )[:32]
            else:
                attempt_id = "provider-attempt-" + uuid.uuid4().hex
                lease_id = "provider-lease-" + uuid.uuid4().hex
            try:
                request = adapter_request(
                    state_root=state_root,
                    run=run,
                    work_order=work_order,
                    adapter=adapter,
                    principal=principal,
                    work_order_digest=order_digest,
                    snapshot_path=snapshot_path,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    timeout_seconds=timeout_seconds,
                    execution_binding=execution_binding,
                )
            except ProviderRunnerError as exc:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": str(exc),
                    "workflow_run": run,
                }
            request_path = adapter_request_path(state_root, run_id, step_id, adapter["provider_adapter_id"])
            secure_directory(state_root, request_path.parent.parent)
            private_atomic_write_json(state_root, request_path, request)
            try:
                report_path, evidence_path, transcript_path = verified_request_artifact_paths(
                    state_root=state_root,
                    run_id=run_id,
                    step_id=step_id,
                    request=request,
                )
            except ProviderRunnerError as exc:
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": str(exc),
                    "workflow_run": run,
                }
            run["provider_execution"] = build_provider_execution(
                run=run,
                adapter_id=adapter_id,
                work_order_digest=order_digest,
                request=request,
                attempt_id=attempt_id,
                lease_id=lease_id,
                timeout_seconds=timeout_seconds,
                principal=principal,
            )
            run_lifecycle.transition_run(
                state_root,
                run_id,
                to_state="waiting_provider",
                reason_class="provider_claimed",
                transition="run_provider",
                principal=principal,
                artifact_refs=[str(order_path), str(snapshot_path), str(request_path)],
                run=run,
            )

        authorization_error = ""
        try:
            authorize_provider_dispatch(
                state_root=state_root,
                run_id=run_id,
                request=request,
                attempt_id=attempt_id,
                lease_id=lease_id,
                principal=principal,
            )
        except (ProviderRunnerError, scoped_worker_executor.ScopedWorkerError) as exc:
            authorization_error = getattr(exc, "reason_class", str(exc))

        if authorization_error:
            outcome, report, details = (
                "provider_unavailable",
                None,
                {"reason": authorization_error, "duration_ms": 0, "_live": False},
            )
        else:
            outcome, report, details = execute_provider(
                request=request,
                adapter=adapter,
                timeout_seconds=timeout_seconds,
                fake_provider_mode=fake_provider_mode,
                live=live,
                heartbeat=lambda: renew_provider_lease(
                    state_root=state_root,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    principal=principal,
                ),
            )

        if outcome == "ok" and report is None:
            outcome = "report_not_written"
            details = {**details, "reason": "report_not_written"}
        elif outcome == "ok":
            intended_model = str(request.get("intended_model") or "")
            effective_model = parsed_effective_model(details)
            details["intended_model"] = intended_model
            if effective_model:
                details["effective_model"] = effective_model
            if not intended_model or effective_model != intended_model:
                outcome = PROVIDER_MODEL_MISMATCH
                report = None
                details["reason"] = PROVIDER_MODEL_MISMATCH

        raw_stdout = details.pop("_raw_stdout", b"")
        raw_stderr = details.pop("_raw_stderr", b"")
        stdout = raw_stdout if isinstance(raw_stdout, bytes) else str(raw_stdout).encode("utf-8")
        stderr = raw_stderr if isinstance(raw_stderr, bytes) else str(raw_stderr).encode("utf-8")
        was_live = bool(details.pop("_live", False))
        attempt_result_path, attempt_transcript_path = provider_attempt_paths(state_root, run_id, attempt_id)
        if was_live:
            transcript_payload = write_live_transcript(
                state_root,
                attempt_transcript_path,
                stdout=stdout,
                stderr=stderr,
                outcome=outcome,
                exit_code=details.get("exit_code"),
            )
            details["stdout_sha256"] = transcript_payload["stdout_sha256"]
            details["stderr_sha256"] = transcript_payload["stderr_sha256"]
            details["transcript_sha256"] = file_sha256(attempt_transcript_path)
        else:
            write_signal_transcript(
                state_root,
                attempt_transcript_path,
                {"outcome": outcome, "details": details},
            )
            details.setdefault("transcript_sha256", file_sha256(attempt_transcript_path))
        journal = {
            "attempt_result_version": "1",
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "work_order_digest": request["work_order_digest"],
            "adapter_request_digest": request["adapter_request_digest"],
            "context_snapshot_digest": request["context_snapshot_digest"],
            "adapter_id": adapter_id,
            "outcome": outcome,
            "reason_class": details.get("reason") or outcome,
            "transcript_path": str(attempt_transcript_path),
            "transcript_sha256": file_sha256(attempt_transcript_path),
            "details": details,
            "report": report,
            "recorded_at": now_iso(),
        }
        private_atomic_write_json(state_root, attempt_result_path, journal)

        with run_lock.hold_global_lock(
            state_root,
            operation="finalize_provider_attempt",
            run_id=run_id,
            principal=principal,
        ):
            current_run = run_store.load_run(state_root, run_id)
            execution = current_run.get("provider_execution")
            if (
                current_run.get("run_state") != "waiting_provider"
                or not isinstance(execution, dict)
                or execution.get("attempt_id") != attempt_id
                or (execution.get("lease") or {}).get("lease_id") != lease_id
            ):
                return {
                    "schema_version": 1,
                    "decision": "blocked",
                    "reason": "provider_lease_lost",
                    "attempt_result_path": str(attempt_result_path),
                    "workflow_run": current_run,
                }
            if was_live:
                canonical_payload = write_live_transcript(
                    state_root,
                    transcript_path,
                    stdout=stdout,
                    stderr=stderr,
                    outcome=outcome,
                    exit_code=details.get("exit_code"),
                )
                details["stdout_sha256"] = canonical_payload["stdout_sha256"]
                details["stderr_sha256"] = canonical_payload["stderr_sha256"]
                details["transcript_sha256"] = file_sha256(transcript_path)
            else:
                write_signal_transcript(
                    state_root,
                    transcript_path,
                    {"outcome": outcome, "details": details},
                )
                details["transcript_sha256"] = file_sha256(transcript_path)
            evidence = normalized_evidence(
                request=request,
                adapter=adapter,
                report=report,
                outcome=outcome,
                details=details,
            )
            private_atomic_write_json(state_root, evidence_path, evidence)
            execution["last_outcome"] = {
                "reason_class": details.get("reason") or outcome,
                "recorded_at": now_iso(),
                "attempt_result_path": str(attempt_result_path),
                "evidence_path": str(evidence_path),
                "transcript_path": str(transcript_path),
            }
            if outcome == "ok" and report is not None:
                run_store.ensure_private_directory(report_path.parent)
                run_store.atomic_write_json(report_path, report)
                execution["phase"] = "result_ready"
                run_store.store_run(state_root, current_run, expected_current_state="waiting_provider")
                retry = False
                transition = None
            else:
                reason_code = str(details.get("reason") or outcome)
                fingerprint = "sha256:" + stable_digest(
                    {"adapter_id": adapter_id, "outcome": outcome, "reason_code": reason_code}
                )
                retry_state = execution["retry"]
                if retry_state.get("last_failure_fingerprint") == fingerprint:
                    retry_state["consecutive_failures"] = int(retry_state.get("consecutive_failures") or 0) + 1
                else:
                    retry_state["last_failure_fingerprint"] = fingerprint
                    retry_state["consecutive_failures"] = 1
                    retry_state["auto_retries_used"] = 0
                non_retryable = reason_code in {
                    "auth_required",
                    "auth_or_quota",
                    "invalid_configuration",
                    "binary_binding_invalid",
                    "provider_authority_not_enforceable",
                    "live_execution_not_enabled",
                    "live_env_guard_missing",
                    "lease_lost",
                    PROVIDER_MODEL_MISMATCH,
                } or bool(authorization_error)
                retry = not non_retryable and int(retry_state["auto_retries_used"]) < int(retry_state["max_auto_retries"])
                if retry:
                    retry_state["auto_retries_used"] = int(retry_state["auto_retries_used"]) + 1
                    execution["phase"] = "retry_scheduled"
                    transition = run_lifecycle.transition_run(
                        state_root,
                        run_id,
                        to_state="step_queued",
                        reason_class="provider_retry_scheduled",
                        transition="retry_provider",
                        principal=principal,
                        artifact_refs=[str(attempt_result_path), str(evidence_path), str(transcript_path)],
                        run=current_run,
                    )
                else:
                    execution["phase"] = "human_gate"
                    transition = run_lifecycle.transition_run(
                        state_root,
                        run_id,
                        to_state="waiting_human",
                        reason_class=outcome,
                        transition="run_provider",
                        principal=principal,
                        artifact_refs=[str(attempt_result_path), str(evidence_path), str(transcript_path)],
                        run=current_run,
                    )
        if retry:
            continue
        if outcome != "ok" or report is None:
            return {
                "schema_version": 1,
                "decision": "blocked",
                "reason": outcome,
                "adapter_request_path": str(request_path),
                "attempt_result_path": str(attempt_result_path),
                "evidence_path": str(evidence_path),
                "transcript_path": str(transcript_path),
                "transition": transition,
                "workflow_run": current_run,
            }
        break

    gate_payload = report_gate.gate_report(
        state_root,
        run_id,
        report_path_arg=str(report_path),
        principal=principal,
    )
    if gate_payload.get("decision") == "ok":
        with run_lock.hold_global_lock(
            state_root,
            operation="complete_provider_execution",
            run_id=run_id,
            principal=principal,
        ):
            completed_run = run_store.load_run(state_root, run_id)
            completed_execution = completed_run.get("provider_execution")
            if isinstance(completed_execution, dict) and completed_execution.get("attempt_id") == attempt_id:
                completed_execution["phase"] = "completed"
                run_store.store_run(
                    state_root,
                    completed_run,
                    expected_current_state=str(completed_run["run_state"]),
                )
                gate_payload["workflow_run"] = completed_run
    append_audit_event(
        state_root=state_root,
        event_type="run_provider",
        principal=principal,
        subject=subject,
        outcome="ok" if gate_payload.get("decision") == "ok" else "blocked",
        details={
            "adapter_request_path": str(request_path),
            "report_path": str(report_path),
            "report_sha256": file_sha256(report_path),
            "evidence_path": str(evidence_path),
            "evidence_sha256": file_sha256(evidence_path),
            "gate_outcome": gate_payload.get("outcome"),
        },
    )
    return {
        "schema_version": 1,
        "decision": gate_payload.get("decision", "ok"),
        "reason": gate_payload.get("reason", "report_valid"),
        "adapter_request_path": str(request_path),
        "report_path": str(report_path),
        "evidence_path": str(evidence_path),
        "transcript_path": str(transcript_path),
        "provider_evidence": evidence,
        "report_gate": gate_payload,
        "workflow_run": gate_payload.get("workflow_run"),
    }


def run_provider_step(
    *,
    state_root: Path,
    run_id: str,
    adapter_id: str = "",
    adapter: str = "",
    timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    fake_provider_mode: str = "success",
    live: bool = False,
    principal: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Compatibility hook for the offline E2E harness."""

    requested_adapter = adapter_id or adapter or DEFAULT_ADAPTER_ID
    if requested_adapter in {"fake_pass", "fake_provider", "fake"}:
        requested_adapter = DEFAULT_ADAPTER_ID
    actor = principal or {
        "principal_type": "harness_runner",
        "principal_id": "e2e-harness",
        "authn_method": "local_test",
    }
    return run_provider(
        state_root=state_root,
        run_id=run_id,
        adapter_id=requested_adapter,
        timeout_seconds=timeout_seconds,
        fake_provider_mode=fake_provider_mode or "success",
        live=live,
        principal=actor,
    )


def run_step(**kwargs: Any) -> dict[str, Any]:
    return run_provider_step(**kwargs)
