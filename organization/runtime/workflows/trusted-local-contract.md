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
