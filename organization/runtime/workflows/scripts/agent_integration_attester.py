#!/usr/bin/env python3
"""Seal host-owned integration evidence into a short-lived attestation.

This tool creates no credential or signing key.  Its authority is the
administrator-owned, non-writable filesystem boundary validated by
``agent_integration_assurance``.  Production CLI operation always uses the
fixed ``/Library/Application Support/Saihai/Assurance`` root; tests use the
library functions with an explicit fixture policy.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import agent_integration_assurance as assurance


def _generation_reference(profile_id: str, generation_id: str) -> str:
    for value in (profile_id, generation_id):
        if not assurance.SAFE_ID.fullmatch(value):
            raise assurance.AssuranceGateError("generation_identity_invalid")
    return f"generations/{profile_id}/{generation_id}"


def discover_evidence_references(
    state_root: Path | str,
    profile_id: str,
    *,
    trust_policy: assurance.TrustPolicy,
) -> tuple[str, ...]:
    """Refuse legacy directory discovery.

    A normal gate must select exactly one generation manifest.  Scanning a
    profile directory could accidentally mix stale or partially renewed
    evidence, so callers must use :func:`load_generation_manifest` instead.
    """

    del state_root, profile_id, trust_policy
    raise assurance.AssuranceGateError("evidence_discovery_forbidden")


def load_generation_manifest(
    state_root: Path | str,
    profile_id: str,
    generation_id: str,
    *,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
) -> tuple[dict[str, Any], bytes, str]:
    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, profile_id)
    reference = _generation_reference(profile_id, generation_id) + "/manifest.json"
    try:
        raw = assurance.secure_read_relative(root, reference, policy=policy)
        value = assurance._load_json_bytes(raw, label="generation_manifest")
    except (assurance.EvidenceTrustError, assurance.AssuranceContractError) as exc:
        raise assurance.AssuranceGateError("generation_manifest_unavailable") from exc
    manifest = assurance._validate_generation_manifest(
        value, profile=profile, generation_id=generation_id
    )
    epoch = assurance._require_commissionable_deployment_epoch(
        root,
        profile_id,
        policy=policy,
    )
    if not hmac.compare_digest(
        str(manifest["deployment_epoch_id"]), str(epoch["epoch_id"])
    ):
        raise assurance.AssuranceGateError("deployment_epoch_mismatch")
    return manifest, raw, reference


def _encoded_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    trust_policy: assurance.TrustPolicy,
    replace: bool,
) -> Path:
    assurance._validate_root_chain(path.parent, trust_policy)
    if os.geteuid() not in trust_policy.expected_owner_uids:
        raise assurance.AssuranceGateError("attester_process_owner_not_authorized")
    encoded = _encoded_json(payload)
    if len(encoded) > trust_policy.max_json_bytes:
        raise assurance.AssuranceGateError("attester_artifact_too_large")
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise assurance.AssuranceGateError("generation_artifact_already_exists") from exc
            temporary_path.unlink()
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except assurance.AssuranceGateError:
        raise
    except OSError as exc:
        raise assurance.AssuranceGateError("attester_artifact_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return path


def create_generation_manifest(
    state_root: Path | str,
    profile_id: str,
    generation_id: str,
    commissioning_id: str,
    evidence_references: Sequence[str],
    *,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze one exact evidence set; no directory discovery is performed."""

    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, profile_id)
    deployment_epoch = assurance._require_commissionable_deployment_epoch(
        root,
        profile_id,
        policy=policy,
    )
    for value in (generation_id, commissioning_id):
        if not isinstance(value, str) or not assurance.SAFE_ID.fullmatch(value):
            raise assurance.AssuranceGateError("generation_identity_invalid")
    if not evidence_references or len(set(evidence_references)) != len(evidence_references):
        raise assurance.AssuranceGateError("generation_evidence_references_invalid")
    prefix = _generation_reference(profile_id, generation_id) + "/evidence/"
    references: list[dict[str, str]] = []
    launch_session: dict[str, Any] | None = None
    launch_session_seen = False
    for reference in sorted(evidence_references):
        if not reference.startswith(prefix):
            raise assurance.AssuranceGateError("generation_evidence_namespace_mismatch")
        try:
            raw = assurance.secure_read_relative(root, reference, policy=policy)
            evidence = assurance._load_json_bytes(raw, label="evidence")
        except (assurance.EvidenceTrustError, assurance.AssuranceContractError) as exc:
            raise assurance.AssuranceGateError("generation_evidence_unavailable") from exc
        if (
            evidence.get("profile_id") != profile_id
            or evidence.get("generation_id") != generation_id
        ):
            raise assurance.AssuranceGateError("generation_evidence_identity_mismatch")
        try:
            candidate_session = assurance._validate_launch_session_shape(
                evidence.get("launch_session"), profile
            )
        except assurance.AssuranceContractError as exc:
            raise assurance.AssuranceGateError("generation_launch_session_invalid") from exc
        if launch_session_seen and launch_session != candidate_session:
            raise assurance.AssuranceGateError("generation_multiple_launch_sessions")
        launch_session = candidate_session
        launch_session_seen = True
        references.append(
            {"reference": reference, "sha256": assurance._sha256_bytes(raw)}
        )
    if not launch_session_seen:
        raise assurance.AssuranceGateError("generation_launch_session_missing")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    seed = {
        "profile_id": profile_id,
        "generation_id": generation_id,
        "commissioning_id": commissioning_id,
        "deployment_epoch_id": deployment_epoch["epoch_id"],
        "evidence": references,
    }
    manifest: dict[str, Any] = {
        "manifest_version": "1",
        "manifest_id": "manifest-"
        + assurance._stable_digest(seed).removeprefix("sha256:")[:24],
        "profile_id": profile_id,
        "generation_id": generation_id,
        "commissioning_id": commissioning_id,
        "deployment_epoch_id": deployment_epoch["epoch_id"],
        "created_at": assurance.format_timestamp(current),
        "registry_subject_digest": assurance.profile_subject_digest(profile),
        "launch_session_digest": (
            launch_session["record_digest"] if launch_session is not None else None
        ),
        "evidence": references,
        "manifest_digest": "sha256:" + "0" * 64,
    }
    manifest["manifest_digest"] = assurance._stable_digest(
        assurance._generation_manifest_material(manifest)
    )
    assurance._validate_generation_manifest(
        manifest, profile=profile, generation_id=generation_id
    )
    target = root / _generation_reference(profile_id, generation_id) / "manifest.json"
    _atomic_write_json(target, manifest, trust_policy=policy, replace=False)
    return {
        "manifest": manifest,
        "reference": str(target.relative_to(root)),
        "sha256": assurance._sha256_bytes(target.read_bytes()),
    }


