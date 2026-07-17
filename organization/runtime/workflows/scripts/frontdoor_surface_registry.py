#!/usr/bin/env python3
"""Typed, data-driven frontend surface registration for the frontdoor.

The registry joins three static contracts without granting authority:

* the bridge submit contract a surface conforms to;
* the launcher and requirements descriptors owned by that surface; and
* the existing host-evidenced assurance profile.

An effective surface identity is informational.  Missing, stale, invalid, or
uncommissioned assurance always resolves to ``advisory`` with claims
suppressed.  Runtime authority continues to come only from
``agent_integration_assurance.require_claim``.
"""

from __future__ import annotations

import json
import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = WORKFLOW_ROOT / "schemas" / "frontdoor-surface-registry.schema.json"
DEFAULT_REGISTRY_PATH = WORKFLOW_ROOT / "profiles" / "frontdoor-surface-registry.json"

SUBMIT_CONTRACT_ID = "main-agent-bridge-submit"
SUBMIT_CONTRACT_VERSION = "1"
ASSURANCE_STATES = ("advisory", "ingress_enforced", "action_enforced")
ASSURANCE_CLAIMS = frozenset(ASSURANCE_STATES[1:])
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
SAFE_MODULE = re.compile(r"^[a-z][a-z0-9_.]{0,95}$")
SAFE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,95}$")

ROOT_FIELDS = {"registry_version", "surfaces"}
DESCRIPTOR_FIELDS = {
    "descriptor_version",
    "frontend_kind",
    "submit_contract",
    "launcher",
    "requirements",
    "assurance",
}
SUBMIT_FIELDS = {"contract_id", "contract_version"}
LAUNCHER_FIELDS = {
    "launcher_id",
    "launch_session_required",
    "verifier_module",
    "verifier_class",
}
REQUIREMENTS_FIELDS = {"requirements_id"}
ASSURANCE_FIELDS = {"profile_id", "target_state"}


class SurfaceRegistryError(ValueError):
    """A surface descriptor, identity, or registration is invalid."""


AssuranceEvaluator = Callable[[str, Optional[Path]], Mapping[str, Any]]


@dataclass(frozen=True)
class SurfaceDescriptor:
    """Static registration for one frontend kind."""

    frontend_kind: str
    submit_contract_id: str
    submit_contract_version: str
    launcher_id: str
    launch_session_required: bool
    verifier_module: str | None
    verifier_class: str | None
    requirements_id: str
    assurance_profile_id: str | None
    target_assurance_state: str


@dataclass(frozen=True)
class SurfaceIdentity:
    """Host-derived identity carried by ingress and bridge artifacts."""

    frontend_kind: str
    assurance_state: str
    target_assurance_state: str
    assurance_profile_id: str | None
    commissioned_claims: tuple[str, ...]
    suppressed_claims: tuple[str, ...]
    submit_contract_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity_version": "1",
            "frontend_kind": self.frontend_kind,
            "assurance_state": self.assurance_state,
            "target_assurance_state": self.target_assurance_state,
            "assurance_profile_id": self.assurance_profile_id,
            "commissioned_claims": list(self.commissioned_claims),
            "suppressed_claims": list(self.suppressed_claims),
            "submit_contract_version": self.submit_contract_version,
        }


def _exact_fields(value: Any, expected: set[str], path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}:type_object"]
    errors = [f"{path}.{key}:required" for key in sorted(expected - set(value))]
    errors.extend(f"{path}.{key}:unexpected" for key in sorted(set(value) - expected))
    return errors


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(SAFE_ID.fullmatch(value))


