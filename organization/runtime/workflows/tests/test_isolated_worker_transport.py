#!/usr/bin/env python3
"""Contract tests for the isolated worker policy-domain transport."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
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
RESULT_RECEIVED_AT = datetime(2026, 7, 17, 0, 4, 59, tzinfo=timezone.utc)


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


def regular_text_change(
    path: str = "README.md",
    content: bytes = b"updated readme\n",
    operation: str = "replace",
) -> dict:
    return {
        "operation": operation,
        "relative_path": path,
        "file_type": "regular",
        "content_encoding": "base64",
        "content_media_type": "text/plain; charset=utf-8",
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def result_envelope() -> dict:
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
            "artifact_id": "worktree-file-changes",
            "media_type": "application/vnd.saihai.regular-text-file-changes+json",
            "format_version": "1",
            "changes": [regular_text_change()],
        },
        "worker_self_reported_execution_evidence": {
            "input_digest_verified": True,
            "result_schema_verified": True,
            "network_egress_observed": False,
            "credential_material_observed": False,
            "host_mounts_observed": False,
        },
    }


def result_with_paths(paths: list[str]) -> dict:
    changed = result_envelope()
    changed["worker_result"]["changed_paths"] = paths
    changed["patch"]["changes"] = [
        regular_text_change(path, b"", "create") for path in paths
    ]
    return changed


def fixed_length_paths(count: int, byte_length: int) -> list[str]:
    paths = []
    for index in range(count):
        prefix = f"d{index:03}/"
        final_component_length = byte_length - len(prefix) - 256
        assert 0 < final_component_length <= transport.MAX_PATH_COMPONENT_BYTES
        paths.append(
            prefix
            + "a" * transport.MAX_PATH_COMPONENT_BYTES
            + "/"
            + "b" * final_component_length
        )
    assert all(len(path.encode("utf-8")) == byte_length for path in paths)
    return paths


def expect_reason(reason: str, callback) -> transport.IsolatedWorkerTransportError:
    try:
        callback()
    except transport.IsolatedWorkerTransportError as exc:
        assert exc.reason == reason, exc.errors
        return exc
    raise AssertionError(f"expected IsolatedWorkerTransportError({reason})")


def test_valid_typed_exchange_is_bound_and_path_free() -> None:
    incoming, outgoing = transport.validate_exchange(
        input_envelope(),
        result_envelope(),
        result_received_at=RESULT_RECEIVED_AT,
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

    assert transport._validate_schema_value(
        [1, 2],
        {"type": "array", "maxItems": 1, "items": {"type": "integer"}},
    ) == ["schema:$:max_items"]
    invalid_bound_errors = transport._validate_schema_value(
        [], {"type": "array", "maxItems": 1.5}
    )
    assert any("maxItems:invalid" in error for error in invalid_bound_errors)


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
        "b/../../Agents-Vault/task.md",
        "../../.codex/state/runs/run.json",
        "../state/runs/run.json",
        "nested/../../state",
        "nested\\escape",
        "C:/host/state/run.json",
        "nul\x00byte",
        ".git/config",
        "nested/.HG/store",
        ".jj/repo/store",
        "docs/cafe\u0301.md",
        ".",
    ):
        changed = result_envelope()
        changed["worker_result"]["changed_paths"] = [changed_path]
        changed["patch"]["changes"][0]["relative_path"] = changed_path
        exc = expect_reason(
            "isolated_worker_result_invalid",
            lambda changed=changed: transport.validate_result_envelope(changed),
        )
        assert any("changed_path_invalid" in item for item in exc.errors), exc.errors


def test_worker_paths_require_strict_portable_utf8_and_byte_limits() -> None:
    component_at_limit = "a" * transport.MAX_PATH_COMPONENT_BYTES
    path_at_limit = "/".join(
        ["a" * 255, "b" * 255, "c" * 255, "d" * 254, "e"]
    )
    assert len(path_at_limit.encode("utf-8")) == transport.MAX_RELATIVE_PATH_BYTES
    transport.validate_result_envelope(
        result_with_paths([component_at_limit, path_at_limit])
    )

    for invalid_path in (
        "a" * (transport.MAX_PATH_COMPONENT_BYTES + 1),
        path_at_limit + "e",
        "docs/reordered-\u202evalue.md",
        "docs/file.txt:stream",
    ):
        exc = expect_reason(
            "isolated_worker_result_invalid",
            lambda invalid_path=invalid_path: transport.validate_result_envelope(
                result_with_paths([invalid_path])
            ),
        )
        assert "changed_path_invalid:$[0]" in exc.errors, exc.errors
        assert "file_change_path_invalid:$[0]" in exc.errors, exc.errors

    surrogate_path = "docs/bad-\udc80.md"
    raw = json.dumps(
        result_with_paths([surrogate_path]), separators=(",", ":")
    ).encode("ascii")
    assert b"\\udc80" in raw
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_bytes(raw),
    )
    assert "changed_path_invalid:$[0]" in exc.errors, exc.errors
    assert "file_change_path_invalid:$[0]" in exc.errors, exc.errors


def test_file_change_count_and_aggregate_path_bounds() -> None:
    at_count_limit = [
        f"bounded/file-{index:03}.md" for index in range(transport.MAX_FILE_CHANGES)
    ]
    transport.validate_result_envelope(result_with_paths(at_count_limit))

    over_count_limit = at_count_limit + ["bounded/file-over-limit.md"]
    changed_paths_over = result_with_paths(over_count_limit)
    changed_paths_over["patch"]["changes"] = changed_paths_over["patch"][
        "changes"
    ][:-1]
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(changed_paths_over),
    )
    assert "schema:$.worker_result.changed_paths:max_items" in exc.errors
    assert "changed_paths_too_many" in exc.errors

    changes_over = result_with_paths(over_count_limit)
    changes_over["worker_result"]["changed_paths"] = changes_over[
        "worker_result"
    ]["changed_paths"][:-1]
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(changes_over),
    )
    assert "schema:$.patch:any_of" in exc.errors
    assert "file_changes_too_many" in exc.errors

    aggregate_path_length = (
        transport.MAX_AGGREGATE_PATH_BYTES // transport.MAX_FILE_CHANGES
    )
    assert (
        aggregate_path_length * transport.MAX_FILE_CHANGES
        == transport.MAX_AGGREGATE_PATH_BYTES
    )
    aggregate_at_limit = fixed_length_paths(
        transport.MAX_FILE_CHANGES, aggregate_path_length
    )
    transport.validate_result_envelope(result_with_paths(aggregate_at_limit))

    aggregate_over_limit = aggregate_at_limit.copy()
    aggregate_over_limit[0] += "b"
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(
            result_with_paths(aggregate_over_limit)
        ),
    )
    assert "changed_paths_aggregate_path_bytes_too_large" in exc.errors
    assert "file_changes_aggregate_path_bytes_too_large" in exc.errors


def test_typed_file_change_content_and_exchange_bindings_fail_closed() -> None:
    changed = result_envelope()
    changed["patch"]["changes"][0]["sha256"] = SHA_A
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(changed),
    )
    assert "file_change_digest_mismatch:$[0]" in exc.errors

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
            lambda changed=changed: transport.validate_exchange(
                input_envelope(),
                changed,
                result_received_at=RESULT_RECEIVED_AT,
            ),
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
        lambda: transport.validate_exchange(
            input_envelope(),
            changed,
            result_received_at=RESULT_RECEIVED_AT,
        ),
    )
    assert "exchange_binding_mismatch:expires_at" in exc.errors


def test_result_receipt_uses_closed_open_trusted_clock_window() -> None:
    issued_at = datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc)
    transport.validate_exchange(
        input_envelope(), result_envelope(), result_received_at=issued_at
    )

    expires_at = datetime(2026, 7, 17, 0, 5, 0, tzinfo=timezone.utc)
    exc = expect_reason(
        "isolated_worker_exchange_invalid",
        lambda: transport.validate_exchange(
            input_envelope(), result_envelope(), result_received_at=expires_at
        ),
    )
    assert "freshness_window_expired" in exc.errors

    exc = expect_reason(
        "isolated_worker_exchange_invalid",
        lambda: transport.validate_exchange(
            input_envelope(),
            result_envelope(),
            result_received_at=datetime(2026, 7, 16, 23, 59, 59, tzinfo=timezone.utc),
        ),
    )
    assert "freshness_window_not_yet_valid" in exc.errors


def test_changed_paths_and_typed_change_paths_are_exact_unique_sets() -> None:
    duplicate_declaration = result_envelope()
    duplicate_declaration["worker_result"]["changed_paths"] = [
        "README.md",
        "README.md",
    ]
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(duplicate_declaration),
    )
    assert "changed_path_duplicate:README.md" in exc.errors

    missing_change = result_envelope()
    missing_change["worker_result"]["changed_paths"].append("docs/design.md")
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(missing_change),
    )
    assert (
        "changed_path_set_mismatch:missing_file_change:docs/design.md" in exc.errors
    )

    undeclared_change = result_envelope()
    undeclared_change["patch"]["changes"].append(
        regular_text_change("docs/design.md", b"design\n", "create")
    )
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(undeclared_change),
    )
    assert (
        "changed_path_set_mismatch:undeclared_file_change:docs/design.md"
        in exc.errors
    )

    duplicate_change = result_envelope()
    duplicate_change["patch"]["changes"].append(regular_text_change())
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(duplicate_change),
    )
    assert "file_change_path_duplicate:README.md" in exc.errors

    case_collision = result_envelope()
    case_collision["worker_result"]["changed_paths"] = [
        "README.md",
        "readme.md",
    ]
    case_collision["patch"]["changes"].append(
        regular_text_change("readme.md", b"collision\n")
    )
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(case_collision),
    )
    assert "changed_path_collision:readme.md" in exc.errors
    assert "file_change_path_collision:readme.md" in exc.errors

    deletion = result_envelope()
    deletion["worker_result"]["changed_paths"] = ["docs/obsolete.md"]
    deletion["patch"]["changes"] = [
        {
            "operation": "delete",
            "relative_path": "docs/obsolete.md",
            "file_type": "regular",
        }
    ]
    validated = transport.validate_result_envelope(deletion)
    assert validated["patch"]["changes"][0]["operation"] == "delete"


def test_opaque_diff_and_non_regular_operations_are_unrepresentable() -> None:
    adversarial_diffs = (
        b"diff --git a/README.md b/../../Agents-Vault/task.md\n",
        b"diff --git a/README.md b/.git/config\n",
        b'diff --git "a/README.md" "b/../../state-tree/run.json"\n',
        (
            b"diff --git a/old.md b/new.md\nsimilarity index 100%\n"
            b"rename from old.md\nrename to new.md\n"
        ),
        (
            b"diff --git a/source.md b/copy.md\nsimilarity index 100%\n"
            b"copy from source.md\ncopy to copy.md\n"
        ),
        b"diff --git a/link b/link\nnew file mode 120000\n+/primary-host/Agents-Vault\n",
    )
    for raw_diff in adversarial_diffs:
        changed = result_envelope()
        changed["patch"] = {
            "artifact_id": "worktree-patch",
            "media_type": "text/x-diff",
            "encoding": "base64",
            "sha256": "sha256:" + hashlib.sha256(raw_diff).hexdigest(),
            "size_bytes": len(raw_diff),
            "content_base64": base64.b64encode(raw_diff).decode("ascii"),
        }
        expect_reason(
            "isolated_worker_result_invalid",
            lambda changed=changed: transport.validate_result_envelope(changed),
        )

    for unsupported in (
        {"operation": "rename", "old_path": "old.md", "relative_path": "new.md"},
        {"operation": "copy", "old_path": "old.md", "relative_path": "new.md"},
        {
            "operation": "create",
            "relative_path": "link",
            "file_type": "symlink",
            "mode": "120000",
            "target": "/primary-host/Agents-Vault",
        },
        {
            "operation": "create",
            "relative_path": "vendor/module",
            "file_type": "submodule",
            "mode": "160000",
        },
    ):
        changed = result_envelope()
        changed["worker_result"]["changed_paths"] = [unsupported["relative_path"]]
        changed["patch"]["changes"] = [unsupported]
        expect_reason(
            "isolated_worker_result_invalid",
            lambda changed=changed: transport.validate_result_envelope(changed),
        )


def test_structured_quoted_path_is_unambiguous_and_binary_content_is_rejected() -> None:
    quoted_path = 'docs/"quoted".md'
    changed = result_envelope()
    changed["worker_result"]["changed_paths"] = [quoted_path]
    changed["patch"]["changes"] = [
        regular_text_change(quoted_path, b"quoted path\n", "create")
    ]
    validated = transport.validate_result_envelope(changed)
    assert validated["patch"]["changes"][0]["relative_path"] == quoted_path

    binary = result_envelope()
    binary["patch"]["changes"] = [regular_text_change(content=b"\xff\x00")]
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(binary),
    )
    assert "file_change_binary_content_forbidden:$[0]" in exc.errors


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


def test_result_raw_and_file_change_bytes_are_bounded_before_trust() -> None:
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
    changed["patch"]["changes"] = [regular_text_change(content=patch_bytes)]
    exc = expect_reason(
        "isolated_worker_result_invalid",
        lambda: transport.validate_result_envelope(changed),
    )
    assert "patch_too_large" in exc.errors


def test_result_raw_json_rejects_duplicate_members_at_every_depth() -> None:
    raw = json.dumps(result_envelope(), separators=(",", ":")).encode("utf-8")
    duplicate_cases = (
        b'{"message_type":"worker_result",' + raw[1:],
        (
            raw.replace(
                b'"worker_result":{',
                b'"worker_result":{"status":"completed",',
                1,
            )
        ),
        (
            raw.replace(
                b'"changes":[{',
                b'"changes":[{"operation":"replace",',
                1,
            )
        ),
    )
    for duplicate_raw in duplicate_cases:
        exc = expect_reason(
            "isolated_worker_result_malformed",
            lambda duplicate_raw=duplicate_raw: transport.validate_result_bytes(
                duplicate_raw
            ),
        )
        assert exc.errors == ("result_json_duplicate_member",)


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

    result_schema = json.loads(transport.RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert (
        result_schema["properties"]["worker_result"]["properties"][
            "changed_paths"
        ]["maxItems"]
        == transport.MAX_FILE_CHANGES
    )
    assert (
        result_schema["$defs"]["patchPayload"]["properties"]["changes"][
            "maxItems"
        ]
        == transport.MAX_FILE_CHANGES
    )


def main() -> None:
    tests = [
        test_valid_typed_exchange_is_bound_and_path_free,
        test_regular_all_of_and_unknown_schema_keywords_fail_closed,
        test_input_rejects_host_and_vault_location_fields,
        test_worker_result_cannot_name_host_writeback_targets,
        test_worker_changed_paths_are_guest_relative_only,
        test_worker_paths_require_strict_portable_utf8_and_byte_limits,
        test_file_change_count_and_aggregate_path_bounds,
        test_typed_file_change_content_and_exchange_bindings_fail_closed,
        test_execution_id_and_freshness_window_are_required_and_bound,
        test_result_receipt_uses_closed_open_trusted_clock_window,
        test_changed_paths_and_typed_change_paths_are_exact_unique_sets,
        test_opaque_diff_and_non_regular_operations_are_unrepresentable,
        test_structured_quoted_path_is_unambiguous_and_binary_content_is_rejected,
        test_input_artifact_bytes_are_size_digest_and_limit_checked,
        test_result_raw_and_file_change_bytes_are_bounded_before_trust,
        test_result_raw_json_rejects_duplicate_members_at_every_depth,
        test_worker_self_reports_are_preserved_but_never_authoritative,
        test_contract_schemas_are_strict_at_boundary_objects,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
