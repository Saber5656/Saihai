# Orchestrator Workflow Contracts

This directory is the contract and local harness surface for the typed agent
orchestrator. It defines process data and the bounded invocation-drain runner.
It does not manage provider credentials, run tmux workers, or start a daemon.

## Scope

| File | Purpose |
|---|---|
| `registry.yaml` | Active workflow registry, common gate profiles, safety classes, deterministic selector policy, scheduler policy |
| `templates/single_step_external_review.yaml` | Single-step readonly external review workflow |
| `templates/research_only.yaml` | No-diff research/design/source-review workflow |
| `templates/standard_code_change.yaml` | Bounded code-change workflow without publication |
| `templates/publication_required.yaml` | Code-change workflow with explicit publication gate |
| `templates/policy_or_permission_change.yaml` | Policy, permission, hook, or governance-impacting workflow |
| `templates/security_sensitive_change.yaml` | Security-sensitive workflow with required security review and optional publication gate |
| `schemas/typed-classification.schema.json` | Required fields an LLM or human may propose before deterministic selection |
| `schemas/activation-envelope.schema.json` | Gate envelope for draft/proposed/approved/blocked activation state |
| `schemas/workflow-run.schema.json` | Durable per-task workflow run state contract |
| `schemas/work-order.schema.json` | Bounded work order contract for one workflow step |
| `schemas/main-agent-bridge-request.schema.json` | Restricted submit request accepted from the main-agent confirmation surface |
| `schemas/orchestrator-projection.schema.json` | Redacted output projection safe for main-agent rendering |
| `schemas/audit-event.schema.json` | Append-only principal/provenance event contract |
| `schemas/provider-adapter-capability.schema.json` | Adapter capability descriptor, including future `tmux_interactive` transport |
| `schemas/external-review-report.schema.json` | Authoritative typed report for external review |
| `schemas/research-report.schema.json` | Authoritative typed report for research-only work |
| `schemas/code-change-report.schema.json` | Authoritative typed report for code-change work |
| `schemas/publication-result.schema.json` | Publication gate result schema |
| `schemas/policy-change-report.schema.json` | Policy or permission change evidence schema |
| `schemas/security-review-report.schema.json` | Security-sensitive review evidence schema |
| `scripts/workflow_selector.py` | Deterministic selector and activation-envelope helper |
| `scripts/frontdoor_orchestrator.py` | Host-owned frontdoor and invocation-drain P0 harness |
| `scripts/provider_runner.py` | Headless provider adapter runner that writes typed reports and normalized evidence |
| `scripts/frontdoor_server.py` | Local HTTP wrapper for Agent UI integration |
| `scripts/task_state_bridge.py` | Derived task/session run views and session-local orchestrator run index writer |
| `tests/test_workflow_selector.py` | Unit/static contract tests |
| `tests/test_task_state_bridge.py` | Task/session bridge and queue-shaped derived view regression tests |
| `frontdoor-orchestrator-protocol.md` | Implementation boundary for Agent UI, host frontdoor, harness, and Claude adapter control |
| `operator-runbook.md` | Day-1 operator workflow, legacy queue/tmux migration notes, stuck-run recovery, rollback, artifact, and validation guidance |

The `.yaml` files in this directory are JSON-compatible by design, matching the
existing runtime config convention in `organization/runtime/infra-team-bootstrap`.

## Runtime Principles

