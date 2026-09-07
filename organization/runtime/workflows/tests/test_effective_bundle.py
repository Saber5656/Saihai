#!/usr/bin/env python3
"""Offline observation tests; legacy diagnostic is not an execution gate."""
import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "organization/runtime/workflows/scripts"
sys.path.insert(0, str(SCRIPTS))


def legacy_red():
    """Run only existing pure helpers, never import or dry-run stateful ITB."""
    source = ROOT / "organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"
    tree = ast.parse(source.read_text())
    names = {"policy_digest_entries", "policy_digest_status"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == 2
    namespace = {"Path": Path, "Any": object, "hashlib": hashlib}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    with tempfile.TemporaryDirectory() as temp:
        policy = Path(temp) / "policy.md"
        policy.write_text("old policy")
        embedded = namespace["policy_digest_entries"]({"policy": policy})
        policy.write_text("new policy")
        current = namespace["policy_digest_entries"]({"policy": policy})
        assert embedded[0]["sha1"] != current[0]["sha1"]
        actual = namespace["policy_digest_status"](current)
        print("Actual legacy readability status:", actual, "; current bytes differ from ready snapshot", flush=True)
        assert actual == "stale_policy_snapshot", "ready is readability, not snapshot freshness; U1 must compare bytes"


if "--legacy-red" in sys.argv:
    legacy_red()
    raise SystemExit(0)

import effective_bundle as bundle


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "source").write_bytes(b"content")
        (self.root / "copy").write_bytes(b"content")
        self.spec = {"members": [{"id": "common", "category": "common", "source": {"root": "repo", "path": "source"}, "installed": {"root": "repo", "path": "copy"}}], "policy_snapshots": []}

    def observe(self, **kwargs):
        return bundle.observe_bundle(catalog_roots={"repo": self.root}, member_spec=self.spec, target_surface="fixture", expected_source_identity=kwargs.pop("expected_source_identity", None), **kwargs)

    def reseal(self, value):
        value["content_digest"] = bundle.digest({k: v for k, v in value.items() if k not in ("content_digest", "observed_at")})

    def test_matching_copy_never_active(self):
        observation = self.observe()
        self.assertEqual(observation["members"][0]["relation"], "matching_copy")
        self.assertEqual(bundle.validate_observation(observation), [])
        self.assertEqual(observation["membership"], "asserted_unverified")
        self.assertEqual(observation["trusted_selection"], "unknown")
        self.assertEqual(observation["deployment"], "not_observed")
        self.assertEqual(observation["runtime_generation"], "unknown")

    def test_identical_reread_stable_digest(self):
        first, second = self.observe(), self.observe()
        self.assertNotEqual(first["observed_at"], second["observed_at"])
        self.assertEqual(first["content_digest"], second["content_digest"])
        self.assertEqual(bundle.compare_bundle(expected=first, observed=second)["comparison"], "equal_assertions")

    def test_copy_drift_same_mtime(self):
        first = self.observe()
        file = self.root / "copy"
        old = file.stat()
        file.write_bytes(b"changed")
        os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
        second = self.observe()
        self.assertEqual(second["members"][0]["relation"], "drift")
        self.assertIn("installed_drift", bundle.compare_bundle(expected=first, observed=second)["reasons"])

    def test_approved_file_symlink(self):
        (self.root / "copy").unlink()
        (self.root / "copy").symlink_to(self.root / "source")
        observed = self.observe(approved_symlinks={str(self.root / "copy"): str(self.root / "source")})
        self.assertEqual(observed["members"][0]["relation"], "same_file")
        self.assertEqual(bundle.validate_observation(observed), [])

    def test_directory_symlink_and_target_change(self):
        (self.root / "directory").mkdir()
        (self.root / "directory/file").write_bytes(b"content")
        (self.root / "link").symlink_to(self.root / "directory", target_is_directory=True)
        self.spec["members"][0]["installed"]["path"] = "link/file"
        approved = {str(self.root / "link/file"): str(self.root / "directory/file")}
        first = self.observe(approved_symlinks=approved)
        self.assertEqual(first["members"][0]["relation"], "matching_copy")
        (self.root / "other").mkdir()
        (self.root / "other/file").write_bytes(b"content")
        (self.root / "link").unlink()
        (self.root / "link").symlink_to(self.root / "other")
        self.assertEqual(self.observe(approved_symlinks=approved)["members"][0]["installed"]["status"], "unapproved_symlink")

    def test_unapproved_link_and_escape(self):
        (self.root / "copy").unlink()
        (self.root / "copy").symlink_to(self.root / "source")
        self.assertEqual(self.observe()["members"][0]["installed"]["status"], "unapproved_symlink")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside).resolve() / "private"
            target.write_text("never collect")
            (self.root / "copy").unlink()
            (self.root / "copy").symlink_to(target)
            observed = self.observe(approved_symlinks={str(self.root / "copy"): str(target)})
            self.assertEqual(observed["members"][0]["installed"]["status"], "path_escape")

    def test_missing_and_unreadable(self):
        (self.root / "copy").unlink()
        self.assertEqual(self.observe()["members"][0]["installed"]["status"], "missing")
        with mock.patch.object(bundle, "_open_regular", side_effect=PermissionError):
            self.assertEqual(self.observe()["members"][0]["source"]["status"], "unreadable")

    def test_bounds_and_special_file(self):
        (self.root / "source").write_bytes(b"x" * (bundle.MAX_BYTES + 1))
        self.assertEqual(self.observe()["members"][0]["source"]["status"], "file_too_large")
        (self.root / "source").unlink()
        os.mkfifo(self.root / "source")
        self.assertEqual(self.observe()["members"][0]["source"]["status"], "not_regular_file")

    def test_replacement_during_read(self):
        real = bundle.os.read
        replaced = False
        def replace(fd, count):
            nonlocal replaced
            data = real(fd, count)
            if not replaced:
                replaced = True
                (self.root / "source").write_bytes(b"changed")
            return data
        with mock.patch.object(bundle.os, "read", side_effect=replace):
            self.assertEqual(self.observe()["members"][0]["source"]["status"], "changed_during_read")

    def test_duplicate_and_omitted_members(self):
        self.spec["members"].append(copy.deepcopy(self.spec["members"][0]))
        with self.assertRaisesRegex(bundle.ObservationError, "duplicate_member"):
            self.observe()
        self.spec["members"] = []
        with self.assertRaisesRegex(bundle.ObservationError, "invalid_member_count"):
            self.observe()

    def test_missing_categories_and_unknown_applicability(self):
        observed = self.observe()
        self.assertIn("policy", observed["missing_categories"])
        self.assertEqual(observed["membership"], "asserted_unverified")
        # Even asserting all categories cannot promote completeness.
        for category in bundle.CATEGORIES[1:]:
            member = copy.deepcopy(self.spec["members"][0])
            member.update(id=category, category=category)
            self.spec["members"].append(member)
        observed = self.observe()
        self.assertEqual(observed["missing_categories"], [])
        self.assertEqual(observed["trusted_selection"], "unknown")

    def test_path_inputs_closed(self):
        for path in ("../secret", "/etc/passwd", "a/../secret", "a/./b", ""):
            with self.subTest(path=path):
                self.spec["members"][0]["source"]["path"] = path
                with self.assertRaises(bundle.ObservationError):
                    self.observe()

    def test_secrets_and_private_paths_absent(self):
        content_marker = "synthetic-content-must-not-be-exported"
        (self.root / "source").write_text(content_marker)
        report = json.dumps(self.observe())
        self.assertNotIn(content_marker, report)
        self.assertNotIn(str(self.root), report)
        self.assertNotIn('"path"', report)

    def test_schema_forgery_and_malformed_inputs(self):
        original = self.observe()
        for key, value in (("schema_version", 2), ("schema_version", True), ("runtime_generation", "active"), ("membership", "complete"), ("trusted_selection", "verified"), ("task_id", "forged"), ("extra", True)):
            with self.subTest(key=key, value=value):
                invalid = copy.deepcopy(original)
                invalid[key] = value
                self.reseal(invalid)
                self.assertTrue(bundle.validate_observation(invalid))
        for invalid in (None, [], "str", 1):
            self.assertTrue(bundle.validate_observation(invalid))

    def test_schema_internal_consistency(self):
        original = self.observe()
        invalid = copy.deepcopy(original)
        invalid["members"][0]["relation"] = "same_file"
        self.reseal(invalid)
        self.assertIn("inconsistent_relation", bundle.validate_observation(invalid))
        invalid = copy.deepcopy(original)
        invalid["members"][0]["source"]["status"] = "missing"
        self.reseal(invalid)
        self.assertIn("inconsistent_file_status", bundle.validate_observation(invalid))
        invalid = copy.deepcopy(original)
        invalid["missing_categories"] = []
        self.reseal(invalid)
        self.assertIn("category_mismatch", bundle.validate_observation(invalid))

    def test_snapshot_requires_both_readable_sources(self):
        policy = b"policy"
        (self.root / "source").write_bytes(policy)
        (self.root / "copy").write_text(f"| policy | `ready` | `{hashlib.sha1(policy).hexdigest()}` | 6 | `private` |\n")
        self.spec = {"members": [
            {"id": "policy", "category": "policy", "source": {"root": "repo", "path": "source"}, "installed": None},
            {"id": "role", "category": "role", "source": {"root": "repo", "path": "copy"}, "installed": None}],
            "policy_snapshots": [{"role": "role", "policy": "policy"}]}
        original = self.observe()
        self.assertEqual(original["policy_snapshots"][0]["status"], "match")
        for missing in ("role", "policy"):
            invalid = copy.deepcopy(original)
            file = next(m["source"] for m in invalid["members"] if m["id"] == missing)
            for key in file:
                if key not in ("location_digest", "status"):
                    file[key] = None
            file["status"] = "missing"
            self.reseal(invalid)
            with self.subTest(missing=missing):
                self.assertIn("inconsistent_snapshot", bundle.validate_observation(invalid))

    def test_snapshot_unknown_and_ambiguous_consistency(self):
        (self.root / "copy").write_text("no policy snapshot")
        self.spec = {"members": [
            {"id": "policy", "category": "policy", "source": {"root": "repo", "path": "source"}, "installed": None},
            {"id": "role", "category": "role", "source": {"root": "repo", "path": "copy"}, "installed": None}],
            "policy_snapshots": [{"role": "role", "policy": "policy"}]}
        original = self.observe()
        self.assertEqual(bundle.validate_observation(original), [])
        for change in ({"status": "unknown"}, {"embedded_sha1": "0" * 40}, {"current_sha1": None}):
            invalid = copy.deepcopy(original)
            invalid["policy_snapshots"][0].update(change)
            self.reseal(invalid)
            with self.subTest(change=change):
                self.assertIn("inconsistent_snapshot", bundle.validate_observation(invalid))
        (self.root / "copy").unlink()
        self.assertEqual(bundle.validate_observation(self.observe()), [])

    def test_strict_json_input(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[] trailing', b'\xff', b'x' * (bundle.MAX_BYTES + 1)):
            with self.subTest(size=len(data)):
                with self.assertRaises(bundle.ObservationError):
                    bundle.decode_json(data)

    def test_source_commit_changed_and_dirty(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        expected = {"root": "repo", "commit": git("rev-parse", "HEAD")}
        self.assertEqual(self.observe(expected_source_identity=expected)["source_identity"]["status"], "match")
        self.assertEqual(self.observe(expected_source_identity={"root": "repo", "commit": "0" * 40})["source_identity"]["status"], "source_changed")
        (self.root / "new").write_text("untracked")
        self.assertEqual(self.observe(expected_source_identity=expected)["source_identity"]["status"], "source_changed")

    def test_source_repository_ignores_ambient_git_selection(self):
        def git(*args):
            return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        head = git("rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp).resolve()
            with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / ".git"), "GIT_WORK_TREE": str(self.root)}):
                result = bundle._source_identity({"root": "repo", "commit": head}, {"repo": other})
            self.assertEqual(result["status"], "unknown")

    def test_source_repository_rejects_subdirectory_identity(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        child = self.root / "nested"
        child.mkdir()
        # The exact requested root must be a checkout, not merely inside one.
        result = bundle._source_identity({"root": "repo", "commit": "0" * 40}, {"repo": child})
        self.assertEqual(result["status"], "unknown")

    def test_git_observation_output_is_bounded(self):
        with self.assertRaisesRegex(bundle.ObservationError, "git_output_too_large"):
            bundle._git_output([sys.executable, "-c", "import sys;sys.stdout.write('x' * 1048577)"], os.environ.copy())
        with mock.patch.object(bundle, "_git_output", side_effect=bundle.ObservationError("git_output_too_large")):
            result = bundle._source_identity({"root": "repo", "commit": "0" * 40}, {"repo": self.root})
        self.assertEqual(result["status"], "unknown")

    def test_cli_fixture_collection_comparison(self):
        first = self.observe()
        (self.root / "copy").write_bytes(b"drift")
        second = self.observe()
        result = subprocess.run([sys.executable, str(SCRIPTS / "effective_bundle.py"), "compare"], input=json.dumps({"expected": first, "observed": second}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed_drift", json.loads(result.stdout)["reasons"])
        self.assertEqual(json.loads(result.stdout)["runtime_generation"], "unknown")

    def test_cli_invalid_input_and_validation(self):
        command = [sys.executable, str(SCRIPTS / "effective_bundle.py"), "validate"]
        good = subprocess.run(command, input=json.dumps(self.observe()), capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout)
        bad = subprocess.run(command, input='{"private-token":"fixture-secret", "private-token":2}', capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn("fixture-secret", bad.stdout + bad.stderr)

    def test_stale_ready_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "policy.md").write_text("old policy")
            old = hashlib.sha1(b"old policy").hexdigest()
            (root / "role.md").write_text(
                f"| policy | `ready` | `{old}` | 10 | `private` |\n"
            )
            members = [
                {"id": "policy", "category": "policy", "source": {"root": "repo", "path": "policy.md"}, "installed": None},
                {"id": "role", "category": "role", "source": {"root": "repo", "path": "role.md"}, "installed": None},
            ]
            spec = {"members": members, "policy_snapshots": [{"role": "role", "policy": "policy"}]}
            first = bundle.observe_bundle(catalog_roots={"repo": root}, member_spec=spec, target_surface="fixture", expected_source_identity=None)
            self.assertEqual(first["policy_snapshots"][0]["status"], "match")
            (root / "policy.md").write_text("new policy")
            after = bundle.observe_bundle(catalog_roots={"repo": root}, member_spec=spec, target_surface="fixture", expected_source_identity=None)
            self.assertEqual(after["policy_snapshots"][0]["status"], "stale_policy_snapshot")
            self.assertEqual(after["runtime_generation"], "unknown")
            self.assertEqual(bundle.validate_observation(after), [])
            self.assertIn("stale_policy_snapshot", bundle.compare_bundle(expected=first, observed=after)["reasons"])


if __name__ == "__main__":
    unittest.main()
