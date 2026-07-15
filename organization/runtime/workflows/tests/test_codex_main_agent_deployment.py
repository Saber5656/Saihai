#!/usr/bin/env python3
"""Focused tests for the Codex A-prime root-owned deployment contract."""

from __future__ import annotations

import json
import inspect
import os
import pwd
import select
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = WORKFLOW_ROOT / "scripts"
PROFILE_ROOT = WORKFLOW_ROOT / "profiles"
SCHEMA_PATH = WORKFLOW_ROOT / "schemas" / "codex-main-agent-deployment.schema.json"
EXAMPLE_PATH = PROFILE_ROOT / "codex-main-agent.deployment.example.json"
sys.path.insert(0, str(SCRIPT_ROOT))

import codex_main_agent_deployment as deployment  # noqa: E402

FAKE_NATIVE_CODEX = b"#!/bin/sh\necho 'codex-cli 0.144.1'\n"


def expect_blocked(reason: str, callback) -> None:
    try:
        callback()
    except deployment.DeploymentError as exc:
        assert exc.reason == reason, (exc.reason, reason)
    else:
        raise AssertionError(f"expected {reason}")


def write_file(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)


@contextmanager
def patched_layout(root: Path):
    root = root.resolve()
    names = [
        "BRIDGE_WRAPPER_PATH",
        "CODEX_LAUNCHER_PATH",
        "MANIFEST_PATH",
        "RUNTIME_CONFIG_PATH",
        "REQUIREMENTS_PATH",
        "DEPLOYMENT_LOCK_PATH",
        "ASSURANCE_ROOT",
        "BACKUP_ROOT",
        "QUARANTINE_ROOT",
        "NATIVE_CODEX_EXPECTED_SHA256",
    ]
    original = {name: getattr(deployment, name) for name in names}
    deployment.BRIDGE_WRAPPER_PATH = root / "usr/local/libexec/saihai-codex-main-agent-bridge"
    deployment.CODEX_LAUNCHER_PATH = root / "usr/local/bin/saihai-codex-main-agent"
    deployment.MANIFEST_PATH = root / "Library/Saihai/Manifests/codex-main-agent.deployment.json"
    deployment.RUNTIME_CONFIG_PATH = root / "Library/Saihai/Config/codex-main-agent.runtime.json"
    deployment.REQUIREMENTS_PATH = root / "etc/codex/requirements.toml"
    deployment.DEPLOYMENT_LOCK_PATH = root / "Library/Saihai/deployment.lock"
    deployment.ASSURANCE_ROOT = root / "Library/Saihai/Assurance"
    deployment.BACKUP_ROOT = root / "Library/Saihai/Backups"
    deployment.QUARANTINE_ROOT = root / "Library/Saihai/Quarantine"
    deployment.NATIVE_CODEX_EXPECTED_SHA256 = deployment.sha256_bytes(FAKE_NATIVE_CODEX)
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(deployment, name, value)


def fixture_manifest(root: Path) -> tuple[Path, dict]:
    root = root.resolve()
    runtime = root / "Library/Saihai/Runtime" / ("a" * 40)
    entrypoint = runtime / deployment.ENTRYPOINT_RELATIVE_PATH
    verifier = runtime / deployment.VERIFIER_RELATIVE_PATH
    deployment_module = runtime / deployment.DEPLOYMENT_MODULE_RELATIVE_PATH
    installer = runtime / "organization/runtime/workflows/scripts/codex_main_agent_install.py"
    supervisor = runtime / deployment.SUPERVISOR_RELATIVE_PATH
    observer = runtime / deployment.OBSERVER_RELATIVE_PATH
    instructions = runtime / deployment.INSTRUCTIONS_RELATIVE_PATH
    native_codex = runtime / "bin/codex"
    write_file(entrypoint, b"#!/usr/bin/env python3\n", 0o644)
    write_file(verifier, b"#!/usr/bin/env python3\n", 0o644)
    write_file(deployment_module, b"# deployment module\n", 0o644)
    write_file(installer, b"# install module\n", 0o644)
    write_file(supervisor, b"# supervisor module\n", 0o644)
    write_file(observer, b"#!/usr/bin/env python3\n# observer module\n", 0o555)
    write_file(instructions, b"route through Saihai\n", 0o644)
    write_file(native_codex, FAKE_NATIVE_CODEX, 0o555)
    for directory in [runtime, *[path for path in runtime.rglob("*") if path.is_dir()]]:
        directory.chmod(0o755)

    python = root / "usr/bin/python3"
    wrapper = deployment.BRIDGE_WRAPPER_PATH
    launcher = deployment.CODEX_LAUNCHER_PATH
    requirements = deployment.REQUIREMENTS_PATH
    user_home = root / "Users/test"
    codex_home = user_home / ".codex-saihai-main-agent"
    profile = codex_home / "saihai-main-agent.config.toml"
    managed_primary = root / "Users/test/dev/Saihai"
    catalog = managed_primary / "directory-path.env"
    write_file(python, b"#!/bin/sh\nexit 0\n", 0o755)
    write_file(wrapper, b"#!/bin/sh\nexit 0\n", 0o555)
    write_file(launcher, b"#!/bin/sh\nexit 0\n", 0o555)
    write_file(requirements, b'default_permissions = "saihai_frontend"\n', 0o644)
    write_file(profile, b'notify = []\n', 0o600)
    user_home.chmod(0o700)
    codex_home.chmod(0o700)
    write_file(catalog, b"SAIHAI_ROOT=/Users/test/dev/Saihai\n", 0o600)
    catalog_stat = catalog.stat()

    manifest = {
        "deployment_version": "1",
        "deployment_id": "codex-main-agent-a-prime",
        "release_commit": "a" * 40,
        "platform": "macos",
        "expected_admin_uid": 0,
        "topology": {
            "frontend_policy_domain": "frontend",
            "write_capable_worker_policy_domain": "worker",
            "shared_rootfs": False,
            "worker_isolation": "separate_host_vm_or_container",
        },
        "bindings": {
            "frontdoor": "codex",
            "principal_id": "codex-main-agent-a-prime",
            "workspace_id": "Saber5656/Saihai",
            "managed_primary": str(managed_primary),
            "checkout_binding": "launch_cwd_registered_worktree",
            "workspace_catalog": "git_worktree_list",
            "assurance_root": str(deployment.ASSURANCE_ROOT),
            "state_root_catalog": str(catalog),
            "deployment_manifest_path": str(deployment.MANIFEST_PATH),
            "runtime_config_path": str(deployment.RUNTIME_CONFIG_PATH),
            "instructions_path": str(instructions),
            "developer_instructions_sha256": deployment.sha256_file(instructions),
            "runtime_user_uid": catalog_stat.st_uid,
            "runtime_user_name": "test",
            "runtime_user_home": str(user_home),
            "codex_home": str(codex_home),
            "state_root": str(user_home / ".codex/state/itb/frontdoor-orchestrator"),
            "state_root_source": "default",
            "state_root_catalog_observation": {
                "status": "present",
                "device": catalog_stat.st_dev,
                "inode": catalog_stat.st_ino,
                "uid": catalog_stat.st_uid,
                "mode": deployment.mode_text(catalog_stat.st_mode),
                "size": catalog_stat.st_size,
                "sha256": deployment.sha256_file(catalog),
            },
        },
        "artifacts": {
            "runtime_bundle": {
                "kind": "tree",
                "path": str(runtime),
                "sha256": deployment.tree_digest(runtime),
                "mode": "0755",
                "digest_algorithm": "saihai-tree-sha256-v1",
                "entrypoint": deployment.ENTRYPOINT_RELATIVE_PATH,
                "verifier": deployment.VERIFIER_RELATIVE_PATH,
                "deployment_module": deployment.DEPLOYMENT_MODULE_RELATIVE_PATH,
            },
            "bridge_wrapper": {
                "kind": "file",
                "path": str(wrapper),
                "sha256": deployment.sha256_file(wrapper),
                "mode": "0555",
            },
            "python_executable": {
                "kind": "file",
                "path": str(python),
                "sha256": deployment.sha256_file(python),
                "mode": "0755",
            },
            "runtime_config": {
                "kind": "file",
                "path": str(deployment.RUNTIME_CONFIG_PATH),
                "sha256": "sha256:" + "0" * 64,
                "mode": "0644",
            },
            "requirements": {
                "kind": "file",
                "path": str(requirements),
                "sha256": deployment.sha256_file(requirements),
                "mode": "0644",
            },
            "instructions": {
                "kind": "file",
                "path": str(instructions),
                "sha256": deployment.sha256_file(instructions),
                "mode": "0644",
            },
            "codex_launcher": {
                "kind": "file",
                "path": str(launcher),
                "sha256": deployment.sha256_file(launcher),
                "mode": "0555",
            },
            "codex_profile": {
                "kind": "file",
                "path": str(profile),
                "sha256": deployment.sha256_file(profile),
                "mode": "0600",
            },
            "native_codex": {
                "kind": "file",
                "path": str(native_codex),
                "sha256": deployment.sha256_file(native_codex),
                "mode": "0555",
                "version": deployment.NATIVE_CODEX_VERSION,
                "package": deployment.NATIVE_CODEX_PACKAGE,
                "package_version": deployment.NATIVE_CODEX_PACKAGE_VERSION,
                "package_integrity": deployment.NATIVE_CODEX_PACKAGE_INTEGRITY,
                "platform": deployment.NATIVE_CODEX_PLATFORM,
                "architecture": deployment.NATIVE_CODEX_ARCHITECTURE,
            },
            "supervisor": {
                "kind": "file",
                "path": str(supervisor),
                "sha256": deployment.sha256_file(supervisor),
                "mode": "0644",
            },
            "observer": {
                "kind": "file",
                "path": str(observer),
                "sha256": deployment.sha256_file(observer),
                "mode": "0555",
            },
        },
        "tool_contract": {
            "server_name": "saihai_bridge",
            "enabled_tools": deployment.ENABLED_TOOLS,
            "forbidden_tools": ["approve", "execute", "shell"],
        },
    }
    runtime_config = deployment.runtime_config_payload(manifest)
    write_file(deployment.RUNTIME_CONFIG_PATH, deployment.canonical_json_bytes(runtime_config), 0o644)
    manifest["artifacts"]["runtime_config"]["sha256"] = deployment.sha256_file(
        deployment.RUNTIME_CONFIG_PATH
    )
    manifest_path = deployment.MANIFEST_PATH
    write_file(manifest_path, deployment.canonical_json_bytes(manifest), 0o644)
    return manifest_path, manifest


