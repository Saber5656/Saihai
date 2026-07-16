#!/usr/bin/env python3
"""Prepare, activate, verify, and recover a Codex A-prime deployment.

Unprivileged preparation is strictly data-only: it never invokes sudo and
never writes a production destination. A human reviews the displayed digests
and uses absolute system tools to freeze the stage as root-owned data. Only a
separately digest-approved, root-copied bootstrap may seal that tree; the
sealed importer then performs transactional activation or rollback.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import grp
import hashlib
import io
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = WORKFLOW_ROOT.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from directory_paths import EnvError as DirectoryPathError  # noqa: E402
from directory_paths import parse_env as parse_directory_catalog  # noqa: E402

PROFILE_ROOT = WORKFLOW_ROOT / "profiles"
SCHEMA_PATH = WORKFLOW_ROOT / "schemas" / "codex-main-agent-deployment.schema.json"
MANIFEST_EXAMPLE_PATH = PROFILE_ROOT / "codex-main-agent.deployment.example.json"
WRAPPER_TEMPLATE_PATH = PROFILE_ROOT / "saihai-codex-main-agent-bridge.wrapper.example"
LAUNCHER_TEMPLATE_PATH = PROFILE_ROOT / "saihai-codex-main-agent.launcher.example"
PROFILE_TEMPLATE_PATH = PROFILE_ROOT / "codex-main-agent.config.example.toml"
REQUIREMENTS_TEMPLATE_PATH = PROFILE_ROOT / "codex-main-agent.requirements.example.toml"
INSTRUCTIONS_RELATIVE_PATH = "organization/runtime/workflows/profiles/codex-main-agent.instructions.md"
ENTRYPOINT_RELATIVE_PATH = "organization/runtime/workflows/scripts/main_agent_bridge_mcp.py"
VERIFIER_RELATIVE_PATH = "organization/runtime/workflows/scripts/codex_main_agent_verify.py"
DEPLOYMENT_MODULE_RELATIVE_PATH = "organization/runtime/workflows/scripts/codex_main_agent_deployment.py"
SUPERVISOR_RELATIVE_PATH = "organization/runtime/workflows/scripts/codex_main_agent_supervisor.py"
OBSERVER_RELATIVE_PATH = "organization/runtime/workflows/scripts/agent_integration_observer.py"

BRIDGE_WRAPPER_PATH = Path("/usr/local/libexec/saihai-codex-main-agent-bridge")
CODEX_LAUNCHER_PATH = Path("/usr/local/bin/saihai-codex-main-agent")
MANIFEST_PATH = Path("/Library/Application Support/Saihai/Manifests/codex-main-agent.deployment.json")
RUNTIME_CONFIG_PATH = Path("/Library/Application Support/Saihai/Config/codex-main-agent.runtime.json")
ASSURANCE_ROOT = Path("/Library/Application Support/Saihai/Assurance")
QUARANTINE_ROOT = Path("/Library/Application Support/Saihai/Quarantine")
BACKUP_ROOT = Path("/Library/Application Support/Saihai/Backups")
# Codex discovers this through /etc/codex/requirements.toml. macOS exposes
# /etc as a symlink to /private/etc, so bind the canonical path and reject all
# symlink ancestors without an exception in the verifier.
REQUIREMENTS_PATH = Path("/private/etc/codex/requirements.toml")
RUNTIME_ROOT = Path("/Library/Application Support/Saihai/Runtime")
DEPLOYMENT_LOCK_PATH = Path("/Library/Application Support/Saihai/deployment.lock")
PYTHON_EXECUTABLE = Path("/usr/bin/python3")
NATIVE_CODEX_EXECUTABLE = Path(
    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
)
NATIVE_CODEX_VERSION = "codex-cli 0.144.1"
# Reviewed npm distribution identity for @openai/codex's darwin-arm64 alias.
# A version string alone is not trust evidence: staging requires both this SRI
# provenance and the independently unpacked native binary SHA-256 below.
NATIVE_CODEX_PACKAGE = "@openai/codex"
NATIVE_CODEX_PACKAGE_VERSION = "0.144.1-darwin-arm64"
NATIVE_CODEX_PACKAGE_INTEGRITY = (
    "sha512-dABeDK+ATqMG54MGBd3VjpKfh5EOoqx9PKVQB2QYDaEXx3F6CdUCXue5QIMfr4OxziUj8pUcLAQyd+KFqiTUFw=="
)
NATIVE_CODEX_PLATFORM = "darwin"
NATIVE_CODEX_ARCHITECTURE = "arm64"
NATIVE_CODEX_EXPECTED_SHA256 = (
    "sha256:29915529b97697def1a957b0505e770aa6a45744435d62fc263e98d7619e167a"
)
CODEX_PROFILE_NAME = "saihai-main-agent"
NATIVE_CODEX_FIXED_CONFIG_OVERRIDES = (
    'approval_policy="never"',
    'default_permissions="saihai_frontend"',
    "notify=[]",
    'web_search="disabled"',
    "features.shell_tool=false",
    "features.shell_snapshot=false",
    "features.unified_exec=false",
    "features.code_mode=false",
    "features.code_mode_host=false",
    "features.code_mode_only=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.enable_fanout=false",
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.plugin_sharing=false",
    "features.tool_suggest=false",
    "features.memories=false",
    "features.exec_permission_approvals=false",
    "features.standalone_web_search=false",
    "features.realtime_conversation=false",
    "features.artifact=false",
    "features.prevent_idle_sleep=false",
    "features.image_generation=false",
    "features.in_app_browser=false",
    "features.browser_use=false",
    "features.browser_use_full_cdp_access=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.skill_mcp_dependency_install=false",
    "features.request_permissions_tool=false",
    "features.auth_elicitation=false",
    "features.tool_call_mcp_elicitation=false",
    "features.guardian_approval=false",
    "features.workspace_dependencies=false",
    "features.hooks=false",
    "features.goals=false",
)

EXPECTED_TOP_LEVEL = {
    "deployment_version",
    "deployment_id",
    "release_commit",
    "platform",
    "expected_admin_uid",
    "topology",
    "bindings",
    "artifacts",
    "tool_contract",
}
EXPECTED_BINDINGS = {
    "frontdoor",
    "principal_id",
    "workspace_id",
    "managed_primary",
    "checkout_binding",
    "workspace_catalog",
    "assurance_root",
    "state_root_catalog",
    "deployment_manifest_path",
    "runtime_config_path",
    "instructions_path",
    "developer_instructions_sha256",
    "runtime_user_uid",
    "runtime_user_name",
    "runtime_user_home",
    "codex_home",
    "state_root",
    "state_root_source",
    "state_root_catalog_observation",
}
EXPECTED_ARTIFACTS = {
    "runtime_bundle",
    "bridge_wrapper",
    "python_executable",
    "runtime_config",
    "requirements",
    "instructions",
    "codex_launcher",
    "codex_profile",
    "native_codex",
    "supervisor",
    "observer",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
MODE = re.compile(r"^0[0-7]{3}$")
USER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
SAFE_STATE_ROOT = re.compile(r"^/(?:[\w .,'@%+=:~&()-]+/)*[\w .,'@%+=:~&()-]+/?$")
ENABLED_TOOLS = ["submit_request", "read_projection", "ack_output"]
REQUIRED_FORBIDDEN_TOOLS = {"approve", "execute", "shell"}
SUPPORTED_DEPLOYMENT_ID = "codex-main-agent-a-prime"
SUPPORTED_PRINCIPAL_ID = "codex-main-agent-a-prime"

FREEZE_REQUEST_NAME = "freeze-request.json"
FREEZE_CHECKSUMS_NAME = "payload.sha256"
FREEZE_BOOTSTRAP_NAME = "freeze-bootstrap.py"
FREEZE_SEAL_NAME = "freeze-seal.json"
ACTIVATION_JOURNAL_NAME = "activation-journal.json"
BACKUP_MANIFEST_NAME = "backup-manifest.json"
PAYLOAD_DIR_NAME = "payload"
FREEZE_VERSION = "1"
TRANSACTION_VERSION = "1"
DEPLOYMENT_EPOCH_VERSION = "1"
DEPLOYMENT_EPOCH_STATES = {
    "transitioning",
    "active_uncommissioned",
    "restored_uncommissioned",
    "uninstalled",
}
DEPLOYMENT_EPOCH_OPERATIONS = {"activate", "rollback", "uninstall"}
DEPLOYMENT_EPOCH_FIELDS = {
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
_FROZEN_MUTABLE_METADATA = {FREEZE_SEAL_NAME, ACTIVATION_JOURNAL_NAME}

# This self-contained bootstrap is written as data into the reviewed stage.
# Phase 1 copies the stage strictly as data with
# absolute system tools.  After the administrator independently compares the
# copied request *and bootstrap* digests, only the root-owned copy of this file
# runs. It verifies the complete payload and writes the seal that permits the
# copied Saihai importer to run. It imports no checkout/stage module and
# executes no payload file.
POST_FREEZE_BOOTSTRAP_SOURCE = r'''import hashlib,json,os,stat,sys
Q="/Library/Application Support/Saihai/Quarantine"
F={"freeze_version","transaction_id","deployment_id","release_commit","frozen_root","payload_relative_path","payload_tree_sha256","payload_entries_sha256","payload_entries","payload_regular_file_count","payload_directory_count","checksums_relative_path","checksums_sha256","bootstrap_relative_path","bootstrap_sha256","activation_importer_relative_path","activation_importer_sha256"}
def die(reason):
 print(json.dumps({"decision":"blocked","reason":reason},sort_keys=True));raise SystemExit(2)
def ident(s):return (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def read(p):
 try:
  a=os.lstat(p);fd=os.open(p,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
 except OSError:die("frozen_file_unopenable")
 try:
  b=os.fstat(fd)
  if not stat.S_ISREG(b.st_mode) or stat.S_ISLNK(a.st_mode):die("frozen_file_not_regular")
  out=[]
  while True:
   c=os.read(fd,1048576)
   if not c:break
   out.append(c)
  d=os.fstat(fd)
 finally:os.close(fd)
 try:e=os.lstat(p)
 except OSError:die("frozen_file_changed")
 if ident(a)!=ident(b) or ident(b)!=ident(d) or ident(d)!=ident(e):die("frozen_file_changed")
 v=b"".join(out)
 if len(v)!=b.st_size:die("frozen_file_changed")
 return v,b
def h(v):return "sha256:"+hashlib.sha256(v).hexdigest()
def canon(v):return (json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()
def safe_rel(v):return isinstance(v,str) and v and not v.startswith("/") and "\0" not in v and "\r" not in v and "\n" not in v and all(x not in ("",".","..") for x in v.split("/"))
def inventory(root):
 out=[];todo=[root]
 while todo:
  parent=todo.pop()
  try:children=sorted(os.scandir(parent),key=lambda x:x.name,reverse=True)
  except OSError:die("frozen_payload_unreadable")
  for child in children:
   p=child.path;rel=os.path.relpath(p,root).replace(os.sep,"/")
   if not safe_rel(rel):die("frozen_payload_path_invalid")
   try:s=os.lstat(p)
   except OSError:die("frozen_payload_unreadable")
   if s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:die("frozen_payload_trust_invalid")
   mode="0"+format(stat.S_IMODE(s.st_mode),"03o")
   if stat.S_ISDIR(s.st_mode):out.append({"kind":"directory","mode":mode,"path":rel});todo.append(p)
   elif stat.S_ISREG(s.st_mode):
    data,ss=read(p);out.append({"kind":"file","mode":mode,"path":rel,"sha256":h(data),"size":ss.st_size})
   else:die("frozen_payload_special_file")
 return sorted(out,key=lambda x:x["path"])
def td(entries):
 d=hashlib.sha256(b"saihai-tree-sha256-v1\0")
 for e in entries:
  if e["kind"]=="directory":d.update(("D\0%s\0%s\n"%(e["path"],e["mode"])).encode())
  else:d.update(("F\0%s\0%s\0%s\0%s\n"%(e["path"],e["mode"],e["size"],e["sha256"][7:])).encode())
 return "sha256:"+d.hexdigest()
if len(sys.argv)!=3 or os.geteuid()!=0:die("trusted_bootstrap_invocation_invalid")
root=os.path.realpath(sys.argv[1]);approved=sys.argv[2]
if os.path.dirname(root)!=Q or not os.path.basename(root):die("frozen_root_outside_quarantine")
try:rs=os.lstat(root)
except OSError:die("frozen_root_unavailable")
if not stat.S_ISDIR(rs.st_mode) or stat.S_ISLNK(rs.st_mode) or rs.st_uid!=0 or stat.S_IMODE(rs.st_mode)!=0o700:die("frozen_root_trust_invalid")
try:names={x.name for x in os.scandir(root)}
except OSError:die("frozen_root_unavailable")
if names!={"payload","freeze-request.json","payload.sha256","freeze-bootstrap.py"}:die("frozen_root_entries_invalid")
request_bytes,request_stat=read(os.path.join(root,"freeze-request.json"))
if request_stat.st_uid!=0 or stat.S_IMODE(request_stat.st_mode)!=0o600 or h(request_bytes)!=approved:die("freeze_request_digest_mismatch")
try:r=json.loads(request_bytes.decode())
except Exception:die("freeze_request_unloadable")
if not isinstance(r,dict) or set(r)!=F or r.get("freeze_version")!="1" or r.get("deployment_id")!="codex-main-agent-a-prime" or r.get("frozen_root")!=root or r.get("transaction_id")!=os.path.basename(root) or r.get("transaction_id")!=str(r.get("deployment_id"))+"-"+str(r.get("release_commit"))[:12] or r.get("payload_relative_path")!="payload" or r.get("checksums_relative_path")!="payload.sha256" or r.get("bootstrap_relative_path")!="freeze-bootstrap.py":die("freeze_request_invalid")
bootstrap,bs=read(os.path.join(root,"freeze-bootstrap.py"))
if bs.st_uid!=0 or stat.S_IMODE(bs.st_mode)!=0o400 or h(bootstrap)!=r.get("bootstrap_sha256") or os.path.realpath(__file__)!=os.path.join(root,"freeze-bootstrap.py"):die("freeze_bootstrap_digest_mismatch")
checksums,cs=read(os.path.join(root,"payload.sha256"))
if cs.st_uid!=0 or stat.S_IMODE(cs.st_mode)!=0o600 or h(checksums)!=r.get("checksums_sha256"):die("freeze_checksums_invalid")
payload=os.path.join(root,"payload")
try:ps=os.lstat(payload)
except OSError:die("frozen_payload_unavailable")
if not stat.S_ISDIR(ps.st_mode) or stat.S_ISLNK(ps.st_mode) or ps.st_uid!=0 or stat.S_IMODE(ps.st_mode)&0o022:die("frozen_payload_trust_invalid")
entries=inventory(payload)
if entries!=r.get("payload_entries") or h(canon(entries))!=r.get("payload_entries_sha256") or td(entries)!=r.get("payload_tree_sha256"):die("frozen_payload_digest_mismatch")
files=sum(1 for e in entries if e["kind"]=="file");dirs=len(entries)-files
if files!=r.get("payload_regular_file_count") or dirs!=r.get("payload_directory_count"):die("frozen_payload_entry_count_mismatch")
imp=r.get("activation_importer_relative_path");matches=[e for e in entries if e["kind"]=="file" and e["path"]==imp and e["sha256"]==r.get("activation_importer_sha256")]
if len(matches)!=1:die("freeze_activation_importer_invalid")
seal={"seal_version":"1","transaction_id":r["transaction_id"],"freeze_request_sha256":approved,"payload_tree_sha256":r["payload_tree_sha256"],"payload_entries_sha256":r["payload_entries_sha256"],"verified_by":"trusted-root-copied-bootstrap-v1"}
path=os.path.join(root,"freeze-seal.json")
try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
except OSError:die("freeze_seal_collision")
try:
 data=canon(seal);off=0
 while off<len(data):off+=os.write(fd,data[off:])
 os.fsync(fd)
finally:os.close(fd)
print(json.dumps({"decision":"frozen_verified","freeze_request_sha256":approved,"payload_tree_sha256":r["payload_tree_sha256"],"transaction_id":r["transaction_id"]},sort_keys=True))
'''


def native_codex_argv(native_path: Path | str) -> list[str]:
    """Return the complete fixed argv for the claimed stock Codex CLI process.

    This is the single source of truth consumed by launcher rendering and host
    canary/evidence producers. The caller supplies only the manifest-bound
    immutable executable path; no user-controlled argv is accepted.
    """

    path = str(native_path)
    if not _safe_absolute_path(path):
        raise DeploymentError("native_codex_path_invalid")
    argv = [
        path,
        "--strict-config",
        "--profile",
        CODEX_PROFILE_NAME,
        "--ask-for-approval",
        "never",
    ]
    for override in NATIVE_CODEX_FIXED_CONFIG_OVERRIDES:
        argv.extend(("-c", override))
    return argv


class DeploymentError(RuntimeError):
    """Fail-closed deployment preparation or verification error."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _stat_identity(observed: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_uid,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _inode_identity(observed: os.stat_result) -> tuple[int, int]:
    return observed.st_dev, observed.st_ino


def sha256_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before_path = path.lstat()
        fd = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError("artifact_unopenable", path.name) from exc
    digest = hashlib.sha256()
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or stat.S_ISLNK(before_path.st_mode):
            raise DeploymentError("artifact_not_regular", path.name)
        count = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        after_fd = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise DeploymentError("artifact_changed_during_hash", path.name) from exc
    if (
        _stat_identity(before_path) != _stat_identity(before_fd)
        or _stat_identity(before_fd) != _stat_identity(after_fd)
        or _stat_identity(after_fd) != _stat_identity(after_path)
        or count != before_fd.st_size
    ):
        raise DeploymentError("artifact_changed_during_hash", path.name)
    return "sha256:" + digest.hexdigest()


