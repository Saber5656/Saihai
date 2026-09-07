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
from types import SimpleNamespace
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

    def test_case_variants_follow_actual_destination_semantics(self):
        archive = self.archive([('python/E/Eterm', 'file', 'upper'),
                                ('python/e/eterm', 'file', 'lower')])
        probe = self.root / 'probe'; probe.mkdir()
        sensitive = tool.case_sensitive_destination(probe)
        destination = self.root / 'case-variants'
        if sensitive:
            tool.safe_extract(archive, destination)
            self.assertEqual((destination/'python/E/Eterm').read_text(), 'upper')
            self.assertEqual((destination/'python/e/eterm').read_text(), 'lower')
        else:
            with self.assertRaisesRegex(tool.ContractError, 'colliding'):
                tool.safe_extract(archive, destination)
        with patch.object(tool, 'case_sensitive_destination', return_value=False):
            with self.assertRaisesRegex(tool.ContractError, 'colliding'):
                tool.safe_extract(archive, self.root/'forced-insensitive')

    def test_case_variant_implicit_parent_is_rejected_on_insensitive_destination(self):
        archive = self.archive([('python/A/one', 'file', 'one'),
                                ('python/a/two', 'file', 'two')])
        with patch.object(tool, 'case_sensitive_destination', return_value=False):
            with self.assertRaisesRegex(tool.ContractError, 'colliding'):
                tool.safe_extract(archive, self.root/'parent-alias')

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


