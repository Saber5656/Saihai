"""Host-owned lifecycle evidence, never model attestations or permission grants."""
from __future__ import annotations

import copy
from pathlib import Path

import requirement_scope as scope
import run_store
import vault_task_records as vault


class LedgerError(ValueError):
    def __init__(self, reason: str):
        self.reason_class = reason
        super().__init__(reason)


def checkpoint(artifact: dict, reference: dict) -> dict:
    ledger = artifact['requirement_ledger']
    dispositions = artifact['brief']['requirement_dispositions']
    by_id = {row['requirement_id']: row['status'] for row in dispositions}
    requirements = ledger['requirements']
    if (len(by_id) != len(dispositions) or len(requirements) != len(by_id)
            or {row['requirement_id'] for row in requirements} != set(by_id)):
        raise LedgerError('ledger_requirement_coverage_mismatch')
    return {'version': 1, 'kind': 'requirement_ledger', 'task_id': artifact['task_id'],
            'reference': copy.deepcopy(reference), 'ledger_digest': scope.digest(ledger),
            'ledger': copy.deepcopy(ledger),
            'requirements': [{'requirement_id': row['requirement_id'],
                              'original_content_digest': scope.digest(row),
                              'disposition': by_id[row['requirement_id']]} for row in requirements],
            'coverage_claim': 'exact_input_ids_and_content_digests_only'}


def acknowledge(artifact: dict, reference: dict, *, persist: bool = False) -> dict:
    # Resolve the canonical task on every call. A private cached ACK is not proof.
    binding = vault.bind_task(artifact['task_id'])
    return vault.ledger_checkpoint(binding, checkpoint(artifact, reference), persist=persist)


def unit_identity(ledger: dict, unit: dict) -> str:
    """Exact unit intent, including hunk grants, independent of global selection."""
    material = {k: copy.deepcopy(v) for k, v in unit.items() if k != 'binding'}
    material['requirements'] = sorted((row for row in ledger['requirements']
                                      if row['requirement_id'] in unit['requirement_ids']),
                                     key=lambda row: row['requirement_id'])
    material['constraints'] = ledger.get('constraints', [])
    return scope.digest(material)


def _completion_path(state_root: Path, identity: str) -> Path:
    return Path(state_root) / 'unit-completions' / (identity[7:] + '.json')


def require_prerequisites(state_root: Path, artifact: dict) -> list[dict]:
    ledger = artifact['requirement_ledger']
    selected = scope.selected_unit(ledger)
    units = {unit['unit_id']: unit for unit in ledger['task_units']}
    todo = list(selected['depends_on'])
    verified = {}
    while todo:
        identifier = todo.pop()
        if identifier in verified:
            continue
        unit = units[identifier]
        identity = unit_identity(ledger, unit)
        path = _completion_path(state_root, identity)
        if not run_store.private_artifact_exists(path):
            raise LedgerError('prerequisite_completion_missing:' + identifier)
        receipt = run_store.read_json(path)
        binding = unit['binding']
        if (binding.get('status') != 'existing' or binding.get('task_id') != receipt.get('task_id')
                or receipt.get('unit_identity') != identity or receipt.get('unit_id') != identifier
                or receipt.get('kind') != 'integrated_unit_completion'
                or receipt.get('result') != 'complete' or not receipt.get('merge_commit')
                or not receipt.get('integrated_checks')
                or any(v != 'success' for v in receipt['integrated_checks'].values())):
            raise LedgerError('prerequisite_completion_mismatch:' + identifier)
        vault.ledger_checkpoint(vault.bind_task(receipt['task_id']), receipt)
        verified[identifier] = receipt
        todo.extend(unit['depends_on'])
    return list(verified.values())