def fixture_uid_reader(manifest: dict):
    profile = Path(manifest["artifacts"]["codex_profile"]["path"])
    user_home = Path(manifest["bindings"]["runtime_user_home"])
    codex_home = Path(manifest["bindings"]["codex_home"])
    runtime_uid = manifest["bindings"]["runtime_user_uid"]
    return lambda path: runtime_uid if path in {profile, user_home, codex_home} else 0


def frozen_stage_fixture(root: Path, *, payload_source: Path | None = None) -> tuple[Path, str, dict]:
    transaction_id = "codex-main-agent-a-prime-aaaaaaaaaaaa"
    frozen_root = root / transaction_id
    frozen_root.mkdir(parents=True, mode=0o700)
    frozen_root = frozen_root.resolve()
    frozen_root.chmod(0o700)
    payload = frozen_root / deployment.PAYLOAD_DIR_NAME
    if payload_source is None:
        importer = payload / "runtime" / ("a" * 40) / (
            "organization/runtime/workflows/scripts/codex_main_agent_install.py"
        )
        write_file(importer, b"# frozen importer\n", 0o644)
        write_file(payload / "data.txt", b"reviewed payload\n", 0o644)
    else:
        shutil.copytree(payload_source, payload)
    for directory in [payload, *[path for path in payload.rglob("*") if path.is_dir()]]:
        directory.chmod(0o755)
    bootstrap = frozen_root / deployment.FREEZE_BOOTSTRAP_NAME
    write_file(bootstrap, deployment.POST_FREEZE_BOOTSTRAP_SOURCE.encode("utf-8"), 0o400)
    entries = deployment.tree_inventory(payload)
    checksums = frozen_root / deployment.FREEZE_CHECKSUMS_NAME
    write_file(
        checksums,
        deployment._payload_checksums(entries, frozen_payload=payload),  # noqa: SLF001
        0o600,
    )
    importer_relative = next(
        entry["path"]
        for entry in entries
        if entry["kind"] == "file" and entry["path"].endswith("codex_main_agent_install.py")
    )
    importer_digest = next(
        entry["sha256"] for entry in entries if entry.get("path") == importer_relative
    )
    request = {
        "freeze_version": deployment.FREEZE_VERSION,
        "transaction_id": transaction_id,
        "deployment_id": "codex-main-agent-a-prime",
        "release_commit": "a" * 40,
        "frozen_root": str(frozen_root),
        "payload_relative_path": deployment.PAYLOAD_DIR_NAME,
        "payload_tree_sha256": deployment.tree_digest(payload),
        "payload_entries_sha256": deployment._inventory_digest(entries),  # noqa: SLF001
        "payload_entries": entries,
        "payload_regular_file_count": sum(1 for entry in entries if entry["kind"] == "file"),
        "payload_directory_count": sum(1 for entry in entries if entry["kind"] == "directory"),
        "checksums_relative_path": deployment.FREEZE_CHECKSUMS_NAME,
        "checksums_sha256": deployment.sha256_file(checksums),
        "bootstrap_relative_path": deployment.FREEZE_BOOTSTRAP_NAME,
        "bootstrap_sha256": deployment.sha256_file(bootstrap),
        "activation_importer_relative_path": importer_relative,
        "activation_importer_sha256": importer_digest,
    }
    request_path = frozen_root / deployment.FREEZE_REQUEST_NAME
    write_file(request_path, deployment.canonical_json_bytes(request), 0o600)
    return frozen_root, deployment.sha256_file(request_path), request


def seal_frozen_stage(frozen_root: Path, request_digest: str, request: dict) -> None:
    seal = {
        "seal_version": deployment.FREEZE_VERSION,
        "transaction_id": request["transaction_id"],
        "freeze_request_sha256": request_digest,
        "payload_tree_sha256": request["payload_tree_sha256"],
        "payload_entries_sha256": request["payload_entries_sha256"],
        "verified_by": "trusted-root-copied-bootstrap-v1",
    }
    write_file(
        frozen_root / deployment.FREEZE_SEAL_NAME,
        deployment.canonical_json_bytes(seal),
        0o400,
    )