def _profile_index(assurance_registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    profiles = assurance_registry.get("profiles")
    if not isinstance(profiles, list):
        return {}
    return {
        str(profile.get("profile_id")): profile
        for profile in profiles
        if isinstance(profile, dict) and isinstance(profile.get("profile_id"), str)
    }


def _target_state(target_claims: Any) -> str:
    claims = set(target_claims) if isinstance(target_claims, list) else set()
    if "action_enforced" in claims:
        return "action_enforced"
    if "ingress_enforced" in claims:
        return "ingress_enforced"
    return "advisory"


def validate_registry(
    value: Any,
    assurance_registry: Mapping[str, Any],
) -> list[str]:
    """Return deterministic descriptor and assurance-link errors."""

    errors = _exact_fields(value, ROOT_FIELDS, "$")
    if not isinstance(value, dict):
        return errors
    if value.get("registry_version") != "1":
        errors.append("$.registry_version:const_1")
    profiles = _profile_index(assurance_registry)
    surfaces = value.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        errors.append("$.surfaces:nonempty_array")
        return sorted(set(errors))
    kinds: set[str] = set()
    for index, descriptor in enumerate(surfaces):
        path = f"$.surfaces[{index}]"
        descriptor_errors = _exact_fields(descriptor, DESCRIPTOR_FIELDS, path)
        errors.extend(descriptor_errors)
        if not isinstance(descriptor, dict):
            continue
        if descriptor.get("descriptor_version") != "1":
            errors.append(f"{path}.descriptor_version:const_1")
        frontend_kind = descriptor.get("frontend_kind")
        if not _safe_id(frontend_kind):
            errors.append(f"{path}.frontend_kind:safe_id")
        elif frontend_kind in kinds:
            errors.append(f"{path}.frontend_kind:duplicate")
        else:
            kinds.add(frontend_kind)

        submit = descriptor.get("submit_contract")
        errors.extend(_exact_fields(submit, SUBMIT_FIELDS, f"{path}.submit_contract"))
        if isinstance(submit, dict):
            if submit.get("contract_id") != SUBMIT_CONTRACT_ID:
                errors.append(f"{path}.submit_contract.contract_id:unsupported")
            if submit.get("contract_version") != SUBMIT_CONTRACT_VERSION:
                errors.append(f"{path}.submit_contract.contract_version:unsupported")

        launcher = descriptor.get("launcher")
        errors.extend(_exact_fields(launcher, LAUNCHER_FIELDS, f"{path}.launcher"))
        if isinstance(launcher, dict):
            if not _safe_id(launcher.get("launcher_id")):
                errors.append(f"{path}.launcher.launcher_id:safe_id")
            if not isinstance(launcher.get("launch_session_required"), bool):
                errors.append(f"{path}.launcher.launch_session_required:type_boolean")
            verifier_module = launcher.get("verifier_module")
            verifier_class = launcher.get("verifier_class")
            for key, item, pattern in (
                ("verifier_module", verifier_module, SAFE_MODULE),
                ("verifier_class", verifier_class, SAFE_SYMBOL),
            ):
                if item is not None and (
                    not isinstance(item, str) or not pattern.fullmatch(item)
                ):
                    errors.append(f"{path}.launcher.{key}:safe_id_or_null")
            if launcher.get("launch_session_required") and (
                verifier_module is None or verifier_class is None
            ):
                errors.append(f"{path}.launcher:verifier_required")

        requirements = descriptor.get("requirements")
        errors.extend(
            _exact_fields(requirements, REQUIREMENTS_FIELDS, f"{path}.requirements")
        )
        if isinstance(requirements, dict) and not _safe_id(
            requirements.get("requirements_id")
        ):
            errors.append(f"{path}.requirements.requirements_id:safe_id")

        assurance = descriptor.get("assurance")
        errors.extend(_exact_fields(assurance, ASSURANCE_FIELDS, f"{path}.assurance"))
        if not isinstance(assurance, dict):
            continue
        profile_id = assurance.get("profile_id")
        target_state = assurance.get("target_state")
        if target_state not in ASSURANCE_STATES:
            errors.append(f"{path}.assurance.target_state:enum")
        if profile_id is None:
            if target_state != "advisory":
                errors.append(f"{path}.assurance.profile_id:required_for_enforced_target")
            continue
        if not _safe_id(profile_id):
            errors.append(f"{path}.assurance.profile_id:safe_id_or_null")
            continue
        profile = profiles.get(profile_id)
        if profile is None:
            errors.append(f"{path}.assurance.profile_id:not_found")
            continue
        if profile.get("surface_role") != "main_agent":
            errors.append(f"{path}.assurance.profile_id:main_agent_required")
        if profile.get("agent_family") != frontend_kind:
            errors.append(f"{path}.assurance.profile_id:frontend_kind_mismatch")
        if target_state != _target_state(profile.get("target_claims")):
            errors.append(f"{path}.assurance.target_state:profile_target_mismatch")
    return sorted(set(errors))


def _descriptor(value: Mapping[str, Any]) -> SurfaceDescriptor:
    submit = value["submit_contract"]
    launcher = value["launcher"]
    requirements = value["requirements"]
    assurance = value["assurance"]
    return SurfaceDescriptor(
        frontend_kind=str(value["frontend_kind"]),
        submit_contract_id=str(submit["contract_id"]),
        submit_contract_version=str(submit["contract_version"]),
        launcher_id=str(launcher["launcher_id"]),
        launch_session_required=bool(launcher["launch_session_required"]),
        verifier_module=(
            str(launcher["verifier_module"])
            if launcher["verifier_module"] is not None
            else None
        ),
        verifier_class=(
            str(launcher["verifier_class"])
            if launcher["verifier_class"] is not None
            else None
        ),
        requirements_id=str(requirements["requirements_id"]),
        assurance_profile_id=(
            str(assurance["profile_id"]) if assurance["profile_id"] is not None else None
        ),
        target_assurance_state=str(assurance["target_state"]),
    )


def _default_assurance_evaluator(
    assurance_registry: Mapping[str, Any],
) -> AssuranceEvaluator:
    def evaluate(profile_id: str, expected_checkout: Path | None) -> Mapping[str, Any]:
        import agent_integration_assurance as assurance

        result = assurance.evaluate_registry(
            assurance_registry,
            profile_id=profile_id,
            expected_checkout=expected_checkout,
        )
        profiles = result.get("profiles")
        if isinstance(profiles, list) and len(profiles) == 1 and isinstance(profiles[0], dict):
            return profiles[0]
        return {"effective_claims": [], "decision": "suppress"}

    return evaluate


class SurfaceRegistry:
    """Validated registry with an explicit public registration mechanism."""

    def __init__(
        self,
        value: Mapping[str, Any],
        *,
        assurance_registry: Mapping[str, Any],
        assurance_evaluator: AssuranceEvaluator | None = None,
    ) -> None:
        import agent_integration_assurance as assurance

        assurance_errors = assurance.validate_registry(assurance_registry)
        if assurance_errors:
            raise SurfaceRegistryError(
                "assurance_registry_invalid:" + ";".join(assurance_errors)
            )
        errors = validate_registry(value, assurance_registry)
        if errors:
            raise SurfaceRegistryError("surface_registry_invalid:" + ";".join(errors))
        self._assurance_registry = json.loads(json.dumps(assurance_registry))
        self._assurance_evaluator = assurance_evaluator or _default_assurance_evaluator(
            self._assurance_registry
        )
        self._descriptors = {
            descriptor.frontend_kind: descriptor
            for descriptor in (_descriptor(item) for item in value["surfaces"])
        }

    @property
    def frontend_kinds(self) -> tuple[str, ...]:
        return tuple(self._descriptors)

    @property
    def descriptors(self) -> Mapping[str, SurfaceDescriptor]:
        return MappingProxyType(dict(self._descriptors))

    def register(self, value: Mapping[str, Any]) -> SurfaceDescriptor:
        """Register one descriptor after full cross-contract validation."""

        candidate = {
            "registry_version": "1",
            "surfaces": [
                *_serialized_descriptors(self._descriptors.values()),
                json.loads(json.dumps(value)),
            ],
        }
        errors = validate_registry(candidate, self._assurance_registry)
        if errors:
            raise SurfaceRegistryError("surface_registration_invalid:" + ";".join(errors))
        descriptor = _descriptor(value)
        self._descriptors[descriptor.frontend_kind] = descriptor
        return descriptor

    def descriptor(self, frontend_kind: str) -> SurfaceDescriptor:
        descriptor = self._descriptors.get(frontend_kind)
        if descriptor is None:
            raise SurfaceRegistryError(f"surface_not_registered:{frontend_kind}")
        return descriptor

    def identify(
        self,
        frontend_kind: str,
        *,
        expected_checkout: Path | None = None,
        launch_session_present: bool | None = None,
    ) -> SurfaceIdentity:
        descriptor = self.descriptor(frontend_kind)
        profile_id = descriptor.assurance_profile_id
        effective_claims: set[str] = set()
        if profile_id is not None:
            try:
                evaluation = self._assurance_evaluator(profile_id, expected_checkout)
            except Exception:
                evaluation = {"effective_claims": [], "decision": "suppress"}
            claims = evaluation.get("effective_claims")
            if isinstance(claims, list):
                effective_claims = {
                    claim for claim in claims if isinstance(claim, str) and claim in ASSURANCE_CLAIMS
                }
        profile = _profile_index(self._assurance_registry).get(profile_id or "", {})
        targeted_claims = {
            claim
            for claim in profile.get("target_claims", [])
            if isinstance(claim, str) and claim in ASSURANCE_CLAIMS
        }
        effective_claims &= targeted_claims
        if descriptor.launch_session_required and launch_session_present is False:
            effective_claims.clear()
        if "action_enforced" in effective_claims:
            state = "action_enforced"
        elif "ingress_enforced" in effective_claims:
            state = "ingress_enforced"
        else:
            state = "advisory"
        return SurfaceIdentity(
            frontend_kind=descriptor.frontend_kind,
            assurance_state=state,
            target_assurance_state=descriptor.target_assurance_state,
            assurance_profile_id=profile_id,
            commissioned_claims=tuple(sorted(effective_claims)),
            suppressed_claims=tuple(sorted(targeted_claims - effective_claims)),
            submit_contract_version=descriptor.submit_contract_version,
        )

    def normalize_launch_session(
        self,
        frontend_kind: str,
        value: Any,
    ) -> dict[str, Any] | None:
        """Run the surface profile's registered launch validator."""

        descriptor = self.descriptor(frontend_kind)
        if value is None:
            return None
        profile = _profile_index(self._assurance_registry).get(
            descriptor.assurance_profile_id or ""
        )
        validator = profile.get("launch_validator") if isinstance(profile, dict) else None
        if not isinstance(validator, dict):
            raise SurfaceRegistryError(
                f"surface_launch_validator_unavailable:{frontend_kind}"
            )
        try:
            module = importlib.import_module(str(validator["module"]))
            function = getattr(module, str(validator["function"]))
            normalized = function(value)
        except Exception as exc:
            raise SurfaceRegistryError(
                f"surface_launch_session_invalid:{frontend_kind}"
            ) from exc
        if not isinstance(normalized, Mapping):
            raise SurfaceRegistryError(
                f"surface_launch_session_invalid:{frontend_kind}"
            )
        return dict(normalized)

    def make_launch_session_verifier(self, frontend_kind: str) -> Any | None:
        descriptor = self.descriptor(frontend_kind)
        if not descriptor.launch_session_required:
            return None
        try:
            module = importlib.import_module(str(descriptor.verifier_module))
            factory = getattr(module, str(descriptor.verifier_class))
            return factory()
        except Exception as exc:
            raise SurfaceRegistryError(
                f"surface_launch_verifier_unavailable:{frontend_kind}"
            ) from exc


def _serialized_descriptors(
    descriptors: Any,
) -> list[dict[str, Any]]:
    return [
        {
            "descriptor_version": "1",
            "frontend_kind": descriptor.frontend_kind,
            "submit_contract": {
                "contract_id": descriptor.submit_contract_id,
                "contract_version": descriptor.submit_contract_version,
            },
            "launcher": {
                "launcher_id": descriptor.launcher_id,
                "launch_session_required": descriptor.launch_session_required,
                "verifier_module": descriptor.verifier_module,
                "verifier_class": descriptor.verifier_class,
            },
            "requirements": {"requirements_id": descriptor.requirements_id},
            "assurance": {
                "profile_id": descriptor.assurance_profile_id,
                "target_state": descriptor.target_assurance_state,
            },
        }
        for descriptor in descriptors
    ]


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SurfaceRegistryError(f"surface_registry_unloadable:{path}") from exc
    if not isinstance(value, dict):
        raise SurfaceRegistryError("surface_registry_root_not_object")
    return value


def default_registry() -> SurfaceRegistry:
    import agent_integration_assurance as assurance

    return SurfaceRegistry(
        load_registry(),
        assurance_registry=assurance.load_registry(),
    )
