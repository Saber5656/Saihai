#!/usr/bin/env python3
"""Bounded, read-only validation evidence inspection. No receipt grants authority."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

import delivery_contract
import delivery_workflow_inventory as inventory
from safe_paths import safe_relative_component
from work_order_builder import _validate_schema_fragment

SCHEMA = Path(__file__).resolve().parents[1] / 'schemas/validation-evidence.schema.json'
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 20000
LAYERS = ('static', 'unit', 'feature', 'e2e', 'build', 'security', 'full', 'device')


class EvidenceError(ValueError):
    """Only fixed diagnostic codes, never raw input or private log text."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def bounded(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = [MAX_ITEMS] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise EvidenceError('structure_budget')
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str or not 1 <= len(key) <= 4096:
                raise EvidenceError('invalid_key')
            bounded(child, depth + 1, budget)
    elif type(value) is list:
        if len(value) > 10000:
            raise EvidenceError('array_budget')
        for child in value:
            bounded(child, depth + 1, budget)
    elif type(value) is str:
        if len(value) > MAX_BYTES or '\x00' in value:
            raise EvidenceError('string_budget')
    elif type(value) is float:
        if not math.isfinite(value):
            raise EvidenceError('nonfinite_number')
    elif value is not None and type(value) not in (int, bool):
        raise EvidenceError('non_json_value')


