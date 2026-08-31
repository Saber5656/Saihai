# Frontdoor Orchestrator Protocol

This document fixes how an Agent UI can control the orchestrator
deterministically.

The key rule is that the UI and the LLM are not the authority for side effects.
The host-owned frontdoor and harness are the authority. LLMs, including Claude,
may propose typed data or produce bounded reports, but they do not select the
workflow, approve activation, create runs, or mutate state transitions. A
bounded worker may carry out only a host-authorized capability after every
runtime gate verifies; the current same-rootfs `managed_worker` gate remains
suppressed.

## Status

| Area | Status |
|---|---|
| Baseline P0 workflow | `single_step_external_review` remains the primary end-to-end readonly path; the other typed templates are also active contracts. |
| Provider execution | The deterministic fake provider and pinned live Claude/Codex `headless_cli` runners are implemented; live use requires the explicit environment gate and host-owned bindings. |
| Claude role | Bounded provider adapter / reviewer, not controller |
| Main-agent protocol | Submit typed request, read redacted projection, and acknowledge output only. This protocol alone is not an active assurance claim. |
| Surface registration | Frontend kinds are loaded from `profiles/frontdoor-surface-registry.json`; see `frontdoor-surface-contract.md` for the ladder and extension procedure. Unknown kinds fail closed. |
| Codex frontend claim | Only a release-pinned Codex CLI 0.144.1 process started by the root-owned Saihai launcher targets `action_enforced`; it remains suppressed until current administrator-owned evidence verifies. Codex App/IDE and `ingress_enforced` are not claimed. |
| Scoped worker | Capability/executor contracts and commissioning scaffolding are implemented, but live `managed_worker` is suppressed. Same-rootfs Codex 0.144.1 external-mutation/git/credential facts are non-promotable; an isolated worker domain with stronger evidence is required, and v0.1.0 ships no automatic cross-domain transport to it. |
| Human UI role | Structured approval surface and state renderer |
| Orchestrator authority | Host-owned selector, activation, run, work-order, principal, audit, and gate logic |

## Components

| Component | Authority | May Use LLM | Writes Durable State |
|---|---|---:|---:|
| Main-agent bridge | Submits typed request, reads projection, acks output | No | Request and ack only |
| Action gateway | Executes validated child-thread actions or conditionally derives/executes a bounded worker capability after both claims verify; the current worker claim is suppressed | No | Capability, action, and worker evidence only |
| Human UI | Shows orchestrator-owned approval challenge and captures human approval | No | No |
| Frontdoor service | Validates typed input, calls selector, creates activation envelope | Optional bounded classifier step only | Yes |
| Deterministic selector | Maps typed classification to workflow decision | No | No |
| Harness engine | Verifies principal, creates workflow run and work orders, owns transitions | No | Yes |
| Provider adapter | Executes one bounded work order | Yes | Report/evidence only |
| Evidence gate | Validates typed report and provider evidence | No | Yes |
| Audit log | Records every transition/rejection/replay with principal | No | Append-only JSONL |

## Deterministic Flow

1. Main-agent bridge submits `submit_request` with its host-pinned registered
   frontend kind, request kind, prompt, refs, and idempotency key. The host
   derives the surface assurance state; the client cannot claim it.
2. Bridge rejects classification, workflow, approval, run, adapter, report, and
   workflow-definition fields.
3. Orchestrator stores the request and returns a redacted projection. The main
   agent can render it or ack it only. The projection includes the request's
   `idempotency_key_digest` for deterministic correlation, never the raw key.
4. A non-bridge principal supplies or confirms a `typed-classification`
   candidate with source, confidence, and evidence.
5. Frontdoor validates the candidate against
   `schemas/typed-classification.schema.json`.
6. Frontdoor calls `workflow_selector.py select`.
7. For ordinary prompts, frontdoor calls `activation-envelope` with
   `activation_source = frontdoor_prompt`.
8. `frontdoor_prompt` can only produce `activation_status = proposed`.
9. Human UI renders the orchestrator-owned approval summary, challenge id,
   planned workflow, permission mode, resolved refs, refs digest, denied
   operations, and rate limit state. It does not render main-agent prose as
   authority.
10. Human clicks the explicit start control.
11. Frontdoor verifies the challenge id and recomputes the activation envelope with
   `activation_source = human_ui`.
12. Only an approved envelope with bounded refs can create a workflow run.
13. Harness verifies a signed non-bridge principal before execution-class
    transitions.
14. Harness creates the workflow run and one work order from the selected
    template.
