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
        _require(isinstance(state, dict) and set(state) - {'intakes'} == STATE_FIELDS, 'fields')
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
        if 'intakes' in state:
            _validate_intakes(state)
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


# U2 candidates are deliberately inert. Authenticated publisher/observer
# receipts and the effective policy must be integrated separately in U3.
INTAKE_FIELDS = {'repository', 'pr', 'bot', 'policy_version', 'trigger_mode', 'original_snapshot',
                 'request_status', 'request_candidate', 'first_response_candidate',
                 'later_response_candidates', 'authentication_status', 'policy_status'}
CANDIDATE_IDENTITY = {'repository', 'pr', 'bot', 'policy_version', 'snapshot', 'request_id'}
REQUEST_FIELDS = CANDIDATE_IDENTITY | {'trigger_mode'}
RESPONSE_FIELDS = CANDIDATE_IDENTITY | {'response_id', 'outcome', 'findings'}
CANDIDATE_FINDING_FIELDS = {'rule_id', 'path', 'anchor', 'severity', 'summary'}
TRIGGER_MODES = {'manual', 'automatic_initial'}


def intake_key(repository: str, pr: int, bot: str, policy_version: str) -> str:
    _require(all(_text(v) for v in (repository, bot, policy_version))
             and _integer(pr, 1, 2**53 - 1), 'invalid_intake_identity')
    _require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) is not None
             and all(part not in {'.', '..'} for part in repository.split('/'))
             and re.fullmatch(r'[A-Za-z0-9_.-]+(?:\[bot\])?', bot) is not None, 'invalid_intake_identity')
    return _digest(dict(repository=repository.casefold(), pr=pr, bot=bot.casefold(), policy_version=policy_version))


def _candidate_matches(candidate: Any, intake: dict[str, Any], *, response: bool, later: bool = False) -> bool:
    if not isinstance(candidate, dict) or set(candidate) != (RESPONSE_FIELDS if response else REQUEST_FIELDS):
        return False
    if not (_integer(candidate['pr'], 1, 2**53 - 1) and _text(candidate['request_id'])
            and _snapshot(candidate['snapshot'])):
        return False
    if not all(candidate[k] == intake[k] for k in ('repository', 'pr', 'bot', 'policy_version')):
        return False
    original = intake['original_snapshot']
    snapshot = candidate['snapshot']
    if not all(snapshot[k] == original[k] for k in ('repository', 'base')):
        return False
    if not later and snapshot != original:
        return False
    if not response:
        return candidate['trigger_mode'] == intake['trigger_mode']
    request = intake['request_candidate']
    if not request or candidate['request_id'] != request['request_id'] or not _text(candidate['response_id']):
        return False
    findings = candidate['findings']
    if not isinstance(findings, list) or len(findings) > 256:
        return False
    if candidate['outcome'] not in ('no_findings', 'findings'):
        return False
    if (candidate['outcome'] == 'no_findings') != (len(findings) == 0):
        return False
    return all(isinstance(row, dict) and set(row) == CANDIDATE_FINDING_FIELDS
               and all(_text(row[k]) for k in ('rule_id', 'anchor', 'summary'))
               and _relative(row['path']) and isinstance(row['severity'], str)
               and row['severity'] in SEVERITIES for row in findings)


def _response_identity(candidate: dict[str, Any]) -> str:
    return _digest({k: v for k, v in candidate.items() if k != 'response_id'})


