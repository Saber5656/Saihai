#!/usr/bin/env python3
"""Bounded toolchain acquisition, extraction and real consumer contracts."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_delivery_toolchain as tool


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, entries):
        path = self.root / 'sample.tar.gz'
        with tarfile.open(path, 'w:gz') as tar:
            for name, kind, value in entries:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    data = value.encode(); info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    info.type = {'sym': tarfile.SYMTYPE, 'hard': tarfile.LNKTYPE,
                                 'dir': tarfile.DIRTYPE, 'fifo': tarfile.FIFOTYPE}[kind]
                    info.linkname = value; tar.addfile(info)
        return path

    def test_known_platform_and_closed_lock(self):
        lock = tool.load_lock(ROOT)
        self.assertEqual(tool.select(lock, 'Darwin', 'arm64')['version'], '3.11.16')
        for os_name, arch in [('Darwin', 'x86_64'), ('Linux', 'arm64'), ('Windows', 'AMD64')]:
            with self.assertRaises(tool.ContractError): tool.select(lock, os_name, arch)
        bad = copy.deepcopy(lock); bad['python']['Darwin-arm64']['url'] = 'https://evil.invalid/a'
        with self.assertRaises(tool.ContractError): tool.validate_lock(bad)
        bad = copy.deepcopy(lock); bad['python']['Darwin-arm64']['size'] = True
        with self.assertRaises(tool.ContractError): tool.validate_lock(bad)

    def test_same_version_wrong_digest_and_size_rejected(self):
        path = self.archive([('python/a', 'file', 'hello')])
        selected = {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        tool.verify_archive(path, selected)
        for key, value in [('sha256', 'a' * 64), ('size', path.stat().st_size + 1)]:
            bad = dict(selected); bad[key] = value
            with self.assertRaises(tool.ContractError): tool.verify_archive(path, bad)

    def test_unsafe_archives_rejected_without_outside_writes(self):
        for entries in [
            [('../escape', 'file', 'x')], [('/absolute', 'file', 'x')],
            [('python/a', 'file', 'x'), ('python/a', 'file', 'y')],
            [('python/a', 'file', 'x'), ('python/a/b', 'file', 'x')],
            [('python/a', 'sym', '../../outside')], [('python/a', 'hard', '/outside')],
            [('python/a', 'fifo', '')], [('python/a', 'sym', 'b'), ('python/b', 'sym', 'a')],
            [('python/a', 'sym', 'b'), ('python/a/child', 'file', 'x'), ('python/b', 'dir', '')],
        ]:
            with self.subTest(entries=entries):
                archive = self.archive(entries)
                dest = self.root / ('extract-' + str(len(list(self.root.iterdir()))))
                with self.assertRaises((tool.ContractError, tarfile.FilterError)):
                    tool.safe_extract(archive, dest)
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_internal_links_and_private_no_overwrite(self):
        archive = self.archive([('python/bin/real', 'file', 'ok'),
                                ('python/bin/python3', 'sym', 'real'),
                                ('python/bin/copy', 'hard', 'python/bin/real')])
        dest = self.root / 'extract'; tool.safe_extract(archive, dest)
        self.assertEqual((dest / 'python/bin/python3').read_text(), 'ok')
        self.assertEqual((dest / 'python/bin/copy').read_text(), 'ok')
        with self.assertRaises((FileExistsError, tool.ContractError)): tool.safe_extract(archive, dest)

    def test_filter_and_expansion_limits_are_mandatory(self):
        archive = self.archive([('python/a', 'file', 'large')])
        with patch.object(tarfile, 'data_filter', None):
            with self.assertRaises(tool.ContractError): tool.safe_extract(archive, self.root / 'no-filter')
        with patch.object(tool, 'MAX_EXPANDED', 1):
            with self.assertRaises(tool.ContractError): tool.safe_extract(archive, self.root / 'too-large')
        with patch.object(tool, 'MAX_MEMBERS', 0):
            with self.assertRaises(tool.ContractError): tool.safe_extract(archive, self.root / 'too-many')

    def test_download_rejects_http_size_and_digest_before_extract(self):
        selected = tool.select(tool.load_lock(ROOT), 'Darwin', 'arm64')
        class Response(io.BytesIO):
            status = 200
            headers = {}
            def geturl(self): return selected['url']
        for payload in [b'', b'wrong bytes']:
            with patch.object(tool, 'open_download', return_value=Response(payload)):
                with self.assertRaises(tool.ContractError): tool.download(selected, self.root / ('download-' + str(len(payload))))

    def test_nonzero_and_timeout_stage_are_retained(self):
        receipt = {'stages': []}
        with self.assertRaises(tool.StageFailure):
            tool.run_stage('failure', [sys.executable, '-c', 'raise SystemExit(7)'], self.root, receipt, os.environ.copy(), 5)
        self.assertEqual(receipt['stages'][-1]['exit'], 7)
        with self.assertRaises(tool.StageFailure):
            tool.run_stage('timeout', [sys.executable, '-c', 'import time;time.sleep(10)'], self.root, receipt, os.environ.copy(), .01)
        self.assertEqual(receipt['stages'][-1]['status'], 'timed_out')

    def test_running_stage_is_persisted_before_child_starts(self):
        receipt = {'stages': []}
        class Child:
            def __init__(inner, *args, **kwargs):
                recorded = json.loads((self.root / 'receipt.json').read_text())
                self.assertEqual(recorded['stages'][-1]['status'], 'running')
            def wait(inner, timeout): return 0
        with patch.object(tool.subprocess, 'Popen', Child):
            tool.run_stage('persist-first', ['fixed-command'], self.root, receipt, {}, 5)

    def test_child_environment_disables_external_config_and_loader_injection(self):
        with patch.dict(os.environ, {'PIP_EXTRA_INDEX_URL': 'https://untrusted.invalid', 'LD_PRELOAD': 'evil',
                                     'DYLD_INSERT_LIBRARIES': 'evil', 'HTTPS_PROXY': 'https://user:secret@proxy.invalid',
                                     'PYTHONHOME': '/other', 'NETRC': '/private/credentials'}, clear=True):
            env = tool.child_environment()
        for key in ('PIP_EXTRA_INDEX_URL', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES', 'HTTPS_PROXY', 'PYTHONHOME'):
            self.assertNotIn(key, list(env))
        self.assertEqual(env['PIP_CONFIG_FILE'], os.devnull)
        self.assertEqual(env['NETRC'], os.devnull)

    def test_runtime_probe_rejects_version_arch_and_path_drift(self):
        selected = tool.select(tool.load_lock(ROOT), 'Darwin', 'arm64')
        good = {'version': '3.11.16', 'machine': 'arm64', 'executable': '/private/runtime/bin/python3', 'prefix': '/private/runtime'}
        tool.check_probe(good, selected, Path(good['executable']))
        for key, value in [('version', '3.11.15'), ('machine', 'x86_64'), ('executable', '/usr/bin/python3')]:
            with self.assertRaises(tool.ContractError): tool.check_probe(dict(good, **{key: value}), selected, Path(good['executable']))

    def test_target_change_invalidates_receipt(self):
        with self.assertRaises(tool.ContractError): tool.require_identity({'head': 'a'}, {'head': 'b'})
        tool.require_identity({'head': 'a'}, {'head': 'a'})

    def test_workflow_preserves_scope_and_consumes_verified_runtime(self):
        workflow = (ROOT / '.github/workflows/validate.yml').read_text()
        for marker in ['push:\n    branches: [main]', 'pull_request:', 'merge_group:', 'concurrency:',
                       'scripts/verify_delivery_toolchain.py', '--run full', 'if: always()', 'retention-days: 14',
                       'actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5',
                       'actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065',
                       'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02']:
            self.assertIn(marker, workflow)
        for forbidden in ['continue-on-error', '|| true', 'pull_request_target', 'security-events: write', '**/*']:
            self.assertNotIn(forbidden, workflow)
        self.assertIn('receipt.json', workflow); self.assertIn('validation.json', workflow)


if __name__ == '__main__': unittest.main()
