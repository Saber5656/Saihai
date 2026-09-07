# Application delivery contracts

`delivery_contract.application_delivery_host()` connects the existing seven
repository delivery profiles to `application_delivery.DeliveryHost`. These APIs
are invoked by the trusted host. They do not change PR publication authority,
provision credentials, create grants, or configure environments. Normal code
validation and merge remain separate from deployment and release.

## Host inputs and repository policy

The host supplies the existing delivery profile, a canonical private state root
outside the worker checkout, approved measuring commands, and a live lookup for
existing `ReleaseGrant` objects. All hosts operating a protected target must use
this same state root. A separate state root is not an alternate concurrency
namespace. This is the existing trusted-local boundary, not remote worker
isolation or a distributed lease service.

`policy` accepts a `ReleasePolicy` pinned to the current source digest, or a
host-pinned `{path, sha256, bytes}` reference to a repository JSON policy.
`load_release_policy()` reads the exact bytes, rejects unknown fields and binds
the policy to the current checkout. The JSON has every field below; it does not
contain authority or a self-referential source digest.

| Field | Meaning |
| --- | --- |
| `environments` | Existing named environments permitted by this policy |
| `target` | A target already declared in the delivery profile |
| `signature_required`, `signature_reason` | Explicit signature applicability |
| `sbom_required`, `sbom_reason` | Explicit dependency inventory applicability |
| `migration_required`, `migration_paths`, `migration_reason` | Change-aware data applicability and exact repository paths |
| `recovery_owner` | The one host recovery owner |
| `health_checks` | Host observer names including `smoke` and `health`; add telemetry/synthetic checks when applicable |
| `timeout_seconds` | Per-observation limit, 1–600 seconds |

Do not deserialize grants or commands from worker reports. The host's grant
lookup must read the current independently approved authority, including
revocation. Grants bind repository, environment, target, artifact digest,
action, owner, expiry, protected-environment status, identity kind, exact
privileges and an existing evidence reference. Short-lived workload identity is
supported; a preexisting host credential may be used where the environment does
not support it. Credential generation and setup remain human-owned.

## Build once and promote immutable bytes

1. `build_once(validation_receipt, validation_identity)` verifies an actual
   private host validation v2 receipt against current source and supplied
   identity, verifies dependency lock bytes, and requires a clean committed
   checkout. It persists a build intent before invoking the host build command.
2. The build writes only the supplied external artifact path. The host measures
   that file, makes it read-only, runs artifact validation, provenance and
   applicable SBOM/signature checks, then rechecks the source and artifact bytes.
   The saved candidate binds source commit, complete source digest, profile,
   policy, lock identity, validation receipt and all command observations.
3. The same build identity returns the saved validated candidate. Substituted
   bytes, caller-edited candidate records, unknown prior builds and current
   source drift block. A failed or interrupted build is not silently replayed.
4. `promote(..., action="deploy")` consumes the current deploy grant and holds a
   nonblocking per-target lock through the host action and verification. A
   persisted unresolved target intent blocks competing operation IDs after a
   crash as well as simultaneous calls.
5. `promote(..., action="release", deployment=...)` additionally requires a
   saved verified deployment of this artifact in this environment and a
   separate release grant. A merge or deploy grant cannot authorize release.

The `perform(artifact_path, binding, grant)` callback is an existing trusted host
adapter. The controller invokes it at most once per durable operation identity.
No real deployment adapter is installed by this feature. Integration must use an
immutable digest-addressed upload/promotion operation; it must not rebuild the
artifact or ignore the grant's target. The controller rechecks the local bytes
and measures the deployed identity after the callback.

## Actual observations

Commands are approved host argv tuples. `SAIHAI_ARTIFACT` identifies the exact
artifact file; `SAIHAI_DELIVERY_BINDING` contains the repository/source/artifact,
environment, target and operation identity to measure. A command returns one
bounded JSON object with `binding`, `result`, positive `cases`, and optional
`facts`. The existing strict host result parser rejects zero, missing, skipped,
failed and unknown required test counts. A binding echo alone is insufficient:
the adapter must actually inspect the artifact/target/provenance/signature and
assert the measured identity. Commands and their executable identities belong
to the trusted host, never to an untrusted application report.

Saved observations contain timestamps, exit status, count classification and
output digests. Raw process output is not put into generic delivery records.
Migration facts use a closed field allowlist; adapters must redact production
data and secrets. Artifacts and individual JSON inputs currently share the
existing 2 MiB bounded reader. Larger production artifacts require a streaming
host adapter before adoption; this implementation does not claim support for
large application bundles.

## Migration and recovery

`migration_applicability` measures the changed paths and explicit applicability.
A non-data change saves that observation and runs no migration procedure. A
migration requires exact migration-file digests and these host observations:

| Observation | Required measured result |
| --- | --- |
| `migration_compatibility` | Old and new app compatibility, non-destructive expand/contract sequence, tested non-lossy recovery |
| `migration_dry_run` | A representative successful dry run |
| `migration_restore_test` | An independent command with successful restore and backup digest |

These results bind the candidate, environment, target and operation. Destructive,
incompatible, irreversible or recovery-unknown changes block for a product/data
requirement decision. No automatic rollback promise is made for those changes.
The included offline fixture actually exercises a temporary SQLite migration,
backup and restore; it does not operate an application or production database.

After deployment, `deployment_identity`, smoke, health and all policy-selected
checks must pass for the current deployed artifact. Failure, timeout, unknown
identity or revoked authority remain non-success and retain the target lease.
Only dependent work for that target is blocked; other targets remain independent.

`recover()` accepts the canonical failed deployment and one already validated
rollback or roll-forward candidate, consumes a fresh recovery grant for its
artifact and the single owner, and persists one corrective ID before acting.
For data migrations, a fresh `recovery_compatibility` measurement must bind the
current artifact, proposed recovery artifact and the original migration digests,
and establish compatible, non-destructive, non-lossy recovery before mutation.
Missing or mismatched evidence blocks. Non-data changes omit this measurement.
The host rechecks source/migration identity immediately before deployment and
source identity after recovery preflight. There is one recovery attempt. Duplicate calls and restarts return that record;
they do not create another task or another mutation. `reconcile()` can measure an
interrupted intent without replaying a deployment or recovery callback. Only
verified recovery clears its target lease. The corrective ID is available to the
existing task system as its idempotency key; this module creates no GitHub Issue.

## Traceable metrics and limits

`DeliveryHost.metrics()` derives events from saved build/deployment/recovery
observations, linking each event to its host record digest. `delivery_metrics()`
also accepts host event streams for CI flakiness, waiting periods and task age.
Duplicate event IDs are deduplicated; conflicting IDs are rejected.

| Metric | Event definition |
| --- | --- |
| Lead time | Commit timestamp → verified deployment, per deployment |
| CI duration | Actual validation command start → end |
| Flaky-test rate | Host-classified flaky test terminal events / known test terminal events |
| Change-failure rate | Measured failed deployment terminal events / known deployment terminal events |
| Recovery time | Failed deployment → verified recovery |
| Stale PR/task age | Opened → host snapshot event |
| Waiting time | Waiting started → waiting ended; separate from repair attempts |

Missing data produces `null`, with unknown counts where events are incomplete.
Unknown deployment outcomes do not become a zero failure rate. These metrics do
not estimate model quality. Deployment/release, DB changes and credentials were
not exercised by the offline validation suite. The host authority boundary is
subject to the parent task's single limited security review before publication.
