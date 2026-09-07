"""Trusted-host application delivery contracts, separate from PR publication.

Only host-supplied adapters may observe grants, build, deploy or recover. No
worker JSON is accepted as authority, and this module provisions no credentials.
Persistent intents prevent uncertain actions being replayed after a restart.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import dataclasses
import math
import os
import re
from pathlib import Path
import subprocess
import time
from typing import Callable

import delivery_contract
import host_validation as validation
import run_store
from validation_evidence import read_bytes, parse_json


class DeliveryBlocked(ValueError):
    """A typed non-success; callers must not infer deployment or release success."""


class ObservedFailure(DeliveryBlocked):
    """A completed bound measuring command reported a failure."""


@dataclass(frozen=True)
class ReleaseGrant:
    # Constructed by the existing trusted host authority adapter, never a report.
    grant_id: str
    repository: str
    environment: str
    target: str
    artifact_digest: str
    actions: tuple[str, ...]
    owner: str
    issued_at: float
    expires_at: float
    revoked: bool
    protected_environment: bool
    identity_kind: str
    privileges: tuple[str, ...]
    evidence_ref: str


@dataclass(frozen=True)
class ReleasePolicy:
    # Trusted host pins a repository-owned policy to the current source identity.
    source_digest: str
    environments: tuple[str, ...]
    target: str
    signature_required: bool
    signature_reason: str
    migration_paths: tuple[str, ...]
    migration_required: bool
    migration_reason: str
    recovery_owner: str
    health_checks: tuple[str, ...] = ('smoke', 'health')
    timeout_seconds: int = 120
    sbom_required: bool = True
    sbom_reason: str = 'Dependency inventory required.'


def load_release_policy(root, reference):
    """Read the exact repository policy bytes pinned by the trusted host."""
    from validation_evidence import read_bound
    raw = read_bound(root, reference)
    value = parse_json(raw)
    fields = {f.name for f in dataclasses.fields(ReleasePolicy)} - {'source_digest'}
    if type(value) is not dict or set(value) != fields:
        raise DeliveryBlocked('repository_release_policy_shape')
    for field in ('environments','migration_paths','health_checks'):
        if type(value[field]) is not list:
            raise DeliveryBlocked('repository_release_policy_array_required')
        value[field] = tuple(value[field])
    return ReleasePolicy(source_digest=validation.source_digest(root), **value)


def _hash(value):
    return validation.digest(value)


def _key(value):
    return _hash(value).split(':')[1]


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


class DeliveryHost:
    """Host integration API. Keep state and adapter configuration outside workers.

    Commands are host-approved argv, not loaded from the delivery profile. Each
    observation must return JSON with the exact supplied binding and result/pass
    counts. The host adapter is responsible for measuring the target, signature,
    provenance or migration in question, rather than echoing the binding.
    """

    def __init__(self, *, root: Path, state: Path, profile: dict,
                 policy: ReleasePolicy, commands: dict[str, tuple[str, ...]],
                 grants: Callable[[str], ReleaseGrant | None]):
        self.root = Path(root).resolve(strict=True)
        self.state = Path(state)
        if (not self.state.is_absolute() or self.state.resolve() != self.state
                or self.state == self.root or self.state.is_relative_to(self.root)):
            raise DeliveryBlocked('host_state_outside_worker_required')
        run_store.ensure_private_directory(self.state)
        if delivery_contract.validate_profile(profile):
            raise DeliveryBlocked('delivery_profile_invalid')
        if (not isinstance(policy, ReleasePolicy) or policy.source_digest != validation.source_digest(self.root)
                or not policy.environments or len(set(policy.environments)) != len(policy.environments)
                or any(type(e) is not str or not e for e in policy.environments)
                or policy.target not in {t['name'] for t in profile['release']['targets']}
                or type(policy.sbom_required) is not bool or not policy.sbom_reason
                or type(policy.signature_required) is not bool or not policy.signature_reason
                or type(policy.migration_required) is not bool or not policy.migration_reason
                or type(policy.health_checks) is not tuple or any(type(n) is not str or not n for n in policy.health_checks)
                or len(set(policy.health_checks)) != len(policy.health_checks)
                or type(policy.migration_paths) is not tuple or any(type(n) is not str or not n for n in policy.migration_paths)
                or not policy.recovery_owner or not {'smoke', 'health'} <= set(policy.health_checks)
                or type(policy.timeout_seconds) is not int or not 1 <= policy.timeout_seconds <= 600):
            raise DeliveryBlocked('release_policy_invalid_or_stale')
        self.profile = parse_json(json.dumps(profile).encode())
        self.policy, self.commands, self.grants = policy, dict(commands), grants
        self.repository = profile['repository']
        self.profile_digest = _hash(profile)
        self.policy_digest = _hash(dataclasses.asdict(policy))

    def _read(self, name):
        return parse_json(read_bytes(self.state, name))

    def _save(self, name, value):
        run_store.atomic_write_json(self.state/name, value)

    @contextmanager
    def _lock(self, identity):
        path = self.state/('lock-'+_key(identity))
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeliveryBlocked('target_lease_busy') from exc
            yield
        finally:
            os.close(fd)

    def _source(self):
        actual = validation.source_digest(self.root)
        if actual != self.policy.source_digest:
            raise DeliveryBlocked('source_changed')
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root).decode().strip()
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=self.root).strip():
            raise DeliveryBlocked('immutable_committed_source_required')
        return {'repository': self.repository, 'commit': head, 'source_digest': actual,
                'profile_digest': self.profile_digest, 'policy_digest': self.policy_digest}

    def _observe(self, name, binding, *, artifact=None):
        binding = parse_json(json.dumps(binding).encode())
        argv = self.commands.get(name)
        if (type(argv) is not tuple or not argv or any(type(a) is not str for a in argv)
                or validation.command_digest(argv) is None):
            raise DeliveryBlocked('host_observer_missing:'+name)
        # Binding is an input to the trusted measuring adapter, not its output.
        env = dict(os.environ, SAIHAI_DELIVERY_BINDING=json.dumps(binding),
                   SAIHAI_ARTIFACT=str(artifact or ''))
        start = time.time(); done = None
        try:
            done = subprocess.run(argv, cwd=self.root, env=env, capture_output=True,
                                  timeout=self.policy.timeout_seconds, check=False)
            if len(done.stdout) > 2*1024*1024 or len(done.stderr) > 2*1024*1024:
                raise DeliveryBlocked('observation_budget:'+name)
            payload = parse_json(done.stdout)
            observed = validation.observe(argv, done.stdout, done.stderr, done.returncode)
            if payload.get('binding') == binding and payload.get('result') == 'fail':
                raise ObservedFailure('observation_failed:'+name)
            if (payload.get('binding') != binding or not observed['passed']
                    or observed['kind'] != 'tests' or observed['executed'] <= 0):
                raise DeliveryBlocked('observation_failed_or_unbound:'+name)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            failure = exc if isinstance(exc, ObservedFailure) else DeliveryBlocked('observation_failed_or_unknown:'+name)
            failure.evidence = {'name':name, 'binding':binding, 'started_at':start, 'ended_at':time.time(),
                                'command_digest':validation.command_digest(argv),
                                'exit':done.returncode if done is not None else None,
                                'stdout_digest':_hash(done.stdout) if done is not None else None,
                                'stderr_digest':_hash(done.stderr) if done is not None else None,
                                'status':'failed' if isinstance(failure, ObservedFailure) else 'unknown'}
            self._save('observation-'+_key([binding,name,start])+'.json', failure.evidence)
            if failure is exc:
                raise
            raise failure from exc
        fields = {
            'migration_applicability': {'applicable', 'paths', 'reason'},
            'migration_compatibility': {'old_app', 'new_app', 'destructive', 'sequence', 'recovery'},
            'migration_dry_run': {'representative'},
            'migration_restore_test': {'restored', 'backup_digest'},
            'recovery_compatibility': {'compatible', 'non_lossy', 'destructive'},
        }.get(name, set())
        facts = payload.get('facts', {})
        if type(facts) is not dict or set(facts) - fields:
            raise DeliveryBlocked('observation_facts_not_redacted:'+name)
        return {'name': name, 'binding': binding, 'command_digest': validation.command_digest(argv),
                'started_at': start, 'ended_at': time.time(), 'exit': done.returncode,
                'stdout_digest': _hash(done.stdout), 'stderr_digest': _hash(done.stderr),
                'observation': observed, 'facts': facts}

    def build_once(self, *, validation_receipt: Path, validation_identity: dict):
        """Build to a host path once; validate those exact bytes before any promotion."""
        source = self._source()
        path = Path(validation_receipt)
        if not path.is_absolute() or path.resolve() != path or path.is_relative_to(self.root) or path.stat().st_mode & 0o077:
            raise DeliveryBlocked('host_validation_receipt_required')
        raw = read_bytes(path.parent, path.name)
        receipt = parse_json(raw)
        validation.validate_receipt(receipt, identity=validation_identity)
        if receipt['source_digest'] != source['source_digest']:
            raise DeliveryBlocked('validation_source_mismatch')
        locks = []
        for lock in self.profile['dependencies']['lockfiles']:
            content = read_bytes(self.root, lock['path'])
            if hashlib.sha256(content).hexdigest() != lock['sha256']:
                raise DeliveryBlocked('dependency_lock_changed')
            locks.append({'path': lock['path'], 'digest': _hash(content)})
        build_id = _key(source)
        record = 'build-'+build_id+'.json'
        with self._lock(['build', build_id]):
            if (self.state/record).exists():
                previous = self._read(record)
                if previous.get('state') != 'validated':
                    raise DeliveryBlocked('build_uncertain_no_replay')
                self._candidate(previous)
                return previous
            artifact = self.state/('artifact-'+build_id)
            if artifact.exists():
                raise DeliveryBlocked('unowned_artifact')
            self._save(record, {'state': 'building', 'source': source})
            build = self._observe('build', source, artifact=artifact)
            content = read_bytes(self.state, artifact.name)
            if not content:
                raise DeliveryBlocked('empty_artifact')
            artifact.chmod(0o400)
            candidate = dict(source, artifact_digest=_hash(content), build_id=build_id,
                             artifact=artifact.name, validation_digest=_hash(raw), locks=locks)
            evidence = [build]
            for name in ('artifact_validation', 'provenance') + (('sbom',) if self.policy.sbom_required else ()) + (('signature',) if self.policy.signature_required else ()):
                evidence.append(self._observe(name, candidate, artifact=artifact))
            if self._source() != source or _hash(read_bytes(self.state, artifact.name)) != candidate['artifact_digest']:
                raise DeliveryBlocked('artifact_or_source_changed_during_validation')
            candidate.update(state='validated', evidence=evidence,
                             committed_at=int(subprocess.check_output(['git','show','-s','--format=%ct',source['commit']],cwd=self.root)),
                             ci_started_at=min(r['started_at_epoch'] for r in receipt['commands']),
                             ci_ended_at=max(r['ended_at_epoch'] for r in receipt['commands']),
                             signature={'required': self.policy.signature_required, 'reason': self.policy.signature_reason})
            self._save(record, candidate)
            return candidate

    def _candidate(self, candidate, *, previous=False):
        if type(candidate) is not dict or not isinstance(candidate.get('build_id'), str):
            raise DeliveryBlocked('candidate_missing')
        canonical = self._read('build-'+_key({k:candidate[k] for k in ('repository','commit','source_digest','profile_digest','policy_digest')})+'.json')
        if (canonical != candidate or candidate.get('state') != 'validated'
                or candidate['repository'] != self.repository or (not previous and candidate['profile_digest'] != self.profile_digest)
                or (not previous and candidate['policy_digest'] != self.policy_digest)
                or _hash(read_bytes(self.state, candidate['artifact'])) != candidate['artifact_digest']):
            raise DeliveryBlocked('candidate_substituted_or_unvalidated')
        return self.state/candidate['artifact']

    def _grant(self, grant_id, candidate, environment, action, owner):
        grant = self.grants(grant_id)  # Fresh lookup also observes revocation.
        now = time.time()
        if (not isinstance(grant, ReleaseGrant) or grant.grant_id != grant_id
                or grant.repository != self.repository or grant.environment != environment
                or environment not in self.policy.environments or grant.target != self.policy.target
                or grant.artifact_digest != candidate['artifact_digest'] or grant.owner != owner
                or action not in grant.actions or grant.revoked is not False
                or grant.protected_environment is not True or not grant.evidence_ref
                or not _finite(grant.issued_at) or not _finite(grant.expires_at)
                or not grant.issued_at <= now < grant.expires_at
                or grant.identity_kind not in ('short_lived_workload', 'host_existing_credential')
                or set(grant.privileges) != {action}):
            raise DeliveryBlocked('existing_environment_release_grant_required')
        return grant

    def _migration(self, binding, artifact):
        evidence = self._observe('migration_applicability', binding, artifact=artifact)
        facts = evidence['facts']
        if (type(facts) is not dict or facts.get('applicable') is not self.policy.migration_required
                or facts.get('paths') != list(self.policy.migration_paths) or not facts.get('reason')):
            raise DeliveryBlocked('migration_applicability_unknown')
        if not self.policy.migration_required:
            if self.policy.migration_paths:
                raise DeliveryBlocked('migration_paths_cannot_be_exempted')
            return [evidence]
        if not self.policy.migration_paths:
            raise DeliveryBlocked('migration_identity_missing')
        identities = [{'path': p, 'digest': _hash(read_bytes(self.root, p))} for p in self.policy.migration_paths]
        migration = dict(binding, migrations=identities)
        compatibility = self._observe('migration_compatibility', migration, artifact=artifact)
        facts = compatibility['facts']
        if (type(facts) is not dict or facts.get('old_app') is not True or facts.get('new_app') is not True
                or facts.get('destructive') is not False or facts.get('sequence') != 'expand_contract'
                or facts.get('recovery') != 'tested_non_lossy'):
            raise DeliveryBlocked('migration_requires_product_data_decision')
        dry = self._observe('migration_dry_run', migration, artifact=artifact)
        recovery = self._observe('migration_restore_test', migration, artifact=artifact)
        if (dry['facts'].get('representative') is not True or recovery['facts'].get('restored') is not True
                or not isinstance(recovery['facts'].get('backup_digest'), str)
                or re.fullmatch('sha256:[a-f0-9]{64}', recovery['facts']['backup_digest']) is None
                or recovery['command_digest'] == dry['command_digest']):
            raise DeliveryBlocked('independent_recovery_measurement_required')
        return [evidence, compatibility, dry, recovery]

    def promote(self, candidate, *, environment, grant_id, operation_id, perform, action='deploy', deployment=None):
        """Invoke one independently authorized host action under its target lease.

        A retry only observes a persisted intent; it never repeats perform().
        The host callback may deploy; no callback is supplied by worker output.
        """
        if type(operation_id) is not str or not operation_id:
            raise DeliveryBlocked('operation_identity_required')
        if action not in ('deploy', 'release'):
            raise DeliveryBlocked('independent_action_required')
        if action == 'release':
            if (type(deployment) is not dict or deployment.get('state') != 'verified'
                    or deployment['binding'].get('action') != 'deploy'
                    or deployment['binding'].get('artifact_digest') != candidate.get('artifact_digest')
                    or deployment['binding'].get('environment') != environment
                    or self._read('deployment-'+_key(deployment['binding'])+'.json') != deployment):
                raise DeliveryBlocked('verified_deployment_required_for_release')
        artifact = self._candidate(candidate)
        source = self._source()
        binding = {k:candidate[k] for k in ('repository','commit','source_digest','artifact_digest','build_id')}
        binding.update(environment=environment, target=self.policy.target, operation_id=operation_id, action=action)
        name = 'deployment-'+_key(binding)+'.json'
        target = ['target', self.repository, environment, self.policy.target]
        lease_name = 'lease-'+_key(target)+'.json'
        with self._lock(target):
            grant = self._grant(grant_id, candidate, environment, action, self.profile['release']['owner'])
            if (self.state/lease_name).exists() and self._read(lease_name).get('operation') not in (None, name):
                raise DeliveryBlocked('target_unresolved_operation')
            if (self.state/name).exists():
                prior = self._read(name)
                if prior['binding'] != binding:
                    raise DeliveryBlocked('deployment_identity_changed')
                return prior  # Includes failed/uncertain; no hidden corrective replay.
            migration = self._migration(binding, artifact)
            self._candidate(candidate)
            self._grant(grant_id, candidate, environment, action, grant.owner)
            if self._source() != source:
                raise DeliveryBlocked('source_changed_before_deployment')
            expected_migrations = [{'path':p, 'digest':_hash(read_bytes(self.root,p))} for p in self.policy.migration_paths]
            if any(row['binding'].get('migrations') != expected_migrations for row in migration[1:]):
                raise DeliveryBlocked('migration_identity_changed_before_deployment')
            self._save(lease_name, {'operation': name})
            row = {'state': 'uncertain', 'binding': binding, 'grant_id': grant_id,
                   'grant_digest':_hash(dataclasses.asdict(grant)), 'authority_evidence_ref':grant.evidence_ref,
                   'migration': migration, 'started_at': time.time(), 'evidence': []}
            self._save(name, row)  # Durable intent before any mutation.
            try:
                perform(artifact, binding, grant)
                self._candidate(candidate)
                for check in ('deployment_identity',) + self.policy.health_checks:
                    row['evidence'].append(self._observe(check, binding, artifact=artifact))
                self._grant(grant_id, candidate, environment, action, grant.owner)
                row['state'] = 'verified'
            except Exception as exc:
                if hasattr(exc, 'evidence'):
                    row['evidence'].append(exc.evidence)
                row['verification'] = 'failed' if isinstance(exc, ObservedFailure) else 'unknown'
                row['state'] = 'failed_or_unknown'
                row['corrective_id'] = 'corrective-'+_key(binding)
                row['recovery_owner'] = self.policy.recovery_owner
            row['ended_at'] = time.time()
            self._save(name, row)
            if row['state'] == 'verified':
                self._save(lease_name, {'operation': None})
            return row

    def recover(self, deployment, candidate, *, grant_id, perform):
        """One host recovery owner, one durable corrective identity, no loop."""
        binding = deployment['binding']
        name = 'deployment-'+_key(binding)+'.json'
        target = ['target', self.repository, binding['environment'], self.policy.target]
        with self._lock(target):
            original = self._read(name)
            if original != deployment or original.get('state') != 'failed_or_unknown':
                raise DeliveryBlocked('current_failed_deployment_required')
            if binding.get('repository') != self.repository or binding.get('target') != self.policy.target:
                raise DeliveryBlocked('recovery_target_mismatch')
            recovery_name = original['corrective_id']+'.json'
            if (self.state/recovery_name).exists():
                return self._read(recovery_name)
            if self._read('lease-'+_key(target)+'.json').get('operation') != name:
                raise DeliveryBlocked('recovery_target_lease_changed')
            artifact = self._candidate(candidate, previous=True)
            grant = self._grant(grant_id, candidate, binding['environment'], 'recover', self.policy.recovery_owner)
            current = dict(binding, artifact_digest=candidate['artifact_digest'], build_id=candidate['build_id'],
                           commit=candidate['commit'], source_digest=candidate['source_digest'])
            recovery_evidence = []
            source = self._source()
            if original['migration'][0]['facts']['applicable']:
                migrations = original['migration'][1]['binding']['migrations']
                measured = self._observe('recovery_compatibility', dict(current,
                    current_artifact_digest=binding['artifact_digest'], current_build_id=binding['build_id'],
                    recovery_artifact_digest=candidate['artifact_digest'], migrations=migrations), artifact=artifact)
                facts = measured['facts']
                if facts.get('compatible') is not True or facts.get('non_lossy') is not True or facts.get('destructive') is not False:
                    raise DeliveryBlocked('non_lossy_recovery_compatibility_required')
                recovery_evidence.append(measured)
            self._candidate(candidate, previous=True)
            self._grant(grant_id, candidate, binding['environment'], 'recover', self.policy.recovery_owner)
            if self._source() != source:
                raise DeliveryBlocked('source_changed_before_recovery')
            row = {'state': 'uncertain', 'corrective_id': original['corrective_id'], 'owner': grant.owner,
                   'binding': current, 'deployment_record': name, 'grant_id':grant_id,
                   'grant_digest':_hash(dataclasses.asdict(grant)), 'authority_evidence_ref':grant.evidence_ref,
                   'started_at': time.time(), 'evidence': recovery_evidence}
            self._save(recovery_name, row)
            try:
                perform(artifact, current, grant)
                self._candidate(candidate, previous=True)
                for check in ('deployment_identity',) + self.policy.health_checks:
                    row['evidence'].append(self._observe(check, current, artifact=artifact))
                self._grant(grant_id, candidate, binding['environment'], 'recover', grant.owner)
                row['state'] = 'recovered_verified'
            except Exception as exc:
                if hasattr(exc, 'evidence'):
                    row['evidence'].append(exc.evidence)
                row['verification'] = 'failed' if isinstance(exc, ObservedFailure) else 'unknown'
                row['state'] = 'recovery_failed_or_unknown'
            row['ended_at'] = time.time()
            self._save(recovery_name, row)
            if row['state'] == 'recovered_verified':
                self._save('lease-'+_key(target)+'.json', {'operation': None})
            return row


    def reconcile(self, deployment, candidate, *, grant_id, recovery=False):
        """Observe an interrupted intent without repeating any deploy/recovery."""
        binding = deployment['binding']
        target = ['target', self.repository, binding['environment'], self.policy.target]
        with self._lock(target):
            if binding.get('repository') != self.repository or binding.get('target') != self.policy.target:
                raise DeliveryBlocked('reconciliation_target_mismatch')
            name = deployment['corrective_id']+'.json' if recovery else 'deployment-'+_key(binding)+'.json'
            row = self._read(name)
            if row != deployment or row.get('state') != 'uncertain':
                raise DeliveryBlocked('current_uncertain_intent_required')
            expected_lease = row.get('deployment_record') if recovery else name
            if self._read('lease-'+_key(target)+'.json').get('operation') != expected_lease:
                raise DeliveryBlocked('reconciliation_target_lease_changed')
            artifact = self._candidate(candidate, previous=recovery)
            if row['binding']['artifact_digest'] != candidate['artifact_digest']:
                raise DeliveryBlocked('reconciliation_artifact_mismatch')
            action = 'recover' if recovery else binding['action']
            owner = self.policy.recovery_owner if recovery else self.profile['release']['owner']
            self._grant(grant_id, candidate, binding['environment'], action, owner)
            try:
                row['evidence'] = [self._observe(check, binding, artifact=artifact)
                                   for check in ('deployment_identity',) + self.policy.health_checks]
                self._candidate(candidate, previous=recovery)
                self._grant(grant_id, candidate, binding['environment'], action, owner)
                row['state'] = 'recovered_verified' if recovery else 'verified'
            except ValueError as exc:
                if hasattr(exc, 'evidence'):
                    row['evidence'].append(exc.evidence)
                row['verification'] = 'failed' if isinstance(exc, ObservedFailure) else 'unknown'
                row['state'] = 'recovery_failed_or_unknown' if recovery else 'failed_or_unknown'
                if not recovery:
                    row.update(corrective_id='corrective-'+_key(binding), recovery_owner=self.policy.recovery_owner)
            row['ended_at'] = time.time()
            self._save(name, row)
            if row['state'] in ('verified', 'recovered_verified'):
                self._save('lease-'+_key(target)+'.json', {'operation': None})
            return row

    def metrics(self):
        """Derive metrics from this host's saved observations, not worker labels."""
        events = []
        def event(kind, at, subject, reference, **extra):
            events.append(dict(id=_key([reference, kind, subject]), kind=kind, at=at,
                               subject=subject, evidence=reference, **extra))
        builds = {}
        for path in sorted(self.state.glob('build-*.json')):
            row = self._read(path.name)
            if row.get('state') != 'validated' or row.get('repository') != self.repository:
                continue
            reference = _hash(row); builds[row['build_id']] = row
            event('ci_started',row['ci_started_at'],row['build_id'],reference)
            event('ci_finished',row['ci_ended_at'],row['build_id'],reference)
        for path in sorted(self.state.glob('deployment-*.json')):
            row = self._read(path.name); binding = row.get('binding', {})
            if binding.get('repository') != self.repository or binding.get('action') != 'deploy':
                continue
            reference = _hash(row); subject = path.name
            build = builds.get(binding['build_id'])
            if build:
                event('commit',build['committed_at'],subject,reference)
            if row['state'] == 'verified':
                event('deployed',row['ended_at'],subject,reference)
            event('deployment_terminal',row.get('ended_at',row['started_at']),subject,reference,
                  failed=False if row['state']=='verified' else (True if row.get('verification')=='failed' else None))
            if row.get('corrective_id'):
                event('deployment_failed',row['ended_at'],subject,reference)
                recovery_path = self.state/(row['corrective_id']+'.json')
                if recovery_path.exists():
                    recovery_row = self._read(recovery_path.name)
                    recovery_ref = _hash(recovery_row)
                    event('repair_started',recovery_row['started_at'],subject,recovery_ref)
                    if recovery_row['state']=='recovered_verified':
                        event('recovered',recovery_row['ended_at'],subject,recovery_ref)
        return {'metrics':delivery_metrics(events), 'events':events}