def record_completion(state_root: Path, artifact: dict, reference: dict, report: dict, result: dict) -> dict:
    """Called only after trusted publication checked merge CI and persisted its result."""
    import re
    selected = scope.selected_unit(artifact['requirement_ledger'])
    if (result.get('status') != 'complete' or result.get('vault_persistence', {}).get('status') != 'persisted'
            or not re.fullmatch(r'[a-f0-9]{40}', result.get('merge_commit', ''))
            or not result.get('integrated_checks')
            or any(value != 'success' for value in result['integrated_checks'].values())
            or report.get('intake_digest') != reference['digest']
            or report.get('task_id') != artifact['task_id']
            or report.get('validation', {}).get('status') != 'passed'):
        raise LedgerError('unit_completion_evidence_incomplete')
    acknowledge(artifact, reference)
    if findings_status(state_root, artifact)['blocked']:
        raise LedgerError('unit_completion_findings_pending')
    prerequisites = require_prerequisites(state_root, artifact)
    vault.checked_attachments([{'path': report['validation']['evidence_path'],
                                'digest': report['validation']['evidence_digest']}])
    validation = run_store.read_json(Path(report['validation']['evidence_path']))
    if (validation.get('status') != 'passed' or not validation.get('commands')
            or any(row.get('exit') != 0 for row in validation['commands'])
            or any(validation.get(k) != report.get(k) for k in ('tree', 'diff_digest', 'execution_id'))
            or report.get('requirement_scope', {}).get('ledger_digest') != scope.digest(artifact['requirement_ledger'])):
        raise LedgerError('unit_completion_validation_mismatch')
    # Mechanical continuations satisfy the original grant. An arbitrary later
    # same-path/range expansion must have a distinct prerequisite identity.
    origin, origin_ref = artifact, reference
    if artifact.get('mechanical_refresh'):
        import request_intake
        for _ in range(5):
            refresh = origin.get('mechanical_refresh')
            if not refresh:
                break
            origin_ref = refresh['parent_reference']
            origin = request_intake.resolve(state_root, origin_ref)
        if origin.get('mechanical_refresh'):
            raise LedgerError('unit_completion_refresh_chain_limit')
        request_intake.verify_refresh_chain(state_root, reference, origin_ref, root=Path(report['worktree']))
    identity = unit_identity(origin['requirement_ledger'], scope.selected_unit(origin['requirement_ledger']))
    receipt = {'version': 1, 'kind': 'integrated_unit_completion', 'task_id': artifact['task_id'],
               'unit_id': selected['unit_id'], 'unit_identity': identity, 'reference': reference,
               'result': 'complete', 'merge_commit': result['merge_commit'],
               'integrated_checks': result['integrated_checks'], 'report_digest': scope.digest(report),
               'prerequisites': [scope.digest(row) for row in prerequisites]}
    path = _completion_path(state_root, identity)
    if run_store.private_artifact_exists(path):
        previous = run_store.read_json(path)
        if previous != receipt:
            raise LedgerError('unit_completion_conflict')
    vault.ledger_checkpoint(vault.bind_task(artifact['task_id']), receipt, persist=True)
    run_store.atomic_write_json(path, receipt)
    return receipt


STAGES = {'intake', 'plan', 'implementation', 'validation', 'review', 'publication', 'merge', 'completion'}


def record_stage(state_root: Path, artifact: dict, reference: dict, *, stage: str,
                 observation: dict, findings: list[dict] | None = None) -> dict:
    """Disposition every observed item; never treat provider findings as scope grants.

    Immutable events avoid lost updates between host stages. The original item is
    retained even when incomplete. Stable content identities deduplicate replay.
    Unknown requirements and claimed critical risks remain typed internal blockers;
    only a host-ledger requirement choice may become a user question.
    """
    if stage not in STAGES or not isinstance(observation, dict):
        raise LedgerError('ledger_stage_invalid')
    findings = [] if findings is None else findings
    if not isinstance(findings, list) or len(findings) > 256 or any(not isinstance(f, dict) for f in findings):
        raise LedgerError('ledger_findings_invalid')
    ledger = artifact['requirement_ledger']
    selected = scope.selected_unit(ledger)
    requirements = {r['requirement_id']: r for r in ledger['requirements']}
    rows = []
    for finding in findings:
        original = copy.deepcopy(finding)
        ids = finding.get('requirement_ids', [])
        typed = isinstance(ids, list) and all(isinstance(i, str) for i in ids)
        known = typed and set(ids) <= set(requirements)
        kind = finding.get('kind', 'ordinary')
        choice = known and any(requirements[i].get('requires_decision') in
                    {'product_requirement', 'material_scope', 'security_authority'} for i in ids)
        if not known or not isinstance(kind, str) or kind not in {'ordinary', 'requirement_choice', 'permission', 'authentication', 'data_loss'}:
            disposition = 'internal_triage_required'
        elif kind in {'permission', 'authentication', 'data_loss'}:
            disposition = 'affected_unit_blocked'
        elif choice:
            disposition = 'material_requirement_choice'
        elif ids and set(ids) <= set(selected['requirement_ids']):
            disposition = 'in_scope_followup'
        else:
            disposition = 'deferred'
        rows.append({'finding_id': scope.digest(original), 'original': original,
                     'original_content_digest': scope.digest(original), 'disposition': disposition,
                     'authority': 'observation_only'})
    event = {'version': 1, 'kind': 'ledger_stage', 'task_id': artifact['task_id'],
             'reference': reference, 'stage': stage, 'observation_digest': scope.digest(observation),
             'findings': rows}
    key = scope.digest(event)[7:]
    path = Path(state_root) / 'ledger-stages' / artifact['task_id'] / (key + '.json')
    if run_store.private_artifact_exists(path) and run_store.read_json(path) != event:
        raise LedgerError('ledger_stage_conflict')
    vault.ledger_checkpoint(vault.bind_task(artifact['task_id']), event, persist=True)
    run_store.atomic_write_json(path, event)
    return event