def parse_json(raw: bytes) -> dict:
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise EvidenceError('byte_budget')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError('duplicate_key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        bounded(value)
    except (ValueError, RecursionError, UnicodeError, OverflowError) as exc:
        raise EvidenceError('invalid_json') from exc
    if type(value) is not dict:
        raise EvidenceError('object_required')
    return value


def read_bytes(root: Path, relative: str) -> bytes:
    """Open each component with dirfd/no-follow; never follow a report-authored link."""
    try:
        parts = relative.split('/')
        for part in parts:
            safe_relative_component(part, label='evidence')
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(child)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise EvidenceError('file_type_or_budget')
                with os.fdopen(child, 'rb', closefd=False) as stream:
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise EvidenceError('byte_budget')
                return raw
            finally:
                os.close(child)
        finally:
            os.close(fd)
    except (OSError, ValueError, AttributeError) as exc:
        raise EvidenceError('confined_read_failed') from exc


def read_bound(root: Path, reference: dict) -> bytes:
    if (type(reference) is not dict or set(reference) != {'path', 'sha256', 'bytes'}
            or type(reference['bytes']) is not int or not 0 <= reference['bytes'] <= MAX_BYTES):
        raise EvidenceError('invalid_artifact_reference')
    raw = read_bytes(root, reference['path'])
    if sha(raw) != reference['sha256'] or len(raw) != reference['bytes']:
        raise EvidenceError('artifact_digest_mismatch')
    return raw


def reasons(code: str, path: str = '$') -> dict:
    return {'code': code, 'path': path}


def validate_validation_evidence(value: Any) -> list[dict]:
    try:
        bounded(value)
        if len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES:
            raise EvidenceError('byte_budget')
        errors = _validate_schema_fragment(value, json.loads(SCHEMA.read_text()), '$')
        # Do not echo unexpected user keys or values through schema diagnostics.
        return [reasons('schema_invalid')] if errors else []
    except (EvidenceError, ValueError, TypeError, RecursionError, OverflowError):
        return [reasons('malformed_or_over_budget')]


def check_fragment(value: Any, name: str) -> bool:
    schema = json.loads(SCHEMA.read_text())
    bounded(value)
    return not _validate_schema_fragment(value, schema['$defs'][name], '$', root_schema=schema)


@dataclass(frozen=True)
class ExecutionBundle:
    receipt_bytes: bytes
    validation_bytes: bytes
    artifacts: tuple[tuple[str, bytes], ...] = ()


def load_execution_bundle(root: Path, references: dict) -> ExecutionBundle:
    if not check_fragment(references, 'bundle'):
        raise EvidenceError('bundle_shape')
    rows = references['artifacts']
    paths = [row['path'] for row in rows] + [references[k]['path'] for k in ('receipt', 'validation')]
    if len(set(paths)) != len(paths):
        raise EvidenceError('duplicate_artifact')
    total = references['receipt']['bytes'] + references['validation']['bytes'] + sum(r['bytes'] for r in rows)
    if total > 8 * MAX_BYTES:
        raise EvidenceError('bundle_budget')
    return ExecutionBundle(read_bound(root, references['receipt']), read_bound(root, references['validation']),
                           tuple((r['path'], read_bound(root, r)) for r in rows))


def instant(value: Any) -> datetime:
    if type(value) is not str or len(value) > 64:
        raise EvidenceError('invalid_timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise EvidenceError('invalid_timestamp') from exc
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise EvidenceError('utc_required')
    return result


def period(row: dict, start='start', end='end') -> bool:
    try:
        return instant(row[start]) <= instant(row[end])
    except (KeyError, EvidenceError):
        return False


def terminal(row: dict) -> bool:
    return row.get('status') == 'success' and type(row.get('exit')) is int and row['exit'] == 0 and period(row)


def inspect_native_bundle(bundle: ExecutionBundle) -> dict:
    """Verify actual #144/U0a bytes without inventing legacy fields or provenance."""
    out = {'integrity': 'invalid', 'reasons': [], 'receipt_sha256': None, 'validation_sha256': None}
    try:
        if type(bundle) is not ExecutionBundle:
            raise EvidenceError('byte_bundle_required')
        receipt = parse_json(bundle.receipt_bytes)
        value = parse_json(bundle.validation_bytes)
        out.update(receipt_sha256=sha(bundle.receipt_bytes), validation_sha256=sha(bundle.validation_bytes))
        allowed = {'schema_version','attempt','start','end','status','exit','bootstrap','host','stages',
                   'authorizes_execution','other_platforms','codeql','policy','target_before','target_after',
                   'lock_digest','selected_platform','artifact','dependency_lock_digest','interpreter',
                   'consumer_interpreter','validation_result','error_type','error'}
        if set(receipt) - allowed or type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1:
            raise EvidenceError('receipt_shape')
        if receipt.get('authorizes_execution') is not False or not terminal(receipt):
            raise EvidenceError('receipt_not_success')
        for name, fields in {
                'bootstrap':{'executable','version'},
                'host':{'system','machine','release','ci_image'},
                'artifact':{'version','machine','url','sha256','size','interpreter'},
                'interpreter':{'executable','prefix','version','machine'},
                'consumer_interpreter':{'executable','prefix','version','machine'},
                'target_before':{'head','head_tree','files_digest','working_patch_digest','index_patch_digest','workflow_digest'},
                'target_after':{'head','head_tree','files_digest','working_patch_digest','index_patch_digest','workflow_digest'},
            }.items():
            if type(receipt.get(name)) is not dict or set(receipt[name]) != fields:
                raise EvidenceError('receipt_nested_shape')
        if type(receipt['host']['ci_image']) is not dict or set(receipt['host']['ci_image']) != {'ImageOS','ImageVersion','RUNNER_OS','RUNNER_ARCH'}:
            raise EvidenceError('host_image_shape')
        if receipt.get('target_before') != receipt.get('target_after') or type(receipt.get('target_before')) is not dict:
            raise EvidenceError('target_changed')
        binding = receipt.get('validation_result')
        if (type(binding) is not dict or set(binding) != {'path','sha256','bytes'} or binding['path'] != 'validation.json'
                or type(binding['bytes']) is not int or binding['bytes'] != len(bundle.validation_bytes)
                or binding['sha256'] != sha(bundle.validation_bytes)):
            raise EvidenceError('sanitized_result_binding_missing_or_mismatch')
        if set(value) != {'schema_version','suite_evidence_version','result','compiled','suites','contracts'}:
            raise EvidenceError('suite_metadata_missing_or_unknown')
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['suite_evidence_version']) is not int or value['suite_evidence_version'] != 1
                or value['result'] != 'pass' or value['compiled'] is not True):
            raise EvidenceError('validation_not_passed')
        stages = receipt.get('stages')
        if type(stages) is not list or not stages or len(stages) > 64:
            raise EvidenceError('stages_missing')
        names = []
        previous = instant(receipt['start'])
        for stage in stages:
            fields = {'name','start','end','status','exit'}
            if type(stage) is not dict or not fields <= set(stage) or set(stage)-fields-{'command','log_sha256','error_type'}:
                raise EvidenceError('stage_shape')
            if not terminal(stage) or instant(stage['start']) < previous or instant(stage['end']) > instant(receipt['end']):
                raise EvidenceError('stage_not_terminal_or_ordered')
            previous = instant(stage['end'])
            if type(stage['name']) is not str or stage['name'] in names:
                raise EvidenceError('duplicate_stage')
            names.append(stage['name'])
            if stage['name'] not in ('download','extract'):
                if (type(stage.get('command')) is not list or not stage['command']
                        or any(type(x) is not str or not x for x in stage['command'])
                        or not valid_hash(stage.get('log_sha256'))):
                    raise EvidenceError('command_stage_metadata_missing')
        if 'full' not in names:
            raise EvidenceError('full_stage_missing')
        interpreter = receipt.get('consumer_interpreter', {})
        if type(interpreter) is not dict or set(interpreter) != {'executable','prefix','version','machine'}:
            raise EvidenceError('consumer_interpreter_missing')
        python = interpreter['executable']
        if type(python) is not str or not python.startswith('/') or python != interpreter['prefix'] + '/bin/python3':
            raise EvidenceError('selected_interpreter_mismatch')
        full = stages[names.index('full')]
        if full['command'] != [python, '-B', 'scripts/validate_all.py']:
            raise EvidenceError('full_command_mismatch')
        suite_fields = {'path','result','cases','command','cwd','started_at','finished_at','exit_code','status',
                        'executed','failed','skipped','unknown','count_method','duration_seconds'}
        suites = value['suites']
        if type(suites) is not list or not 1 <= len(suites) <= 1000:
            raise EvidenceError('required_suites_missing')
        seen = set()
        for suite in suites:
            if type(suite) is not dict or set(suite) != suite_fields:
                raise EvidenceError('suite_shape')
            path = suite['path']
            if not relative_path(path) or path in seen:
                raise EvidenceError('duplicate_or_unsafe_suite')
            seen.add(path)
            if (suite['result'] != 'pass' or suite['status'] != 'passed'
                    or type(suite['exit_code']) is not int or suite['exit_code'] != 0
                    or any(type(suite[k]) is not int or suite[k] < 0 for k in ('cases','executed','failed','skipped','unknown'))
                    or suite['cases'] != suite['executed'] or suite['executed'] <= 0
                    or any(suite[k] != 0 for k in ('failed','skipped','unknown'))):
                raise EvidenceError('required_count_or_outcome_invalid')
            if (suite['count_method'] not in ('structured_result','completed_test_functions','unittest_summary')
                    or not period(suite,'started_at','finished_at')
                    or instant(suite['started_at']) < instant(full['start']) or instant(suite['finished_at']) > instant(full['end'])
                    or type(suite['duration_seconds']) not in (int,float) or not 0 <= suite['duration_seconds'] <= 1e9):
                raise EvidenceError('suite_measurement_invalid')
            if (type(suite['cwd']) is not str or not suite['cwd'].startswith('/')
                    or suite['command'] != [python, suite['cwd']+'/'+path]):
                raise EvidenceError('suite_command_mismatch')
        expected_contracts = [[python,'organization/runtime/workflows/scripts/workflow_selector.py','validate-contracts'],
                              [python,'organization/runtime/workflows/scripts/template_role_validator.py']]
        if value['contracts'] != [{'command':c,'result':'pass'} for c in expected_contracts]:
            raise EvidenceError('contracts_missing_or_not_passed')
        out.update(integrity='consistent', receipt=receipt, validation=value)
    except (EvidenceError, KeyError, TypeError, ValueError, OverflowError):
        # Detailed fixed error codes are safe; malformed type failures remain generic.
        import sys
        exc = sys.exc_info()[1]
        out['reasons'].append(reasons(str(exc) if type(exc) is EvidenceError else 'malformed_bundle'))
    return out


