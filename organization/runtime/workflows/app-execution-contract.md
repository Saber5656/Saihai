# Host-mediated visible App execution (#146/#147)

The existing #67 child action remains a gateway-authorized **record** API.
It neither creates an App task nor reconciles pending results. This host adapter
adds a separate private journal and resource reservations; it does not replace
that immutable record or change the P0 serial workflow concurrency setting.

## Supported integration

The authorized desktop host imports `scripts/app_execution.mjs` and supplies
`createAppHostAdapter({hostTools, journal})` with the documented tool callbacks:
`list_projects`, `create_thread`, `list_threads`, `read_thread`,
`send_message_to_thread`, `wait_threads`. Map these directly to the existing
Codex App tools, without credentials, UI automation or internal IPC. The factory
captures only those callbacks and exposes `step(operationId)`. It receives tool
returns from the awaited call itself, with no public `step(resultJSON)` method.

`journal.next/accept/lost` are the authorized host's bindings to
`app_execution.HostAppJournal`. The Python object takes an existing
`TrustedLocalAuthorization` and a private state root outside worker checkouts.
It is an embedding API, **not** a worker-facing HTTP/JSON endpoint. No standalone
Python-to-App transport is claimed: only the desktop host can supply its actual
session tools. Test callbacks and host-generated JSON do not prove live App
execution. Response digests are integrity references, not authentication or
attestation signatures. A host violating this trust boundary can forge local
records; this module does not claim to prevent a malicious authorized host.

Use `reserve(plan)` before stepping. The closed plan contains `operation_id`,
`issue_id`, `repository_key`, `project_id`, `host_id`, `base`, `instruction`,
`resources`, `dependencies`. Repository keys come from the fresh primary catalog;
only the current local host is supported for checkout inspection. The approved
host base, repository and canonical task binding must match. Other hosts return
`remote_checkout_verifier_unavailable`, never a local filesystem proxy proof.
No new model, role, credential or App filesystem permission is set.

A plan first verifies the real saved project, then sends one create request with
a **read-only startup instruction** and the approved base ref. The App chooses
its output worktree/branch. A returned actual UUID is checked via list/read and
real local Git origin/top-level/common-dir/worktree registry/branch/HEAD/clean
readback. Only a registered, distinct `codex/` child checkout can receive the
implementation message. It is rechecked immediately before dispatch.

Read-only startup is an instruction, **not proof of App sandbox enforcement**.
This adapter withholds implementation dispatch until identity verification; it
cannot revoke arbitrary App editor access. It does not claim the completed
#101/#113 managed boundaries merely because a visible task exists.

## Durable state and uncertainty

| State | Meaning / next action |
|---|---|
| planned | task/repository/host scope fixed; project verification and one creation claim follow |
| creating | creation is claimed before tool invocation; restart never repeats an in-flight call |
| pending | only client ID exists; no actual-ID tool calls or implementation dispatch |
| creation_unknown | create response lost/unstructured/error; reservation retained, no resend |
| identity_waiting / checkout_waiting | actual ID exists but host/project/checkout checks are incomplete |
| ready | checked child checkout; recheck before send |
| dispatch_unknown | send result uncertain; no blind resend |
| sent | structured tool acknowledgement only; no startup-success claim |
| running / polling | exact instruction-bearing user turn observed; wait wakes, read attributes result |
| terminal | same attributed turn completed and App task not active; source task is still not complete |
| blocked / quota_queued | identity or availability blocker; retain unknown writer ownership |

Actual task/turn IDs must be UUIDs. Pending `client-*` values are never usable
thread IDs. Matching title, time, or cwd does not map a pending ID to an actual
thread. Current tools have **no pending-fork lookup**. `preserve_pending` records
an already issued parent fork reservation only; it cannot mark ready. The current
pending fork is not recreated by this implementation. Future supported provider
mapping requires its own real tool contract, not a guessed ID or private lookup.

The source API lacks create/send idempotency keys. Claim-before-call ensures no
second automatic invocation after uncertainty, at the cost of possible waiting
without progress after a crash. It cannot guarantee both exactly-once progress
and automatic recovery of a lost response. No cancellation or external writer
fencing is invented. Unknown reservations never expire into another writer.

Result attribution requires an exact instruction-bearing user turn with an
actual turn ID from `read_thread`. A truncated/absent message remains
`startup_or_result_not_attributable`; a self-reported completion or wait wake
cannot substitute. Live acceptance must check the actual tool response shape;
unsupported/changed shapes stay blocked. Source validation is separate:
`accept_completion(operationId, childAuthorization)` reopens existing private
host claim/request/report/publication records, checks authorization and dispatch
instruction binding, validates the host report and requires completed publication.
Only then may dependent operations proceed. A terminal App message alone cannot
satisfy dependencies. Neither this API nor task completion performs release.

## Reservations and bounded work

The serial default is one usable slot. An approved profile may specify capacity
up to eight and coordinator/scoped-review reservations; worker capacity is the
remainder. Reserve review capacity only for applicable scoped-risk work under the
current policy. One active Issue writer, distinct verified checkout, dependencies
and explicit resource keys constrain dispatch. Resource keys are shared across
repositories (for example a test device or port); do not omit known shared assets.
Vault and publication remain separately serialized by their existing owners.

Journal mutations and bounded local checkout readback hold the existing lock. Tool calls and waits occur
outside it; do not change the original P0 driver's lock/concurrency constants.
Profile drift on restart is rejected. Queue reasons persist across restart; failed
attributed child turns become blocked with their reservations retained. Provider/quota unavailability queues an
unissued plan without model substitution; `resume_queue` needs a fresh host
availability decision. It cannot restart an already issued/uncertain creation.
There is no historical cumulative five-review stop. Each adapter step admits one
call; the host controls polling cadence. Up to 128 operation records and a bounded
256-receipt tail per operation retain state, total count and receipt-chain digest;
older raw App text is not copied into this journal. Capacity limits queue work;
an exhausted journal requires an explicit new recorded scope, never deletion of
existing children or records.

## Verification and limits

`python3.11 organization/runtime/workflows/tests/test_app_execution.py` runs real
temporary Git fixtures, synthetic structured tool responses and the pure JS
adapter under Node. These prove reservation/identity/transition behavior only.
They create no visible App tasks. Node is required for this suite; CI and the
host must provide it rather than count a skipped JS check as success.

The controlled live acceptance still needs actual App creation, mapping of ready
IDs, read-only startup, verified checkout, dispatch and attributed result
collection. Five distinct fixture children are not five verified visible App
children. Parent-host mediation, actual tool outputs and source evidence must be
recorded separately before closing #146/#147.
