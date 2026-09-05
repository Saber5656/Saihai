# Bounded review lifecycle (Issue #136)

## U1: durable bookkeeping, integration pending

The optional `review_lifecycle` member belongs to one existing workflow run and
task. `review_lifecycle.initialize`, `apply_event`, and `observe` use the existing
private run store and global advisory lock. No separate scheduler or state store
is introduced. Runs without this member retain their existing behavior.

The API is an internal host consumer, not a public authorization endpoint.
The caller must resolve its authenticated principal through the existing host
boundary. An arbitrary dictionary naming a host principal is not authentication.
No API accepts `allowed`/`verified` flags as evidence, runs a provider, grants a
work order, performs an edit, or changes the run's execution/terminal state.
`reserve_repair` reserves bookkeeping budget only; execution must still satisfy
all existing work-order, approval, path, expiry and revocation checks.

The record binds the original task/run, host owner, approved activation digest,
original/current repository/base/head snapshot and immutable limits. Initialization
replay must match this identity. Owner changes and activation changes fail closed.
No new signing key or credential is generated. Store validation checks this
optional record on both read and write. A watcher uses `observe`; a bridge or
worker principal cannot mutate review state.

## Events and retained obligations

| Event | Durable effect | Restrictions |
| --- | --- | --- |
| `findings` | Triage semantic findings | Host-classified input only, not raw Bot input |
| `ci_pending` | None | Polls consume no budget and do not create event history |
| `reserve_repair` | Reserve one batch and spend one round before execution | Triage phase, existing task-owned mandatory repair targets within activation paths |
| `repair_failed` | Close batch, increment no-progress/same-blocker counters | Duplicate result is a no-op; conflicting result rejected |
| `repair_produced` | Hold new head for current-snapshot validation | Same repository/base, changed head; obligations remain unresolved |

A semantic finding key hashes `rule_id`, repository-relative `path`, and stable
`anchor`. Delivery/comment IDs do not identify findings or reset the shared
budget. The trusted consumer supplies stable rule/anchor normalization; changing
the content of these fields still cannot reset the lifecycle's global budget.
Ownership conflict stops the affected lifecycle. Optional delivery cannot
downgrade an existing mandatory finding. Finding rows retain severity, evidence
reference, task ownership and disposition; the ledger is bounded at 256 entries.
Oversized or malformed batches are rejected without partially storing changes.

Default limits are five repair reservations, two consecutive failed batches with
the same blockers, and two failed batches without progress. Smaller configured
limits are retained. Only an actual failed repair changes failure counters;
watching, duplicated findings/results and restarts do not. A reserved batch remains
reserved across restart, preventing a second executor reservation. Replaying its
ID is a bookkeeping no-op and must not be interpreted as permission to execute
again. Until U3 verifies a repair, a produced head cannot reset failure counters,
clear obligations, begin another batch, or mark the lifecycle complete.

Scope classification uses the original task ID and existing activation edit/path
scope. Mandatory findings belonging to another task stop affected work; optional
findings are deferred. Neither is an automatic repair grant. Relative literal
paths are supported; ambiguous/absolute activation paths do not grant a match.
The #138 producer integration is pending, so fixtures and this classifier are not
proof of a production intake acknowledgment. Evidence references are retained
as data and never read or executed by this module.

## Subsequent units and effective policy

U2 will bind logical Bot intake to PR/Bot/policy identity and preserve its original
snapshot. U3 must connect authenticated report/evidence validation to this API
and implement validated obligation discharge and subsequent publication stages.
There is intentionally no `validation_passed` event accepting a caller boolean.
Current `report_gate` remains single-step and templates are unchanged in U1.
`integration_status` is always `integration_pending`; no workflow execution,
merge readiness, runtime acceptance, or policy activation is claimed here.

The existing effective review/CI/merge gates remain required until the versioned
transition through dotfiles#11, skills#41, Saihai#128 and #141 is verified. Bot
configuration changes, provider calls, publication, merge and post-merge gates
are outside U1. A migration must preserve the same durable owner/budget and must
not turn old Bot evidence into current-snapshot acceptance.
