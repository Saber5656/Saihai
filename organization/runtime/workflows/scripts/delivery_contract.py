#!/usr/bin/env python3
"""Validate versioned delivery configuration without granting runtime authority.

No profile is an approval, waiver, workflow execution, or successful validation
run. Even valid configuration remains pending independently trusted adoption.
This module reads no credentials and executes no profile-authored commands.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

LAYER_MINIMUMS = {
    "web": frozenset({"static", "unit", "feature", "e2e"}),
    "api": frozenset({"static", "unit", "feature", "e2e"}),
    "mobile": frozenset({"static", "unit", "feature", "e2e", "device"}),
    "library": frozenset({"static", "unit", "feature"}),
    "cli": frozenset({"static", "unit", "feature", "e2e"}),
    "iac": frozenset({"static", "unit", "feature"}),
    "docs": frozenset({"static", "feature"}),
}
LAYERS = frozenset({"static", "unit", "feature", "e2e", "device"})
ID = r"[a-z0-9][a-z0-9_.-]{0,95}"
REPOSITORY = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
ACTION = REPOSITORY + r"(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}){0,4}"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?"
SHA256 = r"[0-9a-f]{64}"
COMMIT = r"[0-9a-f]{40}"


class _Checks:
    """Small closed-input checks; malformed values become errors, not crashes."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, path: str, reason: str) -> None:
        self.errors.append(f"{path}:{reason}")

    def obj(self, value: Any, fields: set[str] | frozenset[str], path: str) -> dict:
        if type(value) is not dict:
            self.fail(path, "object_required")
            return {}
        if set(value) != fields:
            self.fail(path, "fields_missing_or_unknown")
        return value

    def array(self, value: Any, path: str, minimum: int = 0, maximum: int = 64) -> list:
        if type(value) is not list:
            self.fail(path, "array_required")
            return []
        if not minimum <= len(value) <= maximum:
            self.fail(path, "array_length")
            return []
        return value

    def text(self, value: Any, path: str, pattern: str | None = None,
             maximum: int = 512) -> bool:
        if (type(value) is not str or not 1 <= len(value) <= maximum
                or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            self.fail(path, "bounded_text_required")
            return False
        if pattern is not None and re.fullmatch(pattern, value) is None:
            self.fail(path, "invalid_format")
            return False
        return True

    def enum(self, value: Any, choices: set | frozenset, path: str) -> bool:
        if type(value) is not str or value not in choices:
            self.fail(path, "invalid_enum")
            return False
        return True

    def boolean(self, value: Any, path: str) -> None:
        if type(value) is not bool:
            self.fail(path, "boolean_required")

    def integer(self, value: Any, path: str, minimum: int, maximum: int) -> None:
        if type(value) is not int or not minimum <= value <= maximum:
            self.fail(path, "bounded_integer_required")

    def unique(self, value: Any, seen: set[str], path: str) -> None:
        if type(value) is str:
            if value in seen:
                self.fail(path, "duplicate")
            seen.add(value)

    def strings(self, value: Any, path: str, *, minimum: int = 0,
                pattern: str | None = None, maximum: int = 64) -> list[str]:
        result = []
        seen: set[str] = set()
        for index, item in enumerate(self.array(value, path, minimum, maximum)):
            if self.text(item, f"{path}[{index}]", pattern):
                result.append(item)
                self.unique(item, seen, path)
        return result


def _runtime_contract(c: _Checks, profile: dict) -> set[str]:
    names: set[str] = set()
    for index, raw in enumerate(c.array(profile.get("runtimes"), "runtimes", 1, 16)):
        path = f"runtimes[{index}]"
        runtime = c.obj(raw, {"name", "version", "sha256"}, path)
        c.text(runtime.get("name"), path + ".name", ID)
        c.unique(runtime.get("name"), names, path + ".name")
        c.text(runtime.get("version"), path + ".version", VERSION, 96)
        c.text(runtime.get("sha256"), path + ".sha256", SHA256)
    deps = c.obj(profile.get("dependencies"), {"mode", "reason", "lockfiles"}, "dependencies")
    c.enum(deps.get("mode"), {"locked", "none"}, "dependencies.mode")
    c.text(deps.get("reason"), "dependencies.reason")
    locks = c.array(deps.get("lockfiles"), "dependencies.lockfiles", 0, 32)
    if deps.get("mode") == "locked" and not locks:
        c.fail("dependencies.lockfiles", "lockfile_required")
    if deps.get("mode") == "none" and locks:
        c.fail("dependencies.lockfiles", "none_requires_empty_lockfiles")
    paths: set[str] = set()
    for index, raw in enumerate(locks):
        path = f"dependencies.lockfiles[{index}]"
        lock = c.obj(raw, {"path", "sha256"}, path)
        candidate = lock.get("path")
        if c.text(candidate, path + ".path", r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", 256):
            if any(part in {".", ".."} for part in candidate.split("/")):
                c.fail(path + ".path", "unsafe_path")
        c.unique(candidate, paths, path + ".path")
        c.text(lock.get("sha256"), path + ".sha256", SHA256)
    return names


def _layers(c: _Checks, profile: dict) -> set[str]:
    layers = c.obj(profile.get("layers"), LAYERS, "layers")
    kind = profile.get("project_type")
    minimums = LAYER_MINIMUMS.get(kind, frozenset()) if type(kind) is str else frozenset()
    required: set[str] = set()
    for name in sorted(LAYERS):
        layer = c.obj(layers.get(name), {"required", "reason"}, "layers." + name)
        c.boolean(layer.get("required"), f"layers.{name}.required")
        c.text(layer.get("reason"), f"layers.{name}.reason")
        if layer.get("required") is True:
            required.add(name)
        elif name in minimums:
            c.fail("layers." + name, "required_layer")
    return required


def _trust(c: _Checks, job: dict, path: str) -> None:
    trust = job.get("trust")
    c.enum(trust, {"untrusted_pr", "privileged_publish"}, path + ".trust")
    events = c.strings(job.get("events"), path + ".events", minimum=1, maximum=3)
    for event in events:
        c.enum(event, {"pull_request", "push", "merge_group", "workflow_dispatch"}, path + ".events")
    branches = c.strings(job.get("branches"), path + ".branches", pattern=r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")
    credentials = c.strings(job.get("credentials"), path + ".credentials", pattern=ID, maximum=8)
    environment = job.get("environment")
    if environment is not None:
        c.text(environment, path + ".environment", ID)
    c.enum(job.get("source"), {"untrusted_head", "verified_artifact"}, path + ".source")
    permissions = job.get("permissions")
    if type(permissions) is not dict or len(permissions) > 3:
        c.fail(path + ".permissions", "permission_object_required")
        permissions = {}
    for name, permission in permissions.items():
        c.enum(name, {"contents", "packages", "id-token"}, path + ".permissions")
        c.enum(permission, {"read", "write"}, path + ".permissions")
        if name == "id-token" and permission != "write":
            c.fail(path + ".permissions", "invalid_oidc_permission")
    if trust == "untrusted_pr":
        if (credentials or environment is not None or job.get("source") != "untrusted_head"
                or any(name != "contents" or level != "read" for name, level in permissions.items())
                or "workflow_dispatch" in events or job.get("layer") == "publish"):
            c.fail(path, "untrusted_boundary")
    elif trust == "privileged_publish":
        if (not set(events) <= {"push", "workflow_dispatch"} or not branches or not environment
                or job.get("source") != "verified_artifact" or job.get("required") is not False
                or job.get("layer") != "publish"):
            c.fail(path, "privileged_boundary")


def _bounded_resources(c: _Checks, job: dict, path: str) -> None:
    c.integer(job.get("timeout_minutes"), path + ".timeout_minutes", 1, 1440)
    c.integer(job.get("evidence_retention_days"), path + ".evidence_retention_days", 1, 90)
    concurrency = c.obj(job.get("concurrency"), {"group", "cancel_in_progress"}, path + ".concurrency")
    group = concurrency.get("group")
    if c.text(group, path + ".concurrency.group"):
        if not all(token in group for token in ("{repository}", "{job}", "{trust}", "{ref}")):
            c.fail(path + ".concurrency", "isolation_tokens_required")
    c.boolean(concurrency.get("cancel_in_progress"), path + ".concurrency.cancel_in_progress")
    if job.get("trust") == "privileged_publish" and concurrency.get("cancel_in_progress") is not False:
        c.fail(path + ".concurrency", "privileged_publish_must_not_cancel")
    cache = c.obj(job.get("cache"), {"enabled", "namespace", "key", "restore_keys"}, path + ".cache")
    c.boolean(cache.get("enabled"), path + ".cache.enabled")
    c.enum(cache.get("namespace"), {"untrusted_pr", "privileged_publish"}, path + ".cache.namespace")
    if cache.get("namespace") != job.get("trust"):
        c.fail(path + ".cache", "trust_namespace_mismatch")
    keys = c.strings(cache.get("restore_keys"), path + ".cache.restore_keys", maximum=8)
    key = cache.get("key")
    if c.text(key, path + ".cache.key"):
        keys.append(key)
    for key in keys:
        if not all(token in key for token in ("{trust}", "{runtime_digest}", "{lock_digest}")):
            c.fail(path + ".cache", "trust_and_lock_tokens_required")


JOB_FIELDS = {
    "id", "check_name", "layer", "required", "runtime", "trust", "events", "branches",
    "source", "permissions", "credentials", "environment", "actions", "timeout_minutes",
    "concurrency", "cache", "evidence_retention_days",
}


def _jobs(c: _Checks, profile: dict, runtime_names: set[str], required_layers: set[str]) -> set[str]:
    ids: set[str] = set()
    checks: set[str] = set()
    covered: set[str] = set()
    publishers: set[str] = set()
    for index, raw in enumerate(c.array(profile.get("jobs"), "jobs", 1, 64)):
        path = f"jobs[{index}]"
        job = c.obj(raw, JOB_FIELDS, path)
        job_id = job.get("id")
        c.text(job_id, path + ".id", ID)
        c.unique(job_id, ids, path + ".id")
        c.text(job.get("check_name"), path + ".check_name", r"[A-Za-z0-9][A-Za-z0-9 /_.()-]{0,127}")
        c.unique(job.get("check_name"), checks, path + ".check_name")
        c.enum(job.get("layer"), LAYERS | {"publish"}, path + ".layer")
        c.boolean(job.get("required"), path + ".required")
        runtime = job.get("runtime")
        if not c.text(runtime, path + ".runtime", ID) or runtime not in runtime_names:
            c.fail(path + ".runtime", "unknown_runtime")
        _trust(c, job, path)
        _bounded_resources(c, job, path)
        action_ids: set[str] = set()
        for action_index, value in enumerate(c.array(job.get("actions"), path + ".actions", 0, 32)):
            action_path = f"{path}.actions[{action_index}]"
            action = c.obj(value, {"repository", "commit"}, action_path)
            c.text(action.get("repository"), action_path + ".repository", ACTION)
            c.unique(action.get("repository"), action_ids, action_path + ".repository")
            c.text(action.get("commit"), action_path + ".commit", COMMIT)
        if job.get("required") is True and job.get("trust") == "untrusted_pr" and type(job.get("layer")) is str:
            covered.add(job["layer"])
        if job.get("trust") == "privileged_publish" and type(job_id) is str:
            publishers.add(job_id)
    for layer in sorted(required_layers - covered):
        c.fail("jobs." + layer, "missing_required_job")
    return publishers


def _release(c: _Checks, profile: dict, publishers: set[str]) -> None:
    release = c.obj(profile.get("release"), {"owner", "targets", "rollback_prerequisites"}, "release")
    c.text(release.get("owner"), "release.owner", REPOSITORY)
    c.strings(release.get("rollback_prerequisites"), "release.rollback_prerequisites", minimum=1, maximum=16)
    target_ids: set[str] = set()
    targeted_jobs: set[str] = set()
    for index, raw in enumerate(c.array(release.get("targets"), "release.targets", 1, 16)):
        path = f"release.targets[{index}]"
        target = c.obj(raw, {"name", "job"}, path)
        c.text(target.get("name"), path + ".name", ID)
        c.unique(target.get("name"), target_ids, path + ".name")
        job = target.get("job")
        if c.text(job, path + ".job", ID):
            targeted_jobs.add(job)
            if job not in publishers:
                c.fail(path + ".job", "release_job_must_be_privileged")
    if publishers - targeted_jobs:
        c.fail("release.targets", "unowned_release_job")


def validate_profile(profile: Any) -> list[str]:
    """Return deterministic configuration errors; an empty list is not approval."""
    c = _Checks()
    profile = c.obj(profile, {"profile_version", "profile_id", "repository", "project_type",
                              "runtimes", "dependencies", "layers", "jobs", "release"}, "profile")
    c.enum(profile.get("profile_version"), {"1"}, "profile.profile_version")
    c.text(profile.get("profile_id"), "profile.profile_id", ID)
    c.text(profile.get("repository"), "profile.repository", REPOSITORY)
    c.enum(profile.get("project_type"), frozenset(LAYER_MINIMUMS), "profile.project_type")
    runtime_names = _runtime_contract(c, profile)
    required_layers = _layers(c, profile)
    publishers = _jobs(c, profile, runtime_names, required_layers)
    _release(c, profile, publishers)
    return c.errors


def assess_profile(profile: Any) -> dict[str, Any]:
    """Configuration receipt for consumers; adoption and actual checks stay pending."""
    errors = validate_profile(profile)
    digest = None
    if not errors:
        digest = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    return {
        "assessment_version": "1",
        "configuration": "invalid" if errors else "valid",
        "profile_digest": digest,
        "readiness": "blocked" if errors else "pending_policy_adoption",
        "authorizes_execution": False,
        "errors": errors,
        "pending": ["trusted_repository_policy_adoption", "actual_workflow_parity", "actual_run_evidence"],
    }
