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
    descriptor: dict[str, Any] | None = None,
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
    registered = registry.register(descriptor or fake_descriptor())
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


class SimulatedBridgeSubmitCrash(RuntimeError):
    pass


def crash_bridge_submit_after_request_write(
    *,
    state_root: Path,
    payload: dict[str, Any],
    registry: surfaces.SurfaceRegistry,
) -> dict[str, Any]:
    idempotency_file = frontdoor.idempotency_path(
        state_root,
        str(payload["idempotency_key"]),
    )
    original_write_json = frontdoor.write_json

    def crash_before_idempotency_write(path: Path, value: dict[str, Any]) -> None:
        if path == idempotency_file:
            raise SimulatedBridgeSubmitCrash("crash before idempotency record write")
        original_write_json(path, value)

    frontdoor.write_json = crash_before_idempotency_write
    try:
        frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=payload,
            frontend_kind="fixture",
            surface_registry=registry,
        )
    except SimulatedBridgeSubmitCrash:
        pass
    else:
        raise AssertionError("failure injection did not interrupt bridge submit")
    finally:
        frontdoor.write_json = original_write_json

    assert frontdoor.request_path(state_root, str(payload["request_id"])).exists()
    assert not idempotency_file.exists()
    transaction_files = list((state_root / "bridge-transactions").glob("*.json"))
    assert len(transaction_files) == 1
    return frontdoor.read_json(transaction_files[0])


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
    assert "descriptor_digest" in projection_schema["$defs"]["surface_identity"]["required"]


def test_fake_surface_registers_and_routes_through_deterministic_pipeline() -> None:
    registry = fixture_registry(commissioned=True)
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        submitted = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=bridge_payload("req-surface-fixture", "fixture"),
            frontend_kind="fixture",
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
                frontend_kind="unknown-fixture",
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
            frontend_kind="fixture",
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
            frontend_kind="fixture",
            surface_registry=registry,
        )
        assert submitted["surface_identity"]["assurance_state"] == "ingress_enforced"
        descriptor_digest = submitted["surface_identity"]["descriptor_digest"]
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
        assert projection["surface_identity"]["descriptor_digest"] == descriptor_digest
        replayed = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=payload,
            frontend_kind="fixture",
            surface_registry=registry,
        )
        assert replayed["replayed"] is True
        assert replayed["surface_identity"]["assurance_state"] == "advisory"


def test_stored_surface_identity_is_validated_with_explicit_legacy_support() -> None:
    """Compatibility proof for records created before surface_identity existed."""

    registry = fixture_registry(commissioned=False)
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=bridge_payload("req-surface-record", "fixture"),
            frontend_kind="fixture",
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


def test_core_requires_host_owned_surface_and_rejects_payload_conflict() -> None:
    registry = fixture_registry(commissioned=False)
    payload = bridge_payload("req-host-context", "fixture")
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=payload,
                surface_registry=registry,
            )
        except TypeError as exc:
            assert "frontend_kind" in str(exc)
        else:
            raise AssertionError("payload-only surface context was accepted")

        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=payload,
                frontend_kind="codex",
                surface_registry=registry,
            )
        except frontdoor.FrontdoorError as exc:
            assert "payload frontdoor conflicts with host frontend_kind" in str(exc)
        else:
            raise AssertionError("conflicting client surface was accepted")
        assert not frontdoor.request_path(state_root, "req-host-context").exists()


def test_static_descriptor_drift_fails_closed() -> None:
    original = fixture_registry(commissioned=False)
    payload = bridge_payload("req-static-drift", "fixture")
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        submitted = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=payload,
            frontend_kind="fixture",
            surface_registry=original,
        )
        original_digest = submitted["surface_identity"]["descriptor_digest"]

        mutations = (
            ("launcher_id", ("launcher", "launcher_id"), "fixture-launcher-v2"),
            ("requirements_id", ("requirements", "requirements_id"), "fixture-requirements-v2"),
            ("verifier_module", ("launcher", "verifier_module"), "fixture_verifier"),
            ("verifier_class", ("launcher", "verifier_class"), "FixtureVerifier"),
        )
        for label, path, value in mutations:
            descriptor = fake_descriptor()
            descriptor[path[0]][path[1]] = value
            drifted = fixture_registry(commissioned=False, descriptor=descriptor)
            assert drifted.identify("fixture").descriptor_digest != original_digest
            try:
                frontdoor.bridge_read_projection(
                    state_root=state_root,
                    request_id="req-static-drift",
                    frontdoor="fixture",
                    chat_session_id="",
                    enforce_rate_limit=False,
                    surface_registry=drifted,
                )
            except frontdoor.FrontdoorError as exc:
                assert "surface contract drifted" in str(exc), label
            else:
                raise AssertionError(f"{label} descriptor drift was accepted")

        replay_descriptor = fake_descriptor()
        replay_descriptor["launcher"]["launcher_id"] = "fixture-launcher-replay"
        replay_registry = fixture_registry(
            commissioned=False,
            descriptor=replay_descriptor,
        )
        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=payload,
                frontend_kind="fixture",
                surface_registry=replay_registry,
            )
        except frontdoor.FrontdoorError as exc:
            assert "idempotency key surface descriptor drifted" in str(exc)
        else:
            raise AssertionError("static descriptor drift replay was accepted")

        different_request = bridge_payload(
            "req-static-drift-second",
            "fixture",
        )
        different_request["idempotency_key"] = payload["idempotency_key"]
        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=different_request,
                frontend_kind="fixture",
                surface_registry=replay_registry,
            )
        except frontdoor.FrontdoorError as exc:
            assert "idempotency key surface descriptor drifted" in str(exc)
        else:
            raise AssertionError("descriptor drift split the idempotency namespace")
        assert not frontdoor.request_path(
            state_root,
            "req-static-drift-second",
        ).exists()
        assert len(list((state_root / "idempotency").glob("*.json"))) == 1


