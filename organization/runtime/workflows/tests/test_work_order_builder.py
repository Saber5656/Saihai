#!/usr/bin/env python3
"""Tests for work-order construction, validation, and snapshots."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"
TEMPLATE_PATH = ROOT / "organization/runtime/workflows/templates/single_step_external_review.yaml"
SCHEMA_PATH = ROOT / "organization/runtime/workflows/schemas/work-order.schema.json"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import work_order_builder


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def step() -> dict:
    return template()["steps"][0]


def activation_scope(**overrides) -> dict:
    candidate = {
        "allowed_paths": ["organization/runtime/workflows"],
        "allowed_ops": {"edit": False, "commit": False, "push": False, "network": False},
        "step_budget": 1,
        "expires_at": "run_terminal",
    }
    candidate.update(overrides)
    return candidate


def run_record(**overrides) -> dict:
    candidate = {
        "task_id": "TSK-work-order",
        "request_id": "req-work-order",
        "run_id": "run-work-order",
        "workflow_id": "single_step_external_review",
        "current_step": "review",
        "activation": {
            "context_scope": {
                "mode": "bounded_refs",
                "refs": ["organization/runtime/workflows/README.md"],
                "raw_transcript_sharing": "forbidden",
            },
            "activation_scope": activation_scope(),
        },
        "requester": {"frontdoor": "codex", "chat_session_id": "test-session"},
    }
    candidate.update(overrides)
    return candidate


def request_record(**overrides) -> dict:
    candidate = {
        "task_id": "TSK-work-order",
        "request_id": "req-work-order",
        "owner_principal": {
            "principal_type": "main_agent_bridge",
            "principal_id": "codex-main-agent-a-prime",
            "authn_method": "installed_frontend_profile",
        },
        "checkout_identity_digest": "sha256:" + "4" * 64,
        "classification": {"context_scope": "refs_only"},
        "approved_activation": {
            "policy": {},
            "activation_scope": activation_scope(),
            "context_scope": {
                "mode": "bounded_refs",
                "refs": ["organization/runtime/workflows/README.md"],
                "raw_transcript_sharing": "forbidden",
            },
            "workflow_selection": {"workflow_id": "single_step_external_review", "initial_step": "review"},
        },
    }
    candidate.update(overrides)
    return candidate


def refs() -> list[dict]:
    return [
        {
            "type": "repo_file",
            "path": "organization/runtime/workflows/README.md",
            "size_bytes": 123,
            "digest": "sha256:" + "1" * 64,
        }
    ]


def build(state_root: Path, **overrides) -> dict:
    tpl = overrides.pop("template", template())
    stp = overrides.pop("step", tpl["steps"][0])
    return work_order_builder.build_work_order(
        run=overrides.pop("run", run_record()),
        request_record=overrides.pop("request", request_record()),
        template=tpl,
        step=stp,
        issuer_principal_redacted={
            "principal_type": "manual_operator",
            "principal_id": "manual-cli",
            "authn_method": "local_cli",
        },
        resolved_refs=overrides.pop("resolved_refs", refs()),
        policy_digest_value=overrides.pop("policy_digest", "sha256:" + "2" * 64),
        signature=overrides.pop(
            "signature",
            {
                "algorithm": "sha256-local-principal-key",
                "signature": "sha256:" + "3" * 64,
                "signed_at": "2026-07-09T00:00:00+0900",
            },
        ),
        report_path_value=overrides.pop(
            "report_path",
            str(state_root / "reports" / "run-work-order" / "review-external-review-report.json"),
        ),
    )


def test_build_valid_p0_order() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root)
        assert_equal(
            work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root),
            [],
            "valid order errors",
        )
        assert_equal(order["permission_mode"], "readonly", "permission")
        assert_equal(order["assignment_role"], "reviewer", "assignment")
        assert_equal(order["external_provider_allowed"], True, "external provider")
        assert_equal(order["activation_scope"]["step_budget"], 1, "step budget")
        assert_equal(order["activation_scope"]["allowed_ops"], {"edit": False, "commit": False, "push": False, "network": False}, "ops")
        assert "Step 'review'" in order["instruction"], "instruction includes step id"
        assert "external_review_report" in order["instruction"], "instruction includes output contract"
        assert_equal(order["work_order_authority"]["runner_claim"]["claim_state"], "unclaimed", "claim")
        assert_equal(
            order["frontend_request_binding"],
            {
                "owner_principal": {
                    "principal_type": "main_agent_bridge",
                    "principal_id": "codex-main-agent-a-prime",
                    "authn_method": "installed_frontend_profile",
                },
                "checkout_identity_digest": "sha256:" + "4" * 64,
            },
            "frontend request binding",
        )
        assert_equal(
            order["projection_binding"],
            work_order_builder.build_projection_binding(
                request_id="req-work-order",
                task_id="TSK-work-order",
                owner_principal=order["frontend_request_binding"]["owner_principal"],
                checkout_identity_digest="sha256:" + "4" * 64,
            ),
            "projection binding",
        )


def test_projection_binding_is_exact_and_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        for field, replacement in (
            ("request_id", "req-other"),
            ("task_id", "TSK-other"),
            ("owner_principal_digest", "sha256:" + "a" * 64),
            ("checkout_identity_digest", "sha256:" + "b" * 64),
        ):
            order = build(state_root)
            order["projection_binding"][field] = replacement
            errors = work_order_builder.validate_work_order(
                order,
                template=template(),
                step=step(),
                state_root=state_root,
            )
            assert "projection_binding_mismatch" in errors, (field, errors)

        missing = build(state_root)
        missing.pop("projection_binding")
        errors = work_order_builder.validate_work_order(
            missing,
            template=template(),
            step=step(),
            state_root=state_root,
        )
        assert "projection_binding_invalid" in errors


def test_frontend_request_binding_is_all_or_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        request = request_record()
        request.pop("checkout_identity_digest")
        try:
            build(state_root, request=request)
        except work_order_builder.WorkOrderError as exc:
            assert_equal(str(exc), "frontend_request_binding_incomplete", "partial binding reason")
        else:
            raise AssertionError("partial frontend request binding accepted")


def test_cursor_and_grok_requesters_preserve_adapter_neutral_work_order_contract() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        for frontend in ("cursor", "grok"):
            principal_id = f"{frontend}-main-agent-a-prime"
            order = build(
                state_root,
                run=run_record(
                    requester={
                        "frontdoor": frontend,
                        "chat_session_id": f"{frontend}-session",
                    }
                ),
                request=request_record(
                    owner_principal={
                        "principal_type": "main_agent_bridge",
                        "principal_id": principal_id,
                        "authn_method": "installed_frontend_profile",
                    }
                ),
            )
            assert_equal(
                work_order_builder.validate_work_order(
                    order,
                    template=template(),
                    step=step(),
                    state_root=state_root,
                ),
                [],
                f"{frontend} work order errors",
            )
            assert_equal(order["requester"]["frontdoor"], frontend, f"{frontend} requester")
            assert_equal(
                order["frontend_request_binding"]["owner_principal"]["principal_id"],
                principal_id,
                f"{frontend} principal binding",
            )
            assert_equal(order["from_role"], "frontdoor", f"{frontend} normalized role")


def test_required_field_list_matches_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert_equal(sorted(work_order_builder.REQUIRED_WORK_ORDER_FIELDS), sorted(schema["required"]), "required fields")


def test_validate_rejects_missing_refs() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root, resolved_refs=[])
        errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
        assert "context_refs must be non-empty" in errors


def test_validate_rejects_report_path_escape() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root, report_path="/tmp/evil.json")
        errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
        assert "report_path must stay under reports" in errors


def test_validate_rejects_foreign_current_run() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root)
        current_run = run_record(
            task_id="TSK-current",
            request_id="req-current",
            run_id="run-current",
        )
        errors = work_order_builder.validate_work_order(
            order,
            template=template(),
            step=step(),
            state_root=state_root,
            run=current_run,
        )
        assert "task_id must match current run" in errors
        assert "request_id must match current run" in errors
        assert "run_id must match current run" in errors
        assert "report_path must match current run report path" in errors


def test_validate_rejects_bridge_issuer() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root)
        order["work_order_authority"]["issuer_principal"]["principal_type"] = "main_agent_bridge"
        errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
        assert "bridge principal cannot issue work orders" in errors


def test_validate_rejects_schema_extra_raw_transcript_field() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root)
        order["raw_transcript"] = "do not embed raw prompt material"
        errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
        assert "schema:$.raw_transcript:additional_property" in errors
        assert "forbidden_raw_transcript_field:$.raw_transcript" in errors


def test_validate_rejects_p0_mutations() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        cases = [
            ("permission_mode", "edit", "permission_mode must match template step"),
            ("external_provider_allowed", False, "external_provider_allowed must be True"),
        ]
        for field, value, expected_error in cases:
            order = build(state_root)
            order[field] = value
            errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
            assert expected_error in errors, f"{field} should be rejected: {errors}"
        for op in ("edit", "commit", "push", "network"):
            order = build(state_root)
            order["activation_scope"]["allowed_ops"][op] = True
            errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
            assert f"activation_scope.allowed_ops.{op} must be false" in errors
        order = build(state_root)
        order["activation_scope"]["step_budget"] = 2
        errors = work_order_builder.validate_work_order(order, template=template(), step=step(), state_root=state_root)
        assert "activation_scope.step_budget must be 1" in errors


def test_context_mode_downgrade_is_deterministic() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root, request={"classification": {}, "approved_activation": request_record()["approved_activation"]})
        assert_equal(order["context_scope"]["mode"], "refs_only", "downgraded mode")
        assert_equal(order["context_scope"]["context_mode_downgraded_from"], "bounded_refs", "downgrade source")


def test_snapshot_freeze_and_conflict() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root)
        first = work_order_builder.freeze_step_snapshot(state_root, order, iteration=1)
        second = work_order_builder.freeze_step_snapshot(state_root, order, iteration=1)
        assert_equal(second, first, "snapshot replay path")
        mutated = {**order, "instruction": "mutated"}
        try:
            work_order_builder.freeze_step_snapshot(state_root, mutated, iteration=1)
        except work_order_builder.WorkOrderError as exc:
            assert_equal(str(exc), "step_snapshot_conflict", "snapshot conflict")
        else:
            raise AssertionError("mutated order should conflict with frozen snapshot")


def test_snapshot_rejects_malformed_existing_file() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        order = build(state_root)
        path = work_order_builder.snapshot_path(state_root, "run-work-order", "review", 1)
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        try:
            work_order_builder.freeze_step_snapshot(state_root, order, iteration=1)
        except work_order_builder.WorkOrderError as exc:
            assert_equal(str(exc), "step_snapshot_invalid", "malformed snapshot")
        else:
            raise AssertionError("malformed snapshot should be invalid")

        path.write_text("[]", encoding="utf-8")
        try:
            work_order_builder.freeze_step_snapshot(state_root, order, iteration=1)
        except work_order_builder.WorkOrderError as exc:
            assert_equal(str(exc), "step_snapshot_invalid", "non-object snapshot")
        else:
            raise AssertionError("non-object snapshot should be invalid")


def test_snapshot_path_rejects_symlinked_work_order_root() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp)
        state_root = base / "state"
        outside = base / "outside"
        state_root.mkdir()
        outside.mkdir()
        (state_root / "work-orders").symlink_to(outside, target_is_directory=True)
        try:
            work_order_builder.snapshot_path(state_root, "run-work-order", "review", 1)
        except work_order_builder.WorkOrderError as exc:
            assert_equal(str(exc), "state_artifact_path_escape", "snapshot symlink reason")
        else:
            raise AssertionError("symlinked work-order root must be rejected")


def main() -> None:
    tests = [
        test_build_valid_p0_order,
        test_frontend_request_binding_is_all_or_nothing,
        test_projection_binding_is_exact_and_fail_closed,
        test_cursor_and_grok_requesters_preserve_adapter_neutral_work_order_contract,
        test_required_field_list_matches_schema,
        test_validate_rejects_missing_refs,
        test_validate_rejects_report_path_escape,
        test_validate_rejects_foreign_current_run,
        test_validate_rejects_bridge_issuer,
        test_validate_rejects_schema_extra_raw_transcript_field,
        test_validate_rejects_p0_mutations,
        test_context_mode_downgrade_is_deterministic,
        test_snapshot_freeze_and_conflict,
        test_snapshot_rejects_malformed_existing_file,
        test_snapshot_path_rejects_symlinked_work_order_root,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
