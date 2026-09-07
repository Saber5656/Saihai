"""Bounded host scheduling for the existing trusted-local execution APIs."""
from __future__ import annotations

import dataclasses
import math
from pathlib import Path
import time
import uuid

import run_lock
import run_store
import trusted_local_executor as local


WAITING = {'ci_pending', 'integrated_ci_pending', 'mergeability_pending',
           'completion_persistence_pending'}
CONTINUE = {'validated', 'integration_validated', 'retryable_worker', 'retryable_validation'}
HUMAN = {'requires_user_decision', 'waiting_human', 'published_human_gate'}
UNCERTAIN = {'commit_uncertain', 'push_uncertain', 'pr_uncertain', 'merge_uncertain',
             'integration_reconciliation_required', 'mutation_uncertain'}


def drive(*, authorization: local.TrustedLocalAuthorization, state_root: Path,
          request: dict | None = None, max_iterations: int = 32,
          duration_seconds: float = 300, poll_interval_seconds: float = 5,
          commands: local.publication.Commands | None = None) -> dict:
    """Run once, then advance saved state; never replay an existing worker claim.

    The host supplies authority independently. Ceilings admit operations; an
    already admitted worker/validation retains its existing authority timeout.
    Polls use no repair budget. Existing repair APIs own same-cause accounting.
    """
    if not isinstance(authorization, local.TrustedLocalAuthorization):
        raise local.TrustedLocalError('independent_host_authorization_required')
    if (type(max_iterations) is not int or not 1 <= max_iterations <= 256
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in (duration_seconds, poll_interval_seconds))
            or not 0 < duration_seconds <= 3600 or not 0 <= poll_interval_seconds <= 60):
        raise local.TrustedLocalError('drive_bounds_invalid')
    host = authorization.publication
    run_store.validate_artifact_id(host.execution_id, 'execution_id')
    root = Path(state_root)
    worktree = Path(host.worktree).resolve()
    if (not root.is_absolute() or root.resolve() != root or root == worktree
            or worktree in root.parents):
        raise local.TrustedLocalError('drive_state_root_invalid')
    directory = root / 'trusted-local' / host.execution_id
    binding = local.publication.digest(dataclasses.asdict(authorization))
    invocation = 'drive-' + uuid.uuid4().hex
    actor = {'principal_type': 'harness_runner', 'principal_id': local.ACTOR, 'authn_method': 'local_cli'}
    started = time.monotonic()
    iterations = polls = repairs = 0
    result: dict = {}

    def bound_claim() -> bool:
        return (run_store.private_artifact_exists(directory / 'claim.json')
                and run_store.read_json(directory / 'claim.json').get('authorization_digest') == binding)

    def record(action: str, status: str) -> None:
        if bound_claim():
            row = {'invocation_id': invocation, 'authorization_digest': binding,
                   'iteration': iterations, 'action': action, 'status': status,
                   'polls': polls, 'repairs': repairs}
            run_store.append_json_line(directory / 'drive-events.jsonl', row)

    def finish(stop: str, reason: str, resumable: bool = False) -> dict:
        payload = {'schema_version': 1, 'profile': local.PROFILE,
                   'decision': 'blocked' if stop in {'blocked', 'waiting_human', 'uncertain'} else 'ok',
                   'status': 'complete' if stop == 'terminal' else reason, 'stop': stop,
                   'reason_class': reason, 'resumable': resumable,
                   'execution_id': host.execution_id, 'iterations': iterations,
                   'polls': polls, 'repairs': repairs, 'invocation_id': invocation,
                   'last_status': result.get('status')}
        # Keep host continuation receipts separate from completion. Never turn a
        # pending Vault write into complete, and never perform that write here.
        for key in ('pr', 'head', 'merge_commit', 'integrated_checks', 'completion_persistence', 'continuation'):
            if key in result:
                payload[key] = result[key]
        record('stop', reason)
        if bound_claim():
            run_store.atomic_write_json(directory / 'drive.json', dict(payload, authorization_digest=binding))
        return payload

    try:
        while iterations < max_iterations:
            if time.monotonic() - started >= duration_seconds:
                return finish('bounded', 'duration_exhausted', True)
            # This is the existing state-root global lock. Trusted-local worker
            # and publication APIs do not acquire it recursively. Release it
            # before CI sleeps; another invocation must re-read before acting.
            with run_lock.hold_global_lock(root, operation='usage_drive', run_id=host.run_id,
                                           principal=actor):
                if time.monotonic() - started >= duration_seconds:
                    return finish('bounded', 'duration_exhausted', True)
                claimed = run_store.private_artifact_exists(directory / 'claim.json')
                if not claimed:
                    if request is None:
                        return finish('blocked', 'request_required_for_initial_execution')
                    action = 'run'
                else:
                    claim = run_store.read_json(directory / 'claim.json')
                    if claim.get('authorization_digest') != binding:
                        raise local.TrustedLocalError('drive_authority_changed')
                    if request is not None and claim.get('request_digest') != local.publication.digest(request):
                        raise local.TrustedLocalError('drive_request_changed')
                    saved = local.usage_status(host.execution_id, root)
                    integration = saved['integration']['status']
                    if integration in HUMAN:
                        return finish('waiting_human', integration)
                    if integration in UNCERTAIN:
                        return finish('uncertain', integration)
                    if saved['validation']['status'] == 'failed':
                        action = 'repair_validation'
                    elif (saved['validation']['status'] == 'passed'
                          or integration in {'running', 'retryable_worker', 'retryable_validation'}):
                        action = 'advance'
                    else:
                        # Saved running is not a liveness proof. No fresh execute
                        # and no repair without a real failed validation receipt.
                        return finish('blocked', 'execution_incomplete_inspection_required')
                iterations += 1
                record(action, 'started')
                try:
                    if action == 'run':
                        result = local.execute(request, authorization, root)
                    elif action == 'repair_validation':
                        repairs += 1
                        result = local.repair_validation(authorization, root)
                    else:
                        result = local.advance_publication(authorization, root, commands=commands)
                except local.TrustedLocalError as exc:
                    if str(exc) != 'host_validation_failed':
                        raise
                    # The failed receipt, not the exception alone, determines
                    # whether the next iteration may reserve a repair.
                    result = {'status': 'validation_failed'}
                status = result.get('status', 'unknown')
                record(action, status)
                persistence = result.get('completion_persistence')
                if isinstance(persistence, dict) and persistence.get('status') not in {'complete', 'persisted', 'not_required'}:
                    if persistence.get('status') in {'pending', 'running'}:
                        return finish('waiting', 'completion_persistence_pending', True)
                    return finish('blocked', 'completion_persistence_' + str(persistence.get('status', 'unknown')))
                if status == 'complete':
                    return finish('terminal', 'complete')
                if status in HUMAN:
                    return finish('waiting_human', status)
                if status in UNCERTAIN:
                    return finish('uncertain', status)
                if result.get('decision') == 'blocked':
                    return finish('blocked', status)
                if status not in WAITING | CONTINUE | {'validation_failed'}:
                    return finish('blocked', 'unsupported_status')
            if status in WAITING:
                polls += 1
                if iterations < max_iterations:
                    remaining = duration_seconds - (time.monotonic() - started)
                    if remaining > 0:
                        time.sleep(min(poll_interval_seconds, remaining))
        return finish('bounded', 'iteration_exhausted', True)
    except run_lock.LockContentionError:
        return finish('blocked', 'lock_contention', True)