def findings_status(state_root: Path, artifact: dict) -> dict:
    directory = Path(state_root) / 'ledger-stages' / artifact['task_id']
    rows = {}
    events = []
    for path in sorted(directory.glob('*.json')):
        event = run_store.read_json(path)
        if (path.stem != scope.digest(event)[7:] or event.get('task_id') != artifact['task_id']
                or event.get('kind') != 'ledger_stage' or event.get('stage') not in STAGES):
            raise LedgerError('ledger_stage_identity_mismatch')
        vault.ledger_checkpoint(vault.bind_task(artifact['task_id']), event)
        events.append(scope.digest(event))
        for row in event['findings']:
            entry = rows.setdefault(row['finding_id'], dict(row, stages=[], observed_events=[]))
            entry['observed_events'].append(scope.digest(event))
            if event['stage'] not in entry['stages']:
                entry['stages'].append(event['stage'])
            # Never let a later ordinary/deferred observation erase a prior blocker.
            priority = ['deferred', 'in_scope_followup', 'material_requirement_choice',
                        'internal_triage_required', 'affected_unit_blocked']
            if priority.index(row['disposition']) > priority.index(entry['disposition']):
                entry['disposition'] = row['disposition']
    for path in sorted((Path(state_root) / 'ledger-resolutions' / artifact['task_id']).glob('*.json')):
        resolution = run_store.read_json(path)
        if (path.stem != scope.digest(resolution)[7:] or resolution.get('kind') != 'finding_resolution'
                or resolution.get('task_id') != artifact['task_id']):
            raise LedgerError('finding_resolution_identity_mismatch')
        vault.ledger_checkpoint(vault.bind_task(artifact['task_id']), resolution)
        for identifier in resolution['finding_ids']:
            if identifier in rows and set(rows[identifier]['observed_events']) <= set(resolution['observed_events']):
                rows[identifier]['disposition'] = resolution['disposition']
    return {'findings': list(rows.values()), 'event_digests': sorted(events), 'blocked': any(row['disposition'] in
            {'in_scope_followup', 'material_requirement_choice', 'internal_triage_required', 'affected_unit_blocked'}
            for row in rows.values())}


