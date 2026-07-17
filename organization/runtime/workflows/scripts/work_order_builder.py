#!/usr/bin/env python3
"""Work-order construction, validation, and immutable step snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import run_store
import safe_paths

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
WORK_ORDER_SCHEMA_PATH = WORKFLOW_ROOT / "schemas/work-order.schema.json"

REQUIRED_WORK_ORDER_FIELDS = [
    "work_order_version",
    "task_id",
    "request_id",
    "run_id",
    "workflow_id",
    "step_id",
    "from_role",
    "to_role",
    "assignment_role",
    "instruction",
    "expected_output",
    "context_refs",
    "context_scope",
    "permission_mode",
    "external_provider_allowed",
    "report_path",
    "policy_digest",
    "requester",
    "activation_scope",
    "work_order_authority",
]

ASSIGNMENT_ROLES = {"implementer", "reviewer", "qa", "approver", "observer", "publisher"}
PERMISSION_MODES = {"readonly", "edit", "full"}
FORBIDDEN_RAW_TRANSCRIPT_KEYS = {"prompt", "raw_prompt", "raw_transcript", "raw_transcript_text", "transcript"}
LAUNCH_SESSION_FIELDS = {
    "launch_session_version", "session_id", "deployment_id", "profile_id",
    "principal_id", "workspace_id", "subject_pid", "process_start_token",
    "native_realpath", "native_digest", "profile_realpath", "profile_digest",
    "launch_argv_digest", "checkout_realpath", "checkout_identity_digest",
    "issued_at", "valid_until", "status", "session_kind",
    "commissioning_launch_reference", "commissioning_launch_digest", "supervisor_pid",
    "supervisor_start_token", "record_reference", "record_digest",
}
PROJECTION_BINDING_FIELDS = {
    "request_id",
    "task_id",
    "owner_principal_digest",
    "checkout_identity_digest",
}
_WORK_ORDER_SCHEMA_CACHE: dict[str, Any] | None = None


class WorkOrderError(RuntimeError):
    """Typed work-order failure."""


def _state_artifact_path(state_root: Path, namespace: str, *components: str) -> Path:
    try:
        return safe_paths.state_artifact_path(state_root, namespace, *components)
    except safe_paths.SafePathError as exc:
        raise WorkOrderError("state_artifact_path_escape") from exc


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def _normalized_owner_principal(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WorkOrderError("projection_binding_owner_invalid")
    owner = {
        "principal_type": str(value.get("principal_type") or ""),
        "principal_id": str(value.get("principal_id") or ""),
        "authn_method": str(value.get("authn_method") or ""),
    }
    if not all(owner.values()):
        raise WorkOrderError("projection_binding_owner_invalid")
    return owner


def build_projection_binding(
    *,
    request_id: Any,
    task_id: Any,
    owner_principal: Any,
    checkout_identity_digest: Any,
) -> dict[str, str]:
    """Build the exact non-authority binding used to filter bridge summaries."""

    try:
        normalized_request_id = run_store.validate_artifact_id(
            str(request_id or ""), "request_id"
        )
        normalized_task_id = run_store.validate_artifact_id(
            str(task_id or ""), "task_id"
        )
    except run_store.RunStoreError as exc:
        raise WorkOrderError("projection_binding_identifier_invalid") from exc
    owner = _normalized_owner_principal(owner_principal)
    checkout_digest = str(checkout_identity_digest or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", checkout_digest):
        raise WorkOrderError("projection_binding_checkout_invalid")
    return {
        "request_id": normalized_request_id,
        "task_id": normalized_task_id,
        "owner_principal_digest": sha256_digest(owner),
        "checkout_identity_digest": checkout_digest,
    }


def projection_binding_from_request_record(
    request_record: dict[str, Any],
) -> dict[str, str]:
    if not isinstance(request_record, dict):
        raise WorkOrderError("projection_binding_request_invalid")
    return build_projection_binding(
        request_id=request_record.get("request_id"),
        task_id=request_record.get("task_id"),
        owner_principal=request_record.get("owner_principal"),
        checkout_identity_digest=request_record.get("checkout_identity_digest"),
    )


def validate_projection_binding(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != PROJECTION_BINDING_FIELDS:
        raise WorkOrderError("projection_binding_invalid")
    try:
        request_id = run_store.validate_artifact_id(
            str(value.get("request_id") or ""), "request_id"
        )
        task_id = run_store.validate_artifact_id(
            str(value.get("task_id") or ""), "task_id"
        )
    except run_store.RunStoreError as exc:
        raise WorkOrderError("projection_binding_invalid") from exc
    owner_digest = str(value.get("owner_principal_digest") or "")
    checkout_digest = str(value.get("checkout_identity_digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", owner_digest) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", checkout_digest
    ):
        raise WorkOrderError("projection_binding_invalid")
    return {
        "request_id": request_id,
        "task_id": task_id,
        "owner_principal_digest": owner_digest,
        "checkout_identity_digest": checkout_digest,
    }


def normalize_launch_session_identity(value: Any) -> dict[str, Any]:
    try:
        import codex_main_agent_supervisor as supervisor

        normalized = supervisor.validate_session_record_shape(value)
    except Exception as exc:
        raise WorkOrderError("frontend_launch_session_invalid") from exc
    if set(normalized) != LAUNCH_SESSION_FIELDS:
        raise WorkOrderError("frontend_launch_session_invalid")
    return json.loads(json.dumps(dict(normalized)))


def work_order_schema() -> dict[str, Any]:
    global _WORK_ORDER_SCHEMA_CACHE
    if _WORK_ORDER_SCHEMA_CACHE is None:
        _WORK_ORDER_SCHEMA_CACHE = json.loads(WORK_ORDER_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _WORK_ORDER_SCHEMA_CACHE


def _type_matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _json_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent != "$" else f"$.{key}"


def _validate_schema_fragment(
    value: Any,
    schema: dict[str, Any],
    path: str,
    *,
    root_schema: dict[str, Any] | None = None,
) -> list[str]:
    root = schema if root_schema is None else root_schema
    errors: list[str] = []
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if not reference.startswith("#/"):
            return [f"schema:{path}:unsupported_ref"]
        resolved: Any = root
        try:
            for component in reference[2:].split("/"):
                resolved = resolved[component.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return [f"schema:{path}:unresolved_ref"]
        if not isinstance(resolved, dict):
            return [f"schema:{path}:unresolved_ref"]
        errors.extend(
            _validate_schema_fragment(value, resolved, path, root_schema=root)
        )
    expected_type = schema.get("type")
    if expected_type is not None and not _type_matches(value, expected_type):
        errors.append(f"schema:{path}:type")
        return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"schema:{path}:const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"schema:{path}:enum")
    if "pattern" in schema and isinstance(value, str) and not re.search(str(schema["pattern"]), value):
        errors.append(f"schema:{path}:pattern")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            errors.append(f"schema:{path}:minimum")
    if "maximum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > schema["maximum"]:
            errors.append(f"schema:{path}:maximum")

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for field in required:
                if field not in value:
                    errors.append(f"schema:{_json_path(path, str(field))}:required")

        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(
                    _validate_schema_fragment(
                        value[key],
                        child_schema,
                        _json_path(path, str(key)),
                        root_schema=root,
                    )
                )
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                errors.append(f"schema:{_json_path(path, str(key))}:additional_property")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"schema:{path}:min_items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _validate_schema_fragment(
                        item,
                        item_schema,
                        f"{path}[{index}]",
                        root_schema=root,
                    )
                )

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        branch_errors = [
            _validate_schema_fragment(value, branch, path, root_schema=root)
            for branch in schema["anyOf"]
            if isinstance(branch, dict)
        ]
        if branch_errors and all(branch for branch in branch_errors):
            errors.append(f"schema:{path}:any_of")

    for branch in schema.get("allOf", []):
        if not isinstance(branch, dict):
            continue
        condition = branch.get("if")
        if isinstance(condition, dict):
            applies = not _validate_schema_fragment(
                value, condition, path, root_schema=root
            )
            if applies and isinstance(branch.get("then"), dict):
                errors.extend(
                    _validate_schema_fragment(
                        value, branch["then"], path, root_schema=root
                    )
                )
            continue
        errors.extend(
            _validate_schema_fragment(value, branch, path, root_schema=root)
        )

    return errors


def validate_against_work_order_schema(work_order: dict[str, Any]) -> list[str]:
    schema = work_order_schema()
    return _validate_schema_fragment(work_order, schema, "$", root_schema=schema)


def _forbidden_raw_transcript_paths(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = _json_path(path, str(key))
            if key in FORBIDDEN_RAW_TRANSCRIPT_KEYS:
                errors.append(f"forbidden_raw_transcript_field:{child_path}")
            else:
                errors.extend(_forbidden_raw_transcript_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_forbidden_raw_transcript_paths(child, f"{path}[{index}]"))
    return errors


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def report_path(state_root: Path, run_id: str, step_id: str) -> Path:
    return _state_artifact_path(
        state_root,
        "reports",
        run_store.validate_artifact_id(run_id, "run_id"),
        f"{run_store.validate_artifact_id(step_id, 'step_id')}-external-review-report.json",
    )


def snapshot_path(state_root: Path, run_id: str, step_id: str, iteration: int) -> Path:
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
        raise WorkOrderError("iteration must be an integer >= 1")
    safe_run_id = run_store.validate_artifact_id(run_id, "run_id")
    safe_step_id = run_store.validate_artifact_id(step_id, "step_id")
    return _state_artifact_path(
        state_root,
        "work-orders",
        safe_run_id,
        f"{safe_step_id}-snapshot-{iteration}.json",
    )


def _normalized_context_ref(item: dict[str, Any]) -> dict[str, Any]:
    ref = {
        "type": str(item.get("type") or "repo_file"),
        "value": str(item.get("path") or item.get("value") or ""),
    }
    if "size_bytes" in item:
        ref["size_bytes"] = item["size_bytes"]
    if "digest" in item:
        ref["digest"] = item["digest"]
    return ref


def _requested_context_mode(run: dict[str, Any], request_record: dict[str, Any]) -> str:
    classification = request_record.get("classification") if isinstance(request_record.get("classification"), dict) else {}
    activation = run.get("activation") if isinstance(run.get("activation"), dict) else {}
    activation_context = activation.get("context_scope") if isinstance(activation.get("context_scope"), dict) else {}
    return str(classification.get("context_scope") or activation_context.get("mode") or "refs_only")


def _context_scope_for_step(
    *,
    run: dict[str, Any],
    request_record: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, str]:
    requested_mode = _requested_context_mode(run, request_record)
    step_scope = step.get("context_scope") if isinstance(step.get("context_scope"), dict) else {}
    allowed_modes = step_scope.get("allowed_modes") if isinstance(step_scope.get("allowed_modes"), list) else []
    allowed = [str(item) for item in allowed_modes if isinstance(item, str) and item]
    selected = requested_mode if requested_mode in allowed else (allowed[0] if allowed else requested_mode)
    scope = {
        "mode": selected,
        "raw_transcript_sharing": "forbidden",
    }
    if selected != requested_mode:
        scope["context_mode_downgraded_from"] = requested_mode
    return scope


def build_work_order(
    *,
    run: dict[str, Any],
    request_record: dict[str, Any],
    template: dict[str, Any],
    step: dict[str, Any],
    issuer_principal_redacted: dict[str, Any],
    resolved_refs: list[dict[str, Any]],
    policy_digest_value: str,
    signature: dict[str, Any] | None,
    report_path_value: str,
    worker_execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step_id = str(step["id"])
    provider_route = step.get("provider_route") if isinstance(step.get("provider_route"), dict) else {}
    external_provider_allowed = provider_route.get("adapter_kind") == "external_provider"
    context_refs = [_normalized_context_ref(item) for item in resolved_refs if isinstance(item, dict)]
    work_order = {
        "work_order_version": "1",
        "task_id": run["task_id"],
        "request_id": run["request_id"],
        "run_id": run["run_id"],
        "workflow_id": run["workflow_id"],
        "step_id": step_id,
        "from_role": "frontdoor",
        "to_role": str(step["role"]),
        "assignment_role": str(step["assignment_role"]),
        "instruction": (
            f"{template['purpose']} Step '{step_id}' ({step['assignment_role']}): "
            f"follow the input work order contract and produce {step['output_contract']}."
        ),
        "expected_output": str(step["output_contract"]),
        "context_refs": context_refs,
        "context_scope": _context_scope_for_step(run=run, request_record=request_record, step=step),
        "permission_mode": str(step["permission_mode"]),
        "external_provider_allowed": external_provider_allowed,
        "report_path": report_path_value,
        "policy_digest": policy_digest_value,
        "requester": run.get("requester") or {"frontdoor": "manual"},
        "activation_scope": run["activation"]["activation_scope"],
        "work_order_authority": {
            "issuer_principal": issuer_principal_redacted,
            "signature": signature,
            "runner_claim": {
                "claim_state": "unclaimed",
                "lease_expires_at": None,
            },
        },
    }
    owner_principal = request_record.get("owner_principal")
    checkout_identity_digest = request_record.get("checkout_identity_digest")
    if owner_principal is not None or checkout_identity_digest is not None:
        if not isinstance(owner_principal, dict) or not isinstance(checkout_identity_digest, str):
            raise WorkOrderError("frontend_request_binding_incomplete")
        work_order["frontend_request_binding"] = {
            "owner_principal": {
                "principal_type": str(owner_principal.get("principal_type") or ""),
                "principal_id": str(owner_principal.get("principal_id") or ""),
                "authn_method": str(owner_principal.get("authn_method") or ""),
            },
            "checkout_identity_digest": checkout_identity_digest,
        }
        request_binding_source = {
            **request_record,
            "request_id": request_record.get("request_id", run.get("request_id")),
            "task_id": request_record.get("task_id", run.get("task_id")),
        }
        work_order["projection_binding"] = projection_binding_from_request_record(
            request_binding_source
        )
    launch_session_identity = request_record.get("launch_session_identity")
    launch_session_digest = request_record.get("launch_session_digest")
    if launch_session_identity is not None or launch_session_digest:
        if not isinstance(launch_session_digest, str):
            raise WorkOrderError("frontend_launch_session_incomplete")
        normalized_session = normalize_launch_session_identity(launch_session_identity)
        if launch_session_digest != normalized_session["record_digest"]:
            raise WorkOrderError("frontend_launch_session_digest_mismatch")
        if (
            not isinstance(owner_principal, dict)
            or normalized_session["principal_id"] != owner_principal.get("principal_id")
            or normalized_session["checkout_identity_digest"] != checkout_identity_digest
        ):
            raise WorkOrderError("frontend_launch_session_authority_mismatch")
        work_order["frontend_launch_session_binding"] = {
            "launch_session_identity": normalized_session,
            "launch_session_digest": launch_session_digest,
        }
    if worker_execution_plan is not None:
        plan_projection_binding = (
            worker_execution_plan.get("projection_binding")
            if isinstance(worker_execution_plan, dict)
            else None
        )
        if plan_projection_binding != work_order.get("projection_binding"):
            raise WorkOrderError("worker_projection_binding_mismatch")
        work_order["worker_execution_plan"] = worker_execution_plan
    return work_order


def validate_work_order(
    work_order: dict[str, Any],
    *,
    template: dict[str, Any],
    step: dict[str, Any],
    state_root: Path,
    run: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    missing = [field for field in REQUIRED_WORK_ORDER_FIELDS if field not in work_order]
    errors.extend(f"missing_required_field:{field}" for field in missing)
    if missing:
        return errors
    errors.extend(validate_against_work_order_schema(work_order))
    errors.extend(_forbidden_raw_transcript_paths(work_order))

    if work_order.get("work_order_version") != "1":
        errors.append("work_order_version must be '1'")
    if work_order.get("assignment_role") not in ASSIGNMENT_ROLES:
        errors.append("assignment_role unsupported")
    if work_order.get("permission_mode") not in PERMISSION_MODES:
        errors.append("permission_mode unsupported")
    if work_order.get("permission_mode") != step.get("permission_mode"):
        errors.append("permission_mode must match template step")

    context_refs = work_order.get("context_refs")
    if not isinstance(context_refs, list) or not context_refs:
        errors.append("context_refs must be non-empty")
    else:
        for index, ref in enumerate(context_refs):
            if not isinstance(ref, dict):
                errors.append(f"context_refs[{index}] must be object")
                continue
            if not ref.get("type") or not ref.get("value"):
                errors.append(f"context_refs[{index}] missing type or value")
            if "digest" in ref and not str(ref.get("digest")).startswith("sha256:"):
                errors.append(f"context_refs[{index}].digest must start with sha256:")

    context_scope = work_order.get("context_scope")
    if not isinstance(context_scope, dict):
        errors.append("context_scope must be object")
    elif context_scope.get("raw_transcript_sharing") != "forbidden":
        errors.append("context_scope.raw_transcript_sharing must be forbidden")

    report_value = work_order.get("report_path")
    if not isinstance(report_value, str) or not report_value:
        errors.append("report_path must be non-empty")
    elif not path_is_within(Path(report_value), state_root / "reports"):
        errors.append("report_path must stay under reports")

    if not str(work_order.get("policy_digest") or "").startswith("sha256:"):
        errors.append("policy_digest must start with sha256:")

    authority = work_order.get("work_order_authority")
    if not isinstance(authority, dict):
        errors.append("work_order_authority must be object")
    else:
        issuer = authority.get("issuer_principal")
        if not isinstance(issuer, dict):
            errors.append("work_order_authority.issuer_principal must be object")
        elif issuer.get("principal_type") == "main_agent_bridge":
            errors.append("bridge principal cannot issue work orders")
        signature = authority.get("signature")
        if not isinstance(signature, dict) or not str(signature.get("signature") or "").startswith("sha256:"):
            errors.append("work_order_authority.signature must be present")
        runner_claim = authority.get("runner_claim")
        if not isinstance(runner_claim, dict):
            errors.append("work_order_authority.runner_claim must be object")
        else:
            if runner_claim.get("claim_state") != "unclaimed":
                errors.append("work_order_authority.runner_claim.claim_state must be unclaimed")
            if "lease_expires_at" not in runner_claim:
                errors.append("work_order_authority.runner_claim.lease_expires_at must be present")

    launch_binding = work_order.get("frontend_launch_session_binding")
    if launch_binding is not None:
        if not isinstance(launch_binding, dict) or set(launch_binding) != {
            "launch_session_identity",
            "launch_session_digest",
        }:
            errors.append("frontend_launch_session_binding must be exact object")
        else:
            try:
                normalized_session = normalize_launch_session_identity(
                    launch_binding.get("launch_session_identity")
                )
            except WorkOrderError as exc:
                errors.append(str(exc))
            else:
                if launch_binding.get("launch_session_digest") != normalized_session["record_digest"]:
                    errors.append("frontend_launch_session_digest_mismatch")
                frontend_binding = work_order.get("frontend_request_binding")
                if not isinstance(frontend_binding, dict):
                    errors.append("frontend_launch_session_requires_request_binding")
                else:
                    owner = frontend_binding.get("owner_principal")
                    if (
                        not isinstance(owner, dict)
                        or normalized_session["principal_id"] != owner.get("principal_id")
                        or normalized_session["checkout_identity_digest"]
                        != frontend_binding.get("checkout_identity_digest")
                    ):
                        errors.append("frontend_launch_session_authority_mismatch")

    frontend_binding = work_order.get("frontend_request_binding")
    projection_binding = work_order.get("projection_binding")
    if frontend_binding is not None:
        try:
            expected_projection_binding = build_projection_binding(
                request_id=work_order.get("request_id"),
                task_id=work_order.get("task_id"),
                owner_principal=(
                    frontend_binding.get("owner_principal")
                    if isinstance(frontend_binding, dict)
                    else None
                ),
                checkout_identity_digest=(
                    frontend_binding.get("checkout_identity_digest")
                    if isinstance(frontend_binding, dict)
                    else None
                ),
            )
            normalized_projection_binding = validate_projection_binding(
                projection_binding
            )
        except WorkOrderError as exc:
            errors.append(str(exc))
        else:
            if normalized_projection_binding != expected_projection_binding:
                errors.append("projection_binding_mismatch")
    elif projection_binding is not None:
        errors.append("projection_binding_requires_frontend_request_binding")

    worker_plan = work_order.get("worker_execution_plan")
    if worker_plan is not None and (
        not isinstance(worker_plan, dict)
        or worker_plan.get("projection_binding") != projection_binding
    ):
        errors.append("worker_projection_binding_mismatch")

    workflow_id = work_order.get("workflow_id")
    step_id = work_order.get("step_id")
    if step_id != step.get("id"):
        errors.append("step_id must match template step")
    if workflow_id != template.get("workflow_id"):
        errors.append("workflow_id must match template")
    if run is not None:
        for field in ("task_id", "request_id", "run_id"):
            if str(work_order.get(field) or "") != str(run.get(field) or ""):
                errors.append(f"{field} must match current run")
        expected_report_path = report_path(
            state_root,
            str(run.get("run_id") or ""),
            str(step.get("id") or ""),
        )
        if isinstance(report_value, str) and report_value:
            try:
                if Path(report_value).expanduser().resolve() != expected_report_path.resolve():
                    errors.append("report_path must match current run report path")
            except OSError:
                errors.append("report_path must match current run report path")

    if workflow_id == "single_step_external_review":
        expected = {
            "step_id": "review",
            "assignment_role": "reviewer",
            "expected_output": "external_review_report",
            "permission_mode": "readonly",
            "external_provider_allowed": True,
        }
        for field, value in expected.items():
            if work_order.get(field) != value:
                errors.append(f"{field} must be {value!r}")
        activation_scope = work_order.get("activation_scope") if isinstance(work_order.get("activation_scope"), dict) else {}
        if activation_scope.get("step_budget") != 1:
            errors.append("activation_scope.step_budget must be 1")
        allowed_ops = activation_scope.get("allowed_ops") if isinstance(activation_scope.get("allowed_ops"), dict) else {}
        for op in ("edit", "commit", "push", "network"):
            if allowed_ops.get(op) is not False:
                errors.append(f"activation_scope.allowed_ops.{op} must be false")

    return errors


def read_existing_snapshot(path: Path) -> dict[str, Any]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkOrderError("step_snapshot_invalid") from exc
    if not isinstance(existing, dict):
        raise WorkOrderError("step_snapshot_invalid")
    return existing


def freeze_step_snapshot(state_root: Path, work_order: dict[str, Any], *, iteration: int) -> Path:
    run_id = str(work_order.get("run_id") or "")
    step_id = str(work_order.get("step_id") or "")
    path = snapshot_path(state_root, run_id, step_id, iteration)
    digest = sha256_digest(work_order)
    if path.exists():
        existing = read_existing_snapshot(path)
        if existing.get("work_order_digest") != digest:
            raise WorkOrderError("step_snapshot_conflict")
        return path
    payload = {
        "snapshot_version": "1",
        "frozen_at": now_iso(),
        "run_id": run_id,
        "step_id": step_id,
        "iteration": iteration,
        "work_order_digest": digest,
        "work_order": work_order,
        "activation_scope": work_order.get("activation_scope"),
        "context_refs": work_order.get("context_refs"),
        "policy_digest": work_order.get("policy_digest"),
    }
    run_store.atomic_write_json(path, payload)
    return path
