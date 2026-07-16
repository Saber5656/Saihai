#!/usr/bin/env python3
"""Contract and end-to-end tests for the bridge-only MCP server."""

from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = ROOT / "organization/runtime/workflows/scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import main_agent_bridge_mcp as mcp


class FixtureLaunchSessionVerifier:
    def verify_parent_session(self, **arguments: Any) -> dict[str, Any]:
        import codex_main_agent_deployment as deployment

        checkout = arguments["checkout_identity"]
        native = "/usr/bin/true"
        record: dict[str, Any] = {
            "launch_session_version": "2",
            "session_id": "launch-fixture-mcp",
            "deployment_id": arguments["profile_id"],
            "profile_id": arguments["profile_id"],
            "principal_id": arguments["principal_id"],
            "workspace_id": arguments["workspace_id"],
            "subject_pid": arguments["subject_pid"],
            "process_start_token": "proc-" + "1" * 64,
            "native_realpath": native,
            "native_digest": "sha256:" + "2" * 64,
            "profile_realpath": str(Path(__file__).resolve()),
            "profile_digest": "sha256:" + "3" * 64,
            "launch_argv_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    deployment.native_codex_argv(native),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "checkout_realpath": checkout["checkout_realpath"],
            "checkout_identity_digest": checkout["identity_digest"],
            "issued_at": "2026-01-01T00:00:00Z",
            "valid_until": "2099-01-01T00:00:00Z",
            "status": "active",
            "session_kind": "standard",
            "commissioning_launch_reference": None,
            "commissioning_launch_digest": None,
            "supervisor_pid": os.getpid(),
            "supervisor_start_token": "proc-" + "5" * 64,
            "record_reference": "launch-sessions/launch-fixture-mcp.json",
            "record_digest": "sha256:" + "0" * 64,
        }
        material = {key: record[key] for key in sorted(set(record) - {"record_digest"})}
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        record["record_digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return record


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


@contextmanager
def canonical_temp_state(
    *,
    managed_primary: Path = ROOT,
    checkout_root: Path = ROOT,
) -> Iterator[Path]:
    catalog = mcp.frontdoor.host_state_root.DIRECTORY_CATALOG
    previous = dict(catalog)
    previous_verifier = mcp.LAUNCH_SESSION_VERIFIER
    previous_managed_primary = mcp.MANAGED_PRIMARY_ROOT
    previous_checkout_root = mcp.CHECKOUT_ROOT
    with tempfile.TemporaryDirectory() as raw_tmp:
        state_root = Path(raw_tmp).resolve()
        catalog.clear()
        catalog["SAIHAI_ORCH_STATE_ROOT"] = str(state_root)
        mcp.LAUNCH_SESSION_VERIFIER = FixtureLaunchSessionVerifier()
        mcp.MANAGED_PRIMARY_ROOT = managed_primary
        mcp.CHECKOUT_ROOT = checkout_root
        try:
            assert_equal(mcp._canonical_state_root(), state_root, "canonical test state root")
            yield state_root
        finally:
            catalog.clear()
            catalog.update(previous)
            mcp.LAUNCH_SESSION_VERIFIER = previous_verifier
            mcp.MANAGED_PRIMARY_ROOT = previous_managed_primary
            mcp.CHECKOUT_ROOT = previous_checkout_root


def rpc(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    response = mcp.dispatch(message)
    assert isinstance(response, dict), f"request {method} returned no response"
    return response


def call_tool(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = rpc(
        request_id,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    assert "error" not in response, response
    result = response["result"]
    assert isinstance(result, dict)
    return result


def submit_arguments(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace": mcp.WORKSPACE_ID,
        "task_id": "TSK-mcp-bridge",
        "request_id": "req-mcp-bridge",
        "prompt": "Prepare a bounded documentation proposal. PRIVATE_PROMPT_MARKER",
        "refs": ["README.md"],
        "allowed_paths": ["organization/runtime/workflows/scripts"],
        "chat_session_id": "codex-thread-mcp",
        "idempotency_key": "mcp-bridge-idempotency-key",
    }
    payload.update(overrides)
    return payload


def test_initialize_inventory_and_annotations_are_exact() -> None:
    parsed = mcp.parse_args(
        [
            "--frontdoor",
            "claude",
            "--principal-id",
            "claude-main-agent-a-prime",
            "--workspace-id",
            mcp.WORKSPACE_ID,
            "--managed-primary",
            str(mcp.MANAGED_PRIMARY_ROOT),
            "--checkout-root",
            str(mcp.CHECKOUT_ROOT),
            "--state-root",
            str(mcp.frontdoor.DEFAULT_STATE_ROOT),
        ]
    )
    assert_equal(parsed.frontdoor, "claude", "platform-specific frontdoor binding")
    assert_equal(parsed.principal_id, "claude-main-agent-a-prime", "host-pinned principal")

    initialized = rpc(
        1,
        "initialize",
        {
            "protocolVersion": mcp.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "contract-test", "version": "1"},
        },
    )
    assert_equal(initialized["result"]["protocolVersion"], mcp.MCP_PROTOCOL_VERSION, "protocol version")
    assert_equal(initialized["result"]["capabilities"], {"tools": {"listChanged": False}}, "capabilities")

    inventory = rpc(2, "tools/list")["result"]["tools"]
    names = [tool["name"] for tool in inventory]
    assert_equal(names, ["submit_request", "read_projection", "ack_output"], "exact tool inventory")
    assert_equal(set(mcp.TOOL_HANDLERS), set(names), "handler inventory")
    for tool in inventory:
        annotations = tool.get("annotations")
        assert isinstance(annotations, dict), f"missing annotations: {tool['name']}"
        assert_equal(annotations["destructiveHint"], False, f"{tool['name']} destructive hint")
        assert_equal(annotations["openWorldHint"], False, f"{tool['name']} open-world hint")
        schema = tool["inputSchema"]
        assert_equal(schema["additionalProperties"], False, f"{tool['name']} closed input schema")
        assert_equal(schema["properties"]["workspace"]["const"], mcp.WORKSPACE_ID, "workspace const")

    submit_schema = inventory[0]["inputSchema"]
    assert {"refs", "allowed_paths"}.issubset(submit_schema["required"])
    assert_equal(submit_schema["properties"]["refs"]["minItems"], 1, "refs must be explicit")
    refs_description = submit_schema["properties"]["refs"]["description"]
    assert "repository-relative paths" in refs_description
    assert "README.md and CHANGELOG.md" in refs_description


def test_manifest_pinned_state_root_must_exactly_match_configured_identity() -> None:
    original_state_root = mcp.STATE_ROOT
    catalog = mcp.frontdoor.host_state_root.DIRECTORY_CATALOG
    previous_catalog = dict(catalog)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        fixed = root / "fixed"
        fixed.mkdir(mode=0o700)
        try:
            mcp.STATE_ROOT = fixed
            catalog.clear()
            catalog["SAIHAI_ORCH_STATE_ROOT"] = str(fixed)
            assert_equal(mcp._canonical_state_root(), fixed, "manifest-pinned state root")

            catalog["SAIHAI_ORCH_STATE_ROOT"] = str(root / "different-configured-root")
            try:
                mcp._canonical_state_root()
            except mcp.frontdoor.FrontdoorError as exc:
                assert_equal(str(exc), "state_root_not_configured", "mutable catalog drift")
            else:
                raise AssertionError("manifest root must not outlive configured identity")

            mcp.STATE_ROOT = root / "arbitrary-state"
            try:
                mcp._canonical_state_root()
            except mcp.frontdoor.FrontdoorError as exc:
                assert_equal(str(exc), "state_root_not_configured", "arbitrary root")
            else:
                raise AssertionError("arbitrary state root must be rejected")
        finally:
            mcp.STATE_ROOT = original_state_root
            catalog.clear()
            catalog.update(previous_catalog)


def test_cli_and_bridge_share_canonical_state_root_identity_and_symlink_reasons() -> None:
    original_state_root = mcp.STATE_ROOT
    catalog = mcp.frontdoor.host_state_root.DIRECTORY_CATALOG
    previous_catalog = dict(catalog)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        actual_parent = root / "actual-parent"
        actual_parent.mkdir(mode=0o700)
        canonical = actual_parent / "state"
        canonical.mkdir(mode=0o700)
        alias_parent = root / "alias-parent"
        alias_parent.symlink_to(actual_parent, target_is_directory=True)
        try:
            catalog.clear()
            catalog["SAIHAI_ORCH_STATE_ROOT"] = str(canonical)
            mcp.STATE_ROOT = canonical
            cli_value = mcp.frontdoor.trusted_state_root(str(canonical))
            bridge_value = mcp._canonical_state_root()
            assert_equal(str(cli_value), str(canonical), "CLI canonical bytes")
            assert_equal(str(bridge_value), str(canonical), "bridge canonical bytes")
            assert_equal(str(cli_value), str(bridge_value), "accepted value parity")

            alias_state = alias_parent / "state"
            reasons: list[str] = []
            for resolver in (
                lambda: mcp.frontdoor.trusted_state_root(alias_state),
                lambda: _bridge_state_root(alias_state),
            ):
                try:
                    resolver()
                except mcp.frontdoor.FrontdoorError as exc:
                    reasons.append(str(exc))
                else:
                    raise AssertionError("alternate state-root spelling must be rejected")
            assert_equal(reasons, ["state_root_not_configured"] * 2, "exact identity parity")

            catalog["SAIHAI_ORCH_STATE_ROOT"] = str(alias_state)
            reasons = []
            for resolver in (
                lambda: mcp.frontdoor.trusted_state_root(alias_state),
                lambda: _bridge_state_root(alias_state),
            ):
                try:
                    resolver()
                except mcp.frontdoor.FrontdoorError as exc:
                    reasons.append(str(exc))
                else:
                    raise AssertionError("ancestor symlink must be rejected")
            assert_equal(
                reasons,
                ["state_root_symlink_redirection_forbidden"] * 2,
                "ancestor symlink reason parity",
            )
        finally:
            mcp.STATE_ROOT = original_state_root
            catalog.clear()
            catalog.update(previous_catalog)


def _bridge_state_root(state_root: Path) -> Path:
    mcp.STATE_ROOT = state_root
    return mcp._canonical_state_root()


def test_real_frontdoor_submit_projection_ack_and_idempotency() -> None:
    with canonical_temp_state() as state_root:
        arguments = submit_arguments()
        submitted_result = call_tool(10, "submit_request", arguments)
        assert_equal(submitted_result["isError"], False, "submit succeeds")
        submitted = submitted_result["structuredContent"]
        assert_equal(submitted["request_kind"], "agent_task_request", "fixed request kind")
        assert_equal(submitted["request_status"], "waiting_human", "submit status")
        assert_equal(submitted["transition_effect"], "none", "submit transition")
        assert_equal(
            submitted["idempotency_key_digest"],
            mcp.frontdoor.idempotency_key_digest(arguments["idempotency_key"]),
            "projection idempotency digest",
        )
        assert arguments["idempotency_key"] not in json.dumps(submitted_result)
        assert "PRIVATE_PROMPT_MARKER" not in json.dumps(submitted_result), "prompt must not be echoed"

        record_path = state_root / "requests" / "req-mcp-bridge.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert_equal(record["request_kind"], "agent_task_request", "persisted request kind")
        assert_equal(record["context_refs"], ["README.md"], "bounded context ref")
        assert_equal(
            record["allowed_paths"],
            ["organization/runtime/workflows/scripts"],
            "bounded allowed path",
        )
        assert_equal(
            record["bridge_contract"]["allowed_actions"],
            ["submit_request", "read_projection", "ack_output"],
            "persisted bridge actions",
        )
        assert_equal(
            record["owner_principal"]["principal_id"],
            mcp.PRINCIPAL_ID,
            "installed frontend owns request",
        )
        assert_equal(
            record["bridge_contract"]["chat_session_id_role"],
            "correlation_only",
            "chat session is not authorization identity",
        )
        checkout_identity = record["checkout_identity"]
        assert checkout_identity["checkout_realpath"] == str(mcp.CHECKOUT_ROOT)
        assert checkout_identity["identity_digest"] == record["checkout_identity_digest"]
        assert checkout_identity["checkout_kind"] in {
            "managed_primary",
            "registered_linked_worktree",
        }
        assert not (state_root / "runs").exists(), "submit must not create a run"

        replay_result = call_tool(11, "submit_request", arguments)
        assert_equal(replay_result["isError"], False, "idempotent replay succeeds")
        assert_equal(replay_result["structuredContent"]["replayed"], True, "replayed marker")
        assert_equal(len(list((state_root / "requests").glob("*.json"))), 1, "single request record")

        read_result = call_tool(
            12,
            "read_projection",
            {
                "workspace": mcp.WORKSPACE_ID,
                "request_id": arguments["request_id"],
                "chat_session_id": "another-correlation-session",
            },
        )
        projection = read_result["structuredContent"]
        assert_equal(projection["request_status"], "waiting_human", "projection status")
        assert "user_prompt" in projection["redacted_fields"]
        assert "PRIVATE_PROMPT_MARKER" not in json.dumps(read_result), "read must not echo prompt"
        assert "checkout_realpath" not in projection["checkout_identity"]
        assert str(mcp.CHECKOUT_ROOT) not in json.dumps(projection), "checkout path must stay redacted"
        assert_equal(
            projection["checkout_identity"]["identity_digest"],
            record["checkout_identity_digest"],
            "redacted checkout digest",
        )

        before = record_path.read_bytes()
        ack_result = call_tool(
            13,
            "ack_output",
            {
                "workspace": mcp.WORKSPACE_ID,
                "request_id": arguments["request_id"],
                "projection_digest": projection["projection_digest"],
                "chat_session_id": "third-correlation-session",
            },
        )
        ack = ack_result["structuredContent"]
        assert_equal(ack["ack_verified"], True, "ack verified")
        assert_equal(ack["transition_effect"], "none", "ack transition")
        assert "ack_path" not in ack, "host state path must not be returned"
        assert_equal(record_path.read_bytes(), before, "ack leaves request unchanged")
        assert_equal(len(list((state_root / "acks").glob("*.json"))), 1, "ack receipt persisted")
        assert not (state_root / "runs").exists(), "bridge lifecycle must not create a run"
        raw_key = arguments["idempotency_key"].encode("utf-8")
        for path in state_root.rglob("*"):
            if path.is_file():
                assert raw_key not in path.read_bytes(), f"raw idempotency key leaked to {path}"


def test_frontend_instructions_report_digest_not_raw_key() -> None:
    instructions = (
        ROOT
        / "organization/runtime/workflows/profiles/codex-main-agent.instructions.md"
    ).read_text(encoding="utf-8")
    assert "including its `idempotency_key_digest`" in instructions
    assert "Never return or repeat the raw idempotency key" in instructions


def test_forbidden_authority_and_workspace_injection_are_rejected() -> None:
    with canonical_temp_state() as state_root:
        for forbidden in (
            "classify",
            "approve",
            "create_run",
            "drain",
            "execute_worker",
            "publish",
        ):
            response = rpc(20, "tools/call", {"name": forbidden, "arguments": {}})
            assert_equal(response["error"]["code"], -32601, f"{forbidden} unavailable")
            assert forbidden not in response["error"]["message"], "unknown tool name must not be reflected"

        smuggled = call_tool(
            21,
            "submit_request",
            submit_arguments(classification={"task_kind": "standard_code_change"}),
        )
        assert_equal(smuggled["isError"], True, "classification smuggling rejected")
        assert_equal(
            smuggled["structuredContent"],
            {"decision": "blocked", "reason": "saihai_bridge_request_rejected"},
            "opaque rejection",
        )

        alternate_workspace = call_tool(
            22,
            "submit_request",
            submit_arguments(workspace="attacker/other-repository"),
        )
        assert_equal(alternate_workspace["isError"], True, "alternate workspace rejected")

        with tempfile.NamedTemporaryFile() as outside:
            outside_ref = call_tool(
                23,
                "submit_request",
                submit_arguments(refs=[outside.name]),
            )
        assert_equal(outside_ref["isError"], True, "outside workspace ref rejected")
        assert outside.name not in json.dumps(outside_ref), "outside path must not be reflected"

        injected_state_root = call_tool(
            24,
            "read_projection",
            {
                "workspace": mcp.WORKSPACE_ID,
                "request_id": "req-mcp-bridge",
                "chat_session_id": "codex-thread-mcp",
                "state_root": "/tmp/attacker-state",
            },
        )
        assert_equal(injected_state_root["isError"], True, "state root injection rejected")
        assert not (state_root / "requests").exists(), "rejected calls must not persist requests"
        assert not (state_root / "runs").exists(), "rejected calls must not create runs"


def test_installed_principal_owns_read_ack_replay_and_client_cannot_override_host_binding() -> None:
    with canonical_temp_state():
        original_principal = mcp.PRINCIPAL_ID
        try:
            arguments = submit_arguments()
            submitted = call_tool(40, "submit_request", arguments)
            assert_equal(submitted["isError"], False, "owner submit")

            for smuggled_field, value in (
                ("principal_id", "attacker"),
                ("workspace_id", "attacker/repository"),
                ("checkout_root", "/tmp/attacker"),
                ("checkout_identity", {"identity_digest": "sha256:" + "0" * 64}),
            ):
                rejected = call_tool(
                    41,
                    "submit_request",
                    submit_arguments(**{smuggled_field: value}),
                )
                assert_equal(rejected["isError"], True, f"{smuggled_field} override rejected")

            mcp.PRINCIPAL_ID = "codex-main-agent-attacker"
            denied_read = call_tool(
                42,
                "read_projection",
                {
                    "workspace": mcp.WORKSPACE_ID,
                    "request_id": arguments["request_id"],
                    "chat_session_id": arguments["chat_session_id"],
                },
            )
            assert_equal(denied_read["isError"], True, "different installed principal cannot read")
            denied_ack = call_tool(
                43,
                "ack_output",
                {
                    "workspace": mcp.WORKSPACE_ID,
                    "request_id": arguments["request_id"],
                    "projection_digest": submitted["structuredContent"]["projection_digest"],
                    "chat_session_id": arguments["chat_session_id"],
                },
            )
            assert_equal(denied_ack["isError"], True, "different installed principal cannot ack")
            denied_replay = call_tool(44, "submit_request", arguments)
            assert_equal(denied_replay["isError"], True, "different installed principal cannot replay")
        finally:
            mcp.PRINCIPAL_ID = original_principal


def test_pending_request_count_and_byte_quotas_are_per_principal_and_replay_safe() -> None:
    original_count = mcp.MAX_PENDING_REQUESTS_PER_PRINCIPAL
    original_bytes = mcp.MAX_PENDING_BYTES_PER_PRINCIPAL
    try:
        with canonical_temp_state():
            mcp.MAX_PENDING_REQUESTS_PER_PRINCIPAL = 1
            mcp.MAX_PENDING_BYTES_PER_PRINCIPAL = 10 * 1024 * 1024
            first_args = submit_arguments()
            first = call_tool(50, "submit_request", first_args)
            assert_equal(first["isError"], False, "first request fits count quota")
            replay = call_tool(51, "submit_request", first_args)
            assert_equal(replay["isError"], False, "replay bypasses new-request quota")
            second = call_tool(
                52,
                "submit_request",
                submit_arguments(
                    request_id="req-mcp-quota-second",
                    idempotency_key="mcp-quota-second-key",
                ),
            )
            assert_equal(second["isError"], True, "second pending request exceeds count quota")

        with canonical_temp_state() as state_root:
            mcp.MAX_PENDING_REQUESTS_PER_PRINCIPAL = 100
            mcp.MAX_PENDING_BYTES_PER_PRINCIPAL = 1
            blocked = call_tool(
                53,
                "submit_request",
                submit_arguments(
                    request_id="req-mcp-byte-quota",
                    idempotency_key="mcp-byte-quota-key",
                ),
            )
            assert_equal(blocked["isError"], True, "request exceeds byte quota")
            assert not (state_root / "requests" / "req-mcp-byte-quota.json").exists()
            assert not list((state_root / "bridge-transactions").glob("*.json"))
    finally:
        mcp.MAX_PENDING_REQUESTS_PER_PRINCIPAL = original_count
        mcp.MAX_PENDING_BYTES_PER_PRINCIPAL = original_bytes


def test_concurrent_idempotency_and_crash_journal_repair_are_atomic() -> None:
    with canonical_temp_state() as state_root:
        stable_identity = mcp._host_checkout_identity()
        original_identity_function = mcp._host_checkout_identity
        mcp._host_checkout_identity = lambda: dict(stable_identity)
        try:
            arguments = submit_arguments()
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(
                    pool.map(
                        lambda request_id: call_tool(
                            request_id,
                            "submit_request",
                            arguments,
                        ),
                        range(60, 68),
                    )
                )
            assert all(result["isError"] is False for result in results)
            assert_equal(len(list((state_root / "requests").glob("*.json"))), 1, "one request")
            assert_equal(len(list((state_root / "idempotency").glob("*.json"))), 1, "one key")

            request_path = state_root / "requests" / "req-mcp-bridge.json"
            idempotency_path = mcp.frontdoor.idempotency_path(
                state_root,
                arguments["idempotency_key"],
            )
            request_record = json.loads(request_path.read_text(encoding="utf-8"))
            idempotency_record = json.loads(idempotency_path.read_text(encoding="utf-8"))
            transaction = {
                "transaction_version": "1",
                "transaction_state": "prepared",
                "created_at": request_record["created_at"],
                "request_id": request_record["request_id"],
                "request_digest": request_record["request_digest"],
                "idempotency_key_digest": request_record["idempotency_key_digest"],
                "owner_principal": request_record["owner_principal"],
                "request_record": request_record,
                "idempotency_record": idempotency_record,
            }
            transaction_path = mcp.frontdoor.bridge_transaction_path(
                state_root,
                request_record["request_id"],
                request_record["idempotency_key_digest"],
            )
            mcp.frontdoor.write_json(transaction_path, transaction)
            request_path.unlink()
            idempotency_path.unlink()
            orphan_temp = state_root / "requests" / ".req-mcp-bridge.json.crashed.tmp"
            orphan_temp.write_text("partial", encoding="utf-8")
            orphan_temp.chmod(0o600)

            repaired = call_tool(68, "submit_request", arguments)
            assert_equal(repaired["isError"], False, "crash journal repaired")
            assert_equal(repaired["structuredContent"]["replayed"], True, "repaired replay")
            assert request_path.is_file() and idempotency_path.is_file()
            assert not transaction_path.exists()
            assert not orphan_temp.exists()
        finally:
            mcp._host_checkout_identity = original_identity_function


def test_registered_primary_and_linked_worktree_identity_acceptance_and_clone_rejection() -> None:
    with tempfile.TemporaryDirectory() as raw_tmp:
        base = Path(raw_tmp).resolve()
        primary = base / "primary"
        linked = base / "linked"
        clone = base / "clone"
        primary.mkdir()

        def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            )

        subprocess.run(["git", "init", "-b", "main", str(primary)], check=True, capture_output=True)
        git(primary, "config", "user.name", "Saihai Contract Test")
        git(primary, "config", "user.email", "saihai-contract@example.invalid")
        (primary / "README.md").write_text("managed checkout\n", encoding="utf-8")
        git(primary, "add", "README.md")
        git(primary, "commit", "-m", "test fixture")
        git(primary, "worktree", "add", "-b", "feature", str(linked))

        primary_identity = mcp.frontdoor.resolve_checkout_identity(
            workspace_id=mcp.WORKSPACE_ID,
            managed_primary=primary,
            checkout_root=primary,
        )
        linked_identity = mcp.frontdoor.resolve_checkout_identity(
            workspace_id=mcp.WORKSPACE_ID,
            managed_primary=primary,
            checkout_root=linked,
        )
        assert_equal(primary_identity["checkout_kind"], "managed_primary", "primary kind")
        assert_equal(
            linked_identity["checkout_kind"],
            "registered_linked_worktree",
            "linked kind",
        )
        assert_equal(linked_identity["branch"], "feature", "linked branch binding")
        assert linked_identity["head_sha"] == primary_identity["head_sha"]
        assert_equal(linked_identity["identity_version"], "2", "stable identity version")
        assert "worktree_catalog_digest" not in linked_identity
        for field in (
            "identity_digest",
            "git_common_dir_digest",
            "worktree_state_digest",
        ):
            assert linked_identity[field].startswith("sha256:")

        sibling = base / "sibling"
        git(primary, "worktree", "add", "-b", "sibling", str(sibling))
        after_sibling = mcp.frontdoor.resolve_checkout_identity(
            workspace_id=mcp.WORKSPACE_ID,
            managed_primary=primary,
            checkout_root=linked,
        )
        assert_equal(
            after_sibling["identity_digest"],
            linked_identity["identity_digest"],
            "unrelated sibling worktree does not invalidate checkout identity",
        )
        try:
            mcp.frontdoor.validate_checkout_identity(
                {**linked_identity, "identity_version": "1"}
            )
        except mcp.frontdoor.FrontdoorError as exc:
            assert "version is unsupported" in str(exc)
        else:
            raise AssertionError("v1 checkout identity must require re-minting")

        # The immutable runtime bundle is executable code only. Context refs
        # must bind to the active registered checkout selected by the wrapper.
        (linked / "README.md").write_text("linked active checkout\n", encoding="utf-8")
        original_primary = mcp.MANAGED_PRIMARY_ROOT
        original_checkout = mcp.CHECKOUT_ROOT
        try:
            mcp.MANAGED_PRIMARY_ROOT = primary
            mcp.CHECKOUT_ROOT = linked
            with canonical_temp_state(
                managed_primary=primary,
                checkout_root=linked,
            ) as state_root:
                submitted = call_tool(
                    69,
                    "submit_request",
                    submit_arguments(
                        request_id="req-active-checkout-ref",
                        idempotency_key="active-checkout-ref-key",
                        refs=["README.md"],
                        allowed_paths=[],
                    ),
                )
                assert_equal(submitted["isError"], False, "active checkout submit")
                record = json.loads(
                    (state_root / "requests/req-active-checkout-ref.json").read_text(encoding="utf-8")
                )
                resolved_ref = record["resolved_context_refs"][0]
                assert_equal(
                    resolved_ref["digest"],
                    mcp.frontdoor.file_sha256(linked / "README.md"),
                    "ref digest comes from active checkout",
                )
                assert resolved_ref["digest"] != mcp.frontdoor.file_sha256(primary / "README.md")
                # The later approval-integrity refresh must use the same
                # recorded checkout rather than the runtime/source module root.
                refreshed = mcp.frontdoor.refresh_approval_context_refs(record)
                assert_equal(
                    refreshed["resolved_context_refs"][0]["digest"],
                    resolved_ref["digest"],
                    "approval refresh keeps recorded checkout",
                )
                verified = mcp.frontdoor.verified_context_refs_for_work_order(refreshed)
                assert_equal(verified[0]["digest"], resolved_ref["digest"], "work-order ref checkout")
                (linked / "README.md").write_text("changed after approval\n", encoding="utf-8")
                try:
                    mcp.frontdoor.verified_context_refs_for_work_order(refreshed)
                except mcp.frontdoor.FrontdoorError as exc:
                    assert "checkout identity changed after approval" in str(exc)
                else:
                    raise AssertionError("post-approval checkout drift must block work-order refs")
        finally:
            mcp.MANAGED_PRIMARY_ROOT = original_primary
            mcp.CHECKOUT_ROOT = original_checkout

        (linked / "untracked.txt").write_text("first\n", encoding="utf-8")
        first_dirty_identity = mcp.frontdoor.resolve_checkout_identity(
            workspace_id=mcp.WORKSPACE_ID,
            managed_primary=primary,
            checkout_root=linked,
        )
        (linked / "untracked.txt").write_text("second\n", encoding="utf-8")
        second_dirty_identity = mcp.frontdoor.resolve_checkout_identity(
            workspace_id=mcp.WORKSPACE_ID,
            managed_primary=primary,
            checkout_root=linked,
        )
        assert first_dirty_identity["worktree_state_digest"] != second_dirty_identity[
            "worktree_state_digest"
        ], "untracked content changes must alter checkout binding"

        subprocess.run(
            ["git", "clone", "--no-local", str(primary), str(clone)],
            check=True,
            capture_output=True,
        )
        try:
            mcp.frontdoor.resolve_checkout_identity(
                workspace_id=mcp.WORKSPACE_ID,
                managed_primary=primary,
                checkout_root=clone,
            )
        except mcp.frontdoor.FrontdoorError as exc:
            assert "git-common-dir" in str(exc)
        else:
            raise AssertionError("arbitrary clone must not be a registered linked worktree")

        alias = base / "linked-alias"
        alias.symlink_to(linked, target_is_directory=True)
        try:
            mcp.frontdoor.resolve_checkout_identity(
                workspace_id=mcp.WORKSPACE_ID,
                managed_primary=primary,
                checkout_root=alias,
            )
        except mcp.frontdoor.FrontdoorError as exc:
            assert "exact checkout realpath" in str(exc)
        else:
            raise AssertionError("symlink alias must not replace exact checkout realpath")


def test_frame_and_jsonrpc_id_bounds_fail_closed_and_server_survives() -> None:
    oversized = "x" * (mcp.MAX_JSONRPC_FRAME_BYTES + 100)
    ping = json.dumps({"jsonrpc": "2.0", "id": 71, "method": "ping"})
    input_stream = io.StringIO(oversized + "\n" + ping + "\n")
    output_stream = io.StringIO()
    mcp.serve(input_stream, output_stream)
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert_equal(responses[0]["error"]["code"], -32700, "oversized frame rejected")
    assert_equal(responses[1], {"jsonrpc": "2.0", "id": 71, "result": {}}, "server survives")

    oversized_id = mcp.dispatch(
        {"jsonrpc": "2.0", "id": "i" * (mcp.MAX_JSONRPC_ID_CHARS + 1), "method": "ping"}
    )
    assert isinstance(oversized_id, dict)
    assert_equal(oversized_id["id"], None, "unbounded id is never reflected")
    assert_equal(oversized_id["error"]["code"], -32600, "unbounded id rejected")


def test_stdio_parse_and_tool_failures_do_not_kill_server_or_echo_input() -> None:
    secret_marker = "Bearer DO_NOT_ECHO_THIS_SECRET"
    malformed = '{"jsonrpc":"2.0","id":30,"method":"tools/call","params":{"prompt":"' + secret_marker
    notification = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    )
    bad_tool = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "submit_request", "arguments": {"prompt": secret_marker}},
        }
    )
    ping = json.dumps({"jsonrpc": "2.0", "id": 32, "method": "ping"})
    input_stream = io.StringIO("\n".join((malformed, notification, bad_tool, ping)) + "\n")
    output_stream = io.StringIO()
    mcp.serve(input_stream, output_stream)

    raw_output = output_stream.getvalue()
    assert secret_marker not in raw_output, "invalid input must never be echoed"
    responses = [json.loads(line) for line in raw_output.splitlines()]
    assert_equal(len(responses), 3, "notification has no response")
    assert_equal(responses[0]["error"]["code"], -32700, "malformed JSON parse error")
    assert_equal(responses[1]["result"]["isError"], True, "invalid tool args are contained")
    assert_equal(responses[2], {"jsonrpc": "2.0", "id": 32, "result": {}}, "server survives to ping")


def main() -> None:
    tests = [
        test_initialize_inventory_and_annotations_are_exact,
        test_manifest_pinned_state_root_must_exactly_match_configured_identity,
        test_cli_and_bridge_share_canonical_state_root_identity_and_symlink_reasons,
        test_real_frontdoor_submit_projection_ack_and_idempotency,
        test_frontend_instructions_report_digest_not_raw_key,
        test_forbidden_authority_and_workspace_injection_are_rejected,
        test_installed_principal_owns_read_ack_replay_and_client_cannot_override_host_binding,
        test_pending_request_count_and_byte_quotas_are_per_principal_and_replay_safe,
        test_concurrent_idempotency_and_crash_journal_repair_are_atomic,
        test_registered_primary_and_linked_worktree_identity_acceptance_and_clone_rejection,
        test_frame_and_jsonrpc_id_bounds_fail_closed_and_server_survives,
        test_stdio_parse_and_tool_failures_do_not_kill_server_or_echo_input,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