def _validate_intakes(state: dict[str, Any]) -> None:
    intakes = state['intakes']
    _require(isinstance(intakes, dict) and len(intakes) <= 16, 'intakes')
    for key, intake in intakes.items():
        _require(isinstance(intake, dict) and set(intake) - {'alternate'} == INTAKE_FIELDS, 'intake_fields')
        _require(key == intake_key(*(intake[k] for k in ('repository', 'pr', 'bot', 'policy_version'))), 'intake_key')
        _require(_snapshot(intake['original_snapshot'])
                 and all(intake['original_snapshot'][k] == state['original_snapshot'][k]
                         for k in ('repository', 'base')), 'intake_snapshot')
        _require(intake['repository'] == intake['original_snapshot']['repository'], 'intake_repository')
        _require(isinstance(intake['trigger_mode'], str) and intake['trigger_mode'] in TRIGGER_MODES, 'trigger_mode')
        _require(intake['authentication_status'] == 'integration_pending'
                 and intake['policy_status'] == 'inactive', 'intake_authority_forbidden')
        if 'alternate' in intake:
            _validate_alternate(intake, state)
        status = intake['request_status']
        _require(status in ('planned', 'unknown', 'observed'), 'request_status')
        _require(_candidate_matches(intake['request_candidate'], intake, response=False) if status == 'observed'
                 else intake['request_candidate'] is None, 'request_candidate')
        first = intake['first_response_candidate']
        _require(first is None or status == 'observed'
                 and _candidate_matches(first, intake, response=True), 'first_response_candidate')
        later = intake['later_response_candidates']
        _require(isinstance(later, list) and len(later) <= 32 and (first is not None or not later), 'later_candidates')
        _require(all(_candidate_matches(row, intake, response=True, later=True) for row in later), 'later_candidate')
        identities = [_response_identity(row) for row in later]
        _require(len(set(identities)) == len(identities)
                 and (first is None or _response_identity(first) not in identities), 'duplicate_response_candidate')


def _existing_intake_owner(state_root: Path, key: str) -> str | None:
    """Complete private run scan under the caller's host lock; never skip errors.

    Do not use load_run here: its corrupt-JSON quarantine is useful for normal
    loading but must not remove a potential owner from the next lookup.
    Quarantine/error records themselves make ownership completeness unknown.
    """
    paths = run_store.list_private_artifacts(state_root / 'runs')
    _require(len(paths) <= 10000, 'intake_owner_lookup_incomplete')
    owner = None
    for path in paths:
        _require(path.suffix == '.json' and not run_store.RESERVED_ARTIFACT_SUFFIX_RE.search(path.stem),
                 'intake_owner_lookup_incomplete')
        run = run_store.read_json(path)
        _require(not run_store.validate_run_record(run) and run.get('run_id') == path.stem,
                 'intake_owner_lookup_incomplete')
        if key in run.get('review_lifecycle', {}).get('intakes', {}):
            _require(owner is None, 'duplicate_intake_owner_records')
            owner = run['run_id']
    return owner


def prepare_intake(state_root: Path, run_id: str, *, principal: dict[str, Any], pr: int,
                   bot: str, policy_version: str, trigger_mode: str) -> dict[str, Any]:
    """Record a unique inactive intake plan; returns no outbound execution grant."""
    owner = _owner(principal)
    _require(isinstance(trigger_mode, str) and trigger_mode in TRIGGER_MODES, 'invalid_trigger_mode')
    with run_lock.hold_global_lock(state_root, operation='review_intake_prepare', run_id=run_id, principal=owner):
        run = run_store.load_run(state_root, run_id)
        state = run.get('review_lifecycle')
        _require(state is not None, 'lifecycle_not_initialized')
        _live(run, owner, state)
        key = intake_key(state['snapshot']['repository'], pr, bot, policy_version)
        existing_owner = _existing_intake_owner(state_root, key)
        _require(existing_owner in (None, run_id), 'intake_owned_by_other_run')
        intakes = state.setdefault('intakes', {})
        if key in intakes:
            _require(intakes[key]['trigger_mode'] == trigger_mode, 'trigger_mode_conflict')
            return copy.deepcopy(intakes[key])
        _require(len(intakes) < 16, 'intake_capacity_exhausted')
        intake = dict(repository=state['snapshot']['repository'], pr=pr, bot=bot,
                      policy_version=policy_version, trigger_mode=trigger_mode,
                      original_snapshot=copy.deepcopy(state['snapshot']), request_status='planned',
                      request_candidate=None, first_response_candidate=None, later_response_candidates=[],
                      authentication_status='integration_pending', policy_status='inactive')
        intakes[key] = intake
        run_store.store_run(state_root, run, expected_current_state=run['run_state'])
        return copy.deepcopy(intake)