def resolve_findings(state_root: Path, artifact: dict, reference: dict, *, finding_ids: list[str],
                     evidence: dict, disposition: str = 'resolved') -> dict:
    """Host repair closeout with immutable, read-back evidence; no new scope grant.

    A provider suggestion cannot call this through the worker result schema. The
    host supplies its validation receipt (and a scoped review for critical risks).
    Deferral is permitted only for observations already outside selected scope.
    """
    if disposition not in {'resolved', 'deferred'} or not finding_ids or len(set(finding_ids)) != len(finding_ids):
        raise LedgerError('finding_resolution_invalid')
    current = findings_status(state_root, artifact)
    by_id = {row['finding_id']: row for row in current['findings']}
    if not set(finding_ids) <= set(by_id):
        raise LedgerError('finding_resolution_unknown')
    vault.checked_attachments([{'path': evidence['path'], 'digest': evidence['digest']}])
    receipt = run_store.read_json(Path(evidence['path']))
    report_path = Path(evidence.get('report_path', str(Path(evidence['path']).parent / 'report.json')))
    report = run_store.read_json(report_path)
    if (report.get('task_id') != artifact['task_id'] or report.get('intake_digest') != reference['digest']
            or report.get('validation', {}).get('evidence_path') != evidence['path']
            or report.get('validation', {}).get('evidence_digest') != evidence['digest']
            or any(report.get(k) != receipt.get(k) for k in ('tree', 'diff_digest', 'execution_id'))):
        raise LedgerError('finding_resolution_task_evidence_mismatch')
    evidence = dict(evidence, report_path=str(report_path), report_digest=vault.digest(run_store.read_bytes(report_path)))
    if disposition == 'resolved' and (receipt.get('status') not in {'passed', 'validated'}
            or not receipt.get('commands') or any(row.get('exit') != 0 for row in receipt['commands'])):
        raise LedgerError('finding_resolution_validation_required')
    for identifier in finding_ids:
        row = by_id[identifier]
        if disposition == 'deferred' and row['disposition'] != 'deferred':
            raise LedgerError('finding_resolution_cannot_defer_blocker')
        if row['disposition'] == 'affected_unit_blocked' and receipt.get('scope_review', {}).get('decision') != 'approved':
            raise LedgerError('finding_resolution_scoped_review_required')
        if row['disposition'] in {'internal_triage_required', 'material_requirement_choice'}:
            raise LedgerError('finding_resolution_requires_host_ledger_revision')
    event = {'version': 1, 'kind': 'finding_resolution', 'task_id': artifact['task_id'],
             'reference': reference, 'finding_ids': sorted(finding_ids), 'disposition': disposition,
             'evidence': evidence, 'observed_events': current['event_digests']}
    vault.ledger_checkpoint(vault.bind_task(artifact['task_id']), event, persist=True)
    key = scope.digest(event)[7:]
    run_store.atomic_write_json(Path(state_root) / 'ledger-resolutions' / artifact['task_id'] / (key + '.json'), event)
    return event


def resolve_requirement_findings(state_root: Path, before: dict, before_ref: dict,
                                 after: dict, after_ref: dict, *, finding_ids: list[str]) -> dict:
    """Close only choices/unknown IDs explicitly accounted for by a host revision."""
    acknowledge(before, before_ref); acknowledge(after, after_ref)
    if before['task_id'] != after['task_id'] or not finding_ids or len(set(finding_ids)) != len(finding_ids):
        raise LedgerError('finding_revision_identity_mismatch')
    old, new = before['requirement_ledger'], after['requirement_ledger']
    if not new['requirements_history']:
        raise LedgerError('finding_revision_history_missing')
    history = new['requirements_history'][-1]
    if history.get('previous_digest') != scope.digest(old):
        raise LedgerError('finding_revision_parent_mismatch')
    expected = scope.revise(old, expected_digest=scope.digest(old), version=new['requirements_version'],
        replacements=history['replacements'], additions=history['additions'], units=new['task_units'],
        selected_unit_id=new['selected_unit_id'], resolved_decisions=tuple(history['resolved_decisions']))
    if new != expected:
        raise LedgerError('finding_revision_unaccounted_change')
    status = findings_status(state_root, before)
    rows = {row['finding_id']: row for row in status['findings']}
    handled = set(history['resolved_decisions']) | {row['requirement_id'] for row in history['additions']}
    current_requirements = {row['requirement_id']: row for row in new['requirements']}
    for identifier in finding_ids:
        row = rows.get(identifier, {})
        original = row.get('original', {})
        ids = original.get('requirement_ids', [])
        if (row.get('disposition') not in {'material_requirement_choice', 'internal_triage_required'}
                or not isinstance(ids, list) or not ids or not set(ids) <= current_requirements.keys()
                or not set(ids) & handled or any(current_requirements[i].get('requires_decision') for i in ids)):
            raise LedgerError('finding_revision_does_not_resolve_observation')
    event = {'version': 1, 'kind': 'finding_resolution', 'task_id': before['task_id'],
             'reference': after_ref, 'finding_ids': sorted(finding_ids), 'disposition': 'resolved',
             'evidence': {'kind':'canonical_requirement_revision', 'before':before_ref, 'after':after_ref},
             'observed_events': status['event_digests']}
    vault.ledger_checkpoint(vault.bind_task(before['task_id']), event, persist=True)
    run_store.atomic_write_json(Path(state_root) / 'ledger-resolutions' / before['task_id'] /
                               (scope.digest(event)[7:] + '.json'), event)
    return event
