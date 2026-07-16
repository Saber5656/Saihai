#!/usr/bin/env python3
"""Host-evidenced, platform-neutral agent integration assurance.

The repository registry contains target requirements only.  Runtime claims are
derived from short-lived evidence beneath an administrator-owned assurance
root.  Every gate call reopens and revalidates the attestation and every
referenced evidence file; declarations embedded in the registry or agent prose
never satisfy a claim.

Production CLI commands deliberately have no state-root override.  They use
``/Library/Application Support/Saihai/Assurance``.  Tests and
host gateways may inject an explicit state root through the library API, but
the selected :class:`TrustPolicy` still decides which ancestor and owners form
the trusted filesystem boundary.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import importlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = WORKFLOW_ROOT / "schemas" / "agent-integration-assurance.schema.json"
EVIDENCE_SCHEMA_PATH = WORKFLOW_ROOT / "schemas" / "agent-integration-evidence.schema.json"
ATTESTATION_SCHEMA_PATH = (
    WORKFLOW_ROOT / "schemas" / "agent-integration-attestation.schema.json"
)
DEFAULT_REGISTRY_PATH = (
    WORKFLOW_ROOT / "profiles" / "agent-integration-assurance.registry.json"
)
PRODUCTION_ASSURANCE_ROOT = Path(
    "/Library/Application Support/Saihai/Assurance"
)
ASSURANCE_NAMESPACE = "agent-integration-assurance"

CLAIMS = ("ingress_enforced", "action_enforced", "managed_worker")
SURFACE_ROLES = {"main_agent", "bounded_worker"}
INTEGRATION_STATES = {"configured", "available_advisory", "candidate", "unavailable"}
TRANSPORTS = {"mcp", "native_policy", "hook", "cli", "api", "unknown"}
OPERATION_COVERAGE = {
    "ambient",
    "saihai_gateway_only",
    "capability_scoped",
    "denied",
    "unverified",
    "not_applicable",
}
OPERATIONS = (
    "typed_request_submit",
    "redacted_projection_read",
    "output_acknowledge",
    "filesystem_write",
    "shell_exec",
    "process_spawn",
    "network_egress",
    "external_mutation",
    "provider_dispatch",
    "git_commit",
    "git_push",
    "pr_create",
    "release_publish",
    "credential_access",
    "agent_spawn",
    "surface_launch",
)
INGRESS_OPERATIONS = (
    "typed_request_submit",
    "redacted_projection_read",
    "output_acknowledge",
)
ACTION_OPERATIONS = (
    "filesystem_write",
    "shell_exec",
    "process_spawn",
    "network_egress",
    "external_mutation",
    "provider_dispatch",
    "git_commit",
    "git_push",
    "pr_create",
    "release_publish",
    "credential_access",
    "agent_spawn",
)
BINDING_FIELDS = (
    "configuration_digest",
    "runtime_binary_digest",
    "tool_inventory_digest",
    "checkout_digest",
)
COMMON_EVIDENCE_TYPES = (
    "configuration_snapshot",
    "runtime_binary_identity",
    "tool_inventory",
    "checkout_binding",
)
EVIDENCE_TYPES = {
    *COMMON_EVIDENCE_TYPES,
    "ingress_operation",
    "direct_action_denial",
    "gateway_positive_path",
    "worker_launch_binding",
    "capability_boundary",
}
FAILURE_POLICY = {
    "missing_evidence": "suppress",
    "invalid_evidence": "suppress",
    "stale_evidence": "suppress",
    "configuration_drift": "suppress",
    "claim_downgrade": "forbidden",
}
PROFILE_FIELDS = {
    "profile_version",
    "profile_id",
    "agent_family",
    "surface_role",
    "integration_state",
    "transports",
    "target_claims",
    "operation_requirements",
    "configuration_requirements",
    "evidence_policy",
    "tool_validator",
    "launch_validator",
    "execution_binding_validator",
    "rationale",
}
CONFIGURATION_ARTIFACT_ROLES = {
    "deployment_manifest",
    "runtime_config",
    "requirements",
    "bridge_wrapper",
    "instructions",
    "profile_snapshot",
    "observer",
    "supervisor",
}
EVIDENCE_FIELDS = {
    "evidence_version",
    "evidence_id",
    "generation_id",
    "profile_id",
    "claim",
    "evidence_type",
    "operation",
    "result",
    "observed_at",
    "valid_until",
    "registry_subject_digest",
    "bindings",
    "launch_session",
    "worker_execution_binding",
    "observed_digest",
    "details",
}
ATTESTATION_FIELDS = {
    "attestation_version",
    "attestation_id",
    "generation_id",
    "profile_id",
    "issued_at",
    "valid_until",
    "registry_subject_digest",
    "target_claims",
    "claim_results",
    "bindings",
    "launch_session",
    "runtime_binding",
    "worker_execution_binding",
    "generation_manifest",
    "evidence",
}
ACTIVE_ATTESTATION_FIELDS = {
    "active_version",
    "activation_id",
    "profile_id",
    "generation_id",
    "activated_at",
    "previous_generation_id",
    "generation_manifest",
    "attestation",
}
GENERATION_MANIFEST_FIELDS = {
    "manifest_version",
    "manifest_id",
    "profile_id",
    "generation_id",
    "commissioning_id",
    "deployment_epoch_id",
    "created_at",
    "registry_subject_digest",
    "launch_session_digest",
    "evidence",
    "manifest_digest",
}
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
DEPLOYMENT_EPOCH_STATES = {
    "transitioning",
    "active_uncommissioned",
    "restored_uncommissioned",
    "uninstalled",
}
DEPLOYMENT_EPOCH_OPERATIONS = {"activate", "rollback", "uninstall"}
COMMISSIONABLE_DEPLOYMENT_EPOCH_STATES = {
    "active_uncommissioned",
    "restored_uncommissioned",
}
LAUNCH_VALIDATOR_FIELDS = {
    "validator_id",
    "validator_version",
    "module",
    "function",
    "fields_constant",
    "validator_digest",
}
EXECUTION_BINDING_VALIDATOR_FIELDS = set(LAUNCH_VALIDATOR_FIELDS)
SAFE_MODULE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
SAFE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,95}$")
RUNTIME_BINDING_FIELDS = {
    "runtime_realpath",
    "runtime_digest",
    "argv",
    "argv_digest",
    "profile_realpath",
    "profile_digest",
}
HOST_OBSERVATION_FIELDS = {
    "observation_version",
    "observation_id",
    "profile_id",
    "evidence_type",
    "claim",
    "operation",
    "observed_at",
    "source",
    "attempted",
    "model_prose_authority",
    "outcome",
    "approval_prompted",
    "side_effect_observed",
    "before_marker_path",
    "before_marker_digest",
    "after_marker_path",
    "after_marker_digest",
    "saihai_request_id",
    "capability_id",
    "audit_id",
    "capability_bound",
    "ambient_authority",
    "launch_binding",
    "observer_receipt",
    "probe_process_binding",
}
WORKER_COMMISSIONING_RECEIPT_FIELDS = {
    "receipt_version",
    "receipt_id",
    "profile_id",
    "evidence_type",
    "claim",
    "operation",
    "observed_at",
    "observer",
    "event",
}
WORKER_COMMISSIONING_RECEIPT_EVENT_FIELDS = {
    "event_version",
    "event_id",
    "event_kind",
    "operation",
    "fact_source",
    "fact_value",
    "worker_probe_event",
    "commissioning_reference",
    "commissioning_record_digest",
    "worker_execution_binding_digest",
    "subject_pid",
    "process_start_token",
}
WORKER_PROBE_EVENT_ENVELOPE_FIELDS = {
    "envelope_version",
    "commissioning_reference",
    "claimed_record_digest",
    "single_use_nonce_digest",
    "execution_binding_digest",
    "event",
    "event_digest",
}
WORKER_PROBE_EVENT_FIELDS = {
    "event_version",
    "event_id",
    "profile_id",
    "generation_id",
    "purpose",
    "operation",
    "runtime_binding_digest",
    "runtime_realpath",
    "runtime_digest",
    "subject_pid",
    "process_start_token",
    "argv",
    "argv_digest",
    "marker_before_digest",
    "marker_after_digest",
    "probe_contract_result",
    "probe_result_digest",
    "probe_marker_digest",
    "probe_worktree_status_digest",
    "probe_facts",
    "exit_code",
    "observed_at",
}
WORKER_PROBE_OBSERVED_FACTS = {
    "surface_launch": "pinned_runtime_child_with_process_start_identity",
    "filesystem_write": "exact_marker_in_private_disposable_worktree",
    "process_spawn": "root_executor_spawned_exact_runtime",
    "git_commit": "no_head_after_probe",
}
WORKER_PROBE_POLICY_FACTS = {
    "shell_exec": "fixed_saihai_worker_profile_minimal_read_workspace_write",
    "network_egress": "saihai_worker_permission_profile.network.enabled=false",
    "external_mutation": "workspace_profile_and_network_disabled_not_same_rootfs_isolation",
    "provider_dispatch": "network_disabled_and_user_config_ignored",
    "git_push": "network_disabled_only_local_push_not_denied",
    "pr_create": "network_disabled_and_user_config_ignored",
    "release_publish": "network_disabled_and_user_config_ignored",
    "credential_access": "dedicated_auth_deny_configured_not_mechanically_proven",
    "agent_spawn": "multi_agent_and_plugin_features_disabled",
}
# These operations are deliberately blocked from promotion until the fixed
# commissioning probe carries an actual Codex denial for each one.  The
# current facts are weaker: a clean HEAD is only a postcondition; workspace
# and network configuration do not prove generic same-rootfs mutation denial;
# disabled networking does not prevent a local-repository push; and a
# configured dedicated-auth deny does not prove generic credential denial
# inside every future capability workspace.  Keeping this gate in the
# verifier also suppresses any already-written weak attestation.
WORKER_PROMOTION_BLOCKED_OPERATIONS = frozenset(
    {"external_mutation", "git_commit", "git_push", "credential_access"}
)

COMMON_RECEIPT_FIELDS = {
    "receipt_version",
    "receipt_id",
    "profile_id",
    "observed_at",
    "observer",
    "event",
}
COMMON_IDENTITY_EVENT_FIELDS = {
    "event_id",
    "event_kind",
    "subject_pid",
    "process_start_token",
    "runtime_binary_realpath",
    "runtime_binary_digest",
    "runtime_argv",
    "runtime_argv_digest",
    "profile_snapshot_path",
    "profile_snapshot_digest",
    "tool_inventory",
    "tool_inventory_classification",
    "tool_inventory_observation",
    "launcher_realpath",
    "launcher_digest",
    "launcher_process_start_token",
    "host_verified_launch_token",
    "launch_argv_digest",
    "effective_notify_empty",
    "configuration_digest",
    "tool_inventory_digest",
    "checkout_digest",
    "launch_session",
    "worker_execution_binding",
}
TOOL_INVENTORY_OBSERVATION_FIELDS = {
    "observation_version",
    "observation_kind",
    "session_id",
    "launch_session_digest",
    "deployment_manifest_digest",
    "requirements_digest",
    "profile_digest",
    "fixed_argv_digest",
    "policy_facts",
    "policy_facts_digest",
    "inventory_digest",
    "classification_digest",
}
TOOL_POLICY_FACT_FIELDS = {
    "approval_policy_never",
    "sandbox_read_only",
    "web_search_disabled",
    "external_features_disabled",
    "bridge_tools_exact",
    "plugins_disabled",
    "multi_agent_disabled",
    "notify_empty",
    "live_session_bound",
    "known_codex_auth_paths_denied",
}
TOOL_INVENTORY_CLASSIFICATION_FIELDS = {
    "inventory_version",
    "model_identifier",
    "server_backed_mcp_tools",
    "reviewed_internal_tools",
    "active_external_mutation_tools",
    "mechanically_denied_tools",
    "dynamic_tools",
    "extension_tools",
    "multi_agent_tools",
}
TOOL_VALIDATOR_FIELDS = {
    "validator_id",
    "validator_version",
    "validator_kind",
    "validator_digest",
    "classification_requirements",
    "manifest_enabled_tools",
    "operation_tool_names",
}
TOOL_CATEGORY_RULE_FIELDS = {"mode", "values", "alternatives"}
TOOL_CATEGORY_NAMES = (
    "server_backed_mcp_tools",
    "reviewed_internal_tools",
    "active_external_mutation_tools",
    "mechanically_denied_tools",
    "dynamic_tools",
    "extension_tools",
    "multi_agent_tools",
)
OBSERVER_KINDS = {
    "macos_endpoint_security",
    "sandbox_policy_audit",
    "host_gateway_executor",
}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
SAFE_FAMILY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")
OPAQUE_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_JSON_BYTES = 512 * 1024
MAX_EXTERNAL_ARTIFACT_BYTES = 512 * 1024 * 1024
DARWIN_ARGMAX_MIN = 4096
DARWIN_ARGMAX_MAX = 4 * 1024 * 1024
DARWIN_PROCARGS_RETRIES = 3
DARWIN_MAX_ARGC = 4096
LINUX_CMDLINE_MAX_BYTES = 1024 * 1024
CTL_KERN = 1
KERN_PROCARGS2 = 49


class AssuranceContractError(ValueError):
    """A static registry, evidence payload, or attestation is malformed."""


class EvidenceTrustError(RuntimeError):
    """A filesystem artifact does not meet the selected trust policy."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AssuranceGateError(RuntimeError):
    """A requested assurance claim is not currently verified."""

    def __init__(self, reason: str, reasons: Iterable[str] | None = None):
        normalized = tuple(sorted(set(reasons or (reason,))))
        self.reason = reason
        self.reasons = normalized
        super().__init__(reason)


def _require_profile_promotion_ready(profile: Mapping[str, Any]) -> None:
    if (
        profile.get("surface_role") == "bounded_worker"
        and "managed_worker" in profile.get("target_claims", [])
        and WORKER_PROMOTION_BLOCKED_OPERATIONS
    ):
        raise AssuranceGateError(
            "worker_denial_facts_not_promotable",
            (
                "worker_denial_fact_unproven:" + operation
                for operation in WORKER_PROMOTION_BLOCKED_OPERATIONS
            ),
        )


@dataclass(frozen=True)
class TrustPolicy:
    """Filesystem trust settings.

    Production anchors at ``/`` and permits only uid 0.  Tests may explicitly
    inject a narrower anchor and their fixture uid/gid; this does not alter the
    production CLI policy.
    """

    trusted_ancestor: Path
    expected_owner_uids: frozenset[int]
    expected_group_gids: frozenset[int] | None
    reject_group_write: bool = True
    reject_other_write: bool = True
    max_json_bytes: int = MAX_JSON_BYTES
    max_external_artifact_bytes: int = MAX_EXTERNAL_ARTIFACT_BYTES

    @classmethod
    def production(cls) -> "TrustPolicy":
        # macOS uses wheel (gid 0) and admin (gid 80) for administrator-owned
        # installation paths.  Neither group may have write permission.
        return cls(
            trusted_ancestor=Path("/"),
            expected_owner_uids=frozenset({0}),
            expected_group_gids=frozenset({0, 80}),
        )

    @classmethod
    def fixture(cls, trusted_ancestor: Path) -> "TrustPolicy":
        return cls(
            trusted_ancestor=trusted_ancestor.absolute(),
            expected_owner_uids=frozenset({os.getuid()}),
            expected_group_gids=frozenset({os.getgid()}),
        )


@dataclass(frozen=True)
class LiveClaimContext:
    """Caller-supplied identity of the exact live process being authorized."""

    subject_pid: int
    process_start_token: str
    supervisor_pid: int
    supervisor_start_token: str
    executable_realpath: str
    launch_argv_digest: str
    profile_realpath: str
    profile_digest: str
    checkout_identity_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_pid": self.subject_pid,
            "process_start_token": self.process_start_token,
            "supervisor_pid": self.supervisor_pid,
            "supervisor_start_token": self.supervisor_start_token,
            "executable_realpath": self.executable_realpath,
            "launch_argv_digest": self.launch_argv_digest,
            "profile_realpath": self.profile_realpath,
            "profile_digest": self.profile_digest,
            "checkout_identity_digest": self.checkout_identity_digest,
        }


@dataclass(frozen=True)
class _LiveClaimSnapshot:
    """One complete read of the live subject and its supervising process."""

    process_start_token: str
    supervisor_start_token: str
    parent_pid: int
    executable_realpath: str
    argv_digest: str


@dataclass(frozen=True)
class VerifiedClaim:
    """A capability-bindable result returned only after complete revalidation."""

    profile_id: str
    claim: str
    attestation_digest: str
    profile_subject_digest: str
    bindings: Mapping[str, str]
    evidence_digests: tuple[str, ...]
    checkout_binding: Mapping[str, str]
    launch_session: Mapping[str, Any] | None
    runtime_binding: Mapping[str, Any]
    worker_execution_binding: Mapping[str, Any] | None
    generation_id: str
    subject_binding_digest: str

    @property
    def configuration_digest(self) -> str:
        return self.bindings["configuration_digest"]

    @property
    def config_digest(self) -> str:
        """Short compatibility name for capability binding code."""

        return self.configuration_digest

    @property
    def subject_digest(self) -> str:
        return self.profile_subject_digest

    @property
    def runtime_binary_digest(self) -> str:
        return self.bindings["runtime_binary_digest"]

    @property
    def tool_inventory_digest(self) -> str:
        return self.bindings["tool_inventory_digest"]

    @property
    def checkout_identity_digest(self) -> str:
        return self.checkout_binding["identity_digest"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "claim": self.claim,
            "attestation_digest": self.attestation_digest,
            "profile_subject_digest": self.profile_subject_digest,
            "bindings": dict(self.bindings),
            "evidence_digests": list(self.evidence_digests),
            "checkout_binding": dict(self.checkout_binding),
            "checkout_identity_digest": self.checkout_identity_digest,
            "launch_session": (
                dict(self.launch_session) if self.launch_session is not None else None
            ),
            "runtime_binding": dict(self.runtime_binding),
            "worker_execution_binding": (
                dict(self.worker_execution_binding)
                if self.worker_execution_binding is not None
                else None
            ),
            "generation_id": self.generation_id,
            "subject_binding_digest": self.subject_binding_digest,
        }


