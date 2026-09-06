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

U2 binds inactive Bot intake candidates to PR/Bot/policy identity and preserves
the original snapshot, as described below. U3 must connect authenticated report/evidence validation to this API
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

## U2: inactive logical Bot intake candidates

`prepare_intake` and `record_intake_candidate` add an optional `intakes` ledger
without changing existing U1 required fields or repair counters. A logical key
uses repository, PR number, configured Bot and policy version. Repository/Bot
case is normalized for that key; head, delivery ID and run ID are excluded. The
host must resolve the configured Bot identity consistently; this module does
not infer aliases or authenticate an actor from a display name. The first
preparation freezes the then-current snapshot. Later repair heads do not reset it.

Under the same host lock, preparation scans the existing private run records for
that key. Another owning run, including an expired or terminal run, cannot be
silently replaced. Invalid, unreadable, quarantined, diagnostic, or unexpected
artifacts make lookup incomplete and prevent preparation/observation. Scans use
private read helpers without quarantining a possible owner out of the next scan.
No independent index/store or garbage collection is added. The scan is capped
at 10,000 artifacts; oversized state stops instead of guessing that no owner
exists. Each run holds at most 16 logical intakes and each baseline at most 32
later candidates. Capacity errors preserve the prior saved state.

| Internal event | Stored result | Deliberate limit |
| --- | --- | --- |
| `prepare_intake` | One inactive logical plan and original snapshot | Not a request reservation, send instruction or permission |
| `delivery_unknown` | Unknown delivery retained across resume | Cannot reset to planned or request another run |
| `request_observed` | Immutable typed request candidate | Reconciliation remains unauthenticated until U3 |
| `response_observed` | Immutable first response candidate | Neither no-findings nor findings is an acceptance proof |
| `later_response_observed` | Separate bounded later candidates | Must be routed to genuine-blocker triage by U3, not dropped or treated as a retrigger |

Candidate mappings have exact fields. They carry repository/PR/Bot/policy,
request identity and snapshot; responses additionally carry response identity,
no-findings/findings outcome and bounded typed findings. Arbitrary reactions,
silence, error, timeout, wrong identity and stale initial snapshot cannot be
stored as a qualifying response candidate. Extra `verified` or permission flags
are rejected. Candidate shape validation is not producer authentication.
Original candidate IDs are retained; semantically identical response delivery
with a different response ID is a no-op. Conflicting first candidates cannot
overwrite the baseline. Later snapshots must retain its repository/base.

The APIs never execute outbound requests, move U1 phases, resolve findings or
reset its budget. `authentication_status` stays `integration_pending` and
`policy_status` stays `inactive`; no event can change those constants. A real
GitHub publisher/observer receipt producer is not yet connected. Consequently,
these candidates cannot satisfy a required Bot gate or prove successful delivery.

### Prepared phase-policy example

`profiles/review-phase-policy-v1.example.json` describes the desired trigger
plan only. The three repository names are scoped targets for a separately
approved settings executor; inclusion is not authority to mutate them. The Bot
names are configured logical labels, not proof of authenticated GitHub actors.
Manual initial mode excludes automatic-on-open and all modes exclude automatic
push retriggers. Automatic-initial mode observes the initial automatic request
instead of also asking for a manual one. The plan validator rejects conflicting
modes and only accepts inactive/pending status.

These controls are abstract desired behavior, not asserted provider-specific
setting names or observed effective settings. Current CodeRabbit controls remain
unknown, and a Codex inheritance read-back alone does not establish adoption.
The real executor must bind supported controls, preimage, authority, verification,
read-back and recovery to the scoped version before the transition can activate.
The example retains the current effective gates and lists dotfiles#11, skills#41,
Saihai#128 and #141 prerequisites. Nothing reads this example as active runtime
policy. Unsupported controls or an absent authenticated producer stay pending.

### U3 handoff obligations