15. Provider adapter receives only the work order and bounded context refs.
16. Provider writes a typed report and normalized evidence.
17. Evidence gate validates schemas and evidence paths.
18. Harness applies the transition. Provider output never transitions the run
    directly.

For an edit-capable scoped-worker step, steps 14 through 18 use an additional
action gateway boundary. Capability derivation reloads the frozen work order,
verifies the request's host-bound frontend principal and checkout, and requires
both the frontend `action_enforced` claim and the worker `managed_worker`
claim. Execution reloads the canonical capability and re-requires both claims.
The generation, profile-subject, configuration/runtime/tool-inventory, and
evidence-set digests must still match. Frontend checkout identity is rechecked;
the runtime-global worker uses its sentinel assurance digest while the
capability separately binds the exact work-order repository/worktree. Any
missing, stale, or drifted binding suppresses execution before nonce consumption
or worker launch.

Those endpoints are a local host boundary, not cross-domain delivery. The
machine-wide Codex requirements make the frontend domain read-only, so a
write-capable worker must run in a separately governed domain. This release
does not automatically transport the approved work order or capability between
the two domains.

## Required Invariants

| Invariant | Enforcement Point |
|---|---|
| Prompt cannot start orchestration | `frontdoor_prompt` maps to `proposed` / `keep_draft` |
| Surface must be registered | Ingress rejects unknown frontend kinds before request creation; stored requests and projections carry the host-derived `surface_identity` |
| Main-agent bridge cannot classify | `main-agent-bridge-request` forbids `classification` and unknown authority fields |
| Approval requires human action | Approved sources are only `orchestrator-start`, `human_ui`, `manual_cli` |
| Approval is challenge-based | `human_action_id` is derived from proposal/request/ref digests and rate-limited |
| Selection is deterministic | Selector consumes typed classification, not raw prompt text |
| Classification provenance is mandatory | Source, confidence, evidence, threshold, and tie-break are recorded |
| UI cannot choose workflow by itself | Frontdoor must call selector after validating classification |
| Workflow definitions are deploy-owned | Bridge and ordinary execution principals cannot edit workflow templates, gates, steps, or `max_steps` |
| Bridge projection is redacted | Main-agent reads do not expose raw prompt, internal absolute paths, credentials, work-order paths, report paths, or provider sessions |
| Summary projection is request-bound | Child-thread and worker summaries appear only when request id, task id, owner-principal digest, and checkout digest all match; missing or legacy-unbound records are hidden |
| `ack_output` is inert | Ack writes an acknowledgement/audit event only and has `transition_effect = none` |
| Child chat spawning is gateway-owned | Main-agent can see redacted child-thread summaries only; `child-thread-create` requires the `action_gateway_executor` principal |
| Idempotency is required | Bridge submit uses idempotency key + request digest; conflicting replays are rejected. Redacted reads and routing acceptance use only the digest |
| Execution principal is verified | Execution-class transitions require a signed non-bridge principal |
| No unbounded context sharing | `raw_transcript_sharing = forbidden` |
| Baseline readonly-provider P0 has no edit side effects | `single_step_external_review` sets `allowed_ops.edit = false`; separately approved scoped-worker workflows use their own capability boundary. |
| Baseline P0 has no commit/push side effects | `allowed_ops.commit = false`, `allowed_ops.push = false` |
| Baseline P0 has no provider network side effect from activation scope | `allowed_ops.network = false` |
| Runner cannot expand scope | Work order must copy activation scope and template constraints |
| Provider result is not authoritative by itself | Evidence gate validates typed report and normalized evidence |
| Harness owns transitions | Provider adapter has `runner_authority = write_report_only` |
| Audit records authority | Request, approval, execution, replay, ack, and rejection events include principal identity |
| Principal is channel-bound | HTTP bridge/operator/human/harness APIs derive principal from authenticated channel headers, not request body fields |
| Refs stay inside boundary | Context refs resolve under the repository root, reject symlink escape and denylisted secret/key paths, and enforce count/size caps |
| Protocol is not assurance | A bridge endpoint or executor implementation does not activate `action_enforced` or `managed_worker`; only current administrator-owned evidence can satisfy the runtime gate |
| Frontend and worker claims are independent | Scoped-worker derive and execute each require both `action_enforced` and `managed_worker`; failure of either claim blocks the operation |
| Capability binds assurance | The signed capability binds generation, profile-subject, configuration/runtime/tool-inventory, and evidence-set digests plus the exact per-execution repository/worktree identity; execution rejects drift. |

## Host API Shape

