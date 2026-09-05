"""Bounded host-owned review bookkeeping; never grants execution authority.

Only an authenticated host consumer may call these APIs. Principal resolution and
work-order execution remain at the existing frontdoor/executor boundary. These
functions do not accept Bot payloads, execute repairs, or attest quality gates.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import run_lock
import run_store

HOST_TYPES = {'human_operator', 'manual_operator', 'harness_runner', 'orchestrator_start'}
PHASES = {'triage', 'repair', 'current_snapshot_validation', 'stopped'}
SEVERITIES = {'critical', 'high', 'medium', 'low'}
STATE_FIELDS = {'version', 'run_id', 'task_id', 'owner', 'activation_digest', 'original_snapshot',
                'snapshot', 'max_repairs', 'same_blocker_limit', 'no_progress_limit', 'repair_rounds',
                'same_blocker_count', 'no_progress', 'last_blockers', 'findings', 'batches',
                'phase', 'stop_reason', 'integration_status'}
FINDING_FIELDS = {'rule_id', 'path', 'anchor', 'task_id', 'mandatory', 'severity', 'evidence_ref'}
STOP_REASONS = {'incidental_mandatory', 'outside_scope', 'contradictory_finding',
                'same_blocker_cap', 'no_progress_cap', 'repair_budget_exhausted'}


class ReviewLifecycleError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ReviewLifecycleError(reason)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def _integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _relative(value: Any) -> bool:
    return (_text(value) and not value.startswith('/') and '\\' not in value
            and all(part not in {'', '.', '..'} for part in value.split('/')))


def _snapshot(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {'repository', 'base', 'head'}
            and _text(value['repository']) and all(isinstance(value[k], str)
            and re.fullmatch(r'[0-9a-f]{40}', value[k]) for k in ('base', 'head')))


def _owner(principal: dict[str, Any]) -> dict[str, str]:
    _require(isinstance(principal, dict), 'invalid_principal')
    _require(principal.get('principal_type') in HOST_TYPES, 'host_principal_required')
    keys = ('principal_type', 'principal_id', 'authn_method')
    _require(all(_text(principal.get(k)) for k in keys), 'invalid_principal')
    return {k: principal[k] for k in keys}


def _key(finding: dict[str, Any]) -> str:
    # Delivery/comment IDs are deliberately excluded. Task ownership conflicts
    # on the same semantic finding must not create another independent finding.
    return _digest({k: finding[k] for k in ('rule_id', 'path', 'anchor')})


def _finding(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == FINDING_FIELDS
            and all(_text(value[k]) for k in ('rule_id', 'anchor', 'task_id', 'evidence_ref'))
            and _relative(value['path']) and type(value['mandatory']) is bool
            and value['severity'] in SEVERITIES)


def validate_record(state: Any, *, run: dict[str, Any]) -> list[str]:
    """Reject corrupt durable state on both load and store, without side effects."""
    try:
        _require(isinstance(state, dict) and set(state) == STATE_FIELDS, 'fields')
        _require(state['version'] == '1', 'version')
        _require(state['run_id'] == run.get('run_id') and state['task_id'] == run.get('task_id'), 'identity')
        _require(_owner(state['owner']) == state['owner'], 'owner')
        _require(isinstance(state['activation_digest'], str)
                 and re.fullmatch(r'[0-9a-f]{64}', state['activation_digest']) is not None, 'activation_digest')
        _require(_snapshot(state['snapshot']) and _snapshot(state['original_snapshot']), 'snapshot')
        _require(all(state['snapshot'][k] == state['original_snapshot'][k]
                     for k in ('repository', 'base')), 'snapshot_scope')
        _require(_integer(state['max_repairs'], 1, 5), 'max_repairs')
        for key in ('same_blocker_limit', 'no_progress_limit'):
            _require(_integer(state[key], 1, 2), key)
        _require(_integer(state['repair_rounds'], 0, state['max_repairs']), 'repair_rounds')
        for key in ('same_blocker_count', 'no_progress'):
            _require(_integer(state[key], 0, state['repair_rounds']), key)
        _require(state['phase'] in PHASES, 'phase')
        _require(state['integration_status'] == 'integration_pending', 'integration_status')
        _require(state['stop_reason'] in STOP_REASONS if state['phase'] == 'stopped'
                 else state['stop_reason'] is None, 'stop_reason')
        findings = state['findings']
        _require(isinstance(findings, dict) and len(findings) <= 256, 'findings')
        for key, row in findings.items():
            _require(isinstance(row, dict) and set(row) == FINDING_FIELDS | {'disposition'}, 'finding_fields')
            _require(_finding({k: row[k] for k in FINDING_FIELDS}) and key == _key(row), 'finding')
            expected = ('defer' if not row['mandatory'] else
                        'stop' if row['task_id'] != state['task_id'] else None)
            _require(row['disposition'] == expected if expected else row['disposition'] in {'repair', 'stop'},
                     'finding_disposition')
        _require(isinstance(state['last_blockers'], list)
                 and state['last_blockers'] == sorted(set(state['last_blockers']))
                 and all(k in findings for k in state['last_blockers']), 'last_blockers')
        batches = state['batches']
        _require(isinstance(batches, dict) and len(batches) == state['repair_rounds'], 'batches')
        for batch_id, batch in batches.items():
            _require(_text(batch_id) and isinstance(batch, dict)
                     and set(batch) == {'findings', 'snapshot', 'status', 'result_snapshot'}, 'batch')
            _require(isinstance(batch['findings'], list) and bool(batch['findings'])
                     and batch['findings'] == sorted(set(batch['findings']))
                     and all(k in findings for k in batch['findings']), 'batch_findings')
            _require(_snapshot(batch['snapshot']) and batch['status'] in {'reserved', 'failed', 'produced'}, 'batch_status')
            _require(_snapshot(batch['result_snapshot']) if batch['status'] == 'produced'
                     else batch['result_snapshot'] is None, 'batch_result')
        reserved = sum(b['status'] == 'reserved' for b in batches.values())
        _require(reserved <= 1 and (reserved == 1 if state['phase'] == 'repair' else
                                  reserved == 0 if state['phase'] != 'stopped' else True), 'active_batch')
    except (ReviewLifecycleError, TypeError, KeyError, ValueError) as exc:
        return [f'review_lifecycle_invalid:{exc}']
    return []


def _live(run: dict[str, Any], owner: dict[str, str], state: dict[str, Any] | None = None) -> None:
    _require(run['run_state'] not in run_store.TERMINAL_RUN_STATES, 'terminal_run')
    activation = run['activation']
    _require(activation.get('activation_status') == 'approved', 'activation_not_approved')
    if state is not None:
        _require(state['owner'] == owner, 'owner_conflict')
        _require(state['activation_digest'] == _digest(activation), 'activation_changed')
    expiry = activation.get('activation_scope', {}).get('expires_at')
    if expiry != 'run_terminal':
        try:
            expires = datetime.fromisoformat(str(expiry).replace('Z', '+00:00'))
            _require(expires.tzinfo is not None and expires > datetime.now(timezone.utc), 'activation_expired')
        except ValueError as exc:
            raise ReviewLifecycleError('activation_expiry_invalid') from exc


def initialize(state_root: Path, run_id: str, *, principal: dict[str, Any], snapshot: dict[str, str],
               max_repairs: int = 5, same_blocker_limit: int = 2, no_progress_limit: int = 2) -> dict[str, Any]:
    owner = _owner(principal)
    _require(_snapshot(snapshot), 'invalid_snapshot')
    _require(_integer(max_repairs, 1, 5) and _integer(same_blocker_limit, 1, 2)
             and _integer(no_progress_limit, 1, 2), 'invalid_budget')
    with run_lock.hold_global_lock(state_root, operation='review_initialize', run_id=run_id, principal=owner):
        run = run_store.load_run(state_root, run_id)
        state = run.get('review_lifecycle')
        _live(run, owner, state)
        if state is not None:
            _require(state['original_snapshot'] == snapshot and state['max_repairs'] == max_repairs
                     and state['same_blocker_limit'] == same_blocker_limit
                     and state['no_progress_limit'] == no_progress_limit, 'initialization_conflict')
            return copy.deepcopy(state)
        state = dict(version='1', run_id=run_id, task_id=run['task_id'], owner=owner,
                     activation_digest=_digest(run['activation']), original_snapshot=copy.deepcopy(snapshot),
                     snapshot=copy.deepcopy(snapshot), max_repairs=max_repairs,
                     same_blocker_limit=same_blocker_limit, no_progress_limit=no_progress_limit,
                     repair_rounds=0, same_blocker_count=0, no_progress=0, last_blockers=[],
                     findings={}, batches={}, phase='triage', stop_reason=None, integration_status='integration_pending')
        run['review_lifecycle'] = state
        run_store.store_run(state_root, run, expected_current_state=run['run_state'])
        return copy.deepcopy(state)


def observe(state_root: Path, run_id: str) -> dict[str, Any]:
    return copy.deepcopy(run_store.load_run(state_root, run_id).get('review_lifecycle'))


def _stop(state: dict[str, Any], reason: str) -> None:
    if state['phase'] != 'stopped':
        state['phase'], state['stop_reason'] = 'stopped', reason


def _in_scope(path: str, run: dict[str, Any]) -> bool:
    scope = run['activation']['activation_scope']
    return scope.get('allowed_ops', {}).get('edit') is True and any(
        _relative(root) and (path == root or PurePosixPath(root) in PurePosixPath(path).parents)
        for root in scope.get('allowed_paths', []))


def _triage(state: dict[str, Any], run: dict[str, Any], rows: Any) -> None:
    _require(isinstance(rows, list) and len(rows) <= 256, 'invalid_findings')
    for raw in rows:
        _require(isinstance(raw, dict) and set(raw) <= FINDING_FIELDS | {'external_id'}, 'invalid_finding_fields')
        row = {k: v for k, v in raw.items() if k != 'external_id'}
        _require(_finding(row), 'invalid_finding')
        key = _key(row)
        previous = state['findings'].get(key)
        if previous:
            if previous['task_id'] != row['task_id']:
                _stop(state, 'contradictory_finding')
                continue
            if previous['mandatory'] or not row['mandatory']:
                continue  # Never downgrade a mandatory obligation through delivery churn.
        row = copy.deepcopy(row)
        if not row['mandatory']:
            row['disposition'] = 'defer'
        elif row['task_id'] != run['task_id']:
            row['disposition'] = 'stop'
            _stop(state, 'incidental_mandatory')
        elif not _in_scope(row['path'], run):
            row['disposition'] = 'stop'
            _stop(state, 'outside_scope')
        else:
            row['disposition'] = 'repair'
        state['findings'][key] = row
    _require(len(state['findings']) <= 256, 'finding_capacity_exhausted')


def apply_event(state_root: Path, run_id: str, *, principal: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    owner = _owner(principal)
    _require(isinstance(event, dict), 'invalid_event')
    fields = {'ci_pending': {'kind'}, 'findings': {'kind', 'findings'},
              'reserve_repair': {'kind', 'batch_id'}, 'repair_failed': {'kind', 'batch_id'},
              'repair_produced': {'kind', 'batch_id', 'snapshot'}}
    kind = event.get('kind')
    _require(isinstance(kind, str) and kind in fields and set(event) == fields[kind], 'unsupported_event')
    with run_lock.hold_global_lock(state_root, operation='review_event', run_id=run_id, principal=owner):
        run = run_store.load_run(state_root, run_id)
        state = run.get('review_lifecycle')
        _require(state is not None, 'lifecycle_not_initialized')
        _live(run, owner, state)
        before = copy.deepcopy(state)
        if kind == 'findings':
            _triage(state, run, event['findings'])
        elif kind != 'ci_pending':
            batch_id = event['batch_id']
            _require(_text(batch_id), 'invalid_batch_id')
            batch = state['batches'].get(batch_id)
            if kind == 'reserve_repair':
                if batch is None:
                    _require(state['phase'] == 'triage', 'repair_phase_blocked')
                    _require(state['repair_rounds'] < state['max_repairs'], 'repair_budget_exhausted')
                    targets = sorted(k for k, row in state['findings'].items() if row['disposition'] == 'repair')
                    _require(bool(targets), 'no_authorized_repair_targets')
                    _require(all(_in_scope(state['findings'][k]['path'], run) for k in targets), 'outside_scope')
                    state['repair_rounds'] += 1
                    state['phase'] = 'repair'
                    state['batches'][batch_id] = dict(findings=targets, snapshot=copy.deepcopy(state['snapshot']),
                                                    status='reserved', result_snapshot=None)
            else:
                _require(batch is not None, 'unknown_batch')
                desired = 'failed' if kind == 'repair_failed' else 'produced'
                if batch['status'] != 'reserved':
                    _require(batch['status'] == desired and (desired == 'failed'
                             or batch['result_snapshot'] == event['snapshot']), 'batch_result_conflict')
                else:
                    _require(state['phase'] == 'repair', 'repair_phase_blocked')
                    batch['status'] = desired
                    if desired == 'produced':
                        snapshot = event['snapshot']
                        _require(_snapshot(snapshot) and snapshot['head'] != state['snapshot']['head']
                                 and all(snapshot[k] == state['snapshot'][k] for k in ('repository', 'base')),
                                 'repair_snapshot_invalid')
                        batch['result_snapshot'] = copy.deepcopy(snapshot)
                        state['snapshot'] = copy.deepcopy(snapshot)
                        state['phase'] = 'current_snapshot_validation'
                        # A changed tree is not proof of finding resolution or progress.
                        # U3 verified evidence consumer must discharge these obligations.
                    else:
                        blockers = batch['findings']
                        state['same_blocker_count'] = state['same_blocker_count'] + 1 if blockers == state['last_blockers'] else 1
                        state['last_blockers'] = list(blockers)
                        state['no_progress'] += 1
                        state['phase'] = 'triage'
                        if state['same_blocker_count'] >= state['same_blocker_limit']:
                            _stop(state, 'same_blocker_cap')
                        elif state['no_progress'] >= state['no_progress_limit']:
                            _stop(state, 'no_progress_cap')
                        elif state['repair_rounds'] >= state['max_repairs']:
                            _stop(state, 'repair_budget_exhausted')
        if state != before:
            run_store.store_run(state_root, run, expected_current_state=run['run_state'])
        return copy.deepcopy(state)
