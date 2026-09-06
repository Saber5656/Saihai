"""Offline canonical Task Detail discovery and monitor regressions (#137 U1)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import contextlib
import io
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "organization/runtime/infra-task-dispatcher/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
import directory_paths

# Import against an isolated fixture catalog, never the user's Vault.
with tempfile.TemporaryDirectory() as raw:
    env = {key: raw for key, field in directory_paths.SCHEMA.items() if field.required}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("itd_monitor", SCRIPTS / "itd_monitor.py")
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)


class CanonicalMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)

    def write(self, name, text):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def task(self, name, task_id="TSK-20260905-example", status="in_progress", extra=""):
        return self.write("01-Projects/" + name,
                          f"---\ntask_id: {task_id}\nstatus: {status}\n{extra}---\n# Task\n")

    def discover(self):
        return monitor.discover_tasks(self.vault, now=self.now)

    def test_nested_date_id_and_legacy_are_discovered_once(self):
        nested = self.task("Project/TSK-20260905-example/task.md")
        legacy = self.task("Project/TSK-1234-old.md", "TSK-1234")
        child = self.task("Project/TSK-PENDING-parent/issues/child/task.md", "TSK-20260905-child")
        self.assertEqual(set(monitor.task_files(self.vault)), {nested, legacy, child})

    def test_frontmatter_task_detail_in_nonstandard_filename(self):
        path = self.task("Project/record.md", extra="type: task-detail\n")
        self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["path"], path)

    def test_legacy_path_fallback_preserves_short_id(self):
        self.write("01-Projects/Project/TSK-1234-title/task.md", "---\nstatus: ready\n---\n")
        self.assertIn("TSK-1234", self.discover()["tasks"])

    def test_archive_move_preserves_identity_and_authoritative_state(self):
        path = self.task("Project/TSK-20260905-example/task.md", status="done")
        before = self.discover()["tasks"]
        archived = self.vault / "01-Projects/00_Archive/Project/TSK-20260905-example/task.md"
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        after = self.discover()["tasks"]
        self.assertEqual(set(before), set(after))
        self.assertEqual(after["TSK-20260905-example"]["status"], "done")
        self.assertTrue(after["TSK-20260905-example"]["archived"])
        self.assertFalse(any(f["event_type"] == "gate_preflight_missing"
                             for f in monitor.collect_gate_findings(self.vault)))

    def test_duplicate_identical_copy_is_ambiguous_and_never_chosen(self):
        first = self.task("Project/TSK-20260905-example/task.md")
        second = self.write("01-Projects/Project/TSK-20260905-example/task 2.md", first.read_text())
        before = {p: p.read_bytes() for p in (first, second)}
        result = self.discover()
        self.assertNotIn("TSK-20260905-example", result["tasks"])
        self.assertEqual(result["problems"][0]["event_type"], "task_identity_ambiguous")
        self.assertEqual(set(result["problems"][0]["paths"]), {first, second})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_active_and_archive_conflicting_copies_are_ambiguous(self):
        self.task("Project/TSK-20260905-example/task.md", status="ready")
        self.task("00_Archive/Project/TSK-20260905-example/task.md", status="done")
        self.assertFalse(self.discover()["tasks"])
        self.assertEqual(len(self.discover()["problems"]), 1)

    def test_malformed_identity_and_path_frontmatter_conflict_fail_closed(self):
        for identity in ("../../outside", "TSK-12345", "TSK-20260905-other"):
            with self.subTest(identity=identity):
                self.task("Project/TSK-20260905-example/task.md", identity)
                result = self.discover()
                self.assertFalse(result["tasks"])
                self.assertEqual(result["problems"][0]["event_type"], "task_identity_invalid")

    def test_duplicate_frontmatter_keys_do_not_select_last_value(self):
        self.task("Project/TSK-20260905-example/task.md", extra="task_id: TSK-20260905-other\n")
        self.assertFalse(self.discover()["tasks"])
        self.assertTrue(self.discover()["problems"])

    def test_qa01_malformed_typed_copy_quarantines_every_claimed_identity(self):
        first = self.task("Project/TSK-20260905-example/task.md")
        second = self.task("Project/TSK-20260905-other/task.md", "TSK-20260905-other")
        copy = self.write("01-Projects/Project/copy.md", "---\ntype: task-detail\n"
                          "task_id: TSK-20260905-example\ntask_id: TSK-20260905-other\n"
                          "status: ready\n---\n")
        original = {p: p.read_bytes() for p in (first, second, copy)}
        result = self.discover()
        self.assertFalse(result["tasks"])
        self.assertTrue(any(copy in item["paths"] for item in result["problems"]))
        self.assertIn(str(copy.relative_to(self.vault)), result["inputs"])
        self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_sec01_quoted_duplicate_authority_keys_are_rejected(self):
        for quote in ("'", '"'):
            for key, value in (("task_id", "TSK-20260905-other"), ("status", "done")):
                with self.subTest(quote=quote, key=key):
                    self.task("Project/TSK-20260905-example/task.md", status="ready",
                              extra=f"{quote}{key}{quote}: {value}\n")
                    self.assertFalse(self.discover()["tasks"])
                    self.assertTrue(self.discover()["problems"])

    def test_sec02_unreadable_subtree_cannot_prove_unique_authority(self):
        readable = self.task("readable/task.md", "TSK-1234", "ready")
        hidden = self.task("unreadable/task.md", "TSK-1234", "ready").parent

        def walk_with_denied_subtree(top, *, followlinks, onerror):
            self.assertEqual(top, self.vault / "01-Projects")
            self.assertFalse(followlinks)
            yield str(readable.parent), [], [readable.name]
            onerror(PermissionError(13, "Permission denied", str(hidden)))

        with mock.patch("task_discovery.os.walk", side_effect=walk_with_denied_subtree):
            result = self.discover()
        self.assertFalse(result["complete"])
        self.assertFalse(result["tasks"])
        self.assertTrue(any(p["event_type"] == "task_discovery_incomplete"
                            and hidden in p["paths"] for p in result["problems"]))

    def test_sec02_native_directory_denial_when_enforced(self):
        self.task("readable/task.md", "TSK-1234", "ready")
        hidden = self.task("unreadable/task.md", "TSK-1234", "ready").parent
        mode = hidden.stat().st_mode & 0o777
        try:
            hidden.chmod(0)
            try:
                list(hidden.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest("Current privileges bypass directory mode denial; deterministic traversal-error test still runs")
            result = self.discover()
            self.assertFalse(result["complete"])
            self.assertFalse(result["tasks"])
            self.assertTrue(any(p["event_type"] == "task_discovery_incomplete" for p in result["problems"]))
        finally:
            hidden.chmod(mode)
        self.assertEqual(hidden.stat().st_mode & 0o777, mode)

    def test_invalid_copy_cannot_leave_an_arbitrary_valid_winner(self):
        self.task("Project/TSK-20260905-example/task.md")
        self.task("Project/TSK-20260905-example/task 2.md", "TSK-20260905-other")
        self.assertFalse(self.discover()["tasks"])

    def test_missing_metadata_legacy_copy_remains_ambiguous(self):
        self.write("01-Projects/Project/TSK-1234-title.md", "---\nstatus: done\n---\n")
        self.write("01-Projects/Project/TSK-1234-title 2.md", "---\nstatus: done\n---\n")
        self.assertFalse(self.discover()["tasks"])
        self.assertEqual(self.discover()["problems"][0]["event_type"], "task_identity_ambiguous")

    def test_invalid_canonical_folder_does_not_gain_authority_from_frontmatter(self):
        self.task("Project/TSK-12345/task.md", "TSK-1234")
        self.assertFalse(self.discover()["tasks"])

    def test_quoted_and_commented_frontmatter_preserves_id(self):
        self.write("01-Projects/Project/task.md",
                   "---\ntask_id: 'TSK-20260905-example' # identity\nstatus: ready # comment\n---\n")
        self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["status"], "ready")

    def test_nested_yaml_key_cannot_override_authoritative_top_level(self):
        self.task("Project/TSK-20260905-example/task.md", extra="historical:\n  status: waiting_human\n")
        self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["status"], "in_progress")

    def test_symlink_task_and_directory_are_not_read_as_authority(self):
        outside = self.write("outside/task.md", "---\ntask_id: TSK-9999\nstatus: ready\n---\n")
        project = self.vault / "01-Projects/Project"
        project.mkdir(parents=True)
        (project / "task.md").symlink_to(outside)
        (project / "linked").symlink_to(outside.parent, target_is_directory=True)
        self.assertFalse(self.discover()["tasks"])

    def test_task_shaped_symlink_directory_is_reported_without_a_winner(self):
        self.task("readable/task.md", "TSK-1234", "ready")
        outside = self.write("outside/task.md", "---\ntask_id: TSK-1234\nstatus: ready\n---\n")
        link = self.vault / "01-Projects/TSK-1234-copy"
        before = monitor.build_snapshot([self.vault], self.vault / "report.md")
        link.symlink_to(outside.parent, target_is_directory=True)
        result = self.discover()
        self.assertFalse(result["complete"])
        self.assertFalse(result["tasks"])
        self.assertTrue(any(link in p["paths"] for p in result["problems"]))
        self.assertIn(str(link.relative_to(self.vault)), result["inputs"])
        after = monitor.build_snapshot([self.vault], self.vault / "report.md")
        self.assertNotEqual(before["digest"], after["digest"])
        self.assertEqual(after["digest"], monitor.build_snapshot([self.vault], self.vault / "report.md")["digest"])
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.is_file())

    def test_dangling_task_directory_link_is_visible_but_ordinary_link_is_excluded(self):
        self.task("readable/task.md", "TSK-1234", "ready")
        project = self.vault / "01-Projects"
        (project / "ordinary-link").symlink_to(self.vault / "missing", target_is_directory=True)
        self.assertEqual(set(self.discover()["tasks"]), {"TSK-1234"})
        link = project / "TSK-9999-missing"
        link.symlink_to(self.vault / "missing", target_is_directory=True)
        result = self.discover()
        self.assertFalse(result["complete"])
        self.assertFalse(result["tasks"])
        self.assertTrue(any(link in p["paths"] for p in result["problems"]))
        self.assertIn(str(link.relative_to(self.vault)), result["inputs"])

    def test_date_id_in_kanban_is_not_truncated(self):
        self.write("00-Inbox&Tasks/Kanban.md", "## In Progress\n- [ ] [[Project/task|TSK-20260905-example]]\n")
        self.assertEqual(monitor.parse_kanban(self.vault), {"TSK-20260905-example": "In Progress"})

    def test_kanban_wikilink_target_and_alias_are_one_projection_row(self):
        self.write("00-Inbox&Tasks/Kanban.md", "## In Progress\n"
                   "- [[01-Projects/TSK-20260905-example/task|TSK-20260905-example]]\n")
        self.assertEqual(monitor.parse_kanban(self.vault), {"TSK-20260905-example": "In Progress"})

    def test_legacy_wikilink_path_keeps_short_id_without_truncating_invalid_ids(self):
        self.write("00-Inbox&Tasks/Kanban.md", "## In Progress\n"
                   "- [[01-Projects/TSK-1234-old/task|Old task]]\n- TSK-12345\n")
        self.assertEqual(monitor.parse_kanban(self.vault), {"TSK-1234": "In Progress"})

    def test_qa03_nested_child_wikilink_does_not_reassign_parent_row(self):
        self.task("Project/TSK-PENDING-parent/task.md", "TSK-PENDING-parent")
        self.task("Project/TSK-PENDING-parent/issues/child/task.md", "TSK-20260905-child", "deferred")
        self.write("00-Inbox&Tasks/Kanban.md", "## In Progress\n"
                   "- [[Project/TSK-PENDING-parent/task|TSK-PENDING-parent]]\n"
                   "## Deferred\n"
                   "- [[Project/TSK-PENDING-parent/issues/child/task|TSK-20260905-child]]\n")
        self.assertEqual(monitor.parse_kanban(self.vault), {
            "TSK-PENDING-parent": "In Progress", "TSK-20260905-child": "Deferred"})
        self.assertFalse(any(f["event_type"] == "kanban_desync" for f in monitor.collect_gate_findings(self.vault)))

    def test_qa03_nested_child_without_id_alias_resolves_canonical_target(self):
        self.task("Project/TSK-PENDING-parent/issues/child/task.md", "TSK-20260905-child", "deferred")
        self.write("00-Inbox&Tasks/Kanban.md", "## Deferred\n"
                   "- [[01-Projects/Project/TSK-PENDING-parent/issues/child/task.md|Child task]]\n")
        self.assertEqual(monitor.parse_kanban(self.vault), {"TSK-20260905-child": "Deferred"})

    def test_stale_waiting_human_projection_does_not_override_detail(self):
        self.task("Project/TSK-20260905-example/task.md")
        self.write("00-Inbox&Tasks/Kanban.md", "## Waiting Human\n- [ ] TSK-20260905-example\n")
        self.write("00-Inbox&Tasks/Task-Index.md", "| TSK-20260905-example | x | x | x | waiting_human |\n")
        record = self.discover()["tasks"]["TSK-20260905-example"]
        self.assertEqual(record["status"], "in_progress")
        findings = monitor.collect_gate_findings(self.vault)
        self.assertTrue(any(f["event_type"] == "kanban_desync" for f in findings))
        self.assertNotIn("Waiting Human", monitor.kanban_section_for("blocked"))

    def test_waiting_human_without_live_decision_is_unverified(self):
        self.task("Project/TSK-20260905-example/task.md", status="waiting_human",
                  extra="requires_human_approval: true\n")
        record = self.discover()["tasks"]["TSK-20260905-example"]
        self.assertEqual(record["recorded_status"], "waiting_human")
        self.assertEqual(record["status"], "state_unverified")

    def test_only_live_meaningful_decision_supports_waiting_human(self):
        evidence = ("human_decision_id: decision-1\nhuman_decision_status: pending\n"
                    "human_decision_question: Which supported scope should be delivered?\n"
                    "human_decision_kind: scope\nhuman_decision_expires_at: 2026-09-06T00:00:00+00:00\n")
        self.task("Project/TSK-20260905-example/task.md", status="waiting_human", extra=evidence)
        self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["status"], "waiting_human")
        for old, new in (("pending", "answered"), ("2026-09-06", "2026-09-04"),
                         ("kind: scope", "kind: runtime_retry")):
            with self.subTest(change=new):
                self.task("Project/TSK-20260905-example/task.md", status="waiting_human",
                          extra=evidence.replace(old, new))
                self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["status"], "state_unverified")

    def test_deferred_and_incidental_projections_cannot_become_tasks(self):
        self.task("Project/TSK-20260905-example/task.md", status="deferred")
        for kind in ("incidental-findings", "task-index", "kanban", "itd-monitoring-report"):
            self.task(f"Project/{kind}/task.md", "TSK-9999", "ready", f"type: {kind}\n")
        self.write("01-Projects/Project/notes.md", "- [ ] TSK-9999 https://github.com/example/repo/issues/1\n")
        result = self.discover()
        self.assertEqual(set(result["tasks"]), {"TSK-20260905-example"})
        self.assertEqual(result["tasks"]["TSK-20260905-example"]["status"], "deferred")
        self.assertFalse(any(f["event_type"] == "gate_preflight_missing"
                             for f in monitor.collect_gate_findings(self.vault)))

    def test_unknown_status_is_not_ready_or_waiting_human(self):
        self.task("Project/TSK-20260905-example/task.md", status="future_unknown")
        self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["status"], "state_unverified")

    def test_complex_yaml_decision_scalar_does_not_fabricate_a_question(self):
        self.task("Project/task.md", status="waiting_human", extra=(
            "human_decision_id: decision-1\nhuman_decision_status: pending\n"
            "human_decision_question: |\nhuman_decision_kind: scope\n"
            "human_decision_expires_at: 2026-09-06T00:00:00Z\n"))
        self.assertEqual(self.discover()["tasks"]["TSK-20260905-example"]["status"], "state_unverified")

    def test_duplicate_projection_rows_are_not_silently_last_write_wins(self):
        self.write("00-Inbox&Tasks/Kanban.md", "## In Progress\n- TSK-1234\n## Waiting Human\n- TSK-1234\n")
        self.write("00-Inbox&Tasks/Task-Index.md", "| TSK-1234 | a | b | c | ready |\n| TSK-1234 | a | b | c | done |\n")
        self.assertEqual(monitor.parse_kanban(self.vault)["TSK-1234"], "projection_ambiguous")
        self.assertEqual(monitor.parse_task_index(self.vault)["TSK-1234"], "projection_ambiguous")

    def test_content_change_with_same_git_status_changes_snapshot(self):
        path = self.task("Project/TSK-20260905-example/task.md", status="ready")
        report = self.vault / "report.md"
        with mock.patch.object(monitor, "git_snapshot", return_value={"root": str(self.vault), "git": False}):
            before = monitor.build_snapshot([self.vault], report)
            path.write_text(path.read_text().replace("ready", "in_progress"))
            after = monitor.build_snapshot([self.vault], report)
        self.assertNotEqual(before["digest"], after["digest"])

    def test_report_self_update_is_not_a_task_or_snapshot_change(self):
        self.task("Project/TSK-20260905-example/task.md", status="deferred")
        report = self.vault / "01-Projects/TSK-9999-report.md"
        with mock.patch.object(monitor, "git_snapshot", return_value={"root": str(self.vault), "git": False}):
            before = monitor.build_snapshot([self.vault], report)
            report.write_text("---\ntype: itd-monitoring-report\n---\nnew report")
            after = monitor.build_snapshot([self.vault], report)
        self.assertEqual(before["digest"], after["digest"])
        self.assertNotIn(report, monitor.task_files(self.vault))

    def test_runtime_and_role_monitor_and_resolver_match(self):
        for name in ("itd_monitor.py", "task_discovery.py"):
            self.assertEqual((SCRIPTS / name).read_bytes(),
                             (ROOT / "organization/roles/infra-task-dispatcher/scripts" / name).read_bytes())

    def test_cli_unchanged_scan_and_state_transition_only_write_report(self):
        task = self.task("Project/TSK-20260905-example/task.md", status="deferred")
        report = self.vault / "report.md"
        command = ["itd_monitor.py", "--root", str(self.vault), "--report", str(report)]
        with mock.patch.object(monitor, "AGENTS_VAULT", self.vault), \
                mock.patch.object(monitor, "validate_vault"), \
                mock.patch.object(monitor, "git_snapshot", return_value={"root": str(self.vault), "git": False}), \
                mock.patch.object(sys, "argv", command), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(monitor.main(), 0)
            first = report.read_bytes()
            self.assertEqual(monitor.main(), 0)
            self.assertEqual(report.read_bytes(), first)
            task.write_text(task.read_text().replace("deferred", "archived"))
            self.assertEqual(monitor.main(), 0)
            self.assertGreater(len(report.read_bytes()), len(first))
        self.assertEqual(set(p for p in self.vault.rglob("*") if p.is_file()), {task, report})

    def test_qa02_real_git_tracked_report_does_not_trigger_second_cli_write(self):
        vault = self.vault.resolve()
        report = vault / "report.md"
        report.write_text(monitor.ensure_report_header(report))
        git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(vault)]
        for arguments in (["init", "-q"], ["add", "report.md"],
                          ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "-qm", "fixture report"]):
            subprocess.run(git + arguments, check=True, capture_output=True, text=True)
        env = os.environ.copy()
        env.update({key: str(vault) for key, field in directory_paths.SCHEMA.items() if field.required})
        env["SAIHAI_ROOT"] = str(ROOT)
        command = [sys.executable, str(SCRIPTS / "itd_monitor.py"), "--root", str(vault), "--report", str(report)]
        versions = []
        for _ in range(3):
            completed = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            versions.append(report.read_bytes())
        self.assertEqual([v.count(b"## Run ") for v in versions], [1, 1, 1])
        self.assertEqual(versions[0], versions[1])
        self.assertEqual(versions[1], versions[2])
        self.assertEqual(monitor.git_snapshot(vault, report)["status_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
