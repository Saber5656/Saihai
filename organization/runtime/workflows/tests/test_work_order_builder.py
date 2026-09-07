#!/usr/bin/env python3
"""Tests for work-order construction, validation, and snapshots."""

from __future__ import annotations

import json
import sys
import tempfile
from unittest.mock import patch
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
        "task_id": "TSK-PENDING-work-order",
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
        "task_id": "TSK-PENDING-work-order",
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
        provider_adapter_id_value=overrides.pop(
            "provider_adapter_id", "claude_headless_p0"
        ),
        intended_model_value=overrides.pop("intended_model", "claude-sonnet-4-6"),
        worker_execution_plan=overrides.pop("worker_execution_plan", None),
        effective_model_policy_value=overrides.pop(
            "effective_model_policy", "required_exact_match"
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
        assert_equal(order["provider_adapter_id"], "claude_headless_p0", "provider adapter")
        assert_equal(order["intended_model"], "claude-sonnet-4-6", "intended model")
        assert_equal(
            order["effective_model_policy"],
            "required_exact_match",
            "effective model policy",
        )
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
                task_id="TSK-PENDING-work-order",
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
            ("task_id", "TSK-PENDING-other"),
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
        for owner, digest in ((request_record()["owner_principal"], ""),
                              (request_record()["owner_principal"], None),
                              (None, "sha256:" + "4" * 64),
                              (None, False), (None, 0), (None, {})):
            request = request_record(owner_principal=owner, checkout_identity_digest=digest)
            if digest is None:
                request.pop("checkout_identity_digest")
            with patch.object(work_order_builder, "projection_binding_from_request_record") as projection:
                try:
                    build(Path(raw_tmp), request=request)
                except work_order_builder.WorkOrderError as exc:
                    assert_equal(str(exc), "frontend_request_binding_incomplete", "partial binding reason")
                else:
                    raise AssertionError("partial frontend request binding accepted")
                projection.assert_not_called()


def unbound_request() -> dict:
    request = request_record(owner_principal=None)
    request.pop("checkout_identity_digest")
    return request


def standard_template() -> dict:
    return json.loads((TEMPLATE_PATH.parent / "standard_code_change.yaml").read_text())


def test_unbound_readonly_empty_or_absent_digest_is_valid() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        for digest_fields in ({}, {"checkout_identity_digest": ""}, {"checkout_identity_digest": None}):
            request = {**unbound_request(), **digest_fields}
            order = build(state_root, request=request)
            assert "frontend_request_binding" not in order
            assert "projection_binding" not in order
            assert_equal(work_order_builder.validate_work_order(
                order, template=template(), step=step(), state_root=state_root,
            ), [], "unbound readonly order")


def test_unbound_builder_rejects_edit_and_full() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tpl = standard_template()
        for mode in ("edit", "full"):
            stp = {**tpl["steps"][0], "permission_mode": mode}
            run = run_record(workflow_id=tpl["workflow_id"], current_step="implement")
            run["activation"]["activation_scope"]["allowed_ops"]["edit"] = True
            try:
                build(Path(raw_tmp), request=unbound_request(), template=tpl, step=stp, run=run)
            except work_order_builder.WorkOrderError as exc:
                assert_equal(str(exc), "unbound_work_order_requires_readonly", mode)
            else:
                raise AssertionError(f"unbound {mode} implement accepted")


def test_unbound_builder_rejects_enabled_ops_and_worker_plan() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        for op in ("edit", "commit", "push", "network"):
            run = run_record()
            run["activation"]["activation_scope"]["allowed_ops"][op] = True
            try:
                build(Path(raw_tmp), request=unbound_request(), run=run)
            except work_order_builder.WorkOrderError as exc:
                assert_equal(str(exc), "unbound_work_order_requires_readonly", op)
            else:
                raise AssertionError(f"unbound {op} accepted")
        try:
            build(Path(raw_tmp), request=unbound_request(), worker_execution_plan={})
        except work_order_builder.WorkOrderError as exc:
            assert_equal(str(exc), "unbound_work_order_worker_execution_plan_forbidden", "plan")
        else:
            raise AssertionError("unbound worker plan accepted")


def test_unbound_validator_rejects_crafted_authority() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        tpl = standard_template()
        # A real standard-code-change review avoids the external-review-only guard.
        stp = tpl["steps"][1]
        run = run_record(workflow_id=tpl["workflow_id"], current_step=stp["id"])
        valid = build(state_root, template=tpl, step=stp, run=run,
                      report_path=str(work_order_builder.report_path(state_root, run["run_id"], stp["id"])))
        valid.pop("frontend_request_binding")
        valid.pop("projection_binding")
        assert_equal(work_order_builder.validate_work_order(
            valid, template=tpl, step=stp, state_root=state_root, run=run,
        ), [], "bounded readonly standard review")
        for op in ("edit", "commit", "push", "network"):
            for value in (True, None, 0, "false"):
                order = json.loads(json.dumps(valid))
                order["activation_scope"]["allowed_ops"][op] = value
                errors = work_order_builder.validate_work_order(
                    order, template=tpl, step=stp, state_root=state_root, run=run,
                )
                assert "unbound_work_order_requires_readonly" in errors, (op, value, errors)
        for mode in ("edit", "full"):
            order = {**valid, "permission_mode": mode, "step_id": "implement"}
            implement = {**tpl["steps"][0], "permission_mode": mode}
            errors = work_order_builder.validate_work_order(
                order, template=tpl, step=implement, state_root=state_root, run=run,
            )
            assert "unbound_work_order_requires_readonly" in errors, (mode, errors)
        for plan in ({}, None):
            errors = work_order_builder.validate_work_order(
                {**valid, "worker_execution_plan": plan}, template=tpl,
                step=stp, state_root=state_root, run=run,
            )
            assert "unbound_work_order_worker_execution_plan_forbidden" in errors, errors


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
            task_id="TSK-PENDING-current",
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


def chain_template() -> dict:
    path = TEMPLATE_PATH.with_name('readonly_review_chain.yaml')
    return json.loads(path.read_text(encoding='utf-8'))


def chain_run(step_id: str) -> dict:
    value = run_record(workflow_id='readonly_review_chain', current_step=step_id)
    value['activation']['activation_scope']['step_budget'] = 3
    return value


def test_readonly_chain_provider_flags_match_existing_schema() -> None:
    tpl = chain_template()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for stp in tpl['steps']:
            run = chain_run(stp['id'])
            order = build(root, template=tpl, step=stp, run=run,
                          report_path=str(work_order_builder.report_path(root, run['run_id'], stp['id'])))
            assert order['external_provider_allowed'] is (stp['id'] != 'final_evidence')
            assert work_order_builder.validate_work_order(
                order, template=tpl, step=stp, state_root=root, run=run,
            ) == []


def test_readonly_chain_provider_flag_rejects_contract_drift() -> None:
    import copy
    changes = [
        ('step', 'permission_mode', 'edit'), ('step', 'permission_mode', 'full'),
        ('step', 'id', 'unknown'), ('step', 'role', 'git-publisher'),
        ('step', 'assignment_role', 'implementer'),
        ('step', 'output_contract', 'code_change_report'),
        ('run', 'workflow_id', 'research_only'),
        ('run', 'current_step', 'final_evidence'),
        ('template', 'workflow_id', 'research_only'),
        ('template', 'safety_class', 'standard'),
    ]
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        for original in chain_template()['steps'][:2]:
            for target, field, value in changes:
                tpl = chain_template()
                stp = copy.deepcopy(original)
                run = chain_run(stp['id'])
                {'template': tpl, 'step': stp, 'run': run}[target][field] = value
                order = build(root, template=tpl, step=stp, run=run)
                assert order['external_provider_allowed'] is False, (target, field, value)
            for key, value in [('adapter_kind', 'unknown'), ('adapter_kind', 'external_provider'),
                               ('runner_authority', 'edit'), ('transition_authority', 'provider')]:
                tpl = chain_template()
                stp = copy.deepcopy(original)
                stp['provider_route'][key] = value
                order = build(root, template=tpl, step=stp, run=chain_run(stp['id']))
                assert order['external_provider_allowed'] is False, (key, value)
            for target in ('step', 'activation'):
                for key in ('edit', 'commit', 'push', 'network'):
                    for value in (True, 0, 0.0, None, 'false', 'missing'):
                        tpl = chain_template()
                        stp = copy.deepcopy(original)
                        run = chain_run(stp['id'])
                        ops = stp['allowed_ops'] if target == 'step' else run['activation']['activation_scope']['allowed_ops']
                        if value == 'missing':
                            del ops[key]
                        else:
                            ops[key] = value
                        order = build(root, template=tpl, step=stp, run=run)
                        assert order['external_provider_allowed'] is False, (target, key, value)
                for invalid in (None, [], {}, {'edit': False, 'commit': False,
                                               'push': False, 'network': False, 'shell': False}):
                    tpl = chain_template()
                    stp = copy.deepcopy(original)
                    run = chain_run(stp['id'])
                    scope = stp if target == 'step' else run['activation']['activation_scope']
                    scope['allowed_ops'] = invalid
                    order = build(root, template=tpl, step=stp, run=run)
                    assert order['external_provider_allowed'] is False, (target, invalid)


def test_existing_bounded_routes_remain_unpermitted() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        checked = 0
        for path in sorted(TEMPLATE_PATH.parent.glob('*.yaml')):
            tpl = json.loads(path.read_text())
            if tpl['workflow_id'] == 'readonly_review_chain':
                continue
            for stp in tpl['steps']:
                order = build(root, template=tpl, step=stp,
                              run=run_record(workflow_id=tpl['workflow_id'], current_step=stp['id']))
                expected = stp['provider_route']['adapter_kind'] == 'external_provider'
                assert order['external_provider_allowed'] is expected, (tpl['workflow_id'], stp['id'])
                checked += 1
        assert checked == 21


def test_real_readonly_chain_blocks_admission_before_work_order() -> None:
    from test_frontdoor_orchestrator import external_review_classification, load_payload, run_frontdoor
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        classification = external_review_classification(
            task_kind='research', expected_artifacts=['research_report', 'typed_report', 'final_evidence'],
        )
        proposed = load_payload(run_frontdoor(
            root, 'propose', '--task-id', 'TSK-PENDING-chain', '--request-id', 'req-chain',
            '--prompt', 'Research and independently review bounded evidence',
            '--classification', json.dumps(classification),
            '--ref', 'organization/runtime/workflows/README.md',
            check=False,
        ))
        assert proposed['decision'] == 'blocked', proposed
        assert proposed['request_status'] == 'blocked', proposed
        assert proposed['activation']['approval_required_reason'] == 'readonly_chain_runtime_unavailable'
        assert proposed['activation']['next_action'] == 'abort'
        assert proposed['approval'] is None
        # No approval challenge is exposed; a direct approval attempt also fails closed.
        approved = load_payload(run_frontdoor(
            root, 'approve', '--request-id', 'req-chain',
            '--human-action-id', 'unavailable-runtime-cannot-be-approved', check=False,
        ))
        assert approved['decision'] == 'blocked', approved
        record = json.loads(Path(proposed['request_path']).read_text())
        assert record['status'] == 'blocked'
        assert record['proposal']['approval_required_reason'] == 'readonly_chain_runtime_unavailable'
        created = load_payload(run_frontdoor(
            root, 'create-run', '--request-id', 'req-chain', '--run-id', 'run-chain', check=False,
        ))
        assert created['decision'] == 'blocked', created
        assert not list((root / 'runs').glob('*.json'))
        assert not list((root / 'work-orders').rglob('*.json'))


def test_bounded_resolution_instruction_preserves_original_ids() -> None:
    from test_review_lifecycle import ReviewLifecycleTests
    fixture = ReviewLifecycleTests()
    fixture.setUp()
    try:
        ids = fixture.enable_flow()
        fixture.seal(ids)
        run = fixture.produced()
        tpl = standard_template()
        stp = tpl['steps'][1]
        request = request_record(request_id=run['request_id'], task_id=run['task_id'])
        order = build(fixture.root, run=run, template=tpl, step=stp, request=request,
                      report_path=str(work_order_builder.report_path(fixture.root, run['run_id'], stp['id'])))
        errors = work_order_builder.validate_work_order(order, template=tpl, step=stp,
            state_root=fixture.root, run=run)
        assert errors == [], errors
        assert order['expected_output'] == 'code_change_report'
        assert stp['provider_route']['adapter_kind'] == 'bounded_provider'
        assert 'original_findings_only' in order['instruction']
        assert 'original issue' in order['instruction']
        for fid in ids:
            assert fid in order['instruction']
        assert 'c'*40 in order['instruction']
        assert order['permission_mode'] == 'readonly'
    finally:
        fixture.tearDown()


def main() -> None:
    tests = [
        test_readonly_chain_provider_flags_match_existing_schema,
        test_readonly_chain_provider_flag_rejects_contract_drift,
        test_existing_bounded_routes_remain_unpermitted,
        test_real_readonly_chain_blocks_admission_before_work_order,
        test_bounded_resolution_instruction_preserves_original_ids,
        test_build_valid_p0_order,
        test_frontend_request_binding_is_all_or_nothing,
        test_unbound_readonly_empty_or_absent_digest_is_valid,
        test_unbound_builder_rejects_edit_and_full,
        test_unbound_builder_rejects_enabled_ops_and_worker_plan,
        test_unbound_validator_rejects_crafted_authority,
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