The main-agent UI should call only the bridge APIs. Bridge/operator/human/harness
APIs derive principal from authenticated channel headers. Requester fields such
as `frontdoor` and `chat_session_id` are retained as metadata, not authority.
The host pins `frontdoor` to a registered surface. The static descriptor
binding in `surface_identity` is immutable request metadata, while its effective
assurance state is re-evaluated for each projection; neither is authority.
The current P0 implementation exposes both a JSON CLI
(`scripts/configure_organization.py workflow-frontdoor ...`) and a local HTTP
wrapper (`scripts/configure_organization.py workflow-frontdoor-server ...`).
Both call the same host-owned frontdoor/harness functions.

```text
POST /main-agent/submit-request
  input: X-Orchestrator-Channel=bridge, task_id, request_id, request_kind, prompt, refs, idempotency_key
  forbidden: classification, activation, workflow_selection, run_id, report_path, work_order, adapter_request, principal_type
  output: redacted orchestrator projection

GET /main-agent/projections/{request_id}
  input: X-Orchestrator-Channel=bridge
  output: redacted typed projection for main-agent rendering

POST /main-agent/ack-output
  input: X-Orchestrator-Channel=bridge, request_id, projection_digest
  output: acknowledgement with transition_effect = none

POST /action-gateway/child-thread-create
  input: X-Orchestrator-Channel=action_gateway, child-thread-plan, child-thread-create result
  forbidden: raw prompt text, shell_command, git_command, request-body principal fields
  output: durable child-thread action evidence and redacted summary

POST /action-gateway/scoped-worker-capabilities
  input: X-Orchestrator-Channel=action_gateway, run_id, step_id
  forbidden: raw prompt, command, path, branch, worktree, backend, principal, or assurance overrides
  output: redacted capability id/digest, assurance-binding digest, backend id, and expiry

POST /action-gateway/scoped-worker-execute
  input: X-Orchestrator-Channel=action_gateway, capability_id
  forbidden: raw command, environment, path, backend, principal, or assurance overrides
  output: redacted execution summary, result/evidence digests, and next-gate state

POST /frontdoor/propose
  input: X-Orchestrator-Channel=operator, user_prompt, selected_context_refs, typed_classification
  output: proposed activation envelope or blocked/waiting_human reason

POST /frontdoor/approve
  input: X-Orchestrator-Channel=human_ui, request_id, human_action_id
  output: approved activation envelope or blocked reason

POST /orchestrator/runs
  input: X-Orchestrator-Channel=operator, approved activation envelope
  output: workflow_run

POST /orchestrator/runs/{run_id}/drain
  input: X-Orchestrator-Channel=operator, run_id
  output: updated workflow_run and generated work_orders

POST /provider/claude/prepare
  input: X-Orchestrator-Channel=operator, run_id
  output: bounded adapter request, prompt, report path, and evidence paths

POST /provider/reports/validate
  input: X-Orchestrator-Channel=harness, run_id, optional report_path
  output: validated workflow_run terminal state or blocked reason
```

| API Shape | Current CLI | Current HTTP |
|---|---|---|
| `POST /main-agent/submit-request` | `workflow-frontdoor bridge-submit-request` | Implemented |
| `GET /main-agent/projections/{request_id}` | `workflow-frontdoor bridge-read-projection` | Implemented |
| `POST /main-agent/ack-output` | `workflow-frontdoor bridge-ack-output` | Implemented |
| `POST /action-gateway/child-thread-create` | `workflow-frontdoor child-thread-create` | Implemented |
| `POST /action-gateway/scoped-worker-capabilities` | No CLI subcommand; credential-bound action-gateway HTTP path | Implemented; fails closed unless both assurance claims verify |
| `POST /action-gateway/scoped-worker-execute` | No CLI subcommand; credential-bound action-gateway HTTP path | Implemented; live backend also requires its explicit host gate |
| `POST /frontdoor/propose` | `workflow-frontdoor propose` | Implemented |
| `POST /frontdoor/approve` | `workflow-frontdoor approve` | Implemented |
| `POST /orchestrator/runs` | `workflow-frontdoor create-run` | Implemented |
| `POST /orchestrator/runs/{run_id}/drain` | `workflow-frontdoor drain` | Implemented |
| `POST /provider/claude/prepare` | `workflow-frontdoor prepare-claude-adapter` | Implemented |
| `POST /provider/reports/validate` | `workflow-frontdoor validate-report` | Implemented |
| `GET /` | Output confirmation browser UI | Implemented |

The main-agent bridge must not submit an already approved envelope, typed
classification, run id, report path, or workflow definition as authority. On
approval, the frontdoor reloads the stored request, verifies the
orchestrator-owned challenge, recomputes selection, verifies context refs, and
then stamps `approved_by = human_ui_action`.