U3 must authenticate request/result receipts through the existing host boundary,
validate the effective policy version and bind current-snapshot quality evidence
before accepting anything. It must triage later unsolicited genuine blockers
under the same owner/budget and distinguish initial Bot evidence from current
repair acceptance. This unit's offline fixtures prove none of those runtime facts.

Carry review note `S136-U1-NOTE-01`: malformed principal input can raise an
internal exception. The authenticated consumer must convert malformed input and
producer/API errors into a bounded blocked/stop result with no saved mutation or
execution grant; it must never synthesize an authenticated principal. This is a
required U3 connection contract, not a reason to relax current gates in U2.
# Pure observation adapter — classification only

`scripts/review_observation_adapter.py` exposes the pure function
`classify_observation(raw_json, expected=...)`. It reads no files, calls no
provider, stores no lifecycle state and returns no execution/acceptance grant.
Inputs are caller-supplied JSON bytes, not authenticated transport receipts.
`expected` contains repository, PR, base, head and nullable provider request ref;
it is a comparison target and does not confer authority.

The versioned classifier checks numeric actor IDs `136622811` (CodeRabbit) and
`199175422` (ChatGPT Codex connector), exact login and Bot type. These checks
recognize metadata, not the authenticity of the JSON. Authentication status
always remains `integration_pending`, even for a lexical `candidate_match`.

The CodeRabbit grammar recognizes only the generated top-level quota warning
stanza or the exact rate-limited command reply observed on PR154. General errors,
HTTP 429 alone, skipped reviews, quoted examples, duplicate/mixed review sections
and unknown grammar do not qualify. Parsed provider Run ID/command hash is a
`request_ref` candidate; it is not falsely labeled the GitHub manual-trigger
comment ID. Parsed base/head remain absent when the provider reply omits them.

ChatGPT review observations use review ID, structured state and `commit_id`.
Summary comments and reactions are not review results. COMMENTED, APPROVED and
negative states are preserved without conversion to gate success. Historical
base/request identity is not invented from the current PR. Unresolved findings
are not cleared; the input body remains represented by its exact raw digest.

Each bounded input (maximum 1 MiB) produces a raw digest and, when metadata is
valid, an observation key incorporating resource URL, actor ID, observation ID,
provider updated/submitted timestamp and digest. Changed content or timestamp
is therefore a different observation. Duplicate JSON keys/malformed metadata
are rejected into pending. Missing or mismatched resource/request/snapshot
identity remains pending. Resource URL must identify the expected repository,
PR and comment/review ID. No authenticated loader is implemented here.

The current dependency audit found **no verified existing #133 authenticated
GitHub receipt loader or actual review transport API**. Existing provider-runner
context verification and external-review report validation do not supply this
quota/GitHub contract. Proposed publication runtime is not installed and #128's
backend singleton is not evidence of an active production backend. These are
unimplemented dependencies, not operational APIs this adapter can call.
Transport authority remains #133; readiness is #128; skills41 owns its entry
contract. The causal executor connection remains pending, including mechanical
path-scope enforcement. No authority, waiver, key generation or budget reset is
introduced to bridge these gaps.

Tests cover actor spoofing, restrictive grammar, negative states, timestamp/raw
identity, resource/request/snapshot mismatch and bounded malformed input.
Replaying actual saved PR154/155 observations validates classification only:
it is not actual fallback dispatch, conflict resolution, or runtime deployment.

# U3 B — causal conflict candidates (integration pending)

`conflict_observed` records a host-owned candidate for a base change caused by
another PR merge. It binds the owned PR, other merged PR, merge SHA, old/new
repository/base/head, task, owner, branch, sorted in-scope path references and
evidence reference/digest. The conservative candidate form requires the observed
new base to be exactly that merge SHA; a subsequent base advance needs producer
reconciliation. The old snapshot must be current on first observation. This is
not proof that a merge or conflict actually occurred.