class ValidationProjectionTests(unittest.TestCase):
    """Exercise execute's real sanitizer/receipt writer; provisioning is synthetic."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        (self.repo / '.github').mkdir(parents=True)
        (self.repo / '.github/delivery-toolchain.lock.json').write_text('fixture lock')
        (self.repo / '.github/requirements-delivery.lock').write_text('fixture dependency')
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_fixture.py').write_text('print("fixture")')
        self.private = self.root / 'runtime'
        self.python = self.private / 'venv/bin/python3'
        self.counter = 0

    def validation(self):
        return {'schema_version': 1, 'result': 'pass', 'compiled': True,
                'suites': [{'path': 'tests/test_fixture.py', 'result': 'pass', 'cases': 2,
                            'executed': 2, 'failed': 0, 'skipped': 0, 'unknown': 0,
                            'count_method': 'completed_test_functions', 'status': 'passed', 'exit_code': 0,
                            'command': [str(self.python), str(self.repo / 'tests/test_fixture.py')],
                            'cwd': str(self.repo), 'started_at': '2026-09-06T00:00:00+00:00',
                            'finished_at': '2026-09-06T00:00:01+00:00', 'duration_seconds': 1.0}],
                'contracts': [{'command': [str(self.python), 'organization/runtime/workflows/scripts/workflow_selector.py', 'validate-contracts'], 'result': 'pass'},
                              {'command': [str(self.python), 'organization/runtime/workflows/scripts/template_role_validator.py'], 'result': 'pass'}]}

    def execute_fixture(self, result, *, raw=None, mutate_output=False, mutate_log=False, shard_index=None, shard_count=None):
        self.counter += 1
        output = self.root / ('attempt-' + str(self.counter))
        selected = {'version': '3.11.16', 'machine': 'arm64', 'interpreter': 'python/bin/python3.11'}
        lock = {'dependency_lock': {'path': '.github/requirements-delivery.lock',
                                  'sha256': tool.digest(b'fixture dependency')}}
        def stage(name, command, directory, receipt, env, timeout):
            log = directory / (name + '.log')
            if name in ('interpreter-probe', 'venv-probe'):
                data = json.dumps({'version': '3.11.16', 'machine': 'arm64', 'executable': command[0],
                                   'prefix': str(self.private / 'venv')}).encode()
            elif name == 'full': data = raw if raw is not None else json.dumps(result).encode()
            else: data = b'fixture stage'
            log.write_bytes(data)
            receipt['stages'].append({'name': name, 'command': command, 'start': tool.now(),
                                      'end': tool.now(), 'status': 'success', 'exit': 0,
                                      'log_sha256': tool.digest(data)})
            tool.save_receipt(directory, receipt)
            if name == 'full' and mutate_log:
                swapped = copy.deepcopy(result)
                swapped['suites'][0].update(cases=3, executed=3)
                log.write_text(json.dumps(swapped))
            return log
        calls = 0
        def identity(root):
            nonlocal calls
            calls += 1
            if calls == 2 and mutate_output:
                (output / 'validation.json').write_text('{"swapped":true}')
            return {'head': 'synthetic-fixed'}
        # Only external provisioning/stage input and target source are mocked.
        # execute, validation parsing/projection and final receipt persistence are real.
        with patch.object(tool, 'ROOT', self.repo), patch.object(tool.tempfile, 'mkdtemp', return_value=str(self.private)), patch.object(tool, 'load_lock', return_value=lock), patch.object(tool, 'select', return_value=selected), patch.object(tool, 'download'), patch.object(tool, 'safe_extract'), patch.object(tool, 'run_stage', side_effect=stage), patch.object(tool, 'target_identity', side_effect=identity):
            code = tool.execute(output, 'full', shard_index, shard_count)
        return code, output, json.loads((output / 'receipt.json').read_text())

    def test_shard_selection_is_bound_and_never_full(self):
        result = self.validation()
        result['selection'] = {'kind':'shard','index':0,'count':8}
        code, output, receipt = self.execute_fixture(result, shard_index=0, shard_count=8)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((output/'validation.json').read_text())['selection'], result['selection'])
        self.assertEqual(receipt['selection'], result['selection'])
        self.assertIn('--shard-index', receipt['stages'][-1]['command'])
        self.assertNotEqual(self.execute_fixture(result)[0], 0)
        with self.assertRaises(tool.ContractError):
            tool.execute(self.root/'invalid-shard','full',True,8)

    def test_current_suite_evidence_survives_public_projection(self):
        result = self.validation()
        result['suites'][0]['stdout_tail'] = 'private-output-marker'
        result['suites'][0]['environment'] = {'PRIVATE': 'private-environment-marker'}
        result['arbitrary'] = 'private-top-level-marker'
        code, output, receipt = self.execute_fixture(result)
        self.assertEqual(code, 0)
        public = json.loads((output / 'validation.json').read_bytes())
        for key in ('command', 'cwd', 'started_at', 'finished_at', 'exit_code', 'status',
                    'cases', 'executed', 'failed', 'skipped', 'unknown', 'count_method', 'duration_seconds'):
            self.assertEqual(public['suites'][0][key], result['suites'][0][key], key)
        self.assertEqual(public['suite_evidence_version'], 1)
        self.assertNotIn('private-', (output / 'validation.json').read_text())

    def test_receipt_binds_exact_sanitized_bytes_not_log_bytes(self):
        code, output, receipt = self.execute_fixture(self.validation())
        self.assertEqual(code, 0)
        data = (output / 'validation.json').read_bytes()
        self.assertEqual(receipt['validation_result'], {'path': 'validation.json', 'sha256': tool.digest(data), 'bytes': len(data)})
        self.assertNotEqual(receipt['validation_result']['sha256'], receipt['stages'][-1]['log_sha256'])

    def test_malformed_missing_and_unsuccessful_required_fields_fail(self):
        for field, value in [('cases', 0), ('cases', True), ('executed', 0), ('executed', True),
                             ('failed', 1), ('skipped', 1), ('unknown', 1), ('unknown', False),
                             ('exit_code', True), ('exit_code', 7), ('status', 'unknown'),
                             ('count_method', 'unknown'), ('started_at', 'bad'),
                             ('finished_at', '2000-01-01T00:00:00+00:00'), ('duration_seconds', True),
                             ('command', ['arbitrary', 'private-command-marker'])]:
            with self.subTest(field=field, value=value):
                result = self.validation(); result['suites'][0][field] = value
                code, output, receipt = self.execute_fixture(result)
                self.assertNotEqual(code, 0)
                self.assertEqual(receipt['status'], 'failure')
                self.assertNotIn('validation_result', receipt)
                self.assertFalse((output / 'validation.json').exists())
        for field in ('executed', 'failed', 'skipped', 'unknown', 'command', 'cwd', 'started_at',
                      'finished_at', 'exit_code', 'status', 'count_method'):
            with self.subTest(missing=field):
                result = self.validation(); del result['suites'][0][field]
                self.assertNotEqual(self.execute_fixture(result)[0], 0)

    def test_failed_compile_contract_and_duplicate_suite_fail(self):
        for mutate in (lambda r: r.update(compiled=False),
                       lambda r: r['contracts'][0].update(result='fail'),
                       lambda r: r.update(contracts=[]),
                       lambda r: r['suites'].append(copy.deepcopy(r['suites'][0]))):
            result = self.validation(); mutate(result)
            self.assertNotEqual(self.execute_fixture(result)[0], 0)

    def test_duplicate_nonfinite_and_nonobject_json_fail(self):
        good = json.dumps(self.validation())
        for raw in (b'[]', b'null', b'{', good.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1').encode(),
                    good.replace('"duration_seconds": 1.0', '"duration_seconds": NaN').encode()):
            with self.subTest(raw_kind=raw[:10]):
                self.assertNotEqual(self.execute_fixture(None, raw=raw)[0], 0)

    def test_full_log_swap_cannot_change_bound_result(self):
        code, output, receipt = self.execute_fixture(self.validation(), mutate_log=True)
        self.assertNotEqual(code, 0)
        self.assertNotIn('validation_result', receipt)

    def test_changed_result_before_final_receipt_cannot_succeed(self):
        code, output, receipt = self.execute_fixture(self.validation(), mutate_output=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(receipt['status'], 'failure')


    def test_actual_u0_child_evidence_reaches_real_producer(self):
        spec = importlib.util.spec_from_file_location('u0_runner_fixture', ROOT / 'scripts/validate_all.py')
        runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
        suite = self.repo / 'tests/test_fixture.py'
        suite.write_text('import unittest\nclass Check(unittest.TestCase):\n    def test_one(self): self.assertTrue(True)\nunittest.main()\n')
        with patch.object(runner, 'REPO_ROOT', self.repo): row = runner.run_suite(suite)
        self.assertEqual(row['result'], 'pass')
        result = self.validation(); result['suites'] = [row]
        for contract in result['contracts']: contract['command'][0] = sys.executable
        output = self.root / 'real-producer'; output.mkdir()
        receipt = {'stages': []}
        with patch.object(tool, 'ROOT', self.repo):
            log = tool.run_stage('full', [sys.executable, '-c', 'print(' + repr(json.dumps(result)) + ')'], output, receipt, os.environ.copy(), 5)
            tool.publish_validation(log, output, receipt, sys.executable)
            tool.verify_validation_result(output, receipt)
        public = json.loads((output / 'validation.json').read_bytes())
        self.assertEqual(public['suites'][0]['executed'], 1)
        self.assertEqual(public['suites'][0]['count_method'], 'unittest_summary')
        self.assertEqual(public['suites'][0]['command'], row['command'])
        self.assertEqual(json.loads((output / 'receipt.json').read_text())['validation_result']['sha256'], tool.digest((output / 'validation.json').read_bytes()))

    def test_projection_file_budget_symlink_and_depth_are_rejected(self):
        path = self.root / 'bounded.json'
        path.write_bytes(b' ' * (tool.VALIDATION_JSON_LIMIT + 1))
        with self.assertRaises(tool.ContractError): tool.validation_document(path)
        path.write_text('[' * 40 + '0' + ']' * 40)
        with self.assertRaises(tool.ContractError): tool.validation_document(path)
        link = self.root / 'linked.json'; link.symlink_to(path)
        with self.assertRaises((OSError, tool.ContractError)): tool.validation_document(link)
        with self.assertRaises(tool.ContractError): tool.validation_document(self.root)


class CodeQLContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bundle(self, entries=None):
        path = self.root / 'codeql.tar.gz'
        entries = entries or [('codeql/codeql', b'wrapper'), ('codeql/jre/bin/java', b'java'), ('codeql/tools/a.jar', b'jar')]
        with tarfile.open(path, 'w:gz') as tar:
            for name, data in entries:
                member = tarfile.TarInfo(name); member.size = len(data); member.mode = 0o644
                tar.addfile(member, io.BytesIO(data))
        return path

    def test_closed_bundle_and_python_bounds(self):
        lock = tool.load_lock(ROOT)
        self.assertEqual(tool.codeql_select(lock, 'linux64')['size'], 822687033)
        self.assertEqual(tool.MAX_DOWNLOAD, 64 * 1024 * 1024)
        for key in ('unknown', 'latest', '../linux64'):
            with self.assertRaises(tool.ContractError): tool.codeql_select(lock, key)
        bad = copy.deepcopy(lock); bad['codeql']['linux64']['size'] = True
        with self.assertRaises(tool.ContractError): tool.validate_lock(bad)

    def test_real_stream_scan_and_installed_all_members(self):
        archive = self.bundle(); manifest = tool.scan_codeql_archive(archive)
        installed = self.root / 'installed'; installed.mkdir()
        with tarfile.open(archive) as tar: tar.extractall(installed, filter='data')
        tool.verify_codeql_installation(installed / 'codeql', manifest)
        for member in ('jre/bin/java', 'tools/a.jar', 'codeql'):
            path = installed / 'codeql' / member; original = path.read_bytes(); path.write_bytes(b'changed')
            with self.assertRaises(tool.ContractError): tool.verify_codeql_installation(installed / 'codeql', manifest)
            path.write_bytes(original)
        (installed / 'codeql' / 'extra').write_bytes(b'loadable')
        with self.assertRaises(tool.ContractError): tool.verify_codeql_installation(installed / 'codeql', manifest)

    def test_archive_names_and_actual_read_budgets(self):
        for name in ('../outside', '/outside', 'codeql/../escape', 'codeql/a\n', 'codeql/a/../b'):
            archive = self.bundle([(name, b'x')])
            with self.assertRaises(tool.ContractError): tool.scan_codeql_archive(archive)
        archive = self.bundle([('codeql/a', b'a'), ('codeql/a', b'b')])
        with self.assertRaises(tool.ContractError): tool.scan_codeql_archive(archive)
        archive = self.bundle()
        with patch.object(tool, 'CODEQL_EXPANDED_LIMIT', 1):
            with self.assertRaises(tool.ContractError): tool.scan_codeql_archive(archive)
        with patch.object(tool, 'CODEQL_MEMBER_LIMIT', 1):
            with self.assertRaises(tool.ContractError): tool.scan_codeql_archive(archive)

    def test_bounded_json_duplicate_fields_and_non_json_observation(self):
        path = self.root / 'state.json'; path.write_text('{"x":1,"x":2}')
        with self.assertRaises(tool.ContractError): tool.read_codeql_json(path)
        for value in ('a\n', 'a\r', 'x;' + chr(36) + '(echo bad)', 'x' * 4097):
            with self.assertRaises(tool.ContractError): tool.codeql_text(value)

    def test_probe_rejects_bad_chain_before_invocation(self):
        with patch.object(tool, 'bounded_codeql_command') as command:
            with self.assertRaises(tool.ContractError):
                tool.codeql_probe(self.root, 'linux64', 'python', '/tmp/arbitrary', '2.26.0', 'success')
            command.assert_not_called()

    def test_real_probe_chain_and_tampered_jre_block_command(self):
        # Synthetic repository/bundle and host; only the external CodeQL process is mocked.
        archive = self.bundle(); sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        repo = self.root / 'repo'; (repo / '.github/workflows').mkdir(parents=True)
        for name in ('validate', 'codeql'): (repo / '.github/workflows' / (name + '.yml')).write_text('name: synthetic')
        lock = tool.load_lock(ROOT); lock['codeql']['linux64'].update(size=archive.stat().st_size, sha256=sha)
        (repo / '.github/delivery-toolchain.lock.json').write_text(json.dumps(lock))
        for command in (['git', 'init', '-q'], ['git', 'add', '.'], ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture']):
            subprocess.run(command, cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        attempt = self.root / 'attempt'; attempt.mkdir()
        (attempt / 'bundle.tar.gz').write_bytes(archive.read_bytes())
        manifest = tool.scan_codeql_archive(archive); (attempt / 'manifest.json').write_text(json.dumps(manifest))
        temp = (self.root / 'runner-temp').resolve(); extraction = temp / '11111111-1111-1111-1111-111111111111'; extraction.mkdir(parents=True)
        with tarfile.open(archive) as tar: tar.extractall(extraction, filter='data')
        codeql = extraction / 'codeql/codeql'
        registry = dict(tool.CODEQL_BUNDLES, linux64=(archive.stat().st_size, sha))
        with patch.object(tool, 'ROOT', repo), patch.object(tool, 'CODEQL_BUNDLES', registry), patch.object(tool.platform, 'system', return_value='Linux'), patch.object(tool.platform, 'machine', return_value='x86_64'), patch.object(tool.platform, 'libc_ver', return_value=('glibc', '2.35')), patch.dict(os.environ, {'RUNNER_TEMP': str(temp), 'GITHUB_RUN_ID': '5', 'GITHUB_RUN_ATTEMPT': '1'}, clear=True):
            binding = tool.codeql_binding('linux64', 'python', attempt)
            acquired = {'phase': 'acquire', 'start': '2026-09-05T00:00:00+00:00', 'end': '2026-09-05T00:00:01+00:00',
                        'status': 'acquisition_verified', 'exit': 0, 'mode': 'ci-consumer', 'authorizes_execution': False,
                        'policy_status': 'not_adopted', 'runnable_here': True, 'host': {'system': 'Linux', 'machine': 'x86_64'},
                        'runtime': 'not_run', 'analysis': 'not_run', 'authenticated_service_state': 'remote_pending',
                        'binding': binding, 'manifest_sha256': hashlib.sha256((attempt / 'manifest.json').read_bytes()).hexdigest()}
            tool.codeql_write(attempt / 'receipt-acquire.json', acquired)
            with patch.object(tool, 'bounded_codeql_command', return_value={'version': '2.26.0'}) as command:
                args = SimpleNamespace(output=attempt, codeql_phase='probe', bundle='linux64', language='python', fetch_only=False,
                                       codeql_path=str(codeql), codeql_version='2.26.0', init_outcome='success', analysis_outcome='', sarif_id='')
                self.assertEqual(tool.codeql_phase(args), 0)
                result = json.loads((attempt / 'receipt-probe.json').read_text())
                self.assertEqual(result['version'], '2.26.0'); command.assert_called_once()
            # Consumed phase records have closed types, values and nested shapes.
            for field, value in [('host', {'system': False, 'machine': []}),
                                 ('host', {'system': 'Linux', 'machine': 'x86_64', 'approved': True}),
                                 ('host', {'system': 'Darwin', 'machine': 'x86_64'}),
                                 ('runtime', {'approved': True}), ('runtime', 'observed_result'),
                                 ('analysis', False), ('analysis', 'success'), ('runnable_here', False)]:
                with self.subTest(acquire_field=field, value=value):
                    bad = copy.deepcopy(acquired); bad[field] = value
                    tool.codeql_write(attempt / 'receipt-acquire.json', bad)
                    with self.assertRaises(tool.ContractError): tool.codeql_chain(attempt, 'linux64', 'python')
            tool.codeql_write(attempt / 'receipt-acquire.json', acquired)
            for field, value in [('version', '2.25.0'), ('version_document_sha256', True),
                                 ('installation_verification_sha256', 'invalid'), ('command_exit', False),
                                 ('command_start', 'bad'), ('command_end', '2000-01-01T00:00:00+00:00'),
                                 ('command', [str(codeql), 'version']), ('authorizes_execution', 0), ('codeql_path', '')]:
                with self.subTest(probe_field=field):
                    bad = copy.deepcopy(result); bad[field] = value
                    with self.assertRaises(tool.ContractError): tool.validate_codeql_phase_fields(bad)
            bad = copy.deepcopy(result); bad['codeql_path'] = ''; bad['command'] = ['', 'version', '--format=json']
            with self.assertRaises(tool.ContractError): tool.validate_codeql_phase_fields(bad)
            args.codeql_phase = 'observe'; args.sarif_id = 'synthetic-request'
            for outcome in ('failure', 'cancelled', 'skipped', 'timed_out', 'unknown'):
                args.analysis_outcome = outcome
                self.assertEqual(tool.codeql_phase(args), 1)
                observation = json.loads((attempt / 'receipt-observe.json').read_text())
                self.assertEqual(observation['status'], 'failure')
                self.assertFalse(observation['authorizes_execution'])
                self.assertEqual(observation['authenticated_service_state'], 'remote_pending')
                with self.assertRaises(tool.ContractError): tool.codeql_phase(args)
                (attempt / 'receipt-observe.json').unlink()  # separate synthetic case, not production replay
            (attempt / 'results').mkdir(); (attempt / 'results/python.sarif').write_text(json.dumps({'version': '2.1.0', 'runs': [{'results': []}]}))
            args.analysis_outcome = 'success'
            proof_path = attempt / 'installed-verification.json'; proof = proof_path.read_bytes()
            proof_path.write_text('{"verified": 1}')
            bad = copy.deepcopy(result); bad['installation_verification_sha256'] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
            tool.codeql_write(attempt / 'receipt-probe.json', bad)
            self.assertEqual(tool.codeql_phase(args), 1)
            (attempt / 'receipt-observe.json').unlink()
            proof_path.write_bytes(proof); tool.codeql_write(attempt / 'receipt-probe.json', result)
            self.assertEqual(tool.codeql_phase(args), 0)
            observation = json.loads((attempt / 'receipt-observe.json').read_text())
            self.assertEqual(observation['status'], 'observation_recorded')
            self.assertFalse(observation['authorizes_execution'])
            self.assertEqual(observation['authenticated_service_state'], 'remote_pending')
            (attempt / 'installed-verification.json').unlink()
            outside = temp.parent / 'outside' / extraction.name; outside.mkdir(parents=True)
            with tarfile.open(archive) as tar: tar.extractall(outside, filter='data')
            (temp / 'linked').symlink_to(outside.parent, target_is_directory=True)
            for bad_path in (str(temp / '..' / 'outside' / extraction.name / 'codeql/codeql'),
                             str(outside / 'codeql/codeql'),
                             str(temp / 'linked' / extraction.name / 'codeql/codeql'),
                             str(extraction) + '/./codeql/codeql',
                             str(temp / ('-' * 36) / 'codeql/codeql')):
                with self.subTest(init_path=bad_path), patch.object(tool, 'bounded_codeql_command') as command:
                    with self.assertRaises(tool.ContractError): tool.codeql_probe(attempt, 'linux64', 'python', bad_path, '2.26.0', 'success')
                    command.assert_not_called()
                if (attempt / 'installed-verification.json').exists(): (attempt / 'installed-verification.json').unlink()
            for member in ['jre/bin/java', 'tools/a.jar', 'codeql']:
                member_path = extraction / 'codeql' / member; original = member_path.read_bytes(); member_path.write_bytes(b'changed')
                with patch.object(tool, 'bounded_codeql_command') as command:
                    with self.assertRaises(tool.ContractError): tool.codeql_probe(attempt, 'linux64', 'python', str(codeql), '2.26.0', 'success')
                    command.assert_not_called()
                if (attempt / 'installed-verification.json').exists(): (attempt / 'installed-verification.json').unlink()
                member_path.write_bytes(original)
            for bad_path, version, outcome in [(str(codeql) + chr(10), '2.26.0', 'success'), ('/usr/bin/true', '2.26.0', 'success'), ('relative', '2.26.0', 'success'), (str(codeql), '2.25.0', 'success'), (str(codeql), '2.26.0', 'failure')]:
                with patch.object(tool, 'bounded_codeql_command') as command:
                    with self.assertRaises(tool.ContractError): tool.codeql_probe(attempt, 'linux64', 'python', bad_path, version, outcome)
                    command.assert_not_called()
            for field in ('lock_digest', 'workflow_digest', 'language', 'run_id', 'run_attempt'):
                bad = copy.deepcopy(acquired); bad['binding'][field] = 'wrong'
                tool.codeql_write(attempt / 'receipt-acquire.json', bad)
                with patch.object(tool, 'bounded_codeql_command') as command:
                    with self.assertRaises(tool.ContractError): tool.codeql_probe(attempt, 'linux64', 'python', str(codeql), '2.26.0', 'success')
                    command.assert_not_called()
            for field, value in [('status', 'failure'), ('authorizes_execution', True), ('exit', False), ('mode', 'fetch-only'), ('runnable_here', 'yes')]:
                bad = copy.deepcopy(acquired); bad[field] = value; tool.codeql_write(attempt / 'receipt-acquire.json', bad)
                with self.assertRaises(tool.ContractError): tool.codeql_chain(attempt, 'linux64', 'python')

    def test_codeql_download_transport_size_hash_and_http_failures(self):
        selected = dict(tool.codeql_select(tool.load_lock(ROOT), 'linux64'), size=3, sha256=hashlib.sha256(b'abc').hexdigest())
        class Response(io.BytesIO):
            status = 200
            headers = {}
        for index, content in enumerate((b'ab', b'abcd', b'xyz')):
            with patch.object(tool, 'open_download', return_value=Response(content)):
                with self.assertRaises(tool.ContractError): tool.codeql_download(selected, self.root / ('bad-' + str(index)))
        response = Response(b'abc'); response.status = 403
        with patch.object(tool, 'open_download', return_value=response):
            with self.assertRaises(tool.ContractError): tool.codeql_download(selected, self.root / 'http-failure')

    def test_codeql_worker_hard_deadline_kills_blocked_parser(self):
        class Child:
            pid = 123456789
            calls = 0
            def wait(inner, timeout=None):
                inner.calls += 1
                if timeout is not None: raise subprocess.TimeoutExpired('synthetic', timeout)
                return -9
        with patch.object(tool.subprocess, 'Popen', return_value=Child()), patch.object(tool.os, 'killpg') as kill:
            with self.assertRaises(tool.ContractError): tool.run_codeql_worker('scan', self.root / 'archive', self.root / 'manifest')
            kill.assert_called_once()

    def test_unknown_failure_observations_never_become_service_success(self):
        for status in ('failure', 'skipped', 'cancelled', 'timed_out', 'unknown', ''):
            observation = tool.codeql_observations(status, 'some-id', None)
            self.assertNotEqual(observation['analysis'], 'observed_result')
            self.assertEqual(observation['authenticated_service_state'], 'remote_pending')
            self.assertFalse(observation['authorizes_execution'])
        observation = tool.codeql_observations('success', 'id', {'sha256': 'a' * 64, 'runs': 1, 'results': 0})
        self.assertEqual(observation['authenticated_service_state'], 'remote_pending')
        self.assertFalse(observation['authorizes_execution'])


if __name__ == '__main__': unittest.main()