def _read_regular_bytes_no_follow(path: Path, reason: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError(reason) from exc
    chunks: list[bytes] = []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DeploymentError(reason)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _stable_regular_file(path: Path, reason: str) -> tuple[bytes, os.stat_result]:
    """Read one non-symlink regular file and reject concurrent replacement."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before_path = path.lstat()
        fd = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError(reason) from exc
    chunks: list[bytes] = []
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or stat.S_ISLNK(before_path.st_mode):
            raise DeploymentError(reason)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise DeploymentError(reason) from exc
    if _stat_identity(before_path) != _stat_identity(before_fd) or _stat_identity(before_fd) != _stat_identity(after_fd):
        raise DeploymentError(reason)
    if _stat_identity(after_fd) != _stat_identity(after_path):
        raise DeploymentError(reason)
    data = b"".join(chunks)
    if len(data) != before_fd.st_size:
        raise DeploymentError(reason)
    return data, after_fd


def mode_text(mode: int) -> str:
    return f"0{stat.S_IMODE(mode):03o}"


def parse_mode(value: str) -> int:
    if not MODE.fullmatch(value):
        raise DeploymentError("manifest_mode_invalid")
    return int(value, 8)


def _safe_absolute_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    return all(part not in {".", ".."} for part in Path(value).parts)


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or "\x00" in value:
        return False
    return all(part not in {"", ".", ".."} for part in Path(value).parts)


def _require_exact_fields(value: Any, expected: set[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise DeploymentError(reason)
    return value


def _require_supported_identity(
    deployment_id: Any,
    principal_id: Any | None = None,
) -> None:
    """Reject every deployment namespace except the reviewed v0.1 surface."""

    if deployment_id != SUPPORTED_DEPLOYMENT_ID:
        raise DeploymentError("deployment_identity_unsupported")
    if principal_id is not None and principal_id != SUPPORTED_PRINCIPAL_ID:
        raise DeploymentError("deployment_identity_unsupported")


def _validate_catalog_observation(value: Any) -> dict[str, Any]:
    observation = _require_exact_fields(
        value,
        {"status", "device", "inode", "uid", "mode", "size", "sha256"},
        "state_root_catalog_observation_invalid",
    )
    if observation["status"] not in {"present", "absent"}:
        raise DeploymentError("state_root_catalog_observation_invalid")
    for field in ("device", "inode", "uid", "size"):
        if not isinstance(observation[field], int) or isinstance(observation[field], bool) or observation[field] < 0:
            raise DeploymentError("state_root_catalog_observation_invalid")
    parse_mode(observation["mode"])
    if not isinstance(observation["sha256"], str) or not SHA256.fullmatch(observation["sha256"]):
        raise DeploymentError("state_root_catalog_observation_invalid")
    if observation["status"] == "absent" and any(
        observation[field] != 0 for field in ("device", "inode", "size")
    ):
        raise DeploymentError("state_root_catalog_observation_invalid")
    return observation


def validate_manifest(value: Any) -> dict[str, Any]:
    """Validate the security-relevant deployment manifest without dependencies."""

    manifest = _require_exact_fields(value, EXPECTED_TOP_LEVEL, "manifest_fields_invalid")
    if manifest["deployment_version"] != "1":
        raise DeploymentError("deployment_version_invalid")
    _require_supported_identity(manifest["deployment_id"])
    if not isinstance(manifest["release_commit"], str) or not COMMIT_SHA.fullmatch(manifest["release_commit"]):
        raise DeploymentError("release_commit_invalid")
    if manifest["platform"] != "macos" or manifest["expected_admin_uid"] != 0:
        raise DeploymentError("admin_trust_anchor_invalid")

    topology = _require_exact_fields(
        manifest["topology"],
        {
            "frontend_policy_domain",
            "write_capable_worker_policy_domain",
            "shared_rootfs",
            "worker_isolation",
        },
        "topology_invalid",
    )
    for field in ("frontend_policy_domain", "write_capable_worker_policy_domain"):
        if not isinstance(topology[field], str) or not SAFE_ID.fullmatch(topology[field]):
            raise DeploymentError("topology_policy_domain_invalid")
    if topology["frontend_policy_domain"] == topology["write_capable_worker_policy_domain"]:
        raise DeploymentError("worker_policy_domain_not_separate")
    if topology["shared_rootfs"] is not False:
        raise DeploymentError("write_capable_worker_shared_rootfs_forbidden")
    if topology["worker_isolation"] != "separate_host_vm_or_container":
        raise DeploymentError("worker_isolation_invalid")

    bindings = _require_exact_fields(manifest["bindings"], EXPECTED_BINDINGS, "bindings_invalid")
    if bindings["frontdoor"] != "codex":
        raise DeploymentError("frontdoor_binding_invalid")
    _require_supported_identity(manifest["deployment_id"], bindings["principal_id"])
    if bindings["workspace_id"] != "Saber5656/Saihai":
        raise DeploymentError("workspace_binding_invalid")
    if bindings["checkout_binding"] != "launch_cwd_registered_worktree":
        raise DeploymentError("checkout_binding_invalid")
    if bindings["workspace_catalog"] != "git_worktree_list":
        raise DeploymentError("workspace_catalog_invalid")
    for field in (
        "managed_primary",
        "assurance_root",
        "state_root_catalog",
        "deployment_manifest_path",
        "runtime_config_path",
        "instructions_path",
        "runtime_user_home",
        "codex_home",
        "state_root",
    ):
        if not _safe_absolute_path(bindings[field]):
            raise DeploymentError("binding_path_invalid", field)
    if (
        not isinstance(bindings["developer_instructions_sha256"], str)
        or not SHA256.fullmatch(bindings["developer_instructions_sha256"])
    ):
        raise DeploymentError("developer_instructions_digest_invalid")
    if (
        not isinstance(bindings["runtime_user_uid"], int)
        or isinstance(bindings["runtime_user_uid"], bool)
        or bindings["runtime_user_uid"] < 1
    ):
        raise DeploymentError("runtime_user_uid_invalid")
    if not isinstance(bindings["runtime_user_name"], str) or not USER_NAME.fullmatch(
        bindings["runtime_user_name"]
    ):
        raise DeploymentError("runtime_user_name_invalid")
    if bindings["state_root_source"] not in {"catalog", "default"}:
        raise DeploymentError("state_root_source_invalid")
    _validate_catalog_observation(bindings["state_root_catalog_observation"])
    try:
        Path(bindings["codex_home"]).relative_to(Path(bindings["runtime_user_home"]))
    except ValueError as exc:
        raise DeploymentError("codex_home_outside_runtime_user_home") from exc
    if Path(bindings["codex_home"]) != Path(bindings["runtime_user_home"]) / ".codex-saihai-main-agent":
        raise DeploymentError("codex_home_binding_invalid")
    if Path(bindings["state_root_catalog"]) != Path(bindings["managed_primary"]) / "directory-path.env":
        raise DeploymentError("state_root_catalog_binding_invalid")
    if bindings["assurance_root"] != str(ASSURANCE_ROOT):
        raise DeploymentError("assurance_root_binding_invalid")

    artifacts = _require_exact_fields(manifest["artifacts"], EXPECTED_ARTIFACTS, "artifacts_invalid")
    artifact_paths: set[str] = set()
    for name, artifact in artifacts.items():
        required = {"kind", "path", "sha256", "mode"}
        if name == "runtime_bundle":
            required |= {"digest_algorithm", "entrypoint", "verifier", "deployment_module"}
        if name == "native_codex":
            required |= {
                "version",
                "package",
                "package_version",
                "package_integrity",
                "platform",
                "architecture",
            }
        artifact = _require_exact_fields(artifact, required, "artifact_fields_invalid")
        expected_kind = "tree" if name == "runtime_bundle" else "file"
        if artifact["kind"] != expected_kind:
            raise DeploymentError("artifact_kind_invalid", name)
        if not _safe_absolute_path(artifact["path"]):
            raise DeploymentError("artifact_path_invalid", name)
        if artifact["path"] in artifact_paths:
            raise DeploymentError("artifact_path_duplicate", name)
        artifact_paths.add(artifact["path"])
        if not isinstance(artifact["sha256"], str) or not SHA256.fullmatch(artifact["sha256"]):
            raise DeploymentError("artifact_digest_invalid", name)
        parse_mode(artifact["mode"])
        if name == "runtime_bundle":
            if artifact["digest_algorithm"] != "saihai-tree-sha256-v1" or artifact["mode"] != "0755":
                raise DeploymentError("runtime_bundle_contract_invalid")
            if not all(
                _safe_relative_path(artifact[field])
                for field in ("entrypoint", "verifier", "deployment_module")
            ):
                raise DeploymentError("runtime_bundle_relative_path_invalid")
        if name == "native_codex":
            expected_provenance = {
                "version": NATIVE_CODEX_VERSION,
                "package": NATIVE_CODEX_PACKAGE,
                "package_version": NATIVE_CODEX_PACKAGE_VERSION,
                "package_integrity": NATIVE_CODEX_PACKAGE_INTEGRITY,
                "platform": NATIVE_CODEX_PLATFORM,
                "architecture": NATIVE_CODEX_ARCHITECTURE,
            }
            if any(artifact[field] != expected for field, expected in expected_provenance.items()):
                raise DeploymentError("native_codex_provenance_invalid")
            if artifact["sha256"] != NATIVE_CODEX_EXPECTED_SHA256:
                raise DeploymentError("native_codex_distribution_digest_invalid")

    if artifacts["bridge_wrapper"]["path"] != str(BRIDGE_WRAPPER_PATH):
        raise DeploymentError("bridge_wrapper_path_invalid")
    if artifacts["codex_launcher"]["path"] != str(CODEX_LAUNCHER_PATH):
        raise DeploymentError("codex_launcher_path_invalid")
    if artifacts["codex_launcher"]["mode"] != "0555":
        raise DeploymentError("codex_launcher_mode_invalid")
    if artifacts["requirements"]["path"] != str(REQUIREMENTS_PATH):
        raise DeploymentError("requirements_path_invalid")
    if artifacts["runtime_config"]["path"] != bindings["runtime_config_path"]:
        raise DeploymentError("runtime_config_binding_mismatch")
    if artifacts["instructions"]["path"] != bindings["instructions_path"]:
        raise DeploymentError("instructions_binding_mismatch")
    if artifacts["instructions"]["sha256"] != bindings["developer_instructions_sha256"]:
        raise DeploymentError("developer_instructions_digest_mismatch")
    if bindings["deployment_manifest_path"] != str(MANIFEST_PATH):
        raise DeploymentError("deployment_manifest_path_invalid")
    expected_profile = Path(bindings["codex_home"]) / f"{CODEX_PROFILE_NAME}.config.toml"
    if artifacts["codex_profile"]["path"] != str(expected_profile):
        raise DeploymentError("codex_profile_path_invalid")
    if artifacts["codex_profile"]["mode"] != "0600":
        raise DeploymentError("codex_profile_mode_invalid")
    bundle_path = Path(artifacts["runtime_bundle"]["path"])
    try:
        Path(bindings["instructions_path"]).relative_to(bundle_path)
    except ValueError as exc:
        raise DeploymentError("instructions_outside_runtime_bundle") from exc
    try:
        Path(artifacts["native_codex"]["path"]).relative_to(bundle_path)
    except ValueError as exc:
        raise DeploymentError("native_codex_outside_runtime_bundle") from exc
    if Path(artifacts["native_codex"]["path"]) != bundle_path / "bin" / "codex":
        raise DeploymentError("native_codex_path_invalid")
    if artifacts["native_codex"]["mode"] != "0555":
        raise DeploymentError("native_codex_mode_invalid")
    expected_supervisor = bundle_path / SUPERVISOR_RELATIVE_PATH
    if Path(artifacts["supervisor"]["path"]) != expected_supervisor:
        raise DeploymentError("supervisor_path_invalid")
    if artifacts["supervisor"]["mode"] != "0644":
        raise DeploymentError("supervisor_mode_invalid")
    expected_observer = bundle_path / OBSERVER_RELATIVE_PATH
    if Path(artifacts["observer"]["path"]) != expected_observer:
        raise DeploymentError("observer_path_invalid")
    if artifacts["observer"]["mode"] != "0555":
        raise DeploymentError("observer_mode_invalid")

    tools = _require_exact_fields(
        manifest["tool_contract"], {"server_name", "enabled_tools", "forbidden_tools"}, "tool_contract_invalid"
    )
    if tools["server_name"] != "saihai_bridge" or tools["enabled_tools"] != ENABLED_TOOLS:
        raise DeploymentError("bridge_tool_allowlist_invalid")
    forbidden = tools["forbidden_tools"]
    if (
        not isinstance(forbidden, list)
        or len(forbidden) != len(set(forbidden))
        or not all(isinstance(item, str) and SAFE_ID.fullmatch(item) for item in forbidden)
        or not REQUIRED_FORBIDDEN_TOOLS.issubset(set(forbidden))
    ):
        raise DeploymentError("bridge_tool_denylist_invalid")
    if set(ENABLED_TOOLS) & set(forbidden):
        raise DeploymentError("bridge_tool_contract_overlap")
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            _read_regular_bytes_no_follow(path, "manifest_unloadable").decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("manifest_unloadable") from exc
    return validate_manifest(payload)


def tree_digest(root: Path) -> str:
    """Hash a non-symlink tree using relative paths, modes, sizes, and bytes."""

    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise DeploymentError("runtime_bundle_unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise DeploymentError("runtime_bundle_not_directory")
    digest = hashlib.sha256(b"saihai-tree-sha256-v1\0")
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        observed = path.lstat()
        mode = mode_text(observed.st_mode)
        if stat.S_ISDIR(observed.st_mode):
            digest.update(f"D\0{relative}\0{mode}\n".encode("utf-8"))
        elif stat.S_ISREG(observed.st_mode):
            file_digest = sha256_file(path).removeprefix("sha256:")
            digest.update(
                f"F\0{relative}\0{mode}\0{observed.st_size}\0{file_digest}\n".encode("utf-8")
            )
        else:
            raise DeploymentError("runtime_bundle_special_file", relative)
    return "sha256:" + digest.hexdigest()


def tree_inventory(root: Path) -> list[dict[str, Any]]:
    """Return the complete, mode-bound regular-file/directory tree inventory.

    Ownership is deliberately omitted because the reviewed user-owned stage is
    copied as data and then re-owned by root.  The post-freeze verifier checks
    the root ownership independently before any frozen Saihai code may run.
    """

    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise DeploymentError("payload_tree_unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise DeploymentError("payload_tree_not_directory")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if not _safe_relative_path(relative) or any(character in relative for character in "\r\n"):
            raise DeploymentError("payload_entry_path_invalid", relative)
        observed = path.lstat()
        if stat.S_ISDIR(observed.st_mode):
            entries.append(
                {"kind": "directory", "mode": mode_text(observed.st_mode), "path": relative}
            )
        elif stat.S_ISREG(observed.st_mode):
            entries.append(
                {
                    "kind": "file",
                    "mode": mode_text(observed.st_mode),
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size": observed.st_size,
                }
            )
        else:
            raise DeploymentError("payload_tree_special_file", relative)
    return entries


def _inventory_digest(entries: list[dict[str, Any]]) -> str:
    return sha256_bytes(canonical_json_bytes(entries))


def _payload_checksums(entries: list[dict[str, Any]], *, frozen_payload: Path) -> bytes:
    lines: list[str] = []
    for entry in entries:
        if entry["kind"] != "file":
            continue
        absolute = frozen_payload / entry["path"]
        if any(character in str(absolute) for character in "\r\n"):
            raise DeploymentError("frozen_path_invalid")
        lines.append(f"{entry['sha256'].removeprefix('sha256:')}  {absolute}\n")
    return "".join(lines).encode("utf-8")


_FREEZE_REQUEST_FIELDS = {
    "freeze_version",
    "transaction_id",
    "deployment_id",
    "release_commit",
    "frozen_root",
    "payload_relative_path",
    "payload_tree_sha256",
    "payload_entries_sha256",
    "payload_entries",
    "payload_regular_file_count",
    "payload_directory_count",
    "checksums_relative_path",
    "checksums_sha256",
    "bootstrap_relative_path",
    "bootstrap_sha256",
    "activation_importer_relative_path",
    "activation_importer_sha256",
}


def validate_freeze_request(value: Any, *, frozen_root: Path | None = None) -> dict[str, Any]:
    request = _require_exact_fields(value, _FREEZE_REQUEST_FIELDS, "freeze_request_fields_invalid")
    if request["freeze_version"] != FREEZE_VERSION:
        raise DeploymentError("freeze_version_invalid")
    if not isinstance(request["transaction_id"], str) or not SAFE_ID.fullmatch(request["transaction_id"]):
        raise DeploymentError("freeze_transaction_id_invalid")
    _require_supported_identity(request["deployment_id"])
    if not isinstance(request["release_commit"], str) or not COMMIT_SHA.fullmatch(request["release_commit"]):
        raise DeploymentError("freeze_release_commit_invalid")
    if request["transaction_id"] != f"{request['deployment_id']}-{request['release_commit'][:12]}":
        raise DeploymentError("freeze_transaction_binding_mismatch")
    if not _safe_absolute_path(request["frozen_root"]):
        raise DeploymentError("freeze_root_invalid")
    if frozen_root is not None and Path(request["frozen_root"]) != frozen_root:
        raise DeploymentError("freeze_root_mismatch")
    if request["payload_relative_path"] != PAYLOAD_DIR_NAME:
        raise DeploymentError("freeze_payload_path_invalid")
    if request["checksums_relative_path"] != FREEZE_CHECKSUMS_NAME:
        raise DeploymentError("freeze_checksums_path_invalid")
    if request["bootstrap_relative_path"] != FREEZE_BOOTSTRAP_NAME:
        raise DeploymentError("freeze_bootstrap_path_invalid")
    for field in (
        "payload_tree_sha256",
        "payload_entries_sha256",
        "checksums_sha256",
        "bootstrap_sha256",
        "activation_importer_sha256",
    ):
        if not isinstance(request[field], str) or not SHA256.fullmatch(request[field]):
            raise DeploymentError("freeze_digest_invalid", field)
    entries = request["payload_entries"]
    if not isinstance(entries, list) or not entries:
        raise DeploymentError("freeze_payload_entries_invalid")
    seen: set[str] = set()
    file_count = 0
    directory_count = 0
    importer_digest = ""
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in {"file", "directory"}:
            raise DeploymentError("freeze_payload_entry_invalid")
        expected = {"kind", "mode", "path"}
        if entry["kind"] == "file":
            expected |= {"sha256", "size"}
        if set(entry) != expected or not _safe_relative_path(entry["path"]):
            raise DeploymentError("freeze_payload_entry_invalid")
        if any(character in entry["path"] for character in "\r\n") or entry["path"] in seen:
            raise DeploymentError("freeze_payload_entry_invalid")
        seen.add(entry["path"])
        parse_mode(entry["mode"])
        if parse_mode(entry["mode"]) & 0o022:
            raise DeploymentError("freeze_payload_entry_writable", entry["path"])
        if entry["kind"] == "file":
            if (
                not isinstance(entry["sha256"], str)
                or not SHA256.fullmatch(entry["sha256"])
                or not isinstance(entry["size"], int)
                or isinstance(entry["size"], bool)
                or entry["size"] < 0
            ):
                raise DeploymentError("freeze_payload_entry_invalid")
            file_count += 1
            if entry["path"] == request["activation_importer_relative_path"]:
                importer_digest = entry["sha256"]
        else:
            directory_count += 1
    if entries != sorted(entries, key=lambda entry: entry["path"]):
        raise DeploymentError("freeze_payload_entries_not_sorted")
    if _inventory_digest(entries) != request["payload_entries_sha256"]:
        raise DeploymentError("freeze_payload_entries_digest_mismatch")
    if request["payload_regular_file_count"] != file_count or request["payload_directory_count"] != directory_count:
        raise DeploymentError("freeze_payload_entry_count_mismatch")
    if (
        not _safe_relative_path(request["activation_importer_relative_path"])
        or importer_digest != request["activation_importer_sha256"]
    ):
        raise DeploymentError("freeze_activation_importer_invalid")
    return request


def _load_json_regular(path: Path, reason: str) -> Any:
    try:
        return json.loads(_read_regular_bytes_no_follow(path, reason).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(reason) from exc


def verify_frozen_stage(
    frozen_root: Path,
    approved_freeze_request_sha256: str,
    *,
    expected_uid: int = 0,
    require_seal: bool = True,
    enforce_production_root: bool = True,
    uid_reader: Callable[[Path], int] | None = None,
    verify_runtime_home_ancestors: bool = True,
) -> dict[str, Any]:
    """Verify a root-frozen stage before activation or recovery.

    The production CLI fixes ``expected_uid`` to root and enforces the
    quarantine prefix.  The injectable values support non-root hermetic tests
    and are not exposed by either command-line wrapper.
    """

    if not SHA256.fullmatch(approved_freeze_request_sha256):
        raise DeploymentError("approved_freeze_request_digest_invalid")
    try:
        frozen_root = frozen_root.resolve(strict=True)
    except OSError as exc:
        raise DeploymentError("frozen_root_unavailable") from exc
    if enforce_production_root:
        try:
            frozen_root.relative_to(QUARANTINE_ROOT)
        except ValueError as exc:
            raise DeploymentError("frozen_root_outside_quarantine") from exc
        if frozen_root.parent != QUARANTINE_ROOT:
            raise DeploymentError("frozen_root_not_transaction_directory")
    root_stat = frozen_root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
        or root_stat.st_uid != expected_uid
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise DeploymentError("frozen_root_trust_invalid")
    if enforce_production_root:
        _verify_safe_ancestors(frozen_root, lambda path: path.lstat().st_uid)
    allowed = {
        PAYLOAD_DIR_NAME,
        FREEZE_REQUEST_NAME,
        FREEZE_CHECKSUMS_NAME,
        FREEZE_BOOTSTRAP_NAME,
    } | _FROZEN_MUTABLE_METADATA
    actual_names = {entry.name for entry in os.scandir(frozen_root)}
    required = {PAYLOAD_DIR_NAME, FREEZE_REQUEST_NAME, FREEZE_CHECKSUMS_NAME, FREEZE_BOOTSTRAP_NAME}
    if not required.issubset(actual_names) or not actual_names.issubset(allowed):
        raise DeploymentError("frozen_root_entries_invalid")
    request_path = frozen_root / FREEZE_REQUEST_NAME
    if sha256_file(request_path) != approved_freeze_request_sha256:
        raise DeploymentError("freeze_request_digest_mismatch")
    request = validate_freeze_request(
        _load_json_regular(request_path, "freeze_request_unloadable"), frozen_root=frozen_root
    )
    if frozen_root.name != request["transaction_id"]:
        raise DeploymentError("freeze_transaction_directory_mismatch")
    metadata_modes = {
        request_path: 0o600,
        frozen_root / FREEZE_CHECKSUMS_NAME: 0o600,
        frozen_root / FREEZE_BOOTSTRAP_NAME: 0o400,
    }
    for metadata_path, expected_mode in metadata_modes.items():
        observed = metadata_path.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_uid != expected_uid
            or stat.S_IMODE(observed.st_mode) != expected_mode
        ):
            raise DeploymentError("frozen_metadata_trust_invalid", metadata_path.name)
    checksums = frozen_root / FREEZE_CHECKSUMS_NAME
    if sha256_file(checksums) != request["checksums_sha256"]:
        raise DeploymentError("freeze_checksums_digest_mismatch")
    if sha256_file(frozen_root / FREEZE_BOOTSTRAP_NAME) != request["bootstrap_sha256"]:
        raise DeploymentError("freeze_bootstrap_digest_mismatch")
    payload = frozen_root / PAYLOAD_DIR_NAME
    actual_entries = tree_inventory(payload)
    if actual_entries != request["payload_entries"]:
        raise DeploymentError("frozen_payload_entries_mismatch")
    if tree_digest(payload) != request["payload_tree_sha256"]:
        raise DeploymentError("frozen_payload_tree_digest_mismatch")
    for path in [payload, *sorted(payload.rglob("*"))]:
        observed = path.lstat()
        if observed.st_uid != expected_uid:
            raise DeploymentError("frozen_payload_owner_invalid", path.name)
        if stat.S_IMODE(observed.st_mode) & 0o022:
            raise DeploymentError("frozen_payload_mode_invalid", path.name)
    if require_seal:
        seal_path = frozen_root / FREEZE_SEAL_NAME
        seal = _load_json_regular(seal_path, "freeze_seal_unloadable")
        expected_seal = {
            "seal_version": FREEZE_VERSION,
            "transaction_id": request["transaction_id"],
            "freeze_request_sha256": approved_freeze_request_sha256,
            "payload_tree_sha256": request["payload_tree_sha256"],
            "payload_entries_sha256": request["payload_entries_sha256"],
            "verified_by": "trusted-root-copied-bootstrap-v1",
        }
        if seal != expected_seal:
            raise DeploymentError("freeze_seal_invalid")
        seal_stat = seal_path.lstat()
        if (
            not stat.S_ISREG(seal_stat.st_mode)
            or stat.S_ISLNK(seal_stat.st_mode)
            or seal_stat.st_uid != expected_uid
            or stat.S_IMODE(seal_stat.st_mode) != 0o400
        ):
            raise DeploymentError("freeze_seal_trust_invalid")
    staged_manifest_path = payload / "manifest" / MANIFEST_PATH.name
    if staged_manifest_path.exists() or staged_manifest_path.is_symlink():
        staged_manifest = load_manifest(staged_manifest_path)
        runtime_uid = staged_manifest["bindings"]["runtime_user_uid"]
        require_trusted_runtime_user_home(
            Path(staged_manifest["bindings"]["runtime_user_home"]),
            runtime_uid,
            uid_reader=uid_reader or (lambda path: path.lstat().st_uid),
            verify_ancestors=verify_runtime_home_ancestors,
        )
    return request


def _ancestors(path: Path) -> Iterable[Path]:
    current = path.parent
    entries: list[Path] = []
    while current != current.parent:
        entries.append(current)
        current = current.parent
    return reversed(entries)


def _verify_safe_ancestors(path: Path, uid_reader: Callable[[Path], int]) -> None:
    for parent in _ancestors(path):
        try:
            observed = parent.lstat()
        except OSError as exc:
            raise DeploymentError("artifact_parent_unavailable", parent.name) from exc
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("artifact_parent_invalid", parent.name)
        if uid_reader(parent) != 0 or stat.S_IMODE(observed.st_mode) & 0o022:
            raise DeploymentError("artifact_parent_not_admin_safe", parent.name)


def require_trusted_runtime_user_home(
    user_home: Path,
    runtime_uid: int,
    *,
    uid_reader: Callable[[Path], int],
    verify_ancestors: bool,
) -> Path:
    """Return the canonical runtime home or fail closed.

    The runtime home is user-owned state, not an administrator-owned
    authority artifact.  It must nevertheless be a stable, non-symlink
    directory that only its owner (or root through normal administration) can
    modify.  All deployment and launch gates use this single validator so the
    trust decision cannot drift between preparation, activation, and runtime
    verification.
    """

    path = Path(user_home)
    try:
        if not path.is_absolute():
            raise DeploymentError("runtime_user_home_trust_invalid")
        resolved = path.resolve(strict=True)
        observed = path.lstat()
        owner = uid_reader(path)
    except DeploymentError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeploymentError("runtime_user_home_trust_invalid") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or owner != runtime_uid
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise DeploymentError("runtime_user_home_trust_invalid")
    if verify_ancestors:
        try:
            _verify_safe_ancestors(path, uid_reader)
        except DeploymentError as exc:
            raise DeploymentError("runtime_user_home_trust_invalid", exc.reason) from exc
    return path


def _verify_file_artifact(
    name: str,
    artifact: dict[str, Any],
    uid_reader: Callable[[Path], int],
    *,
    verify_ancestors: bool,
) -> None:
    path = Path(artifact["path"])
    if verify_ancestors:
        _verify_safe_ancestors(path, uid_reader)
    try:
        observed = path.lstat()
    except OSError as exc:
        raise DeploymentError("artifact_unavailable", name) from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise DeploymentError("artifact_not_regular", name)
    if uid_reader(path) != 0:
        raise DeploymentError("artifact_owner_invalid", name)
    if stat.S_IMODE(observed.st_mode) != parse_mode(artifact["mode"]):
        raise DeploymentError("artifact_mode_invalid", name)
    if sha256_file(path) != artifact["sha256"]:
        raise DeploymentError("artifact_digest_mismatch", name)


def _verify_bundle_artifact(
    artifact: dict[str, Any],
    uid_reader: Callable[[Path], int],
    *,
    verify_ancestors: bool,
) -> None:
    root = Path(artifact["path"])
    if verify_ancestors:
        _verify_safe_ancestors(root, uid_reader)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise DeploymentError("runtime_bundle_unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise DeploymentError("runtime_bundle_not_directory")
    if uid_reader(root) != 0 or stat.S_IMODE(root_stat.st_mode) != parse_mode(artifact["mode"]):
        raise DeploymentError("runtime_bundle_root_trust_invalid")
    for path in sorted(root.rglob("*")):
        observed = path.lstat()
        if not (stat.S_ISDIR(observed.st_mode) or stat.S_ISREG(observed.st_mode)):
            raise DeploymentError("runtime_bundle_special_file", path.name)
        if uid_reader(path) != 0 or stat.S_IMODE(observed.st_mode) & 0o022:
            raise DeploymentError("runtime_bundle_entry_trust_invalid", path.name)
    if tree_digest(root) != artifact["sha256"]:
        raise DeploymentError("runtime_bundle_digest_mismatch")
    for field in ("entrypoint", "verifier", "deployment_module"):
        target = root / artifact[field]
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise DeploymentError("runtime_bundle_path_escape", field) from exc
        if not target.is_file() or target.is_symlink():
            raise DeploymentError("runtime_bundle_required_file_invalid", field)


def _verify_runtime_config(manifest: dict[str, Any]) -> None:
    path = Path(manifest["artifacts"]["runtime_config"]["path"])
    try:
        value = json.loads(
            _read_regular_bytes_no_follow(path, "runtime_config_unloadable").decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("runtime_config_unloadable") from exc
    expected = runtime_config_payload(manifest)
    if value != expected:
        raise DeploymentError("runtime_config_binding_mismatch")


def _verify_catalog_observation(manifest: dict[str, Any]) -> None:
    bindings = manifest["bindings"]
    path = Path(bindings["state_root_catalog"])
    expected = bindings["state_root_catalog_observation"]
    if expected["status"] == "absent":
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DeploymentError("state_root_catalog_drift") from exc
        raise DeploymentError("state_root_catalog_drift")
    data, observed = _stable_regular_file(path, "state_root_catalog_drift")
    actual = {
        "status": "present",
        "device": observed.st_dev,
        "inode": observed.st_ino,
        "uid": observed.st_uid,
        "mode": mode_text(observed.st_mode),
        "size": observed.st_size,
        "sha256": sha256_bytes(data),
    }
    if actual != expected:
        raise DeploymentError("state_root_catalog_drift")


def _verify_native_codex_version(manifest: dict[str, Any]) -> None:
    artifact = manifest["artifacts"]["native_codex"]
    path = Path(artifact["path"])
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError("native_codex_version_unavailable") from exc
    if completed.returncode != 0 or completed.stderr or completed.stdout.strip() != artifact["version"]:
        raise DeploymentError("native_codex_version_mismatch")


def _verify_codex_home(
    manifest: dict[str, Any],
    uid_reader: Callable[[Path], int],
    *,
    verify_ancestors: bool,
) -> None:
    """Verify the manifest-bound user state boundary without trusting HOME.

    CODEX_HOME is user-writable because stock Codex stores human-created auth
    and session state there. We therefore verify its exact path, ownership,
    mode, non-symlink parents, absent base config, and exact profile digest on
    every launch. A malicious same-uid human race remains explicitly outside
    the process-scoped action claim.
    """

    bindings = manifest["bindings"]
    runtime_uid = bindings["runtime_user_uid"]
    user_home = Path(bindings["runtime_user_home"])
    codex_home = Path(bindings["codex_home"])
    require_trusted_runtime_user_home(
        user_home,
        runtime_uid,
        uid_reader=uid_reader,
        verify_ancestors=verify_ancestors,
    )
    try:
        codex_home_stat = codex_home.lstat()
    except OSError as exc:
        raise DeploymentError("codex_home_unavailable") from exc
    if (
        not stat.S_ISDIR(codex_home_stat.st_mode)
        or stat.S_ISLNK(codex_home_stat.st_mode)
        or uid_reader(codex_home) != runtime_uid
        or stat.S_IMODE(codex_home_stat.st_mode) != 0o700
    ):
        raise DeploymentError("codex_home_trust_invalid")
    base_config = codex_home / "config.toml"
    if base_config.exists() or base_config.is_symlink():
        raise DeploymentError("codex_home_base_config_forbidden")

    profile = manifest["artifacts"]["codex_profile"]
    profile_path = Path(profile["path"])
    try:
        observed_profile = profile_path.lstat()
    except OSError as exc:
        raise DeploymentError("artifact_unavailable", "codex_profile") from exc
    if (
        not stat.S_ISREG(observed_profile.st_mode)
        or stat.S_ISLNK(observed_profile.st_mode)
        or uid_reader(profile_path) != runtime_uid
        or stat.S_IMODE(observed_profile.st_mode) != 0o600
        or sha256_file(profile_path) != profile["sha256"]
    ):
        raise DeploymentError("codex_profile_trust_invalid")


def verify_deployment(
    manifest_path: Path,
    *,
    uid_reader: Callable[[Path], int] | None = None,
    verify_ancestors: bool = True,
) -> dict[str, Any]:
    """Verify installed root-owned artifacts.

    ``uid_reader`` and ``verify_ancestors=False`` exist only for hermetic unit
    fixtures. The production CLI exposes neither override.
    """

    reader = uid_reader or (lambda path: path.lstat().st_uid)
    if verify_ancestors:
        _verify_safe_ancestors(manifest_path, reader)
    try:
        manifest_stat = manifest_path.lstat()
    except OSError as exc:
        raise DeploymentError("manifest_unavailable") from exc
    if not stat.S_ISREG(manifest_stat.st_mode) or stat.S_ISLNK(manifest_stat.st_mode):
        raise DeploymentError("manifest_not_regular")
    if reader(manifest_path) != 0 or stat.S_IMODE(manifest_stat.st_mode) != 0o644:
        raise DeploymentError("manifest_trust_invalid")
    manifest = load_manifest(manifest_path)
    artifacts = manifest["artifacts"]
    _verify_bundle_artifact(
        artifacts["runtime_bundle"], reader, verify_ancestors=verify_ancestors
    )
    for name in sorted(EXPECTED_ARTIFACTS - {"runtime_bundle", "codex_profile"}):
        _verify_file_artifact(
            name, artifacts[name], reader, verify_ancestors=verify_ancestors
        )
    _verify_codex_home(manifest, reader, verify_ancestors=verify_ancestors)
    _verify_runtime_config(manifest)
    _verify_catalog_observation(manifest)
    _verify_native_codex_version(manifest)
    return {
        "decision": "verified",
        "deployment_id": manifest["deployment_id"],
        "release_commit": manifest["release_commit"],
        "topology": "separate_policy_domains",
        "tool_contract": "bridge_only",
        "surface": "fixed_stock_codex_cli_launcher",
    }


def runtime_config_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    bundle = manifest["artifacts"]["runtime_bundle"]
    return {
        "runtime_config_version": "1",
        "deployment_id": manifest["deployment_id"],
        "release_commit": manifest["release_commit"],
        "bindings": manifest["bindings"],
        "executable": {
            "bridge_wrapper": manifest["artifacts"]["bridge_wrapper"]["path"],
            "codex_launcher": manifest["artifacts"]["codex_launcher"]["path"],
            "native_codex": manifest["artifacts"]["native_codex"]["path"],
            "native_codex_version": manifest["artifacts"]["native_codex"]["version"],
            "native_codex_argv": native_codex_argv(
                manifest["artifacts"]["native_codex"]["path"]
            ),
            "codex_profile": manifest["artifacts"]["codex_profile"]["path"],
            "python": manifest["artifacts"]["python_executable"]["path"],
            "entrypoint": str(Path(bundle["path"]) / bundle["entrypoint"]),
            "verifier": str(Path(bundle["path"]) / bundle["verifier"]),
            "deployment_module": str(Path(bundle["path"]) / bundle["deployment_module"]),
            "supervisor": manifest["artifacts"]["supervisor"]["path"],
            "supervisor_sha256": manifest["artifacts"]["supervisor"]["sha256"],
            "observer": manifest["artifacts"]["observer"]["path"],
            "observer_sha256": manifest["artifacts"]["observer"]["sha256"],
        },
        "tool_contract": manifest["tool_contract"],
    }


def _shell_assignment(value: str) -> str:
    return shlex.quote(value)


def render_wrapper(
    template: str,
    *,
    python_executable: Path,
    python_sha256: str,
    verifier: Path,
    verifier_sha256: str,
    deployment_module: Path,
    deployment_module_sha256: str,
    manifest_path: Path,
    entrypoint: Path,
    frontdoor: str,
    principal_id: str,
    workspace_id: str,
    managed_primary: Path,
    state_root: Path,
    state_root_catalog: Path,
    state_root_catalog_status: str,
    state_root_catalog_sha256: str,
) -> str:
    replacements = {
        "@@PYTHON_EXECUTABLE@@": str(python_executable),
        "@@PYTHON_SHA256@@": python_sha256,
        "@@VERIFIER@@": str(verifier),
        "@@VERIFIER_SHA256@@": verifier_sha256,
        "@@DEPLOYMENT_MODULE@@": str(deployment_module),
        "@@DEPLOYMENT_MODULE_SHA256@@": deployment_module_sha256,
        "@@MANIFEST@@": str(manifest_path),
        "@@ENTRYPOINT@@": str(entrypoint),
        "@@FRONTDOOR@@": frontdoor,
        "@@PRINCIPAL_ID@@": principal_id,
        "@@WORKSPACE_ID@@": workspace_id,
        "@@MANAGED_PRIMARY@@": str(managed_primary),
        "@@STATE_ROOT@@": str(state_root),
        "@@STATE_ROOT_CATALOG@@": str(state_root_catalog),
        "@@STATE_ROOT_CATALOG_STATUS@@": state_root_catalog_status,
        "@@STATE_ROOT_CATALOG_SHA256@@": state_root_catalog_sha256,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, _shell_assignment(value))
    if "@@" in rendered:
        raise DeploymentError("wrapper_template_placeholder_unresolved")
    return rendered


def render_launcher(
    template: str,
    *,
    native_codex: Path,
    native_codex_sha256: str,
    native_codex_version: str,
    python_executable: Path,
    verifier: Path,
    verifier_sha256: str,
    deployment_module: Path,
    deployment_module_sha256: str,
    supervisor: Path,
    supervisor_sha256: str,
    manifest_path: Path,
    runtime_user_uid: int,
    runtime_user_name: str,
    runtime_user_home: Path,
    codex_home: Path,
    profile_path: Path,
    profile_sha256: str,
) -> str:
    replacements = {
        "@@NATIVE_CODEX@@": str(native_codex),
        "@@NATIVE_CODEX_SHA256@@": native_codex_sha256,
        "@@NATIVE_CODEX_VERSION@@": native_codex_version,
        "@@PYTHON_EXECUTABLE@@": str(python_executable),
        "@@VERIFIER@@": str(verifier),
        "@@VERIFIER_SHA256@@": verifier_sha256,
        "@@DEPLOYMENT_MODULE@@": str(deployment_module),
        "@@DEPLOYMENT_MODULE_SHA256@@": deployment_module_sha256,
        "@@SUPERVISOR@@": str(supervisor),
        "@@SUPERVISOR_SHA256@@": supervisor_sha256,
        "@@MANIFEST@@": str(manifest_path),
        "@@RUNTIME_USER_UID@@": str(runtime_user_uid),
        "@@RUNTIME_USER_NAME@@": runtime_user_name,
        "@@RUNTIME_USER_HOME@@": str(runtime_user_home),
        "@@CODEX_HOME@@": str(codex_home),
        "@@PROFILE_PATH@@": str(profile_path),
        "@@PROFILE_SHA256@@": profile_sha256,
    }
    fixed_argv = native_codex_argv(native_codex)
    rendered_argv = " \\\n  ".join(_shell_assignment(item) for item in fixed_argv)
    rendered = template.replace("@@NATIVE_CODEX_ARGV@@", rendered_argv)
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, _shell_assignment(value))
    if "@@" in rendered:
        raise DeploymentError("launcher_template_placeholder_unresolved")
    return rendered


def _git(source_root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *args],
            capture_output=True,
            text=text,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError("git_preflight_unavailable") from exc
    if completed.returncode:
        raise DeploymentError("git_preflight_failed", args[0] if args else "git")
    return completed


def _release_commit(source_root: Path) -> str:
    commit = _git(source_root, "rev-parse", "HEAD").stdout.strip()
    if not COMMIT_SHA.fullmatch(commit):
        raise DeploymentError("release_commit_invalid")
    return commit


def _require_clean_release_checkout(source_root: Path) -> str:
    status = _git(source_root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status.strip():
        raise DeploymentError("release_checkout_not_clean")
    return _release_commit(source_root)


def _extract_git_archive(source_root: Path, commit: str, destination: Path) -> None:
    archive = _git(source_root, "archive", "--format=tar", commit, text=False).stdout
    destination.mkdir(parents=True, mode=0o755)
    destination.chmod(0o755)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        for member in handle.getmembers():
            if not _safe_relative_path(member.name):
                raise DeploymentError("release_archive_path_invalid")
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o755)
                target.chmod(0o755)
                continue
            if not member.isfile():
                raise DeploymentError("release_archive_special_file", member.name)
            if member.mode & 0o7000:
                raise DeploymentError("release_archive_mode_unsafe", member.name)
            # Git archives can reflect a group-writable umask even though Git
            # tracks only executable vs non-executable files. Normalize to the
            # immutable installed bundle modes before hashing.
            mode = 0o755 if member.mode & 0o111 else 0o644
            source = handle.extractfile(member)
            if source is None:
                raise DeploymentError("release_archive_member_unreadable", member.name)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(target, flags, mode)
            try:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(fd, chunk[offset:])
            finally:
                os.close(fd)
            target.chmod(mode)


def _write_new(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
    finally:
        os.close(fd)
    path.chmod(mode)


def _copy_new_regular(source: Path, destination: Path, mode: int) -> None:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    try:
        before_path = source.lstat()
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise DeploymentError("native_codex_unopenable") from exc
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    destination_fd = -1
    destination_created = False
    try:
        before_fd = os.fstat(source_fd)
        if not stat.S_ISREG(before_fd.st_mode) or stat.S_ISLNK(before_path.st_mode):
            raise DeploymentError("native_codex_not_regular")
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        destination_created = True
        copied = 0
        source_digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                source_digest.update(chunk)
                offset = 0
                while offset < len(chunk):
                    offset += os.write(destination_fd, chunk[offset:])
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
            destination_fd = -1
        after_fd = os.fstat(source_fd)
        try:
            after_path = source.lstat()
        except OSError as exc:
            raise DeploymentError("native_codex_changed_during_copy") from exc
        if (
            _stat_identity(before_path) != _stat_identity(before_fd)
            or _stat_identity(before_fd) != _stat_identity(after_fd)
            or _stat_identity(after_fd) != _stat_identity(after_path)
            or copied != before_fd.st_size
        ):
            raise DeploymentError("native_codex_changed_during_copy")
        destination.chmod(mode)
        # Hashing the copied destination performs another stable before/after
        # identity check and closes the staging-manifest race independently.
        if sha256_file(destination) != "sha256:" + source_digest.hexdigest():
            raise DeploymentError("native_codex_copy_digest_mismatch")
    except Exception:
        if destination_fd >= 0:
            os.close(destination_fd)
        if destination_created:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise
    finally:
        os.close(source_fd)


def _native_codex_version(path: Path) -> str:
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError("native_codex_version_unavailable") from exc
    version = completed.stdout.strip()
    if completed.returncode != 0 or completed.stderr or version != NATIVE_CODEX_VERSION:
        raise DeploymentError("native_codex_version_mismatch")
    return version


def _assert_trusted_native_codex_distribution(path: Path) -> str:
    if sys.platform != NATIVE_CODEX_PLATFORM or platform.machine() != NATIVE_CODEX_ARCHITECTURE:
        raise DeploymentError("native_codex_platform_unsupported")
    digest = sha256_file(path)
    if digest != NATIVE_CODEX_EXPECTED_SHA256:
        raise DeploymentError("native_codex_distribution_digest_mismatch")
    return digest


def _catalog_observation(path: Path, *, runtime_uid: int) -> tuple[dict[str, Any], bytes | None]:
    try:
        data, observed = _stable_regular_file(path, "state_root_catalog_unreadable")
    except DeploymentError as exc:
        if exc.reason != "state_root_catalog_unreadable":
            raise
        try:
            path.lstat()
        except FileNotFoundError:
            return (
                {
                    "status": "absent",
                    "device": 0,
                    "inode": 0,
                    "uid": runtime_uid,
                    "mode": "0000",
                    "size": 0,
                    "sha256": sha256_bytes(b""),
                },
                None,
            )
        except OSError:
            pass
        raise
    if observed.st_uid != runtime_uid or stat.S_IMODE(observed.st_mode) != 0o600:
        raise DeploymentError("state_root_catalog_trust_invalid")
    return (
        {
            "status": "present",
            "device": observed.st_dev,
            "inode": observed.st_ino,
            "uid": observed.st_uid,
            "mode": mode_text(observed.st_mode),
            "size": observed.st_size,
            "sha256": sha256_bytes(data),
        },
        data,
    )


def _resolved_state_root(
    *,
    catalog_path: Path,
    catalog_bytes: bytes | None,
    user_home: Path,
) -> tuple[Path, str]:
    configured = ""
    if catalog_bytes is not None:
        try:
            parsed = parse_directory_catalog(catalog_bytes.decode("utf-8"))
        except (UnicodeError, DirectoryPathError) as exc:
            raise DeploymentError("state_root_catalog_invalid") from exc
        configured = parsed.get("SAIHAI_ORCH_STATE_ROOT") or parsed.get("SAHAI_ORCH_STATE_ROOT") or ""
    if configured:
        if not SAFE_STATE_ROOT.fullmatch(configured) or any(
            part in {".", ".."} for part in configured.split("/")
        ):
            raise DeploymentError("state_root_catalog_invalid")
        root = Path(configured)
        source = "catalog"
    else:
        root = user_home / ".codex" / "state" / "itb" / "frontdoor-orchestrator"
        source = "default"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise DeploymentError("state_root_invalid")
    return root.resolve(strict=False), source


def _render_codex_text(template: str, *, user_home: Path, release_commit: str) -> str:
    return template.replace("/Users/YOU", str(user_home)).replace("RELEASE_ID", release_commit)


def _render_profile(
    template: str,
    *,
    user_home: Path,
    release_commit: str,
    developer_instructions: str,
) -> str:
    marker = "@@SAIHAI_DEVELOPER_INSTRUCTIONS@@"
    if template.count(marker) != 1 or "'''" in developer_instructions:
        raise DeploymentError("developer_instructions_template_invalid")
    rendered = _render_codex_text(
        template,
        user_home=user_home,
        release_commit=release_commit,
    )
    return rendered.replace(marker, developer_instructions)


def _artifact(path: Path, mode: str) -> dict[str, Any]:
    return {"kind": "file", "path": str(path), "sha256": "", "mode": mode}


def _admin_flow_commands(
    stage_root: Path,
    manifest: dict[str, Any],
    *,
    approved_freeze_request_sha256: str,
    transaction_id: str,
) -> dict[str, Any]:
    """Return data-only argv for two explicit administrator gates."""

    _require_supported_identity(
        manifest.get("deployment_id"),
        manifest.get("bindings", {}).get("principal_id", "")
        if isinstance(manifest.get("bindings"), dict)
        else "",
    )
    if not SAFE_ID.fullmatch(transaction_id):
        raise DeploymentError("freeze_transaction_id_invalid")
    expected_transaction = f"{manifest['deployment_id']}-{manifest['release_commit'][:12]}"
    if transaction_id != expected_transaction:
        raise DeploymentError("freeze_transaction_binding_mismatch")
    if not SHA256.fullmatch(approved_freeze_request_sha256):
        raise DeploymentError("approved_freeze_request_digest_invalid")
    if not stage_root.is_absolute() or any(character in str(stage_root) for character in "\r\n"):
        raise DeploymentError("stage_root_path_invalid")

    frozen_root = QUARANTINE_ROOT / transaction_id
    release = manifest["release_commit"]
    importer = frozen_root / PAYLOAD_DIR_NAME / "runtime" / release / (
        "organization/runtime/workflows/scripts/codex_main_agent_install.py"
    )
    request = frozen_root / FREEZE_REQUEST_NAME
    checksums = frozen_root / FREEZE_CHECKSUMS_NAME
    bootstrap = frozen_root / FREEZE_BOOTSTRAP_NAME
    freeze_commands = [
        ["/usr/bin/sudo", "/usr/bin/test", "!", "-L", str(QUARANTINE_ROOT.parent)],
        ["/usr/bin/sudo", "/usr/bin/test", "!", "-L", str(QUARANTINE_ROOT)],
        ["/usr/bin/sudo", "/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "0700", str(QUARANTINE_ROOT)],
        ["/usr/bin/sudo", "/usr/bin/test", "!", "-L", str(frozen_root)],
        ["/usr/bin/sudo", "/usr/bin/test", "!", "-e", str(frozen_root)],
        ["/usr/bin/sudo", "/bin/cp", "-R", "-P", str(stage_root), str(frozen_root)],
        ["/usr/bin/sudo", "/usr/sbin/chown", "-R", "-P", "-h", "root:wheel", str(frozen_root)],
        ["/usr/bin/sudo", "/bin/chmod", "-R", "-P", "-h", "-N", str(frozen_root)],
        ["/usr/bin/sudo", "/bin/chmod", "-R", "-P", "-h", "go-w", str(frozen_root)],
        ["/usr/bin/sudo", "/bin/chmod", "-h", "0700", str(frozen_root)],
    ]
    human_digest_checks = [
        ["/usr/bin/sudo", "/usr/bin/stat", "-f", "%HT %Su %Sg %Lp", str(request)],
        ["/usr/bin/sudo", "/usr/bin/shasum", "-a", "256", str(request)],
        ["/usr/bin/sudo", "/usr/bin/shasum", "-a", "256", str(bootstrap)],
        ["/usr/bin/sudo", "/usr/bin/shasum", "-a", "256", str(checksums)],
        ["/usr/bin/sudo", "/usr/bin/shasum", "-a", "256", "-c", str(checksums)],
    ]
    seal_command = [
        "/usr/bin/sudo", "/usr/bin/python3", "-I", "-B", str(bootstrap),
        str(frozen_root), approved_freeze_request_sha256,
    ]
    tail = [
        "--frozen-root", str(frozen_root),
        "--approved-freeze-request-sha256", approved_freeze_request_sha256,
    ]
    activate = ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-B", str(importer), "activate", *tail]
    rollback = ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-B", str(importer), "rollback", *tail]
    uninstall = ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-B", str(importer), "uninstall", *tail]
    return {
        "phase_1_freeze_argv": freeze_commands,
        "human_post_freeze_check_argv": human_digest_checks,
        "trusted_post_freeze_seal_argv": seal_command,
        "phase_2_activate_argv": activate,
        "rollback_argv": rollback,
        "uninstall_argv": uninstall,
        "freeze_copy_failure_precheck_argv": [
            ["/usr/bin/sudo", "/usr/bin/test", "!", "-L", str(frozen_root / FREEZE_SEAL_NAME)],
            ["/usr/bin/sudo", "/usr/bin/test", "!", "-e", str(frozen_root / FREEZE_SEAL_NAME)],
            ["/usr/bin/sudo", "/usr/bin/test", "!", "-L", str(frozen_root / ACTIVATION_JOURNAL_NAME)],
            ["/usr/bin/sudo", "/usr/bin/test", "!", "-e", str(frozen_root / ACTIVATION_JOURNAL_NAME)],
        ],
        "freeze_copy_failure_cleanup_argv": ["/usr/bin/sudo", "/bin/rm", "-rf", str(frozen_root)],
        "trusted_bootstrap_sha256": sha256_bytes(POST_FREEZE_BOOTSTRAP_SOURCE.encode("utf-8")),
        "frozen_root": str(frozen_root),
        "frozen_importer": str(importer),
    }


def _path_digest(path: Path, kind: str) -> str:
    return tree_digest(path) if kind == "tree" else sha256_file(path)


def _path_descriptor(path: Path, kind: str) -> dict[str, Any]:
    observed = path.lstat()
    if kind == "tree":
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("managed_target_kind_invalid", path.name)
    elif not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise DeploymentError("managed_target_kind_invalid", path.name)
    return {
        "kind": kind,
        "path": str(path),
        "sha256": _path_digest(path, kind),
        "mode": mode_text(observed.st_mode),
        "uid": observed.st_uid,
        "gid": observed.st_gid,
    }


def _descriptor_matches(path: Path, descriptor: dict[str, Any]) -> bool:
    try:
        actual = _path_descriptor(path, descriptor["kind"])
        return all(actual[field] == descriptor[field] for field in actual)
    except (OSError, DeploymentError):
        return False


def _open_stable_directory_fd(
    path: Path,
    reason: str,
    *,
    expected_uid: int | None = None,
    reject_group_or_other_write: bool = False,
) -> int:
    """Open an exact directory identity without following its final component."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        after = path.lstat()
    except OSError as exc:
        raise DeploymentError(reason, path.name) from exc
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or _inode_identity(before) != _inode_identity(opened)
        or _inode_identity(opened) != _inode_identity(after)
        or (expected_uid is not None and opened.st_uid != expected_uid)
        or (reject_group_or_other_write and stat.S_IMODE(opened.st_mode) & 0o022)
    ):
        os.close(fd)
        raise DeploymentError(reason, path.name)
    return fd


