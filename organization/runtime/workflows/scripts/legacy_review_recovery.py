"""Explicit current-content recovery for an initial, unsealed legacy review."""
from pathlib import Path
import uuid

import run_lock
import run_store
import scoped_worker_executor as worker


def _require(condition, reason):
    if not condition:
        raise worker.ScopedWorkerError(reason)


def _path(root, run, execution):
    run_store.validate_artifact_id(run['run_id'], 'run_id')
    run_store.validate_artifact_id(execution['execution_id'], 'execution_id')
    # Keep lexical components: private IO rejects symlinked parents.
    return root / 'worker-evidence' / run['run_id'] / ('current-review-recovery-' + execution['execution_id'] + '.json')


def _binding(root, run, bundle):
    row, execution, capability, evidence = bundle
    _require(run.get('workflow_id') == 'standard_code_change', 'review_recovery_workflow_invalid')
    for field in ('task_id', 'request_id', 'run_id'):
        _require(run.get(field) == capability.get(field) == execution.get(field), 'review_recovery_identity_mismatch')
    _require(worker.sha256_digest(worker._capability_material(capability)) == capability['capability_digest'],
             'review_recovery_capability_mismatch')
    consumed = capability.get('execution_state', {})
    _require(consumed.get('nonce_state') == 'consumed' and consumed.get('last_execution_id') == execution['execution_id'],
             'review_recovery_execution_mismatch')
    order = worker._read_state_json(root / 'work-orders' / run['run_id'] / 'implement.json', reason='work_order_unavailable')
    _require(worker.sha256_digest(order) == capability['work_order_digest'], 'review_recovery_order_mismatch')
    worker.verify_work_order_signature(root, order)
    activation = run.get('activation', {})
    _require(activation.get('activation_status') == 'approved'
             and activation.get('activation_scope') == order.get('activation_scope')
             and activation['activation_scope'].get('allowed_paths') == capability['allowed_paths'],
             'review_recovery_scope_mismatch')
    instruction = worker._read_state_json(Path(capability['prompt_artifact']['path']), reason='instruction_artifact_invalid')
    _require(worker.sha256_digest(instruction) == capability['prompt_artifact']['digest'], 'review_recovery_instruction_mismatch')
    tree = Path(capability['worktree']['worktree_path'])
    worker.verify_task_worktree_after_execution(capability, tree)
    return {'task_id': run['task_id'], 'request_id': run['request_id'], 'run_id': run['run_id'],
            'execution_id': execution['execution_id'], 'execution_digest': worker.sha256_digest(execution),
            'capability_digest': capability['capability_digest'], 'original_evidence_digest': row['evidence_digest'],
            'activation_digest': worker.sha256_digest(activation), 'worktree': capability['worktree'],
            'repository': capability['repository'], 'allowed_paths': capability['allowed_paths']}, tree


def _load_context(root, run, bundle):
    _, execution, capability, _ = bundle
    path = _path(root, run, execution)
    if not run_store.private_artifact_exists(path):
        raise worker.ScopedWorkerError('review_execution_context_missing')
    binding, tree = _binding(root, run, bundle)
    snapshot = worker._read_state_json(path, reason='review_recovery_snapshot_invalid')
    _require(isinstance(snapshot, dict), 'review_recovery_snapshot_invalid')
    material = {k: v for k, v in snapshot.items() if k != 'snapshot_digest'}
    _require(snapshot.get('kind') == 'current_content_review_recovery'
             and snapshot.get('binding') == binding
             and snapshot.get('snapshot_digest') == worker.sha256_digest(material), 'review_recovery_snapshot_mismatch')
    changed = worker._changed_paths(tree)
    _require(snapshot.get('changed_paths') == changed
             and snapshot.get('context') == worker.capture_review_context(capability, tree, changed), 'review_recovery_content_drift')
    return snapshot['context']


def load_context(root, run, bundle):
    try:
        return _load_context(root, run, bundle)
    except (run_store.RunStoreError, KeyError, TypeError, ValueError, OSError) as exc:
        raise worker.ScopedWorkerError('review_recovery_state_invalid') from exc


def recover(*, state_root, run_id, principal):
    import frontdoor_orchestrator as frontdoor
    frontdoor.precheck_execution_principal(state_root=state_root, principal=principal,
        transition='recover_legacy_review_context', subject={'run_id': run_id})
    run_store.validate_artifact_id(run_id, 'run_id')
    with run_lock.hold_global_lock(state_root, operation='recover_legacy_review_context', run_id=run_id, principal=principal):
        run = run_store.load_run(state_root, run_id)
        bundle = worker.completed_review_evidence(state_root, run)
        _, execution, capability, evidence = bundle
        _require('review_context' not in evidence, 'review_recovery_not_legacy')
        path = _path(state_root, run, execution)
        if run_store.private_artifact_exists(path):
            load_context(state_root, run, bundle)
            snapshot = worker._read_state_json(path, reason='review_recovery_snapshot_invalid')
            return {'decision': 'ok', 'status': 'existing', 'recovery_id': snapshot['recovery_id'], 'snapshot_digest': snapshot['snapshot_digest']}
        completed = [r for r in run.get('step_history', []) if r.get('status') == 'completed']
        _require(run.get('current_step') == 'review' and run.get('run_state') == 'step_queued'
                 and run.get('iteration') == 2 and len(completed) == 1 and completed[0].get('step_id') == 'implement'
                 and not run.get('provider_execution') and not run.get('review_lifecycle'), 'review_recovery_not_initial')
        _require(not run_store.list_private_artifacts(state_root / 'work-orders' / run_id, prefix='review', suffix='.json')
                 and not run_store.list_private_artifacts(state_root / 'reports' / run_id, prefix='review', suffix='.json'),
                 'review_recovery_already_sealed')
        binding, tree = _binding(state_root, run, bundle)
        budget = run['activation']['activation_scope'].get('step_budget')
        _require(type(budget) is int and budget >= run['iteration'], 'review_recovery_budget_exhausted')
        changed = worker._changed_paths(tree)
        context = worker.capture_review_context(capability, tree, changed)
        snapshot = {'version': 1, 'kind': 'current_content_review_recovery', 'recovery_id': 'recovery-' + uuid.uuid4().hex,
                    'created_at': worker.now_iso(), 'binding': binding, 'changed_paths': changed, 'context': context}
        snapshot['snapshot_digest'] = worker.sha256_digest(snapshot)
        _require(run_store.create_private_file(path, worker.canonical_json(snapshot)), 'review_recovery_snapshot_exists')
        # Read-back includes current-content and all identity checks. No old state is rewritten.
        load_context(state_root, run, bundle)
        return {'decision': 'ok', 'status': 'created', 'recovery_id': snapshot['recovery_id'], 'snapshot_digest': snapshot['snapshot_digest']}