def build_attestation(
    state_root: Path | str,
    profile_id: str,
    generation_id: str,
    *,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    expected_checkout: Path | str | None = None,
) -> dict[str, Any]:
    """Verify evidence and derive an attestation payload without writing it."""

    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, profile_id)
    if not profile["target_claims"]:
        raise assurance.AssuranceGateError("advisory_profile_has_no_attestation")
    if profile["integration_state"] != "configured":
        raise assurance.AssuranceGateError(f"integration_{profile['integration_state']}")
    root = assurance.assurance_root_from_state_root(state_root)
    policy = trust_policy or assurance.TrustPolicy.production()
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    checkout = Path(expected_checkout) if expected_checkout is not None else None
    manifest, manifest_raw, manifest_reference = load_generation_manifest(
        root,
        profile_id,
        generation_id,
        registry=selected_registry,
        trust_policy=policy,
    )
    evidence_references = [item["reference"] for item in manifest["evidence"]]
    manifest_evidence = {item["reference"]: item["sha256"] for item in manifest["evidence"]}
    expected_prefix = f"generations/{profile_id}/{generation_id}/evidence/"

    references: list[dict[str, str]] = []
    evidence_keys: dict[tuple[str, str, str | None], str] = {}
    common_bindings: dict[str, str] | None = None
    checkout_identity: dict[str, str] | None = None
    earliest_expiration: datetime | None = None
    launch_session: dict[str, Any] | None = None
    launch_session_seen = False
    runtime_binding: dict[str, Any] | None = None
    worker_execution_binding: dict[str, Any] | None = None
    worker_execution_binding_seen = False

    for reference in sorted(evidence_references):
        if not reference.startswith(expected_prefix):
            raise assurance.AssuranceGateError("evidence_reference_profile_namespace_mismatch")
        try:
            raw = assurance.secure_read_relative(root, reference, policy=policy)
        except assurance.EvidenceTrustError as exc:
            raise assurance.AssuranceGateError(f"evidence_{exc.reason}") from exc
        digest = assurance._sha256_bytes(raw)
        if not hmac.compare_digest(digest, manifest_evidence[reference]):
            raise assurance.AssuranceGateError("generation_evidence_digest_mismatch")
        try:
            evidence = assurance._load_json_bytes(raw, label="evidence")
        except assurance.AssuranceContractError as exc:
            raise assurance.AssuranceGateError("evidence_invalid_json") from exc
        (
            candidate_checkout,
            candidate_runtime,
            candidate_session,
            candidate_worker_execution,
        ) = assurance._verify_evidence_payload(
            evidence,
            profile=profile,
            assurance_root=root,
            now=current_time,
            policy=policy,
            expected_checkout=checkout,
        )
        bindings = assurance._validate_bindings(
            evidence["bindings"],
            label="evidence_bindings",
        )
        if common_bindings is None:
            common_bindings = bindings
        elif common_bindings != bindings:
            raise assurance.AssuranceGateError("evidence_binding_mismatch")
        if candidate_checkout is not None:
            if checkout_identity is not None and checkout_identity != candidate_checkout:
                raise assurance.AssuranceGateError("multiple_checkout_bindings")
            checkout_identity = candidate_checkout
        if candidate_runtime is not None:
            if runtime_binding is not None and runtime_binding != candidate_runtime:
                raise assurance.AssuranceGateError("multiple_runtime_bindings")
            runtime_binding = candidate_runtime
        if (
            worker_execution_binding_seen
            and worker_execution_binding != candidate_worker_execution
        ):
            raise assurance.AssuranceGateError(
                "multiple_worker_execution_bindings"
            )
        worker_execution_binding = candidate_worker_execution
        worker_execution_binding_seen = True
        if launch_session_seen and launch_session != candidate_session:
            raise assurance.AssuranceGateError("multiple_launch_sessions")
        launch_session = candidate_session
        launch_session_seen = True
        if evidence.get("generation_id") != generation_id:
            raise assurance.AssuranceGateError("evidence_generation_mismatch")
        key = (evidence["evidence_type"], evidence["claim"], evidence["operation"])
        if key in evidence_keys:
            raise assurance.AssuranceGateError("duplicate_evidence_key")
        evidence_keys[key] = evidence["result"]
        expiration = assurance._parse_timestamp(
            evidence["valid_until"],
            label="valid_until",
        )
        earliest_expiration = (
            expiration
            if earliest_expiration is None
            else min(earliest_expiration, expiration)
        )
        references.append({"reference": reference, "sha256": digest})

    required = assurance._required_evidence_keys(profile)
    if set(evidence_keys) - required:
        raise assurance.AssuranceGateError("unexpected_evidence_key")
    claim_results: dict[str, str] = {}
    missing_reasons: list[str] = []
    for claim in profile["target_claims"]:
        claim_keys = assurance._claim_required_keys(claim)
        missing = sorted(key for key in claim_keys if evidence_keys.get(key) != "pass")
        claim_results[claim] = "fail" if missing else "pass"
        missing_reasons.extend(
            f"{claim}:evidence_not_passed:{kind}:{operation or 'none'}"
            for kind, _claim, operation in missing
        )
    if missing_reasons:
        raise assurance.AssuranceGateError("claim_evidence_incomplete", missing_reasons)
    assurance._require_profile_promotion_ready(profile)
    if (
        common_bindings is None
        or checkout_identity is None
        or earliest_expiration is None
        or not launch_session_seen
        or runtime_binding is None
    ):
        raise assurance.AssuranceGateError("common_evidence_incomplete")
    if common_bindings["checkout_digest"] != checkout_identity["identity_digest"]:
        raise assurance.AssuranceGateError("checkout_binding_digest_mismatch")
    expected_launch_digest = (
        launch_session["record_digest"] if launch_session is not None else None
    )
    if manifest["launch_session_digest"] != expected_launch_digest:
        raise assurance.AssuranceGateError("generation_launch_session_mismatch")

    max_expiration = current_time + timedelta(
        seconds=profile["evidence_policy"]["max_age_seconds"]
    )
    valid_until = min(earliest_expiration, max_expiration)
    seed = {
        "profile_id": profile_id,
        "generation_id": generation_id,
        "issued_at": assurance.format_timestamp(current_time),
        "references": references,
    }
    return {
        "attestation_version": "2",
        "attestation_id": "att-" + assurance._stable_digest(seed).removeprefix("sha256:")[:24],
        "generation_id": generation_id,
        "profile_id": profile_id,
        "issued_at": assurance.format_timestamp(current_time),
        "valid_until": assurance.format_timestamp(valid_until),
        "registry_subject_digest": assurance.profile_subject_digest(profile),
        "target_claims": list(profile["target_claims"]),
        "claim_results": claim_results,
        "bindings": common_bindings,
        "launch_session": launch_session,
        "runtime_binding": runtime_binding,
        "worker_execution_binding": worker_execution_binding,
        "generation_manifest": {
            "reference": manifest_reference,
            "sha256": assurance._sha256_bytes(manifest_raw),
        },
        "evidence": references,
    }