def _named_entry_stat(parent_fd: int, name: str, reason: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise DeploymentError(reason, name) from exc


def _ensure_directory(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    creator_uid: int | None = None,
    race_reason: str = "install_directory_race_detected",
) -> bool:
    """Create and permission one directory through stable descriptors only."""

    parent_fd = _open_stable_directory_fd(path.parent, "install_parent_invalid")
    child_fd = -1
    created = False
    try:
        try:
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(path.name, mode=mode, dir_fd=parent_fd)
            except OSError as exc:
                raise DeploymentError(race_reason, path.name) from exc
            created = True
            named = _named_entry_stat(parent_fd, path.name, race_reason)
        except OSError as exc:
            raise DeploymentError("install_directory_unavailable", path.name) from exc
        if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
            raise DeploymentError(race_reason if created else "install_directory_invalid", path.name)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            child_fd = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise DeploymentError(race_reason, path.name) from exc
        opened = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _inode_identity(named) != _inode_identity(opened)
        ):
            raise DeploymentError(race_reason, path.name)
        if created:
            expected_creator = os.geteuid() if creator_uid is None else creator_uid
            if opened.st_uid != expected_creator:
                raise DeploymentError(race_reason, path.name)
            os.fchown(child_fd, uid, gid)
            os.fchmod(child_fd, mode)
            updated = os.fstat(child_fd)
            if updated.st_uid != uid or updated.st_gid != gid or stat.S_IMODE(updated.st_mode) != mode:
                raise DeploymentError(race_reason, path.name)
        final_named = _named_entry_stat(parent_fd, path.name, race_reason)
        if _inode_identity(final_named) != _inode_identity(opened):
            raise DeploymentError(race_reason, path.name)
        os.fsync(parent_fd)
        return created
    except DeploymentError:
        raise
    except OSError as exc:
        raise DeploymentError(race_reason, path.name) from exc
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(parent_fd)


