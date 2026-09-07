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