def frozen_deployment_fixture(
    root: Path, *, keep_installed_targets: bool
) -> tuple[Path, str, dict, dict]:
    root = root.resolve()
    manifest_path, manifest = fixture_manifest(root)
    payload_source = root / "reviewed-payload"
    runtime_source = Path(manifest["artifacts"]["runtime_bundle"]["path"])
    shutil.copytree(runtime_source, payload_source / "runtime" / manifest["release_commit"])
    for source, destination in (
        (deployment.RUNTIME_CONFIG_PATH, payload_source / "config" / deployment.RUNTIME_CONFIG_PATH.name),
        (deployment.REQUIREMENTS_PATH, payload_source / "etc" / "requirements.toml"),
        (deployment.BRIDGE_WRAPPER_PATH, payload_source / "libexec" / deployment.BRIDGE_WRAPPER_PATH.name),
        (deployment.CODEX_LAUNCHER_PATH, payload_source / "bin" / deployment.CODEX_LAUNCHER_PATH.name),
        (
            Path(manifest["artifacts"]["codex_profile"]["path"]),
            payload_source / "user" / f"{deployment.CODEX_PROFILE_NAME}.config.toml",
        ),
        (manifest_path, payload_source / "manifest" / deployment.MANIFEST_PATH.name),
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    if not keep_installed_targets:
        shutil.rmtree(runtime_source)
        for path in (
            deployment.RUNTIME_CONFIG_PATH,
            deployment.REQUIREMENTS_PATH,
            deployment.BRIDGE_WRAPPER_PATH,
            deployment.CODEX_LAUNCHER_PATH,
            Path(manifest["artifacts"]["codex_profile"]["path"]),
            manifest_path,
        ):
            path.unlink()
    frozen_root, request_digest, request = frozen_stage_fixture(
        root / "frozen", payload_source=payload_source
    )
    seal_frozen_stage(frozen_root, request_digest, request)
    return frozen_root, request_digest, request, manifest


def test_example_matches_schema_and_manual_validator() -> None:
    value = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    deployment.validate_manifest(value)
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(value)


def test_v01_deployment_identity_is_fixed_at_every_gate() -> None:
    alternate = "alternate-main-agent"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        with patched_layout(root):
            manifest_path, manifest = fixture_manifest(root)
            alternate_manifest = json.loads(json.dumps(manifest))
            alternate_manifest["deployment_id"] = alternate
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.validate_manifest(alternate_manifest),
            )
            alternate_manifest = json.loads(json.dumps(manifest))
            alternate_manifest["bindings"]["principal_id"] = alternate
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.validate_manifest(alternate_manifest),
            )
            alternate_path = root / "alternate-manifest.json"
            write_file(
                alternate_path,
                deployment.canonical_json_bytes(
                    {**manifest, "deployment_id": alternate}
                ),
                0o644,
            )
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.load_manifest(alternate_path),
            )
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.verify_deployment(
                    alternate_path,
                    uid_reader=lambda _path: 0,
                    verify_ancestors=False,
                ),
            )
            frozen, _digest, request = frozen_stage_fixture(root / "frozen")
            alternate_request = {
                **request,
                "deployment_id": alternate,
                "transaction_id": f"{alternate}-aaaaaaaaaaaa",
            }
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.validate_freeze_request(alternate_request),
            )
            request_path = frozen / deployment.FREEZE_REQUEST_NAME
            write_file(
                request_path,
                deployment.canonical_json_bytes(
                    {**request, "deployment_id": alternate}
                ),
                0o600,
            )
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.verify_frozen_stage(
                    frozen,
                    deployment.sha256_file(request_path),
                    expected_uid=os.getuid(),
                    require_seal=False,
                    enforce_production_root=False,
                ),
            )
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.deployment_epoch_path(alternate),
            )
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment.prepare_deployment(
                    source_root=root / "missing-source",
                    stage_root=root / "stage",
                    managed_primary=root / "missing-primary",
                    user_home=root / "missing-home",
                    principal_id=alternate,
                ),
            )
            alternate_admin_manifest = json.loads(json.dumps(manifest))
            alternate_admin_manifest["deployment_id"] = alternate
            alternate_admin_manifest["bindings"]["principal_id"] = alternate
            expect_blocked(
                "deployment_identity_unsupported",
                lambda: deployment._admin_flow_commands(  # noqa: SLF001
                    root / "stage",
                    alternate_admin_manifest,
                    approved_freeze_request_sha256="sha256:" + "0" * 64,
                    transaction_id=f"{alternate}-aaaaaaaaaaaa",
                ),
            )

            original_verify = deployment.verify_frozen_stage
            original_load = deployment.load_manifest
            deployment.verify_frozen_stage = lambda *_args, **_kwargs: {
                "frozen_root": str(frozen),
                "deployment_id": alternate,
                "release_commit": "a" * 40,
                "transaction_id": f"{alternate}-aaaaaaaaaaaa",
            }
            deployment.load_manifest = lambda _path: manifest
            try:
                expect_blocked(
                    "deployment_identity_unsupported",
                    lambda: deployment.activate_frozen_deployment(
                        frozen,
                        "sha256:" + "0" * 64,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
                for operation in ("rollback", "uninstall"):
                    expect_blocked(
                        "deployment_identity_unsupported",
                        lambda operation=operation: deployment.rollback_frozen_deployment(
                            frozen,
                            "sha256:" + "0" * 64,
                            admin_uid=os.getuid(),
                            admin_gid=os.getgid(),
                            verify_ancestors=False,
                            enforce_production_root=False,
                            operation=operation,
                        ),
                    )
            finally:
                deployment.verify_frozen_stage = original_verify
                deployment.load_manifest = original_load

            assert manifest_path.exists()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["deployment_id"] == {
        "const": deployment.SUPPORTED_DEPLOYMENT_ID
    }
    assert schema["properties"]["bindings"]["properties"]["principal_id"] == {
        "const": deployment.SUPPORTED_PRINCIPAL_ID
    }
    assert "validate_freeze_request" in inspect.getsource(deployment.verify_frozen_stage)
    assert "choices=(SUPPORTED_PRINCIPAL_ID,)" in inspect.getsource(deployment.install_main)
    assert 'r.get("deployment_id")!="codex-main-agent-a-prime"' in deployment.POST_FREEZE_BOOTSTRAP_SOURCE


def test_shared_worker_rootfs_is_rejected() -> None:
    value = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    value["topology"]["shared_rootfs"] = True
    expect_blocked("write_capable_worker_shared_rootfs_forbidden", lambda: deployment.validate_manifest(value))


def test_developer_instructions_digest_is_manifest_bound() -> None:
    value = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    value["bindings"]["developer_instructions_sha256"] = "sha256:" + "1" * 64
    expect_blocked(
        "developer_instructions_digest_mismatch",
        lambda: deployment.validate_manifest(value),
    )


def test_wrapper_is_zero_argument_and_pins_host_bindings() -> None:
    template = (PROFILE_ROOT / "saihai-codex-main-agent-bridge.wrapper.example").read_text(encoding="utf-8")
    rendered = deployment.render_wrapper(
        template,
        python_executable=Path("/usr/bin/python3"),
        python_sha256="sha256:" + "1" * 64,
        verifier=Path("/Library/Saihai/verify.py"),
        verifier_sha256="sha256:" + "2" * 64,
        deployment_module=Path("/Library/Saihai/deployment.py"),
        deployment_module_sha256="sha256:" + "3" * 64,
        manifest_path=Path("/Library/Saihai/manifest.json"),
        entrypoint=Path("/Library/Saihai/bridge.py"),
        frontdoor="codex",
        principal_id="codex-main-agent-a-prime",
        workspace_id="Saber5656/Saihai",
        managed_primary=Path("/Users/test/dev/Saihai"),
        state_root=Path("/Users/test/.codex/state/itb/frontdoor-orchestrator"),
        state_root_catalog=Path("/Users/test/dev/Saihai/directory-path.env"),
        state_root_catalog_status="present",
        state_root_catalog_sha256="sha256:" + "4" * 64,
    )
    assert "@@" not in rendered
    for needle in [
        'if [ "$#" -ne 0 ]',
        "/usr/bin/env -i",
        "--principal-id",
        "--workspace-id",
        "--managed-primary",
        "--checkout-root",
        "--state-root",
        "state_root_catalog_sha256=",
        "pwd -P",
    ]:
        assert needle in rendered
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / "wrapper"
        write_file(wrapper, rendered.encode("utf-8"), 0o755)
        result = subprocess.run([str(wrapper), "override"], text=True, capture_output=True, check=False)
        assert result.returncode == 64
        assert "accepts no arguments" in result.stderr


def test_tree_digest_rejects_symlinks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bundle"
        root.mkdir()
        write_file(root / "file", b"one", 0o644)
        first = deployment.tree_digest(root)
        assert first == deployment.tree_digest(root)
        (root / "link").symlink_to("file")
        expect_blocked("runtime_bundle_special_file", lambda: deployment.tree_digest(root))


def test_safe_ancestors_reject_symlink_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_parent = root / "real-parent"
        real_parent.mkdir()
        linked_parent = root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        original_ancestors = deployment._ancestors  # noqa: SLF001 - security contract test
        deployment._ancestors = lambda _path: [root, linked_parent]  # noqa: SLF001
        try:
            expect_blocked(
                "artifact_parent_invalid",
                lambda: deployment._verify_safe_ancestors(  # noqa: SLF001 - security contract test
                    linked_parent / "artifact",
                    uid_reader=lambda _path: 0,
                ),
            )
        finally:
            deployment._ancestors = original_ancestors  # noqa: SLF001


def test_verifier_accepts_exact_fixture_and_rejects_tamper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched_layout(root):
            manifest_path, manifest = fixture_manifest(root)
            result = deployment.verify_deployment(
                manifest_path,
                uid_reader=fixture_uid_reader(manifest),
                verify_ancestors=False,
            )
            assert result["decision"] == "verified"
            deployment.RUNTIME_CONFIG_PATH.write_text("{}\n", encoding="utf-8")
            expect_blocked(
                "artifact_digest_mismatch",
                lambda: deployment.verify_deployment(
                    manifest_path,
                    uid_reader=fixture_uid_reader(manifest),
                    verify_ancestors=False,
                ),
            )


def test_verifier_rejects_symlink_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched_layout(root):
            manifest_path, manifest = fixture_manifest(root)
            requirements = deployment.REQUIREMENTS_PATH
            copy_path = requirements.with_name("requirements-real.toml")
            requirements.replace(copy_path)
            requirements.symlink_to(copy_path)
            expect_blocked(
                "artifact_not_regular",
                lambda: deployment.verify_deployment(
                    manifest_path,
                    uid_reader=fixture_uid_reader(manifest),
                    verify_ancestors=False,
                ),
            )


def test_profiles_pin_wrapper_tools_sandbox_and_sensitive_reads() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    profile = tomllib.loads((PROFILE_ROOT / "codex-main-agent.config.example.toml").read_text(encoding="utf-8"))
    bridge = profile["mcp_servers"]["saihai_bridge"]
    assert bridge["command"] == "/usr/local/libexec/saihai-codex-main-agent-bridge"
    assert bridge["args"] == []
    assert bridge["enabled_tools"] == deployment.ENABLED_TOOLS
    assert deployment.REQUIRED_FORBIDDEN_TOOLS.issubset(set(bridge["disabled_tools"]))
    assert "model_instructions_file" not in profile
    assert profile["developer_instructions"] == "@@SAIHAI_DEVELOPER_INSTRUCTIONS@@"

    instructions = (PROFILE_ROOT / "codex-main-agent.instructions.md").read_text(encoding="utf-8")
    rendered = deployment._render_profile(  # noqa: SLF001 - exact profile contract test
        (PROFILE_ROOT / "codex-main-agent.config.example.toml").read_text(encoding="utf-8"),
        user_home=Path("/Users/test"),
        release_commit="a" * 40,
        developer_instructions=instructions,
    )
    rendered_profile = tomllib.loads(rendered)
    assert "model_instructions_file" not in rendered_profile
    assert rendered_profile["developer_instructions"] == instructions

    requirements = tomllib.loads(
        (PROFILE_ROOT / "codex-main-agent.requirements.example.toml").read_text(encoding="utf-8")
    )
    assert "allowed_sandbox_modes" not in requirements
    assert requirements["allowed_approval_policies"] == ["never"]
    identity = requirements["mcp_servers"]["saihai_bridge"]["identity"]["command"]
    assert identity == {"executable": "/usr/local/libexec/saihai-codex-main-agent-bridge", "args": []}
    deny_read = requirements["permissions"]["filesystem"]["deny_read"]
    for sensitive in [
        ".ssh",
        ".aws",
        ".codex/auth.json",
        ".codex-saihai-main-agent/auth.json",
        ".git-credentials",
        "Keychains",
    ]:
        assert any(sensitive in item for item in deny_read)


def test_admin_plan_freezes_data_before_any_root_owned_saihai_execution() -> None:
    manifest = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    digest = "sha256:" + "1" * 64
    transaction_id = "codex-main-agent-a-prime-000000000000"
    flow = deployment._admin_flow_commands(  # noqa: SLF001 - focused contract test
        Path("/tmp/reviewed-stage"),
        manifest,
        approved_freeze_request_sha256=digest,
        transaction_id=transaction_id,
    )
    freeze = flow["phase_1_freeze_argv"]
    assert all(command[0] == "/usr/bin/sudo" and command[1].startswith("/") for command in freeze)
    assert all("python" not in command[1] for command in freeze)
    assert any(command[1:4] == ["/bin/cp", "-R", "-P"] for command in freeze)
    seal = flow["trusted_post_freeze_seal_argv"]
    assert seal[:5] == ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-B", str(deployment.QUARANTINE_ROOT / transaction_id / deployment.FREEZE_BOOTSTRAP_NAME)]
    assert "-c" not in seal
    assert "/tmp/reviewed-stage" not in seal
    activate = flow["phase_2_activate_argv"]
    assert activate[4].startswith(str(deployment.QUARANTINE_ROOT / transaction_id))
    assert "/tmp/reviewed-stage" not in activate
    cleanup = flow["freeze_copy_failure_cleanup_argv"]
    assert cleanup == ["/usr/bin/sudo", "/bin/rm", "-rf", str(deployment.QUARANTINE_ROOT / transaction_id)]
    cleanup_checks = [item for command in flow["freeze_copy_failure_precheck_argv"] for item in command]
    assert str(deployment.QUARANTINE_ROOT / transaction_id / deployment.FREEZE_SEAL_NAME) in cleanup_checks
    assert str(deployment.QUARANTINE_ROOT / transaction_id / deployment.ACTIVATION_JOURNAL_NAME) in cleanup_checks
    assurance = {str(path): mode for path, mode in deployment._assurance_directories("codex-main-agent-a-prime")}  # noqa: SLF001
    assert str(deployment.ASSURANCE_ROOT / "launch-sessions") in assurance
    assert str(deployment.ASSURANCE_ROOT / "commissioning-launches") in assurance
    assert str(deployment.ASSURANCE_ROOT / "active") in assurance
    assert assurance[str(deployment.ASSURANCE_ROOT / "epochs")] == 0o755
    assert str(deployment.ASSURANCE_ROOT / "generations" / "codex-main-agent-a-prime") in assurance
    assert assurance[str(deployment.ASSURANCE_ROOT / "commissioning" / "codex-main-agent-a-prime")] == 0o700


def test_verifier_rejects_profile_catalog_binary_and_version_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched_layout(root):
            manifest_path, manifest = fixture_manifest(root)
            reader = fixture_uid_reader(manifest)
            profile = Path(manifest["artifacts"]["codex_profile"]["path"])
            profile.write_text("notify = ['bad']\n", encoding="utf-8")
            expect_blocked(
                "codex_profile_trust_invalid",
                lambda: deployment.verify_deployment(
                    manifest_path, uid_reader=reader, verify_ancestors=False
                ),
            )

        with patched_layout(root / "catalog-drift"):
            manifest_path, manifest = fixture_manifest(root / "catalog-drift")
            reader = fixture_uid_reader(manifest)
            catalog = Path(manifest["bindings"]["state_root_catalog"])
            catalog.write_text("SAIHAI_ROOT=/changed\n", encoding="utf-8")
            expect_blocked(
                "state_root_catalog_drift",
                lambda: deployment.verify_deployment(
                    manifest_path, uid_reader=reader, verify_ancestors=False
                ),
            )

        with patched_layout(root / "binary-drift"):
            manifest_path, manifest = fixture_manifest(root / "binary-drift")
            reader = fixture_uid_reader(manifest)
            binary = Path(manifest["artifacts"]["native_codex"]["path"])
            binary.chmod(0o755)
            binary.write_text("#!/bin/sh\necho altered\n", encoding="utf-8")
            binary.chmod(0o555)
            expect_blocked(
                "runtime_bundle_digest_mismatch",
                lambda: deployment.verify_deployment(
                    manifest_path, uid_reader=reader, verify_ancestors=False
                ),
            )

        with patched_layout(root / "version-drift"):
            _manifest_path, manifest = fixture_manifest(root / "version-drift")
            binary = Path(manifest["artifacts"]["native_codex"]["path"])
            binary.chmod(0o755)
            binary.write_text("#!/bin/sh\necho 'codex-cli 9.9.9'\n", encoding="utf-8")
            binary.chmod(0o555)
            expect_blocked(
                "native_codex_version_mismatch",
                lambda: deployment._verify_native_codex_version(manifest),  # noqa: SLF001
            )


def test_verifier_rejects_codex_home_symlink_and_alternate_base_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched_layout(root / "base-config"):
            manifest_path, manifest = fixture_manifest(root / "base-config")
            reader = fixture_uid_reader(manifest)
            codex_home = Path(manifest["bindings"]["codex_home"])
            write_file(codex_home / "config.toml", b"notify = ['bypass']\n", 0o600)
            expect_blocked(
                "codex_home_base_config_forbidden",
                lambda: deployment.verify_deployment(
                    manifest_path, uid_reader=reader, verify_ancestors=False
                ),
            )

        with patched_layout(root / "symlink-home"):
            manifest_path, manifest = fixture_manifest(root / "symlink-home")
            reader = fixture_uid_reader(manifest)
            codex_home = Path(manifest["bindings"]["codex_home"])
            real_home = codex_home.with_name("codex-home-real")
            codex_home.rename(real_home)
            codex_home.symlink_to(real_home, target_is_directory=True)
            expect_blocked(
                "codex_home_trust_invalid",
                lambda: deployment.verify_deployment(
                    manifest_path, uid_reader=reader, verify_ancestors=False
                ),
            )


def test_fixed_launcher_rejects_argv_and_ignores_alternate_home() -> None:
    template = (PROFILE_ROOT / "saihai-codex-main-agent.launcher.example").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        capture = root / "capture.txt"
        capture_argv = root / "capture.argv"
        native = root / "native-codex"
        native_script = (
            "#!/bin/sh\n"
            "if [ \"${1-}\" = \"--version\" ]; then echo 'codex-cli 0.144.1'; exit 0; fi\n"
            f"printf 'HOME=%s\\nCODEX_HOME=%s\\n' \"$HOME\" \"$CODEX_HOME\" > {capture!s}\n"
            f"printf '%s\\n' \"$@\" > {capture_argv!s}\n"
        )
        write_file(native, native_script.encode("utf-8"), 0o555)
        python = root / "python"
        verifier = root / "verify.py"
        module = root / "deployment.py"
        supervisor = root / "supervisor.py"
        manifest = root / "manifest.json"
        profile = root / "home/.codex-saihai-main-agent/saihai-main-agent.config.toml"
        for path, value, mode in [
            (python, b"#!/bin/sh\nexit 0\n", 0o555),
            (verifier, b"# verifier\n", 0o444),
            (module, b"# module\n", 0o444),
            (supervisor, b"# supervisor\n", 0o444),
            (manifest, b"{}\n", 0o444),
            (profile, b"notify = []\n", 0o600),
        ]:
            write_file(path, value, mode)
        launcher_text = deployment.render_launcher(
            template,
            native_codex=native,
            native_codex_sha256=deployment.sha256_file(native),
            native_codex_version=deployment.NATIVE_CODEX_VERSION,
            python_executable=python,
            verifier=verifier,
            verifier_sha256=deployment.sha256_file(verifier),
            deployment_module=module,
            deployment_module_sha256=deployment.sha256_file(module),
            supervisor=supervisor,
            supervisor_sha256=deployment.sha256_file(supervisor),
            manifest_path=manifest,
            runtime_user_uid=os.getuid(),
            runtime_user_name=pwd.getpwuid(os.getuid()).pw_name,
            runtime_user_home=root / "home",
            codex_home=root / "home/.codex-saihai-main-agent",
            profile_path=profile,
            profile_sha256=deployment.sha256_file(profile),
        )
        launcher = root / "launcher"
        write_file(launcher, launcher_text.encode("utf-8"), 0o555)
        rejected = subprocess.run([str(launcher), "--config", "notify=['bad']"], capture_output=True, text=True)
        assert rejected.returncode == 64
        assert not capture.exists()
        non_admin = subprocess.run(
            [str(launcher)],
            capture_output=True,
            text=True,
            env={"HOME": "/tmp/attacker-home", "TERM": "xterm"},
        )
        assert non_admin.returncode == 77
        assert "requires a human sudo invocation" in non_admin.stderr
        assert not capture.exists()
        assert str(supervisor) in launcher_text
        assert "SUDO_UID" in launcher_text and "SUDO_USER" in launcher_text
        assert '"$python_executable" -I -B "$supervisor"' in launcher_text
        assert deployment.native_codex_argv(native)[0] == str(native)
        assert "--sandbox" not in deployment.native_codex_argv(native)
        assert 'sandbox_mode="read-only"' not in deployment.NATIVE_CODEX_FIXED_CONFIG_OVERRIDES
        profile_text = (PROFILE_ROOT / "codex-main-agent.config.example.toml").read_text(
            encoding="utf-8"
        )
        feature_block = profile_text.split("[features]", 1)[1].split("[mcp_servers", 1)[0]
        pinned_false_features = [
            line.split("=", 1)[0].strip()
            for line in feature_block.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and line.rstrip().endswith("= false")
        ]
        assert len(pinned_false_features) >= 10
        for feature in pinned_false_features:
            assert f"features.{feature}=false" in deployment.NATIVE_CODEX_FIXED_CONFIG_OVERRIDES


def test_runtime_staging_does_not_create_fake_git_marker() -> None:
    source = inspect.getsource(deployment.prepare_deployment)
    assert 'runtime_stage / ".git"' not in source
    assert "_copy_new_regular(native_codex_executable" in source


def test_deployed_env_override_is_forbidden_and_immutable_imports_work() -> None:
    env = dict(os.environ)
    env.update(
        {
            "SAIHAI_DEPLOYED_STATE_ROOT": "/tmp/attacker-state",
            "SAIHAI_DEPLOYED_MANAGED_PRIMARY": "/tmp/attacker-primary",
            "SAIHAI_DEPLOYED_STATE_ROOT_CATALOG": "/tmp/attacker-primary/directory-path.env",
        }
    )
    for module in ("frontdoor_orchestrator", "main_agent_bridge_mcp"):
        blocked = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                f"import sys; sys.path.insert(0, {str(SCRIPT_ROOT)!r}); import {module}",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert blocked.returncode != 0
        assert "deployed state-root environment override forbidden" in blocked.stderr

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Path(tmp) / "Runtime" / ("a" * 40)
        runtime_scripts = runtime / "organization/runtime/workflows/scripts"
        shutil.copytree(SCRIPT_ROOT, runtime_scripts)
        runtime_profiles = runtime / "organization/runtime/workflows/profiles"
        shutil.copytree(PROFILE_ROOT, runtime_profiles)
        shutil.copytree(
            WORKFLOW_ROOT / "schemas",
            runtime / "organization/runtime/workflows/schemas",
        )
        shutil.copy2(WORKFLOW_ROOT.parents[2] / "directory_paths.py", runtime / "directory_paths.py")
        shutil.copy2(WORKFLOW_ROOT.parents[2] / "saihai_env.py", runtime / "saihai_env.py")
        assert not (runtime / ".git").exists()
        imported = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(runtime_scripts)!r}); "
                    "import agent_integration_canary, agent_integration_attester, main_agent_bridge_mcp; "
                    "print('immutable_import_ok')"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert imported.returncode == 0, imported.stderr
        assert imported.stdout.strip() == "immutable_import_ok"