def test_bridge_crash_recovery_uses_raw_key_index_and_deduplicates() -> None:
    registry = fixture_registry(commissioned=False)
    payload = bridge_payload("req-crash-recovery", "fixture")
    raw_key = str(payload["idempotency_key"])
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        transaction = crash_bridge_submit_after_request_write(
            state_root=state_root,
            payload=payload,
            registry=registry,
        )
        raw_path_digest = frontdoor.idempotency_key_digest(raw_key)
        bound_contract_digest = frontdoor.bridge_idempotency_key_digest(
            raw_key,
            registry.identify("fixture").as_dict(),
        )
        normal_path = frontdoor.idempotency_path(state_root, raw_key)
        journal_path = frontdoor.idempotency_path_from_digest(
            state_root,
            str(transaction["idempotency_path_digest"]),
        )
        assert transaction["idempotency_path_digest"] == raw_path_digest
        assert transaction["idempotency_key_digest"] == bound_contract_digest
        assert raw_path_digest != bound_contract_digest
        assert journal_path == normal_path

        assert frontdoor.recover_bridge_submit_transactions(state_root) == 1
        assert normal_path.exists()
        assert not frontdoor.idempotency_path_from_digest(
            state_root,
            bound_contract_digest,
        ).exists()

        replayed = frontdoor.bridge_submit_request(
            state_root=state_root,
            payload=payload,
            frontend_kind="fixture",
            surface_registry=registry,
        )
        assert replayed["replayed"] is True
        assert replayed["request_id"] == payload["request_id"]

        duplicate = bridge_payload("req-crash-recovery-duplicate", "fixture")
        duplicate["idempotency_key"] = raw_key
        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=duplicate,
                frontend_kind="fixture",
                surface_registry=registry,
            )
        except frontdoor.FrontdoorError as exc:
            assert "idempotency conflict" in str(exc)
        else:
            raise AssertionError("recovered idempotency key accepted a second request")
        assert not frontdoor.request_path(
            state_root,
            str(duplicate["request_id"]),
        ).exists()
        assert len(list((state_root / "idempotency").glob("*.json"))) == 1


def test_bridge_crash_recovery_descriptor_drift_fails_closed() -> None:
    original = fixture_registry(commissioned=False)
    payload = bridge_payload("req-crash-drift", "fixture")
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp)
        crash_bridge_submit_after_request_write(
            state_root=state_root,
            payload=payload,
            registry=original,
        )
        assert frontdoor.recover_bridge_submit_transactions(state_root) == 1

        drifted_descriptor = fake_descriptor()
        drifted_descriptor["launcher"]["launcher_id"] = "fixture-launcher-drifted"
        drifted = fixture_registry(
            commissioned=False,
            descriptor=drifted_descriptor,
        )
        replay = bridge_payload("req-crash-drift-replay", "fixture")
        replay["idempotency_key"] = payload["idempotency_key"]
        try:
            frontdoor.bridge_submit_request(
                state_root=state_root,
                payload=replay,
                frontend_kind="fixture",
                surface_registry=drifted,
            )
        except frontdoor.FrontdoorError as exc:
            assert "idempotency key surface descriptor drifted" in str(exc)
        else:
            raise AssertionError("descriptor drift split the recovered idempotency namespace")
        assert not frontdoor.request_path(
            state_root,
            str(replay["request_id"]),
        ).exists()
        assert len(list((state_root / "idempotency").glob("*.json"))) == 1


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
        test_core_requires_host_owned_surface_and_rejects_payload_conflict,
        test_static_descriptor_drift_fails_closed,
        test_bridge_crash_recovery_uses_raw_key_index_and_deduplicates,
        test_bridge_crash_recovery_descriptor_drift_fails_closed,
        test_http_surface_is_host_pinned,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}))


if __name__ == "__main__":
    main()