| Principle | P0 Contract |
|---|---|
| Prompt does not start orchestration | `frontdoor_prompt` activation can only produce `proposed` state. |
| Explicit activation only | `orchestrator-start`, `human_ui`, or `manual_cli` can approve a selected bounded workflow. |
| Workflow selection is deterministic | The selector consumes typed classification; it does not read free-form prompt text. |
| Classification has provenance | A classification must include source, confidence, and evidence. `frontdoor_llm_proposal` is not an authority source. |
| Main agent is a confirmation bridge | The main-agent bridge can submit a typed request, read a redacted projection, and ack output only. |
| Execution requires a signed non-bridge principal | Run creation, drain, resume, abort, adapter preparation, report validation, and workflow-definition changes reject `main_agent_bridge`. |
| Audit is append-only | Frontdoor, bridge, approval, execution, replay, and rejection decisions write principal-scoped audit events. |
| Agent output is not authoritative | `typed_report_file` and normalized evidence are canonical. stdout, tmux pane output, and provider transcript are signals only. |
| Context sharing is typed | Shared run state is durable typed state; step snapshots are immutable; provider transcripts remain confined evidence paths. |
| Common gates are centralized | `registry.yaml` defines reusable `entry.*` and `exit.*` gate profiles; templates reference them by id. |
| Safety class cannot downgrade | Selector validation rejects routing `policy` or `security` classifications into weaker templates. |
| Publication is a separate axis | Publication is represented by `publication_gate` and `exit.publication_result_recorded`, not by duplicating every workflow. |
| Scheduler is bounded | Policy is invocation-drain, durable state, global advisory lock, concurrency 1. |
| Provider is an adapter | `headless_cli` is the default transport. `tmux_interactive` is modeled but not implemented. |

## Provider Runner

`scripts/provider_runner.py` consumes a validated work order and a provider
adapter descriptor from `registry.yaml`. The runner dispatches through adapter
metadata rather than hard-coded provider names, writes a normalized evidence
artifact under `provider-evidence/`, writes a typed report under `reports/`,
and then hands the report to the report gate.

The active descriptor set covers these provider targets:

| Adapter | Provider target | Bridge pattern |
|---|---|---|
| `claude_headless_p0` | `claude_headless` | `none` |
| `codex_cli_openai_p0` | `codex_cli_openai` | `none` |
| `hermes_agent_oneshot_p0` | `hermes_agent` | `oneshot` |
| `cursor_cli_p0` | `cursor_cli` | `none` |
| `grok_build_cli_candidate_p0` | `grok_build_cli` | `none` |

Grok routing remains data-driven: the registry lists Hermes Agent, Grok Build
CLI, and CursorCLI as candidates, and selecting one does not require runner
core changes. Hermes Agent is represented as a one-shot bridge provider; async
callback or polling support is not claimed unless a separate runtime adds it.

Failure modes are typed. Provider unavailable and timeout move the run to
`waiting_human`; malformed output and non-zero exit move it to `failed`.
stdout and transcript payloads are stored only as signal artifacts or digests,
never copied into shared run state.

## Run Store Layout

Default state root: `~/.codex/state/itb/frontdoor-orchestrator`

```text
<state_root>/                                  default: ~/.codex/state/itb/frontdoor-orchestrator
  runs/
    <run_id>.json                              canonical workflow-run record (schema-validated)
    <run_id>.error.json                        latest typed store/load error artifact (overwritten)
    <run_id>.corrupt-<n>.json                  quarantined unreadable payload, n = 1,2,...
    .<name>.<hex>.tmp                          in-flight temp files; readers MUST ignore them
```

| Path | Purpose |
|---|---|
| `runs/<run_id>.json` | Canonical workflow-run record. It is validated before store/load acceptance and written with atomic replace semantics. |
| `runs/<run_id>.error.json` | Latest typed store/load error artifact for that run. It is overwritten atomically. |
| `runs/<run_id>.corrupt-<n>.json` | Quarantined unreadable canonical payload. The lowest available `n >= 1` is used. |
| `runs/.<name>.<hex>.tmp` | In-flight temp file for atomic writes. Readers must ignore these files. |

Naming rules:

