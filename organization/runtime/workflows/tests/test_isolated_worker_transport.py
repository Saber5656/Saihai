#!/usr/bin/env python3
"""Contract tests for the isolated worker policy-domain transport."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = WORKFLOW_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import isolated_worker_transport as transport  # noqa: E402


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
BASE_REVISION = "1" * 40


def generation() -> dict:
    return {
        "profile_id": "codex-scoped-worker",
        "generation_id": "generation-isolated-001",
        "attestation_digest": SHA_A,
        "bindings_digest": SHA_B,
        "evidence_set_digest": SHA_C,
    }


def input_envelope() -> dict:
    return {
        "transport_version": "1",
        "message_type": "approved_work_order",
        "transfer_id": "xfer-" + "1" * 24,
        "execution_id": "exec-" + "2" * 24,
        "issued_at": "2026-07-17T00:00:00Z",
        "expires_at": "2026-07-17T00:05:00Z",
        "task_id": "TSK-isolated-worker",
        "request_id": "req-isolated-worker",
        "run_id": "run-isolated-worker",
        "step_id": "implement",
        "work_order_digest": SHA_D,
        "authority": {
            "approval_state": "approved",
            "capability_digest": SHA_E,
            "assurance_binding_digest": SHA_A,
            "max_execution_count": 1,
        },
        "managed_worker_generation": generation(),
        "execution_policy": {
            "permission_mode": "edit",
            "allowed_operations": [
                "read_context",
                "write_result",
                "edit_worktree",
                "run_tests",
            ],
            "allowed_paths": ["."],
            "network_egress": "denied",
            "provider_dispatch": "denied",
            "credential_access": "denied",
            "host_write_back": "denied",
        },
        "approved_work_order": {
            "artifact_id": "approved-work-order",
            "media_type": "application/vnd.saihai.work-order+json",
            "sha256": SHA_D,
            "size_bytes": 4096,
        },
        "repository_snapshot": {
            "artifact_id": "repository-snapshot",
            "media_type": "application/vnd.saihai.repository-snapshot+tar",
            "sha256": SHA_B,
            "size_bytes": 8192,
            "repo_full_name": "Saber5656/Saihai",
            "base_revision": BASE_REVISION,
            "git_metadata": "excluded",
        },
        "result_contract": {
            "artifact_id": "isolated-worker-result-schema",
            "media_type": "application/schema+json",
            "sha256": SHA_C,
            "size_bytes": 2048,
        },
    }


def result_envelope() -> dict:
    patch_bytes = b"diff --git a/README.md b/README.md\n"
    return {
        "transport_version": "1",
        "message_type": "worker_result",
        "transfer_id": "xfer-" + "1" * 24,
        "execution_id": "exec-" + "2" * 24,
        "issued_at": "2026-07-17T00:00:00Z",
        "expires_at": "2026-07-17T00:05:00Z",
        "task_id": "TSK-isolated-worker",
        "request_id": "req-isolated-worker",
        "run_id": "run-isolated-worker",
        "step_id": "implement",
        "work_order_digest": SHA_D,
        "managed_worker_generation": generation(),
        "base_revision": BASE_REVISION,
        "worker_result": {
            "result_version": "1",
            "status": "completed",
            "summary": "Bounded change completed.",
            "changed_paths": ["README.md"],
            "tests": [
                {"name": "contract", "status": "pass", "summary": "passed"}
            ],
            "self_reported_evidence": [
                {"kind": "diff", "label": "captured patch", "status": "pass", "sha256": SHA_E}
            ],
        },
        "patch": {
            "artifact_id": "worktree-patch",
            "media_type": "text/x-diff",
            "encoding": "base64",
            "sha256": "sha256:" + hashlib.sha256(patch_bytes).hexdigest(),
            "size_bytes": len(patch_bytes),
            "content_base64": base64.b64encode(patch_bytes).decode("ascii"),
        },
        "worker_self_reported_execution_evidence": {
            "input_digest_verified": True,
            "result_schema_verified": True,
            "network_egress_observed": False,
            "credential_material_observed": False,
            "host_mounts_observed": False,
        },
    }


def expect_reason(reason: str, callback) -> transport.IsolatedWorkerTransportError:
    try:
        callback()
    except transport.IsolatedWorkerTransportError as exc:
        assert exc.reason == reason, exc.errors
        return exc
    raise AssertionError(f"expected IsolatedWorkerTransportError({reason})")


def test_valid_typed_exchange_is_bound_and_path_free() -> None:
    incoming, outgoing = transport.validate_exchange(
        input_envelope(), result_envelope()
    )
    assert incoming["authority"]["max_execution_count"] == 1
    assert incoming["execution_id"] == outgoing["execution_id"]
    assert outgoing["worker_self_reported_execution_evidence"]["host_mounts_observed"] is False
    assert outgoing["worker_result"]["changed_paths"] == ["README.md"]


def test_regular_all_of_and_unknown_schema_keywords_fail_closed() -> None:
    changed = input_envelope()
    changed["approved_work_order"]["artifact_id"] = "worker-chosen-order"
    exc = expect_reason(
        "isolated_worker_input_invalid",
        lambda: transport.validate_input_envelope(changed),
    )
    assert "schema:$.approved_work_order.artifact_id:const" in exc.errors

    errors = transport._validate_schema_value(
        "value",
        {
            "type": "string",
            "oneOf": [{"const": "value"}, {"const": "other"}],
        },
    )
    assert any("unsupported_keyword" in error for error in errors), errors


def test_input_rejects_host_and_vault_location_fields() -> None:
    for field in ("host_path", "host_state_root", "vault_path", "write_target"):
        changed = input_envelope()
        changed["repository_snapshot"][field] = (
            "/Users/example/Library/Agents-Vault" if "vault" in field else "/host/state"
        )
        exc = expect_reason(
            "isolated_worker_input_invalid",
            lambda changed=changed: transport.validate_input_envelope(changed),
        )
        assert any("forbidden_location_field" in item for item in exc.errors), exc.errors


def test_worker_result_cannot_name_host_writeback_targets() -> None:
    for field, target in (
        ("path", "/Users/example/Library/Agents-Vault/task.md"),
        ("vault_path", "/primary-host/Agents-Vault"),
        ("state_root", "/Library/Application Support/Saihai/Assurance"),
        ("file_uri", "file:///primary-host/state/runs/run.json"),
    ):
        changed = result_envelope()
        changed["worker_result"]["self_reported_evidence"][0][field] = target
        exc = expect_reason(
            "isolated_worker_result_invalid",
            lambda changed=changed: transport.validate_result_envelope(changed),
        )
        assert any("forbidden_location_field" in item for item in exc.errors), exc.errors


def test_worker_changed_paths_are_guest_relative_only() -> None:
    for changed_path in (
        "/primary-host/Agents-Vault/task.md",
        "../state/runs/run.json",
        "nested/../../state",
        "nested\\escape",
        ".",
    ):
        changed = result_envelope()
        changed["worker_result"]["changed_paths"] = [changed_path]
        exc = expect_reason(
            "isolated_worker_result_invalid",
            lambda changed=changed: transport.validate_result_envelope(changed),
        )
        assert any("changed_path_invalid" in item for item in exc.errors), exc.errors


def test_patch_and_exchange_bindings_fail_closed() -> None:
    changed = result_envelope()
    changed["patch"]["sha256"] = SHA_A
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(changed),
    )
    assert "patch_digest_mismatch" in exc.errors

    for field in ("transfer_id", "execution_id", "work_order_digest", "run_id"):
        changed = result_envelope()
        changed[field] = (
            "xfer-" + "9" * 24
            if field == "transfer_id"
            else (
                "exec-" + "9" * 24
                if field == "execution_id"
                else (SHA_A if field == "work_order_digest" else "run-other")
            )
        )
        exc = expect_reason(
            "isolated_worker_exchange_invalid",
            lambda changed=changed: transport.validate_exchange(input_envelope(), changed),
        )
        assert f"exchange_binding_mismatch:{field}" in exc.errors


def test_execution_id_and_freshness_window_are_required_and_bound() -> None:
    for factory, validator in (
        (input_envelope, transport.validate_input_envelope),
        (result_envelope, transport.validate_result_envelope),
    ):
        changed = factory()
        del changed["execution_id"]
        exc = expect_reason(
            "isolated_worker_input_invalid"
            if factory is input_envelope
            else "isolated_worker_result_invalid",
            lambda changed=changed, validator=validator: validator(changed),
        )
        assert any("execution_id:required" in error for error in exc.errors), exc.errors

    for field, value, expected in (
        ("issued_at", "2026-02-30T00:00:00Z", "freshness_window_invalid:issued_at"),
        ("expires_at", "2026-07-16T23:59:59Z", "freshness_window_invalid:ordering"),
        ("expires_at", "2026-07-17T00:05:01Z", "freshness_window_invalid:too_long"),
    ):
        changed = input_envelope()
        changed[field] = value
        exc = expect_reason(
            "isolated_worker_input_invalid",
            lambda changed=changed: transport.validate_input_envelope(changed),
        )
        assert expected in exc.errors, exc.errors

    changed = result_envelope()
    changed["expires_at"] = "2026-07-17T00:04:59Z"
    exc = expect_reason(
        "isolated_worker_exchange_invalid",
        lambda: transport.validate_exchange(input_envelope(), changed),
    )
    assert "exchange_binding_mismatch:expires_at" in exc.errors


def _bind_input_artifacts(envelope: dict, artifacts: dict[str, bytes]) -> None:
    for field in transport.INPUT_ARTIFACT_FIELDS:
        descriptor = envelope[field]
        raw = artifacts[descriptor["artifact_id"]]
        descriptor["size_bytes"] = len(raw)
        descriptor["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    envelope["work_order_digest"] = envelope["approved_work_order"]["sha256"]


def test_input_artifact_bytes_are_size_digest_and_limit_checked() -> None:
    artifacts = {
        "approved-work-order": b'{"work_order_version":"1"}',
        "repository-snapshot": b"safe archive bytes",
        "isolated-worker-result-schema": b'{"type":"object"}',
    }
    envelope = input_envelope()
    _bind_input_artifacts(envelope, artifacts)
    validated = transport.validate_input_envelope(envelope, artifacts=artifacts)
    assert validated["repository_snapshot"]["size_bytes"] == len(
        artifacts["repository-snapshot"]
    )

    tampered = dict(artifacts)
    tampered["repository-snapshot"] += b"tamper"
    exc = expect_reason(
        "isolated_worker_input_invalid",
        lambda: transport.validate_input_envelope(envelope, artifacts=tampered),
    )
    assert "artifact_size_mismatch:repository-snapshot" in exc.errors
    assert "artifact_digest_mismatch:repository-snapshot" in exc.errors

    oversize_artifacts = dict(artifacts)
    oversize_artifacts["approved-work-order"] = b"x" * (
        transport.MAX_WORK_ORDER_BYTES + 1
    )
    oversize_envelope = input_envelope()
    _bind_input_artifacts(oversize_envelope, oversize_artifacts)
    exc = expect_reason(
        "isolated_worker_input_invalid",
        lambda: transport.validate_input_envelope(
            oversize_envelope, artifacts=oversize_artifacts
        ),
    )
    assert "artifact_too_large:approved-work-order" in exc.errors


def test_result_raw_and_patch_bytes_are_bounded_before_trust() -> None:
    raw = json.dumps(result_envelope(), separators=(",", ":")).encode("utf-8")
    assert transport.validate_result_bytes(raw)["message_type"] == "worker_result"

    exc = expect_reason(
        "isolated_worker_result_too_large",
        lambda: transport.validate_result_bytes(
            b"{" + b" " * transport.MAX_RESULT_ENVELOPE_BYTES
        ),
    )
    assert exc.errors == ("result_envelope_too_large",)

    deeply_nested = (
        b"[" * (transport.MAX_RESULT_NESTING_DEPTH + 1)
        + b"0"
        + b"]" * (transport.MAX_RESULT_NESTING_DEPTH + 1)
    )
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_bytes(deeply_nested),
    )
    assert exc.errors == ("result_structure_too_deep",)

    patch_bytes = b"x" * (transport.MAX_PATCH_BYTES + 1)
    changed = result_envelope()
    changed["patch"]["content_base64"] = base64.b64encode(patch_bytes).decode(
        "ascii"
    )
    changed["patch"]["size_bytes"] = len(patch_bytes)
    changed["patch"]["sha256"] = (
        "sha256:" + hashlib.sha256(patch_bytes).hexdigest()
    )
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(changed),
    )
    assert "patch_too_large" in exc.errors


def test_worker_self_reports_are_preserved_but_never_authoritative() -> None:
    changed = result_envelope()
    self_report = changed["worker_self_reported_execution_evidence"]
    self_report["input_digest_verified"] = False
    self_report["result_schema_verified"] = False
    self_report["network_egress_observed"] = True
    validated = transport.validate_result_envelope(changed)
    assert validated["worker_self_reported_execution_evidence"] == self_report


def test_contract_schemas_are_strict_at_boundary_objects() -> None:
    for path in (transport.INPUT_SCHEMA_PATH, transport.RESULT_SCHEMA_PATH):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert not ({"host_path", "vault_path", "state_root"} & set(schema["properties"]))


def main() -> None:
    tests = [
        test_valid_typed_exchange_is_bound_and_path_free,
        test_regular_all_of_and_unknown_schema_keywords_fail_closed,
        test_input_rejects_host_and_vault_location_fields,
        test_worker_result_cannot_name_host_writeback_targets,
        test_worker_changed_paths_are_guest_relative_only,
        test_patch_and_exchange_bindings_fail_closed,
        test_execution_id_and_freshness_window_are_required_and_bound,
        test_input_artifact_bytes_are_size_digest_and_limit_checked,
        test_result_raw_and_patch_bytes_are_bounded_before_trust,
        test_worker_self_reports_are_preserved_but_never_authoritative,
        test_contract_schemas_are_strict_at_boundary_objects,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
