# Trusted-local usage execution

This explicitly authorized local route runs an installed Codex CLI, real scoped
validation, and host publication. It uses existing CLI authentication without
creating, copying, or configuring credentials. The worker cannot publish.
Ordinary development records review as `not_required`; boundary changes require
the parent's one scoped risk review reference. This route does not fabricate a
human activation or managed-domain attestation.

## Host entry points

```text
python3.11 scripts/saihai.py usage run --request /absolute/request.json --authorization /absolute/authority.json --state-root /absolute/private-state
python3.11 scripts/saihai.py usage advance --authorization /absolute/authority.json --state-root /absolute/private-state
```

The request has exactly `task_id`, `request_id`, `run_id`, `execution_id`, and
`instruction`. The independently supplied authority file is host-owned, canonical,
absolute, and mode 0600. Its fields match `TrustedLocalAuthorization`: the
`publication` object documented in [host publication](host-publication-contract.md),
absolute `executable`, `executable_digest`, existing `codex_home`, approved `model`,
`validation_commands` (arrays of argv strings), `review_policy` (normally
`normal_optional`, or `scoped_risk_once`), and `timeout_seconds` (default 900).
Authority and state must be outside worker-writable scope. No worker result can
select these inputs or authorize publication.

`usage run` claims the execution ID once, fixes the installed runtime digest,
uses a task-scoped CLI filesystem policy, checks actual changed paths against
approved scope, and runs the exact host-selected validation commands through
that policy. Successful process and validation evidence bind the actual tree
and binary diff. Worker-declared test success is not validation evidence.
Failures produce a blocked outcome and no publishable report. A claimed worker
execution is not silently replayed.

`usage advance` performs one bounded host publication step. Call it again for
pending CI. On another PR's conflict, the host fetches main and merges it locally;
the worker can repair approved conflict files while the host owns Git mutations.
The new tree receives real validation, a fresh execution identity, and fresh
host publication authority before the same branch and PR resume. Only repeated
failures of the same unresolved conflict count toward the limit of five; unrelated
historical review attempts do not block this route. Out-of-scope conflicts or
requirement decisions stop for user input. An ambiguous local merge commit stops
as `integration_reconciliation_required` rather than repeating that mutation.

After merge, `usage advance` checks required CI against the actual merge SHA.
Only `complete` means integrated CI succeeded; `integrated_ci_pending` is resumable
and `integrated_ci_failed` blocks further completion. Release remains separate.

## Evidence and limits

`state-root/trusted-local/execution-id` contains the private claim, request,
authority reference, process receipt, worker result, validation, review, report,
outcome, and publication state. Process receipts retain PID/start token, command
and environment digests, timestamps, exit status, and output digests. Conflict
continuations retain each fresh identity and validation. The host must serialize
writers to the worktree and protect this evidence directory.

The installed CLI engine accesses its existing authentication normally; spawned
worker tools are constrained by the explicit filesystem policy and network deny.
This is a trusted-local operating assumption, not proof of hostile-process or
managed-domain isolation. The host runs Git/GitHub using existing authentication;
GitHub native protection enforces the configured publication policy. Live startup
and local sandbox probes complement fixture tests; synthetic GitHub fixtures do
not by themselves prove a real end-to-end task.

## Repair a failed validation without replaying the worker

```text
python3.11 scripts/saihai.py usage repair-validation --authorization /absolute/original-authority.json --state-root /absolute/private-state
```

This host entry requires the original authority and an actual failed validation
receipt. The current dirty tree and binary diff must exactly match that receipt;
HEAD, branch, scope, runtime digest, and validation commands remain bound to the
original task. It creates a new `original-execution-id-repair-N` directory and
exclusive claim. The worker receives the retained task result and only the
validation repair instruction. It cannot replay the original execution or widen
its write scope. Each consecutive identical failure permits at most five repairs.
An interrupted repair without a failed validation receipt requires inspection;
it is not blindly spawned again.

Failed validation retains private diagnostic JSON, bounded to the last 16 KiB
of each output stream, with a digest in the receipt. Repair input explicitly
labels this output untrusted. Older digest-only receipts are preserved; the host
re-observes the same failed tree with the same validation plan to obtain a
separate `repair-diagnostic-validation.json` and diagnostic artifact.

The original directory's `validation-repair.json` is the host continuation:
`original_authorization_digest`, `execution_id` (the current child), `status`,
`attempt`, `cause`, and `same_cause_retries`. Readers show the original intake but
follow this current execution reference for process, validation, outcome and
report. A `running` or `failed` continuation must never make the old failed
receipt look successful. A `validated` continuation identifies a child whose
real validation passed. `usage advance` accepts the original authority and
publishes that child's report, retaining publication state in the original
directory. Original claims and validation receipts remain unchanged.

The optional `--repair-instruction` supplies up to 8 KiB of explicit host guidance
within the existing task scope; it cannot alter authority or allowed paths.
The monotonically increasing `attempt` is separate from `same_cause_retries`;
a changed failure cause resets only the consecutive counter. Validation timings
and the private execution directory are normalized out of the cause identity.

## Bounded automatic host progression

```text
python3.11 scripts/saihai.py usage drive --request /absolute/request.json --authorization /absolute/authority.json --state-root /absolute/private-state
python3.11 scripts/saihai.py usage drive --authorization /absolute/authority.json --state-root /absolute/private-state
```

`usage drive` composes the existing run, validation repair and publication
advance APIs. The request is required only before the exclusive execution claim
exists. Resume checks the original authority and, when supplied, request digest.
An existing claim without a usable execution receipt stops for inspection; it
never starts the original worker again. The canonical private state directory
must be outside the authorized worktree.