def _atomic_write_attestation(
    assurance_root: Path,
    profile_id: str,
    generation_id: str,
    payload: Mapping[str, Any],
    *,
    trust_policy: assurance.TrustPolicy,
) -> Path:
    target = (
        assurance_root
        / _generation_reference(profile_id, generation_id)
        / "attestation.json"
    )
    return _atomic_write_json(
        target, payload, trust_policy=trust_policy, replace=False
    )


def seal_attestation(
    state_root: Path | str,
    profile_id: str,
    generation_id: str,
    *,
    registry: Mapping[str, Any] | None = None,
    trust_policy: assurance.TrustPolicy | None = None,
    now: datetime | None = None,
    expected_checkout: Path | str | None = None,
) -> dict[str, Any]:
    """Seal one immutable generation, then atomically activate only that generation."""

    policy = trust_policy or assurance.TrustPolicy.production()
    root = assurance.assurance_root_from_state_root(state_root)
    payload = build_attestation(
        root,
        profile_id,
        generation_id,
        registry=registry,
        trust_policy=policy,
        now=now,
        expected_checkout=expected_checkout,
    )
    errors = assurance.validate_attestation_payload(payload)
    if errors:
        raise assurance.AssuranceGateError("generated_attestation_invalid", errors)
    manifest, manifest_raw, manifest_reference = load_generation_manifest(
        root,
        profile_id,
        generation_id,
        registry=registry,
        trust_policy=policy,
    )
    try:
        import agent_integration_observer as observer

        observer.require_completed_commissioning(
            root,
            profile_id=profile_id,
            generation_id=generation_id,
            commissioning_id=manifest["commissioning_id"],
            generation_manifest_digest=manifest["manifest_digest"],
            trust_policy=policy,
            now=now,
        )
    except assurance.AssuranceGateError:
        raise
    except Exception as exc:
        raise assurance.AssuranceGateError("commissioning_completion_unverifiable") from exc
    attestation_path = (
        root
        / _generation_reference(profile_id, generation_id)
        / "attestation.json"
    )
    if attestation_path.exists():
        try:
            existing_raw = assurance.secure_read_relative(
                root, str(attestation_path.relative_to(root)), policy=policy
            )
        except assurance.EvidenceTrustError as exc:
            raise assurance.AssuranceGateError(
                "existing_attestation_untrusted"
            ) from exc
        if existing_raw != _encoded_json(payload):
            raise assurance.AssuranceGateError("existing_attestation_mismatch")
    else:
        attestation_path = _atomic_write_attestation(
            root,
            profile_id,
            generation_id,
            payload,
            trust_policy=policy,
        )
    raw = assurance.secure_read_relative(
        root, str(attestation_path.relative_to(root)), policy=policy
    )
    if raw != _encoded_json(payload):
        raise assurance.AssuranceGateError("attestation_post_write_mismatch")

    selected_registry = registry if registry is not None else assurance.load_registry()
    profile = assurance._select_profile(selected_registry, profile_id)

    def informational_claim_records() -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for claim in profile["target_claims"]:
            verified = assurance._verify_claim_unbound_informational(
                root,
                profile_id,
                claim,
                expected_checkout,
                registry=selected_registry,
                trust_policy=policy,
                now=now,
            )
            records.append(
                {
                    "authority": "informational_only",
                    "profile_id": profile_id,
                    "claim": claim,
                    "attestation_digest": verified.digest,
                    "profile_subject_digest": assurance.profile_subject_digest(
                        profile
                    ),
                    "bindings": dict(verified.payload["bindings"]),
                    "evidence_digests": list(verified.evidence_digests),
                    "checkout_binding": dict(verified.checkout_binding),
                    "launch_session": (
                        dict(verified.launch_session)
                        if verified.launch_session is not None
                        else None
                    ),
                    "runtime_binding": dict(verified.runtime_binding),
                    "worker_execution_binding": (
                        dict(verified.worker_execution_binding)
                        if verified.worker_execution_binding is not None
                        else None
                    ),
                    "generation_id": verified.generation_id,
                }
            )
        return records

    active_dir = root / "active"
    assurance._validate_root_chain(active_dir, policy)
    active_path = active_dir / f"{profile_id}.json"
    lock_path = active_dir / f".{profile_id}.lock"
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    previous_raw: bytes | None = None
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if active_path.exists():
            previous_raw = assurance.secure_read_relative(
                root, str(active_path.relative_to(root)), policy=policy
            )
            try:
                previous = assurance._load_json_bytes(
                    previous_raw, label="previous_active"
                )
            except assurance.AssuranceContractError as exc:
                raise assurance.AssuranceGateError("previous_active_invalid") from exc
            previous_generation = previous.get("generation_id")
            if previous_generation == generation_id:
                expected_attestation = {
                    "reference": str(attestation_path.relative_to(root)),
                    "sha256": assurance._sha256_bytes(raw),
                }
                if previous.get("attestation") != expected_attestation:
                    raise assurance.AssuranceGateError(
                        "active_generation_attestation_mismatch"
                    )
                records = informational_claim_records()
                return {"attestation": payload, "verified_claims": records}
        else:
            previous_generation = None
        activated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        active_seed = {
            "profile_id": profile_id,
            "generation_id": generation_id,
            "attestation_sha256": assurance._sha256_bytes(raw),
        }
        active_payload = {
            "active_version": "1",
            "activation_id": "active-"
            + assurance._stable_digest(active_seed).removeprefix("sha256:")[:24],
            "profile_id": profile_id,
            "generation_id": generation_id,
            "activated_at": assurance.format_timestamp(activated_at),
            "previous_generation_id": previous_generation,
            "generation_manifest": {
                "reference": manifest_reference,
                "sha256": assurance._sha256_bytes(manifest_raw),
            },
            "attestation": {
                "reference": str(attestation_path.relative_to(root)),
                "sha256": assurance._sha256_bytes(raw),
            },
        }
        _atomic_write_json(
            active_path, active_payload, trust_policy=policy, replace=True
        )
        try:
            records = informational_claim_records()
        except Exception:
            if previous_raw is None:
                try:
                    active_path.unlink()
                except OSError:
                    pass
            else:
                previous_payload = assurance._load_json_bytes(
                    previous_raw, label="previous_active"
                )
                _atomic_write_json(
                    active_path,
                    previous_payload,
                    trust_policy=policy,
                    replace=True,
                )
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return {"attestation": payload, "verified_claims": records}


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Seal admin-owned agent assurance evidence")
    parser.add_argument("--registry", type=Path, default=assurance.DEFAULT_REGISTRY_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    seal_parser = subparsers.add_parser("seal", help="Seal all evidence for one profile")
    seal_parser.add_argument("--profile", required=True)
    seal_parser.add_argument("--generation", required=True)
    seal_parser.add_argument("--expected-checkout", type=Path)
    args = parser.parse_args()
    try:
        registry = assurance.load_registry(args.registry)
        result = seal_attestation(
            assurance.production_assurance_root(),
            args.profile,
            args.generation,
            registry=registry,
            expected_checkout=args.expected_checkout,
        )
    except (
        assurance.AssuranceContractError,
        assurance.AssuranceGateError,
        assurance.EvidenceTrustError,
    ) as exc:
        reasons = (
            list(exc.reasons)
            if isinstance(exc, assurance.AssuranceGateError)
            else [str(exc)]
        )
        _print({"decision": "suppress", "profile_id": args.profile, "reasons": reasons})
        raise SystemExit(2) from exc
    _print({"decision": "sealed", **result})


if __name__ == "__main__":
    main()