Dirty, out-of-scope, stale, same-PR, owner/task mismatch, active repair, stopped or
budget-exhausted observations cannot start a new candidate. The branch field is
an immutable claimed identity within this task's candidate sequence; actual
checkout/dirty/causal-merge verification is pending the existing producer and
executor contract. No Git command, edit, push, merge, key creation or authority
grant is performed by this API.

An accepted candidate conservatively withholds quality readiness by moving to
`current_snapshot_validation` and records `proof_status=invalidated`. Both the
current snapshot and original Bot baseline remain unchanged: unverified new
base/head data cannot become an authenticated snapshot. The candidate's
`authentication_status` remains `integration_pending`. No validation-pass or
conflict-authorized transition exists. A prior stopped state cannot be reopened
by replaying an existing candidate. Existing findings and negative results remain.

At most five immutable logical causal candidates are retained. Redelivery is
idempotent; conflicting evidence for the same causal identity is rejected.
Owner, branch, counters, limits and repair batches survive restart. Observation
does not reserve or execute a repair and therefore does not spend a repair round.
The future authenticated conflict executor must reserve from the same cumulative
budget, preserve dirty work and both approved requirements, and obtain focused/
full validation and independent role review for the changed identity before
normal push. It must not create a fresh run to reset the budget or use force push.

TDD acceptance covers causal identity, stale/dirty/scope/owner rejection,
deduplication and conflicting evidence, cap/stopped preservation, unchanged
budget/baseline, quality invalidation without fake authentication, durable
corruption and bounded capacity. Tests are in `tests/test_review_lifecycle.py`.
Actual conflict resolution and actual authenticated reviewer integration remain
pending. Neither U3 A nor U3 B completes Issue #136 by itself.

# U3 A — quota-only alternate candidates (integration pending)

This unit adds host-owned bookkeeping only. It sends no request and does not
authenticate a Bot receipt, activate policy, accept a review, or waive a gate.
The existing host boundary remains responsible for resolving principals. No
caller-supplied authentication flag is accepted. Integration with the real
producer and consumer remains pending under #133/#128.

`quota_observed` stores a separate `usage_limit` failure candidate for an observed
CodeRabbit request. Its repository, PR, Bot, policy, request and original snapshot
must match. Issuer, evidence reference and digest are candidate metadata, not
proof of authenticity. `usage_limit` is never a successful response outcome.
Timeout, generic error, silence, skipped execution and HTTP 429 alone do not
qualify. Only a future authenticated quota-only producer may authorize fallback.

One optional `alternate` record per logical intake retains the immutable quota
candidate, independent request/result identities and an unknown-delivery state.
Its status always remains `integration_pending`; even a no-findings result is
not acceptance. Errors and rejected results are retained and cannot be rewritten
as success. Primary responses, later findings, task obligations, owner, repair
budget, CI, GitHub protection and mandatory role/atomic merge gates are preserved.

An existing configured ChatGPT intake (`chatgpt` or `codex` logical label) must be
reconciled before any new alternate request observation. An observed initial
request and result on the same PR/policy/current snapshot can be linked using
`alternate_existing_intake_linked`. Linking sends nothing. Pending or cross-run
intakes block new request observation; cross-run linking awaits producer
integration. Labels alone never prove the provider. A new manual `chatgpt`
candidate is allowed only when no such existing intake is found in the complete
owner scan. This is record validation, not outbound permission.

New request/result observations require the current snapshot. Historical duplicate
observations remain idempotent without becoming renewed proof; original Bot
baseline and alternate request are never rewritten after head changes. There is
no resend/reset API. Result absence or stale identity cannot advance the lifecycle.

TDD acceptance: exact quota versus generic failures/fake auth; missing request or
result; separate alternate identity; primary finding preservation; negative-result
immutability; stale snapshots; durable corruption; restart/duplicate delivery;
existing configured initial result reuse without a second request. Focused tests
live in `tests/test_review_intake.py`. This unit is not actual fallback runtime
acceptance, publication readiness, or completion of Issue #136.