def _ensure_parent_chain(path: Path, *, uid: int, gid: int) -> list[Path]:
    missing: list[Path] = []
    current = path
    while True:
        try:
            observed = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current == current.parent:
                raise DeploymentError("install_parent_root_missing")
            current = current.parent
            continue
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("install_parent_invalid", current.name)
        break
    ancestor = current.parent
    while ancestor != ancestor.parent:
        try:
            observed = ancestor.lstat()
        except OSError as exc:
            raise DeploymentError("install_parent_unavailable", ancestor.name) from exc
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("install_parent_invalid", ancestor.name)
        ancestor = ancestor.parent
    created: list[Path] = []
    for directory in reversed(missing):
        if _ensure_directory(directory, uid=uid, gid=gid, mode=0o755):
            created.append(directory)
    return created


@contextmanager
def _deployment_transaction_lock(
    *,
    admin_uid: int,
    admin_gid: int,
    verify_ancestors: bool,
) -> Iterable[None]:
    """Hold the single host-wide deployment transaction lock without waiting."""

    _ensure_parent_chain(DEPLOYMENT_LOCK_PATH.parent, uid=admin_uid, gid=admin_gid)
    if verify_ancestors and admin_uid == 0:
        _verify_safe_ancestors(
            DEPLOYMENT_LOCK_PATH.parent,
            lambda candidate: candidate.lstat().st_uid,
        )
    parent_fd = _open_stable_directory_fd(
        DEPLOYMENT_LOCK_PATH.parent,
        "deployment_lock_parent_invalid",
        expected_uid=admin_uid,
        reject_group_or_other_write=True,
    )
    lock_fd = -1
    created = False
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(
                DEPLOYMENT_LOCK_PATH.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            created = True
        except FileExistsError:
            lock_fd = os.open(DEPLOYMENT_LOCK_PATH.name, flags, dir_fd=parent_fd)
        observed = os.fstat(lock_fd)
        named = _named_entry_stat(
            parent_fd,
            DEPLOYMENT_LOCK_PATH.name,
            "deployment_lock_trust_invalid",
        )
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or _inode_identity(observed) != _inode_identity(named)
        ):
            raise DeploymentError("deployment_lock_trust_invalid")
        if created:
            if observed.st_uid != os.geteuid():
                raise DeploymentError("deployment_lock_trust_invalid")
            os.fchown(lock_fd, admin_uid, admin_gid)
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
            os.fsync(parent_fd)
            observed = os.fstat(lock_fd)
        if observed.st_uid != admin_uid or stat.S_IMODE(observed.st_mode) != 0o600:
            raise DeploymentError("deployment_lock_trust_invalid")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}:
                raise DeploymentError("deployment_transaction_in_progress") from exc
            raise DeploymentError("deployment_lock_unavailable") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except DeploymentError:
        raise
    except OSError as exc:
        raise DeploymentError("deployment_lock_unavailable") from exc
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(parent_fd)