def record_intake_candidate(state_root: Path, run_id: str, *, principal: dict[str, Any],
                            event: dict[str, Any]) -> dict[str, Any]:
    """Store typed observations only. No authentication or acceptance is inferred."""
    owner = _owner(principal)
    _require(isinstance(event, dict), 'invalid_intake_event')
    kind = event.get('kind')
    _require(isinstance(kind, str) and kind in (
        'delivery_unknown', 'request_observed', 'response_observed', 'later_response_observed',
        'quota_observed', 'alternate_delivery_unknown', 'alternate_request_observed', 'alternate_response_observed',
        'alternate_existing_intake_linked'),
             'unsupported_intake_event')
    fields = {'kind', 'intake_key'} | (set() if kind in ('delivery_unknown', 'alternate_delivery_unknown') else {'candidate'})
    if kind == 'alternate_existing_intake_linked':
        fields = {'kind', 'intake_key', 'source_intake_key'}
    _require(set(event) == fields and isinstance(event['intake_key'], str), 'invalid_intake_event')
    with run_lock.hold_global_lock(state_root, operation='review_intake_candidate', run_id=run_id, principal=owner):
        run = run_store.load_run(state_root, run_id)
        state = run.get('review_lifecycle')
        _require(state is not None, 'lifecycle_not_initialized')
        _live(run, owner, state)
        key = event['intake_key']
        _require(_existing_intake_owner(state_root, key) == run_id, 'intake_owner_unknown')
        intake = state.get('intakes', {}).get(key)
        _require(intake is not None, 'intake_not_initialized')
        before = copy.deepcopy(intake)
        if kind == 'alternate_request_observed':
            for bot in ('chatgpt', 'codex'):
                alternate_key = intake_key(intake['repository'], intake['pr'], bot, intake['policy_version'])
                _require(_existing_intake_owner(state_root, alternate_key) is None,
                         'existing_alternate_intake_requires_reconciliation')
        if kind == 'quota_observed' or kind.startswith('alternate_'):
            _record_alternate(intake, state, event)
        elif kind == 'delivery_unknown':
            if intake['request_status'] == 'planned':
                intake['request_status'] = 'unknown'
        elif kind == 'request_observed':
            candidate = event['candidate']
            _require(_candidate_matches(candidate, intake, response=False), 'invalid_request_candidate')
            _require(intake['request_candidate'] is None or intake['request_candidate'] == candidate,
                     'request_candidate_conflict')
            intake['request_candidate'] = copy.deepcopy(candidate)
            intake['request_status'] = 'observed'
        else:
            candidate = event['candidate']
            later = kind == 'later_response_observed'
            _require(intake['request_status'] == 'observed'
                     and _candidate_matches(candidate, intake, response=True, later=later), 'invalid_response_candidate')
            first = intake['first_response_candidate']
            if not later:
                _require(first is None or _response_identity(first) == _response_identity(candidate), 'baseline_immutable')
                if first is None:
                    intake['first_response_candidate'] = copy.deepcopy(candidate)
            else:
                _require(first is not None, 'baseline_missing')
                candidates = intake['later_response_candidates']
                seen = {_response_identity(row) for row in [first, *candidates]}
                if _response_identity(candidate) not in seen:
                    _require(len(candidates) < 32, 'later_candidate_capacity_exhausted')
                    candidates.append(copy.deepcopy(candidate))
        if before != intake:
            run_store.store_run(state_root, run, expected_current_state=run['run_state'])
        return copy.deepcopy(intake)


QUOTA_FIELDS = CANDIDATE_IDENTITY | {'reason', 'issuer', 'evidence_ref', 'evidence_digest'}
ALTERNATE_FIELDS = {'failure_candidate', 'request_status', 'request_candidate', 'response_candidate', 'status'}


