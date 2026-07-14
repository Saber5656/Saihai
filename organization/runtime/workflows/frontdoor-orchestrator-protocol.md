# Frontdoor Orchestrator Protocol

This document fixes how an Agent UI can control the orchestrator
deterministically.

The key rule is that the UI and the LLM are not the authority for side effects.
The host-owned frontdoor and harness are the authority. LLMs, including Claude,
may propose typed data or produce bounded reports, but they do not select the
workflow, approve activation, create runs, mutate state transitions, or execute
side effects.

## Status

| Area | Status |
|---|---|
| P0 supported workflow | `single_step_external_review` |
| Supported permission mode | `readonly` only |
| Provider execution | Modeled as adapter; live runner is a later phase |
| Claude role | Bounded provider adapter / reviewer, not controller |
| Main-agent role | Output confirmation bridge only |
| Human UI role | Structured approval surface and state renderer |
| Orchestrator authority | Host-owned selector, activation, run, work-order, principal, audit, and gate logic |

## Components

| Component | Authority | May Use LLM | Writes Durable State |
|---|---|---:|---:|
| Main-agent bridge | Submits typed request, reads projection, acks output | No | Request and ack only |
| Action gateway | Executes validated side-effect actions such as child thread creation | No | Action evidence only |
| Human UI | Shows orchestrator-owned approval challenge and captures human approval | No | No |
| Frontdoor service | Validates typed input, calls selector, creates activation envelope | Optional bounded classifier step only | Yes |
| Deterministic selector | Maps typed classification to workflow decision | No | No |
| Harness engine | Verifies principal, creates workflow run and work orders, owns transitions | No | Yes |
| Provider adapter | Executes one bounded work order | Yes | Report/evidence only |
| Evidence gate | Validates typed report and provider evidence | No | Yes |
| Audit log | Records every transition/rejection/replay with principal | No | Append-only JSONL |

## Deterministic Flow

1. Main-agent bridge submits `submit_request` with request kind, prompt, refs,
   and idempotency key.
2. Bridge rejects classification, workflow, approval, run, adapter, report, and
   workflow-definition fields.
3. Orchestrator stores the request and returns a redacted projection. The main
   agent can render it or ack it only.
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

## Required Invariants

| Invariant | Enforcement Point |
|---|---|
| Prompt cannot start orchestration | `frontdoor_prompt` maps to `proposed` / `keep_draft` |
| Main-agent bridge cannot classify | `main-agent-bridge-request` forbids `classification` and unknown authority fields |
| Approval requires human action | Approved sources are only `orchestrator-start`, `human_ui`, `manual_cli` |
| Approval is challenge-based | `human_action_id` is derived from proposal/request/ref digests and rate-limited |
| Selection is deterministic | Selector consumes typed classification, not raw prompt text |
| Classification provenance is mandatory | Source, confidence, evidence, threshold, and tie-break are recorded |
| UI cannot choose workflow by itself | Frontdoor must call selector after validating classification |
| Workflow definitions are deploy-owned | Bridge and ordinary execution principals cannot edit workflow templates, gates, steps, or `max_steps` |
| Bridge projection is redacted | Main-agent reads do not expose raw prompt, internal absolute paths, credentials, work-order paths, report paths, or provider sessions |
| `ack_output` is inert | Ack writes an acknowledgement/audit event only and has `transition_effect = none` |
| Child chat spawning is gateway-owned | Main-agent can see redacted child-thread summaries only; `child-thread-create` requires the `action_gateway_executor` principal |
| Idempotency is required | Bridge submit uses idempotency key + request digest; conflicting replays are rejected |
| Execution principal is verified | Execution-class transitions require a signed non-bridge principal |
| No unbounded context sharing | `raw_transcript_sharing = forbidden` |
| P0 has no edit side effects | `allowed_ops.edit = false` |
| P0 has no commit/push side effects | `allowed_ops.commit = false`, `allowed_ops.push = false` |
| P0 has no provider network side effect from activation scope | `allowed_ops.network = false` |
| Runner cannot expand scope | Work order must copy activation scope and template constraints |
| Provider result is not authoritative by itself | Evidence gate validates typed report and normalized evidence |
| Harness owns transitions | Provider adapter has `runner_authority = write_report_only` |
| Audit records authority | Request, approval, execution, replay, ack, and rejection events include principal identity |
| Principal is channel-bound | HTTP bridge/operator/human/harness APIs derive principal from authenticated channel headers, not request body fields |
| Refs stay inside boundary | Context refs resolve under the repository root, reject symlink escape and denylisted secret/key paths, and enforce count/size caps |

## Host API Shape

The main-agent UI should call only the bridge APIs. Bridge/operator/human/harness
APIs derive principal from authenticated channel headers. Requester fields such
as `frontdoor` and `chat_session_id` are retained as metadata, not authority.
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
summaries and never expose raw worktree paths, repo roots, arbitrary prompts,
shell commands, git commands, or raw Codex App `create_thread` / `fork_thread`
tools.

Raw request/run HTTP reads are not exposed to principal-less main-agent reads.
The bridge projection is the supported main-agent read surface.

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

## Minimal P0 Implementation Plan

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

Readonly external review remains the released P0 contract. The scoped worker
executor implementation is feature-gated and experimental: it is not a v0.1
product-scope claim until Issue #81 review/QA completes and a human approves the
corresponding #53 release-scope decision. Publication, policy-change,
security-sensitive execution, subpath grants, and provider/network tool access
remain waiting-human or planned behavior.

For edit-capable deterministic control, the next required contract is a
host-owned action gateway that withholds write, shell, commit, push, network,
and provider-dispatch tools from the LLM unless an approved work order grants
that exact operation.

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

The capability binds task, run, work-order digest, executor principal, fixed
Codex CLI backend, repository revision, derived task worktree/branch, closed
operations, path scope, network/provider policy, prompt-artifact digest,
expiry, nonce, and maximum execution count. Host HMAC verification, canonical
state comparison, and atomic nonce consumption happen before worktree creation
or process launch. Tamper, replay, expiry, cross-binding, path/symlink escape,
principal/backend mismatch, and ungranted provider/network use fail closed.

Initial v1 supports only the whole task worktree. Finer subpath grants are not
treated as enforced until an OS-level mechanism can guarantee them. Commit,
push, PR, worker-tool network, and arbitrary provider dispatch remain outside
the capability. Codex CLI model transport is fixed by the host backend and does
not grant network/provider tools to the worker. Main-agent projections expose
only execution/result/evidence digests and status; canonical capability,
instruction, worktree path, raw result, and evidence path stay redacted.
