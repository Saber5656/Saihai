#!/usr/bin/env python3
"""Actual workflow projection, strict parsing and bidirectional parity tests."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import delivery_workflow_inventory as inventory

FIXTURE = ROOT / "tests/fixtures/delivery/workflow-inventory-cases.v1.json"
ACTUAL = ROOT / "profiles/delivery-inventory/saihai.v1.json"


def context(event="pull_request", ref="refs/heads/feature", base_ref="refs/heads/main"):
    return {"event": event, "ref": ref, "base_ref": base_ref, "fork": True, "action": None}


def target():
    return {"repository": "example/project", "head_sha": "a" * 40, "base_sha": "b" * 40}


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text())

    def sources(self):
        return {".github/workflows/test.yml": self.fixture["workflow"]}

    def observed(self):
        return inventory.observe_workflows(self.sources())

    def snapshot(self):
        observed = self.observed()
        return {"inventory_version": "1", "repository": "example/project",
                "source_digests": observed["source_digests"], "jobs": observed["jobs"],
                "lock_digests": {"deps.lock": "c" * 64}}

    def report(self, **changes):
        args = dict(sources=self.sources(), expected=self.snapshot(), target=target(),
                    event_context=context(), lock_digests={"deps.lock": "c" * 64}, policy_snapshot=None)
        args.update(changes)
        return inventory.audit_inventory(**args)

    def test_yaml_on_is_string_and_native_loader_is_unchanged(self):
        native_before = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
        parsed = inventory.parse_workflow("on: [push]\nflag: false\n")
        self.assertEqual(parsed["on"], ["push"])
        self.assertIs(parsed["flag"], False)
        self.assertEqual(yaml.SafeLoader.yaml_implicit_resolvers, native_before)
        self.assertEqual(yaml.safe_load("on: yes"), {True: True})

    def test_duplicate_keys_tags_aliases_and_resource_bounds(self):
        for raw in self.fixture["invalid_yaml"]:
            with self.subTest(raw=raw):
                with self.assertRaises(inventory.InventoryError):
                    inventory.parse_workflow(raw)
        for raw in ("x: " + "a" * 1048576, "x: " + "[" * 65 + "0" + "]" * 65,
                    "x: [" + ",".join("0" for _ in range(10001)) + "]"):
            with self.assertRaises(inventory.InventoryError):
                inventory.parse_workflow(raw)

    def test_matrix_include_exclude_and_stable_cell_identity(self):
        observed = self.observed()
        self.assertEqual(observed["parsing"], "valid", observed)
        self.assertEqual([j["matrix"] for j in observed["jobs"]], self.fixture["expected_cells"])
        self.assertEqual(len({j["cell_id"] for j in observed["jobs"]}), 4)
        self.assertEqual([j["check_name"] for j in observed["jobs"]],
                         ["Test (3.11, linux)", "Test (3.11, macos)",
                          "Test (3.12, linux)", "Test (3.13, linux)"])
        self.assertTrue(all(j["job_id"] == "test" for j in observed["jobs"]))
        self.assertNotEqual(observed["jobs"][0]["cell_id"], observed["jobs"][0]["check_name"])

    def test_dynamic_or_excessive_matrix_is_unknown_not_empty_success(self):
        for matrix in ("${{ fromJSON(needs.prepare.outputs.matrix) }}", {"python": []},
                       {"x": list(range(32)), "y": list(range(32))}):
            doc = inventory.parse_workflow(self.fixture["workflow"])
            doc["jobs"]["test"]["strategy"]["matrix"] = matrix
            observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
            self.assertTrue(observed["errors"] or observed["gaps"])
            self.assertNotEqual(observed["parsing"], "valid")

    def test_unknown_runner_expression_and_matrix_name_are_not_guessed(self):
        doc = inventory.parse_workflow(self.fixture["workflow"])
        doc["jobs"]["test"]["runs-on"] = "${{ fromJSON(inputs.runner) }}"
        observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
        self.assertIn("unsupported_expression", json.dumps(observed["gaps"]))
        del doc["jobs"]["test"]["name"]
        observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
        self.assertTrue(all(row["check_name"] is None for row in observed["jobs"]))

    def test_matrix_scalars_keep_json_types_and_global_cell_budget(self):
        doc = inventory.parse_workflow(self.fixture["workflow"])
        doc["jobs"]["test"]["strategy"]["matrix"] = {"value": [True, 1], "exclude": [{"value": True}]}
        observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
        self.assertEqual([row["matrix"] for row in observed["jobs"]], [{"value": 1}])
        doc["jobs"]["test"]["strategy"]["matrix"] = {"x": list(range(16)), "y": list(range(16))}
        doc["jobs"]["other"] = copy.deepcopy(doc["jobs"]["test"])
        observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
        self.assertIn("total_cell_budget", json.dumps(observed["errors"]))

    def test_missing_extra_and_unknown_fields_are_visible(self):
        for mutate, marker in (
            (lambda d: d["jobs"]["test"].update(unapproved=True), "unknown_field"),
            (lambda d: d["jobs"]["test"].pop("steps"), "steps"),
            (lambda d: d["jobs"]["test"]["steps"][0].update(extra="value"), "unknown_field"),
            (lambda d: d.update(jobs={}), "jobs"),
        ):
            doc = inventory.parse_workflow(self.fixture["workflow"]); mutate(doc)
            observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
            self.assertIn(marker, json.dumps(observed))
            self.assertTrue(observed["gaps"] or observed["errors"])

    def test_review_malformed_nested_contracts_are_structurally_invalid(self):
        mutations = [
            lambda d: d["jobs"]["test"].pop("steps"),
            lambda d: d["jobs"]["test"]["steps"][1].update(run=42),
            lambda d: d["jobs"]["test"].update(**{"runs-on": []}),
            lambda d: d["jobs"]["test"].update(**{"runs-on": ["ubuntu", False]}),
            lambda d: d["jobs"]["test"].update(**{"runs-on": {"labels": []}}),
            lambda d: d.update(permissions={"madeup": "write"}),
            lambda d: d["jobs"]["test"].update(permissions={"contents": "godmode"}),
            lambda d: d.update(permissions={"id-token": "read"}),
            lambda d: d.update(defaults={"run": {"unapproved": True}}),
            lambda d: d["jobs"]["test"].update(defaults={"run": {"shell": False}}),
            lambda d: d.update(concurrency=False),
            lambda d: d["jobs"]["test"].update(concurrency={}),
            lambda d: d.update(concurrency={"group": "safe", "cancel-in-progress": 1}),
            lambda d: d["jobs"]["test"].update(services={"db": {"image": ""}}),
            lambda d: d["jobs"]["test"].update(needs=[False]),
            lambda d: d["jobs"]["test"].update(environment={"name": "test", "approved": True}),
            lambda d: d["jobs"]["test"]["strategy"].update(**{"fail-fast": 1}),
            lambda d: d["jobs"]["test"]["strategy"].update(**{"max-parallel": 0}),
            lambda d: d["jobs"]["test"]["steps"][1].update(env={"SEED": {"value": "42"}}),
            lambda d: d["jobs"]["test"]["steps"][0].update(with_={"unknown": "true"}),
            lambda d: d["on"].update(push=False),
            lambda d: d["on"].update(pull_request={"branches": []}),
            lambda d: d["on"].update(merge_group={"approved": True}),
            lambda d: d["on"].update(schedule=[{"cron": False}]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = inventory.parse_workflow(self.fixture["workflow"]); mutate(doc)
                observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
                self.assertEqual(observed["parsing"], "invalid", observed)
                self.assertTrue(observed["errors"])
                for row in observed["jobs"]:
                    self.assertEqual(inventory.applicability(row, context())["state"], "unknown")
                expected = {"inventory_version": "1", "repository": "example/project", "source_digests": observed["source_digests"],
                            "jobs": observed["jobs"], "lock_digests": {"deps.lock": "c" * 64}}
                result = self.report(sources={".github/workflows/test.yml": json.dumps(doc)}, expected=expected)
                self.assertTrue(all(row["execution"]["classification"] == "unknown" for row in result["jobs"]))

    def test_quality_gaps_remain_distinct_from_invalid_schema(self):
        observed = self.observed()
        self.assertEqual(observed["parsing"], "valid")
        self.assertEqual(observed["errors"], [])
        self.assertIn("missing_concurrency", json.dumps(observed["gaps"]))
        doc = inventory.parse_workflow(self.fixture["workflow"])
        doc["concurrency"] = False
        observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
        self.assertEqual(observed["parsing"], "invalid")
        self.assertIn("concurrency", json.dumps(observed["errors"]))
        self.assertIn("missing_concurrency", json.dumps(observed["gaps"]))

    def test_projection_keeps_all_commands_actions_and_context(self):
        job = self.observed()["jobs"][0]
        self.assertEqual(job["contract"]["workflow"]["env"], {"GLOBAL": "yes"})
        self.assertEqual(job["contract"]["job"]["services"]["db"]["image"], "postgres@sha256:" + "d" * 64)
        step = job["contract"]["job"]["steps"][1]
        self.assertEqual(step["run"], "python -m unittest\n")
        self.assertEqual(step["env"], {"SEED": "42"})
        self.assertEqual(step["working-directory"], "src")
        self.assertEqual(job["contract"]["job"]["defaults"], {"run": {"shell": "bash"}})
        self.assertEqual(job["contract"]["job"]["permissions"], {"contents": "read"})

    def test_bidirectional_drift_in_every_contract_field(self):
        for field, value in (
            ("runs-on", "windows-latest"), ("container", "python:latest"),
            ("services", {}), ("env", {"CHANGED": "true"}), ("defaults", {}),
            ("needs", ["new_job"]), ("if", "false"), ("permissions", {"contents": "write"}),
            ("steps", [{"run": "echo placeholder"}]), ("timeout-minutes", 2),
        ):
            with self.subTest(field=field):
                expected = self.snapshot()
                expected["jobs"][0]["contract"]["job"][field] = value
                report = self.report(expected=expected)
                self.assertEqual(report["parity"], "drift")
                self.assertIn(field, json.dumps(report["drift"]))
        expected = self.snapshot(); expected["jobs"].pop()
        self.assertIn("unexpected_job", json.dumps(self.report(expected=expected)["drift"]))
        expected = self.snapshot(); extra = copy.deepcopy(expected["jobs"][0]); extra["cell_id"] = "stale"
        expected["jobs"].append(extra)
        self.assertIn("missing_job", json.dumps(self.report(expected=expected)["drift"]))

    def test_source_policy_inventory_lock_and_target_digests_bind_evidence(self):
        first = self.report()
        changed = self.report(sources={k: v + "\n# changed after local success\n" for k, v in self.sources().items()})
        self.assertEqual(changed["parity"], "drift")
        self.assertNotEqual(first["inventory_digest"], changed["inventory_digest"])
        self.assertNotEqual(first["binding_digest"], changed["binding_digest"])
        changed = self.report(lock_digests={"deps.lock": "d" * 64})
        self.assertIn("lock_digests", json.dumps(changed["drift"]))
        changed_target = target(); changed_target["head_sha"] = "c" * 40
        self.assertNotEqual(first["binding_digest"], self.report(target=changed_target)["binding_digest"])
        policy = {"policy_version": "1", "repository": "example/project", "required_checks": ["Test (3.11, linux)"]}
        result = self.report(policy_snapshot=policy)
        self.assertEqual(result["policy_status"], "unverified")
        self.assertEqual(len(result["policy_digest"]), 64)
        self.assertFalse(result["authorizes_execution"])

    def test_actual_event_branch_and_pr_base_applicability(self):
        job = self.observed()["jobs"][0]
        for ctx, expected in ((context(), "applicable"), (context("push", "refs/heads/main"), "applicable"),
                              (context("push", "refs/heads/feature"), "not_applicable"),
                              (context("pull_request", base_ref="refs/heads/dev"), "not_applicable"),
                              (context("merge_group", "refs/heads/gh-readonly-queue/main/a"), "applicable")):
            with self.subTest(ctx=ctx):
                self.assertEqual(inventory.applicability(job, ctx)["state"], expected)
        job["contract"]["job"]["if"] = "${{ needs.prepare.result == 'success' }}"
        self.assertEqual(inventory.applicability(job, context())["state"], "unknown")
        job["contract"]["job"]["if"] = False
        self.assertEqual(inventory.applicability(job, context())["state"], "not_applicable")

    def test_required_but_not_triggered_check_never_becomes_success(self):
        policy = {"policy_version": "1", "repository": "example/project", "required_checks": ["Test (3.11, linux)"]}
        result = self.report(policy_snapshot=policy, event_context=context("push", "refs/heads/feature"))
        self.assertIn("required_check_not_applicable", json.dumps(result["gaps"]))
        self.assertEqual(result["readiness"], "blocked")

    def test_schema_and_unsupported_context_fail_closed(self):
        for change in ({"expected": {}}, {"event_context": {}}, {"target": {}},
                       {"policy_snapshot": {"approved": True}}, {"lock_digests": {"../outside": "a" * 64}}):
            with self.subTest(change=change):
                self.assertEqual(self.report(**change)["readiness"], "blocked")
        for doc in (None, [], True, {"jobs": {"test": None}}, {"on": ["push"], "jobs": []}):
            observed = inventory.observe_workflows({".github/workflows/test.yml": json.dumps(doc)})
            self.assertTrue(observed["errors"] or observed["gaps"])

    def test_real_repository_maps_three_cells_and_exposes_current_gaps(self):
        expected = json.loads(ACTUAL.read_text())
        result = inventory.audit_repository(REPO, expected, context(),
                                            {"repository": "Saber5656/Saihai", "head_sha": "a" * 40, "base_sha": "b" * 40})
        self.assertEqual(result["parsing"], "valid", result)
        self.assertEqual(result["parity"], "match", result["drift"])
        self.assertEqual([(x["job_id"], x["matrix"]) for x in result["jobs"]],
                         [("analyze", {"language": "actions"}), ("analyze", {"language": "python"}), ("validate", {})])
        text = json.dumps(result["gaps"])
        for marker in ("floating_runtime", "missing_concurrency", "evidence_retention_unset", "merge_group_missing", "authoritative_policy_missing"):
            self.assertIn(marker, text)
        self.assertEqual(result["readiness"], "blocked")
        self.assertFalse(result["authorizes_execution"])
        for job in result["jobs"][:2]:
            self.assertEqual(job["execution"]["classification"], "hybrid")
            self.assertEqual(job["execution"]["local_state"], "local_unavailable")
            self.assertEqual(job["execution"]["remote_state"], "remote_pending")

    def test_actual_source_addition_and_symlink_cannot_hide_from_inventory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); (root / ".github/workflows").mkdir(parents=True)
            source = root / ".github/workflows/test.yml"; source.write_text(self.fixture["workflow"])
            (root / "deps.lock").write_text("locked\n")
            expected = self.snapshot()
            (root / ".github/workflows/extra.yml").write_text(self.fixture["workflow"])
            result = inventory.audit_repository(root, expected, context(), target())
            self.assertEqual(result["parity"], "drift")
            source.unlink(); source.symlink_to(root / ".github/workflows/extra.yml")
            result = inventory.audit_repository(root, expected, context(), target())
            self.assertIn("symlink", json.dumps(result["errors"]))

    def test_actual_reader_hashes_exact_bytes_and_rejects_directory_symlinks(self):
        import hashlib
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); (root / ".github/workflows").mkdir(parents=True)
            content = self.fixture["workflow"].replace("\n", "\r\n").encode()
            (root / ".github/workflows/test.yml").write_bytes(content)
            (root / "deps.lock").write_bytes(b"locked\r\n")
            observed = inventory.observe_workflows({".github/workflows/test.yml": content.decode()})
            expected = {"inventory_version": "1", "repository": "example/project", "jobs": observed["jobs"],
                        "source_digests": observed["source_digests"], "lock_digests": {"deps.lock": hashlib.sha256(b"locked\r\n").hexdigest()}}
            result = inventory.audit_repository(root, expected, context(), target())
            self.assertEqual(result["parity"], "match", result)
            (root / "links").symlink_to(root, target_is_directory=True)
            expected["lock_digests"] = {"links/deps.lock": "a" * 64}
            self.assertIn("symlink", json.dumps(inventory.audit_repository(root, expected, context(), target())["errors"]))

    def test_locked_dependency_and_ci_use_same_isolated_interpreter(self):
        lock = (REPO / ".github/requirements-delivery.lock").read_text()
        self.assertIn("PyYAML==6.0.3", lock)
        for digest in ("652cb6edd41e718550aad172851962662ff2681490a8a711af6a4d288dd96824",
                       "b8bb0864c5a28024fac8a632c443c87c5aa6f215c0b126c449ae1a150412f31d"):
            self.assertIn(digest, lock)
        workflow = (REPO / ".github/workflows/validate.yml").read_text()
        self.assertIn("--require-hashes --only-binary=:all:", workflow)
        self.assertIn("python3 -m venv", workflow)
        self.assertIn('"$RUNNER_TEMP/delivery-venv/bin/python3" scripts/validate_all.py', workflow)
        self.assertEqual(yaml.__version__, "6.0.3")
        self.assertEqual(sys.version_info[:2], (3, 11))

    def test_existing_itb_optional_yaml_path_remains_native_and_fallback_works(self):
        builder = REPO / "organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"
        spec = importlib.util.spec_from_file_location("delivery_test_itb_builder", builder)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertIsNotNone(module._pyyaml)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.yml"; path.write_text("on: yes\n")
            self.assertEqual(module.load_yaml_config(path), {True: True})
            module._pyyaml = None
            path.write_text("name: fallback\ncount: 2\n")
            self.assertEqual(module.load_yaml_config(path), {"name": "fallback", "count": 2})


if __name__ == "__main__":
    unittest.main()