- `run_id` must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$` and must not contain path separators or traversal segments.
- Linkage is embedded in the run record through `task_id` and `request_id`.
- The request record lives at `requests/<request_id>.json`.
- No extra canonical run index files are maintained by this workflow-run store.

## Canonical State vs Compatibility Views

| Artifact | Status | Owner |
|---|---|---|
| `<orch_root>/runs/<run_id>.json` | **canonical** run state | orchestrator (#20) |
| `<orch_root>/transitions/<run_id>/*` | **canonical** transition evidence | report gate (#11) |
| `<orch_root>/provider-evidence/...` | **canonical** provider evidence | runner (#42) |
| `<session_dir>/orchestrator-runs.json` | **view/index** (rebuildable) | this issue |
| `task-view` CLI / HTTP output | **derived view** (never stored) | this issue |
| `queue/inbox|tasks|reports` | canonical for role-queue work — orchestrator NEVER writes here | ITB |

Role-queue files are owned by `agent-call`/ITB workers. The orchestrator exposes
queue-shaped evidence rows for viewer/task-detail consumption, but it does not
write synthetic rows into `queue/inbox`, `queue/tasks`, or `queue/reports`.

The session-local `orchestrator-runs.json` file is a pointer/index for viewers
that enumerate ITB session directories. It is rebuilt from canonical
orchestrator runs whenever a linked run is created, drained, replayed, or
terminally validated. If no matching ITB session directory exists, the
orchestrator silently skips the index write and leaves the run transition
unchanged.

## Workflow Run Lifecycle

`scripts/run_lifecycle.py` is the single source of truth for host-owned
workflow-run state transitions. Each accepted transition appends a normalized
record to `workflow_run.transitions` with `seq`, `from_state`, `to_state`,
`reason_class`, `occurred_at`, principal, signature, and artifact references.
Terminal runs are immutable.

| From state | Allowed next states | Goal state mapping |
|---|---|---|
| `created` | `step_queued`, `waiting_human`, `aborted` | `approved` |
| `step_queued` | `waiting_provider`, `waiting_human`, `aborted` | `active` |
| `waiting_provider` | `step_queued`, `validating`, `waiting_human`, `failed`, `aborted` | `active` |
| `validating` | `complete`, `failed`, `waiting_human`, `aborted` | `active` |
| `waiting_human` | `step_queued`, `failed`, `aborted` | `blocked` |
| `remediating` | `step_queued`, `failed`, `aborted` | `active` |
| `complete` | none | `complete` |
| `failed` | none | `blocked` |
| `aborted` | none | `aborted` |

`resume` reuses durable run state and never creates a duplicate run. It returns
the next operator action for `created`, `step_queued`, and `validating`; it can
requeue `waiting_human` only with `--requeue`; and it reclaims an expired or
missing provider lease by resetting the work-order runner claim and moving the
run back to `step_queued`. `abort` moves any non-terminal run to terminal
`aborted`; terminal aborts replay without mutation.

## Report Gate

`scripts/report_gate.py` owns typed external-review report validation and the
state transition from provider signal to canonical workflow result. The
frontdoor `validate-report` command is a thin wrapper over this gate.

| Outcome | Run transition | Canonical effect |
|---|---|---|
| `report_valid` | `validating -> complete` | `pass` or valid `findings` becomes the terminal workflow result. |
| `report_invalid` | `validating -> failed` | Schema/evidence failures block completion with terminal `blocked / invalid_report`. |
| `scope_violation` | `validating -> waiting_human` | Raw transcript leakage, evidence path escape, or cross-run identity mismatch requires human review. |
| `provider_reported_blocked` | `validating -> waiting_human` | A schema-valid provider `blocked` result is treated as a human decision point, not terminal failure. |

Every report-gate evaluation writes a normalized transition artifact:

```text
<state_root>/transitions/<run_id>/<seq>-report-gate.json
```

Invalid or scope-violating reports are preserved at the submitted report path
and get an additional rejection artifact:

```text
<state_root>/reports/<run_id>/<step_id>-rejection-<n>.json
```

The gate never copies transcript content into shared run state. Provider
evidence and transcript paths remain references under `provider-evidence/`;
stdout and transcript payloads are still signal-only.

## Final Gate And Vault Evidence

`scripts/completion_gate.py` owns the final completion verification contract.
A workflow run may be treated as complete by strict-flow tooling ONLY when
`verify-completion` returns decision `complete`. A terminal run record alone is
insufficient.

`verify-completion` re-checks the durable chain after `validate-report`:

| Check | Blocked reason |
|---|---|
| Run loads and is terminal `complete` / `complete` | `run_unloadable`, `run_not_terminal_complete` |
| Work order and frozen snapshot still match | `missing_work_order`, `work_order_snapshot_mismatch` |
| Typed report exists, revalidates, and matches run identity | `missing_typed_report`, `invalid_typed_report`, `report_identity_mismatch` |
| Provider evidence exists, stays inside state root, and matches run/step | `missing_provider_evidence`, `evidence_path_escape` |
| Optional transcript digest still matches | `digest_mismatch` |
| Report-gate transition artifact confirms `report_valid -> complete` | `missing_transition_artifact` |
| Activation remains explicitly approved from a legal source | `activation_not_approved` |

Snapshot, digest, and transition checks gracefully report `skipped` when that
artifact kind is absent for the run. If the artifact kind exists and does not
match, the gate blocks.

On success, the gate writes a narrow `completion_verification` annotation into
the already terminal run without changing `run_state`; blocked verifications
never annotate. The Vault evidence block is intentionally thin and never copies
raw transcript, prompt, instruction, stdout, or provider output content:

| Gate-IO-Contract requirement | Orchestrator artifact satisfying it |
|---|---|
| Role Execution Evidence (role/result/usage source) | Vault evidence block: terminal status plus provider evidence metadata |
| review evidence for task completion | typed report plus `verify-completion` decision |
| Queue Evidence table row | `task-view` queue-shaped evidence |
| Invocation Evidence (model/request/session ids) | normalized provider evidence fields |
| Vault final update completion condition | `verification_decision == "complete"` plus evidence block in Task Detail |
| finalization-check input | `verify-completion` JSON decision and reasons |

Generate JSON for tooling:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state verify-completion \
  --run-id <run_id>
```

Generate markdown for a Vault Task Detail:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state verify-completion \
  --run-id <run_id> \
  --format markdown
```

## Work Orders And Step Snapshots

`drain` turns an approved run into a bounded work order for the current
template step. The generated work order is validated before any provider
adapter can consume it.

```text
<state_root>/
  work-orders/
    <run_id>/
      <step_id>.json                         canonical work order
      <step_id>-snapshot-<iteration>.json    immutable step inputs
```

The work order contains the deterministic instruction, role assignment,
permission mode, typed context refs, canonical report path, activation scope,
policy digest, requester metadata, and signed issuer authority. P0
`single_step_external_review` work orders are forced to `readonly`, reviewer
assignment, `external_provider_allowed: true`, `step_budget: 1`, and
`edit`/`commit`/`push`/`network` all false.

The snapshot records a stable digest of the work order plus the activation
scope, context refs, and policy digest used for that step attempt. Replaying
`drain` verifies the existing snapshot digest; a mismatch blocks the run as
`work_order_invalid` instead of regenerating mutable provider inputs.

## Active Template Routes

| Classification | Selected workflow | Notes |
|---|---|---|
| `external_review` + `readonly` | `single_step_external_review` | Keeps the existing one-step external review route. |
| `research` | `research_only` | Read-only, no-diff research and decision support. |
| `code_change` without publication | `standard_code_change` | Bounded edits, review, QA, and final evidence. |
| `code_change` with publication or `publication` | `publication_required` | Adds explicit publication result evidence. |
| `policy_change` | `policy_or_permission_change` | Requires policy approval evidence before mutation. |
| `security_sensitive: true` | `security_sensitive_change` | Requires security review; publication gate is required only when publication is requested. |

## Selector CLI

Run contract validation:

```sh
python3 organization/runtime/workflows/scripts/workflow_selector.py validate-contracts
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

Select from typed classification:

```sh
python3 scripts/configure_organization.py workflow-selector select \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

Create a proposed envelope for an ordinary prompt source:

```sh
python3 scripts/configure_organization.py workflow-selector activation-envelope \
  --activation-source frontdoor_prompt \
  --task-id TSK-example \
  --request-id req-example \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

Create an approved envelope only from explicit invocation:

```sh
python3 scripts/configure_organization.py workflow-selector activation-envelope \
  --activation-source orchestrator-start \
  --task-id TSK-example \
  --request-id req-example \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

## Sahai CLI

`scripts/saihai.py` is the operator command surface for the deterministic
frontdoor/workflow split. It keeps frontdoor control separate from workflow-run
control:

| Group | Commands currently backed on main | Boundary |
|---|---|---|
| `frontdoor` | `propose`, `approve`, `status` | Propose and explicitly approve activation artifacts. Never creates workflow runs. |
| `workflow` | `create-run`, `drain`, `validate-report` | Consumes approved activation/run artifacts. Does not accept raw prompt text as authority. |

Commands from the target design whose backing implementations are not yet
merged are intentionally absent from the parser. There are no dead stubs for
`run-step` or `list`. `verify-completion` is available through the
compatibility facade. `resume`, `abort`, `task-view`, and
`lock-status` are currently exposed through the compatibility facade below
until #19 re-exposes takt-style workflow commands.

```sh
python3 scripts/saihai.py frontdoor --state-root /tmp/frontdoor-state propose \
  --task-id TSK-example \
  --request-id req-example \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'

python3 scripts/saihai.py frontdoor --state-root /tmp/frontdoor-state status \
  --request-id req-example

python3 scripts/saihai.py frontdoor --state-root /tmp/frontdoor-state approve \
  --request-id req-example \
  --nonce <approval.human_action_id>

python3 scripts/saihai.py workflow --state-root /tmp/frontdoor-state create-run \
  --request-id req-example

python3 scripts/saihai.py workflow --state-root /tmp/frontdoor-state drain \
  --run-id <run_id>

python3 scripts/saihai.py workflow --state-root /tmp/frontdoor-state validate-report \
  --run-id <run_id>
```

`frontdoor approve` maps `--nonce` to the proposal challenge digest. `frontdoor
status` reads the stored request record without mutating state. Workflow
commands expose only typed artifact identifiers and report paths; they do not
define `--prompt` or `--classification`.

## Compatibility Facade

The host-owned frontdoor/harness remains available through the organization
facade for skills and automation:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state propose \
  --task-id TSK-example \
  --request-id req-example \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state approve \
  --request-id req-example \
  --human-action-id <approval.human_action_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state orchestrator-start-approve \
  --request-id req-example \
  --human-action-id <approval.human_action_id> \
  --invoked-at 2026-07-09T00:00:00+0900 \
  --chat-session-id thread-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state manual-approve \
  --request-id req-example \
  --human-action-id <approval.human_action_id> \
  --confirm approve-req-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state create-run \
  --request-id req-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state drain \
  --run-id <run_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state prepare-claude-adapter \
  --run-id <run_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state run-provider \
  --run-id <run_id> \
  --adapter-id claude_headless_p0 \
  --fake-provider-mode success

The P0 runner uses fake provider mode to validate adapter dispatch and
evidence/report paths. Live `command_argv` adapters are rejected as
`provider_unavailable` until sandbox/snapshot support can enforce
harness-owned readonly constraints.

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state validate-report \
  --run-id <run_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state resume \
  --run-id <run_id> \
  --requeue

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state abort \
  --run-id <run_id> \
  --reason "operator cancelled"

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state task-view \
  --task-id TSK-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state lock-status
```

The facade is compatibility-preserving and delegates to the same
`frontdoor_orchestrator.py` functions as `saihai`. New operator workflows should
prefer `saihai`; existing automation can keep using `workflow-frontdoor`.

The `human_action_id` is a proposal-digest challenge returned by `propose`.
It is not arbitrary UI text. Execution commands accept `--principal-type`,
`--principal-id`, and `--authn-method`; `main_agent_bridge` is rejected for
execution-class transitions.

### Activation Approval Sources

Ordinary `frontdoor_prompt` activation can only create a `proposed` or
`blocked` envelope. A workflow run can be created only after one of the
explicit local approval paths stores an approved activation envelope:

| Source | CLI path | Extra gate | Approved-by value |
|---|---|---|---|
| `human_ui` | `approve` | `human_action_id` challenge | `human_ui_action` |
| `orchestrator-start` | `orchestrator-start-approve` | `human_action_id` challenge plus invocation evidence (`skill`, `invoked_at`, `chat_session_id`) | `human_explicit_skill_invocation` |
| `manual_cli` | `manual-approve` | `human_action_id` challenge plus `--confirm approve-<request_id>` | `manual_operator` |

Approval revalidates bounded context refs before changing request state. If a
resolved ref digest changes after proposal, approval is blocked with
`context_refs_changed_since_proposal`. Destructive work is blocked. Publication
and policy-change work remain `waiting_human` for their separate gates and do
not store `approved_activation`.

Activation envelope snapshots are written under:

```text
<state_root>/envelopes/<request_id>/<seq>-<activation_status>.json
```

`create-run` returns the request record path and the snapshot list. It also
updates the request record's `linked_runs` list without duplicating replayed
run IDs.

## Scheduler Lock And P0 Concurrency

Workflow-run execution uses an invocation-drain scheduler with a per-state-root
global advisory lock:

```text
<state_root>/locks/global-advisory.lock.d/owner.json
```

The lock serializes mutating harness operations (`create-run`, `drain`,
`resume`, `abort`, and `validate-report`). Lock contention returns typed JSON
and does not mutate the run record:

```json
{
  "schema_version": 1,
  "decision": "blocked",
  "reason": "lock_contention",
  "owner": {
    "operation": "drain_run",
    "run_id": "run-example"
  }
}
```

The P0 concurrency guard is separate from the filesystem lock. While holding
the lock, `drain` refuses to advance a run when another run in the same state
root is already in an in-flight state (`waiting_provider` or `validating`),
returning `reason: "concurrency_limit_reached"` with the blocking run IDs.

Operators can inspect the lock without changing state:

```sh
python3 scripts/configure_organization.py workflow-frontdoor \
  --state-root /tmp/frontdoor-state lock-status
```

`lock-status` reports `locked`, the current `owner`, and stale-lock diagnostics.
Stale locks are reclaimable by the next mutating operation only when the lock
directory is older than the stale threshold and the recorded owner process is
missing, unreadable, invalid, or no longer alive. A live owner is never
reclaimed automatically.

## Day-1 Operator Workflow

Use [operator-runbook.md](operator-runbook.md) for the supported day-1 flow:
validate contracts, propose, approve, create run, drain, prepare adapter,
validate report, inspect evidence, and recover or roll back stuck runs with
`resume` / `abort`.

The currently implemented workflow-frontdoor commands are `propose`, `approve`,
`orchestrator-start-approve`, `manual-approve`, `create-run`, `drain`,
`adapter-capability`, `prepare-claude-adapter`, `run-provider`,
`validate-report`, `verify-completion`, `resume`,
`abort`, `task-view`, `bridge-submit-request`, `bridge-read-projection`,
`bridge-ack-output`, `channel-token`, and
`lock-status`. Dedicated raw run detail and evidence inspection commands are
still planned; the runbook uses canonical artifact inspection where needed.

## Main-Agent Bridge CLI

Use this surface when the main agent is acting only as an orchestrator output
confirmation UI:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state bridge-submit-request \
  --task-id TSK-example \
  --request-id req-example \
  --request-kind external_review_request \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --idempotency-key req-example-v1

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state bridge-read-projection \
  --request-id req-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state bridge-ack-output \
  --request-id req-example \
  --projection-digest <projection_digest>
```

The bridge rejects classification, workflow selection, approval, run IDs,
report paths, adapter requests, and workflow-definition data. `ack_output` is a
pure acknowledgement and has `transition_effect = none`. The acknowledgement is
accepted only when `projection_digest` matches the current redacted projection.

Context refs are resolved by the frontdoor before they can be shown in an
approval view or passed to a work order. Refs must point to existing files under
the repository root, cannot escape through symlinks, cannot include `.git`,
`.env*`, credential, secret, token, or key material, and are capped by count and
file/total byte limits. Approval summaries render the resolved repository
relative path, size, and digest.

## Frontdoor HTTP API

The same host-owned operations are exposed as a local JSON API for an Agent UI:

```sh
python3 scripts/configure_organization.py workflow-frontdoor-server \
  --state-root /tmp/frontdoor-state \
  --host 127.0.0.1 \
  --port 8766
```

| Endpoint | Harness Operation |
|---|---|
| `GET /` | Main-agent output confirmation UI |
| `GET /healthz` | Health check |
| `POST /main-agent/submit-request` | Restricted bridge submit; derives principal from authenticated `bridge` channel headers |
| `GET /main-agent/projections/{request_id}` | Redacted typed projection for main-agent rendering; derives principal from authenticated `bridge` channel headers |
| `POST /main-agent/ack-output` | Verified no-op acknowledgement; derives principal from authenticated `bridge` channel headers |
| `POST /frontdoor/propose` | Operator path for `workflow-frontdoor propose`; derives principal from authenticated `operator` channel headers |
| `POST /frontdoor/approve` | Human UI path for `workflow-frontdoor approve`; derives principal from authenticated `human_ui` channel headers and challenge id |
| `POST /orchestrator/runs` | Operator path for `workflow-frontdoor create-run`; derives principal from authenticated `operator` channel headers |
| `POST /orchestrator/runs/{run_id}/drain` | Operator path for `workflow-frontdoor drain`; derives principal from authenticated `operator` channel headers |
| `POST /orchestrator/runs/{run_id}/resume` | Operator path for `workflow-frontdoor resume`; derives principal from authenticated `operator` channel headers; body accepts `{"requeue": true}` |
| `POST /orchestrator/runs/{run_id}/abort` | Operator path for `workflow-frontdoor abort`; derives principal from authenticated `operator` channel headers; body accepts `{"reason": "..."}` |
| `GET /orchestrator/runs/{run_id}/verify-completion` | Operator or harness path for `workflow-frontdoor verify-completion`; returns JSON decision and Vault evidence block |
| `GET /orchestrator/tasks/{task_id}/runs` | Operator path for derived `task-view`; returns thin run links and queue-shaped evidence without raw run state |
| `POST /provider/claude/prepare` | Operator path for `workflow-frontdoor prepare-claude-adapter`; derives principal from authenticated `operator` channel headers |
| `POST /provider/reports/validate` | Harness gate path for `workflow-frontdoor validate-report`; derives principal from authenticated `harness` channel headers |

There are intentionally no HTTP routes for `orchestrator-start-approve` or
`manual-approve`. Those approval sources are local-only skill invocation and
operator shell paths; HTTP `POST /frontdoor/approve` remains the `human_ui`
path.

Generate a local channel token with:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state channel-token \
  --channel bridge
```

Send the returned token in `X-Orchestrator-Token` with
`X-Orchestrator-Channel: bridge`, `operator`, `human_ui`, or `harness`. The HTTP
server rejects `principal_type`, `principal_id`, and `authn_method` in request
bodies. Bridge audit events use the authenticated `bridge` channel principal and
record requester / peer metadata only as non-authoritative details.

## Non-Scope

| Non-scope | Reason |
|---|---|
| Provider live execution | Runner is a later phase and must be user-owned. |
| LaunchAgent/watch daemon | P0 fixes the contract first; daemon mode is future work. |
| tmux worker | The adapter schema can represent it, but there is no execution path in P0. |
| Viewer UI | Artifacts are structured for later Viewer consumption. |
| deploy/push/PR automation | Publication requires a separate gate. |
