"""Host-only observation of bridge output acknowledgements; no notification execution."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import math


def run_output(store, state_root: Path, record: dict) -> dict | None:
    run_id = record.get('run_id')
    if not run_id:
        return None
    run = store.load_run(state_root, run_id)
    if run.get('request_id') != record.get('request_id') or run.get('task_id') != record.get('task_id'):
        raise store.RunStoreError('output_run_binding_mismatch')
    return {key: run.get(key) for key in ('run_id', 'run_state', 'current_step', 'iteration')}


def status(host, *, state_root: Path, principal: dict, stale_seconds: int = 900, now_epoch: float | None = None) -> dict:
    if principal.get('principal_type') not in {'manual_operator', 'human_operator', 'harness_runner'}:
        raise host.FrontdoorError('output_monitor_requires_host_principal')
    if type(stale_seconds) is not int or not 1 <= stale_seconds <= 86400:
        raise host.FrontdoorError('output_stale_age_invalid')
    now = datetime.now(timezone.utc).timestamp() if now_epoch is None else now_epoch
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise host.FrontdoorError('output_clock_invalid')
    rows = []
    with host.run_lock.hold_global_lock(state_root, operation='output_status', principal=principal):
        requests = host.list_state_files(host.state_paths(state_root)['requests'], suffix='.json')
        if len(requests) > 1000:
            raise host.FrontdoorError('output_inventory_limit')
        for path in requests:
            record = host.read_json(path)
            owner = host.bridge_owner_principal(record)
            if owner.get('principal_type') != 'main_agent_bridge' or not record.get('run_id'):
                continue
            projection, _ = host.build_bridge_projection(state_root=state_root, request_id=record['request_id'], principal=owner)
            digest = host.bridge_projection_digest(projection)
            key = host.validate_artifact_id(record['request_id'], 'request_id')
            saved = state_root / 'output-observations' / (key + '.json')
            previous = host.read_json(saved) if host.state_file_exists(saved) else {}
            if previous and previous.get('request_id') != key:
                raise host.FrontdoorError('output_observation_binding_mismatch')
            same = previous.get('projection_digest') == digest
            first = previous.get('first_observed_at', now) if same else now
            if not isinstance(first, (int, float)) or not math.isfinite(first) or first > now:
                raise host.FrontdoorError('output_observation_clock_invalid')
            ack_binding = {'request_id': key, 'projection_digest': digest, 'principal': owner}
            ack_path = host.state_paths(state_root)['acks'] / (f'{key}-{host.stable_digest(ack_binding)[:24]}.json')
            ack = host.read_json(ack_path) if host.state_file_exists(ack_path) else {}
            acknowledged = (ack.get('ack_verified') is True and ack.get('request_id') == key
                            and ack.get('projection_digest') == digest and ack.get('principal') == owner)
            age = max(0, now - first)
            stale = not acknowledged and age >= stale_seconds
            current = dict(request_id=key, run_id=record['run_id'], projection_digest=digest,
                           first_observed_at=first, stale_output=stale, acknowledged=acknowledged)
            if previous.get('stale_output') and (not stale or not same):
                host.append_audit_event(state_root=state_root, event_type='stale_output_cleared', principal=principal,
                    subject={'request_id':key,'run_id':record['run_id']}, outcome='ok',
                    details={'projection_digest':previous.get('projection_digest'),'reason':'acknowledged' if acknowledged and same else 'output_replaced'})
            if stale and (not previous.get('stale_output') or not same):
                host.append_audit_event(state_root=state_root, event_type='stale_output_raised', principal=principal,
                    subject={'request_id':key,'run_id':record['run_id']}, outcome='stale_output',
                    details={'projection_digest':digest,'age_seconds':age,'threshold_seconds':stale_seconds})
            if current != previous:
                host.write_json(saved, current)
            rows.append(dict(current, age_seconds=age))
    return {'schema_version':1,'decision':'ok','outputs':rows,'stale_outputs':[r for r in rows if r['stale_output']],
            'notification':'disabled_no_hook','transition_effect':'none','age_basis':'host_first_observation_of_current_projection'}