Bridge output acknowledgement verifies `projection_digest` against the current
redacted projection before writing an ack record. A mismatch is blocked and
records only an audit event with `ack_verified = false`.

Child-thread spawning is intentionally outside the bridge authority. The
orchestrator may produce or store a deterministic `child-thread-plan`, but only
the `action_gateway` channel can record `child-thread-create` evidence. The
recorded result includes the created/reused thread id or pending worktree id,
branch, worktree digest, instruction artifact ref/digest, executor principal,
and idempotency replay status. Main-agent projections expose only redacted
summaries whose request, task, owner-principal, and checkout bindings exactly
match the current request. Missing, legacy-unbound, or mismatched records stay
hidden. Projections never expose raw worktree paths, repo roots, arbitrary
prompts, shell commands, git commands, or raw Codex App `create_thread` /
`fork_thread` tools.

Raw request/run HTTP reads are not exposed to principal-less main-agent reads.
The bridge projection is the supported main-agent read surface.

This binding prevents accidental cross-request disclosure inside the supported
state contract. A process with arbitrary write access to the private
orchestrator state root could forge the binding itself; that same-uid/host
compromise is outside this contract and must not be described as prevented by
projection filtering.

## Claude Adapter Boundary

Claude is safe in this design when it is called only through a provider adapter
with a work order.

| Allowed | Forbidden |
|---|---|
| Read bounded refs listed in `context_refs` | Receive raw full transcripts |
| Return `external-review-report` JSON | Select workflow |
| Write normalized provider evidence | Approve activation |
| Include evidence refs for findings | Create or mutate workflow run state |
| Report gaps and uncertainties | Execute edits, commits, pushes, or publication |

The adapter may store a provider transcript path as confined evidence under the
orchestrator state root. The transcript path is a signal for audit only; it is
not shared back into the main-agent projection and is not authoritative.

## Baseline P0 Implementation Map

| Step | Deliverable | Existing Contract |
|---|---|---|
| 1 | Main-agent bridge endpoint | `main-agent-bridge-request`, idempotency, redacted projection |
| 2 | Frontdoor proposal endpoint | `typed-classification`, selector, proposed activation |
| 3 | Frontdoor approval endpoint | `human_ui` activation source and challenge digest |
| 4 | Workflow run creator | `workflow-run.schema.json`, signed non-bridge principal |
| 5 | Work order creator | `work-order.schema.json`, work-order signature, unclaimed lease |
| 6 | Invocation-drain harness | registry `p0_scheduler_policy` |
| 7 | Claude headless adapter | provider adapter writes report/evidence only |
| 8 | Evidence gate | `external-review-report.schema.json`, provider evidence path checks |
| 9 | UI output renderer | redacted projection and ack-only confirmation |

Steps 1 through 9 are implemented for P0. The browser UI shell is intentionally
thin: it can call only the main-agent bridge APIs and render returned state. It
does not expose typed-classification editing, approval, run creation, drain,
adapter preparation, report validation, or raw request/run reads.

## P0 Certainty Boundary

Readonly external review remains the baseline P0 contract. PR #81 added the
host-owned scoped-worker capability and executor path: an authenticated action
gateway can derive a single-use capability from an approved, frozen work order
and, only when both assurance claims verify, start the bounded worker. This is
an implemented v0.1 contract, not a claim that the current same-rootfs worker
can pass `managed_worker`, and not a future Issue #81 placeholder.

Implementation does not by itself activate a runtime claim. The Codex frontend
targets only `action_enforced`, while the scoped Codex worker independently
targets `managed_worker`. The current frontend suite can promote
`action_enforced` after its root-observed evidence is sealed into an immutable
active generation. The current same-rootfs worker suite cannot promote
`managed_worker`; only stronger evidence from a separately isolated worker
policy domain could do so. The frontend positive path is exactly one typed
request in `waiting_human`, with no capability, worker, run, provider dispatch,
report, or marker change. Claude has no target claim and remains advisory.
Cursor and Grok retain future
`ingress_enforced`/`action_enforced` targets but remain suppressed
candidate/unavailable integrations.

The current same-rootfs worker is intentionally non-promotable. Codex 0.144.1
cannot prove generic external-mutation, absolute denial of a local
bare-repository `git push`, or credential denial merely from its current
configuration. Its `external_mutation`, `git_commit`, `git_push`, and
`credential_access` evidence has `result=fail` with inconclusive host
observations and does not satisfy the claim. The facts named
`workspace_profile_and_network_disabled_not_same_rootfs_isolation` and
`dedicated_auth_deny_configured_not_mechanically_proven` are explicit
non-claims.
Worker `commission-seal` therefore fails closed with
`worker_denial_facts_not_promotable`; only stronger evidence from a separately
isolated worker policy domain may activate `managed_worker`.