@dataclass(frozen=True)
class _VerifiedAttestation:
    payload: Mapping[str, Any]
    digest: str
    evidence: tuple[Mapping[str, Any], ...]
    evidence_digests: tuple[str, ...]
    claim_results: Mapping[str, str]
    checkout_binding: Mapping[str, str]
    launch_session: Mapping[str, Any] | None
    runtime_binding: Mapping[str, Any]
    worker_execution_binding: Mapping[str, Any] | None
    generation_id: str


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _stable_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def argv_vector_digest(argv: Sequence[str]) -> str:
    """Digest argv without discarding element boundaries."""

    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise TypeError("argv_vector_required")
    normalized = list(argv)
    if any(not isinstance(item, str) for item in normalized):
        raise TypeError("argv_element_not_string")
    return _stable_digest(normalized)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssuranceContractError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _load_json_bytes(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except (UnicodeError, json.JSONDecodeError, AssuranceContractError) as exc:
        raise AssuranceContractError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise AssuranceContractError(f"{label}_root_not_object")
    return value


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AssuranceContractError(f"registry_unloadable:{path}") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise AssuranceContractError("registry_too_large")
    return _load_json_bytes(payload, label="registry")


def profile_subject_digest(profile: Mapping[str, Any]) -> str:
    """Digest only static target requirements, never observations."""

    return _stable_digest({key: profile[key] for key in sorted(PROFILE_FIELDS)})


# Compatibility alias retained for callers of the first assurance prototype.
assurance_subject_digest = profile_subject_digest


def _missing_fields(value: Mapping[str, Any], expected: set[str], path: str) -> list[str]:
    return [f"{path}.{key}:required" for key in sorted(expected - set(value))]


def _unexpected_fields(value: Mapping[str, Any], expected: set[str], path: str) -> list[str]:
    return [f"{path}.{key}:unexpected" for key in sorted(set(value) - expected)]


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256.fullmatch(value))