Defaults are 32 admitted operations, 300 seconds, and five seconds between CI
observations. `--max-iterations` accepts 1–256, `--duration-seconds` accepts a
positive value up to 3600, and `--poll-interval-seconds` accepts 0–60. These are
operation admission bounds: an already admitted operation retains its existing
worker timeout. CI polling consumes no repair budget. Actual failed validation
receipts use the existing same-cause repair limit; driver invocation does not
reset that limit. The state-root lock is released before polling sleeps.

The result distinguishes terminal completion, resumable bounds, human decisions,
uncertain mutations and blocked execution. Publication reconciliation remains
owned by the existing adapter. Completion persistence receipts are returned
separately, and pending persistence does not become task completion. This driver
does not write the canonical Vault. Private `drive.json` and
`drive-events.jsonl` contain bounded status metadata, not worker transcripts.
The existing `usage run`, `usage advance`, and readonly `drive-run` remain
available. Synthetic GitHub tests verify scheduling, not a live deployment.

## Validation profiles and current evidence

Host validation now emits version 2 receipts. Each command retains actual argv,
time, exit and output digests. Test commands require positive measured counts and
no failed/skipped/unknown results. A non-test check records no invented test count.
The receipt binds all nonignored source bytes/modes, the exact host command plan
and selected executable. Publication and code-change final gates recheck current
artifacts; a provider's `passed` field alone is insufficient. Legacy versionless
receipts remain history and are not upgraded into current successful evidence.

During host integration, previous results are reused only when the complete source
digest, command plan, executable bytes and optional profile reference are unchanged.
Head/base changes alone do not require rerunning unaffected work. Changes to any
source input conservatively require revalidation; no transitive dependency inference
is claimed. Full validation belongs to the integrated feature, not each commit.
The file reader limits each source/artifact to 2 MiB and rejects parent symlinks;
oversized input is an explicit unavailable result, never successful validation.

`TrustedLocalAuthorization.validation_profile` is optional. When absent it is
omitted from authorization hashing to preserve previous authority identity. When
present it is `{path, sha256}`: a canonical absolute mode-0600 JSON file outside
the worker worktree, selected by the host. It binds an existing delivery `profile`,
`layers`, `behavior`, `owners`, `owner_evidence`, `artifact`, `device_evidence`, and
`tdd`. No worker report can select this file. Profile-less tasks retain the exact
host-selected command plan; they do not claim mobile or release readiness.

`layers` names static/unit/feature/e2e/build/security/full/device. Each entry has
`required` (boolean), a nonempty host applicability `reason`, and `commands` (zero
based indexes into host validation_commands). Required test layers need measured
tests. Required full evidence cannot be a single shard. Existing profile minima
cannot be disabled. A docs-only plan cannot exempt runtime instructions or code.

Owners are `{ci, cd, rollback}`. The digest-bound owner artifact must exactly match
repository, profile_digest, owners, release targets and rollback prerequisites.
Mobile behavior additionally needs a current artifact ref and digest-bound physical
device record (model, OS, operation, start/end, passed status, artifact hash and
image ref). Simulator and different-build results fail. Behavioral profile plans
retain digest-bound Red/Green records, unchanged test refs and explicit refactor
disposition. Host-owned acquisition is the trusted-local provenance assumption;
this does not claim managed-domain attestation or authorize a release.

For Saihai itself, the existing workflow inventory is compared with actual
workflow bytes and locks at validation time. CI retains eight pinned-runtime
shards and the aggregate required `validate` job. Shard receipts explicitly bind
index/count so they cannot be mistaken for a whole-suite run. Only actual CI
results can satisfy remote checks. Luna invocations explicitly select max reasoning
without changing global CLI configuration.

Legacy standard_code_change QA/final/completion consumes the host-owned
`reports/<run_id>/host-validation.json`, bound to the latest completed implement
execution and current worktree. Hosts must run actual scoped validation to produce
it; the gate never manufactures one from a provider report. The normal usage route
continues to use its own execution directory and independently supplied authority.


## Finished worker process recovery

Worker results use a private strict provider schema: every property is required,
with nullable forms for canonical optional fields. Only optional null values are
removed before the unchanged canonical validator. Raw results remain saved.
Normal worker processes now retain private bounded stdout/stderr diagnostics and
post-process source identity, including nonzero exits.

`usage drive` can continue a proven finished nonzero worker process in a fresh
child execution. It verifies the original and current claims, unchanged authority
and saved request digests, dead process identity, and exact source tree. Missing,
running, unknown or successful process evidence is not retry authority. Legacy
failures without a saved source identity permit only an unchanged clean authorized
HEAD. Old claims, requests, process receipts and outcomes remain intact; the host
`validation-repair.json` continuation selects the current child for status and
publication, including subsequent validation repair.

Five consecutive observed failures of the same typed cause and measured worker
strategy stop that sequence. The strategy fingerprints wire schema, decoder,
provider command, prompt and approved executable/model/installation. Retry IDs,
host restarts and unrelated commits do not reset it. An actual correction starts
a new sequence while all earlier attempts remain saved. Driver iteration and
duration bounds remain independent.

If an approved harness correction changes installed bytes, the host may supply:

```sh
python3.11 scripts/saihai.py usage drive --authorization /absolute/authority.json --state-root /absolute/private-state --worker-recovery-plan /absolute/private-plan.json
```

This explicit host file is never selected from worker/request output and must be
private and outside the worker checkout. Only after proving the failed execution
may the child pin and read back the new plan. Existing catalog roots, member
locations, surface, policies, symlinks, sync destination and authorization remain
unchanged; only source commit and expected content digest may differ. The old
plan is preserved, its successful readback is not asserted, and a parent/child
continuation receipt records both plan digests. No automatic plan replacement,
authority expansion, runtime rollback or credential handling is performed.