def _quota_matches(candidate: Any, intake: dict[str, Any]) -> bool:
    # Issuer/evidence are unverified candidate metadata, never authentication.
    if not isinstance(candidate, dict) or set(candidate) != QUOTA_FIELDS:
        return False
    request = intake['request_candidate']
    return (intake['bot'].casefold() == 'coderabbitai' and request is not None
            and all(candidate[k] == request[k] for k in CANDIDATE_IDENTITY)
            and candidate['reason'] == 'usage_limit'
            and all(_text(candidate[k]) for k in ('issuer', 'evidence_ref'))
            and isinstance(candidate['evidence_digest'], str)
            and re.fullmatch(r'[0-9a-f]{64}', candidate['evidence_digest']) is not None)


def _alternate_intake(intake: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return dict(intake, bot=request['bot'], trigger_mode=request['trigger_mode'],
                original_snapshot=request['snapshot'], request_candidate=request)


def _alternate_request_matches(candidate: Any, intake: dict[str, Any], *, linked: bool = False) -> bool:
    if not isinstance(candidate, dict) or set(candidate) != REQUEST_FIELDS or not _snapshot(candidate['snapshot']):
        return False
    return ((linked or candidate['bot'] == 'chatgpt' and candidate['trigger_mode'] == 'manual')
            and candidate['bot'] in ('chatgpt', 'codex')
            and all(candidate['snapshot'][k] == intake['original_snapshot'][k] for k in ('repository', 'base'))
            and candidate['request_id'] != intake['request_candidate']['request_id']
            and _candidate_matches(candidate, _alternate_intake(intake, candidate), response=False))


def _alternate_response_matches(candidate: Any, intake: dict[str, Any], request: Any) -> bool:
    if request is None or not isinstance(candidate, dict) or set(candidate) != RESPONSE_FIELDS:
        return False
    if candidate['outcome'] in ('error', 'rejected'):
        # Negative results are retained as negative, even if they carry findings.
        shaped = dict(candidate, outcome='findings' if candidate['findings'] else 'no_findings')
    else:
        shaped = candidate
    return _candidate_matches(shaped, _alternate_intake(intake, request), response=True)


def _validate_alternate(intake: dict[str, Any], state: dict[str, Any]) -> None:
    row = intake['alternate']
    _require(isinstance(row, dict) and set(row) - {'source_intake_key'} == ALTERNATE_FIELDS, 'alternate_fields')
    _require(row['status'] == 'integration_pending', 'alternate_authority_forbidden')
    _require(_quota_matches(row['failure_candidate'], intake), 'quota_candidate')
    _require(row['request_status'] in ('planned', 'unknown', 'observed'), 'alternate_request_status')
    request = row['request_candidate']
    _require(_alternate_request_matches(request, intake, linked='source_intake_key' in row) if row['request_status'] == 'observed'
             else request is None, 'alternate_request_candidate')
    if 'source_intake_key' in row:
        source = _linked_intake(intake, state, row['source_intake_key'])
        _require(request is not None and request == source['request_candidate'], 'alternate_source_request')
        _require(row['response_candidate'] is None or row['response_candidate'] == source['first_response_candidate'],
                 'alternate_source_response')
    response = row['response_candidate']
    _require(response is None or _alternate_response_matches(response, intake, request), 'alternate_response_candidate')



def _same_alternate_scope(intake: dict[str, Any], source: dict[str, Any]) -> bool:
    return (isinstance(source['bot'], str) and source['bot'].casefold() in ('chatgpt', 'codex')
            and all(source[k] == intake[k] for k in ('repository', 'pr', 'policy_version')))


def _linked_intake(intake: dict[str, Any], state: dict[str, Any], key: Any) -> dict[str, Any]:
    _require(isinstance(key, str), 'invalid_alternate_source')
    source = state['intakes'].get(key)
    _require(source is not None and _same_alternate_scope(intake, source), 'invalid_alternate_source')
    return source


def _record_alternate(intake: dict[str, Any], state: dict[str, Any], event: dict[str, Any]) -> None:
    kind = event['kind']
    if kind == 'quota_observed':
        candidate = event['candidate']
        _require(_quota_matches(candidate, intake), 'invalid_quota_candidate')
        if 'alternate' in intake:
            _require(intake['alternate']['failure_candidate'] == candidate, 'quota_candidate_conflict')
        else:
            intake['alternate'] = dict(failure_candidate=copy.deepcopy(candidate), request_status='planned',
                                       request_candidate=None, response_candidate=None, status='integration_pending')
        return
    row = intake.get('alternate')
    _require(row is not None, 'quota_candidate_required')
    if kind == 'alternate_existing_intake_linked':
        source = _linked_intake(intake, state, event['source_intake_key'])
        request = source['request_candidate']
        _require(request is not None and source['first_response_candidate'] is not None, 'alternate_source_incomplete')
        if row['request_candidate'] is not None:
            _require(row.get('source_intake_key') == event['source_intake_key'], 'alternate_candidate_conflict')
            return
        _require(request['snapshot'] == state['snapshot'], 'alternate_snapshot_stale')
        row.update(source_intake_key=event['source_intake_key'], request_status='observed',
                   request_candidate=copy.deepcopy(request),
                   response_candidate=copy.deepcopy(source['first_response_candidate']))
        return
    if kind == 'alternate_delivery_unknown':
        if row['request_status'] == 'planned':
            row['request_status'] = 'unknown'
        return
    _require('source_intake_key' not in row, 'alternate_existing_intake_linked')
    candidate = event['candidate']
    request_event = kind == 'alternate_request_observed'
    _require(_alternate_request_matches(candidate, intake) if request_event else
             _alternate_response_matches(candidate, intake, row['request_candidate']), 'invalid_alternate_candidate')
    if request_event:
        _require(not any(_same_alternate_scope(intake, source) for source in state['intakes'].values()),
                 'existing_alternate_intake_requires_reconciliation')
    slot = 'request_candidate' if request_event else 'response_candidate'
    existing = row[slot]
    if existing is not None:
        _require(existing == candidate if request_event else
                 _response_identity(existing) == _response_identity(candidate), 'alternate_candidate_conflict')
        return  # Idempotent historical observation is not renewed current proof.
    _require(candidate['snapshot'] == state['snapshot'], 'alternate_snapshot_stale')
    row[slot] = copy.deepcopy(candidate)
    if request_event:
        row['request_status'] = 'observed'


def validate_intake_policy_plan(plan: Any) -> list[str]:
    """Validate a prepared example, not an effective policy or settings receipt."""
    fields = {'version', 'status', 'repositories', 'producer_contract', 'settings_readback',
              'prerequisites', 'bots', 'preserved_gates'}
    try:
        _require(isinstance(plan, dict) and set(plan) == fields, 'policy_plan_fields')
        _require(plan['version'] == 'review-phase-v1' and plan['status'] == 'inactive'
                 and plan['producer_contract'] == 'integration_pending' and plan['settings_readback'] == 'pending',
                 'inactive_plan_required')
        _require(isinstance(plan['repositories'], list)
                 and sorted(plan['repositories']) == ['Saber5656/Saihai', 'Saber5656/dotfiles', 'Saber5656/skills'], 'repositories')
        _require(plan['prerequisites'] == ['dotfiles#11', 'skills#41', 'Saihai#128', 'Saihai#141'], 'prerequisites')
        _require(plan['preserved_gates'] == 'current_effective_policy_until_verified_transition', 'preserved_gates')
        bots = plan['bots']
        _require(isinstance(bots, list) and 1 <= len(bots) <= 16, 'bots')
        for bot in bots:
            _require(isinstance(bot, dict) and set(bot) == {'bot', 'trigger_mode', 'automatic_on_open', 'automatic_on_push'}, 'bot_plan')
            _require(_text(bot['bot']) and bot['trigger_mode'] in TRIGGER_MODES
                     and type(bot['automatic_on_open']) is bool and bot['automatic_on_push'] is False, 'bot_plan_controls')
            _require(bot['automatic_on_open'] == (bot['trigger_mode'] == 'automatic_initial'), 'duplicate_initial_trigger')
        _require(len({bot['bot'] for bot in bots}) == len(bots), 'duplicate_bot_plan')
    except (ReviewLifecycleError, TypeError, KeyError, ValueError) as exc:
        return [f'intake_policy_plan_invalid:{exc}']
    return []