def _copy_regular_new(
    source: Path,
    destination: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    source_data, source_stat = _stable_regular_file(source, "install_source_changed")
    parent_fd = _open_stable_directory_fd(destination.parent, "install_parent_invalid")
    fd = -1
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(destination.name, flags, mode, dir_fd=parent_fd)
        created = True
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise DeploymentError("install_copy_race_detected", destination.name)
        offset = 0
        while offset < len(source_data):
            offset += os.write(fd, source_data[offset:])
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        os.fsync(fd)
        completed = os.fstat(fd)
        named = _named_entry_stat(parent_fd, destination.name, "install_copy_race_detected")
        if (
            _inode_identity(opened) != _inode_identity(completed)
            or _inode_identity(completed) != _inode_identity(named)
            or completed.st_size != source_stat.st_size
        ):
            raise DeploymentError("install_copy_digest_mismatch", destination.name)
        os.fsync(parent_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
            fd = -1
        if created:
            try:
                os.unlink(destination.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _copy_tree_new(source: Path, destination: Path, *, uid: int, gid: int) -> None:
    try:
        source_stat = source.lstat()
    except OSError as exc:
        raise DeploymentError("install_tree_source_unavailable") from exc
    if not stat.S_ISDIR(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
        raise DeploymentError("install_tree_source_invalid")
    if not _ensure_directory(
        destination,
        uid=uid,
        gid=gid,
        mode=stat.S_IMODE(source_stat.st_mode),
    ):
        raise DeploymentError("install_tree_destination_collision", destination.name)
    try:
        for child in sorted(os.scandir(source), key=lambda entry: entry.name):
            child_source = Path(child.path)
            child_destination = destination / child.name
            observed = child_source.lstat()
            if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
                _copy_tree_new(child_source, child_destination, uid=uid, gid=gid)
            elif stat.S_ISREG(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
                _copy_regular_new(
                    child_source,
                    child_destination,
                    mode=stat.S_IMODE(observed.st_mode),
                    uid=uid,
                    gid=gid,
                )
            else:
                raise DeploymentError("install_tree_source_special_file", child.name)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _remove_regular_or_tree(path: Path, kind: str) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return
    if kind == "tree":
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("managed_target_kind_invalid", path.name)
        shutil.rmtree(path)
    else:
        if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("managed_target_kind_invalid", path.name)
        path.unlink()


def _write_atomic_json(path: Path, value: Any, *, mode: int, uid: int, gid: int) -> None:
    """Durably replace JSON through one verified parent directory descriptor."""

    parent_fd = _open_stable_directory_fd(
        path.parent,
        "transaction_metadata_parent_invalid",
        expected_uid=uid,
        reject_group_or_other_write=True,
    )
    temporary_name = f".{path.name}.tmp"
    temporary_fd = -1
    renamed = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise DeploymentError("transaction_metadata_target_invalid", path.name) from exc
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or stat.S_ISLNK(existing.st_mode)
            or existing.st_uid != uid
            or stat.S_IMODE(existing.st_mode) != mode
        ):
            raise DeploymentError("transaction_metadata_target_invalid", path.name)
        try:
            os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DeploymentError("transaction_metadata_temp_collision", path.name) from exc
        else:
            raise DeploymentError("transaction_metadata_temp_collision", path.name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, flags, mode, dir_fd=parent_fd)
        opened = os.fstat(temporary_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise DeploymentError("transaction_metadata_temp_invalid", path.name)
        data = canonical_json_bytes(value)
        offset = 0
        while offset < len(data):
            offset += os.write(temporary_fd, data[offset:])
        os.fchown(temporary_fd, uid, gid)
        os.fchmod(temporary_fd, mode)
        os.fsync(temporary_fd)
        completed = os.fstat(temporary_fd)
        named = _named_entry_stat(
            parent_fd,
            temporary_name,
            "transaction_metadata_temp_invalid",
        )
        if (
            _inode_identity(opened) != _inode_identity(completed)
            or _inode_identity(completed) != _inode_identity(named)
            or completed.st_size != len(data)
            or completed.st_uid != uid
            or completed.st_gid != gid
            or stat.S_IMODE(completed.st_mode) != mode
        ):
            raise DeploymentError("transaction_metadata_temp_invalid", path.name)
        os.rename(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        renamed = True
        os.fsync(parent_fd)
    except DeploymentError:
        if not renamed:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    except OSError as exc:
        if not renamed:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        raise DeploymentError("transaction_metadata_write_failed", path.name) from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        os.close(parent_fd)


def deployment_epoch_path(profile_id: str) -> Path:
    _require_supported_identity(profile_id)
    return ASSURANCE_ROOT / "epochs" / f"{profile_id}.json"


def _parse_epoch_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DeploymentError("deployment_epoch_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DeploymentError("deployment_epoch_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DeploymentError("deployment_epoch_invalid")
    return parsed.astimezone(timezone.utc)


def _format_epoch_timestamp(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_deployment_epoch(
    value: Any,
    *,
    profile_id: str | None = None,
) -> dict[str, Any]:
    epoch = _require_exact_fields(value, DEPLOYMENT_EPOCH_FIELDS, "deployment_epoch_invalid")
    if epoch["epoch_version"] != DEPLOYMENT_EPOCH_VERSION:
        raise DeploymentError("deployment_epoch_invalid")
    _require_supported_identity(epoch["profile_id"])
    if profile_id is not None and epoch["profile_id"] != profile_id:
        raise DeploymentError("deployment_epoch_profile_mismatch")
    for field in ("epoch_id", "transaction_id"):
        if not isinstance(epoch[field], str) or not SAFE_ID.fullmatch(epoch[field]):
            raise DeploymentError("deployment_epoch_invalid")
    previous = epoch["previous_epoch_id"]
    if previous is not None and (
        not isinstance(previous, str) or not SAFE_ID.fullmatch(previous)
    ):
        raise DeploymentError("deployment_epoch_invalid")
    if previous == epoch["epoch_id"]:
        raise DeploymentError("deployment_epoch_invalid")
    if epoch["state"] not in DEPLOYMENT_EPOCH_STATES:
        raise DeploymentError("deployment_epoch_invalid")
    if epoch["operation"] not in DEPLOYMENT_EPOCH_OPERATIONS:
        raise DeploymentError("deployment_epoch_invalid")
    rotated_at = _parse_epoch_timestamp(epoch["rotated_at"])
    if epoch["state"] == "transitioning":
        if epoch["finalized_at"] is not None:
            raise DeploymentError("deployment_epoch_invalid")
    else:
        finalized_at = _parse_epoch_timestamp(epoch["finalized_at"])
        if finalized_at < rotated_at:
            raise DeploymentError("deployment_epoch_invalid")
    expected_final_state = {
        "activate": "active_uncommissioned",
        "rollback": "restored_uncommissioned",
        "uninstall": "uninstalled",
    }[epoch["operation"]]
    if epoch["state"] != "transitioning" and epoch["state"] != expected_final_state:
        raise DeploymentError("deployment_epoch_invalid")
    return epoch


def _ensure_epoch_directory(
    *,
    admin_uid: int,
    admin_gid: int,
    verify_ancestors: bool,
) -> Path:
    epochs_root = ASSURANCE_ROOT / "epochs"
    for directory in (ASSURANCE_ROOT, epochs_root):
        _ensure_parent_chain(directory.parent, uid=admin_uid, gid=admin_gid)
        _ensure_directory(directory, uid=admin_uid, gid=admin_gid, mode=0o755)
        try:
            observed = directory.lstat()
        except OSError as exc:
            raise DeploymentError("deployment_epoch_directory_trust_invalid") from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_uid != admin_uid
            or stat.S_IMODE(observed.st_mode) != 0o755
        ):
            raise DeploymentError("deployment_epoch_directory_trust_invalid")
        if verify_ancestors and admin_uid == 0:
            _verify_safe_ancestors(directory, lambda path: path.lstat().st_uid)
    return epochs_root


def load_deployment_epoch(
    profile_id: str,
    *,
    admin_uid: int = 0,
    verify_ancestors: bool = True,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    path = deployment_epoch_path(profile_id)
    try:
        observed = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise DeploymentError("deployment_epoch_missing")
    except OSError as exc:
        raise DeploymentError("deployment_epoch_unavailable") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != admin_uid
        or stat.S_IMODE(observed.st_mode) != 0o644
    ):
        raise DeploymentError("deployment_epoch_trust_invalid")
    epochs_root = path.parent
    for directory in (ASSURANCE_ROOT, epochs_root):
        try:
            root_observed = directory.lstat()
        except OSError as exc:
            raise DeploymentError("deployment_epoch_directory_trust_invalid") from exc
        if (
            not stat.S_ISDIR(root_observed.st_mode)
            or stat.S_ISLNK(root_observed.st_mode)
            or root_observed.st_uid != admin_uid
            or stat.S_IMODE(root_observed.st_mode) != 0o755
        ):
            raise DeploymentError("deployment_epoch_directory_trust_invalid")
    if verify_ancestors and admin_uid == 0:
        _verify_safe_ancestors(path, lambda candidate: candidate.lstat().st_uid)
    return validate_deployment_epoch(
        _load_json_regular(path, "deployment_epoch_unloadable"),
        profile_id=profile_id,
    )


def _rotate_deployment_epoch(
    profile_id: str,
    *,
    operation: str,
    transaction_id: str,
    admin_uid: int,
    admin_gid: int,
    verify_ancestors: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    if operation not in DEPLOYMENT_EPOCH_OPERATIONS:
        raise DeploymentError("deployment_epoch_operation_invalid")
    if not isinstance(transaction_id, str) or not SAFE_ID.fullmatch(transaction_id):
        raise DeploymentError("deployment_epoch_transaction_invalid")
    _ensure_epoch_directory(
        admin_uid=admin_uid,
        admin_gid=admin_gid,
        verify_ancestors=verify_ancestors,
    )
    previous = load_deployment_epoch(
        profile_id,
        admin_uid=admin_uid,
        verify_ancestors=verify_ancestors,
        allow_missing=True,
    )
    epoch = validate_deployment_epoch(
        {
            "epoch_version": DEPLOYMENT_EPOCH_VERSION,
            "profile_id": profile_id,
            "epoch_id": f"epoch-{uuid.uuid4().hex}",
            "state": "transitioning",
            "operation": operation,
            "transaction_id": transaction_id,
            "previous_epoch_id": previous["epoch_id"] if previous is not None else None,
            "rotated_at": _format_epoch_timestamp(now),
            "finalized_at": None,
        },
        profile_id=profile_id,
    )
    path = deployment_epoch_path(profile_id)
    _write_atomic_json(path, epoch, mode=0o644, uid=admin_uid, gid=admin_gid)
    return epoch


def _finalize_deployment_epoch(
    epoch: dict[str, Any],
    *,
    state: str,
    admin_uid: int,
    admin_gid: int,
    verify_ancestors: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    expected = validate_deployment_epoch(epoch, profile_id=epoch.get("profile_id"))
    current = load_deployment_epoch(
        expected["profile_id"],
        admin_uid=admin_uid,
        verify_ancestors=verify_ancestors,
    )
    if current != expected or current["state"] != "transitioning":
        raise DeploymentError("deployment_epoch_changed")
    finalized = validate_deployment_epoch(
        {**expected, "state": state, "finalized_at": _format_epoch_timestamp(now)},
        profile_id=expected["profile_id"],
    )
    _write_atomic_json(
        deployment_epoch_path(expected["profile_id"]),
        finalized,
        mode=0o644,
        uid=admin_uid,
        gid=admin_gid,
    )
    return finalized


def _deployment_specs(
    manifest: dict[str, Any],
    *,
    payload_root: Path,
    admin_uid: int,
    admin_gid: int,
) -> list[dict[str, Any]]:
    release = manifest["release_commit"]
    runtime_uid = manifest["bindings"]["runtime_user_uid"]
    try:
        runtime_gid = pwd.getpwuid(runtime_uid).pw_gid
    except KeyError as exc:
        raise DeploymentError("runtime_user_account_unavailable") from exc
    source_manifest = payload_root / "manifest" / MANIFEST_PATH.name
    specs = [
        {
            "name": "runtime_bundle",
            "kind": "tree",
            "source": str(payload_root / "runtime" / release),
            "path": manifest["artifacts"]["runtime_bundle"]["path"],
            "sha256": manifest["artifacts"]["runtime_bundle"]["sha256"],
            "mode": manifest["artifacts"]["runtime_bundle"]["mode"],
            "uid": admin_uid,
            "gid": admin_gid,
        },
        *[
            {
                "name": name,
                "kind": "file",
                "source": str(source),
                "path": manifest["artifacts"][name]["path"],
                "sha256": manifest["artifacts"][name]["sha256"],
                "mode": manifest["artifacts"][name]["mode"],
                "uid": runtime_uid if name == "codex_profile" else admin_uid,
                "gid": runtime_gid if name == "codex_profile" else admin_gid,
            }
            for name, source in (
                ("runtime_config", payload_root / "config" / RUNTIME_CONFIG_PATH.name),
                ("requirements", payload_root / "etc" / "requirements.toml"),
                ("bridge_wrapper", payload_root / "libexec" / BRIDGE_WRAPPER_PATH.name),
                ("codex_launcher", payload_root / "bin" / CODEX_LAUNCHER_PATH.name),
                ("codex_profile", payload_root / "user" / f"{CODEX_PROFILE_NAME}.config.toml"),
            )
        ],
        {
            "name": "deployment_manifest",
            "kind": "file",
            "source": str(source_manifest),
            "path": str(MANIFEST_PATH),
            "sha256": sha256_file(source_manifest),
            "mode": "0644",
            "uid": admin_uid,
            "gid": admin_gid,
        },
    ]
    for spec in specs:
        source = Path(spec["source"])
        if _path_digest(source, spec["kind"]) != spec["sha256"]:
            raise DeploymentError("frozen_install_source_digest_mismatch", spec["name"])
        if mode_text(source.lstat().st_mode) != spec["mode"]:
            raise DeploymentError("frozen_install_source_mode_mismatch", spec["name"])
    return specs


def _prior_specs(manifest: dict[str, Any], *, admin_uid: int) -> list[dict[str, Any]]:
    names = [
        "runtime_bundle",
        "runtime_config",
        "requirements",
        "bridge_wrapper",
        "codex_launcher",
        "codex_profile",
    ]
    specs = []
    for name in names:
        artifact = manifest["artifacts"][name]
        specs.append(
            {
                "name": name,
                "kind": artifact["kind"],
                "path": artifact["path"],
                "sha256": artifact["sha256"],
                "mode": artifact["mode"],
                "uid": (
                    manifest["bindings"]["runtime_user_uid"]
                    if name == "codex_profile"
                    else admin_uid
                ),
            }
        )
    manifest_stat = MANIFEST_PATH.lstat()
    specs.append(
        {
            "name": "deployment_manifest",
            "kind": "file",
            "path": str(MANIFEST_PATH),
            "sha256": sha256_file(MANIFEST_PATH),
            "mode": mode_text(manifest_stat.st_mode),
            "uid": admin_uid,
        }
    )
    return specs


def _load_prior_deployment(
    *,
    new_specs: list[dict[str, Any]],
    uid_reader: Callable[[Path], int] | None,
    verify_ancestors: bool,
    admin_uid: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    manifest_present = MANIFEST_PATH.exists() or MANIFEST_PATH.is_symlink()
    if not manifest_present:
        collisions = [spec["name"] for spec in new_specs if Path(spec["path"]).exists() or Path(spec["path"]).is_symlink()]
        if collisions:
            raise DeploymentError("unowned_existing_target_collision", ",".join(collisions))
        return None, []
    try:
        prior = load_manifest(MANIFEST_PATH)
        verify_deployment(MANIFEST_PATH, uid_reader=uid_reader, verify_ancestors=verify_ancestors)
    except DeploymentError as exc:
        raise DeploymentError("existing_deployment_untrusted", exc.reason) from exc
    prior_specs = _prior_specs(prior, admin_uid=admin_uid)
    prior_paths = {spec["path"] for spec in prior_specs}
    collisions = [
        spec["name"]
        for spec in new_specs
        if (Path(spec["path"]).exists() or Path(spec["path"]).is_symlink())
        and spec["path"] not in prior_paths
    ]
    if collisions:
        raise DeploymentError("unowned_existing_target_collision", ",".join(collisions))
    return prior, prior_specs


def _backup_prior_deployment(
    prior_specs: list[dict[str, Any]],
    *,
    backup_root: Path,
    transaction_id: str,
    admin_uid: int,
    admin_gid: int,
) -> dict[str, Any]:
    if backup_root.exists() or backup_root.is_symlink():
        raise DeploymentError("backup_transaction_collision")
    _ensure_parent_chain(backup_root.parent, uid=admin_uid, gid=admin_gid)
    _ensure_directory(backup_root, uid=admin_uid, gid=admin_gid, mode=0o700)
    if admin_uid == 0:
        _verify_safe_ancestors(backup_root, lambda path: path.lstat().st_uid)
    artifacts_root = backup_root / "artifacts"
    _ensure_directory(artifacts_root, uid=admin_uid, gid=admin_gid, mode=0o700)
    entries: list[dict[str, Any]] = []
    try:
        for spec in prior_specs:
            source = Path(spec["path"])
            descriptor = _path_descriptor(source, spec["kind"])
            if any(descriptor[field] != spec[field] for field in ("sha256", "mode", "uid")):
                raise DeploymentError("existing_deployment_changed_before_backup", spec["name"])
            backup_path = artifacts_root / spec["name"]
            if spec["kind"] == "tree":
                _copy_tree_new(source, backup_path, uid=admin_uid, gid=admin_gid)
            else:
                _copy_regular_new(
                    source,
                    backup_path,
                    mode=parse_mode(descriptor["mode"]),
                    uid=admin_uid,
                    gid=admin_gid,
                )
            if _path_digest(backup_path, spec["kind"]) != descriptor["sha256"]:
                raise DeploymentError("backup_digest_mismatch", spec["name"])
            entries.append({**descriptor, "name": spec["name"], "backup_path": str(backup_path)})
        result = {
            "backup_version": TRANSACTION_VERSION,
            "transaction_id": transaction_id,
            "prior_deployment_present": bool(prior_specs),
            "entries": entries,
        }
        manifest_path = backup_root / BACKUP_MANIFEST_NAME
        _write_atomic_json(
            manifest_path,
            result,
            mode=0o400,
            uid=admin_uid,
            gid=admin_gid,
        )
        return result
    except Exception:
        shutil.rmtree(backup_root, ignore_errors=True)
        raise


def _install_one_spec(spec: dict[str, Any], *, transaction_id: str) -> None:
    source = Path(spec["source"])
    target = Path(spec["path"])
    temporary = _transaction_temporary_path(target, transaction_id)
    if temporary.exists() or temporary.is_symlink():
        raise DeploymentError("install_temporary_collision", spec["name"])
    _ensure_parent_chain(target.parent, uid=spec["uid"], gid=spec["gid"])
    if spec["uid"] == 0:
        _verify_safe_ancestors(target, lambda path: path.lstat().st_uid)
    try:
        if spec["kind"] == "tree":
            _copy_tree_new(source, temporary, uid=spec["uid"], gid=spec["gid"])
        else:
            _copy_regular_new(
                source,
                temporary,
                mode=parse_mode(spec["mode"]),
                uid=spec["uid"],
                gid=spec["gid"],
            )
        if _path_digest(temporary, spec["kind"]) != spec["sha256"]:
            raise DeploymentError("install_temporary_digest_mismatch", spec["name"])
        if spec["kind"] == "tree" and target.exists():
            _remove_regular_or_tree(target, "tree")
        os.replace(temporary, target)
    except Exception:
        try:
            _remove_regular_or_tree(temporary, spec["kind"])
        except (OSError, DeploymentError):
            pass
        raise


def _transaction_temporary_path(target: Path, transaction_id: str) -> Path:
    return target.with_name(f".{target.name}.saihai-{transaction_id}.tmp")


def _remove_transaction_temporary(
    target: dict[str, Any], *, transaction_id: str, admin_uid: int
) -> None:
    temporary = _transaction_temporary_path(Path(target["path"]), transaction_id)
    try:
        observed = temporary.lstat()
    except FileNotFoundError:
        return
    if target["kind"] == "tree":
        valid_kind = stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode)
    else:
        valid_kind = stat.S_ISREG(observed.st_mode) and not stat.S_ISLNK(observed.st_mode)
    if not valid_kind:
        raise DeploymentError("rollback_temporary_target_invalid", target["name"])
    descriptor = {**target, "path": str(temporary)}
    if observed.st_uid != admin_uid and not _descriptor_matches(temporary, descriptor):
        raise DeploymentError("rollback_temporary_target_unrecognized", target["name"])
    _remove_regular_or_tree(temporary, target["kind"])


def _journal_new_target(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "kind": spec["kind"],
        "path": spec["path"],
        "sha256": spec["sha256"],
        "mode": spec["mode"],
        "uid": spec["uid"],
        "gid": spec["gid"],
    }


def _assurance_directories(deployment_id: str) -> list[tuple[Path, int]]:
    return [
        (ASSURANCE_ROOT, 0o755),
        (ASSURANCE_ROOT / "epochs", 0o755),
        (ASSURANCE_ROOT / "commissioning", 0o755),
        (ASSURANCE_ROOT / "commissioning" / deployment_id, 0o700),
        (ASSURANCE_ROOT / "generations", 0o755),
        (ASSURANCE_ROOT / "generations" / deployment_id, 0o755),
        (ASSURANCE_ROOT / "active", 0o755),
        (ASSURANCE_ROOT / "launch-sessions", 0o755),
        (ASSURANCE_ROOT / "commissioning-launches", 0o755),
    ]


def _prepare_codex_home(
    manifest: dict[str, Any],
    *,
    admin_uid: int,
    admin_gid: int,
    uid_reader: Callable[[Path], int] | None = None,
    verify_ancestors: bool = True,
) -> list[Path]:
    del admin_gid
    bindings = manifest["bindings"]
    user_home = Path(bindings["runtime_user_home"])
    codex_home = Path(bindings["codex_home"])
    runtime_uid = bindings["runtime_user_uid"]
    try:
        runtime_gid = pwd.getpwuid(runtime_uid).pw_gid
    except KeyError as exc:
        raise DeploymentError("runtime_user_account_unavailable") from exc
    require_trusted_runtime_user_home(
        user_home,
        runtime_uid,
        uid_reader=uid_reader or (lambda path: path.lstat().st_uid),
        verify_ancestors=verify_ancestors,
    )
    created: list[Path] = []
    if _ensure_directory(
        codex_home,
        uid=runtime_uid,
        gid=runtime_gid,
        mode=0o700,
        creator_uid=admin_uid,
        race_reason="codex_home_race_detected",
    ):
        created.append(codex_home)
    home_fd = _open_stable_directory_fd(
        codex_home,
        "codex_home_trust_invalid",
        expected_uid=runtime_uid,
        reject_group_or_other_write=True,
    )
    try:
        observed = os.fstat(home_fd)
        if stat.S_IMODE(observed.st_mode) != 0o700:
            raise DeploymentError("codex_home_trust_invalid")
        try:
            os.stat("config.toml", dir_fd=home_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DeploymentError("codex_home_trust_invalid") from exc
        else:
            raise DeploymentError("codex_home_base_config_forbidden")
    finally:
        os.close(home_fd)
    return created


def _load_backup_manifest(path: Path, transaction_id: str, *, admin_uid: int) -> dict[str, Any]:
    expected_root = BACKUP_ROOT / transaction_id
    if path != expected_root / BACKUP_MANIFEST_NAME:
        raise DeploymentError("backup_manifest_path_invalid")
    try:
        root_stat = expected_root.lstat()
        manifest_stat = path.lstat()
    except OSError as exc:
        raise DeploymentError("backup_manifest_unloadable") from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
        or root_stat.st_uid != admin_uid
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or not stat.S_ISREG(manifest_stat.st_mode)
        or stat.S_ISLNK(manifest_stat.st_mode)
        or manifest_stat.st_uid != admin_uid
        or stat.S_IMODE(manifest_stat.st_mode) != 0o400
    ):
        raise DeploymentError("backup_manifest_trust_invalid")
    value = _load_json_regular(path, "backup_manifest_unloadable")
    if (
        not isinstance(value, dict)
        or set(value) != {"backup_version", "transaction_id", "prior_deployment_present", "entries"}
        or value["backup_version"] != TRANSACTION_VERSION
        or value["transaction_id"] != transaction_id
        or not isinstance(value["prior_deployment_present"], bool)
        or not isinstance(value["entries"], list)
    ):
        raise DeploymentError("backup_manifest_invalid")
    expected_names = {
        "runtime_bundle",
        "runtime_config",
        "requirements",
        "bridge_wrapper",
        "codex_launcher",
        "codex_profile",
        "deployment_manifest",
    }
    seen: set[str] = set()
    for entry in value["entries"]:
        fields = {"kind", "path", "sha256", "mode", "uid", "gid", "name", "backup_path"}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise DeploymentError("backup_manifest_entry_invalid")
        name = entry["name"]
        if name not in expected_names or name in seen:
            raise DeploymentError("backup_manifest_entry_invalid")
        seen.add(name)
        if entry["kind"] not in {"file", "tree"} or not _safe_absolute_path(entry["path"]):
            raise DeploymentError("backup_manifest_entry_invalid")
        if entry["backup_path"] != str(expected_root / "artifacts" / name):
            raise DeploymentError("backup_manifest_entry_invalid")
        if not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"]):
            raise DeploymentError("backup_manifest_entry_invalid")
        parse_mode(entry["mode"])
        for field in ("uid", "gid"):
            if not isinstance(entry[field], int) or isinstance(entry[field], bool) or entry[field] < 0:
                raise DeploymentError("backup_manifest_entry_invalid")
    if value["prior_deployment_present"] != bool(value["entries"]):
        raise DeploymentError("backup_manifest_invalid")
    return value


def _restore_backup_entry(entry: dict[str, Any], *, transaction_id: str) -> None:
    target = Path(entry["path"])
    backup = Path(entry["backup_path"])
    if _path_digest(backup, entry["kind"]) != entry["sha256"]:
        raise DeploymentError("backup_artifact_digest_mismatch", entry["name"])
    spec = {
        "name": f"rollback-{entry['name']}",
        "kind": entry["kind"],
        "source": str(backup),
        "path": str(target),
        "sha256": entry["sha256"],
        "mode": entry["mode"],
        "uid": entry["uid"],
        "gid": entry["gid"],
    }
    _install_one_spec(spec, transaction_id=f"{transaction_id}-rollback")


def rollback_frozen_deployment(
    frozen_root: Path,
    approved_freeze_request_sha256: str,
    *,
    admin_uid: int = 0,
    admin_gid: int = 0,
    uid_reader: Callable[[Path], int] | None = None,
    verify_ancestors: bool = True,
    enforce_production_root: bool = True,
    operation: str = "rollback",
) -> dict[str, Any]:
    with _deployment_transaction_lock(
        admin_uid=admin_uid,
        admin_gid=admin_gid,
        verify_ancestors=verify_ancestors,
    ):
        return _rollback_frozen_deployment_transaction(
            frozen_root,
            approved_freeze_request_sha256,
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            uid_reader=uid_reader,
            verify_ancestors=verify_ancestors,
            enforce_production_root=enforce_production_root,
            operation=operation,
        )


def _rollback_frozen_deployment_transaction(
    frozen_root: Path,
    approved_freeze_request_sha256: str,
    *,
    admin_uid: int = 0,
    admin_gid: int = 0,
    uid_reader: Callable[[Path], int] | None = None,
    verify_ancestors: bool = True,
    enforce_production_root: bool = True,
    operation: str = "rollback",
) -> dict[str, Any]:
    if operation not in {"rollback", "uninstall"}:
        raise DeploymentError("deployment_epoch_operation_invalid")
    request = verify_frozen_stage(
        frozen_root,
        approved_freeze_request_sha256,
        expected_uid=admin_uid,
        require_seal=True,
        enforce_production_root=enforce_production_root,
        uid_reader=uid_reader,
        verify_runtime_home_ancestors=verify_ancestors,
    )
    frozen_root = Path(request["frozen_root"])
    manifest = load_manifest(frozen_root / PAYLOAD_DIR_NAME / "manifest" / MANIFEST_PATH.name)
    _require_supported_identity(request["deployment_id"], manifest["bindings"]["principal_id"])
    expected_specs = _deployment_specs(
        manifest,
        payload_root=frozen_root / PAYLOAD_DIR_NAME,
        admin_uid=admin_uid,
        admin_gid=admin_gid,
    )
    journal_path = frozen_root / ACTIVATION_JOURNAL_NAME
    try:
        journal_stat = journal_path.lstat()
    except FileNotFoundError:
        # Rotation intentionally precedes journal creation.  A crash in that
        # narrow window is recoverable only when no transaction artifact or
        # target mutation can have occurred and the matching transition epoch
        # is still current.  Recovery itself rotates again; it never restores
        # the former epoch bytes.
        if operation != "rollback":
            raise DeploymentError("activation_journal_required_for_uninstall")
        current_epoch = load_deployment_epoch(
            manifest["deployment_id"],
            admin_uid=admin_uid,
            verify_ancestors=verify_ancestors,
            allow_missing=True,
        )
        if (
            current_epoch is None
            or current_epoch["state"] != "transitioning"
            or current_epoch["operation"] != "activate"
            or current_epoch["transaction_id"] != request["transaction_id"]
        ):
            raise DeploymentError("activation_journal_unavailable")
        backup_root = BACKUP_ROOT / request["transaction_id"]
        candidate = MANIFEST_PATH.with_name(
            f".{MANIFEST_PATH.name}.saihai-{request['transaction_id']}.candidate"
        )
        transaction_paths = [backup_root, candidate]
        transaction_paths.extend(
            _transaction_temporary_path(Path(spec["path"]), request["transaction_id"])
            for spec in expected_specs
        )
        if any(path.exists() or path.is_symlink() for path in transaction_paths):
            raise DeploymentError("activation_journal_missing_with_transaction_artifacts")
        _load_prior_deployment(
            new_specs=expected_specs,
            uid_reader=uid_reader,
            verify_ancestors=verify_ancestors,
            admin_uid=admin_uid,
        )
        recovery_epoch = _rotate_deployment_epoch(
            manifest["deployment_id"],
            operation="rollback",
            transaction_id=request["transaction_id"],
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            verify_ancestors=verify_ancestors,
        )
        finalized_epoch = _finalize_deployment_epoch(
            recovery_epoch,
            state="restored_uncommissioned",
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            verify_ancestors=verify_ancestors,
        )
        return {
            "decision": "rolled_back",
            "transaction_id": request["transaction_id"],
            "operation": operation,
            "journal_recovery": "pre_journal_transition",
            "deployment_epoch_id": finalized_epoch["epoch_id"],
            "deployment_epoch_state": finalized_epoch["state"],
        }
    except OSError as exc:
        raise DeploymentError("activation_journal_unavailable") from exc
    if (
        not stat.S_ISREG(journal_stat.st_mode)
        or stat.S_ISLNK(journal_stat.st_mode)
        or journal_stat.st_uid != admin_uid
        or stat.S_IMODE(journal_stat.st_mode) != 0o600
    ):
        raise DeploymentError("activation_journal_trust_invalid")
    journal = _load_json_regular(journal_path, "activation_journal_unloadable")
    required = {
        "transaction_version",
        "transaction_id",
        "state",
        "backup_manifest",
        "new_targets",
        "created_directories",
        "prior_deployment_present",
        "prior_manifest_sha256",
    }
    if not isinstance(journal, dict) or set(journal) != required or journal.get("transaction_id") != request["transaction_id"]:
        raise DeploymentError("activation_journal_invalid")
    if journal["transaction_version"] != TRANSACTION_VERSION or journal["state"] not in {
        "preflight_complete",
        "backup_complete",
        "activated",
        "rolled_back",
        "uninstalled",
    }:
        raise DeploymentError("activation_journal_invalid")
    if journal["backup_manifest"] != str(BACKUP_ROOT / request["transaction_id"] / BACKUP_MANIFEST_NAME):
        raise DeploymentError("activation_journal_invalid")
    if not isinstance(journal["prior_deployment_present"], bool):
        raise DeploymentError("activation_journal_invalid")
    if journal["prior_deployment_present"]:
        if not isinstance(journal["prior_manifest_sha256"], str) or not SHA256.fullmatch(
            journal["prior_manifest_sha256"]
        ):
            raise DeploymentError("activation_journal_invalid")
    elif journal["prior_manifest_sha256"] is not None:
        raise DeploymentError("activation_journal_invalid")
    if journal["new_targets"] != [_journal_new_target(spec) for spec in expected_specs]:
        raise DeploymentError("activation_journal_targets_invalid")
    if not isinstance(journal["created_directories"], list) or not all(
        isinstance(item, str) and _safe_absolute_path(item) for item in journal["created_directories"]
    ):
        raise DeploymentError("activation_journal_directories_invalid")
    codex_home = Path(manifest["bindings"]["codex_home"])
    assurance_parent = ASSURANCE_ROOT.parent
    for item in journal["created_directories"]:
        candidate = Path(item)
        allowed = candidate == codex_home
        try:
            candidate.relative_to(assurance_parent)
            allowed = True
        except ValueError:
            pass
        if not allowed:
            raise DeploymentError("activation_journal_directories_invalid")
    epoch = _rotate_deployment_epoch(
        manifest["deployment_id"],
        operation=operation,
        transaction_id=request["transaction_id"],
        admin_uid=admin_uid,
        admin_gid=admin_gid,
        verify_ancestors=verify_ancestors,
    )
    final_epoch_state = (
        "restored_uncommissioned" if operation == "rollback" else "uninstalled"
    )
    if journal["state"] == "rolled_back" and operation == "rollback":
        finalized_epoch = _finalize_deployment_epoch(
            epoch,
            state=final_epoch_state,
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            verify_ancestors=verify_ancestors,
        )
        return {
            "decision": "rolled_back",
            "transaction_id": request["transaction_id"],
            "operation": operation,
            "deployment_epoch_id": finalized_epoch["epoch_id"],
            "deployment_epoch_state": finalized_epoch["state"],
        }
    backup_manifest_path = Path(journal["backup_manifest"])
    missing_backup_after_rollback = (
        journal["state"] == "rolled_back"
        and operation == "uninstall"
        and not (backup_manifest_path.exists() or backup_manifest_path.is_symlink())
    )
    if journal["state"] == "preflight_complete" or missing_backup_after_rollback:
        prior_specs: list[dict[str, Any]] = []
        if journal["prior_deployment_present"]:
            if not (MANIFEST_PATH.exists() or MANIFEST_PATH.is_symlink()) or sha256_file(
                MANIFEST_PATH
            ) != journal["prior_manifest_sha256"]:
                raise DeploymentError("preflight_recovery_manifest_drift")
            verify_deployment(
                MANIFEST_PATH, uid_reader=uid_reader, verify_ancestors=verify_ancestors
            )
            prior_specs = _prior_specs(load_manifest(MANIFEST_PATH), admin_uid=admin_uid)
        elif any(Path(target["path"]).exists() or Path(target["path"]).is_symlink() for target in journal["new_targets"]):
            reason = (
                "uninstall_target_unrecognized"
                if missing_backup_after_rollback
                else "preflight_recovery_target_collision"
            )
            raise DeploymentError(reason)
        partial_backup = BACKUP_ROOT / request["transaction_id"]
        if partial_backup.exists() or partial_backup.is_symlink():
            observed = partial_backup.lstat()
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or observed.st_uid != admin_uid
            ):
                raise DeploymentError("preflight_recovery_backup_invalid")
            shutil.rmtree(partial_backup)
        if operation == "rollback":
            journal["state"] = "rolled_back"
            _write_atomic_json(
                journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid
            )
            finalized_epoch = _finalize_deployment_epoch(
                epoch,
                state=final_epoch_state,
                admin_uid=admin_uid,
                admin_gid=admin_gid,
                verify_ancestors=verify_ancestors,
            )
            return {
                "decision": "rolled_back",
                "transaction_id": request["transaction_id"],
                "operation": operation,
                "deployment_epoch_id": finalized_epoch["epoch_id"],
                "deployment_epoch_state": finalized_epoch["state"],
            }
        _backup_prior_deployment(
            prior_specs,
            backup_root=partial_backup,
            transaction_id=request["transaction_id"],
            admin_uid=admin_uid,
            admin_gid=admin_gid,
        )
        journal["state"] = "backup_complete"
        _write_atomic_json(journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid)
    backup = _load_backup_manifest(
        backup_manifest_path, request["transaction_id"], admin_uid=admin_uid
    )
    for target in journal["new_targets"]:
        _remove_transaction_temporary(
            target, transaction_id=request["transaction_id"], admin_uid=admin_uid
        )
    candidate = MANIFEST_PATH.with_name(
        f".{MANIFEST_PATH.name}.saihai-{request['transaction_id']}.candidate"
    )
    if candidate.exists() or candidate.is_symlink():
        observed_candidate = candidate.lstat()
        if (
            not stat.S_ISREG(observed_candidate.st_mode)
            or stat.S_ISLNK(observed_candidate.st_mode)
            or observed_candidate.st_uid != admin_uid
        ):
            raise DeploymentError("rollback_manifest_candidate_invalid")
        candidate.unlink()
    descriptor_fields = ("kind", "path", "sha256", "mode", "uid", "gid")
    prior_by_path = {entry["path"]: entry for entry in backup["entries"]}
    if operation == "rollback":
        for target in reversed(journal["new_targets"]):
            path = Path(target["path"])
            if not (path.exists() or path.is_symlink()):
                continue
            prior = prior_by_path.get(target["path"])
            if _descriptor_matches(path, target):
                _remove_regular_or_tree(path, target["kind"])
            elif prior is not None and _descriptor_matches(
                path, {key: prior[key] for key in descriptor_fields}
            ):
                continue
            else:
                raise DeploymentError("rollback_target_unrecognized", target["name"])
        for entry in backup["entries"]:
            target = Path(entry["path"])
            descriptor = {key: entry[key] for key in descriptor_fields}
            if target.exists() and _descriptor_matches(target, descriptor):
                continue
            if target.exists() or target.is_symlink():
                raise DeploymentError("rollback_restore_collision", entry["name"])
            _restore_backup_entry(entry, transaction_id=request["transaction_id"])
    else:
        descriptors_by_path: dict[str, list[dict[str, Any]]] = {}
        names_by_path: dict[str, str] = {}
        ordered_paths: list[str] = []
        for target in reversed(journal["new_targets"]):
            path_text = target["path"]
            if path_text not in descriptors_by_path:
                ordered_paths.append(path_text)
                descriptors_by_path[path_text] = []
                names_by_path[path_text] = target["name"]
            descriptors_by_path[path_text].append(target)
        for entry in reversed(backup["entries"]):
            path_text = entry["path"]
            if path_text not in descriptors_by_path:
                ordered_paths.append(path_text)
                descriptors_by_path[path_text] = []
                names_by_path[path_text] = entry["name"]
            descriptors_by_path[path_text].append(
                {key: entry[key] for key in descriptor_fields}
            )
        for path_text in ordered_paths:
            path = Path(path_text)
            if not (path.exists() or path.is_symlink()):
                continue
            matching = next(
                (
                    descriptor
                    for descriptor in descriptors_by_path[path_text]
                    if _descriptor_matches(path, descriptor)
                ),
                None,
            )
            if matching is None:
                raise DeploymentError(
                    "uninstall_target_unrecognized", names_by_path[path_text]
                )
            _remove_regular_or_tree(path, matching["kind"])
        if any(
            Path(path_text).exists() or Path(path_text).is_symlink()
            for path_text in ordered_paths
        ):
            raise DeploymentError("uninstall_target_removal_incomplete")
    for directory_text in reversed(journal["created_directories"]):
        directory = Path(directory_text)
        try:
            observed = directory.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DeploymentError("rollback_created_directory_invalid", directory.name)
        try:
            directory.rmdir()
        except OSError:
            # User auth/session state or lifecycle evidence may legitimately
            # make a Saihai-created directory non-empty. Never delete it
            # recursively during rollback/uninstall.
            continue
    journal["state"] = "rolled_back" if operation == "rollback" else "uninstalled"
    _write_atomic_json(journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid)
    finalized_epoch = _finalize_deployment_epoch(
        epoch,
        state=final_epoch_state,
        admin_uid=admin_uid,
        admin_gid=admin_gid,
        verify_ancestors=verify_ancestors,
    )
    return {
        "decision": "rolled_back" if operation == "rollback" else "uninstalled",
        "transaction_id": request["transaction_id"],
        "operation": operation,
        "deployment_epoch_id": finalized_epoch["epoch_id"],
        "deployment_epoch_state": finalized_epoch["state"],
    }


def activate_frozen_deployment(
    frozen_root: Path,
    approved_freeze_request_sha256: str,
    *,
    admin_uid: int = 0,
    admin_gid: int = 0,
    uid_reader: Callable[[Path], int] | None = None,
    verify_ancestors: bool = True,
    enforce_production_root: bool = True,
) -> dict[str, Any]:
    with _deployment_transaction_lock(
        admin_uid=admin_uid,
        admin_gid=admin_gid,
        verify_ancestors=verify_ancestors,
    ):
        return _activate_frozen_deployment_transaction(
            frozen_root,
            approved_freeze_request_sha256,
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            uid_reader=uid_reader,
            verify_ancestors=verify_ancestors,
            enforce_production_root=enforce_production_root,
        )


def _activate_frozen_deployment_transaction(
    frozen_root: Path,
    approved_freeze_request_sha256: str,
    *,
    admin_uid: int = 0,
    admin_gid: int = 0,
    uid_reader: Callable[[Path], int] | None = None,
    verify_ancestors: bool = True,
    enforce_production_root: bool = True,
) -> dict[str, Any]:
    request = verify_frozen_stage(
        frozen_root,
        approved_freeze_request_sha256,
        expected_uid=admin_uid,
        require_seal=True,
        enforce_production_root=enforce_production_root,
        uid_reader=uid_reader,
        verify_runtime_home_ancestors=verify_ancestors,
    )
    frozen_root = Path(request["frozen_root"])
    payload_root = frozen_root / PAYLOAD_DIR_NAME
    source_manifest = payload_root / "manifest" / MANIFEST_PATH.name
    manifest = load_manifest(source_manifest)
    _require_supported_identity(request["deployment_id"], manifest["bindings"]["principal_id"])
    if manifest["release_commit"] != request["release_commit"] or manifest["deployment_id"] != request["deployment_id"]:
        raise DeploymentError("frozen_manifest_request_mismatch")
    specs = _deployment_specs(
        manifest, payload_root=payload_root, admin_uid=admin_uid, admin_gid=admin_gid
    )
    prior, prior_specs = _load_prior_deployment(
        new_specs=specs,
        uid_reader=uid_reader,
        verify_ancestors=verify_ancestors,
        admin_uid=admin_uid,
    )
    backup_root = BACKUP_ROOT / request["transaction_id"]
    journal_path = frozen_root / ACTIVATION_JOURNAL_NAME
    candidate = MANIFEST_PATH.with_name(f".{MANIFEST_PATH.name}.saihai-{request['transaction_id']}.candidate")
    collision_paths = [journal_path, backup_root, candidate]
    collision_paths.extend(
        _transaction_temporary_path(Path(spec["path"]), request["transaction_id"])
        for spec in specs
    )
    if any(path.exists() or path.is_symlink() for path in collision_paths):
        raise DeploymentError("activation_transaction_collision")
    # Revoke every prior generation before the journal or any managed target
    # is mutated.  All exceptions below deliberately leave this transition
    # current until a separately invoked rollback rotates a new epoch.
    epoch = _rotate_deployment_epoch(
        manifest["deployment_id"],
        operation="activate",
        transaction_id=request["transaction_id"],
        admin_uid=admin_uid,
        admin_gid=admin_gid,
        verify_ancestors=verify_ancestors,
    )
    journal = {
        "transaction_version": TRANSACTION_VERSION,
        "transaction_id": request["transaction_id"],
        "state": "preflight_complete",
        "backup_manifest": str(backup_root / BACKUP_MANIFEST_NAME),
        "new_targets": [_journal_new_target(spec) for spec in specs],
        "created_directories": [],
        "prior_deployment_present": prior is not None,
        "prior_manifest_sha256": (
            next(spec["sha256"] for spec in prior_specs if spec["name"] == "deployment_manifest")
            if prior is not None
            else None
        ),
    }
    _write_atomic_json(
        journal_path,
        journal,
        mode=0o600,
        uid=admin_uid,
        gid=admin_gid,
    )
    manifest_spec = next(spec for spec in specs if spec["name"] == "deployment_manifest")
    try:
        _backup_prior_deployment(
            prior_specs,
            backup_root=backup_root,
            transaction_id=request["transaction_id"],
            admin_uid=admin_uid,
            admin_gid=admin_gid,
        )
        journal["state"] = "backup_complete"
        _write_atomic_json(journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid)
        created_directories = _prepare_codex_home(
            manifest,
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            uid_reader=uid_reader,
            verify_ancestors=verify_ancestors,
        )
        journal["created_directories"].extend(str(path) for path in created_directories)
        _write_atomic_json(journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid)
        for directory, required_mode in _assurance_directories(manifest["deployment_id"]):
            parents = _ensure_parent_chain(directory.parent, uid=admin_uid, gid=admin_gid)
            for parent in parents:
                if str(parent) not in journal["created_directories"]:
                    journal["created_directories"].append(str(parent))
            if _ensure_directory(directory, uid=admin_uid, gid=admin_gid, mode=required_mode):
                journal["created_directories"].append(str(directory))
            observed = directory.lstat()
            if observed.st_uid != admin_uid or stat.S_IMODE(observed.st_mode) != required_mode:
                raise DeploymentError("assurance_directory_trust_invalid", directory.name)
            if admin_uid == 0:
                _verify_safe_ancestors(directory, lambda path: path.lstat().st_uid)
            _write_atomic_json(journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid)
        for spec in specs:
            if spec is manifest_spec:
                continue
            _install_one_spec(spec, transaction_id=request["transaction_id"])
        _ensure_parent_chain(MANIFEST_PATH.parent, uid=admin_uid, gid=admin_gid)
        if candidate.exists() or candidate.is_symlink():
            raise DeploymentError("manifest_candidate_collision")
        _copy_regular_new(
            Path(manifest_spec["source"]),
            candidate,
            mode=0o644,
            uid=admin_uid,
            gid=admin_gid,
        )
        verify_deployment(candidate, uid_reader=uid_reader, verify_ancestors=verify_ancestors)
        os.replace(candidate, MANIFEST_PATH)
        journal["state"] = "activated"
        _write_atomic_json(journal_path, journal, mode=0o600, uid=admin_uid, gid=admin_gid)
        finalized_epoch = _finalize_deployment_epoch(
            epoch,
            state="active_uncommissioned",
            admin_uid=admin_uid,
            admin_gid=admin_gid,
            verify_ancestors=verify_ancestors,
        )
    except Exception as activation_error:
        if isinstance(activation_error, DeploymentError):
            raise activation_error
        raise DeploymentError("activation_failed") from activation_error
    return {
        "decision": "activated",
        "transaction_id": request["transaction_id"],
        "release_commit": manifest["release_commit"],
        "backup_manifest": str(backup_root / BACKUP_MANIFEST_NAME),
        "rollback_available": True,
        "deployment_epoch_id": finalized_epoch["epoch_id"],
        "deployment_epoch_state": finalized_epoch["state"],
    }


def prepare_deployment(
    *,
    source_root: Path,
    stage_root: Path,
    managed_primary: Path,
    user_home: Path,
    python_executable: Path = PYTHON_EXECUTABLE,
    native_codex_executable: Path = NATIVE_CODEX_EXECUTABLE,
    principal_id: str = "codex-main-agent-a-prime",
    frontend_policy_domain: str = "codex-main-agent-host",
    worker_policy_domain: str = "saihai-worker-isolated",
) -> dict[str, Any]:
    """Create a reviewable data stage and two-gate admin plan.

    The preparer performs no administrator action.  In particular, no command
    in the plan executes a path beneath this user-writable stage.
    """

    _require_supported_identity(principal_id, principal_id)

    source_root = source_root.resolve(strict=True)
    managed_primary = managed_primary.resolve(strict=True)
    python_executable = python_executable.resolve(strict=True)
    native_codex_executable = native_codex_executable.resolve(strict=True)
    user_home = Path(user_home)
    try:
        user_observed = user_home.lstat()
    except OSError as exc:
        raise DeploymentError("runtime_user_home_trust_invalid") from exc
    runtime_uid = user_observed.st_uid
    if runtime_uid < 1:
        raise DeploymentError("runtime_user_home_trust_invalid")
    user_home = require_trusted_runtime_user_home(
        user_home,
        runtime_uid,
        uid_reader=lambda path: path.lstat().st_uid,
        verify_ancestors=True,
    )
    try:
        runtime_account = pwd.getpwuid(runtime_uid)
    except KeyError as exc:
        raise DeploymentError("runtime_user_account_unavailable") from exc
    try:
        account_home = Path(runtime_account.pw_dir).resolve(strict=True)
    except OSError as exc:
        raise DeploymentError("runtime_user_home_mismatch") from exc
    if account_home != user_home:
        raise DeploymentError("runtime_user_home_mismatch")
    runtime_user_name = runtime_account.pw_name
    if not USER_NAME.fullmatch(runtime_user_name):
        raise DeploymentError("runtime_user_name_invalid")
    if stage_root.exists() and any(stage_root.iterdir()):
        raise DeploymentError("stage_root_not_empty")
    stage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage_root.chmod(0o700)
    stage_root = stage_root.resolve(strict=True)
    if any(character in str(stage_root) for character in "\r\n"):
        raise DeploymentError("stage_root_path_invalid")
    payload_root = stage_root / PAYLOAD_DIR_NAME
    if frontend_policy_domain == worker_policy_domain:
        raise DeploymentError("worker_policy_domain_not_separate")
    codex_home = user_home / ".codex-saihai-main-agent"
    profile_target = codex_home / f"{CODEX_PROFILE_NAME}.config.toml"
    catalog_path = managed_primary / "directory-path.env"
    catalog_observation, catalog_bytes = _catalog_observation(catalog_path, runtime_uid=runtime_uid)
    state_root, state_root_source = _resolved_state_root(
        catalog_path=catalog_path,
        catalog_bytes=catalog_bytes,
        user_home=user_home,
    )
    native_observed = native_codex_executable.lstat()
    if (
        not stat.S_ISREG(native_observed.st_mode)
        or stat.S_ISLNK(native_observed.st_mode)
        or stat.S_IMODE(native_observed.st_mode) & 0o022
    ):
        raise DeploymentError("native_codex_source_not_safe")
    _native_codex_version(native_codex_executable)
    _assert_trusted_native_codex_distribution(native_codex_executable)
    release_commit = _require_clean_release_checkout(source_root)
    runtime_target = RUNTIME_ROOT / release_commit
    runtime_stage = payload_root / "runtime" / release_commit
    _extract_git_archive(source_root, release_commit, runtime_stage)
    native_relative_path = "bin/codex"
    native_stage = runtime_stage / native_relative_path
    _copy_new_regular(native_codex_executable, native_stage, 0o555)
    _native_codex_version(native_stage)
    _assert_trusted_native_codex_distribution(native_stage)
    observer_stage = runtime_stage / OBSERVER_RELATIVE_PATH
    observer_observed = observer_stage.lstat()
    if not stat.S_ISREG(observer_observed.st_mode) or stat.S_ISLNK(observer_observed.st_mode):
        raise DeploymentError("observer_source_invalid")
    observer_stage.chmod(0o555)

    instructions_target = runtime_target / INSTRUCTIONS_RELATIVE_PATH
    manifest: dict[str, Any] = {
        "deployment_version": "1",
        "deployment_id": principal_id,
        "release_commit": release_commit,
        "platform": "macos",
        "expected_admin_uid": 0,
        "topology": {
            "frontend_policy_domain": frontend_policy_domain,
            "write_capable_worker_policy_domain": worker_policy_domain,
            "shared_rootfs": False,
            "worker_isolation": "separate_host_vm_or_container",
        },
        "bindings": {
            "frontdoor": "codex",
            "principal_id": principal_id,
            "workspace_id": "Saber5656/Saihai",
            "managed_primary": str(managed_primary),
            "checkout_binding": "launch_cwd_registered_worktree",
            "workspace_catalog": "git_worktree_list",
            "assurance_root": str(ASSURANCE_ROOT),
            "state_root_catalog": str(managed_primary / "directory-path.env"),
            "deployment_manifest_path": str(MANIFEST_PATH),
            "runtime_config_path": str(RUNTIME_CONFIG_PATH),
            "instructions_path": str(instructions_target),
            "developer_instructions_sha256": "sha256:" + "0" * 64,
            "runtime_user_uid": runtime_uid,
            "runtime_user_name": runtime_user_name,
            "runtime_user_home": str(user_home),
            "codex_home": str(codex_home),
            "state_root": str(state_root),
            "state_root_source": state_root_source,
            "state_root_catalog_observation": catalog_observation,
        },
        "artifacts": {
            "runtime_bundle": {
                "kind": "tree",
                "path": str(runtime_target),
                "sha256": "",
                "mode": "0755",
                "digest_algorithm": "saihai-tree-sha256-v1",
                "entrypoint": ENTRYPOINT_RELATIVE_PATH,
                "verifier": VERIFIER_RELATIVE_PATH,
                "deployment_module": DEPLOYMENT_MODULE_RELATIVE_PATH,
            },
            "bridge_wrapper": _artifact(BRIDGE_WRAPPER_PATH, "0555"),
            "python_executable": _artifact(python_executable, mode_text(python_executable.lstat().st_mode)),
            "runtime_config": _artifact(RUNTIME_CONFIG_PATH, "0644"),
            "requirements": _artifact(REQUIREMENTS_PATH, "0644"),
            "instructions": _artifact(instructions_target, "0644"),
            "codex_launcher": _artifact(CODEX_LAUNCHER_PATH, "0555"),
            "codex_profile": _artifact(profile_target, "0600"),
            "native_codex": {
                **_artifact(runtime_target / native_relative_path, "0555"),
                "version": NATIVE_CODEX_VERSION,
                "package": NATIVE_CODEX_PACKAGE,
                "package_version": NATIVE_CODEX_PACKAGE_VERSION,
                "package_integrity": NATIVE_CODEX_PACKAGE_INTEGRITY,
                "platform": NATIVE_CODEX_PLATFORM,
                "architecture": NATIVE_CODEX_ARCHITECTURE,
            },
            "supervisor": _artifact(runtime_target / SUPERVISOR_RELATIVE_PATH, "0644"),
            "observer": _artifact(runtime_target / OBSERVER_RELATIVE_PATH, "0555"),
        },
        "tool_contract": {
            "server_name": "saihai_bridge",
            "enabled_tools": ENABLED_TOOLS,
            "forbidden_tools": ["approve", "classify", "create_run", "execute", "provider_dispatch", "shell"],
        },
    }

    python_observed = python_executable.lstat()
    if not stat.S_ISREG(python_observed.st_mode) or stat.S_ISLNK(python_observed.st_mode):
        raise DeploymentError("python_executable_not_regular")
    if python_observed.st_uid != 0 or stat.S_IMODE(python_observed.st_mode) & 0o022:
        raise DeploymentError("python_executable_not_admin_safe")
    manifest["artifacts"]["python_executable"]["sha256"] = sha256_file(python_executable)
    manifest["artifacts"]["native_codex"]["sha256"] = sha256_file(native_stage)
    manifest["artifacts"]["supervisor"]["sha256"] = sha256_file(
        runtime_stage / SUPERVISOR_RELATIVE_PATH
    )
    manifest["artifacts"]["observer"]["sha256"] = sha256_file(observer_stage)
    manifest["artifacts"]["runtime_bundle"]["sha256"] = tree_digest(runtime_stage)
    manifest["artifacts"]["instructions"]["sha256"] = sha256_file(runtime_stage / INSTRUCTIONS_RELATIVE_PATH)
    manifest["bindings"]["developer_instructions_sha256"] = manifest["artifacts"]["instructions"]["sha256"]

    requirements = _render_codex_text(
        (runtime_stage / REQUIREMENTS_TEMPLATE_PATH.relative_to(WORKFLOW_ROOT.parent.parent.parent)).read_text(
            encoding="utf-8"
        ),
        user_home=user_home,
        release_commit=release_commit,
    )
    requirements_stage = payload_root / "etc" / "requirements.toml"
    _write_new(requirements_stage, requirements.encode("utf-8"), 0o644)
    manifest["artifacts"]["requirements"]["sha256"] = sha256_file(requirements_stage)

    instructions_text = (runtime_stage / INSTRUCTIONS_RELATIVE_PATH).read_text(encoding="utf-8")
    profile = _render_profile(
        (runtime_stage / PROFILE_TEMPLATE_PATH.relative_to(WORKFLOW_ROOT.parent.parent.parent)).read_text(
            encoding="utf-8"
        ),
        user_home=user_home,
        release_commit=release_commit,
        developer_instructions=instructions_text,
    )
    profile_stage = payload_root / "user" / f"{CODEX_PROFILE_NAME}.config.toml"
    _write_new(profile_stage, profile.encode("utf-8"), 0o600)
    manifest["artifacts"]["codex_profile"]["sha256"] = sha256_file(profile_stage)

    verifier_stage = runtime_stage / VERIFIER_RELATIVE_PATH
    entrypoint_target = runtime_target / ENTRYPOINT_RELATIVE_PATH
    verifier_target = runtime_target / VERIFIER_RELATIVE_PATH
    deployment_module_target = runtime_target / DEPLOYMENT_MODULE_RELATIVE_PATH
    wrapper_template = (runtime_stage / WRAPPER_TEMPLATE_PATH.relative_to(WORKFLOW_ROOT.parent.parent.parent)).read_text(
        encoding="utf-8"
    )
    wrapper = render_wrapper(
        wrapper_template,
        python_executable=python_executable,
        python_sha256=manifest["artifacts"]["python_executable"]["sha256"],
        verifier=verifier_target,
        verifier_sha256=sha256_file(verifier_stage),
        deployment_module=deployment_module_target,
        deployment_module_sha256=sha256_file(runtime_stage / DEPLOYMENT_MODULE_RELATIVE_PATH),
        manifest_path=MANIFEST_PATH,
        entrypoint=entrypoint_target,
        frontdoor="codex",
        principal_id=principal_id,
        workspace_id="Saber5656/Saihai",
        managed_primary=managed_primary,
        state_root=state_root,
        state_root_catalog=catalog_path,
        state_root_catalog_status=catalog_observation["status"],
        state_root_catalog_sha256=catalog_observation["sha256"],
    )
    wrapper_stage = payload_root / "libexec" / BRIDGE_WRAPPER_PATH.name
    _write_new(wrapper_stage, wrapper.encode("utf-8"), 0o555)
    manifest["artifacts"]["bridge_wrapper"]["sha256"] = sha256_file(wrapper_stage)

    launcher_template = (
        runtime_stage / LAUNCHER_TEMPLATE_PATH.relative_to(WORKFLOW_ROOT.parent.parent.parent)
    ).read_text(encoding="utf-8")
    launcher = render_launcher(
        launcher_template,
        native_codex=runtime_target / native_relative_path,
        native_codex_sha256=manifest["artifacts"]["native_codex"]["sha256"],
        native_codex_version=NATIVE_CODEX_VERSION,
        python_executable=python_executable,
        verifier=verifier_target,
        verifier_sha256=sha256_file(verifier_stage),
        deployment_module=deployment_module_target,
        deployment_module_sha256=sha256_file(runtime_stage / DEPLOYMENT_MODULE_RELATIVE_PATH),
        supervisor=runtime_target / SUPERVISOR_RELATIVE_PATH,
        supervisor_sha256=manifest["artifacts"]["supervisor"]["sha256"],
        manifest_path=MANIFEST_PATH,
        runtime_user_uid=runtime_uid,
        runtime_user_name=runtime_user_name,
        runtime_user_home=user_home,
        codex_home=codex_home,
        profile_path=profile_target,
        profile_sha256=manifest["artifacts"]["codex_profile"]["sha256"],
    )
    launcher_stage = payload_root / "bin" / CODEX_LAUNCHER_PATH.name
    _write_new(launcher_stage, launcher.encode("utf-8"), 0o555)
    manifest["artifacts"]["codex_launcher"]["sha256"] = sha256_file(launcher_stage)

    # Runtime config is generated from the same bindings used to render the
    # zero-argument wrapper, making drift independently detectable.
    runtime_config_stage = payload_root / "config" / RUNTIME_CONFIG_PATH.name
    runtime_config = runtime_config_payload(manifest)
    _write_new(runtime_config_stage, canonical_json_bytes(runtime_config), 0o644)
    manifest["artifacts"]["runtime_config"]["sha256"] = sha256_file(runtime_config_stage)

    validate_manifest(manifest)
    manifest_stage = payload_root / "manifest" / MANIFEST_PATH.name
    _write_new(manifest_stage, canonical_json_bytes(manifest), 0o644)
    transaction_id = f"{principal_id}-{release_commit[:12]}"
    if not SAFE_ID.fullmatch(transaction_id):
        raise DeploymentError("freeze_transaction_id_invalid")
    frozen_root = QUARANTINE_ROOT / transaction_id
    payload_entries = tree_inventory(payload_root)
    activation_importer_relative = (
        f"runtime/{release_commit}/organization/runtime/workflows/scripts/"
        "codex_main_agent_install.py"
    )
    importer_entries = [
        entry
        for entry in payload_entries
        if entry["kind"] == "file" and entry["path"] == activation_importer_relative
    ]
    if len(importer_entries) != 1:
        raise DeploymentError("freeze_activation_importer_invalid")
    checksums_bytes = _payload_checksums(
        payload_entries, frozen_payload=frozen_root / PAYLOAD_DIR_NAME
    )
    checksums_stage = stage_root / FREEZE_CHECKSUMS_NAME
    _write_new(checksums_stage, checksums_bytes, 0o600)
    bootstrap_stage = stage_root / FREEZE_BOOTSTRAP_NAME
    _write_new(bootstrap_stage, POST_FREEZE_BOOTSTRAP_SOURCE.encode("utf-8"), 0o400)
    freeze_request = {
        "freeze_version": FREEZE_VERSION,
        "transaction_id": transaction_id,
        "deployment_id": principal_id,
        "release_commit": release_commit,
        "frozen_root": str(frozen_root),
        "payload_relative_path": PAYLOAD_DIR_NAME,
        "payload_tree_sha256": tree_digest(payload_root),
        "payload_entries_sha256": _inventory_digest(payload_entries),
        "payload_entries": payload_entries,
        "payload_regular_file_count": sum(
            1 for entry in payload_entries if entry["kind"] == "file"
        ),
        "payload_directory_count": sum(
            1 for entry in payload_entries if entry["kind"] == "directory"
        ),
        "checksums_relative_path": FREEZE_CHECKSUMS_NAME,
        "checksums_sha256": sha256_file(checksums_stage),
        "bootstrap_relative_path": FREEZE_BOOTSTRAP_NAME,
        "bootstrap_sha256": sha256_file(bootstrap_stage),
        "activation_importer_relative_path": activation_importer_relative,
        "activation_importer_sha256": importer_entries[0]["sha256"],
    }
    validate_freeze_request(freeze_request, frozen_root=frozen_root)
    freeze_request_stage = stage_root / FREEZE_REQUEST_NAME
    _write_new(freeze_request_stage, canonical_json_bytes(freeze_request), 0o600)
    approved_request_digest = sha256_file(freeze_request_stage)
    flow = _admin_flow_commands(
        stage_root,
        manifest,
        approved_freeze_request_sha256=approved_request_digest,
        transaction_id=transaction_id,
    )
    plan = {
        "decision": "human_admin_gate",
        "side_effects_performed": "staging_only",
        "admin_flow_version": "2",
        "release_commit": release_commit,
        "stage_root": str(stage_root),
        "manifest": str(manifest_stage.resolve()),
        "freeze_request": str(freeze_request_stage),
        "approved_freeze_request_sha256": approved_request_digest,
        "approved_payload_tree_sha256": freeze_request["payload_tree_sha256"],
        "approved_payload_entries_sha256": freeze_request["payload_entries_sha256"],
        "approved_freeze_bootstrap_sha256": freeze_request["bootstrap_sha256"],
        "expected_post_freeze_request_owner": "root:wheel",
        "expected_post_freeze_request_mode": "0600",
        "required_human_gate": (
            "compare the root-copied freeze-request and bootstrap SHA-256 values with the approved values "
            "before the trusted seal command; compare every checksum result before activation"
        ),
        "topology_precondition": "write-capable worker uses a separate host, VM, or container rootfs",
        "native_codex_source": str(native_codex_executable),
        "native_codex_source_sha256_at_stage_copy": manifest["artifacts"]["native_codex"]["sha256"],
        "native_codex_version": NATIVE_CODEX_VERSION,
        "native_codex_package": NATIVE_CODEX_PACKAGE,
        "native_codex_package_version": NATIVE_CODEX_PACKAGE_VERSION,
        "native_codex_package_integrity": NATIVE_CODEX_PACKAGE_INTEGRITY,
        "native_codex_platform": NATIVE_CODEX_PLATFORM,
        "native_codex_architecture": NATIVE_CODEX_ARCHITECTURE,
        **flow,
    }
    for key in (
        "phase_1_freeze_argv",
        "human_post_freeze_check_argv",
        "freeze_copy_failure_precheck_argv",
    ):
        plan[key.replace("_argv", "_commands")] = [
            shlex.join(command) for command in plan[key]
        ]
    for key in (
        "trusted_post_freeze_seal_argv",
        "phase_2_activate_argv",
        "rollback_argv",
        "uninstall_argv",
        "freeze_copy_failure_cleanup_argv",
    ):
        plan[key.replace("_argv", "_command")] = shlex.join(plan[key])
    return plan


def install_main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in {"activate", "rollback", "uninstall"}:
        action = raw_argv[0]
        action_parser = argparse.ArgumentParser(
            description="Activate or recover a verified root-frozen Codex deployment"
        )
        action_parser.add_argument("action", choices=("activate", "rollback", "uninstall"))
        action_parser.add_argument("--frozen-root", type=Path, required=True)
        action_parser.add_argument("--approved-freeze-request-sha256", required=True)
        action_args = action_parser.parse_args(raw_argv)
        if os.geteuid() != 0:
            print(json.dumps({"decision": "blocked", "reason": "administrator_uid_required"}, sort_keys=True))
            return 2
        try:
            try:
                admin_gid = grp.getgrnam("wheel").gr_gid
            except KeyError:
                admin_gid = 0
            if action == "activate":
                result = activate_frozen_deployment(
                    action_args.frozen_root,
                    action_args.approved_freeze_request_sha256,
                    admin_uid=0,
                    admin_gid=admin_gid,
                )
            else:
                result = rollback_frozen_deployment(
                    action_args.frozen_root,
                    action_args.approved_freeze_request_sha256,
                    admin_uid=0,
                    admin_gid=admin_gid,
                    operation=action,
                )
        except DeploymentError as exc:
            print(json.dumps({"decision": "blocked", "reason": exc.reason}, sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    parser = argparse.ArgumentParser(description="Prepare (but never install) a Codex A-prime deployment")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--stage-root", type=Path, required=True)
    parser.add_argument("--managed-primary", type=Path, required=True)
    parser.add_argument("--user-home", type=Path, required=True)
    parser.add_argument("--python-executable", type=Path, default=PYTHON_EXECUTABLE)
    parser.add_argument(
        "--native-codex-executable",
        type=Path,
        default=NATIVE_CODEX_EXECUTABLE,
        help=(
            "Explicit stock native Codex path to copy into the immutable runtime; "
            "override this only for another install path of the pinned darwin-arm64 distribution"
        ),
    )
    parser.add_argument(
        "--principal-id",
        choices=(SUPPORTED_PRINCIPAL_ID,),
        default=SUPPORTED_PRINCIPAL_ID,
    )
    parser.add_argument("--frontend-policy-domain", default="codex-main-agent-host")
    parser.add_argument("--worker-policy-domain", required=True)
    args = parser.parse_args(raw_argv)
    try:
        plan = prepare_deployment(
            source_root=args.source_root,
            stage_root=args.stage_root,
            managed_primary=args.managed_primary,
            user_home=args.user_home,
            python_executable=args.python_executable,
            native_codex_executable=args.native_codex_executable,
            principal_id=args.principal_id,
            frontend_policy_domain=args.frontend_policy_domain,
            worker_policy_domain=args.worker_policy_domain,
        )
    except DeploymentError as exc:
        print(json.dumps({"decision": "blocked", "reason": exc.reason}, sort_keys=True))
        return 2
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a root-owned Codex A-prime deployment")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify_deployment(args.manifest)
    except DeploymentError as exc:
        if not args.quiet:
            print(json.dumps({"decision": "blocked", "reason": exc.reason}, sort_keys=True))
        return 2
    if not args.quiet:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(verify_main())