The production deployment is bound by
`/Library/Application Support/Saihai/Manifests/codex-main-agent.deployment.json`
and `/Library/Application Support/Saihai/Config/codex-main-agent.runtime.json`.
The current deployment epoch is separately recorded under
`/Library/Application Support/Saihai/Assurance/epochs/`; activate, rollback,
and uninstall revoke the previous epoch before target mutation. Authority
checks bind the selected generation to that epoch and to the current live
process identity, including parent, executable, argv, profile, and checkout.
The full live-process tuple is sampled before artifact verification and again
immediately before the claim returns; both samples must match the stored and
typed context bindings. This reduces, but does not claim to eliminate, the
minimal scheduling interval after the final sample.
Private commissioning records live under
`/Library/Application Support/Saihai/Assurance/commissioning/`. Immutable
generation evidence and attestations live under
`/Library/Application Support/Saihai/Assurance/generations/`; the selected
generation is named by `/Library/Application Support/Saihai/Assurance/active/`.
Standard and commissioning launches are separately recorded under
`/Library/Application Support/Saihai/Assurance/launch-sessions/` and
`/Library/Application Support/Saihai/Assurance/commissioning-launches/`.
Public assurance directories/files use exact `0755`/`0644`, private
commissioning uses exact `0700`/`0600`, and lock files use `0600`; drift
suppresses the claim. Model prose and static configuration are not claim
authority.

The frontend `credential_access = denied` fact covers only the two known Codex
auth paths, its dedicated `CODEX_HOME`, and absence of credential-capable tool
classes in the fixed inventory. It is not a claim that arbitrary user-readable
secret-bearing files are inaccessible and cannot promote the worker's generic
credential-denial fact.

The shipped executor grants neither network/provider tools nor publication
authority.  Commit, push, pull-request creation, release publication,
policy-change execution, security-sensitive execution, and mechanically
enforced subpath grants remain separate gates or unsupported.  The frontend
main agent still receives no action-gateway credential and cannot derive or
execute a capability itself.

The worker generation is runtime-global; its assurance `checkout_digest` is the
sentinel for `checkout_binding=capability_per_execution` and
`repository_scope=host_verified_work_order`, not a particular checkout.
Capability derivation and pre-execution checks separately bind and revalidate
the actual work-order repository/worktree. Same-rootfs hardening and probe
records are defense in depth only and do not replace isolated-domain evidence.

The first narrow action gateway path is `child-thread-create`. It exists only
for issue-scoped child worktree chat spawning and does not give the main-agent
bridge general implementation, worktree, shell, git, or thread-control
authority.

### Scoped worker executor

Edit-capable work is not performed by the main-agent bridge. A host-owned
executor may derive a `scoped-worker-capability` only from the canonical frozen
work order and authenticated action-gateway channel. The derive and execute
requests accept identifiers only; they do not accept raw commands, prompts,
paths, branches, worktree locations, network/provider selections, or principal
claims.

The capability binds task, request, run, work-order digest, executor principal,
fixed Codex CLI backend, repository revision, derived task worktree/branch,
closed operations, path scope, network/provider policy, prompt-artifact digest,
expiry, nonce, maximum execution count, and independent frontend/worker
assurance bindings. Derive and execute both revalidate `action_enforced` and
`managed_worker`; execute rejects any attestation, subject, binding,
evidence-set, or checkout drift. Host HMAC verification, canonical state
comparison, and atomic nonce consumption happen before worktree creation or
process launch. Tamper, replay, expiry, cross-binding, path/symlink escape,
principal/backend mismatch, assurance drift, and ungranted provider/network use
fail closed.

The action gateway does not make this an automatically connected cross-domain
system. A deployment must provide the separately governed worker domain and a
separately approved transport/integration boundary; no such automatic transport
is shipped in v0.1.0.

Initial v1 supports only the whole task worktree. Finer subpath grants are not
treated as enforced until an OS-level mechanism can guarantee them. Commit,
push, PR, worker-tool network, and arbitrary provider dispatch remain outside
the capability. Codex CLI model transport is fixed by the host backend and does
not grant network/provider tools to the worker. Main-agent projections expose
only execution/result/evidence digests and status; canonical capability,
instruction, worktree path, raw result, and evidence path stay redacted.