def test_native_executable_override_is_explicit_in_installer_cli() -> None:
    assert "operation=action" in inspect.getsource(deployment.install_main)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_ROOT / "codex_main_agent_install.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--native-codex-executable" in result.stdout
    if os.geteuid() == 0:
        return
    non_admin = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_ROOT / "codex_main_agent_install.py"),
            "activate",
            "--frozen-root",
            "/Library/Application Support/Saihai/Quarantine/test",
            "--approved-freeze-request-sha256",
            "sha256:" + "0" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert non_admin.returncode == 2
    assert "administrator_uid_required" in non_admin.stdout


def test_version_spoof_with_wrong_distribution_digest_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "codex"
        write_file(fake, FAKE_NATIVE_CODEX, 0o555)
        assert deployment._native_codex_version(fake) == deployment.NATIVE_CODEX_VERSION  # noqa: SLF001
        expect_blocked(
            "native_codex_distribution_digest_mismatch",
            lambda: deployment._assert_trusted_native_codex_distribution(fake),  # noqa: SLF001
        )


def test_freeze_gate_rejects_request_bootstrap_payload_and_seal_tamper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        frozen, digest, request = frozen_stage_fixture(root / "request")
        request_path = frozen / deployment.FREEZE_REQUEST_NAME
        request_path.chmod(0o600)
        request_path.write_text("{}\n", encoding="utf-8")
        expect_blocked(
            "freeze_request_digest_mismatch",
            lambda: deployment.verify_frozen_stage(
                frozen,
                digest,
                expected_uid=os.getuid(),
                require_seal=False,
                enforce_production_root=False,
            ),
        )

        frozen, digest, request = frozen_stage_fixture(root / "bootstrap")
        bootstrap = frozen / deployment.FREEZE_BOOTSTRAP_NAME
        bootstrap.chmod(0o600)
        bootstrap.write_text("# replacement\n", encoding="utf-8")
        bootstrap.chmod(0o400)
        expect_blocked(
            "freeze_bootstrap_digest_mismatch",
            lambda: deployment.verify_frozen_stage(
                frozen,
                digest,
                expected_uid=os.getuid(),
                require_seal=False,
                enforce_production_root=False,
            ),
        )

        frozen, digest, request = frozen_stage_fixture(root / "symlink")
        data = frozen / deployment.PAYLOAD_DIR_NAME / "data.txt"
        data.unlink()
        data.symlink_to("/etc/passwd")
        expect_blocked(
            "payload_tree_special_file",
            lambda: deployment.verify_frozen_stage(
                frozen,
                digest,
                expected_uid=os.getuid(),
                require_seal=False,
                enforce_production_root=False,
            ),
        )

        frozen, digest, request = frozen_stage_fixture(root / "special")
        os.mkfifo(frozen / deployment.PAYLOAD_DIR_NAME / "fifo")
        expect_blocked(
            "payload_tree_special_file",
            lambda: deployment.verify_frozen_stage(
                frozen,
                digest,
                expected_uid=os.getuid(),
                require_seal=False,
                enforce_production_root=False,
            ),
        )

        frozen, digest, request = frozen_stage_fixture(root / "no-seal")
        checksummed = subprocess.run(
            [
                "/usr/bin/shasum",
                "-a",
                "256",
                "-c",
                str(frozen / deployment.FREEZE_CHECKSUMS_NAME),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert checksummed.returncode == 0, checksummed.stderr
        expect_blocked(
            "freeze_seal_unloadable",
            lambda: deployment.verify_frozen_stage(
                frozen,
                digest,
                expected_uid=os.getuid(),
                require_seal=True,
                enforce_production_root=False,
            ),
        )

        frozen, digest, request = frozen_stage_fixture(root / "unexpected-entry")
        write_file(frozen / "attacker.py", b"raise SystemExit('executed')\n", 0o400)
        expect_blocked(
            "frozen_root_entries_invalid",
            lambda: deployment.verify_frozen_stage(
                frozen,
                digest,
                expected_uid=os.getuid(),
                require_seal=False,
                enforce_production_root=False,
            ),
        )


def test_secure_copy_rejects_source_replacement_race() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source"
        destination = root / "destination"
        write_file(source, b"a" * (2 * 1024 * 1024), 0o600)
        source_inode = source.stat().st_ino
        original_read = deployment.os.read
        changed = False

        def racing_read(fd: int, size: int) -> bytes:
            nonlocal changed
            chunk = original_read(fd, size)
            if chunk and not changed and os.fstat(fd).st_ino == source_inode:
                changed = True
                with source.open("r+b") as handle:
                    handle.seek(0)
                    handle.write(b"b")
                    handle.flush()
                    os.fsync(handle.fileno())
            return chunk

        deployment.os.read = racing_read
        try:
            expect_blocked(
                "install_source_changed",
                lambda: deployment._copy_regular_new(  # noqa: SLF001
                    source,
                    destination,
                    mode=0o600,
                    uid=os.getuid(),
                    gid=os.getgid(),
                ),
            )
        finally:
            deployment.os.read = original_read
        assert not destination.exists()


def test_runtime_user_home_validator_is_shared_and_fail_closed() -> None:
    callers = (
        deployment.prepare_deployment,
        deployment.verify_frozen_stage,
        deployment._prepare_codex_home,  # noqa: SLF001 - static shared-gate contract
        deployment._verify_codex_home,  # noqa: SLF001 - static shared-gate contract
    )
    for caller in callers:
        assert "require_trusted_runtime_user_home" in inspect.getsource(caller)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        runtime_uid = os.getuid()
        home = root / "trusted-home"
        home.mkdir(mode=0o700)
        home.chmod(0o700)
        reader = lambda path: runtime_uid if path == home else 0
        assert deployment.require_trusted_runtime_user_home(
            home,
            runtime_uid,
            uid_reader=reader,
            verify_ancestors=False,
        ) == home
        for mode in (0o775, 0o757):
            home.chmod(mode)
            expect_blocked(
                "runtime_user_home_trust_invalid",
                lambda: deployment.require_trusted_runtime_user_home(
                    home,
                    runtime_uid,
                    uid_reader=reader,
                    verify_ancestors=False,
                ),
            )
        home.chmod(0o700)
        expect_blocked(
            "runtime_user_home_trust_invalid",
            lambda: deployment.require_trusted_runtime_user_home(
                home,
                runtime_uid,
                uid_reader=lambda _path: runtime_uid + 1,
                verify_ancestors=False,
            ),
        )
        symlink_home = root / "symlink-home"
        symlink_home.symlink_to(home, target_is_directory=True)
        expect_blocked(
            "runtime_user_home_trust_invalid",
            lambda: deployment.require_trusted_runtime_user_home(
                symlink_home,
                runtime_uid,
                uid_reader=lambda _path: runtime_uid,
                verify_ancestors=False,
            ),
        )
        unsafe_parent = root / "unsafe-parent"
        unsafe_parent.mkdir(mode=0o777)
        unsafe_parent.chmod(0o777)
        unsafe_home = unsafe_parent / "home"
        unsafe_home.mkdir(mode=0o700)
        unsafe_home.chmod(0o700)
        expect_blocked(
            "runtime_user_home_trust_invalid",
            lambda: deployment.require_trusted_runtime_user_home(
                unsafe_home,
                runtime_uid,
                uid_reader=lambda path: runtime_uid if path == unsafe_home else 0,
                verify_ancestors=True,
            ),
        )

    for mode in (0o775, 0o757):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patched_layout(root):
                manifest_path, manifest = fixture_manifest(root)
                user_home = Path(manifest["bindings"]["runtime_user_home"])
                user_home.chmod(mode)
                reader = fixture_uid_reader(manifest)
                expect_blocked(
                    "runtime_user_home_trust_invalid",
                    lambda: deployment.verify_deployment(
                        manifest_path,
                        uid_reader=reader,
                        verify_ancestors=False,
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patched_layout(root):
                frozen, digest, _request, manifest = frozen_deployment_fixture(
                    root, keep_installed_targets=False
                )
                user_home = Path(manifest["bindings"]["runtime_user_home"])
                user_home.chmod(mode)
                reader = fixture_uid_reader(manifest)
                expect_blocked(
                    "runtime_user_home_trust_invalid",
                    lambda: deployment.verify_frozen_stage(
                        frozen,
                        digest,
                        expected_uid=os.getuid(),
                        require_seal=True,
                        enforce_production_root=False,
                        uid_reader=reader,
                        verify_runtime_home_ancestors=False,
                    ),
                )
                expect_blocked(
                    "runtime_user_home_trust_invalid",
                    lambda: deployment.activate_frozen_deployment(
                        frozen,
                        digest,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        uid_reader=reader,
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
                assert not (frozen / deployment.ACTIVATION_JOURNAL_NAME).exists()
                assert not deployment.deployment_epoch_path(
                    manifest["deployment_id"]
                ).exists()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            managed = root / "managed"
            user_home = root / "prepare-home"
            python = root / "python3"
            native = root / "codex"
            source.mkdir()
            managed.mkdir()
            user_home.mkdir()
            user_home.chmod(mode)
            write_file(python, b"#!/bin/sh\n", 0o755)
            write_file(native, FAKE_NATIVE_CODEX, 0o555)
            stage = root / "stage"
            expect_blocked(
                "runtime_user_home_trust_invalid",
                lambda: deployment.prepare_deployment(
                    source_root=source,
                    stage_root=stage,
                    managed_primary=managed,
                    user_home=user_home,
                    python_executable=python,
                    native_codex_executable=native,
                    worker_policy_domain="isolated-worker",
                ),
            )
            assert not stage.exists()


def test_codex_home_creation_rejects_symlink_swap_without_touching_victim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        runtime_uid = os.getuid()
        runtime_gid = pwd.getpwuid(runtime_uid).pw_gid
        user_home = root / "trusted-home"
        user_home.mkdir(mode=0o700)
        user_home.chmod(0o700)
        codex_home = user_home / ".codex-saihai-main-agent"
        victim = root / "victim"
        victim.mkdir(mode=0o700)
        victim.chmod(0o600)
        victim_before = victim.lstat()
        manifest = {
            "bindings": {
                "runtime_user_home": str(user_home),
                "codex_home": str(codex_home),
                "runtime_user_uid": runtime_uid,
            }
        }
        original_mkdir = deployment.os.mkdir
        swapped = False

        def racing_mkdir(path, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            original_mkdir(path, mode, dir_fd=dir_fd)
            if path == codex_home.name and dir_fd is not None and not swapped:
                deployment.os.rename(
                    path,
                    f"{path}.moved",
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
                deployment.os.symlink(
                    str(victim),
                    path,
                    target_is_directory=True,
                    dir_fd=dir_fd,
                )
                swapped = True

        deployment.os.mkdir = racing_mkdir
        try:
            expect_blocked(
                "codex_home_race_detected",
                lambda: deployment._prepare_codex_home(  # noqa: SLF001
                    manifest,
                    admin_uid=runtime_uid,
                    admin_gid=runtime_gid,
                    uid_reader=lambda path: runtime_uid if path == user_home else path.lstat().st_uid,
                    verify_ancestors=False,
                ),
            )
        finally:
            deployment.os.mkdir = original_mkdir
        victim_after = victim.lstat()
        assert swapped is True
        assert stat.S_IMODE(victim_after.st_mode) == stat.S_IMODE(victim_before.st_mode) == 0o600
        assert victim_after.st_uid == victim_before.st_uid
        assert "os.chown" not in inspect.getsource(deployment._ensure_directory)
        assert ".chmod(" not in inspect.getsource(deployment._ensure_directory)
        assert "os.chown" not in inspect.getsource(deployment._copy_tree_new)
        assert ".chmod(" not in inspect.getsource(deployment._copy_tree_new)


def test_atomic_metadata_write_is_descriptor_relative_and_directory_durable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp).resolve() / "metadata"
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        target = parent / "epoch.json"
        events: list[str] = []
        original_fsync = deployment.os.fsync
        original_rename = deployment.os.rename

        def tracked_fsync(fd):
            observed = deployment.os.fstat(fd)
            events.append("dir_fsync" if stat.S_ISDIR(observed.st_mode) else "file_fsync")
            return original_fsync(fd)

        def tracked_rename(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
            events.append("rename")
            return original_rename(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        deployment.os.fsync = tracked_fsync
        deployment.os.rename = tracked_rename
        try:
            deployment._write_atomic_json(  # noqa: SLF001
                target,
                {"state": "transitioning"},
                mode=0o600,
                uid=os.getuid(),
                gid=os.getgid(),
            )
        finally:
            deployment.os.fsync = original_fsync
            deployment.os.rename = original_rename
        assert events == ["file_fsync", "rename", "dir_fsync"]
        assert json.loads(target.read_text(encoding="utf-8")) == {"state": "transitioning"}
        source = inspect.getsource(deployment._write_atomic_json)
        for required in ("dir_fd=parent_fd", "os.fsync(temporary_fd)", "os.rename(", "os.fsync(parent_fd)"):
            assert required in source


def test_epoch_crash_after_rename_preserves_transition_and_lock_is_nonblocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        with patched_layout(root):
            epochs = deployment.ASSURANCE_ROOT / "epochs"
            epochs.mkdir(parents=True, mode=0o755)
            deployment.ASSURANCE_ROOT.chmod(0o755)
            epochs.chmod(0o755)
            original_fsync = deployment.os.fsync
            original_rename = deployment.os.rename
            renamed = False

            def tracked_rename(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
                nonlocal renamed
                result = original_rename(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                if destination == f"{deployment.SUPPORTED_DEPLOYMENT_ID}.json":
                    renamed = True
                return result

            def interrupted_fsync(fd):
                if renamed and stat.S_ISDIR(deployment.os.fstat(fd).st_mode):
                    raise OSError("simulated crash before parent fsync")
                return original_fsync(fd)

            deployment.os.rename = tracked_rename
            deployment.os.fsync = interrupted_fsync
            try:
                expect_blocked(
                    "transaction_metadata_write_failed",
                    lambda: deployment._rotate_deployment_epoch(  # noqa: SLF001
                        deployment.SUPPORTED_DEPLOYMENT_ID,
                        operation="activate",
                        transaction_id="crash-test",
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        verify_ancestors=False,
                    ),
                )
            finally:
                deployment.os.rename = original_rename
                deployment.os.fsync = original_fsync
            transition = deployment.load_deployment_epoch(
                deployment.SUPPORTED_DEPLOYMENT_ID,
                admin_uid=os.getuid(),
                verify_ancestors=False,
            )
            assert transition is not None and transition["state"] == "transitioning"

            holder_source = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(SCRIPT_ROOT)!r})
import codex_main_agent_deployment as deployment
deployment.DEPLOYMENT_LOCK_PATH = Path({str(deployment.DEPLOYMENT_LOCK_PATH)!r})
with deployment._deployment_transaction_lock(
    admin_uid=os.getuid(),
    admin_gid=os.getgid(),
    verify_ancestors=False,
):
    print("locked", flush=True)
    sys.stdin.readline()
"""
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_source],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert holder.stdout is not None
            readable, _, _ = select.select([holder.stdout], [], [], 10)
            if not readable:
                holder.terminate()
                holder.wait(timeout=10)
                raise AssertionError(
                    "deployment lock holder readiness timed out: "
                    + (holder.stderr.read() if holder.stderr else "")
                )
            readiness = holder.stdout.readline().strip()
            if readiness != "locked":
                holder.terminate()
                _stdout, stderr = holder.communicate(timeout=10)
                raise AssertionError(f"deployment lock holder failed: {stderr}")
            try:
                callbacks = (
                    lambda: deployment.activate_frozen_deployment(
                        root / "unused",
                        "sha256:" + "0" * 64,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                    lambda: deployment.rollback_frozen_deployment(
                        root / "unused",
                        "sha256:" + "0" * 64,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        verify_ancestors=False,
                        enforce_production_root=False,
                        operation="rollback",
                    ),
                    lambda: deployment.rollback_frozen_deployment(
                            root / "unused",
                            "sha256:" + "0" * 64,
                            admin_uid=os.getuid(),
                            admin_gid=os.getgid(),
                            verify_ancestors=False,
                            enforce_production_root=False,
                            operation="uninstall",
                    ),
                )
                for callback in callbacks:
                    expect_blocked("deployment_transaction_in_progress", callback)
            finally:
                assert holder.stdin is not None
                holder.stdin.write("release\n")
                holder.stdin.flush()
                holder.wait(timeout=10)
                assert holder.returncode == 0, holder.stderr.read() if holder.stderr else ""
            lock_stat = deployment.DEPLOYMENT_LOCK_PATH.lstat()
            assert stat.S_ISREG(lock_stat.st_mode)
            assert lock_stat.st_uid == os.getuid()
            assert stat.S_IMODE(lock_stat.st_mode) == 0o600


def test_deployment_epoch_rotates_before_journal_and_never_reverts_on_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        with patched_layout(root / "ordering"):
            frozen, digest, request, manifest = frozen_deployment_fixture(
                root / "ordering", keep_installed_targets=False
            )
            original_write = deployment._write_atomic_json  # noqa: SLF001
            observed_before_journal = False

            def checked_write(destination, value, *, mode, uid, gid):
                nonlocal observed_before_journal
                if destination == frozen / deployment.ACTIVATION_JOURNAL_NAME:
                    epoch = deployment.load_deployment_epoch(
                        manifest["deployment_id"],
                        admin_uid=os.getuid(),
                        verify_ancestors=False,
                    )
                    assert epoch is not None
                    assert epoch["state"] == "transitioning"
                    assert epoch["operation"] == "activate"
                    assert epoch["transaction_id"] == request["transaction_id"]
                    observed_before_journal = True
                return original_write(destination, value, mode=mode, uid=uid, gid=gid)

            deployment._write_atomic_json = checked_write  # noqa: SLF001
            try:
                result = deployment.activate_frozen_deployment(
                    frozen,
                    digest,
                    admin_uid=os.getuid(),
                    admin_gid=os.getgid(),
                    uid_reader=fixture_uid_reader(manifest),
                    verify_ancestors=False,
                    enforce_production_root=False,
                )
            finally:
                deployment._write_atomic_json = original_write  # noqa: SLF001
            assert observed_before_journal
            assert result["deployment_epoch_state"] == "active_uncommissioned"
            epoch_path = deployment.deployment_epoch_path(manifest["deployment_id"])
            assert epoch_path.stat().st_mode & 0o777 == 0o644
            assert epoch_path.parent.stat().st_mode & 0o777 == 0o755

        with patched_layout(root / "pre-journal-interruption"):
            frozen, digest, _request, manifest = frozen_deployment_fixture(
                root / "pre-journal-interruption", keep_installed_targets=False
            )
            original_write = deployment._write_atomic_json  # noqa: SLF001

            def interrupt_journal(destination, value, *, mode, uid, gid):
                if destination == frozen / deployment.ACTIVATION_JOURNAL_NAME:
                    raise deployment.DeploymentError("journal_write_interrupted")
                return original_write(destination, value, mode=mode, uid=uid, gid=gid)

            deployment._write_atomic_json = interrupt_journal  # noqa: SLF001
            try:
                expect_blocked(
                    "journal_write_interrupted",
                    lambda: deployment.activate_frozen_deployment(
                        frozen,
                        digest,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        uid_reader=fixture_uid_reader(manifest),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
            finally:
                deployment._write_atomic_json = original_write  # noqa: SLF001
            assert not (frozen / deployment.ACTIVATION_JOURNAL_NAME).exists()
            interrupted_epoch = deployment.load_deployment_epoch(
                manifest["deployment_id"],
                admin_uid=os.getuid(),
                verify_ancestors=False,
            )
            assert interrupted_epoch is not None
            assert interrupted_epoch["state"] == "transitioning"
            epoch_path = deployment.deployment_epoch_path(manifest["deployment_id"])
            for changed in (
                {**interrupted_epoch, "operation": "rollback"},
                {**interrupted_epoch, "transaction_id": "different-transaction"},
            ):
                write_file(
                    epoch_path,
                    deployment.canonical_json_bytes(changed),
                    0o644,
                )
                expect_blocked(
                    "activation_journal_unavailable",
                    lambda: deployment.rollback_frozen_deployment(
                        frozen,
                        digest,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        uid_reader=fixture_uid_reader(manifest),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
            write_file(
                epoch_path,
                deployment.canonical_json_bytes(interrupted_epoch),
                0o644,
            )
            expect_blocked(
                "activation_journal_required_for_uninstall",
                lambda: deployment.rollback_frozen_deployment(
                    frozen,
                    digest,
                    admin_uid=os.getuid(),
                    admin_gid=os.getgid(),
                    uid_reader=fixture_uid_reader(manifest),
                    verify_ancestors=False,
                    enforce_production_root=False,
                    operation="uninstall",
                ),
            )
            residual_backup = deployment.BACKUP_ROOT / interrupted_epoch["transaction_id"]
            write_file(residual_backup / "partial", b"partial\n", 0o600)
            expect_blocked(
                "activation_journal_missing_with_transaction_artifacts",
                lambda: deployment.rollback_frozen_deployment(
                    frozen,
                    digest,
                    admin_uid=os.getuid(),
                    admin_gid=os.getgid(),
                    uid_reader=fixture_uid_reader(manifest),
                    verify_ancestors=False,
                    enforce_production_root=False,
                ),
            )
            shutil.rmtree(residual_backup)
            recovered = deployment.rollback_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                uid_reader=fixture_uid_reader(manifest),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            assert recovered["journal_recovery"] == "pre_journal_transition"
            assert recovered["deployment_epoch_state"] == "restored_uncommissioned"

        with patched_layout(root / "failure"):
            frozen, digest, _request, manifest = frozen_deployment_fixture(
                root / "failure", keep_installed_targets=False
            )
            prior_transition = deployment._rotate_deployment_epoch(  # noqa: SLF001
                manifest["deployment_id"],
                operation="activate",
                transaction_id="prior-activation",
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
            )
            deployment._finalize_deployment_epoch(  # noqa: SLF001
                prior_transition,
                state="active_uncommissioned",
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
            )
            epoch_path = deployment.deployment_epoch_path(manifest["deployment_id"])
            prior_bytes = epoch_path.read_bytes()
            prior_epoch = json.loads(prior_bytes)
            original_install = deployment._install_one_spec  # noqa: SLF001

            def interrupted(_spec, *, transaction_id):
                del transaction_id
                raise deployment.DeploymentError("epoch_test_interrupted")

            deployment._install_one_spec = interrupted  # noqa: SLF001
            try:
                expect_blocked(
                    "epoch_test_interrupted",
                    lambda: deployment.activate_frozen_deployment(
                        frozen,
                        digest,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        uid_reader=fixture_uid_reader(manifest),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
            finally:
                deployment._install_one_spec = original_install  # noqa: SLF001
            assert epoch_path.read_bytes() != prior_bytes
            failed_epoch = deployment.load_deployment_epoch(
                manifest["deployment_id"],
                admin_uid=os.getuid(),
                verify_ancestors=False,
            )
            assert failed_epoch is not None
            assert failed_epoch["state"] == "transitioning"
            assert failed_epoch["previous_epoch_id"] == prior_epoch["epoch_id"]
            epoch_path.chmod(0o600)
            expect_blocked(
                "deployment_epoch_trust_invalid",
                lambda: deployment.load_deployment_epoch(
                    manifest["deployment_id"],
                    admin_uid=os.getuid(),
                    verify_ancestors=False,
                ),
            )


def test_activation_collision_interruption_and_fresh_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched_layout(root / "collision"):
            frozen, digest, _request, manifest = frozen_deployment_fixture(
                root / "collision", keep_installed_targets=False
            )
            write_file(deployment.BRIDGE_WRAPPER_PATH, b"unowned\n", 0o555)
            expect_blocked(
                "unowned_existing_target_collision",
                lambda: deployment.activate_frozen_deployment(
                    frozen,
                    digest,
                    admin_uid=os.getuid(),
                    admin_gid=os.getgid(),
                    uid_reader=fixture_uid_reader(manifest),
                    verify_ancestors=False,
                    enforce_production_root=False,
                ),
            )
            assert not deployment.deployment_epoch_path(
                manifest["deployment_id"]
            ).exists()

        with patched_layout(root / "interrupted"):
            frozen, digest, _request, manifest = frozen_deployment_fixture(
                root / "interrupted", keep_installed_targets=False
            )
            original_install = deployment._install_one_spec  # noqa: SLF001
            calls = 0
            interrupted_temporary = None

            def interrupted(spec, *, transaction_id):
                nonlocal calls, interrupted_temporary
                calls += 1
                if calls == 2:
                    interrupted_temporary = deployment._transaction_temporary_path(  # noqa: SLF001
                        Path(spec["path"]), transaction_id
                    )
                    interrupted_temporary.parent.mkdir(parents=True, exist_ok=True)
                    write_file(interrupted_temporary, b"partial transaction file\n", 0o600)
                    raise deployment.DeploymentError("test_interrupted_install")
                return original_install(spec, transaction_id=transaction_id)

            deployment._install_one_spec = interrupted  # noqa: SLF001
            try:
                expect_blocked(
                    "test_interrupted_install",
                    lambda: deployment.activate_frozen_deployment(
                        frozen,
                        digest,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        uid_reader=fixture_uid_reader(manifest),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
            finally:
                deployment._install_one_spec = original_install  # noqa: SLF001
            journal = json.loads((frozen / deployment.ACTIVATION_JOURNAL_NAME).read_text(encoding="utf-8"))
            assert journal["state"] == "backup_complete"
            interrupted_epoch = deployment.load_deployment_epoch(
                manifest["deployment_id"],
                admin_uid=os.getuid(),
                verify_ancestors=False,
            )
            assert interrupted_epoch is not None
            assert interrupted_epoch["state"] == "transitioning"
            assert interrupted_epoch["operation"] == "activate"
            assert Path(manifest["artifacts"]["runtime_bundle"]["path"]).exists()
            assert interrupted_temporary is not None and interrupted_temporary.exists()
            recovered = deployment.rollback_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            assert recovered["deployment_epoch_state"] == "restored_uncommissioned"
            recovered_epoch = deployment.load_deployment_epoch(
                manifest["deployment_id"],
                admin_uid=os.getuid(),
                verify_ancestors=False,
            )
            assert recovered_epoch is not None
            assert recovered_epoch["previous_epoch_id"] == interrupted_epoch["epoch_id"]
            assert not Path(manifest["artifacts"]["runtime_bundle"]["path"]).exists()
            assert not interrupted_temporary.exists()

        with patched_layout(root / "backup-interrupted"):
            frozen, digest, request, manifest = frozen_deployment_fixture(
                root / "backup-interrupted", keep_installed_targets=False
            )
            original_backup = deployment._backup_prior_deployment  # noqa: SLF001

            def interrupted_backup(
                prior_specs,
                *,
                backup_root,
                transaction_id,
                admin_uid,
                admin_gid,
            ):
                del prior_specs, transaction_id
                backup_root.mkdir(parents=True)
                backup_root.chmod(0o700)
                os.chown(backup_root, admin_uid, admin_gid)
                write_file(backup_root / "partial", b"partial\n", 0o600)
                raise deployment.DeploymentError("test_backup_interrupted")

            deployment._backup_prior_deployment = interrupted_backup  # noqa: SLF001
            try:
                expect_blocked(
                    "test_backup_interrupted",
                    lambda: deployment.activate_frozen_deployment(
                        frozen,
                        digest,
                        admin_uid=os.getuid(),
                        admin_gid=os.getgid(),
                        uid_reader=fixture_uid_reader(manifest),
                        verify_ancestors=False,
                        enforce_production_root=False,
                    ),
                )
            finally:
                deployment._backup_prior_deployment = original_backup  # noqa: SLF001
            journal = json.loads((frozen / deployment.ACTIVATION_JOURNAL_NAME).read_text(encoding="utf-8"))
            assert journal["state"] == "preflight_complete"
            failed_epoch = deployment.load_deployment_epoch(
                manifest["deployment_id"],
                admin_uid=os.getuid(),
                verify_ancestors=False,
            )
            assert failed_epoch is not None and failed_epoch["state"] == "transitioning"
            assert (deployment.BACKUP_ROOT / request["transaction_id"]).exists()
            recovered = deployment.rollback_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            assert recovered["deployment_epoch_state"] == "restored_uncommissioned"
            assert not (deployment.BACKUP_ROOT / request["transaction_id"]).exists()

        with patched_layout(root / "rollback"):
            frozen, digest, _request, manifest = frozen_deployment_fixture(
                root / "rollback", keep_installed_targets=False
            )
            result = deployment.activate_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                uid_reader=fixture_uid_reader(manifest),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            assert result["decision"] == "activated"
            assert result["deployment_epoch_state"] == "active_uncommissioned"
            assert deployment.MANIFEST_PATH.exists()
            rolled_back = deployment.rollback_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            assert rolled_back["decision"] == "rolled_back"
            assert rolled_back["deployment_epoch_state"] == "restored_uncommissioned"
            assert not deployment.MANIFEST_PATH.exists()
            assert not Path(manifest["artifacts"]["runtime_bundle"]["path"]).exists()
            uninstalled = deployment.rollback_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
                enforce_production_root=False,
                operation="uninstall",
            )
            assert uninstalled["deployment_epoch_state"] == "uninstalled"


def test_activation_backup_manifest_binds_prior_digest_owner_mode_and_restores() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patched_layout(root):
            frozen, digest, request, manifest = frozen_deployment_fixture(
                root, keep_installed_targets=True
            )
            before = {
                "manifest": deployment.sha256_file(deployment.MANIFEST_PATH),
                "runtime": deployment.tree_digest(Path(manifest["artifacts"]["runtime_bundle"]["path"])),
            }
            result = deployment.activate_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                uid_reader=fixture_uid_reader(manifest),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            backup = json.loads(Path(result["backup_manifest"]).read_text(encoding="utf-8"))
            assert backup["prior_deployment_present"] is True
            assert backup["entries"]
            for entry in backup["entries"]:
                assert {"sha256", "uid", "gid", "mode", "path", "backup_path"}.issubset(entry)
            deployment.rollback_frozen_deployment(
                frozen,
                digest,
                admin_uid=os.getuid(),
                admin_gid=os.getgid(),
                verify_ancestors=False,
                enforce_production_root=False,
            )
            assert deployment.sha256_file(deployment.MANIFEST_PATH) == before["manifest"]
            assert deployment.tree_digest(Path(manifest["artifacts"]["runtime_bundle"]["path"])) == before["runtime"]


def main() -> None:
    tests = [
        test_example_matches_schema_and_manual_validator,
        test_v01_deployment_identity_is_fixed_at_every_gate,
        test_shared_worker_rootfs_is_rejected,
        test_developer_instructions_digest_is_manifest_bound,
        test_wrapper_is_zero_argument_and_pins_host_bindings,
        test_tree_digest_rejects_symlinks,
        test_safe_ancestors_reject_symlink_directory,
        test_verifier_accepts_exact_fixture_and_rejects_tamper,
        test_verifier_rejects_symlink_artifact,
        test_profiles_pin_wrapper_tools_sandbox_and_sensitive_reads,
        test_admin_plan_freezes_data_before_any_root_owned_saihai_execution,
        test_verifier_rejects_profile_catalog_binary_and_version_drift,
        test_verifier_rejects_codex_home_symlink_and_alternate_base_config,
        test_fixed_launcher_rejects_argv_and_ignores_alternate_home,
        test_runtime_staging_does_not_create_fake_git_marker,
        test_deployed_env_override_is_forbidden_and_immutable_imports_work,
        test_native_executable_override_is_explicit_in_installer_cli,
        test_version_spoof_with_wrong_distribution_digest_is_rejected,
        test_freeze_gate_rejects_request_bootstrap_payload_and_seal_tamper,
        test_secure_copy_rejects_source_replacement_race,
        test_runtime_user_home_validator_is_shared_and_fail_closed,
        test_codex_home_creation_rejects_symlink_swap_without_touching_victim,
        test_atomic_metadata_write_is_descriptor_relative_and_directory_durable,
        test_epoch_crash_after_rename_preserves_transition_and_lock_is_nonblocking,
        test_deployment_epoch_rotates_before_journal_and_never_reverts_on_failure,
        test_activation_collision_interruption_and_fresh_rollback,
        test_activation_backup_manifest_binds_prior_digest_owner_mode_and_restores,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, sort_keys=True))


if __name__ == "__main__":
    main()