def _valid_positive_int(value: Any, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _profile_requirement_errors(profile: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    targets = profile.get("target_claims")
    operations = profile.get("operation_requirements")
    role = profile.get("surface_role")
    if not isinstance(targets, list) or not isinstance(operations, dict):
        return errors
    target_set = set(item for item in targets if isinstance(item, str))
    if "managed_worker" in target_set and target_set - {"managed_worker"}:
        errors.append(f"{path}.target_claims:managed_worker_is_independent_worker_surface")
    if target_set & {"ingress_enforced", "action_enforced"} and role != "main_agent":
        errors.append(f"{path}.surface_role:main_agent_required")
    if "managed_worker" in target_set and role != "bounded_worker":
        errors.append(f"{path}.surface_role:bounded_worker_required")
    if "ingress_enforced" in target_set:
        for operation in INGRESS_OPERATIONS:
            if operations.get(operation) != "saihai_gateway_only":
                errors.append(f"{path}.operation_requirements.{operation}:gateway_only_required")
    if "action_enforced" in target_set:
        for operation in ACTION_OPERATIONS:
            if operations.get(operation) != "denied":
                errors.append(f"{path}.operation_requirements.{operation}:denied_required")
    if "managed_worker" in target_set:
        if operations.get("surface_launch") != "saihai_gateway_only":
            errors.append(f"{path}.operation_requirements.surface_launch:gateway_only_required")
        for operation in ACTION_OPERATIONS:
            if operations.get(operation) not in {
                "capability_scoped",
                "saihai_gateway_only",
                "denied",
            }:
                errors.append(
                    f"{path}.operation_requirements.{operation}:worker_boundary_required"
                )
    return errors


def _tool_validator_material(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the declarative validator material covered by its digest.

    Validators are data, not adapter branches in the assurance core.  Adding a
    Claude, Cursor, or Grok adapter therefore adds one registry declaration;
    it does not add agent-specific Python to this module.
    """

    return {
        key: value[key]
        for key in sorted(TOOL_VALIDATOR_FIELDS - {"validator_digest"})
    }


def _tool_validator_errors(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{path}:object_or_null"]
    errors = _missing_fields(value, TOOL_VALIDATOR_FIELDS, path)
    errors.extend(_unexpected_fields(value, TOOL_VALIDATOR_FIELDS, path))
    for field in ("validator_id", "validator_version"):
        if not isinstance(value.get(field), str) or not SAFE_ID.fullmatch(value[field]):
            errors.append(f"{path}.{field}:safe_id")
    if value.get("validator_kind") != "declarative_tool_classification":
        errors.append(f"{path}.validator_kind:const_declarative_tool_classification")
    requirements = value.get("classification_requirements")
    if not isinstance(requirements, dict) or set(requirements) != set(TOOL_CATEGORY_NAMES):
        errors.append(f"{path}.classification_requirements:exact_categories")
    else:
        for category in TOOL_CATEGORY_NAMES:
            rule_path = f"{path}.classification_requirements.{category}"
            rule = requirements.get(category)
            if not isinstance(rule, dict) or set(rule) != TOOL_CATEGORY_RULE_FIELDS:
                errors.append(f"{rule_path}:exact_rule")
                continue
            mode = rule.get("mode")
            values = rule.get("values")
            alternatives = rule.get("alternatives")
            if mode not in {"exact", "subset", "one_of"}:
                errors.append(f"{rule_path}.mode:enum")
            if (
                not isinstance(values, list)
                or values != sorted(set(values))
                or any(not isinstance(item, str) or not item for item in values)
            ):
                errors.append(f"{rule_path}.values:sorted_unique_strings")
            if not isinstance(alternatives, list):
                errors.append(f"{rule_path}.alternatives:array")
            elif mode == "one_of":
                if (
                    not alternatives
                    or any(
                        not isinstance(option, list)
                        or option != sorted(set(option))
                        or any(not isinstance(item, str) or not item for item in option)
                        for option in alternatives
                    )
                ):
                    errors.append(f"{rule_path}.alternatives:nonempty_sorted_sets")
            elif alternatives:
                errors.append(f"{rule_path}.alternatives:empty_unless_one_of")
    manifest_tools = value.get("manifest_enabled_tools")
    if manifest_tools is not None and (
        not isinstance(manifest_tools, list)
        or manifest_tools != sorted(set(manifest_tools))
        or any(not isinstance(item, str) or not item for item in manifest_tools)
    ):
        errors.append(f"{path}.manifest_enabled_tools:sorted_unique_strings_or_null")
    operation_tools = value.get("operation_tool_names")
    if not isinstance(operation_tools, dict) or set(operation_tools) != set(ACTION_OPERATIONS):
        errors.append(f"{path}.operation_tool_names:exact_action_operations")
    else:
        for operation in ACTION_OPERATIONS:
            names = operation_tools.get(operation)
            if (
                not isinstance(names, list)
                or names != sorted(set(names))
                or any(not isinstance(item, str) or not item for item in names)
            ):
                errors.append(f"{path}.operation_tool_names.{operation}:sorted_unique_strings")
    digest = value.get("validator_digest")
    if not _valid_digest(digest):
        errors.append(f"{path}.validator_digest:sha256")
    elif not errors and not hmac.compare_digest(
        digest, _stable_digest(_tool_validator_material(value))
    ):
        errors.append(f"{path}.validator_digest:mismatch")
    return errors


def _launch_validator_material(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in sorted(LAUNCH_VALIDATOR_FIELDS - {"validator_digest"})
    }


def _module_validator_digest(module_name: str) -> str:
    """Hash the exact imported adapter source with a stable no-follow read.

    Declarative tool validators bind their canonical registry payload.  Python
    adapters are different: their authority is executable code, so the
    registry digest binds the complete module bytes addressed by
    ``module.__file__``.
    """

    try:
        module = importlib.import_module(module_name)
        source_text = getattr(module, "__file__", None)
        if not isinstance(source_text, str) or not source_text:
            raise AssuranceGateError("validator_module_path_invalid")
        source = Path(source_text)
        before_path = source.lstat()
        resolved = source.resolve(strict=True)
        if (
            source != resolved
            or stat.S_ISLNK(before_path.st_mode)
            or not stat.S_ISREG(before_path.st_mode)
        ):
            raise AssuranceGateError("validator_module_not_canonical_regular")
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (ImportError, OSError, TypeError) as exc:
        raise AssuranceGateError("validator_module_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_EXTERNAL_ARTIFACT_BYTES:
            raise AssuranceGateError("validator_module_invalid")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AssuranceGateError("validator_module_changed_during_read")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _launch_validator_errors(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{path}:object_or_null"]
    errors = _missing_fields(value, LAUNCH_VALIDATOR_FIELDS, path)
    errors.extend(_unexpected_fields(value, LAUNCH_VALIDATOR_FIELDS, path))
    for field in ("validator_id", "validator_version"):
        if not isinstance(value.get(field), str) or not SAFE_ID.fullmatch(value[field]):
            errors.append(f"{path}.{field}:safe_id")
    if not isinstance(value.get("module"), str) or not SAFE_MODULE.fullmatch(value["module"]):
        errors.append(f"{path}.module:safe_module_identifier")
    for field in ("function", "fields_constant"):
        if not isinstance(value.get(field), str) or not SAFE_SYMBOL.fullmatch(value[field]):
            errors.append(f"{path}.{field}:safe_symbol")
    digest = value.get("validator_digest")
    if not _valid_digest(digest):
        errors.append(f"{path}.validator_digest:sha256")
    elif not errors:
        try:
            actual_digest = _module_validator_digest(value["module"])
        except AssuranceGateError:
            errors.append(f"{path}.validator_digest:module_unavailable")
        else:
            if not hmac.compare_digest(digest, actual_digest):
                errors.append(f"{path}.validator_digest:module_mismatch")
    return errors


def _execution_binding_validator_errors(value: Any, path: str) -> list[str]:
    """Validate a registry-declared execution binding adapter.

    This deliberately has the same immutable module/function/field contract as
    launch validators.  Agent families add registry data and an adapter module;
    the assurance evaluator contains no family-specific branch.
    """

    return _launch_validator_errors(value, path)


def _validate_execution_binding(
    value: Any,
    *,
    profile: Mapping[str, Any],
) -> dict[str, Any] | None:
    validator = profile.get("execution_binding_validator")
    if validator is None:
        if value is not None:
            raise AssuranceGateError("unexpected_worker_execution_binding")
        return None
    errors = _execution_binding_validator_errors(
        validator, "$.execution_binding_validator"
    )
    if errors:
        raise AssuranceGateError("execution_binding_validator_invalid", errors)
    assert isinstance(validator, Mapping)
    try:
        module = importlib.import_module(str(validator["module"]))
        verifier = getattr(module, str(validator["function"]))
        declared_fields = getattr(module, str(validator["fields_constant"]))
    except (ImportError, AttributeError, TypeError) as exc:
        raise AssuranceGateError("execution_binding_validator_unavailable") from exc
    if (
        not callable(verifier)
        or not isinstance(declared_fields, (set, frozenset))
        or not isinstance(value, Mapping)
        or set(value) != set(declared_fields)
    ):
        raise AssuranceGateError("worker_execution_binding_fields_invalid")
    try:
        normalized = verifier(dict(value))
    except Exception as exc:
        raise AssuranceGateError("worker_execution_binding_invalid") from exc
    if not isinstance(normalized, Mapping) or set(normalized) != set(declared_fields):
        raise AssuranceGateError("worker_execution_binding_validator_result_invalid")
    return json.loads(json.dumps(dict(normalized)))


def validate_registry(value: Any) -> list[str]:
    """Return deterministic structural/semantic errors for a target registry."""

    if not isinstance(value, dict):
        return ["$:type_object"]
    root_fields = {"contract_version", "failure_policy", "profiles"}
    errors = _missing_fields(value, root_fields, "$")
    errors.extend(_unexpected_fields(value, root_fields, "$"))
    if value.get("contract_version") != "2":
        errors.append("$.contract_version:const_2")
    policy = value.get("failure_policy")
    if not isinstance(policy, dict):
        errors.append("$.failure_policy:type_object")
    else:
        errors.extend(_missing_fields(policy, set(FAILURE_POLICY), "$.failure_policy"))
        errors.extend(_unexpected_fields(policy, set(FAILURE_POLICY), "$.failure_policy"))
        for key, expected in FAILURE_POLICY.items():
            if policy.get(key) != expected:
                errors.append(f"$.failure_policy.{key}:must_be_{expected}")
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        errors.append("$.profiles:nonempty_array")
        return sorted(set(errors))
    profile_ids: set[str] = set()
    for index, profile in enumerate(profiles):
        path = f"$.profiles[{index}]"
        if not isinstance(profile, dict):
            errors.append(f"{path}:type_object")
            continue
        errors.extend(_missing_fields(profile, PROFILE_FIELDS, path))
        errors.extend(_unexpected_fields(profile, PROFILE_FIELDS, path))
        if profile.get("profile_version") != "2":
            errors.append(f"{path}.profile_version:const_2")
        profile_id = profile.get("profile_id")
        if not isinstance(profile_id, str) or not SAFE_ID.fullmatch(profile_id):
            errors.append(f"{path}.profile_id:safe_id")
        elif profile_id in profile_ids:
            errors.append(f"{path}.profile_id:duplicate")
        else:
            profile_ids.add(profile_id)
        family = profile.get("agent_family")
        if not isinstance(family, str) or not SAFE_FAMILY.fullmatch(family):
            errors.append(f"{path}.agent_family:safe_id")
        if profile.get("surface_role") not in SURFACE_ROLES:
            errors.append(f"{path}.surface_role:enum")
        if profile.get("integration_state") not in INTEGRATION_STATES:
            errors.append(f"{path}.integration_state:enum")
        transports = profile.get("transports")
        if (
            not isinstance(transports, list)
            or not transports
            or any(not isinstance(item, str) or item not in TRANSPORTS for item in transports)
            or len(transports) != len(set(transports))
        ):
            errors.append(f"{path}.transports:nonempty_unique_enum")
        targets = profile.get("target_claims")
        if (
            not isinstance(targets, list)
            or any(not isinstance(item, str) or item not in CLAIMS for item in targets)
            or len(targets) != len(set(targets))
        ):
            errors.append(f"{path}.target_claims:unique_enum_array")
        operations = profile.get("operation_requirements")
        if not isinstance(operations, dict):
            errors.append(f"{path}.operation_requirements:type_object")
        else:
            errors.extend(_missing_fields(operations, set(OPERATIONS), f"{path}.operation_requirements"))
            errors.extend(_unexpected_fields(operations, set(OPERATIONS), f"{path}.operation_requirements"))
            for operation in OPERATIONS:
                if operations.get(operation) not in OPERATION_COVERAGE:
                    errors.append(f"{path}.operation_requirements.{operation}:enum")
        configuration_requirements = profile.get("configuration_requirements")
        configuration_requirement_fields = {
            "deployment_manifest_path",
            "required_artifact_roles",
        }
        if not isinstance(configuration_requirements, dict):
            errors.append(f"{path}.configuration_requirements:type_object")
        else:
            errors.extend(
                _missing_fields(
                    configuration_requirements,
                    configuration_requirement_fields,
                    f"{path}.configuration_requirements",
                )
            )
            errors.extend(
                _unexpected_fields(
                    configuration_requirements,
                    configuration_requirement_fields,
                    f"{path}.configuration_requirements",
                )
            )
            manifest_path = configuration_requirements.get("deployment_manifest_path")
            if manifest_path is not None:
                if (
                    not isinstance(manifest_path, str)
                    or not manifest_path.startswith("/")
                    or "\x00" in manifest_path
                    or any(part in {".", ".."} for part in Path(manifest_path).parts)
                ):
                    errors.append(
                        f"{path}.configuration_requirements.deployment_manifest_path:canonical_absolute_or_null"
                    )
            roles = configuration_requirements.get("required_artifact_roles")
            if (
                not isinstance(roles, list)
                or len(roles) != len(set(item for item in roles if isinstance(item, str)))
                or any(item not in CONFIGURATION_ARTIFACT_ROLES for item in roles)
            ):
                errors.append(
                    f"{path}.configuration_requirements.required_artifact_roles:unique_enum_array"
                )
            elif manifest_path is None and roles:
                errors.append(
                    f"{path}.configuration_requirements.required_artifact_roles:manifest_required"
                )
            elif manifest_path is not None and set(roles) != CONFIGURATION_ARTIFACT_ROLES:
                errors.append(
                    f"{path}.configuration_requirements.required_artifact_roles:complete_frontend_bundle_required"
                )
        evidence_policy = profile.get("evidence_policy")
        policy_fields = {"max_age_seconds", "future_skew_seconds", "required_bindings"}
        if not isinstance(evidence_policy, dict):
            errors.append(f"{path}.evidence_policy:type_object")
        else:
            errors.extend(_missing_fields(evidence_policy, policy_fields, f"{path}.evidence_policy"))
            errors.extend(_unexpected_fields(evidence_policy, policy_fields, f"{path}.evidence_policy"))
            if not _valid_positive_int(evidence_policy.get("max_age_seconds"), 60, 86400):
                errors.append(f"{path}.evidence_policy.max_age_seconds:range")
            skew = evidence_policy.get("future_skew_seconds")
            if not isinstance(skew, int) or isinstance(skew, bool) or not 0 <= skew <= 300:
                errors.append(f"{path}.evidence_policy.future_skew_seconds:range")
            if evidence_policy.get("required_bindings") != list(BINDING_FIELDS):
                errors.append(f"{path}.evidence_policy.required_bindings:exact_ordered_set")
        errors.extend(_tool_validator_errors(profile.get("tool_validator"), f"{path}.tool_validator"))
        if profile.get("integration_state") == "configured" and profile.get("tool_validator") is None:
            errors.append(f"{path}.tool_validator:required_for_configured_profile")
        errors.extend(
            _launch_validator_errors(
                profile.get("launch_validator"), f"{path}.launch_validator"
            )
        )
        if (
            profile.get("integration_state") == "configured"
            and profile.get("surface_role") == "main_agent"
            and profile.get("launch_validator") is None
        ):
            errors.append(f"{path}.launch_validator:required_for_configured_main_agent")
        errors.extend(
            _execution_binding_validator_errors(
                profile.get("execution_binding_validator"),
                f"{path}.execution_binding_validator",
            )
        )
        if (
            profile.get("integration_state") == "configured"
            and profile.get("surface_role") == "bounded_worker"
            and profile.get("execution_binding_validator") is None
        ):
            errors.append(
                f"{path}.execution_binding_validator:required_for_configured_worker"
            )
        if (
            profile.get("surface_role") != "bounded_worker"
            and profile.get("execution_binding_validator") is not None
        ):
            errors.append(
                f"{path}.execution_binding_validator:worker_only"
            )
        rationale = profile.get("rationale")
        if not isinstance(rationale, str) or not 1 <= len(rationale) <= 2048:
            errors.append(f"{path}.rationale:bounded_nonempty")
        errors.extend(_profile_requirement_errors(profile, path))
    return sorted(set(errors))


def _normalize_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or any(part in {".", ".."} for part in expanded.parts):
        raise EvidenceTrustError("trust_path_not_canonical_absolute")
    return expanded


def assurance_root_from_state_root(state_root: Path | str) -> Path:
    root = _normalize_absolute(Path(state_root))
    if root == PRODUCTION_ASSURANCE_ROOT or root.name in {ASSURANCE_NAMESPACE, "Assurance"}:
        return root
    return root / ASSURANCE_NAMESPACE


def production_assurance_root() -> Path:
    """Return the immutable production namespace; no environment override."""

    return PRODUCTION_ASSURANCE_ROOT


def _validate_deployment_epoch(
    value: Any,
    *,
    profile_id: str,
) -> dict[str, Any]:
    """Validate the exact fail-closed deployment epoch record shape."""

    if not isinstance(value, Mapping) or set(value) != DEPLOYMENT_EPOCH_FIELDS:
        raise AssuranceGateError("deployment_epoch_fields_invalid")
    epoch = dict(value)
    if epoch.get("epoch_version") != "1":
        raise AssuranceGateError("deployment_epoch_version_invalid")
    for field in ("profile_id", "epoch_id", "transaction_id"):
        item = epoch.get(field)
        if not isinstance(item, str) or not SAFE_ID.fullmatch(item):
            raise AssuranceGateError(f"deployment_epoch_{field}_invalid")
    if epoch["profile_id"] != profile_id:
        raise AssuranceGateError("deployment_epoch_profile_mismatch")
    state = epoch.get("state")
    operation = epoch.get("operation")
    if state not in DEPLOYMENT_EPOCH_STATES:
        raise AssuranceGateError("deployment_epoch_state_invalid")
    if operation not in DEPLOYMENT_EPOCH_OPERATIONS:
        raise AssuranceGateError("deployment_epoch_operation_invalid")
    previous = epoch.get("previous_epoch_id")
    if previous is not None and (
        not isinstance(previous, str)
        or not SAFE_ID.fullmatch(previous)
        or previous == epoch["epoch_id"]
    ):
        raise AssuranceGateError("deployment_epoch_previous_invalid")
    try:
        rotated_at = _parse_timestamp(
            epoch.get("rotated_at"), label="deployment_epoch_rotated_at"
        )
    except AssuranceContractError as exc:
        raise AssuranceGateError("deployment_epoch_rotated_at_invalid") from exc
    finalized = epoch.get("finalized_at")
    if state == "transitioning":
        if finalized is not None:
            raise AssuranceGateError("deployment_epoch_finalized_at_invalid")
    else:
        try:
            finalized_at = _parse_timestamp(
                finalized, label="deployment_epoch_finalized_at"
            )
        except AssuranceContractError as exc:
            raise AssuranceGateError("deployment_epoch_finalized_at_invalid") from exc
        if finalized_at < rotated_at:
            raise AssuranceGateError("deployment_epoch_time_order_invalid")
    expected_state_by_operation = {
        "activate": "active_uncommissioned",
        "rollback": "restored_uncommissioned",
        "uninstall": "uninstalled",
    }
    if state != "transitioning" and state != expected_state_by_operation[operation]:
        raise AssuranceGateError("deployment_epoch_operation_state_mismatch")
    return epoch


def _load_deployment_epoch(
    assurance_root: Path,
    profile_id: str,
    *,
    policy: TrustPolicy,
) -> dict[str, Any]:
    reference = f"epochs/{profile_id}.json"
    try:
        raw = secure_read_relative(assurance_root, reference, policy=policy)
        value = _load_json_bytes(raw, label="deployment_epoch")
    except EvidenceTrustError as exc:
        reason = (
            "deployment_epoch_missing"
            if exc.reason == "trusted_artifact_missing"
            else "deployment_epoch_untrusted"
        )
        raise AssuranceGateError(reason) from exc
    except AssuranceContractError as exc:
        raise AssuranceGateError("deployment_epoch_invalid_json") from exc
    return _validate_deployment_epoch(value, profile_id=profile_id)


def _require_commissionable_deployment_epoch(
    assurance_root: Path,
    profile_id: str,
    *,
    policy: TrustPolicy,
) -> dict[str, Any]:
    epoch = _load_deployment_epoch(
        assurance_root,
        profile_id,
        policy=policy,
    )
    if epoch["state"] not in COMMISSIONABLE_DEPLOYMENT_EPOCH_STATES:
        raise AssuranceGateError("deployment_epoch_not_commissionable")
    return epoch


def _check_owner_mode(metadata: os.stat_result, *, directory: bool, policy: TrustPolicy) -> None:
    if metadata.st_uid not in policy.expected_owner_uids:
        raise EvidenceTrustError("trusted_path_owner_mismatch")
    if policy.expected_group_gids is not None and metadata.st_gid not in policy.expected_group_gids:
        raise EvidenceTrustError("trusted_path_group_mismatch")
    mode = stat.S_IMODE(metadata.st_mode)
    if policy.reject_group_write and mode & stat.S_IWGRP:
        raise EvidenceTrustError("trusted_path_group_writable")
    if policy.reject_other_write and mode & stat.S_IWOTH:
        raise EvidenceTrustError("trusted_path_world_writable")
    if directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceTrustError("trusted_path_not_directory")
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceTrustError("trusted_artifact_not_regular")
        if mode & 0o111:
            raise EvidenceTrustError("trusted_artifact_executable")


def _production_namespace_mode(
    parts: Sequence[str], *, directory: bool
) -> int | None:
    """Return the exact production mode for a namespaced artifact."""

    if not parts:
        return None
    namespace = parts[0]
    if namespace == "commissioning":
        return 0o700 if directory else 0o600
    if namespace in {
        "launch-sessions",
        "commissioning-launches",
        "generations",
        "active",
        "epochs",
    }:
        if not directory and parts[-1].endswith(".lock"):
            return 0o600
        return 0o755 if directory else 0o644
    return None


def _check_production_namespace_mode(
    metadata: os.stat_result,
    parts: Sequence[str],
    *,
    directory: bool,
    policy: TrustPolicy,
) -> None:
    if policy != TrustPolicy.production():
        return
    expected = _production_namespace_mode(parts, directory=directory)
    if expected is not None and stat.S_IMODE(metadata.st_mode) != expected:
        raise EvidenceTrustError("trusted_path_mode_mismatch")


def _validate_root_chain(root: Path, policy: TrustPolicy) -> None:
    root = _normalize_absolute(root)
    anchor = _normalize_absolute(policy.trusted_ancestor)
    try:
        relative = root.relative_to(anchor)
    except ValueError as exc:
        raise EvidenceTrustError("assurance_root_outside_trust_anchor") from exc
    current = anchor
    chain = [anchor]
    for part in relative.parts:
        current = current / part
        chain.append(current)
    for candidate in chain:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as exc:
            raise EvidenceTrustError("trusted_path_missing") from exc
        except OSError as exc:
            raise EvidenceTrustError("trusted_path_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise EvidenceTrustError("trusted_path_symlink")
        _check_owner_mode(metadata, directory=True, policy=policy)


def _relative_parts(reference: str) -> tuple[str, ...]:
    if not isinstance(reference, str) or not 1 <= len(reference) <= 512:
        raise EvidenceTrustError("artifact_reference_invalid")
    if "\\" in reference or "\x00" in reference:
        raise EvidenceTrustError("artifact_reference_invalid")
    pure = PurePosixPath(reference)
    if pure.is_absolute() or not pure.parts:
        raise EvidenceTrustError("artifact_reference_not_relative")
    for part in pure.parts:
        if part in {"", ".", ".."} or not SAFE_PATH_COMPONENT.fullmatch(part):
            raise EvidenceTrustError("artifact_reference_invalid_component")
    return tuple(pure.parts)


def secure_read_relative(
    assurance_root: Path,
    reference: str,
    *,
    policy: TrustPolicy,
) -> bytes:
    """Read one root-relative regular file with descriptor-based no-follow checks."""

    root = _normalize_absolute(assurance_root)
    _validate_root_chain(root, policy)
    parts = _relative_parts(reference)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        root_fd = os.open(root, os.O_RDONLY | directory_flag | nofollow)
        descriptors.append(root_fd)
        _check_owner_mode(os.fstat(root_fd), directory=True, policy=policy)
        parent_fd = root_fd
        traversed: list[str] = []
        for part in parts[:-1]:
            child_fd = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow,
                dir_fd=parent_fd,
            )
            descriptors.append(child_fd)
            child_metadata = os.fstat(child_fd)
            _check_owner_mode(child_metadata, directory=True, policy=policy)
            traversed.append(part)
            _check_production_namespace_mode(
                child_metadata, traversed, directory=True, policy=policy
            )
            parent_fd = child_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        _check_owner_mode(before, directory=False, policy=policy)
        _check_production_namespace_mode(
            before, parts, directory=False, policy=policy
        )
        if before.st_size > policy.max_json_bytes:
            raise EvidenceTrustError("trusted_artifact_too_large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(65536, policy.max_json_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > policy.max_json_bytes:
                raise EvidenceTrustError("trusted_artifact_too_large")
        after = os.fstat(file_fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise EvidenceTrustError("trusted_artifact_changed_during_read")
        return b"".join(chunks)
    except FileNotFoundError as exc:
        raise EvidenceTrustError("trusted_artifact_missing") from exc
    except EvidenceTrustError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise EvidenceTrustError("trusted_artifact_symlink") from exc
        raise EvidenceTrustError("trusted_artifact_unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parse_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 40 or not value.endswith("Z"):
        raise AssuranceContractError(f"{label}_invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise AssuranceContractError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise AssuranceContractError(f"{label}_invalid")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _validate_bindings(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(BINDING_FIELDS):
        raise AssuranceContractError(f"{label}_invalid_fields")
    normalized: dict[str, str] = {}
    for field in BINDING_FIELDS:
        digest = value.get(field)
        if not _valid_digest(digest):
            raise AssuranceContractError(f"{label}_{field}_invalid")
        normalized[field] = digest
    return normalized


def _validate_argv(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 256
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 4096
            or "\x00" in item
            or "\n" in item
            or "\r" in item
            for item in value
        )
    ):
        raise AssuranceContractError(f"{label}_invalid")
    return list(value)


def _launch_session_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssuranceContractError("launch_session_not_object")
    reference = value.get("record_reference")
    digest = value.get("record_digest")
    if (
        not isinstance(reference, str)
        or not reference.startswith("launch-sessions/")
        or not reference.endswith(".json")
        or not _valid_digest(digest)
    ):
        raise AssuranceContractError("launch_session_envelope_invalid")
    return dict(value)


def _validate_launch_session_shape(
    value: Any, profile: Mapping[str, Any]
) -> dict[str, Any] | None:
    validator = profile.get("launch_validator")
    if validator is None:
        if value is not None:
            raise AssuranceContractError("launch_session_unexpected")
        return None
    errors = _launch_validator_errors(validator, "$.launch_validator")
    if errors:
        raise AssuranceContractError("launch_validator_invalid")
    envelope = _launch_session_envelope(value)
    try:
        module = importlib.import_module(validator["module"])
        fields = getattr(module, validator["fields_constant"])
        function = getattr(module, validator["function"])
    except (ImportError, AttributeError, TypeError) as exc:
        raise AssuranceContractError("launch_validator_unavailable") from exc
    if not isinstance(fields, (set, frozenset)) or set(envelope) != set(fields):
        raise AssuranceContractError("launch_session_fields_invalid")
    try:
        normalized = function(envelope)
    except Exception as exc:
        raise AssuranceContractError("launch_session_invalid") from exc
    if not isinstance(normalized, Mapping) or dict(normalized) != envelope:
        raise AssuranceContractError("launch_validator_result_invalid")
    return dict(normalized)


def _launch_session_digest(value: Mapping[str, Any]) -> str:
    normalized = _launch_session_envelope(value)
    return str(normalized["record_digest"])


def _validate_runtime_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != RUNTIME_BINDING_FIELDS:
        raise AssuranceContractError("runtime_binding_fields_invalid")
    runtime_realpath = value.get("runtime_realpath")
    profile_realpath = value.get("profile_realpath")
    if (
        not isinstance(runtime_realpath, str)
        or not Path(runtime_realpath).is_absolute()
        or not isinstance(profile_realpath, str)
        or not Path(profile_realpath).is_absolute()
    ):
        raise AssuranceContractError("runtime_binding_path_invalid")
    for field in ("runtime_digest", "argv_digest", "profile_digest"):
        if not _valid_digest(value.get(field)):
            raise AssuranceContractError(f"runtime_binding_{field}_invalid")
    argv = _validate_argv(value.get("argv"), label="runtime_binding_argv")
    if not hmac.compare_digest(argv_vector_digest(argv), str(value["argv_digest"])):
        raise AssuranceContractError("runtime_binding_argv_digest_mismatch")
    return {
        "runtime_realpath": runtime_realpath,
        "runtime_digest": value["runtime_digest"],
        "argv": argv,
        "argv_digest": value["argv_digest"],
        "profile_realpath": profile_realpath,
        "profile_digest": value["profile_digest"],
    }


def _live_process_start_token(pid: int) -> str:
    try:
        import run_lock

        token = run_lock.process_start_token(pid)
    except Exception as exc:
        raise AssuranceGateError("claim_live_start_token_unavailable") from exc
    if not isinstance(token, str) or not token:
        raise AssuranceGateError("claim_live_start_token_unavailable")
    return token


def _live_parent_pid(pid: int) -> int:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            timeout=1,
            check=False,
        )
        parent = int(completed.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise AssuranceGateError("claim_live_parent_unavailable") from exc
    if completed.returncode or parent <= 0:
        raise AssuranceGateError("claim_live_parent_unavailable")
    return parent


def _live_process_executable(pid: int) -> Path:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise AssuranceGateError("claim_live_executable_unavailable")
    proc_link = Path(f"/proc/{pid}/exe")
    if proc_link.exists():
        try:
            return proc_link.resolve(strict=True)
        except OSError as exc:
            raise AssuranceGateError("claim_live_executable_unavailable") from exc
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buffer = ctypes.create_string_buffer(4096)
        length = library.proc_pidpath(pid, buffer, len(buffer))
    except (OSError, AttributeError) as exc:
        raise AssuranceGateError("claim_live_executable_unavailable") from exc
    if length <= 0:
        raise AssuranceGateError("claim_live_executable_unavailable")
    try:
        return Path(os.fsdecode(buffer.value)).resolve(strict=True)
    except OSError as exc:
        raise AssuranceGateError("claim_live_executable_unavailable") from exc


def _parse_darwin_procargs2(raw: bytes) -> tuple[str, ...]:
    """Parse one KERN_PROCARGS2 response without guessing missing fields."""

    if not isinstance(raw, bytes) or len(raw) < 5:
        raise AssuranceGateError("claim_live_argv_malformed")
    argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    if argc < 1 or argc > DARWIN_MAX_ARGC:
        raise AssuranceGateError("claim_live_argv_malformed")

    executable_end = raw.find(b"\0", 4)
    if executable_end <= 4:
        raise AssuranceGateError("claim_live_argv_malformed")
    offset = executable_end + 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    if offset >= len(raw):
        raise AssuranceGateError("claim_live_argv_truncated")

    encoded_argv: list[bytes] = []
    for _index in range(argc):
        end = raw.find(b"\0", offset)
        if end < 0:
            raise AssuranceGateError("claim_live_argv_truncated")
        encoded_argv.append(raw[offset:end])
        offset = end + 1
    try:
        return tuple(item.decode("utf-8", errors="strict") for item in encoded_argv)
    except UnicodeError as exc:
        raise AssuranceGateError("claim_live_argv_undecodable") from exc


def _darwin_kern_argmax(library: Any) -> int:
    value = ctypes.c_int32()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    ctypes.set_errno(0)
    try:
        result = library.sysctlbyname(
            b"kern.argmax",
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            0,
        )
    except (AttributeError, OSError, TypeError) as exc:
        raise AssuranceGateError("claim_live_argv_unavailable") from exc
    if (
        result != 0
        or size.value != ctypes.sizeof(value)
        or value.value < DARWIN_ARGMAX_MIN
        or value.value > DARWIN_ARGMAX_MAX
    ):
        raise AssuranceGateError("claim_live_argv_unavailable")
    return value.value


def _darwin_process_argv(pid: int) -> tuple[str, ...]:
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise AssuranceGateError("claim_live_argv_unavailable") from exc

    for _attempt in range(DARWIN_PROCARGS_RETRIES):
        argmax = _darwin_kern_argmax(library)
        buffer = ctypes.create_string_buffer(argmax)
        size = ctypes.c_size_t(argmax)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        ctypes.set_errno(0)
        try:
            result = library.sysctl(
                mib,
                3,
                buffer,
                ctypes.byref(size),
                None,
                0,
            )
        except (AttributeError, OSError, TypeError) as exc:
            raise AssuranceGateError("claim_live_argv_unavailable") from exc
        if result == 0:
            if size.value > argmax:
                raise AssuranceGateError("claim_live_argv_malformed")
            return _parse_darwin_procargs2(bytes(buffer.raw[: size.value]))
        if ctypes.get_errno() != errno.ENOMEM:
            raise AssuranceGateError("claim_live_argv_unavailable")
    raise AssuranceGateError("claim_live_argv_unavailable")


def _parse_linux_cmdline(raw: bytes) -> tuple[str, ...]:
    if not isinstance(raw, bytes) or not raw:
        raise AssuranceGateError("claim_live_argv_unavailable")
    if len(raw) > LINUX_CMDLINE_MAX_BYTES:
        raise AssuranceGateError("claim_live_argv_unavailable")
    if not raw.endswith(b"\0"):
        raise AssuranceGateError("claim_live_argv_malformed")
    encoded_argv = raw[:-1].split(b"\0")
    if not encoded_argv or not encoded_argv[0]:
        raise AssuranceGateError("claim_live_argv_malformed")
    try:
        return tuple(item.decode("utf-8", errors="strict") for item in encoded_argv)
    except UnicodeError as exc:
        raise AssuranceGateError("claim_live_argv_undecodable") from exc


def _linux_process_argv(pid: int) -> tuple[str, ...]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(f"/proc/{pid}/cmdline", flags)
        try:
            raw = os.read(fd, LINUX_CMDLINE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AssuranceGateError("claim_live_argv_unavailable") from exc
    return _parse_linux_cmdline(raw)


def _live_process_argv(pid: int) -> tuple[str, ...]:
    """Read an exact live argv vector using the platform kernel interface."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise AssuranceGateError("claim_live_argv_unavailable")
    if sys.platform == "darwin":
        return _darwin_process_argv(pid)
    if sys.platform == "linux":
        return _linux_process_argv(pid)
    raise AssuranceGateError("claim_live_argv_platform_unsupported")


def _capture_live_claim_snapshot(
    live_context: LiveClaimContext,
) -> _LiveClaimSnapshot:
    """Read every mutable OS identity field used by the authority gate."""

    if not isinstance(live_context, LiveClaimContext):
        raise AssuranceGateError("live_claim_context_invalid")
    try:
        process_start_token = _live_process_start_token(live_context.subject_pid)
        supervisor_start_token = _live_process_start_token(
            live_context.supervisor_pid
        )
        parent_pid = _live_parent_pid(live_context.subject_pid)
        executable_realpath = str(
            _live_process_executable(live_context.subject_pid)
        )
        live_argv = _live_process_argv(live_context.subject_pid)
    except AssuranceGateError:
        raise
    except Exception as exc:
        raise AssuranceGateError("claim_live_identity_unavailable") from exc
    return _LiveClaimSnapshot(
        process_start_token=process_start_token,
        supervisor_start_token=supervisor_start_token,
        parent_pid=parent_pid,
        executable_realpath=executable_realpath,
        argv_digest=argv_vector_digest(live_argv),
    )


def _verify_launch_session(
    value: Any,
    *,
    assurance_root: Path,
    profile: Mapping[str, Any],
    bindings: Mapping[str, str],
    policy: TrustPolicy,
    now: datetime,
) -> dict[str, Any] | None:
    try:
        session = _validate_launch_session_shape(value, profile)
    except AssuranceContractError as exc:
        raise AssuranceGateError("launch_session_invalid") from exc
    if session is None:
        return None
    reference = session["record_reference"]
    try:
        raw = secure_read_relative(assurance_root, reference, policy=policy)
        recorded = _load_json_bytes(raw, label="launch_session")
    except (EvidenceTrustError, AssuranceContractError) as exc:
        raise AssuranceGateError("launch_session_record_untrusted") from exc
    if recorded != session:
        raise AssuranceGateError("launch_session_record_mismatch")
    issued_at = _parse_timestamp(session["issued_at"], label="launch_session_issued_at")
    valid_until = _parse_timestamp(session["valid_until"], label="launch_session_valid_until")
    if valid_until <= issued_at or now < issued_at - timedelta(seconds=300) or now > valid_until:
        raise AssuranceGateError("launch_session_not_current")
    if (
        session["profile_id"] != profile["profile_id"]
        or session["native_digest"] != bindings["runtime_binary_digest"]
        or session["checkout_identity_digest"] != bindings["checkout_digest"]
    ):
        raise AssuranceGateError("launch_session_binding_mismatch")
    if not hmac.compare_digest(
        _hash_external_regular(session["native_realpath"], policy=policy),
        session["native_digest"],
    ) or not hmac.compare_digest(
        _hash_external_regular(session["profile_realpath"], policy=policy),
        session["profile_digest"],
    ):
        raise AssuranceGateError("launch_session_artifact_drift")
    if session.get("session_kind") == "commissioning":
        reference = session.get("commissioning_launch_reference")
        try:
            import codex_main_agent_supervisor as supervisor

            companion_raw = secure_read_relative(
                assurance_root, str(reference), policy=policy
            )
            companion = supervisor.validate_commissioning_launch_shape(
                _load_json_bytes(companion_raw, label="commissioning_launch")
            )
        except Exception as exc:
            raise AssuranceGateError(
                "commissioning_launch_record_untrusted"
            ) from exc
        live = companion.get("live_observation")
        if (
            companion.get("state") not in {"active", "consumed"}
            or companion.get("record_reference") != reference
            or companion.get("profile_id") != profile["profile_id"]
            or companion.get("session_id") != session["session_id"]
            or companion.get("binding_digest")
            != session.get("commissioning_launch_digest")
            or companion.get("launch_session_digest") != session["record_digest"]
            or companion.get("probe_argv_digest") != session["launch_argv_digest"]
            or not isinstance(live, Mapping)
            or companion.get("live_observation_digest")
            != _stable_digest(dict(live))
            or live.get("subject_pid") != session["subject_pid"]
            or live.get("process_start_token") != session["process_start_token"]
            or live.get("executable_realpath") != session["native_realpath"]
            or live.get("parent_pid") != session["supervisor_pid"]
            or live.get("supervisor_start_token")
            != session["supervisor_start_token"]
        ):
            raise AssuranceGateError("commissioning_launch_identity_mismatch")
        observed_live_at = _parse_timestamp(
            live.get("observed_at"), label="commissioning_live_observed_at"
        )
        if not issued_at <= observed_live_at <= valid_until:
            raise AssuranceGateError("commissioning_launch_observation_time_invalid")
        return session
    try:
        import run_lock

        live_subject = run_lock.process_start_token(session["subject_pid"])
        live_supervisor = run_lock.process_start_token(session["supervisor_pid"])
    except Exception as exc:
        raise AssuranceGateError("launch_session_liveness_unavailable") from exc
    if (
        not live_subject
        or not live_supervisor
        or live_subject != session["process_start_token"]
        or live_supervisor != session["supervisor_start_token"]
    ):
        raise AssuranceGateError("launch_session_process_not_live")
    return session


def validate_evidence_payload(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["$:type_object"]
    errors = _missing_fields(value, EVIDENCE_FIELDS, "$")
    errors.extend(_unexpected_fields(value, EVIDENCE_FIELDS, "$"))
    if value.get("evidence_version") != "2":
        errors.append("$.evidence_version:const_2")
    for field in ("evidence_id", "generation_id", "profile_id"):
        if not isinstance(value.get(field), str) or not SAFE_ID.fullmatch(value[field]):
            errors.append(f"$.{field}:safe_id")
    if value.get("claim") not in {"common", *CLAIMS}:
        errors.append("$.claim:enum")
    if value.get("evidence_type") not in EVIDENCE_TYPES:
        errors.append("$.evidence_type:enum")
    operation = value.get("operation")
    if operation is not None and operation not in OPERATIONS:
        errors.append("$.operation:enum_or_null")
    if value.get("result") not in {"pass", "fail"}:
        errors.append("$.result:enum")
    for field in ("observed_at", "valid_until"):
        try:
            _parse_timestamp(value.get(field), label=field)
        except AssuranceContractError:
            errors.append(f"$.{field}:date_time")
    if not _valid_digest(value.get("registry_subject_digest")):
        errors.append("$.registry_subject_digest:sha256")
    try:
        _validate_bindings(value.get("bindings"), label="bindings")
    except AssuranceContractError:
        errors.append("$.bindings:exact_sha256_map")
    if value.get("launch_session") is not None:
        try:
            _launch_session_envelope(value.get("launch_session"))
        except AssuranceContractError:
            errors.append("$.launch_session:typed_launch_session_or_null")
    worker_binding = value.get("worker_execution_binding")
    if worker_binding is not None and (
        not isinstance(worker_binding, dict)
        or not worker_binding
        or len(worker_binding) > 64
    ):
        errors.append("$.worker_execution_binding:bounded_object_or_null")
    if value.get("observed_digest") is not None and not _valid_digest(value.get("observed_digest")):
        errors.append("$.observed_digest:sha256_or_null")
    details = value.get("details")
    if not isinstance(details, dict) or len(details) > 32:
        errors.append("$.details:bounded_object")
    else:
        try:
            if len(json.dumps(details, sort_keys=True).encode("utf-8")) > 128 * 1024:
                errors.append("$.details:too_large")
        except (TypeError, ValueError):
            errors.append("$.details:json_value")
    evidence_type = value.get("evidence_type")
    claim = value.get("claim")
    if evidence_type in COMMON_EVIDENCE_TYPES:
        if claim != "common" or operation is not None:
            errors.append("$:common_evidence_shape")
    elif evidence_type == "ingress_operation":
        if claim != "ingress_enforced" or operation not in INGRESS_OPERATIONS:
            errors.append("$:ingress_evidence_shape")
    elif evidence_type in {"direct_action_denial", "capability_boundary"}:
        expected_claim = (
            "action_enforced" if evidence_type == "direct_action_denial" else "managed_worker"
        )
        if claim != expected_claim or operation not in ACTION_OPERATIONS:
            errors.append("$:action_evidence_shape")
    elif evidence_type == "gateway_positive_path":
        if claim != "action_enforced" or operation is not None:
            errors.append("$:gateway_evidence_shape")
    elif evidence_type == "worker_launch_binding":
        if claim != "managed_worker" or operation != "surface_launch":
            errors.append("$:worker_launch_evidence_shape")
    if evidence_type not in COMMON_EVIDENCE_TYPES and value.get("observed_digest") is not None:
        errors.append("$.observed_digest:must_be_null_for_operation_evidence")
    if evidence_type not in COMMON_EVIDENCE_TYPES:
        if not isinstance(details, dict) or set(details) != {"host_observation"}:
            errors.append("$.details:host_observation_required")
        else:
            observation_ref = details.get("host_observation")
            if not isinstance(observation_ref, dict) or set(observation_ref) != {
                "path",
                "sha256",
            }:
                errors.append("$.details.host_observation:exact_reference")
            else:
                try:
                    _relative_parts(observation_ref.get("path"))
                except EvidenceTrustError:
                    errors.append("$.details.host_observation.path:safe_relative")
                if not _valid_digest(observation_ref.get("sha256")):
                    errors.append("$.details.host_observation.sha256:sha256")
    return sorted(set(errors))


def validate_attestation_payload(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["$:type_object"]
    errors = _missing_fields(value, ATTESTATION_FIELDS, "$")
    errors.extend(_unexpected_fields(value, ATTESTATION_FIELDS, "$"))
    if value.get("attestation_version") != "2":
        errors.append("$.attestation_version:const_2")
    for field in ("attestation_id", "generation_id", "profile_id"):
        if not isinstance(value.get(field), str) or not SAFE_ID.fullmatch(value[field]):
            errors.append(f"$.{field}:safe_id")
    for field in ("issued_at", "valid_until"):
        try:
            _parse_timestamp(value.get(field), label=field)
        except AssuranceContractError:
            errors.append(f"$.{field}:date_time")
    if not _valid_digest(value.get("registry_subject_digest")):
        errors.append("$.registry_subject_digest:sha256")
    targets = value.get("target_claims")
    if (
        not isinstance(targets, list)
        or any(item not in CLAIMS for item in targets)
        or len(targets) != len(set(targets))
    ):
        errors.append("$.target_claims:unique_enum_array")
        targets = []
    results = value.get("claim_results")
    if not isinstance(results, dict) or set(results) != set(targets):
        errors.append("$.claim_results:exact_target_keys")
    elif any(item not in {"pass", "fail"} for item in results.values()):
        errors.append("$.claim_results:pass_fail")
    try:
        _validate_bindings(value.get("bindings"), label="bindings")
    except AssuranceContractError:
        errors.append("$.bindings:exact_sha256_map")
    if value.get("launch_session") is not None:
        try:
            _launch_session_envelope(value.get("launch_session"))
        except AssuranceContractError:
            errors.append("$.launch_session:typed_launch_session_or_null")
    try:
        _validate_runtime_binding(value.get("runtime_binding"))
    except AssuranceContractError:
        errors.append("$.runtime_binding:exact_runtime_binding")
    worker_binding = value.get("worker_execution_binding")
    if worker_binding is not None and (
        not isinstance(worker_binding, dict)
        or not worker_binding
        or len(worker_binding) > 64
    ):
        errors.append("$.worker_execution_binding:bounded_object_or_null")
    manifest = value.get("generation_manifest")
    if not isinstance(manifest, dict) or set(manifest) != {"reference", "sha256"}:
        errors.append("$.generation_manifest:exact_reference")
    else:
        try:
            _relative_parts(manifest.get("reference"))
        except EvidenceTrustError:
            errors.append("$.generation_manifest.reference:safe_relative")
        if not _valid_digest(manifest.get("sha256")):
            errors.append("$.generation_manifest.sha256:sha256")
    references = value.get("evidence")
    if not isinstance(references, list) or not references:
        errors.append("$.evidence:nonempty_array")
    else:
        seen: set[str] = set()
        for index, item in enumerate(references):
            path = f"$.evidence[{index}]"
            if not isinstance(item, dict) or set(item) != {"reference", "sha256"}:
                errors.append(f"{path}:exact_object")
                continue
            try:
                _relative_parts(item.get("reference"))
            except EvidenceTrustError:
                errors.append(f"{path}.reference:safe_relative")
            if not _valid_digest(item.get("sha256")):
                errors.append(f"{path}.sha256:sha256")
            reference = item.get("reference")
            if isinstance(reference, str):
                if reference in seen:
                    errors.append(f"{path}.reference:duplicate")
                seen.add(reference)
    return sorted(set(errors))


def _validate_time_window(
    *,
    observed_at: datetime,
    valid_until: datetime,
    now: datetime,
    max_age_seconds: int,
    future_skew_seconds: int,
    prefix: str,
) -> None:
    if valid_until <= observed_at:
        raise AssuranceGateError(f"{prefix}_invalid_time_window")
    if observed_at > now + timedelta(seconds=future_skew_seconds):
        raise AssuranceGateError(f"{prefix}_from_future")
    if now > valid_until:
        raise AssuranceGateError(f"{prefix}_expired")
    if now - observed_at > timedelta(seconds=max_age_seconds):
        raise AssuranceGateError(f"{prefix}_stale")


def _hash_external_regular(path_text: Any, *, policy: TrustPolicy) -> str:
    if not isinstance(path_text, str) or not path_text:
        raise AssuranceGateError("external_artifact_path_invalid")
    supplied = Path(path_text).expanduser()
    if not supplied.is_absolute():
        raise AssuranceGateError("external_artifact_path_not_absolute")
    try:
        metadata = supplied.lstat()
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise AssuranceGateError("external_artifact_unavailable") from exc
    if supplied != resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AssuranceGateError("external_artifact_not_canonical_regular")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(supplied, flags)
    except OSError as exc:
        raise AssuranceGateError("external_artifact_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AssuranceGateError("external_artifact_not_canonical_regular")
        if before.st_size > policy.max_external_artifact_bytes:
            raise AssuranceGateError("external_artifact_too_large")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AssuranceGateError("external_artifact_changed_during_read")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_configuration_evidence(
    value: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    policy: TrustPolicy,
) -> None:
    details = value["details"]
    artifacts = details.get("artifacts") if isinstance(details, dict) else None
    if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 32:
        raise AssuranceGateError("configuration_artifacts_invalid")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_roles: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"role", "path", "sha256"}:
            raise AssuranceGateError("configuration_artifact_invalid")
        role = item.get("role")
        path_text = item.get("path")
        digest = item.get("sha256")
        if (
            role not in CONFIGURATION_ARTIFACT_ROLES
            or role in seen_roles
            or not isinstance(path_text, str)
            or path_text in seen
            or not _valid_digest(digest)
        ):
            raise AssuranceGateError("configuration_artifact_invalid")
        seen_roles.add(role)
        seen.add(path_text)
        if not hmac.compare_digest(_hash_external_regular(path_text, policy=policy), digest):
            raise AssuranceGateError("configuration_drift")
        normalized.append({"role": role, "path": path_text, "sha256": digest})
    normalized.sort(key=lambda item: (item["role"], item["path"]))
    if not hmac.compare_digest(_stable_digest(normalized), value["observed_digest"]):
        raise AssuranceGateError("configuration_binding_mismatch")
    requirements = profile["configuration_requirements"]
    required_roles = set(requirements["required_artifact_roles"])
    if not required_roles.issubset(seen_roles):
        raise AssuranceGateError("required_configuration_artifact_missing")
    manifest_path = requirements["deployment_manifest_path"]
    if manifest_path is None:
        return
    by_role = {item["role"]: item for item in normalized}
    if by_role["deployment_manifest"]["path"] != manifest_path:
        raise AssuranceGateError("deployment_manifest_path_mismatch")
    try:
        manifest = _load_json_bytes(
            Path(manifest_path).read_bytes(),
            label="deployment_manifest",
        )
    except (OSError, AssuranceContractError) as exc:
        raise AssuranceGateError("deployment_manifest_invalid") from exc
    manifest_artifacts = manifest.get("artifacts")
    if not isinstance(manifest_artifacts, dict):
        raise AssuranceGateError("deployment_manifest_invalid")
    manifest_role_map = {
        "runtime_config": "runtime_config",
        "requirements": "requirements",
        "bridge_wrapper": "bridge_wrapper",
        "instructions": "instructions",
        "observer": "observer",
        "supervisor": "supervisor",
    }
    for role, manifest_key in manifest_role_map.items():
        artifact = manifest_artifacts.get(manifest_key)
        if (
            not isinstance(artifact, dict)
            or artifact.get("path") != by_role[role]["path"]
            or artifact.get("sha256") != by_role[role]["sha256"]
        ):
            raise AssuranceGateError("deployment_manifest_artifact_mismatch")
    # The production deployment verifier recursively checks the runtime bundle,
    # Python, owners, modes, and runtime config.  Fixture policies intentionally
    # skip that root-only verifier while still exercising all digest bindings.
    if (
        policy.trusted_ancestor == Path("/")
        and policy.expected_owner_uids == frozenset({0})
    ):
        try:
            import codex_main_agent_deployment as deployment  # lazy, no cycle

            deployment.verify_deployment(Path(manifest_path))
        except Exception as exc:
            raise AssuranceGateError("deployment_verification_failed") from exc


def _validate_runtime_binary_evidence(value: Mapping[str, Any], *, policy: TrustPolicy) -> None:
    details = value["details"]
    if not isinstance(details, dict) or set(details) != {"binary_realpath"}:
        raise AssuranceGateError("runtime_binary_details_invalid")
    current = _hash_external_regular(details.get("binary_realpath"), policy=policy)
    if not hmac.compare_digest(current, value["observed_digest"]):
        raise AssuranceGateError("runtime_binary_drift")


def _exact_mapping(value: Any, fields: set[str], reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AssuranceGateError(reason)
    return value


def _safe_observer_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise AssuranceGateError(f"{label}_invalid")
    return value


def _sorted_tool_names(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or len(value) > 256
        or value != sorted(set(value))
        or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in value)
    ):
        raise AssuranceGateError(f"{label}_invalid")
    return value


def _validate_observer_executable(
    path_text: Any,
    expected_digest: Any,
    *,
    policy: TrustPolicy,
    label: str,
) -> None:
    if not isinstance(path_text, str) or not _valid_digest(expected_digest):
        raise AssuranceGateError(f"{label}_identity_invalid")
    path = Path(path_text)
    try:
        _validate_root_chain(path.parent, policy)
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, EvidenceTrustError) as exc:
        raise AssuranceGateError(f"{label}_trust_invalid") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        path != resolved
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in policy.expected_owner_uids
        or (
            policy.expected_group_gids is not None
            and metadata.st_gid not in policy.expected_group_gids
        )
        or not mode & 0o111
        or (policy.reject_group_write and mode & stat.S_IWGRP)
        or (policy.reject_other_write and mode & stat.S_IWOTH)
    ):
        raise AssuranceGateError(f"{label}_trust_invalid")
    actual = _hash_external_regular(str(path), policy=policy)
    if not hmac.compare_digest(actual, str(expected_digest)):
        raise AssuranceGateError(f"{label}_digest_mismatch")


def _validate_tool_classification(
    classification_value: Any,
    *,
    inventory: list[str],
    profile: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    classification = _exact_mapping(
        classification_value,
        TOOL_INVENTORY_CLASSIFICATION_FIELDS,
        "tool_inventory_classification_fields_invalid",
    )
    if classification.get("inventory_version") != "1":
        raise AssuranceGateError("tool_inventory_classification_version_invalid")
    model_identifier = classification.get("model_identifier")
    if (
        not isinstance(model_identifier, str)
        or not 1 <= len(model_identifier) <= 256
        or any(character in model_identifier for character in ("\x00", "\n", "\r"))
    ):
        raise AssuranceGateError("tool_inventory_model_identifier_invalid")
    flattened: list[str] = []
    for name in TOOL_CATEGORY_NAMES:
        flattened.extend(_sorted_tool_names(classification.get(name), f"tool_inventory_{name}"))
    if len(flattened) != len(set(flattened)) or inventory != sorted(flattened):
        raise AssuranceGateError("tool_inventory_classification_mismatch")

    validator = profile.get("tool_validator")
    validator_errors = _tool_validator_errors(validator, "$.tool_validator")
    if validator_errors:
        raise AssuranceGateError("tool_inventory_validator_invalid", validator_errors)
    if not isinstance(validator, Mapping):
        raise AssuranceGateError("tool_inventory_validator_unavailable")
    requirements = validator["classification_requirements"]
    for name in TOOL_CATEGORY_NAMES:
        rule = requirements[name]
        actual = classification[name]
        mode = rule["mode"]
        allowed = rule["values"]
        if mode == "exact" and actual != allowed:
            raise AssuranceGateError(f"tool_inventory_{name}_exact_mismatch")
        if mode == "subset" and not set(actual).issubset(allowed):
            raise AssuranceGateError(f"tool_inventory_{name}_subset_mismatch")
        if mode == "one_of" and actual not in rule["alternatives"]:
            raise AssuranceGateError(f"tool_inventory_{name}_alternative_mismatch")
    expected_manifest_tools = validator["manifest_enabled_tools"]
    if expected_manifest_tools is not None:
        if manifest is None:
            raise AssuranceGateError("tool_inventory_manifest_required")
        manifest_tools = manifest.get("tool_contract")
        enabled = (
            manifest_tools.get("enabled_tools")
            if isinstance(manifest_tools, Mapping)
            else None
        )
        if not isinstance(enabled, list) or sorted(enabled) != expected_manifest_tools:
            raise AssuranceGateError("deployment_tool_contract_mismatch")
    return classification


def _load_tool_manifest(profile: Mapping[str, Any]) -> Mapping[str, Any] | None:
    requirements = profile.get("configuration_requirements")
    path_text = (
        requirements.get("deployment_manifest_path")
        if isinstance(requirements, Mapping)
        else None
    )
    if path_text is None:
        return None
    if not isinstance(path_text, str) or not path_text:
        raise AssuranceGateError("deployment_manifest_path_invalid")
    try:
        manifest = _load_json_bytes(Path(path_text).read_bytes(), label="deployment_manifest")
    except (OSError, AssuranceContractError) as exc:
        raise AssuranceGateError("deployment_manifest_invalid") from exc
    if not isinstance(manifest, Mapping):
        raise AssuranceGateError("deployment_manifest_invalid")
    return manifest


def _validate_tool_inventory_evidence(
    value: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    assurance_root: Path,
    policy: TrustPolicy,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    execution_binding = _validate_execution_binding(
        value.get("worker_execution_binding"), profile=profile
    )
    details = _exact_mapping(
        value["details"],
        {
            "inventory",
            "classification",
            "process_binding",
            "launcher_binding",
            "observer_receipt",
        },
        "tool_inventory_details_invalid",
    )
    inventory = _sorted_tool_names(details.get("inventory"), "tool_inventory", nonempty=True)
    if not hmac.compare_digest(_stable_digest(inventory), value["observed_digest"]):
        raise AssuranceGateError("tool_inventory_binding_mismatch")
    process = _exact_mapping(
        details.get("process_binding"),
        {"subject_pid", "process_start_token"},
        "tool_inventory_process_binding_invalid",
    )
    subject_pid = process.get("subject_pid")
    if not isinstance(subject_pid, int) or isinstance(subject_pid, bool) or subject_pid <= 0:
        raise AssuranceGateError("tool_inventory_subject_pid_invalid")
    process_start_token = _safe_observer_token(
        process.get("process_start_token"), "tool_inventory_process_start_token"
    )

    receipt_reference = _exact_mapping(
        details.get("observer_receipt"),
        {"path", "sha256"},
        "common_observer_receipt_reference_invalid",
    )
    receipt_raw = _read_bound_host_artifact(
        assurance_root,
        receipt_reference,
        policy=policy,
        expected_prefix=(
            f"generations/{profile['profile_id']}/{value['generation_id']}/observer/"
        ),
        label="common_observer_receipt",
    )
    try:
        receipt = _load_json_bytes(receipt_raw, label="common_observer_receipt")
    except AssuranceContractError as exc:
        raise AssuranceGateError("common_observer_receipt_invalid_json") from exc
    receipt = _exact_mapping(
        receipt,
        COMMON_RECEIPT_FIELDS,
        "common_observer_receipt_fields_invalid",
    )
    if (
        receipt.get("receipt_version") != "1"
        or receipt.get("profile_id") != profile["profile_id"]
        or receipt.get("observed_at") != value.get("observed_at")
    ):
        raise AssuranceGateError("common_observer_receipt_identity_mismatch")
    _safe_observer_token(receipt.get("receipt_id"), "common_observer_receipt_id")
    observer = _exact_mapping(
        receipt.get("observer"),
        {"kind", "binary_realpath", "binary_sha256"},
        "common_observer_fields_invalid",
    )
    if observer.get("kind") not in OBSERVER_KINDS:
        raise AssuranceGateError("common_observer_kind_invalid")
    _validate_observer_executable(
        observer.get("binary_realpath"),
        observer.get("binary_sha256"),
        policy=policy,
        label="common_observer_binary",
    )
    event = _exact_mapping(
        receipt.get("event"),
        COMMON_IDENTITY_EVENT_FIELDS,
        "common_identity_event_fields_invalid",
    )
    _safe_observer_token(event.get("event_id"), "common_observer_event_id")
    if (
        event.get("event_kind") != "effective_agent_identity"
        or event.get("subject_pid") != subject_pid
        or event.get("process_start_token") != process_start_token
        or event.get("tool_inventory") != inventory
        or event.get("tool_inventory_classification") != details.get("classification")
        or event.get("worker_execution_binding") != execution_binding
        or any(event.get(field) != value["bindings"].get(field) for field in BINDING_FIELDS)
        or event.get("launch_session") != value.get("launch_session")
    ):
        raise AssuranceGateError("common_observer_receipt_evidence_mismatch")

    manifest = _load_tool_manifest(profile)
    tool_observation = event.get("tool_inventory_observation")
    if manifest is None:
        if tool_observation is not None:
            raise AssuranceGateError("worker_unexpected_tool_inventory_observation")
    else:
        observed_policy = _exact_mapping(
            tool_observation,
            TOOL_INVENTORY_OBSERVATION_FIELDS,
            "tool_inventory_observation_fields_invalid",
        )
        policy_facts = _exact_mapping(
            observed_policy.get("policy_facts"),
            TOOL_POLICY_FACT_FIELDS,
            "tool_inventory_policy_facts_invalid",
        )
        requirements_artifact = manifest.get("artifacts", {}).get("requirements")
        profile_artifact = manifest.get("artifacts", {}).get("codex_profile")
        manifest_path = profile.get("configuration_requirements", {}).get(
            "deployment_manifest_path"
        )
        launch_session = value.get("launch_session")
        if (
            observed_policy.get("observation_version") != "1"
            or observed_policy.get("observation_kind")
            != "live_session_machine_policy"
            or set(policy_facts.values()) != {True}
            or observed_policy.get("policy_facts_digest")
            != _stable_digest(policy_facts)
            or observed_policy.get("inventory_digest")
            != _stable_digest(inventory)
            or observed_policy.get("classification_digest")
            != _stable_digest(details.get("classification"))
            or not isinstance(launch_session, Mapping)
            or observed_policy.get("session_id")
            != launch_session.get("session_id")
            or observed_policy.get("launch_session_digest")
            != launch_session.get("record_digest")
            or not isinstance(manifest_path, str)
            or observed_policy.get("deployment_manifest_digest")
            != _hash_external_regular(manifest_path, policy=policy)
            or not isinstance(requirements_artifact, Mapping)
            or observed_policy.get("requirements_digest")
            != requirements_artifact.get("sha256")
            or not isinstance(profile_artifact, Mapping)
            or observed_policy.get("profile_digest")
            != profile_artifact.get("sha256")
            or observed_policy.get("fixed_argv_digest")
            != event.get("runtime_argv_digest")
        ):
            raise AssuranceGateError("tool_inventory_observation_mismatch")
    _validate_tool_classification(
        details.get("classification"),
        inventory=inventory,
        profile=profile,
        manifest=manifest,
    )
    runtime_path = event.get("runtime_binary_realpath")
    runtime_digest = event.get("runtime_binary_digest")
    runtime_argv = event.get("runtime_argv")
    runtime_argv_digest = event.get("runtime_argv_digest")
    profile_path = event.get("profile_snapshot_path")
    profile_digest = event.get("profile_snapshot_digest")
    if not isinstance(profile_path, str) or not _valid_digest(profile_digest):
        raise AssuranceGateError("profile_snapshot_identity_invalid")
    try:
        normalized_argv = _validate_argv(runtime_argv, label="runtime_argv")
    except AssuranceContractError as exc:
        raise AssuranceGateError("runtime_argv_invalid") from exc
    if not _valid_digest(runtime_argv_digest) or not hmac.compare_digest(
        argv_vector_digest(normalized_argv), str(runtime_argv_digest)
    ):
        raise AssuranceGateError("runtime_argv_digest_mismatch")
    if execution_binding is not None and (
        execution_binding.get("runtime_realpath") != runtime_path
        or execution_binding.get("runtime_digest") != runtime_digest
        or execution_binding.get("argv_template") != normalized_argv
        or execution_binding.get("argv_digest") != runtime_argv_digest
    ):
        raise AssuranceGateError("worker_execution_runtime_projection_mismatch")
    if not hmac.compare_digest(_hash_external_regular(profile_path, policy=policy), str(profile_digest)):
        raise AssuranceGateError("profile_snapshot_digest_mismatch")

    launcher_binding = details.get("launcher_binding")
    if manifest is None:
        if launcher_binding is not None or any(
            event.get(field) is not None
            for field in (
                "launcher_realpath",
                "launcher_digest",
                "launcher_process_start_token",
                "host_verified_launch_token",
                "launch_argv_digest",
                "effective_notify_empty",
            )
        ):
            raise AssuranceGateError("worker_unexpected_frontend_launch_binding")
        _validate_observer_executable(
            runtime_path,
            runtime_digest,
            policy=policy,
            label="worker_runtime_binary",
        )
        return (
            _validate_runtime_binding(
                {
                "runtime_realpath": runtime_path,
                "runtime_digest": runtime_digest,
                "argv": normalized_argv,
                "argv_digest": runtime_argv_digest,
                "profile_realpath": profile_path,
                "profile_digest": profile_digest,
                }
            ),
            execution_binding,
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise AssuranceGateError("deployment_manifest_invalid")
    launcher_artifact = artifacts.get("codex_launcher")
    native_artifact = artifacts.get("native_codex")
    profile_artifact = artifacts.get("codex_profile")
    if not all(isinstance(item, Mapping) for item in (launcher_artifact, native_artifact, profile_artifact)):
        raise AssuranceGateError("deployment_launch_artifact_missing")
    launcher = _exact_mapping(
        launcher_binding,
        {
            "subject_pid",
            "process_start_token",
            "launcher_realpath",
            "launcher_digest",
            "launcher_process_start_token",
            "host_verified_launch_token",
            "launch_argv",
            "launch_argv_digest",
            "effective_notify_empty",
        },
        "common_launcher_binding_fields_invalid",
    )
    _safe_observer_token(
        launcher.get("host_verified_launch_token"), "host_verified_launch_token"
    )
    try:
        import codex_main_agent_deployment as deployment  # lazy; canonical argv source

        canonical_argv = deployment.native_codex_argv(str(native_artifact["path"]))
    except Exception as exc:
        raise AssuranceGateError("canonical_launch_argv_unavailable") from exc
    canonical_digest = argv_vector_digest(canonical_argv)
    if (
        launcher.get("subject_pid") != subject_pid
        or launcher.get("process_start_token") != process_start_token
        or launcher.get("launcher_realpath") != launcher_artifact.get("path")
        or launcher.get("launcher_digest") != launcher_artifact.get("sha256")
        or launcher.get("launcher_process_start_token") != process_start_token
        or launcher.get("launch_argv") != canonical_argv
        or launcher.get("launch_argv_digest") != canonical_digest
        or launcher.get("effective_notify_empty") is not True
        or runtime_path != native_artifact.get("path")
        or runtime_digest != native_artifact.get("sha256")
        or profile_path != profile_artifact.get("path")
        or profile_digest != profile_artifact.get("sha256")
        or any(
            event.get(field) != launcher.get(field)
            for field in (
                "launcher_realpath",
                "launcher_digest",
                "launcher_process_start_token",
                "host_verified_launch_token",
                "launch_argv_digest",
                "effective_notify_empty",
            )
        )
    ):
        raise AssuranceGateError("common_launcher_binding_mismatch")
    _validate_observer_executable(
        launcher.get("launcher_realpath"),
        launcher.get("launcher_digest"),
        policy=policy,
        label="launcher_binary",
    )
    _validate_observer_executable(
        runtime_path,
        runtime_digest,
        policy=policy,
        label="runtime_binary",
    )
    if normalized_argv != canonical_argv or runtime_argv_digest != canonical_digest:
        raise AssuranceGateError("runtime_launch_argv_mismatch")
    return (
        _validate_runtime_binding(
            {
            "runtime_realpath": runtime_path,
            "runtime_digest": runtime_digest,
            "argv": normalized_argv,
            "argv_digest": runtime_argv_digest,
            "profile_realpath": profile_path,
            "profile_digest": profile_digest,
            }
        ),
        execution_binding,
    )


CHECKOUT_IDENTITY_FIELDS = {
    "identity_version",
    "workspace_id",
    "checkout_kind",
    "checkout_realpath",
    "managed_primary_realpath",
    "branch",
    "head_sha",
    "tree_sha",
    "git_common_dir_digest",
    "worktree_state_digest",
    "identity_digest",
}
WORKER_CHECKOUT_SENTINEL_MATERIAL = {
    "checkout_binding": "capability_per_execution",
    "repository_scope": "host_verified_work_order",
}
WORKER_CHECKOUT_BINDING_FIELDS = {
    "checkout_binding",
    "repository_scope",
    "identity_digest",
}


def worker_checkout_binding() -> dict[str, str]:
    return {
        **WORKER_CHECKOUT_SENTINEL_MATERIAL,
        "identity_digest": _stable_digest(WORKER_CHECKOUT_SENTINEL_MATERIAL),
    }


def _validate_checkout_identity_shape(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != CHECKOUT_IDENTITY_FIELDS:
        raise AssuranceGateError("checkout_identity_invalid")
    normalized = {key: str(value[key]) for key in CHECKOUT_IDENTITY_FIELDS}
    if normalized["identity_version"] != "2":
        raise AssuranceGateError("checkout_identity_invalid")
    if normalized["checkout_kind"] not in {"managed_primary", "registered_linked_worktree"}:
        raise AssuranceGateError("checkout_identity_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", normalized["workspace_id"]):
        raise AssuranceGateError("checkout_identity_invalid")
    if not GIT_SHA.fullmatch(normalized["head_sha"]) or not GIT_SHA.fullmatch(normalized["tree_sha"]):
        raise AssuranceGateError("checkout_identity_invalid")
    for field in (
        "git_common_dir_digest",
        "worktree_state_digest",
        "identity_digest",
    ):
        if not _valid_digest(normalized[field]):
            raise AssuranceGateError("checkout_identity_invalid")
    material = {key: normalized[key] for key in CHECKOUT_IDENTITY_FIELDS if key != "identity_digest"}
    expected = _stable_digest(material)
    if not hmac.compare_digest(expected, normalized["identity_digest"]):
        raise AssuranceGateError("checkout_identity_digest_mismatch")
    return normalized


def _recompute_checkout_identity(identity: Mapping[str, str]) -> dict[str, str]:
    # Lazy import avoids a cycle when frontdoor_orchestrator imports this module
    # to gate capability derivation/execution.
    try:
        import frontdoor_orchestrator as frontdoor  # type: ignore

        current = frontdoor.resolve_checkout_identity(
            workspace_id=identity["workspace_id"],
            managed_primary=Path(identity["managed_primary_realpath"]),
            checkout_root=Path(identity["checkout_realpath"]),
        )
    except Exception as exc:  # the public gate exports a stable reason, not git details
        raise AssuranceGateError("checkout_binding_unverifiable") from exc
    return {key: str(current[key]) for key in CHECKOUT_IDENTITY_FIELDS}


def _validate_checkout_evidence(
    value: Mapping[str, Any],
    *,
    expected_checkout: Path | None,
    profile: Mapping[str, Any],
) -> dict[str, str]:
    details = value["details"]
    if profile.get("surface_role") == "bounded_worker":
        if expected_checkout is not None:
            raise AssuranceGateError("worker_checkout_sentinel_not_repository_identity")
        if not isinstance(details, dict) or set(details) != {"checkout_binding"}:
            raise AssuranceGateError("checkout_binding_details_invalid")
        binding = details["checkout_binding"]
        expected = worker_checkout_binding()
        if (
            not isinstance(binding, dict)
            or set(binding) != WORKER_CHECKOUT_BINDING_FIELDS
            or binding != expected
            or value["observed_digest"] != expected["identity_digest"]
        ):
            raise AssuranceGateError("worker_checkout_sentinel_invalid")
        return expected
    if not isinstance(details, dict) or set(details) != {"checkout_identity"}:
        raise AssuranceGateError("checkout_binding_details_invalid")
    identity = _validate_checkout_identity_shape(details["checkout_identity"])
    if not hmac.compare_digest(identity["identity_digest"], value["observed_digest"]):
        raise AssuranceGateError("checkout_binding_digest_mismatch")
    if expected_checkout is not None:
        supplied = expected_checkout.expanduser()
        try:
            exact = supplied.resolve(strict=True)
        except OSError as exc:
            raise AssuranceGateError("expected_checkout_unavailable") from exc
        if not supplied.is_absolute() or supplied != exact:
            raise AssuranceGateError("expected_checkout_not_canonical")
        if str(exact) != identity["checkout_realpath"]:
            raise AssuranceGateError("expected_checkout_mismatch")
    current = _recompute_checkout_identity(identity)
    if current != identity:
        raise AssuranceGateError("checkout_binding_drift")
    return identity


def _optional_host_id(value: Any, *, label: str, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not OPAQUE_HOST_ID.fullmatch(value):
        raise AssuranceGateError(f"host_observation_{label}_invalid")
    return value


def _read_bound_host_artifact(
    assurance_root: Path,
    artifact: Mapping[str, Any],
    *,
    policy: TrustPolicy,
    expected_prefix: str,
    label: str,
) -> bytes:
    if not isinstance(artifact, Mapping) or set(artifact) != {"path", "sha256"}:
        raise AssuranceGateError(f"{label}_reference_invalid")
    path = artifact.get("path")
    digest = artifact.get("sha256")
    if not isinstance(path, str) or not path.startswith(expected_prefix) or not _valid_digest(digest):
        raise AssuranceGateError(f"{label}_reference_invalid")
    try:
        raw = secure_read_relative(assurance_root, path, policy=policy)
    except EvidenceTrustError as exc:
        raise AssuranceGateError(f"{label}_{exc.reason}") from exc
    if not hmac.compare_digest(_sha256_bytes(raw), digest):
        raise AssuranceGateError(f"{label}_digest_mismatch")
    return raw


def _verify_worker_commissioning_receipt(
    evidence: Mapping[str, Any],
    observation: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    assurance_root: Path,
    policy: TrustPolicy,
) -> None:
    receipt = _exact_mapping(
        receipt,
        WORKER_COMMISSIONING_RECEIPT_FIELDS,
        "worker_commissioning_receipt_fields_invalid",
    )
    receipt_event = _exact_mapping(
        receipt.get("event"),
        WORKER_COMMISSIONING_RECEIPT_EVENT_FIELDS,
        "worker_commissioning_receipt_event_fields_invalid",
    )
    observer = _exact_mapping(
        receipt.get("observer"),
        {"kind", "binary_realpath", "binary_sha256"},
        "worker_commissioning_observer_fields_invalid",
    )
    _validate_observer_executable(
        observer.get("binary_realpath"),
        observer.get("binary_sha256"),
        policy=policy,
        label="worker_commissioning_observer",
    )
    if observer.get("kind") != "sandbox_policy_audit":
        raise AssuranceGateError("worker_commissioning_observer_kind_invalid")
    if (
        receipt.get("receipt_version") != "1"
        or receipt.get("profile_id") != evidence["profile_id"]
        or receipt.get("evidence_type") != evidence["evidence_type"]
        or receipt.get("claim") != "managed_worker"
        or receipt.get("operation") != evidence["operation"]
        or receipt.get("observed_at") != evidence["observed_at"]
        or receipt_event.get("event_version") != "1"
        or not isinstance(receipt_event.get("event_id"), str)
        or not SAFE_ID.fullmatch(receipt_event["event_id"])
        or receipt_event.get("operation") != evidence["operation"]
        or receipt_event.get("subject_pid") is None
        or receipt_event.get("process_start_token") is None
        or receipt_event.get("subject_pid") <= 0
    ):
        raise AssuranceGateError("worker_commissioning_receipt_identity_mismatch")
    # Worker commissioning uses the full execution binding in every evidence
    # object.  The receipt binds its digest, while the adapter independently
    # revalidates the executable, argv template, environment, and profile mode.
    execution_binding = evidence.get("worker_execution_binding")
    if (
        not isinstance(execution_binding, Mapping)
        or receipt_event.get("worker_execution_binding_digest")
        != _stable_digest(dict(execution_binding))
    ):
        raise AssuranceGateError("worker_commissioning_execution_binding_mismatch")
    probe_reference = receipt_event.get("worker_probe_event")
    probe_raw = _read_bound_host_artifact(
        assurance_root,
        probe_reference,
        policy=policy,
        expected_prefix=f"commissioning/{evidence['profile_id']}/",
        label="worker_probe_event",
    )
    try:
        envelope = _load_json_bytes(probe_raw, label="worker_probe_event")
    except AssuranceContractError as exc:
        raise AssuranceGateError("worker_probe_event_invalid_json") from exc
    envelope = _exact_mapping(
        envelope,
        WORKER_PROBE_EVENT_ENVELOPE_FIELDS,
        "worker_probe_event_envelope_fields_invalid",
    )
    probe_event = _exact_mapping(
        envelope.get("event"),
        WORKER_PROBE_EVENT_FIELDS,
        "worker_probe_event_fields_invalid",
    )
    facts = _exact_mapping(
        probe_event.get("probe_facts"),
        {"facts_version", "observed", "mechanical_policy", "not_model_prose"},
        "worker_probe_facts_fields_invalid",
    )
    if (
        envelope.get("envelope_version") != "1"
        or envelope.get("event_digest") != _stable_digest(dict(probe_event))
        or envelope.get("execution_binding_digest")
        != _stable_digest(dict(execution_binding))
        or probe_event.get("event_version") != "1"
        or probe_event.get("profile_id") != evidence["profile_id"]
        or probe_event.get("generation_id") != evidence["generation_id"]
        or probe_event.get("runtime_binding_digest")
        != envelope.get("execution_binding_digest")
        or probe_event.get("runtime_realpath")
        != execution_binding.get("runtime_realpath")
        or probe_event.get("runtime_digest")
        != execution_binding.get("runtime_digest")
        or probe_event.get("subject_pid") != receipt_event.get("subject_pid")
        or probe_event.get("process_start_token")
        != receipt_event.get("process_start_token")
        or probe_event.get("probe_contract_result") != "pass"
        or probe_event.get("exit_code") != 0
        or probe_event.get("marker_before_digest")
        != probe_event.get("marker_after_digest")
        or facts.get("facts_version") != "1"
        or facts.get("observed") != WORKER_PROBE_OBSERVED_FACTS
        or facts.get("mechanical_policy") != WORKER_PROBE_POLICY_FACTS
        or facts.get("not_model_prose") is not True
        or probe_event.get("observed_at") != evidence.get("observed_at")
    ):
        raise AssuranceGateError("worker_probe_event_mismatch")
    try:
        import scoped_worker_executor as scoped_worker

        expected_argv = scoped_worker.commissioning_probe_argv(
            dict(execution_binding)
        )
    except Exception as exc:
        raise AssuranceGateError("worker_probe_argv_unverifiable") from exc
    if (
        probe_event.get("argv") != expected_argv
        or probe_event.get("argv_digest") != argv_vector_digest(expected_argv)
        or any(
            not _valid_digest(probe_event.get(field))
            for field in (
                "marker_before_digest",
                "probe_result_digest",
                "probe_marker_digest",
                "probe_worktree_status_digest",
            )
        )
    ):
        raise AssuranceGateError("worker_probe_argv_or_digest_mismatch")
    commissioning_reference = receipt_event.get("commissioning_reference")
    if (
        commissioning_reference != envelope.get("commissioning_reference")
        or not isinstance(commissioning_reference, str)
    ):
        raise AssuranceGateError("worker_commissioning_reference_mismatch")
    try:
        commissioning_raw = secure_read_relative(
            assurance_root, commissioning_reference, policy=policy
        )
        commissioning = _load_json_bytes(
            commissioning_raw, label="worker_commissioning"
        )
    except (EvidenceTrustError, AssuranceContractError) as exc:
        raise AssuranceGateError("worker_commissioning_record_untrusted") from exc
    material = {
        key: commissioning[key]
        for key in sorted(set(commissioning) - {"record_digest"})
    }
    if commissioning.get("record_digest") != _stable_digest(material):
        raise AssuranceGateError("worker_commissioning_record_digest_mismatch")
    observed_record = dict(commissioning)
    if observed_record.get("state") == "completed":
        observed_record["state"] = "observed"
        observed_record["generation_manifest_digest"] = None
        observed_record["record_digest"] = "sha256:" + "0" * 64
        observed_digest = _stable_digest(
            {
                key: observed_record[key]
                for key in sorted(set(observed_record) - {"record_digest"})
            }
        )
    else:
        observed_digest = str(observed_record.get("record_digest") or "")
    claimed_record = dict(commissioning)
    claimed_record.update(
        {
            "state": "claimed",
            "completed_at": None,
            "consumer_event_digest": None,
            "generation_manifest_digest": None,
            "record_digest": "sha256:" + "0" * 64,
        }
    )
    claimed_digest = _stable_digest(
        {
            key: claimed_record[key]
            for key in sorted(set(claimed_record) - {"record_digest"})
        }
    )
    expected_probe_reference = commissioning_reference.removesuffix(".json") + "-worker-event.json"
    if (
        commissioning.get("state") not in {"observed", "completed"}
        or commissioning.get("profile_id") != evidence["profile_id"]
        or commissioning.get("generation_id") != evidence["generation_id"]
        or commissioning.get("consumer_event_digest")
        != envelope.get("event_digest")
        or commissioning.get("execution_binding_digest")
        != envelope.get("execution_binding_digest")
        or receipt_event.get("commissioning_record_digest") != observed_digest
        or envelope.get("claimed_record_digest") != claimed_digest
        or envelope.get("single_use_nonce_digest")
        != _stable_digest(
            {"single_use_nonce": commissioning.get("single_use_nonce")}
        )
        or probe_reference.get("path") != expected_probe_reference
        or commissioning.get("purpose")
        != "managed_worker_surface_launch_probe"
        or commissioning.get("operation") != "surface_launch"
    ):
        raise AssuranceGateError("worker_commissioning_record_mismatch")
    operation = str(evidence["operation"])
    if evidence["evidence_type"] == "worker_launch_binding":
        expected_source = "observed"
        expected_value = WORKER_PROBE_OBSERVED_FACTS["surface_launch"]
        expected_kind = "worker_commissioning_observed_fact"
    elif operation in {"filesystem_write", "process_spawn"}:
        expected_source = "observed"
        expected_value = WORKER_PROBE_OBSERVED_FACTS[operation]
        expected_kind = "worker_commissioning_observed_fact"
    elif operation == "git_commit":
        expected_source = "postcondition_invariant"
        expected_value = WORKER_PROBE_OBSERVED_FACTS[operation]
        expected_kind = "worker_commissioning_postcondition_proof"
    else:
        expected_source = "mechanical_policy"
        expected_value = WORKER_PROBE_POLICY_FACTS.get(operation)
        expected_kind = "worker_commissioning_policy_proof"
    if (
        expected_value is None
        or receipt_event.get("fact_source") != expected_source
        or receipt_event.get("fact_value") != expected_value
        or receipt_event.get("event_kind") != expected_kind
    ):
        raise AssuranceGateError("worker_commissioning_fact_mismatch")


def _verify_host_observation(
    evidence: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    assurance_root: Path,
    policy: TrustPolicy,
) -> None:
    """Verify host artifacts for one attempted operation.

    Model text is never an authority signal.  A pass requires a separately
    digest-bound host observation plus actual before/after marker files.
    """

    reference = evidence["details"]["host_observation"]
    prefix = (
        f"generations/{profile['profile_id']}/{evidence['generation_id']}/observations/"
    )
    raw = _read_bound_host_artifact(
        assurance_root,
        reference,
        policy=policy,
        expected_prefix=prefix,
        label="host_observation",
    )
    try:
        observation = _load_json_bytes(raw, label="host_observation")
    except AssuranceContractError as exc:
        raise AssuranceGateError("host_observation_invalid_json") from exc
    if set(observation) != HOST_OBSERVATION_FIELDS:
        raise AssuranceGateError("host_observation_invalid_fields")
    if observation.get("observation_version") != "1":
        raise AssuranceGateError("host_observation_version_invalid")
    if not isinstance(observation.get("observation_id"), str) or not SAFE_ID.fullmatch(
        observation["observation_id"]
    ):
        raise AssuranceGateError("host_observation_id_invalid")
    for field in ("profile_id", "evidence_type", "claim", "operation", "observed_at"):
        if observation.get(field) != evidence.get(field):
            raise AssuranceGateError(f"host_observation_{field}_mismatch")
    if observation.get("source") != "host_harness":
        raise AssuranceGateError("host_observation_source_invalid")
    if not isinstance(observation.get("attempted"), bool):
        raise AssuranceGateError("host_observation_attempted_invalid")
    if observation.get("model_prose_authority") is not False:
        raise AssuranceGateError("model_prose_not_evidence")
    if not isinstance(observation.get("approval_prompted"), bool):
        raise AssuranceGateError("host_observation_approval_invalid")
    if not isinstance(observation.get("side_effect_observed"), bool):
        raise AssuranceGateError("host_observation_side_effect_invalid")
    if not isinstance(observation.get("capability_bound"), bool):
        raise AssuranceGateError("host_observation_capability_binding_invalid")
    if observation.get("ambient_authority") is not False:
        raise AssuranceGateError("host_observation_ambient_authority")
    receipt_reference = observation.get("observer_receipt")
    receipt: Mapping[str, Any] | None = None
    receipt_event: Mapping[str, Any] | None = None
    probe_binding = observation.get("probe_process_binding")
    if receipt_reference is None:
        if probe_binding is not None:
            raise AssuranceGateError("host_observation_probe_receipt_missing")
    else:
        receipt_raw = _read_bound_host_artifact(
            assurance_root,
            receipt_reference,
            policy=policy,
            expected_prefix=(
                f"generations/{profile['profile_id']}/{evidence['generation_id']}/observer/"
            ),
            label="operation_observer_receipt",
        )
        try:
            receipt = _load_json_bytes(receipt_raw, label="operation_observer_receipt")
        except AssuranceContractError as exc:
            raise AssuranceGateError("operation_observer_receipt_invalid") from exc
        receipt_event = receipt.get("event") if isinstance(receipt, Mapping) else None
        if (
            not isinstance(receipt_event, Mapping)
            or receipt.get("profile_id") != evidence["profile_id"]
            or receipt.get("evidence_type") != evidence["evidence_type"]
            or receipt.get("operation") != evidence["operation"]
            or receipt_event.get("probe_process_binding") != probe_binding
        ):
            raise AssuranceGateError("operation_observer_receipt_mismatch")
    if profile.get("surface_role") == "bounded_worker":
        if receipt is None:
            # Production never accepts synthesized worker observations.  Some
            # legacy fixture builders exercise generic attestation mechanics
            # without a root commissioning event; their explicit fixture
            # policy cannot be selected by any production CLI.
            if policy == TrustPolicy.production():
                raise AssuranceGateError("worker_commissioning_receipt_missing")
        else:
            _verify_worker_commissioning_receipt(
                evidence,
                observation,
                receipt,
                assurance_root=assurance_root,
                policy=policy,
            )
    if probe_binding is not None:
        if not isinstance(probe_binding, Mapping):
            raise AssuranceGateError("commissioning_probe_binding_invalid")
        try:
            import codex_main_agent_supervisor as supervisor

            session_raw = secure_read_relative(
                assurance_root,
                str(probe_binding.get("launch_session_reference") or ""),
                policy=policy,
            )
            companion_raw = secure_read_relative(
                assurance_root,
                str(probe_binding.get("commissioning_launch_reference") or ""),
                policy=policy,
            )
            probe_session = supervisor.validate_session_record_shape(
                _load_json_bytes(session_raw, label="operation_probe_session")
            )
            companion = supervisor.validate_commissioning_launch_shape(
                _load_json_bytes(companion_raw, label="operation_probe_companion")
            )
        except Exception as exc:
            raise AssuranceGateError("commissioning_probe_record_untrusted") from exc
        expected_probe = {
            "direct_action_denial": "frontend_filesystem_denial",
            "gateway_positive_path": "frontend_gateway_routing",
        }.get(evidence["evidence_type"])
        live = companion.get("live_observation")
        if (
            expected_probe is None
            or probe_session.get("session_kind") != "commissioning"
            or probe_session.get("record_digest")
            != probe_binding.get("launch_session_digest")
            or probe_session.get("subject_pid") != probe_binding.get("subject_pid")
            or probe_session.get("process_start_token")
            != probe_binding.get("process_start_token")
            or probe_session.get("native_realpath")
            != probe_binding.get("runtime_realpath")
            or probe_session.get("native_digest")
            != probe_binding.get("runtime_digest")
            or companion.get("state") not in {"active", "consumed"}
            or companion.get("generation_id") != evidence["generation_id"]
            or companion.get("profile_id") != evidence["profile_id"]
            or companion.get("probe_id") != expected_probe
            or companion.get("commissioning_id")
            != probe_binding.get("commissioning_id")
            or companion.get("nonce_digest") != probe_binding.get("nonce_digest")
            or companion.get("binding_digest")
            != probe_binding.get("commissioning_binding_digest")
            or companion.get("launch_session_digest")
            != probe_binding.get("launch_session_digest")
            or companion.get("probe_argv_digest")
            != probe_binding.get("argv_digest")
            or not isinstance(live, Mapping)
            or companion.get("live_observation_digest")
            != probe_binding.get("commissioning_live_observation_digest")
            or companion.get("live_observation_digest")
            != _stable_digest(dict(live))
        ):
            raise AssuranceGateError("commissioning_probe_record_mismatch")
    marker_prefix = (
        f"generations/{profile['profile_id']}/{evidence['generation_id']}/markers/"
    )
    before_ref = {
        "path": observation.get("before_marker_path"),
        "sha256": observation.get("before_marker_digest"),
    }
    after_ref = {
        "path": observation.get("after_marker_path"),
        "sha256": observation.get("after_marker_digest"),
    }
    _read_bound_host_artifact(
        assurance_root,
        before_ref,
        policy=policy,
        expected_prefix=marker_prefix,
        label="before_marker",
    )
    _read_bound_host_artifact(
        assurance_root,
        after_ref,
        policy=policy,
        expected_prefix=marker_prefix,
        label="after_marker",
    )
    evidence_type = evidence["evidence_type"]
    outcome = observation.get("outcome")
    request_id = observation.get("saihai_request_id")
    capability_id = observation.get("capability_id")
    audit_id = observation.get("audit_id")
    if evidence_type == "direct_action_denial":
        if outcome != "denied":
            raise AssuranceGateError("direct_action_not_denied")
        if observation["approval_prompted"] is not False:
            raise AssuranceGateError("direct_action_prompted_for_approval")
        if observation["side_effect_observed"] is not False:
            raise AssuranceGateError("direct_action_side_effect_observed")
        if observation["capability_bound"] is not False:
            raise AssuranceGateError("direct_action_unexpected_capability")
        if observation["before_marker_digest"] != observation["after_marker_digest"]:
            raise AssuranceGateError("direct_action_marker_changed")
        _optional_host_id(request_id, label="request_id")
        _optional_host_id(audit_id, label="audit_id")
        if capability_id is not None:
            raise AssuranceGateError("direct_action_unexpected_capability")
        if observation["attempted"] is False:
            if probe_binding is not None:
                raise AssuranceGateError("mechanical_absence_probe_unexpected")
            if (
                receipt_event is None
                or receipt_event.get("event_kind") != "mechanical_absence_proof"
                or receipt_event.get("observation_basis") != "mechanically_absent"
                or receipt_event.get("structured_event_digest") is not None
            ):
                raise AssuranceGateError("host_observation_inconclusive")
    elif evidence_type == "gateway_positive_path":
        if observation["attempted"] is not True:
            raise AssuranceGateError("gateway_positive_path_not_attempted")
        if (
            outcome != "routed_to_saihai"
            or observation["side_effect_observed"] is not False
            or observation["before_marker_digest"] != observation["after_marker_digest"]
        ):
            raise AssuranceGateError("gateway_positive_path_not_observed")
        if (
            observation["approval_prompted"] is not False
            or observation["capability_bound"] is not False
        ):
            raise AssuranceGateError("gateway_positive_path_exceeded_frontend_authority")
        _optional_host_id(request_id, label="request_id", required=True)
        _optional_host_id(audit_id, label="audit_id", required=True)
        if capability_id is not None:
            raise AssuranceGateError("gateway_positive_path_unexpected_capability")
    elif evidence_type == "ingress_operation":
        if observation["attempted"] is not True:
            raise AssuranceGateError("ingress_operation_not_attempted")
        if outcome != "allowed_via_saihai" or observation["capability_bound"] is not False:
            raise AssuranceGateError("ingress_host_path_not_observed")
        _optional_host_id(request_id, label="request_id", required=True)
        _optional_host_id(audit_id, label="audit_id", required=True)
        if capability_id is not None:
            raise AssuranceGateError("ingress_unexpected_capability")
    elif evidence_type == "capability_boundary":
        expected_coverage = profile["operation_requirements"][evidence["operation"]]
        _optional_host_id(request_id, label="request_id", required=True)
        _optional_host_id(capability_id, label="capability_id", required=True)
        _optional_host_id(audit_id, label="audit_id", required=True)
        if observation["capability_bound"] is not True:
            raise AssuranceGateError("worker_operation_not_capability_bound")
        if evidence["operation"] in WORKER_PROMOTION_BLOCKED_OPERATIONS:
            if (
                evidence["result"] != "fail"
                or outcome != "inconclusive"
                or observation["attempted"] is not False
                or observation["side_effect_observed"] is not False
                or observation["before_marker_digest"]
                != observation["after_marker_digest"]
            ):
                raise AssuranceGateError("worker_denial_fact_not_promotable")
        elif expected_coverage == "capability_scoped":
            expected_attempted = evidence["operation"] in {
                "filesystem_write",
                "process_spawn",
            }
            if (
                outcome != "capability_scoped"
                or observation["attempted"] is not expected_attempted
                or observation["side_effect_observed"] is not expected_attempted
            ):
                raise AssuranceGateError("worker_capability_positive_not_observed")
        else:
            expected_outcome = (
                "post_execution_invariant"
                if evidence["operation"] == "git_commit"
                else "denied"
            )
            if (
                outcome != expected_outcome
                or observation["attempted"] is not False
                or observation["side_effect_observed"] is not False
            ):
                raise AssuranceGateError("worker_denial_not_observed")
            if observation["before_marker_digest"] != observation["after_marker_digest"]:
                raise AssuranceGateError("worker_denial_marker_changed")
    elif evidence_type == "worker_launch_binding":
        if observation["attempted"] is not True:
            raise AssuranceGateError("worker_launch_not_attempted")
        _optional_host_id(request_id, label="request_id", required=True)
        _optional_host_id(capability_id, label="capability_id", required=True)
        _optional_host_id(audit_id, label="audit_id", required=True)
        launch = observation.get("launch_binding")
        expected_launch = {
            "binary_digest": evidence["bindings"]["runtime_binary_digest"],
            "configuration_digest": evidence["bindings"]["configuration_digest"],
            "checkout_identity_digest": evidence["bindings"]["checkout_digest"],
            "host_owned": True,
        }
        if (
            outcome != "launched"
            or observation["capability_bound"] is not True
            or observation["side_effect_observed"] is not True
            or launch != expected_launch
        ):
            raise AssuranceGateError("worker_launch_not_host_bound")
    if evidence_type != "worker_launch_binding" and observation.get("launch_binding") is not None:
        raise AssuranceGateError("host_observation_unexpected_launch_binding")


def _required_evidence_keys(profile: Mapping[str, Any]) -> set[tuple[str, str, str | None]]:
    required = {(kind, "common", None) for kind in COMMON_EVIDENCE_TYPES}
    targets = set(profile["target_claims"])
    if "ingress_enforced" in targets:
        required.update(
            ("ingress_operation", "ingress_enforced", operation)
            for operation in INGRESS_OPERATIONS
        )
    if "action_enforced" in targets:
        required.update(
            ("direct_action_denial", "action_enforced", operation)
            for operation in ACTION_OPERATIONS
        )
        required.add(("gateway_positive_path", "action_enforced", None))
    if "managed_worker" in targets:
        required.add(("worker_launch_binding", "managed_worker", "surface_launch"))
        required.update(
            ("capability_boundary", "managed_worker", operation)
            for operation in ACTION_OPERATIONS
        )
    return required


def _claim_required_keys(claim: str) -> set[tuple[str, str, str | None]]:
    common = {(kind, "common", None) for kind in COMMON_EVIDENCE_TYPES}
    if claim == "ingress_enforced":
        return common | {
            ("ingress_operation", claim, operation) for operation in INGRESS_OPERATIONS
        }
    if claim == "action_enforced":
        return common | {
            ("direct_action_denial", claim, operation) for operation in ACTION_OPERATIONS
        } | {("gateway_positive_path", claim, None)}
    if claim == "managed_worker":
        return common | {
            ("capability_boundary", claim, operation) for operation in ACTION_OPERATIONS
        } | {("worker_launch_binding", claim, "surface_launch")}
    raise AssuranceContractError("unsupported_claim")


def _verify_evidence_payload(
    value: dict[str, Any],
    *,
    profile: Mapping[str, Any],
    assurance_root: Path,
    now: datetime,
    policy: TrustPolicy,
    expected_checkout: Path | None,
) -> tuple[
    dict[str, str] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    errors = validate_evidence_payload(value)
    if errors:
        raise AssuranceGateError("invalid_evidence", errors)
    if value["profile_id"] != profile["profile_id"]:
        raise AssuranceGateError("evidence_profile_mismatch")
    subject = profile_subject_digest(profile)
    if not hmac.compare_digest(value["registry_subject_digest"], subject):
        raise AssuranceGateError("evidence_subject_mismatch")
    observed_at = _parse_timestamp(value["observed_at"], label="observed_at")
    valid_until = _parse_timestamp(value["valid_until"], label="valid_until")
    _validate_time_window(
        observed_at=observed_at,
        valid_until=valid_until,
        now=now,
        max_age_seconds=profile["evidence_policy"]["max_age_seconds"],
        future_skew_seconds=profile["evidence_policy"]["future_skew_seconds"],
        prefix="evidence",
    )
    bindings = _validate_bindings(value["bindings"], label="evidence_bindings")
    worker_execution_binding = _validate_execution_binding(
        value.get("worker_execution_binding"), profile=profile
    )
    launch_session = _verify_launch_session(
        value["launch_session"],
        assurance_root=assurance_root,
        profile=profile,
        bindings=bindings,
        policy=policy,
        now=now,
    )
    evidence_type = value["evidence_type"]
    binding_field_by_type = {
        "configuration_snapshot": "configuration_digest",
        "runtime_binary_identity": "runtime_binary_digest",
        "tool_inventory": "tool_inventory_digest",
        "checkout_binding": "checkout_digest",
    }
    if evidence_type in binding_field_by_type:
        field = binding_field_by_type[evidence_type]
        if not hmac.compare_digest(value["observed_digest"], bindings[field]):
            raise AssuranceGateError("evidence_observed_binding_mismatch")
    checkout_identity: dict[str, str] | None = None
    runtime_binding: dict[str, Any] | None = None
    if evidence_type == "configuration_snapshot":
        _validate_configuration_evidence(value, profile=profile, policy=policy)
    elif evidence_type == "runtime_binary_identity":
        _validate_runtime_binary_evidence(value, policy=policy)
    elif evidence_type == "tool_inventory":
        runtime_binding, observed_execution_binding = _validate_tool_inventory_evidence(
            value,
            profile=profile,
            assurance_root=assurance_root,
            policy=policy,
        )
        if observed_execution_binding != worker_execution_binding:
            raise AssuranceGateError("worker_execution_binding_evidence_mismatch")
    elif evidence_type == "checkout_binding":
        checkout_identity = _validate_checkout_evidence(
            value,
            expected_checkout=expected_checkout,
            profile=profile,
        )
    else:
        _verify_host_observation(
            value,
            profile=profile,
            assurance_root=assurance_root,
            policy=policy,
        )
    return (
        checkout_identity,
        runtime_binding,
        launch_session,
        worker_execution_binding,
    )


def _artifact_reference(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"reference", "sha256"}:
        raise AssuranceGateError(f"{label}_invalid")
    reference = value.get("reference")
    digest = value.get("sha256")
    try:
        _relative_parts(reference)
    except EvidenceTrustError as exc:
        raise AssuranceGateError(f"{label}_invalid") from exc
    if not _valid_digest(digest):
        raise AssuranceGateError(f"{label}_invalid")
    return {"reference": str(reference), "sha256": str(digest)}


def _generation_manifest_material(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in sorted(GENERATION_MANIFEST_FIELDS - {"manifest_digest"})
    }


def _validate_generation_manifest(
    value: Any,
    *,
    profile: Mapping[str, Any],
    generation_id: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != GENERATION_MANIFEST_FIELDS:
        raise AssuranceGateError("generation_manifest_fields_invalid")
    manifest = dict(value)
    if manifest.get("manifest_version") != "1":
        raise AssuranceGateError("generation_manifest_version_invalid")
    for field in (
        "manifest_id",
        "generation_id",
        "profile_id",
        "commissioning_id",
        "deployment_epoch_id",
    ):
        if not isinstance(manifest.get(field), str) or not SAFE_ID.fullmatch(manifest[field]):
            raise AssuranceGateError(f"generation_manifest_{field}_invalid")
    if (
        manifest["profile_id"] != profile["profile_id"]
        or manifest["generation_id"] != generation_id
        or manifest["registry_subject_digest"] != profile_subject_digest(profile)
    ):
        raise AssuranceGateError("generation_manifest_binding_mismatch")
    launch_digest = manifest.get("launch_session_digest")
    if profile.get("launch_validator") is None:
        if launch_digest is not None:
            raise AssuranceGateError("generation_unexpected_launch_session")
    elif not _valid_digest(launch_digest):
        raise AssuranceGateError("generation_launch_session_digest_invalid")
    _parse_timestamp(manifest.get("created_at"), label="generation_manifest_created_at")
    references = manifest.get("evidence")
    if not isinstance(references, list) or not references:
        raise AssuranceGateError("generation_manifest_evidence_invalid")
    normalized = [_artifact_reference(item, label="generation_evidence") for item in references]
    if normalized != sorted(normalized, key=lambda item: item["reference"]):
        raise AssuranceGateError("generation_manifest_evidence_not_sorted")
    if len({item["reference"] for item in normalized}) != len(normalized):
        raise AssuranceGateError("generation_manifest_evidence_duplicate")
    expected_prefix = f"generations/{profile['profile_id']}/{generation_id}/evidence/"
    if any(not item["reference"].startswith(expected_prefix) for item in normalized):
        raise AssuranceGateError("generation_manifest_evidence_namespace_mismatch")
    digest = manifest.get("manifest_digest")
    if not _valid_digest(digest) or not hmac.compare_digest(
        str(digest), _stable_digest(_generation_manifest_material(manifest))
    ):
        raise AssuranceGateError("generation_manifest_digest_mismatch")
    manifest["evidence"] = normalized
    return manifest


def _load_active_generation(
    assurance_root: Path,
    profile: Mapping[str, Any],
    *,
    policy: TrustPolicy,
) -> tuple[dict[str, Any], bytes, dict[str, Any], bytes]:
    deployment_epoch = _require_commissionable_deployment_epoch(
        assurance_root,
        str(profile["profile_id"]),
        policy=policy,
    )
    active_reference = f"active/{profile['profile_id']}.json"
    try:
        active_raw = secure_read_relative(assurance_root, active_reference, policy=policy)
        active = _load_json_bytes(active_raw, label="active_attestation")
    except EvidenceTrustError as exc:
        reason = "attestation_missing" if exc.reason == "trusted_artifact_missing" else exc.reason
        raise AssuranceGateError(reason) from exc
    except AssuranceContractError as exc:
        raise AssuranceGateError("active_attestation_invalid_json") from exc
    if set(active) != ACTIVE_ATTESTATION_FIELDS or active.get("active_version") != "1":
        raise AssuranceGateError("active_attestation_fields_invalid")
    for field in ("activation_id", "profile_id", "generation_id"):
        if not isinstance(active.get(field), str) or not SAFE_ID.fullmatch(active[field]):
            raise AssuranceGateError(f"active_attestation_{field}_invalid")
    if active["profile_id"] != profile["profile_id"]:
        raise AssuranceGateError("active_attestation_profile_mismatch")
    previous = active.get("previous_generation_id")
    if previous is not None and (
        not isinstance(previous, str)
        or not SAFE_ID.fullmatch(previous)
        or previous == active["generation_id"]
    ):
        raise AssuranceGateError("active_attestation_previous_invalid")
    _parse_timestamp(active.get("activated_at"), label="active_attestation_activated_at")
    generation_id = active["generation_id"]
    manifest_ref = _artifact_reference(
        active.get("generation_manifest"), label="active_generation_manifest"
    )
    attestation_ref = _artifact_reference(active.get("attestation"), label="active_attestation")
    expected_root = f"generations/{profile['profile_id']}/{generation_id}/"
    if manifest_ref["reference"] != expected_root + "manifest.json":
        raise AssuranceGateError("active_generation_manifest_reference_mismatch")
    if attestation_ref["reference"] != expected_root + "attestation.json":
        raise AssuranceGateError("active_attestation_reference_mismatch")
    try:
        manifest_raw = secure_read_relative(
            assurance_root, manifest_ref["reference"], policy=policy
        )
        attestation_raw = secure_read_relative(
            assurance_root, attestation_ref["reference"], policy=policy
        )
    except EvidenceTrustError as exc:
        raise AssuranceGateError(f"active_generation_{exc.reason}") from exc
    if not hmac.compare_digest(_sha256_bytes(manifest_raw), manifest_ref["sha256"]):
        raise AssuranceGateError("active_generation_manifest_digest_mismatch")
    if not hmac.compare_digest(_sha256_bytes(attestation_raw), attestation_ref["sha256"]):
        raise AssuranceGateError("active_attestation_digest_mismatch")
    try:
        manifest_value = _load_json_bytes(manifest_raw, label="generation_manifest")
    except AssuranceContractError as exc:
        raise AssuranceGateError("generation_manifest_invalid_json") from exc
    manifest = _validate_generation_manifest(
        manifest_value, profile=profile, generation_id=generation_id
    )
    if not hmac.compare_digest(
        str(manifest["deployment_epoch_id"]),
        str(deployment_epoch["epoch_id"]),
    ):
        raise AssuranceGateError("deployment_epoch_mismatch")
    return active, attestation_raw, manifest, manifest_raw


def _verify_attestation(
    profile: Mapping[str, Any],
    *,
    assurance_root: Path,
    policy: TrustPolicy,
    now: datetime,
    expected_checkout: Path | None,
) -> _VerifiedAttestation:
    active, raw, generation_manifest, manifest_raw = _load_active_generation(
        assurance_root, profile, policy=policy
    )
    digest = _sha256_bytes(raw)
    try:
        attestation = _load_json_bytes(raw, label="attestation")
    except AssuranceContractError as exc:
        raise AssuranceGateError("attestation_invalid_json") from exc
    errors = validate_attestation_payload(attestation)
    if errors:
        raise AssuranceGateError("invalid_attestation", errors)
    if attestation["profile_id"] != profile["profile_id"]:
        raise AssuranceGateError("attestation_profile_mismatch")
    generation_id = active["generation_id"]
    if attestation["generation_id"] != generation_id:
        raise AssuranceGateError("attestation_generation_mismatch")
    expected_manifest_reference = {
        "reference": f"generations/{profile['profile_id']}/{generation_id}/manifest.json",
        "sha256": _sha256_bytes(manifest_raw),
    }
    if attestation["generation_manifest"] != expected_manifest_reference:
        raise AssuranceGateError("attestation_generation_manifest_mismatch")
    subject = profile_subject_digest(profile)
    if not hmac.compare_digest(attestation["registry_subject_digest"], subject):
        raise AssuranceGateError("attestation_subject_mismatch")
    if attestation["target_claims"] != profile["target_claims"]:
        raise AssuranceGateError("attestation_target_claims_mismatch")
    issued_at = _parse_timestamp(attestation["issued_at"], label="issued_at")
    valid_until = _parse_timestamp(attestation["valid_until"], label="valid_until")
    _validate_time_window(
        observed_at=issued_at,
        valid_until=valid_until,
        now=now,
        max_age_seconds=profile["evidence_policy"]["max_age_seconds"],
        future_skew_seconds=profile["evidence_policy"]["future_skew_seconds"],
        prefix="attestation",
    )
    attestation_bindings = _validate_bindings(
        attestation["bindings"],
        label="attestation_bindings",
    )
    evidence_payloads: list[Mapping[str, Any]] = []
    evidence_digests: list[str] = []
    evidence_keys: dict[tuple[str, str, str | None], str] = {}
    checkout_identity: dict[str, str] | None = None
    runtime_binding: dict[str, Any] | None = None
    worker_execution_binding: dict[str, Any] | None = None
    worker_execution_binding_seen = False
    launch_session: dict[str, Any] | None = None
    launch_session_seen = False
    profile_prefix = f"generations/{profile['profile_id']}/{generation_id}/evidence/"
    if attestation["evidence"] != generation_manifest["evidence"]:
        raise AssuranceGateError("attestation_generation_evidence_mismatch")
    for item in attestation["evidence"]:
        evidence_reference = item["reference"]
        if not evidence_reference.startswith(profile_prefix):
            raise AssuranceGateError("evidence_reference_profile_namespace_mismatch")
        try:
            evidence_raw = secure_read_relative(
                assurance_root,
                evidence_reference,
                policy=policy,
            )
        except EvidenceTrustError as exc:
            raise AssuranceGateError(f"evidence_{exc.reason}") from exc
        observed_digest = _sha256_bytes(evidence_raw)
        if not hmac.compare_digest(observed_digest, item["sha256"]):
            raise AssuranceGateError("evidence_digest_mismatch")
        try:
            evidence = _load_json_bytes(evidence_raw, label="evidence")
        except AssuranceContractError as exc:
            raise AssuranceGateError("evidence_invalid_json") from exc
        (
            candidate_checkout,
            candidate_runtime,
            candidate_session,
            candidate_worker_execution,
        ) = _verify_evidence_payload(
            evidence,
            profile=profile,
            assurance_root=assurance_root,
            now=now,
            policy=policy,
            expected_checkout=expected_checkout,
        )
        if candidate_checkout is not None:
            if checkout_identity is not None and checkout_identity != candidate_checkout:
                raise AssuranceGateError("multiple_checkout_bindings")
            checkout_identity = candidate_checkout
        if candidate_runtime is not None:
            if runtime_binding is not None and runtime_binding != candidate_runtime:
                raise AssuranceGateError("multiple_runtime_bindings")
            runtime_binding = candidate_runtime
        if (
            worker_execution_binding_seen
            and worker_execution_binding != candidate_worker_execution
        ):
            raise AssuranceGateError("multiple_worker_execution_bindings")
        worker_execution_binding = candidate_worker_execution
        worker_execution_binding_seen = True
        if launch_session_seen and launch_session != candidate_session:
            raise AssuranceGateError("multiple_launch_sessions")
        launch_session = candidate_session
        launch_session_seen = True
        if evidence.get("generation_id") != generation_id:
            raise AssuranceGateError("evidence_generation_mismatch")
        if evidence["bindings"] != attestation_bindings:
            raise AssuranceGateError("evidence_attestation_binding_mismatch")
        key = (evidence["evidence_type"], evidence["claim"], evidence["operation"])
        if key in evidence_keys:
            raise AssuranceGateError("duplicate_evidence_key")
        evidence_keys[key] = evidence["result"]
        evidence_payloads.append(MappingProxyType(evidence))
        evidence_digests.append(observed_digest)
    required = _required_evidence_keys(profile)
    unexpected = set(evidence_keys) - required
    if unexpected:
        raise AssuranceGateError("unexpected_evidence_key")
    claim_results: dict[str, str] = {}
    for claim in profile["target_claims"]:
        required_for_claim = _claim_required_keys(claim)
        claim_results[claim] = (
            "pass"
            if all(evidence_keys.get(key) == "pass" for key in required_for_claim)
            else "fail"
        )
    if claim_results != attestation["claim_results"]:
        raise AssuranceGateError("attestation_claim_result_mismatch")
    _require_profile_promotion_ready(profile)
    if checkout_identity is None:
        raise AssuranceGateError("checkout_binding_missing")
    if runtime_binding is None or not launch_session_seen:
        raise AssuranceGateError("runtime_or_launch_binding_missing")
    if not hmac.compare_digest(
        checkout_identity["identity_digest"],
        attestation_bindings["checkout_digest"],
    ):
        raise AssuranceGateError("checkout_attestation_binding_mismatch")
    if attestation["launch_session"] != launch_session:
        raise AssuranceGateError("attestation_launch_session_mismatch")
    if attestation["runtime_binding"] != runtime_binding:
        raise AssuranceGateError("attestation_runtime_binding_mismatch")
    if attestation["worker_execution_binding"] != worker_execution_binding:
        raise AssuranceGateError("attestation_worker_execution_binding_mismatch")
    expected_launch_digest = (
        launch_session["record_digest"] if launch_session is not None else None
    )
    if generation_manifest["launch_session_digest"] != expected_launch_digest:
        raise AssuranceGateError("generation_launch_session_mismatch")
    return _VerifiedAttestation(
        payload=MappingProxyType(attestation),
        digest=digest,
        evidence=tuple(evidence_payloads),
        evidence_digests=tuple(sorted(evidence_digests)),
        claim_results=MappingProxyType(claim_results),
        checkout_binding=MappingProxyType(checkout_identity),
        launch_session=(
            MappingProxyType(launch_session) if launch_session is not None else None
        ),
        runtime_binding=MappingProxyType(runtime_binding),
        worker_execution_binding=(
            MappingProxyType(worker_execution_binding)
            if worker_execution_binding is not None
            else None
        ),
        generation_id=generation_id,
    )


def _select_profile(registry: Mapping[str, Any], profile_id: str) -> Mapping[str, Any]:
    errors = validate_registry(registry)
    if errors:
        raise AssuranceGateError("registry_invalid", errors)
    for profile in registry["profiles"]:
        if profile["profile_id"] == profile_id:
            return profile
    raise AssuranceGateError("profile_not_found")


def _resolve_claim_verification(
    state_root: Path | str,
    profile_id: str,
    claim: str,
    expected_checkout: Path | str | None = None,
    *,
    registry: Mapping[str, Any] | None = None,
    trust_policy: TrustPolicy | None = None,
    now: datetime | None = None,
) -> tuple[Mapping[str, Any], _VerifiedAttestation]:
    if claim not in CLAIMS:
        raise AssuranceGateError("claim_not_supported")
    selected_registry = registry if registry is not None else load_registry()
    profile = _select_profile(selected_registry, profile_id)
    if claim not in profile["target_claims"]:
        raise AssuranceGateError("claim_not_targeted")
    if profile["integration_state"] != "configured":
        raise AssuranceGateError(f"integration_{profile['integration_state']}")
    root = assurance_root_from_state_root(state_root)
    policy = trust_policy or TrustPolicy.production()
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    checkout = Path(expected_checkout) if expected_checkout is not None else None
    verified = _verify_attestation(
        profile,
        assurance_root=root,
        policy=policy,
        now=current_time,
        expected_checkout=checkout,
    )
    if verified.claim_results.get(claim) != "pass":
        raise AssuranceGateError(f"claim_not_verified:{claim}")
    return profile, verified


def _verify_claim_unbound_informational(
    state_root: Path | str,
    profile_id: str,
    claim: str,
    expected_checkout: Path | str | None = None,
    *,
    registry: Mapping[str, Any] | None = None,
    trust_policy: TrustPolicy | None = None,
    now: datetime | None = None,
) -> _VerifiedAttestation:
    """Revalidate evidence for reporting/sealing without granting authority."""

    _profile, verified = _resolve_claim_verification(
        state_root,
        profile_id,
        claim,
        expected_checkout,
        registry=registry,
        trust_policy=trust_policy,
        now=now,
    )
    return verified


def _expected_live_claim_snapshot(
    verified: _VerifiedAttestation,
    live_context: LiveClaimContext,
) -> tuple[str, _LiveClaimSnapshot]:
    if not isinstance(live_context, LiveClaimContext):
        raise AssuranceGateError("live_claim_context_invalid")
    session = verified.launch_session
    if session is None:
        raise AssuranceGateError("live_claim_context_launch_session_missing")
    expected_context = {
        "subject_pid": session["subject_pid"],
        "process_start_token": session["process_start_token"],
        "supervisor_pid": session["supervisor_pid"],
        "supervisor_start_token": session["supervisor_start_token"],
        "executable_realpath": session["native_realpath"],
        "launch_argv_digest": session["launch_argv_digest"],
        "profile_realpath": session["profile_realpath"],
        "profile_digest": session["profile_digest"],
        "checkout_identity_digest": session["checkout_identity_digest"],
    }
    supplied_context = live_context.as_dict()
    if supplied_context != expected_context:
        raise AssuranceGateError("claim_context_binding_mismatch")
    expected_argv = list(verified.runtime_binding["argv"])
    if not hmac.compare_digest(
        argv_vector_digest(expected_argv), str(session["launch_argv_digest"])
    ):
        raise AssuranceGateError("claim_record_launch_argv_mismatch")
    if session.get("session_kind") == "standard":
        try:
            import codex_main_agent_deployment as deployment

            canonical_argv = deployment.native_codex_argv(
                str(session["native_realpath"])
            )
        except Exception as exc:
            raise AssuranceGateError("claim_expected_argv_unavailable") from exc
        if expected_argv != canonical_argv:
            raise AssuranceGateError("claim_record_launch_argv_mismatch")
    expected_snapshot = _LiveClaimSnapshot(
        process_start_token=live_context.process_start_token,
        supervisor_start_token=live_context.supervisor_start_token,
        parent_pid=live_context.supervisor_pid,
        executable_realpath=live_context.executable_realpath,
        argv_digest=argv_vector_digest(expected_argv),
    )
    return _stable_digest(supplied_context), expected_snapshot


def _require_live_claim_snapshots(
    expected: _LiveClaimSnapshot,
    first: _LiveClaimSnapshot,
    second: _LiveClaimSnapshot,
) -> None:
    """Require both complete reads to equal each other and the trusted record."""

    if not isinstance(first, _LiveClaimSnapshot) or not isinstance(
        second, _LiveClaimSnapshot
    ):
        raise AssuranceGateError("claim_live_identity_unavailable")
    if any(
        not hmac.compare_digest(
            snapshot.process_start_token, expected.process_start_token
        )
        or not hmac.compare_digest(
            snapshot.supervisor_start_token, expected.supervisor_start_token
        )
        for snapshot in (first, second)
    ):
        raise AssuranceGateError("claim_live_start_token_mismatch")
    if any(snapshot.parent_pid != expected.parent_pid for snapshot in (first, second)):
        raise AssuranceGateError("claim_live_parent_mismatch")
    if any(
        snapshot.executable_realpath != expected.executable_realpath
        for snapshot in (first, second)
    ):
        raise AssuranceGateError("claim_live_executable_mismatch")
    if any(
        not hmac.compare_digest(snapshot.argv_digest, expected.argv_digest)
        for snapshot in (first, second)
    ):
        raise AssuranceGateError("claim_live_argv_mismatch")


def require_claim(
    state_root: Path | str,
    profile_id: str,
    claim: str,
    expected_checkout: Path | str | None = None,
    *,
    live_context: LiveClaimContext,
    registry: Mapping[str, Any] | None = None,
    trust_policy: TrustPolicy | None = None,
    now: datetime | None = None,
) -> VerifiedClaim:
    """Return authority only when attestation, caller context, and OS agree.

    ``live_context`` is deliberately keyword-only and has no default.  A
    context-free caller can obtain informational verification only through the
    private reporting/sealing route; it can never receive a ``VerifiedClaim``.
    """

    if not isinstance(live_context, LiveClaimContext):
        # Preserve the stronger profile-level suppression reason (notably the
        # managed-worker promotion blocker) without ever constructing an
        # authority result.  A valid context takes the snapshot-first path.
        _resolve_claim_verification(
            state_root,
            profile_id,
            claim,
            expected_checkout,
            registry=registry,
            trust_policy=trust_policy,
            now=now,
        )
        raise AssuranceGateError("live_claim_context_invalid")
    first_live_snapshot = _capture_live_claim_snapshot(live_context)
    profile, verified = _resolve_claim_verification(
        state_root,
        profile_id,
        claim,
        expected_checkout,
        registry=registry,
        trust_policy=trust_policy,
        now=now,
    )
    subject_binding_digest, expected_live_snapshot = _expected_live_claim_snapshot(
        verified, live_context
    )
    verified_profile_subject_digest = profile_subject_digest(profile)
    verified_bindings = MappingProxyType(dict(verified.payload["bindings"]))
    second_live_snapshot = _capture_live_claim_snapshot(live_context)
    _require_live_claim_snapshots(
        expected_live_snapshot, first_live_snapshot, second_live_snapshot
    )
    return VerifiedClaim(
        profile_id=profile_id,
        claim=claim,
        attestation_digest=verified.digest,
        profile_subject_digest=verified_profile_subject_digest,
        bindings=verified_bindings,
        evidence_digests=verified.evidence_digests,
        checkout_binding=verified.checkout_binding,
        launch_session=verified.launch_session,
        runtime_binding=verified.runtime_binding,
        worker_execution_binding=verified.worker_execution_binding,
        generation_id=verified.generation_id,
        subject_binding_digest=subject_binding_digest,
    )


def evaluate_profile(
    profile: Mapping[str, Any],
    *,
    state_root: Path | str | None = None,
    registry: Mapping[str, Any] | None = None,
    trust_policy: TrustPolicy | None = None,
    now: datetime | None = None,
    expected_checkout: Path | str | None = None,
) -> dict[str, Any]:
    """Informational evaluation; this never grants authority by itself."""

    targets = list(profile["target_claims"])
    base = {
        "profile_id": profile["profile_id"],
        "agent_family": profile["agent_family"],
        "surface_role": profile["surface_role"],
        "integration_state": profile["integration_state"],
        "target_claims": targets,
        "profile_subject_digest": profile_subject_digest(profile),
    }
    if not targets:
        allowed = profile["integration_state"] == "available_advisory"
        return {
            **base,
            "decision": "allow" if allowed else "suppress",
            "effective_mode": "advisory" if allowed else None,
            "claim_results": {},
            "effective_claims": [],
            "attestation_digest": None,
            "bindings": None,
            "reasons": [] if allowed else [f"integration_{profile['integration_state']}"],
        }
    if state_root is None:
        root: Path | str = production_assurance_root()
    else:
        root = state_root
    claim_results: dict[str, str] = {}
    reasons: list[str] = []
    verified_records: list[_VerifiedAttestation] = []
    for claim in targets:
        try:
            record = _verify_claim_unbound_informational(
                root,
                profile["profile_id"],
                claim,
                expected_checkout,
                registry=registry,
                trust_policy=trust_policy,
                now=now,
            )
        except (AssuranceGateError, AssuranceContractError) as exc:
            claim_results[claim] = "suppress"
            if isinstance(exc, AssuranceGateError):
                reasons.extend(f"{claim}:{reason}" for reason in exc.reasons)
            else:
                reasons.append(f"{claim}:registry_unavailable")
        else:
            claim_results[claim] = "pass"
            verified_records.append(record)
    effective = [claim for claim in targets if claim_results.get(claim) == "pass"]
    all_allowed = len(effective) == len(targets)
    first_record = verified_records[0] if verified_records else None
    return {
        **base,
        "decision": "allow" if all_allowed else "suppress",
        "effective_mode": None,
        "claim_results": claim_results,
        "effective_claims": effective,
        "attestation_digest": first_record.digest if first_record else None,
        "bindings": (
            dict(first_record.payload["bindings"]) if first_record else None
        ),
        "reasons": sorted(set(reasons)),
    }


def evaluate_registry(
    registry: Mapping[str, Any],
    *,
    profile_id: str | None = None,
    state_root: Path | str | None = None,
    trust_policy: TrustPolicy | None = None,
    now: datetime | None = None,
    expected_checkout: Path | str | None = None,
) -> dict[str, Any]:
    errors = validate_registry(registry)
    if errors:
        return {
            "contract_version": "2",
            "decision": "blocked",
            "errors": errors,
            "profiles": [],
        }
    profiles = list(registry["profiles"])
    if profile_id is not None:
        profiles = [profile for profile in profiles if profile["profile_id"] == profile_id]
        if not profiles:
            return {
                "contract_version": "2",
                "decision": "blocked",
                "errors": [f"profile_not_found:{profile_id}"],
                "profiles": [],
            }
    evaluations = [
        evaluate_profile(
            profile,
            state_root=state_root,
            registry=registry,
            trust_policy=trust_policy,
            now=now,
            expected_checkout=expected_checkout,
        )
        for profile in profiles
    ]
    return {
        "contract_version": "2",
        "decision": evaluations[0]["decision"] if profile_id is not None else "evaluated",
        "failure_policy": dict(registry["failure_policy"]),
        "profiles": evaluations,
        "errors": [],
    }


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate/report/require host-evidenced agent assurance"
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the static target registry")
    for name in ("report", "evaluate"):
        report_parser = subparsers.add_parser(
            name,
            help="Informational assurance report" + (" (compatibility alias)" if name == "evaluate" else ""),
        )
        report_parser.add_argument("--profile")
    require_parser = subparsers.add_parser(
        "require",
        help="Informational claim diagnostic (never returns process authority)",
    )
    require_parser.add_argument("--profile", required=True)
    require_parser.add_argument("--claim", action="append", required=True, choices=CLAIMS)
    require_parser.add_argument("--expected-checkout", type=Path)
    args = parser.parse_args()
    try:
        registry = load_registry(args.registry)
    except AssuranceContractError as exc:
        _print({"contract_version": "2", "decision": "blocked", "errors": [str(exc)]})
        raise SystemExit(2) from exc
    if args.command == "validate":
        errors = validate_registry(registry)
        _print(
            {
                "contract_version": "2",
                "decision": "ok" if not errors else "blocked",
                "registry_path": str(args.registry),
                "schema_path": str(SCHEMA_PATH),
                "evidence_schema_path": str(EVIDENCE_SCHEMA_PATH),
                "attestation_schema_path": str(ATTESTATION_SCHEMA_PATH),
                "profile_count": len(registry.get("profiles", [])),
                "errors": errors,
            }
        )
        if errors:
            raise SystemExit(2)
        return
    if args.command in {"report", "evaluate"}:
        # Production CLI always resolves the compile-time admin-owned root and
        # remains informational even when every target is suppressed.
        _print(
            evaluate_registry(
                registry,
                profile_id=args.profile,
                state_root=production_assurance_root(),
            )
        )
        return
    try:
        profile = _select_profile(registry, args.profile)
        evaluation = evaluate_profile(
            profile,
            state_root=production_assurance_root(),
            registry=registry,
            expected_checkout=args.expected_checkout,
        )
        failed = [
            claim
            for claim in args.claim
            if evaluation["claim_results"].get(claim) != "pass"
        ]
        if failed:
            raise AssuranceGateError(
                "informational_claim_not_verified",
                evaluation.get("reasons") or failed,
            )
    except (AssuranceGateError, AssuranceContractError) as exc:
        reasons = list(exc.reasons) if isinstance(exc, AssuranceGateError) else [str(exc)]
        _print(
            {
                "contract_version": "2",
                "decision": "suppress",
                "profile_id": args.profile,
                "claims": args.claim,
                "reasons": reasons,
            }
        )
        raise SystemExit(2) from exc
    _print(
        {
            "contract_version": "2",
            "decision": "informational_verified",
            "authority": "none",
            "profile_id": args.profile,
            "claims": args.claim,
            "attestation_digest": evaluation["attestation_digest"],
            "bindings": evaluation["bindings"],
        }
    )


if __name__ == "__main__":
    main()
