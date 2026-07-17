#!/usr/bin/env python3
"""Surface registration, routing, and fail-closed contract tests."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = WORKFLOW_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402
import frontdoor_orchestrator as frontdoor  # noqa: E402
import frontdoor_server  # noqa: E402
import frontdoor_surface_registry as surfaces  # noqa: E402


def fake_assurance_registry() -> dict[str, Any]:
    value = assurance.load_registry()
    candidate = copy.deepcopy(
        next(
            profile
            for profile in value["profiles"]
            if profile["profile_id"] == "cursor-main-agent-candidate"
        )
    )
    candidate.update(
        {
            "profile_id": "fixture-main-agent-ingress",
            "agent_family": "fixture",
            "target_claims": ["ingress_enforced"],
            "rationale": "Fixture surface target used only by the offline registry test.",
        }
    )
    value["profiles"].append(candidate)
    assert assurance.validate_registry(value) == []
    return value


def fake_descriptor() -> dict[str, Any]:
    return {
        "descriptor_version": "1",
        "frontend_kind": "fixture",
        "submit_contract": {
            "contract_id": "main-agent-bridge-submit",
            "contract_version": "1",
        },
        "launcher": {
            "launcher_id": "fixture-launcher",
            "launch_session_required": False,
            "verifier_module": None,
            "verifier_class": None,
        },
        "requirements": {"requirements_id": "fixture-requirements"},
        "assurance": {
            "profile_id": "fixture-main-agent-ingress",
            "target_state": "ingress_enforced",
        },
    }


def fixture_registry(
    *,
    commissioned: bool | dict[str, bool],
) -> surfaces.SurfaceRegistry:
    assurance_registry = fake_assurance_registry()

    def evaluate(profile_id: str, _checkout: Path | None) -> dict[str, Any]:
        assert profile_id == "fixture-main-agent-ingress"
        current = (
            commissioned["value"] if isinstance(commissioned, dict) else commissioned
        )
        return {
            "decision": "allow" if current else "suppress",
            "effective_claims": ["ingress_enforced"] if current else [],
        }

    registry = surfaces.SurfaceRegistry(
        surfaces.load_registry(),
        assurance_registry=assurance_registry,
        assurance_evaluator=evaluate,
    )
    registered = registry.register(fake_descriptor())
    assert registered.frontend_kind == "fixture"
    return registry


def bridge_payload(request_id: str, frontend_kind: str) -> dict[str, Any]:
    return {
        "task_id": "TSK-surface-fixture",
        "request_id": request_id,
        "request_kind": "external_review_request",
        "prompt": "Route the same deterministic review workflow.",
        "refs": ["organization/runtime/workflows/README.md"],
        "allowed_paths": ["organization/runtime/workflows"],
        "frontdoor": frontend_kind,
        "idempotency_key": request_id + "-key",
    }


def test_shipped_registry_and_schema_are_closed_and_codex_first() -> None:
    schema = json.loads(surfaces.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["title"] == "Frontdoor Surface Registry"
    assert schema["additionalProperties"] is False
    payload = surfaces.load_registry()
    registry = surfaces.default_registry()
    assert surfaces.validate_registry(payload, assurance.load_registry()) == []
    assert registry.frontend_kinds[0] == "codex"
    codex = registry.descriptor("codex")
    assert codex.assurance_profile_id == "codex-main-agent-a-prime"
    assert codex.target_assurance_state == "action_enforced"
    assert codex.launch_session_required is True
    assert codex.submit_contract_version == "1"
    projection_schema = json.loads(
        (WORKFLOW_ROOT / "schemas" / "orchestrator-projection.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "surface_identity" in projection_schema["required"]
    assert projection_schema["properties"]["surface_identity"] == {
        "$ref": "#/$defs/surface_identity"
    }
    assert projection_schema["$defs"]["surface_identity"]["additionalProperties"] is False


def test_fake_surface_registers_and_routes_through_deterministic_pipeline() -> None:
    registry = fixture_registry(commissioned=True)
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        submitted = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=bridge_payload("req-surface-fixture", "fixture"),
            surface_registry=registry,
        )
        identity = submitted["surface_identity"]
        assert identity["frontend_kind"] == "fixture"
        assert identity["assurance_state"] == "ingress_enforced"
        assert identity["commissioned_claims"] == ["ingress_enforced"]

        classified = frontdoor.proposed_request(
            state_root=state_root,
            task_id="TSK-surface-fixture",
            request_id="req-surface-fixture",
            user_prompt="Route the same deterministic review workflow.",
            refs=["organization/runtime/workflows/README.md"],
            classification={
                "classification_version": "1",
                "classification_source": "deterministic_fixture",
                "classification_confidence": 1.0,
                "classification_evidence": ["surface-registry-test"],
                "task_kind": "external_review",
                "permission_required": "readonly",
                "external_provider_required": True,
                "publication_required": False,
                "security_sensitive": False,
                "destructive_operation": False,
                "context_scope": "refs_only",
                "expected_artifacts": ["typed_report"],
            },
            allowed_paths=["organization/runtime/workflows"],
            expires_at="run_terminal",
            frontdoor="fixture",
            chat_session_id="fixture-session",
            principal=frontdoor.default_manual_principal(),
            surface_registry=registry,
        )
        approved = frontdoor.approve_request(
            state_root=state_root,
            request_id="req-surface-fixture",
            human_action_id=classified["approval"]["human_action_id"],
            principal=frontdoor.make_principal(
                "human_operator", "fixture-human", authn_method="local_ui"
            ),
        )
        run = frontdoor.create_run(
            state_root=state_root,
            request_id="req-surface-fixture",
            run_id="",
            resume_policy="manual",
            principal=frontdoor.default_manual_principal(),
        )
        assert approved["decision"] == "ok"
        assert run["decision"] == "ok"
        record = frontdoor.read_json(
            frontdoor.request_path(state_root, "req-surface-fixture")
        )
        assert record["surface_identity"] == identity


def test_unknown_surface_is_rejected_before_request_creation() -> None:
    injected = bridge_payload("req-injected-surface", "codex")
    injected["surface_identity"] = {
        "frontend_kind": "codex",
        "assurance_state": "action_enforced",
    }
    validation_errors = frontdoor.validate_bridge_submit_payload(injected)
    assert any("forbidden_fields:surface_identity" in item for item in validation_errors)
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=bridge_payload("req-unknown-surface", "unknown-fixture"),
            )
        except frontdoor.FrontdoorError as exc:
            assert "surface_not_registered:unknown-fixture" in str(exc)
        else:
            raise AssertionError("unregistered surface was accepted")
        assert not frontdoor.request_path(state_root, "req-unknown-surface").exists()


def test_uncommissioned_registered_surface_is_advisory_and_suppressed() -> None:
    registry = fixture_registry(commissioned=False)
    identity = registry.identify("fixture").as_dict()
    assert identity["assurance_state"] == "advisory"
    assert identity["target_assurance_state"] == "ingress_enforced"
    assert identity["commissioned_claims"] == []
    assert identity["suppressed_claims"] == ["ingress_enforced"]
    with tempfile.TemporaryDirectory() as raw_tmp:
        projection = frontdoor.bridge_submit_request(
            state_root=Path(raw_tmp),
            payload=bridge_payload("req-uncommissioned-surface", "fixture"),
            surface_registry=registry,
        )
        assert projection["surface_identity"] == identity


def test_required_launcher_without_session_suppresses_current_claim() -> None:
    registry = surfaces.SurfaceRegistry(
        surfaces.load_registry(),
        assurance_registry=assurance.load_registry(),
        assurance_evaluator=lambda _profile_id, _checkout: {
            "decision": "allow",
            "effective_claims": ["action_enforced"],
        },
    )
    with_launch = registry.identify("codex", launch_session_present=True).as_dict()
    without_launch = registry.identify("codex", launch_session_present=False).as_dict()
    assert with_launch["assurance_state"] == "action_enforced"
    assert without_launch["assurance_state"] == "advisory"
    assert without_launch["commissioned_claims"] == []
    assert without_launch["suppressed_claims"] == ["action_enforced"]


def test_projection_and_replay_recheck_assurance_downgrade() -> None:
    commissioned = {"value": True}
    registry = fixture_registry(commissioned=commissioned)
    payload = bridge_payload("req-surface-downgrade", "fixture")
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        submitted = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=payload,
            surface_registry=registry,
        )
        assert submitted["surface_identity"]["assurance_state"] == "ingress_enforced"
        commissioned["value"] = False
        projection = frontdoor.bridge_read_projection(
            state_root=state_root,
            request_id="req-surface-downgrade",
            frontdoor="fixture",
            chat_session_id="",
            enforce_rate_limit=False,
            surface_registry=registry,
        )
        assert projection["surface_identity"]["assurance_state"] == "advisory"
        assert projection["surface_identity"]["commissioned_claims"] == []
        replayed = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=payload,
            surface_registry=registry,
        )
        assert replayed["replayed"] is True
        assert replayed["surface_identity"]["assurance_state"] == "advisory"


def test_stored_surface_identity_is_validated_with_explicit_legacy_support() -> None:
    registry = fixture_registry(commissioned=False)
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=bridge_payload("req-surface-record", "fixture"),
            surface_registry=registry,
        )
        path = frontdoor.request_path(state_root, "req-surface-record")
        record = frontdoor.read_json(path)
        record.pop("surface_identity")
        frontdoor.write_json(path, record)
        legacy_projection = frontdoor.bridge_read_projection(
            state_root=state_root,
            request_id="req-surface-record",
            frontdoor="fixture",
            chat_session_id="",
            enforce_rate_limit=False,
            surface_registry=registry,
        )
        assert legacy_projection["surface_identity"]["frontend_kind"] == "fixture"

        record["surface_identity"] = None
        frontdoor.write_json(path, record)
        try:
            frontdoor.bridge_read_projection(
                state_root=state_root,
                request_id="req-surface-record",
                frontdoor="fixture",
                chat_session_id="",
                enforce_rate_limit=False,
                surface_registry=registry,
            )
        except frontdoor.FrontdoorError as exc:
            assert "surface identity is invalid" in str(exc)
        else:
            raise AssertionError("malformed stored surface identity was accepted")


def test_http_surface_is_host_pinned() -> None:
    handler = object.__new__(frontdoor_server.Handler)
    handler.server = SimpleNamespace(frontend_kind="codex")
    assert handler._frontend_kind({}) == "codex"
    assert handler._frontend_kind({"frontdoor": "codex"}) == "codex"
    try:
        handler._frontend_kind({"frontdoor": "cursor"})
    except frontdoor.FrontdoorError as exc:
        assert "host-configured frontend" in str(exc)
    else:
        raise AssertionError("HTTP request overrode the host-pinned surface")


def main() -> None:
    tests = [
        test_shipped_registry_and_schema_are_closed_and_codex_first,
        test_fake_surface_registers_and_routes_through_deterministic_pipeline,
        test_unknown_surface_is_rejected_before_request_creation,
        test_uncommissioned_registered_surface_is_advisory_and_suppressed,
        test_required_launcher_without_session_suppresses_current_claim,
        test_projection_and_replay_recheck_assurance_downgrade,
        test_stored_surface_identity_is_validated_with_explicit_legacy_support,
        test_http_surface_is_host_pinned,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    main()
