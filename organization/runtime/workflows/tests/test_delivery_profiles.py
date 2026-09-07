#!/usr/bin/env python3
"""Negative coverage for delivery configuration; fixtures never grant authority."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import delivery_contract as contract


def example_profile(kind="web"):
    """Independent representative contract inputs, not production adoption."""
    required = {
        "web": {"static", "unit", "feature", "e2e"},
        "api": {"static", "unit", "feature", "e2e"},
        "mobile": {"static", "unit", "feature", "e2e", "device"},
        "library": {"static", "unit", "feature"},
        "cli": {"static", "unit", "feature", "e2e"},
        "iac": {"static", "unit", "feature"},
        "docs": {"static", "feature"},
    }[kind]
    layers = {
        name: {"required": name in required,
               "reason": "Required representative coverage." if name in required
               else "Not applicable to this representative project; adoption must confirm."}
        for name in ("static", "unit", "feature", "e2e", "device")
    }
    def job(layer, publish=False):
        trust = "privileged_publish" if publish else "untrusted_pr"
        return {
            "id": layer, "check_name": "delivery / " + layer, "layer": layer,
            "required": not publish, "runtime": "python", "trust": trust,
            "events": ["workflow_dispatch"] if publish else ["pull_request", "push", "merge_group"],
            "branches": ["main"] if publish else [],
            "source": "verified_artifact" if publish else "untrusted_head",
            "permissions": {"contents": "write" if publish else "read"},
            "credentials": ["release-token"] if publish else [],
            "environment": "release" if publish else None,
            "actions": [{"repository": "actions/checkout", "commit": "a" * 40}],
            "timeout_minutes": 10,
            "concurrency": {"group": "delivery-{repository}-{job}-{trust}-{ref}",
                            "cancel_in_progress": not publish},
            "cache": {"enabled": False, "namespace": trust,
                      "key": "deps-{trust}-{runtime_digest}-{lock_digest}", "restore_keys": []},
            "evidence_retention_days": 14,
        }
    return {
        "profile_version": "1", "profile_id": "example-" + kind,
        "repository": "example/" + kind, "project_type": kind,
        "runtimes": [{"name": "python", "version": "3.11.9", "sha256": "1" * 64}],
        "dependencies": {"mode": "locked", "reason": "Fixture dependency lock.",
                         "lockfiles": [{"path": "requirements.lock", "sha256": "2" * 64}]},
        "layers": layers,
        "jobs": [job(layer) for layer in sorted(required)] + [job("publish", True)],
        "release": {"owner": "example/release-team", "targets": [{"name": "package", "job": "publish"}],
                    "rollback_prerequisites": ["Retain and verify the previous immutable artifact.",
                                               "Confirm the release owner can restore that artifact."]},
    }


class DeliveryProfileTests(unittest.TestCase):
    def assert_invalid(self, value, fragment):
        errors = contract.validate_profile(value)
        self.assertTrue(errors, fragment)
        self.assertIn(fragment, "\n".join(errors))

    def test_seven_representative_profiles_validate_without_authority(self):
        for kind in ("web", "api", "mobile", "library", "cli", "iac", "docs"):
            with self.subTest(kind=kind):
                profile = example_profile(kind)
                self.assertEqual(contract.validate_profile(profile), [])
                result = contract.assess_profile(profile)
                self.assertEqual(result["configuration"], "valid")
                self.assertEqual(result["readiness"], "pending_policy_adoption")
                self.assertFalse(result["authorizes_execution"])

    def test_missing_profile_cannot_claim_readiness(self):
        for value in (None, {}, [], "approved"):
            with self.subTest(value=value):
                self.assertEqual(contract.assess_profile(value)["readiness"], "blocked")

    def test_missing_or_inappropriate_layers(self):
        for kind, layer in (("web", "e2e"), ("api", "feature"), ("mobile", "device"),
                            ("library", "unit"), ("cli", "e2e"), ("iac", "feature"), ("docs", "static")):
            profile = example_profile(kind)
            profile["layers"][layer]["required"] = False
            self.assert_invalid(profile, "required_layer")
        profile = example_profile()
        del profile["layers"]["device"]
        self.assert_invalid(profile, "fields")

    def test_required_jobs_are_present_unique_and_not_publish_jobs(self):
        profile = example_profile()
        profile["jobs"] = [job for job in profile["jobs"] if job["layer"] != "unit"]
        self.assert_invalid(profile, "missing_required_job")
        profile = example_profile()
        profile["jobs"][0]["required"] = False
        self.assert_invalid(profile, "missing_required_job")
        profile = example_profile()
        profile["jobs"].append(copy.deepcopy(profile["jobs"][0]))
        self.assert_invalid(profile, "duplicate")
        profile = example_profile()
        profile["jobs"][1]["check_name"] = profile["jobs"][0]["check_name"]
        self.assert_invalid(profile, "duplicate")

    def test_untrusted_job_rejects_credentials_writes_environment_and_publish_source(self):
        for key, value in (("permissions", {"contents": "write"}), ("permissions", {"id-token": "write"}),
                           ("credentials", ["release-token"]), ("environment", "release"),
                           ("source", "verified_artifact")):
            with self.subTest(key=key, value=value):
                profile = example_profile()
                profile["jobs"][0][key] = value
                self.assert_invalid(profile, "untrusted")

    def test_privileged_publication_is_separate_and_bounded(self):
        for key, value in (("events", ["pull_request"]), ("events", ["pull_request_target"]),
                           ("branches", []), ("source", "untrusted_head"), ("environment", None),
                           ("required", True), ("layer", "unit")):
            with self.subTest(key=key):
                profile = example_profile()
                profile["jobs"][-1][key] = value
                self.assert_invalid(profile, "privileged")
        profile = example_profile()
        profile["jobs"][-1]["permissions"]["administration"] = "write"
        self.assert_invalid(profile, "permission")
        profile = example_profile()
        profile["jobs"][-1]["concurrency"]["cancel_in_progress"] = True
        self.assert_invalid(profile, "privileged")

    def test_immutable_runtime_dependency_and_action_references(self):
        for mutate, expected in (
            (lambda p: p["runtimes"][0].update(version="latest"), "version"),
            (lambda p: p["runtimes"][0].update(sha256="main"), "sha256"),
            (lambda p: p["dependencies"].update(lockfiles=[]), "lockfile"),
            (lambda p: p["dependencies"]["lockfiles"][0].update(path="../escape.lock"), "path"),
            (lambda p: p["dependencies"]["lockfiles"][0].update(sha256="unpinned"), "sha256"),
            (lambda p: p["jobs"][0]["actions"][0].update(commit="v4"), "commit"),
            (lambda p: p["jobs"][0].update(runtime="unknown"), "runtime"),
        ):
            profile = example_profile(); mutate(profile); self.assert_invalid(profile, expected)
        profile = example_profile()
        profile["dependencies"] = {"mode": "none", "reason": "Standard library only.", "lockfiles": []}
        self.assertEqual(contract.validate_profile(profile), [])
        profile["dependencies"]["reason"] = ""
        self.assert_invalid(profile, "reason")

    def test_timeout_concurrency_retention_and_cache_trust(self):
        for mutate, expected in (
            (lambda j: j.update(timeout_minutes=0), "timeout"),
            (lambda j: j.update(timeout_minutes=1441), "timeout"),
            (lambda j: j.update(evidence_retention_days=0), "retention"),
            (lambda j: j.update(evidence_retention_days=91), "retention"),
            (lambda j: j["concurrency"].update(group="global"), "concurrency"),
            (lambda j: j["cache"].update(namespace="privileged_publish"), "cache"),
            (lambda j: j["cache"].update(enabled=True, key="shared"), "cache"),
            (lambda j: j["cache"].update(restore_keys=["deps-"]), "cache"),
        ):
            profile = example_profile(); mutate(profile["jobs"][0]); self.assert_invalid(profile, expected)

    def test_release_ownership_and_rollback_prerequisites(self):
        for key, value in (("owner", ""), ("targets", []), ("rollback_prerequisites", [])):
            profile = example_profile(); profile["release"][key] = value
            self.assert_invalid(profile, key)
        profile = example_profile()
        profile["release"]["targets"][0]["job"] = "unit"
        self.assert_invalid(profile, "release_job")

    def test_closed_nested_types_enums_and_lengths(self):
        for mutate in (
            lambda p: p.update(approved=True),
            lambda p: p.update(profile_version=1),
            lambda p: p.update(project_type="unknown"),
            lambda p: p.update(profile_id="x" * 97),
            lambda p: p.update(jobs=[None]),
            lambda p: p["runtimes"][0].update(extra=True),
            lambda p: p["layers"]["unit"].update(required=1),
            lambda p: p["jobs"][0].update(timeout_minutes=True),
            lambda p: p["jobs"][0].update(credentials="secret"),
            lambda p: p["jobs"][0]["cache"].update(enabled=1),
            lambda p: p["jobs"][0]["concurrency"].update(waiver=True),
            lambda p: p["release"]["targets"][0].update(approved=True),
            lambda p: p["dependencies"]["lockfiles"][0].update(approved=True),
        ):
            profile = example_profile(); mutate(profile)
            self.assertTrue(contract.validate_profile(profile))

    def test_profile_digest_is_canonical_and_input_is_not_mutated(self):
        profile = example_profile(); original = copy.deepcopy(profile)
        first = contract.assess_profile(profile)
        self.assertEqual(profile, original)
        reverse = dict(reversed(list(profile.items())))
        self.assertEqual(first["profile_digest"], contract.assess_profile(reverse)["profile_digest"])
        profile["jobs"][0]["timeout_minutes"] = 11
        self.assertNotEqual(first["profile_digest"], contract.assess_profile(profile)["profile_digest"])
        self.assertEqual(original, example_profile())
        self.assertEqual(len(first["profile_digest"]), 64)

    def test_malformed_collections_and_nested_objects_fail_without_exceptions(self):
        paths = (
            ("runtimes",), ("dependencies",), ("layers",), ("jobs",), ("release",),
            ("runtimes", 0), ("dependencies", "lockfiles"),
            ("dependencies", "lockfiles", 0), ("layers", "unit"), ("jobs", 0),
            ("jobs", 0, "permissions"), ("jobs", 0, "actions"), ("jobs", 0, "actions", 0),
            ("jobs", 0, "concurrency"), ("jobs", 0, "cache"),
            ("jobs", 0, "cache", "restore_keys"), ("release", "targets"),
            ("release", "targets", 0), ("release", "rollback_prerequisites"),
        )
        for path in paths:
            for value in (None, True, 1, "approved"):
                with self.subTest(path=path, value=value):
                    profile = example_profile(); parent = profile
                    for part in path[:-1]:
                        parent = parent[part]
                    parent[path[-1]] = value
                    self.assertTrue(contract.validate_profile(profile))
        profile = example_profile()
        profile["jobs"] *= 65
        self.assert_invalid(profile, "array_length")
        for path, value in (("project_type", []), ("profile_version", {}), ("repository", " ../repo")):
            profile = example_profile(); profile[path] = value
            self.assertTrue(contract.validate_profile(profile))

    def test_unused_publishers_and_duplicate_locks_and_runtimes_fail(self):
        for mutate in (
            lambda p: p["runtimes"].append(copy.deepcopy(p["runtimes"][0])),
            lambda p: p["dependencies"]["lockfiles"].append(copy.deepcopy(p["dependencies"]["lockfiles"][0])),
            lambda p: p["jobs"][0]["actions"].append(copy.deepcopy(p["jobs"][0]["actions"][0])),
        ):
            profile = example_profile(); mutate(profile); self.assert_invalid(profile, "duplicate")
        profile = example_profile()
        publisher = copy.deepcopy(profile["jobs"][-1])
        publisher.update(id="orphan", check_name="delivery / orphan")
        profile["jobs"].append(publisher)
        self.assert_invalid(profile, "unowned_release_job")

    def test_repository_examples_are_configuration_only(self):
        paths = sorted((ROOT / "profiles" / "delivery").glob("*.json"))
        self.assertEqual(len(paths), 7)
        for path in paths:
            result = contract.assess_profile(json.loads(path.read_text()))
            self.assertEqual(result["configuration"], "valid", (path, result))
            self.assertEqual(result["readiness"], "pending_policy_adoption")


if __name__ == "__main__":
    unittest.main()