def delivery_metrics(events):
    """Traceable event pairs; missing data remains None, never zero success."""
    unique = {}
    for row in events:
        if (type(row) is not dict or not isinstance(row.get('id'), str) or not row['id']
                or not _finite(row.get('at')) or not row.get('subject') or not row.get('evidence')):
            raise DeliveryBlocked('metric_event_invalid')
        if row['id'] in unique and unique[row['id']] != row:
            raise DeliveryBlocked('metric_event_identity_conflict')
        unique[row['id']] = row
    values = list(unique.values())
    result = {}
    for metric, start, end in [('lead_time','commit','deployed'), ('ci_duration','ci_started','ci_finished'),
                               ('recovery_time','deployment_failed','recovered'),
                               ('waiting_time','waiting_started','waiting_ended'),
                               ('stale_age','opened','snapshot')]:
        pairs = []
        for subject in sorted({r['subject'] for r in values}):
            a = [r for r in values if r['subject']==subject and r.get('kind')==start]
            b = [r for r in values if r['subject']==subject and r.get('kind')==end]
            if len(a)==len(b)==1 and b[0]['at']>=a[0]['at']:
                pairs.append({'seconds':b[0]['at']-a[0]['at'], 'events':[a[0]['id'],b[0]['id']]})
        relevant = {r['subject'] for r in values if r.get('kind') in (start,end)}
        result[metric] = {'value':sum(p['seconds'] for p in pairs)/len(pairs) if pairs else None,
                          'samples':pairs, 'unknown_count':len(relevant)-len(pairs)}
    for metric, kind, field in [('flaky_test_rate','test_terminal','flaky'),('change_failure_rate','deployment_terminal','failed')]:
        all_rows = [r for r in values if r.get('kind')==kind]
        rows = [r for r in all_rows if type(r.get(field)) is bool]
        unknown = len(all_rows)-len(rows)
        result[metric] = {'value':sum(r[field] for r in rows)/len(rows) if rows and not unknown else None,
                          'events':[r['id'] for r in rows], 'unknown_count':unknown}
    result['repair_attempts'] = {'value':sum(r.get('kind')=='repair_started' for r in values) if values else None,
                                  'events':[r['id'] for r in values if r.get('kind')=='repair_started']}
    return result