def valid_hash(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def relative_path(value: Any) -> bool:
    try:
        if type(value) is not str or len(value) > 4096:
            return False
        for part in value.split('/'):
            safe_relative_component(part, label='path')
        return True
    except ValueError:
        return False


def observe_repository(root: Path, target: dict, expected_inventory: dict) -> dict:
    """Fixed Git reads and confined file reads; does not execute supplied commands."""
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvidenceError('git_identity_unavailable') from exc
    if not check_fragment(target, 'target'):
        raise EvidenceError('target_shape')
    root = root.absolute()
    head = git('rev-parse','HEAD').decode().strip()
    baseline = target['base']
    names = sorted(set(os.fsdecode(p) for p in git('ls-files','-co','--exclude-standard','-z').split(b'\0') if p))
    if len(names) > 10000:
        raise EvidenceError('target_file_budget')
    files = []
    producer_files = {}
    for name in names:
        if not relative_path(name):
            raise EvidenceError('unsafe_target_path')
        path = root/name
        # Refuse intermediate symlinks; final symlinks are identity, never followed.
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise EvidenceError('target_parent_symlink')
        if path.is_symlink():
            raw = os.fsencode(os.readlink(path)); mode = '120000'
        elif path.is_file():
            raw = read_bytes(root,name); mode = '100755' if path.stat().st_mode & 0o111 else '100644'
        elif not path.exists():
            files.append({'path':name,'mode':'000000','sha256':None,'bytes':0})
            continue
        else:
            raise EvidenceError('nonregular_target')
        files.append({'path':name,'mode':mode,'sha256':sha(raw),'bytes':len(raw)})
        producer_files[name] = {'mode':oct(path.lstat().st_mode),'sha256':sha(raw)}
    tree_rows = git('ls-tree','-rz',target['snapshot']['tree']).split(b'\0')
    tree_files = []
    for row in tree_rows:
        if not row:
            continue
        meta, name = row.split(b'\t',1); mode, kind, oid = meta.split()
        if kind != b'blob':
            raise EvidenceError('unsupported_tree_entry')
        if int(git('cat-file','-s',oid.decode())) > MAX_BYTES:
            raise EvidenceError('tree_blob_budget')
        raw = git('cat-file','blob',oid.decode())
        if len(raw) > MAX_BYTES:
            raise EvidenceError('tree_blob_budget')
        tree_files.append({'path':os.fsdecode(name),'mode':mode.decode(),'sha256':sha(raw),'bytes':len(raw)})
    actual_present = [row for row in files if row['mode'] != '000000']
    tree_matches = sorted(tree_files,key=lambda x:x['path']) == actual_present
    workflow_dir = root/'.github/workflows'
    if workflow_dir.is_symlink() or workflow_dir.parent.is_symlink():
        raise EvidenceError('workflow_symlink')
    sources = {p.relative_to(root).as_posix():read_bytes(root,p.relative_to(root).as_posix()).decode()
               for p in sorted([*workflow_dir.glob('*.yml'),*workflow_dir.glob('*.yaml')])}
    lock_names = expected_inventory.get('lock_digests',{})
    if type(lock_names) is not dict or len(lock_names) > 32:
        raise EvidenceError('lock_inventory_shape')
    locks = {name:sha(read_bytes(root,name)) for name in lock_names}
    producer = {'head':head,'head_tree':git('rev-parse','HEAD^{tree}').decode().strip(),
                'files_digest':digest(producer_files),'working_patch_digest':sha(git('diff','HEAD','--binary','--full-index')),
                'index_patch_digest':sha(git('diff','--cached','--binary','--full-index')),
                'workflow_digest':sha(read_bytes(root,'.github/workflows/validate.yml')) if '.github/workflows/validate.yml' in sources else None}
    actual_snapshot = {'kind':target['snapshot']['kind'],'head':head if target['snapshot']['kind']=='commit' else None,
                       'tree':target['snapshot']['tree'],'files':files,'patch_sha256':sha(git('diff',baseline,'--binary','--full-index'))}
    return {'target':{**target,'snapshot':actual_snapshot},'tree_matches':tree_matches,'checkout_head':head,
            'producer_target':producer,'sources':sources,'lock_digests':locks,'expected':expected_inventory,
            'toolchain_lock':parse_json(read_bytes(root,'.github/delivery-toolchain.lock.json')) if (root/'.github/delivery-toolchain.lock.json').is_file() else None,
            'toolchain_lock_sha256':sha(read_bytes(root,'.github/delivery-toolchain.lock.json')) if (root/'.github/delivery-toolchain.lock.json').is_file() else None}


def artifact_present(bundle: ExecutionBundle, ref: dict | None) -> bool:
    if ref is None:
        return False
    return any(name == ref['path'] and sha(raw) == ref['sha256'] and len(raw) == ref['bytes']
               for name,raw in bundle.artifacts)


def artifact_matches(bundle: ExecutionBundle, ref: dict | None, expected: dict) -> bool:
    if not artifact_present(bundle, ref):
        return False
    try:
        raw = next(raw for name, raw in bundle.artifacts if name == ref['path'])
        return parse_json(raw) == expected
    except EvidenceError:
        return False


def _test_record(record: dict, bundle: ExecutionBundle, outcome: str) -> bool:
    if record is None or not period(record) or not artifact_present(bundle,record['result']):
        return False
    if (record['status'] != outcome or record['executed'] <= 0 or record['skipped'] or record['unknown']
            or (outcome=='passed' and (record['exit'] != 0 or record['failed']))
            or (outcome=='failed' and (record['exit']==0 or record['failed']<=0))):
        return False
    # The actual bounded result artifact must contain this exact event (no recursive ref).
    expected = {k:v for k,v in record.items() if k != 'result'}
    raw = next(raw for name,raw in bundle.artifacts if name == record['result']['path'])
    try:
        return parse_json(raw) == expected
    except EvidenceError:
        return False


def _applicability(change: dict, target: dict, profile: dict, bundle: ExecutionBundle, add, pending) -> None:
    layers = change['layers']
    if sorted(x['name'] for x in layers) != sorted(LAYERS):
        add('layer_inventory_missing_or_duplicate')
    for layer in layers:
        if layer['applicability'] != 'required':
            # No authenticated adoption producer is available in this consumer.
            add('applicability_unverified',layer['name'])
            if not artifact_present(bundle,layer['policy_ref']):
                add('applicability_evidence_missing',layer['name'])
        if profile['layers'].get(layer['name'],{}).get('required') and layer['applicability'] != 'required':
            add('profile_required_layer_downgraded',layer['name'])
    if change['behavior'] != 'behavioral':
        add('change_classification_unverified')
    else:
        tdd = change['tdd']; red=tdd['red']; green=tdd['green']
        if not _test_record(red,bundle,'failed'):
            add('original_red_missing_or_invalid')
        if not _test_record(green,bundle,'passed'):
            add('green_missing_or_invalid')
        if red is not None and green is not None:
            if (red['target']['repository'] != target['repository'] or red['target']['task'] != target['task']
                    or red['target']['unit'] != target['unit'] or red['target']['base'] != target['base']
                    or green['target'] != target or red['target']['snapshot'] == green['target']['snapshot']
                    or not period(red) or not period(green) or instant(red['end']) > instant(green['start'])):
                add('test_first_order_or_target_mismatch')
            if red['command'] != green['command']:
                add('red_green_command_mismatch')
            if red['tests'] != green['tests'] or not red['tests']:
                add('red_tests_not_retained')
            for event in (red,green):
                if any(row not in event['target']['snapshot']['files'] for row in event['tests']):
                    add('test_snapshot_mismatch')
        if tdd['refactor']=='unknown' or (tdd['refactor']=='performed' and
                (not _test_record(tdd['refactor_result'],bundle,'passed') or tdd['refactor_result']['target'] != target)):
            add('refactor_validation_missing')
        if tdd['refactor']=='none' and tdd['refactor_result'] is not None:
            add('refactor_record_conflict')
        pending.add('test_first_provenance_unknown')
    if profile['project_type']=='mobile' and change['behavior']=='behavioral':
        if not change['devices']:
            add('development_physical_device_missing')
        for device in change['devices']:
            if (device['surface']!='physical' or device['status']!='passed' or not period(device)
                    or change['build_sha256'] is None or device['artifact_sha256']!=change['build_sha256']
                    or not artifact_present(bundle,device['image']) or not artifact_present(bundle,device['result'])):
                add('physical_device_evidence_invalid')
            if not artifact_matches(bundle, device['result'], {k:v for k,v in device.items() if k not in {'result','provenance'}}):
                add('physical_device_result_mismatch')
            if not artifact_present(bundle,device['provenance']):
                add('physical_device_provenance_missing')
        pending.add('physical_device_provenance_unknown')
    start=change['project_start']
    if start['repository']!=target['repository'] or start['profile_sha256']!=change['profile_sha256']:
        add('project_start_binding_mismatch')
    if any('example' in start[k].lower() for k in ('ci_owner','cd_owner','rollback_owner')):
        add('project_owner_placeholder')
    if not artifact_present(bundle,start['owner_evidence']):
        add('project_owner_evidence_missing')
    elif not artifact_matches(bundle, start['owner_evidence'], {k:v for k,v in start.items() if k != 'owner_evidence'}):
        add('project_owner_evidence_mismatch')
    pending.add('project_owner_adoption_unknown')


def assess_validation_evidence(*, expected_target, delivery_profile, inventory_observation,
                               execution_bundle, change_contract, phase) -> dict:
    result={'assessment_version':1,'integrity':'invalid','readiness':'blocked','authorizes_execution':False,
            'layers':[],'cells':[],'blocking_reasons':[],'pending_dependencies':[], 'bindings':{}}
    def add(code,path='$'):
        item=reasons(code,path)
        if item not in result['blocking_reasons']:
            result['blocking_reasons'].append(item)
    pending={'policy_adoption_unknown','execution_provenance_unknown','runtime_gate_wiring_pending'}
    try:
        if phase not in ('change_unit','final_pre_pr','remote_after_pr') or not check_fragment(expected_target,'target') or not check_fragment(change_contract,'change'):
            raise EvidenceError('assessment_contract_invalid')
        if delivery_contract.validate_profile(delivery_profile):
            raise EvidenceError('delivery_profile_invalid')
        target=expected_target; change=change_contract; observed=inventory_observation
        if delivery_profile['repository']!=target['repository'] or change['target']!=target:
            add('repository_or_change_identity_mismatch')
        if change['profile_sha256']!=digest(delivery_profile):
            add('profile_digest_mismatch')
        if type(observed) is not dict or not observed.get('tree_matches') or observed.get('target')!=target:
            add('current_source_identity_mismatch')
        if target['snapshot']['kind']=='intended' and target['snapshot']['head'] is not None:
            add('intended_tree_is_not_commit')
        if target['snapshot']['kind']=='commit' and target['snapshot']['head'] is None:
            add('commit_head_missing')
        if change['inventory_sha256']!=digest(observed['expected']):
            add('inventory_digest_mismatch')
        contexts=[c['context'] for c in change['commands']]
        if any(x!=contexts[0] for x in contexts):
            add('event_context_ambiguous')
        audit=inventory.audit_inventory(sources=observed['sources'],expected=observed['expected'],
                  target={'repository':target['repository'],'head_sha':observed['checkout_head'],'base_sha':target['base']},
                  event_context=contexts[0],lock_digests=observed['lock_digests'])
        result['inventory']={k:audit.get(k) for k in ('parsing','parity','readiness','policy_status','authorizes_execution')}
        if audit.get('parsing')!='valid' or audit.get('parity')!='match':
            add('workflow_or_lock_inventory_mismatch')
        if audit.get('gaps'):
            pending.add('workflow_quality_or_policy_gaps')
        for lock in delivery_profile['dependencies']['lockfiles']:
            if observed['lock_digests'].get(lock['path'])!=lock['sha256']:
                add('profile_dependency_lock_mismatch')
        native=inspect_native_bundle(execution_bundle)
        result['bindings']={'target_sha256':digest(target),'profile_sha256':digest(delivery_profile),
                            'inventory_sha256':digest(observed['expected']),'lock_sha256':digest(observed['lock_digests']),
                            'receipt_sha256':native['receipt_sha256'],'validation_sha256':native['validation_sha256']}
        result['integrity']=native['integrity']
        pending.add('workflow_to_local_execution_provenance_unknown')
        if native['integrity']!='consistent':
            for reason in native['reasons']:
                add(reason['code'])
        receipt=native.get('receipt',{}); validation=native.get('validation',{})
        lock=observed.get('toolchain_lock')
        if type(lock) is not dict or receipt.get('lock_digest')!=observed.get('toolchain_lock_sha256'):
            add('runtime_lock_binding_missing')
        else:
            selected=lock.get('python',{}).get(receipt.get('selected_platform'))
            if selected!=receipt.get('artifact') or lock.get('dependency_lock',{}).get('sha256')!=receipt.get('dependency_lock_digest'):
                add('selected_runtime_lock_mismatch')
            for probe in ('interpreter','consumer_interpreter'):
                if (type(selected) is not dict or receipt.get(probe,{}).get('version')!=selected.get('version')
                        or receipt.get(probe,{}).get('machine')!=selected.get('machine')):
                    add('interpreter_artifact_mismatch')
        if receipt.get('target_before')!=observed.get('producer_target'):
            add('receipt_current_identity_mismatch')
        # Canonical matrix + workflow/job + physical step ordinal; not check-name matching.
        jobs={row['cell_id']:row for row in audit['jobs']}; required_steps=set()
        for key,row in jobs.items():
            for index,step in enumerate(row['contract']['job'].get('steps',[])):
                if 'run' in step:
                    required_steps.add((key,index))
        seen=set()
        for command in change['commands']:
            matrix=command['matrix']
            if len({x['name'] for x in matrix})!=len(matrix):
                add('duplicate_matrix_key')
            cell=command['workflow']+'::'+command['job']+'::'+json.dumps({x['name']:x['value'] for x in matrix},sort_keys=True,separators=(',',':'))
            key=(cell,command['ordinal']); row=jobs.get(cell); state='missing'
            if key in seen:
                add('duplicate_command_cell')
            seen.add(key)
            if row is None or key not in required_steps:
                add('unexpected_command_cell')
            else:
                step=row['contract']['job']['steps'][command['ordinal']]
                invocation=command['invocation']
                # Workflow run bodies are shell contracts. No invented shell-to-argv equivalence.
                if command['workflow_body']!=step['run'] or (invocation['kind']=='shell' and (invocation['body']!=step['run'] or invocation['argv'])):
                    add('workflow_command_contract_mismatch')
                if inventory.applicability(row,command['context'])['state']!='applicable':
                    add('event_not_applicable')
                services=row['contract']['job'].get('services',{})
                if command['services_sha256']!=digest(services):
                    add('service_contract_mismatch')
                if services:
                    pending.add('service_execution_provenance_unknown')
            if command['placement']=='remote':
                state='remote_pending' if phase=='final_pre_pr' else 'missing'
                pending.add('remote_execution_missing')
            elif native['integrity']=='consistent':
                matches=[s for s in validation['suites'] if command['source']=='suite' and s['path']==command['result_name']]
                if command['source']=='stage':
                    matches=[s for s in receipt['stages'] if s['name']==command['result_name']]
                if len(matches)==1:
                    actual=matches[0]; runner=command['runner']; host=receipt.get('host',{}); runtime=receipt.get('artifact',{})
                    good=(command['invocation']['kind']=='argv' and command['invocation']['body'] is None
                          and command['invocation']['argv']==actual.get('command')
                          and command['interpreter']==receipt.get('consumer_interpreter',{}).get('executable')
                          and command['runtime_sha256']==runtime.get('sha256')
                          and any(r['sha256']==command['runtime_sha256'] for r in delivery_profile['runtimes'])
                          and command['dependency_lock_sha256']==receipt.get('dependency_lock_digest')
                          and runner['os']==host.get('system') and runner['arch']==host.get('machine')
                          and runner['image']==host.get('ci_image',{}).get('ImageVersion')
                          and not (row and row['contract']['job'].get('services')))
                    if command['source']=='suite':
                        good=good and command['cwd']==actual['cwd']
                    else:
                        # Native stage receipt does not record cwd/count for arbitrary layers.
                        good=False
                    state='consistent' if good else 'local_unavailable'
                else:
                    state='local_unavailable'
            if state!='consistent':
                add(state,command['layer'])
            result['cells'].append({'cell_sha256':digest([cell,command['ordinal']]),'layer':command['layer'],'state':state})
        if seen!=required_steps:
            add('command_inventory_missing_or_extra')
        for name in LAYERS:
            cells=[x for x in result['cells'] if x['layer']==name]
            state='consistent' if cells and all(x['state']=='consistent' for x in cells) else 'missing_or_unverified'
            result['layers'].append({'layer':name,'state':state})
            if not cells:
                add('layer_command_missing',name)
        _applicability(change,target,delivery_profile,execution_bundle,add,pending)
        if phase!='change_unit' and not artifact_present(execution_bundle,change['commit_bridge']):
            add('immutable_commit_bridge_missing')
        if phase!='change_unit':
            pending.add('commit_bridge_provenance_unknown')
    except (EvidenceError, KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError):
        add('malformed_or_incomplete_evidence')
    result['pending_dependencies']=sorted(pending)
    return result


def inspect_request(root: Path, repository_root: Path, request: dict) -> dict:
    errors=validate_validation_evidence(request)
    if errors:
        return {'readiness':'blocked','authorizes_execution':False,'blocking_reasons':errors}
    profile=parse_json(read_bound(root,request['delivery_profile']))
    expected=parse_json(read_bound(root,request['inventory']))
    observed=observe_repository(repository_root,request['expected_target'],expected)
    bundle=load_execution_bundle(root,request['execution_bundle'])
    return assess_validation_evidence(expected_target=request['expected_target'],delivery_profile=profile,
              inventory_observation=observed,execution_bundle=bundle,change_contract=request['change_contract'],phase=request['phase'])


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--repository-root',type=Path)
    parser.add_argument('--request',required=True)
    args=parser.parse_args()
    try:
        result=inspect_request(args.root,args.repository_root or args.root,parse_json(read_bytes(args.root,args.request)))
    except (EvidenceError,OSError,UnicodeError):
        result={'readiness':'blocked','authorizes_execution':False,'blocking_reasons':[reasons('evidence_read_failed')]}
    print(json.dumps(result,sort_keys=True,allow_nan=False))
    raise SystemExit(1)  # Diagnostic consistency never grants adoption or runtime permission.


if __name__=='__main__':
    main()
