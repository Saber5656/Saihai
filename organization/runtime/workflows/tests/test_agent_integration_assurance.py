#!/usr/bin/env python3
"""Static contract and fail-closed CLI tests for agent assurance v2."""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = WORKFLOW_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import agent_integration_assurance as assurance  # noqa: E402
import codex_main_agent_supervisor as supervisor  # noqa: E402
import scoped_worker_executor as scoped_worker  # noqa: E402


def registry() -> dict:
    return assurance.load_registry()


def by_id(profile_id: str) -> dict:
    return copy.deepcopy(
        next(item for item in registry()["profiles"] if item["profile_id"] == profile_id)
    )


def test_three_schemas_and_static_registry_are_separate_from_observations() -> None:
    target_schema = json.loads(assurance.SCHEMA_PATH.read_text(encoding="utf-8"))
    evidence_schema = json.loads(assurance.EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    attestation_schema = json.loads(
        assurance.ATTESTATION_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    assert target_schema["title"] == "Agent Integration Assurance Target Registry"
    assert evidence_schema["title"] == "Agent Integration Host Evidence"
    assert attestation_schema["title"] == "Agent Integration Host Attestation"
    assert target_schema["additionalProperties"] is False
    assert evidence_schema["additionalProperties"] is False
    assert attestation_schema["additionalProperties"] is False
    text = json.dumps(registry(), sort_keys=True)
    assert "target_level" not in text
    assert '"attestation"' not in text
    assert "observed_digest" not in text
    assert assurance.validate_registry(registry()) == []


def test_shipped_profiles_are_truthful_and_include_worker() -> None:
    profiles = {item["profile_id"]: item for item in registry()["profiles"]}
    assert set(profiles) == {
        "codex-main-agent-a-prime",
        "claude-main-agent-advisory",
        "cursor-main-agent-candidate",
        "grok-main-agent-unavailable",
        "codex-scoped-worker",
    }
    codex_frontend = profiles["codex-main-agent-a-prime"]
    assert codex_frontend["target_claims"] == ["action_enforced"]
    assert codex_frontend["transports"] == ["cli", "mcp", "native_policy"]
    assert "root-owned Saihai launcher" in codex_frontend["rationale"]
    assert "Codex App and IDE surfaces are outside this claim" in codex_frontend["rationale"]
    assert profiles["claude-main-agent-advisory"]["target_claims"] == []
    assert profiles["cursor-main-agent-candidate"]["integration_state"] == "candidate"
    assert profiles["grok-main-agent-unavailable"]["integration_state"] == "unavailable"
    worker = profiles["codex-scoped-worker"]
    assert worker["target_claims"] == ["managed_worker"]
    assert worker["surface_role"] == "bounded_worker"
    assert worker["operation_requirements"]["network_egress"] == "denied"
    assert worker["operation_requirements"]["filesystem_write"] == "capability_scoped"


def test_claims_are_orthogonal_not_ranked_levels() -> None:
    action_only = by_id("codex-main-agent-a-prime")
    action_only["target_claims"] = ["action_enforced"]
    value = registry()
    value["profiles"] = [action_only]
    assert assurance.validate_registry(value) == []
    assert "LEVELS" not in assurance.__dict__

    invalid = copy.deepcopy(action_only)
    invalid["target_claims"] = ["action_enforced", "managed_worker"]
    value["profiles"] = [invalid]
    errors = assurance.validate_registry(value)
    assert any("managed_worker_is_independent_worker_surface" in item for item in errors)


def test_advisory_is_computed_only_for_empty_targets() -> None:
    evaluated = assurance.evaluate_registry(registry())
    profiles = {item["profile_id"]: item for item in evaluated["profiles"]}
    claude = profiles["claude-main-agent-advisory"]
    assert claude["decision"] == "allow"
    assert claude["effective_mode"] == "advisory"
    assert claude["effective_claims"] == []

    for profile_id in (
        "codex-main-agent-a-prime",
        "cursor-main-agent-candidate",
        "grok-main-agent-unavailable",
        "codex-scoped-worker",
    ):
        item = profiles[profile_id]
        assert item["effective_mode"] is None
        assert item["decision"] == "suppress"
    assert "advisory" not in assurance.CLAIMS


def test_every_action_claim_requires_all_twelve_operation_keys() -> None:
    assert len(assurance.ACTION_OPERATIONS) == 12
    profile = by_id("codex-main-agent-a-prime")
    keys = assurance._claim_required_keys("action_enforced")
    denial_operations = {
        operation
        for evidence_type, claim, operation in keys
        if evidence_type == "direct_action_denial" and claim == "action_enforced"
    }
    assert denial_operations == set(assurance.ACTION_OPERATIONS)
    assert ("gateway_positive_path", "action_enforced", None) in keys
    assert assurance._required_evidence_keys(profile).issuperset(keys)


def test_registry_rejects_weakened_operation_and_unknown_fields() -> None:
    value = registry()
    profile = value["profiles"][0]
    profile["operation_requirements"]["credential_access"] = "unverified"
    profile["observed_attestation"] = {"status": "passed"}
    errors = assurance.validate_registry(value)
    assert any("credential_access:denied_required" in item for item in errors)
    assert any("observed_attestation:unexpected" in item for item in errors)


def test_informational_report_and_compat_evaluate_do_not_gate_by_exit_code() -> None:
    script = Path(assurance.__file__)
    for command in ("report", "evaluate"):
        result = subprocess.run(
            [sys.executable, str(script), command, "--profile", "codex-main-agent-a-prime"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["decision"] == "suppress"
        assert payload["profiles"][0]["effective_mode"] is None


def test_require_is_the_only_claim_cli_gate_and_fails_closed() -> None:
    script = Path(assurance.__file__)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "require",
            "--profile",
            "codex-main-agent-a-prime",
            "--claim",
            "action_enforced",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["decision"] == "suppress"
    assert payload["reasons"]


def test_production_root_is_fixed_and_no_cli_override_exists() -> None:
    assert str(assurance.production_assurance_root()) == (
        "/Library/Application Support/Saihai/Assurance"
    )
    script = Path(assurance.__file__)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--state-root" not in result.stdout
    assert "SAIHAI_ASSURANCE_ROOT" not in Path(assurance.__file__).read_text(encoding="utf-8")


def test_import_order_has_no_frontdoor_cycle() -> None:
    for source in (
        "import frontdoor_orchestrator; import agent_integration_assurance",
        "import agent_integration_assurance; import frontdoor_orchestrator",
    ):
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_execution_binding_and_launch_session_schemas_match_normalizers() -> None:
    evidence_schema = json.loads(
        assurance.EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    attestation_schema = json.loads(
        assurance.ATTESTATION_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    for schema in (evidence_schema, attestation_schema):
        assert set(schema["$defs"]["workerExecutionBinding"]["required"]) == set(
            scoped_worker.WORKER_RUNTIME_BINDING_FIELDS
        )
        assert (
            schema["$defs"]["workerExecutionBinding"]["additionalProperties"]
            is False
        )
        assert set(schema["$defs"]["launchSession"]["required"]) == set(
            supervisor.SESSION_RECORD_FIELDS
        )
        assert schema["$defs"]["launchSession"]["properties"][
            "launch_session_version"
        ] == {"const": supervisor.SESSION_VERSION}
    worker = by_id("codex-scoped-worker")
    validator = worker["execution_binding_validator"]
    assert validator["module"] == "scoped_worker_executor"
    assert validator["function"] == "verify_worker_runtime_binding"
    assert validator["fields_constant"] == "WORKER_RUNTIME_BINDING_FIELDS"
    assert assurance._execution_binding_validator_errors(
        validator, "$.execution_binding_validator"
    ) == []


def test_executable_validator_digest_binds_exact_module_bytes() -> None:
    frontend = by_id("codex-main-agent-a-prime")
    validator = frontend["launch_validator"]
    assert validator["validator_digest"] == assurance._module_validator_digest(
        validator["module"]
    )
    original = supervisor.__file__
    assert isinstance(original, str)
    with tempfile.TemporaryDirectory() as temporary:
        drifted = Path(temporary).resolve() / "codex_main_agent_supervisor.py"
        drifted.write_bytes(Path(original).read_bytes() + b"\n# drift\n")
        supervisor.__file__ = str(drifted)
        try:
            errors = assurance._launch_validator_errors(
                validator, "$.launch_validator"
            )
        finally:
            supervisor.__file__ = original
    assert "$.launch_validator.validator_digest:module_mismatch" in errors


def test_production_namespace_modes_are_exact_and_drift_suppresses() -> None:
    assert assurance._production_namespace_mode(
        ("commissioning", "profile"), directory=True
    ) == 0o700
    assert assurance._production_namespace_mode(
        ("commissioning", "profile", "grant.json"), directory=False
    ) == 0o600
    assert assurance._production_namespace_mode(
        ("generations", "profile"), directory=True
    ) == 0o755
    assert assurance._production_namespace_mode(
        ("active", "profile.json"), directory=False
    ) == 0o644
    assert assurance._production_namespace_mode(
        ("epochs", "profile.json"), directory=False
    ) == 0o644
    with tempfile.TemporaryDirectory() as temporary:
        artifact = Path(temporary) / "artifact.json"
        artifact.write_text("{}\n", encoding="utf-8")
        artifact.chmod(0o600)
        try:
            assurance._check_production_namespace_mode(
                artifact.stat(),
                ("generations", "profile", "artifact.json"),
                directory=False,
                policy=assurance.TrustPolicy.production(),
            )
        except assurance.EvidenceTrustError as exc:
            assert exc.reason == "trusted_path_mode_mismatch"
        else:
            raise AssertionError("public mode drift must suppress")


def test_unexpected_live_network_or_dynamic_tool_suppresses() -> None:
    profile = by_id("codex-main-agent-a-prime")
    validator = profile["tool_validator"]
    base = {
        "inventory_version": "1",
        "model_identifier": "live-fixture",
        **{
            name: list(validator["classification_requirements"][name]["values"])
            for name in assurance.TOOL_CATEGORY_NAMES
        },
    }
    manifest = {
        "tool_contract": {
            "enabled_tools": list(validator["manifest_enabled_tools"])
        }
    }
    for category, tool in (
        ("dynamic_tools", "web__run"),
        ("active_external_mutation_tools", "network"),
    ):
        classification = copy.deepcopy(base)
        classification[category].append(tool)
        classification[category].sort()
        inventory = sorted(
            item
            for name in assurance.TOOL_CATEGORY_NAMES
            for item in classification[name]
        )
        try:
            assurance._validate_tool_classification(
                classification,
                inventory=inventory,
                profile=profile,
                manifest=manifest,
            )
        except assurance.AssuranceGateError as exc:
            assert "exact_mismatch" in exc.reason
        else:
            raise AssertionError(f"unexpected {tool} must suppress")


def test_worker_weak_denial_facts_are_a_promotion_blocker() -> None:
    worker = by_id("codex-scoped-worker")
    try:
        assurance._require_profile_promotion_ready(worker)
    except assurance.AssuranceGateError as exc:
        assert exc.reason == "worker_denial_facts_not_promotable"
        assert exc.reasons == (
            "worker_denial_fact_unproven:credential_access",
            "worker_denial_fact_unproven:external_mutation",
            "worker_denial_fact_unproven:git_commit",
            "worker_denial_fact_unproven:git_push",
        )
    else:
        raise AssertionError("weak worker denial facts must never promote")

    assurance._require_profile_promotion_ready(by_id("codex-main-agent-a-prime"))


def test_unbound_claim_verification_has_only_two_informational_callers() -> None:
    target = "_verify_claim_unbound_informational"
    callsites: set[tuple[str, tuple[str, ...]]] = set()

    class CallVisitor(ast.NodeVisitor):
        def __init__(self, filename: str) -> None:
            self.filename = filename
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            if name == target:
                callsites.add((self.filename, tuple(self.stack)))
            self.generic_visit(node)

    for path in sorted(SCRIPT_DIR.glob("*.py")):
        CallVisitor(path.name).visit(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    assert callsites == {
        ("agent_integration_assurance.py", ("evaluate_profile",)),
        (
            "agent_integration_attester.py",
            ("seal_attestation", "informational_claim_records"),
        ),
    }


def test_credential_fact_is_narrowly_named_for_known_codex_auth_paths() -> None:
    assert "known_codex_auth_paths_denied" in assurance.TOOL_POLICY_FACT_FIELDS
    assert "credential_paths_denied" not in assurance.TOOL_POLICY_FACT_FIELDS
    rationale = by_id("codex-main-agent-a-prime")["rationale"]
    assert "known legacy and dedicated CODEX_HOME auth.json paths" in rationale
    assert "does not claim that every user-readable secret file" in rationale


def test_generation_manifest_and_epoch_contracts_are_exact() -> None:
    assert "deployment_epoch_id" in assurance.GENERATION_MANIFEST_FIELDS
    assert assurance.DEPLOYMENT_EPOCH_FIELDS == {
        "epoch_version",
        "profile_id",
        "epoch_id",
        "state",
        "operation",
        "transaction_id",
        "previous_epoch_id",
        "rotated_at",
        "finalized_at",
    }


def _live_snapshot_claim_fixture() -> tuple[
    dict,
    assurance._VerifiedAttestation,
    assurance.LiveClaimContext,
    assurance._LiveClaimSnapshot,
]:
    profile = by_id("codex-main-agent-a-prime")
    argv = ["/trusted/codex", "--fixed"]
    digest = "sha256:" + "a" * 64
    context = assurance.LiveClaimContext(
        subject_pid=4101,
        process_start_token="subject-start",
        supervisor_pid=4100,
        supervisor_start_token="supervisor-start",
        executable_realpath=argv[0],
        launch_argv_digest=assurance._stable_digest(argv),
        profile_realpath="/trusted/profile.toml",
        profile_digest="sha256:" + "b" * 64,
        checkout_identity_digest="sha256:" + "c" * 64,
    )
    session = {
        "subject_pid": context.subject_pid,
        "process_start_token": context.process_start_token,
        "supervisor_pid": context.supervisor_pid,
        "supervisor_start_token": context.supervisor_start_token,
        "native_realpath": context.executable_realpath,
        "launch_argv_digest": context.launch_argv_digest,
        "profile_realpath": context.profile_realpath,
        "profile_digest": context.profile_digest,
        "checkout_identity_digest": context.checkout_identity_digest,
        "session_kind": "commissioning",
    }
    verified = assurance._VerifiedAttestation(
        payload={
            "bindings": {
                "configuration_digest": digest,
                "runtime_binary_digest": digest,
                "tool_inventory_digest": digest,
                "checkout_digest": digest,
            }
        },
        digest=digest,
        evidence=(),
        evidence_digests=(),
        claim_results={"action_enforced": "pass"},
        checkout_binding={"identity_digest": context.checkout_identity_digest},
        launch_session=session,
        runtime_binding={"argv": argv},
        worker_execution_binding=None,
        generation_id="generation-fixture",
    )
    snapshot = assurance._LiveClaimSnapshot(
        process_start_token=context.process_start_token,
        supervisor_start_token=context.supervisor_start_token,
        parent_pid=context.supervisor_pid,
        executable_realpath=context.executable_realpath,
        argv_digest=assurance.argv_vector_digest(argv),
    )
    return profile, verified, context, snapshot


def _darwin_procargs2_fixture(
    argv: list[str],
    *,
    executable: str = "/usr/bin/python3",
    padding: int = 2,
    environment: bytes = b"PATH=/usr/bin\0",
) -> bytes:
    return (
        len(argv).to_bytes(4, sys.byteorder, signed=True)
        + executable.encode("utf-8")
        + b"\0"
        + (b"\0" * padding)
        + b"\0".join(item.encode("utf-8") for item in argv)
        + b"\0"
        + environment
    )


def test_argv_vector_digest_preserves_space_boundaries() -> None:
    left = ["/tmp/alpha beta", "gamma"]
    right = ["/tmp/alpha", "beta gamma"]
    assert " ".join(left) == " ".join(right)
    assert assurance.argv_vector_digest(left) != assurance.argv_vector_digest(right)

    _profile, _verified, _context, baseline = _live_snapshot_claim_fixture()
    expected = replace(baseline, argv_digest=assurance.argv_vector_digest(left))
    observed = replace(baseline, argv_digest=assurance.argv_vector_digest(right))
    try:
        assurance._require_live_claim_snapshots(expected, observed, observed)
    except assurance.AssuranceGateError as exc:
        assert exc.reason == "claim_live_argv_mismatch"
    else:
        raise AssertionError("space-boundary collision must suppress")


def test_fixed_vector_cannot_equal_one_joined_element() -> None:
    fixed = ["/trusted/codex"] + [f"--fixed-{index}" for index in range(81)]
    collapsed = [" ".join(fixed)]
    assert len(fixed) == 82
    assert assurance.argv_vector_digest(fixed) != assurance.argv_vector_digest(collapsed)


def test_darwin_procargs2_parser_preserves_exact_vector() -> None:
    argv = ["/tmp/alpha beta", "gamma", "", "日本語"]
    raw = _darwin_procargs2_fixture(argv)
    assert assurance._parse_darwin_procargs2(raw) == tuple(argv)

    with_environment = _darwin_procargs2_fixture(
        ["/trusted/codex", "--fixed"],
        environment=b"LOOKS=like-an-argument\0SECOND=value\0",
    )
    assert assurance._parse_darwin_procargs2(with_environment) == (
        "/trusted/codex",
        "--fixed",
    )


def test_darwin_procargs2_parser_fails_closed_on_malformed_data() -> None:
    malformed_cases = (
        b"",
        (0).to_bytes(4, sys.byteorder, signed=True) + b"x\0x\0",
        (1).to_bytes(4, sys.byteorder, signed=True) + b"\0x\0",
        (1).to_bytes(4, sys.byteorder, signed=True) + b"no-terminator",
    )
    for raw in malformed_cases:
        try:
            assurance._parse_darwin_procargs2(raw)
        except assurance.AssuranceGateError as exc:
            assert exc.reason == "claim_live_argv_malformed"
        else:
            raise AssertionError("malformed KERN_PROCARGS2 data must suppress")

    truncated = (
        (2).to_bytes(4, sys.byteorder, signed=True)
        + b"/usr/bin/codex\0\0/trusted/codex\0missing-terminator"
    )
    try:
        assurance._parse_darwin_procargs2(truncated)
    except assurance.AssuranceGateError as exc:
        assert exc.reason == "claim_live_argv_truncated"
    else:
        raise AssertionError("truncated KERN_PROCARGS2 argv must suppress")


def test_linux_cmdline_parser_preserves_empty_arguments_and_trailing_nul() -> None:
    assert assurance._parse_linux_cmdline(b"a\0\0b\0") == ("a", "", "b")
    assert assurance._parse_linux_cmdline(b"a b\0c\0") == ("a b", "c")

    for raw, reason in (
        (b"a\0b", "claim_live_argv_malformed"),
        (b"\0", "claim_live_argv_malformed"),
        (b"", "claim_live_argv_unavailable"),
    ):
        try:
            assurance._parse_linux_cmdline(raw)
        except assurance.AssuranceGateError as exc:
            assert exc.reason == reason
        else:
            raise AssertionError("invalid Linux cmdline must suppress")


def test_live_argv_path_has_no_lossy_join_or_ps_fallback() -> None:
    source = Path(assurance.__file__).read_text(encoding="utf-8")
    assert "def _live_process_args" not in source
    assert '"-ww", "-o", "args="' not in source
    assert '_stable_digest(" ".join' not in source


def _run_snapshot_claim(
    snapshots: list[object],
) -> tuple[assurance.VerifiedClaim, list[str]]:
    profile, verified, context, _expected = _live_snapshot_claim_fixture()
    events: list[str] = []
    remaining = iter(snapshots)
    original_resolve = assurance._resolve_claim_verification
    original_capture = assurance._capture_live_claim_snapshot

    def fake_resolve(*_args, **_kwargs):
        events.append("verification")
        return profile, verified

    def fake_capture(_context):
        events.append(f"snapshot-{events.count('verification') + 1}")
        value = next(remaining)
        if isinstance(value, Exception):
            raise value
        return value

    assurance._resolve_claim_verification = fake_resolve
    assurance._capture_live_claim_snapshot = fake_capture
    try:
        result = assurance.require_claim(
            Path("/unused-state-root"),
            profile["profile_id"],
            "action_enforced",
            live_context=context,
        )
    finally:
        assurance._resolve_claim_verification = original_resolve
        assurance._capture_live_claim_snapshot = original_capture
    return result, events


def test_require_claim_read_check_rechecks_complete_live_snapshot() -> None:
    _profile, _verified, context, expected = _live_snapshot_claim_fixture()
    result, events = _run_snapshot_claim([expected, expected])
    assert events == ["snapshot-1", "verification", "snapshot-2"]
    assert result.subject_binding_digest == assurance._stable_digest(context.as_dict())


def test_second_live_snapshot_drift_each_field_suppresses() -> None:
    _profile, _verified, _context, expected = _live_snapshot_claim_fixture()
    cases = (
        ("process_start_token", "changed-subject", "claim_live_start_token_mismatch"),
        (
            "supervisor_start_token",
            "changed-supervisor",
            "claim_live_start_token_mismatch",
        ),
        ("parent_pid", expected.parent_pid + 1, "claim_live_parent_mismatch"),
        (
            "executable_realpath",
            "/changed/codex",
            "claim_live_executable_mismatch",
        ),
        ("argv_digest", "sha256:" + "d" * 64, "claim_live_argv_mismatch"),
    )
    for field, value, reason in cases:
        try:
            _run_snapshot_claim([expected, replace(expected, **{field: value})])
        except assurance.AssuranceGateError as exc:
            assert exc.reason == reason
        else:
            raise AssertionError(f"second snapshot drift in {field} must suppress")


def test_first_stale_snapshot_cannot_be_healed_by_matching_second_read() -> None:
    _profile, _verified, _context, expected = _live_snapshot_claim_fixture()
    stale_cases = (
        ("process_start_token", "stale-subject", "claim_live_start_token_mismatch"),
        (
            "supervisor_start_token",
            "stale-supervisor",
            "claim_live_start_token_mismatch",
        ),
        ("parent_pid", expected.parent_pid + 1, "claim_live_parent_mismatch"),
        (
            "executable_realpath",
            "/stale/codex",
            "claim_live_executable_mismatch",
        ),
        ("argv_digest", "sha256:" + "e" * 64, "claim_live_argv_mismatch"),
    )
    for field, value, reason in stale_cases:
        try:
            _run_snapshot_claim([replace(expected, **{field: value}), expected])
        except assurance.AssuranceGateError as exc:
            assert exc.reason == reason
        else:
            raise AssertionError(f"stale first snapshot in {field} must suppress")


def test_unavailable_first_or_second_live_snapshot_suppresses() -> None:
    _profile, _verified, _context, expected = _live_snapshot_claim_fixture()
    for snapshots in (
        [assurance.AssuranceGateError("claim_live_argv_unavailable"), expected],
        [expected, assurance.AssuranceGateError("claim_live_argv_unavailable")],
    ):
        try:
            _run_snapshot_claim(snapshots)
        except assurance.AssuranceGateError as exc:
            assert exc.reason == "claim_live_argv_unavailable"
        else:
            raise AssertionError("an unavailable live snapshot must suppress")


def main() -> None:
    tests = [
        test_three_schemas_and_static_registry_are_separate_from_observations,
        test_shipped_profiles_are_truthful_and_include_worker,
        test_claims_are_orthogonal_not_ranked_levels,
        test_advisory_is_computed_only_for_empty_targets,
        test_every_action_claim_requires_all_twelve_operation_keys,
        test_registry_rejects_weakened_operation_and_unknown_fields,
        test_informational_report_and_compat_evaluate_do_not_gate_by_exit_code,
        test_require_is_the_only_claim_cli_gate_and_fails_closed,
        test_production_root_is_fixed_and_no_cli_override_exists,
        test_import_order_has_no_frontdoor_cycle,
        test_execution_binding_and_launch_session_schemas_match_normalizers,
        test_executable_validator_digest_binds_exact_module_bytes,
        test_production_namespace_modes_are_exact_and_drift_suppresses,
        test_unexpected_live_network_or_dynamic_tool_suppresses,
        test_worker_weak_denial_facts_are_a_promotion_blocker,
        test_unbound_claim_verification_has_only_two_informational_callers,
        test_credential_fact_is_narrowly_named_for_known_codex_auth_paths,
        test_generation_manifest_and_epoch_contracts_are_exact,
        test_argv_vector_digest_preserves_space_boundaries,
        test_fixed_vector_cannot_equal_one_joined_element,
        test_darwin_procargs2_parser_preserves_exact_vector,
        test_darwin_procargs2_parser_fails_closed_on_malformed_data,
        test_linux_cmdline_parser_preserves_empty_arguments_and_trailing_nul,
        test_live_argv_path_has_no_lossy_join_or_ps_fallback,
        test_require_claim_read_check_rechecks_complete_live_snapshot,
        test_second_live_snapshot_drift_each_field_suppresses,
        test_first_stale_snapshot_cannot_be_healed_by_matching_second_read,
        test_unavailable_first_or_second_live_snapshot_suppresses,
    ]
    for test in tests:
        test()
    print(f"agent integration assurance tests passed: {len(tests)}")


if __name__ == "__main__":
    main()
