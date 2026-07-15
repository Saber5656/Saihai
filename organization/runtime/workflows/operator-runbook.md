# Orchestrator Operator Runbook

This runbook is the day-1 operating guide for typed workflow runs. It separates
the primary `saihai` CLI, the compatibility `workflow-frontdoor` facade, the
read-only viewer, and capabilities that remain intentionally unimplemented.

Related issues:

| Issue | Scope |
|---|---|
| [#16](https://github.com/Saber5656/Saihai/issues/16) | Parent runbook, migration, artifact, and cleanup tracker |
| [#36](https://github.com/Saber5656/Saihai/issues/36) | Day-1 operator workflow |
| [#37](https://github.com/Saber5656/Saihai/issues/37) | Queue/tmux legacy migration and compatibility cleanup |
| [#38](https://github.com/Saber5656/Saihai/issues/38) | Stuck-run recovery and rollback procedures |
| [#50](https://github.com/Saber5656/Saihai/issues/50) | Template-coverage acceptance verification and closeout |
| [#54](https://github.com/Saber5656/Saihai/issues/54) | Enforced frontend-session tool profile |
| [#81](https://github.com/Saber5656/Saihai/issues/81) | Host-verified capability and scoped worker execution |

## Status Boundary

| Area | Current status |
|---|---|
| Current harness | `scripts/configure_organization.py workflow-frontdoor ...` delegates to `organization/runtime/workflows/scripts/frontdoor_orchestrator.py`. |
| Supported provider path | `run-provider` supports an explicitly selected offline fake-adapter mode. Live Claude/Codex execution requires `--live`, the exact environment guard, pinned executable bindings, and host-owned confinement configuration. |
| Current execution transport | `headless_cli` is implemented. Provider credentials and host confinement configuration remain operator-owned. |
| Legacy tmux transport | `tmux_interactive` is modeled for compatibility only and has no execution path here. |
| Current scheduler | Invocation-drain, durable state, global advisory lock, concurrency 1, provider leases, heartbeat, bounded retries, resume, and abort are implemented. |
| Read surfaces | `task-view`, `lock-status`, completion verification, the localhost frontdoor API, and the read-only workflow viewer/API are implemented. |
| Still planned | Dedicated raw run detail/evidence CLI commands, daemon/watch mode, and tmux worker execution are not implemented. |

The enforced frontend profile from #54 and the scoped worker executor from #81
serve different authority boundaries. An orchestrator-frontend session starts
through the enforced launcher and remains limited to typed request submission,
redacted projection reads, and result acknowledgement. It does not inherit
implementation, shell, Git, worktree, provider, or publication authority.

After approval, only the host-owned executor may verify a canonical capability
derived from the work order, create or select its planned task worktree, and
launch the bounded Codex CLI worker. The worker receives only the capability's
explicit operations, paths, network, provider, and execution limits. Commit,
push, and pull-request publication remain subject to their separate approval
and publication gates. The shipped executor rejects all network and provider
grants; any future external execution remains outside this capability and must
use a separately approved gate. See [the main-agent enforcement runbook](../../../docs/runbooks/main-agent-enforcement.md)
for launcher and canary verification procedures.

## Day-1 Happy Path

Use a disposable state root while testing:

```sh
STATE_ROOT=/tmp/frontdoor-state
```

1. Validate static workflow contracts.

```sh
python3 organization/runtime/workflows/scripts/workflow_selector.py validate-contracts
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

2. Propose a typed request.

Input: `task-id`, `request-id`, at least one bounded `--ref`, and a typed
classification JSON object.

Output: request JSON under `requests/`, an activation envelope, approval view,
and audit events. A prompt-only request can become `proposed`; a request without
typed classification remains `waiting_human`.

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" propose \
  --task-id TSK-example \
  --request-id req-example \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

3. Approve the proposal.

Input: the exact `approval.human_action_id` returned by `propose`.

Output: the request record gains an `approved_activation` and
`approval_record`. Approval challenge mismatches are blocked and rate-limited.

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" approve \
  --request-id req-example \
  --human-action-id <approval.human_action_id>
```

4. Create the workflow run.

Input: an approved request. Optional `--run-id`; otherwise a stable run id is
derived from request id and workflow id. `--resume-policy manual` is the
current default and records manual recovery expectation in the run.

Output: run JSON under `runs/`.

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" create-run \
  --request-id req-example \
  --resume-policy manual
```

5. Drain the run.

Input: `run-id`.

Output: a work order under `work-orders/<run_id>/<step_id>.json`; run state
moves from `created` to `step_queued`.

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" drain \
  --run-id <run_id>
```

6. Run the provider through the host-owned runner.

Input: `run-id` whose current step has a work order.

Use the fake provider for a reproducible offline day-1 smoke:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" run-provider \
  --run-id <run_id> \
  --adapter-id claude_headless_p0 \
  --fake-provider-mode success
```

Live execution requires operator-managed pinned executable path/digest values,
the exact environment guard, and an explicit `--live` flag. Codex additionally
requires a pinned host confinement wrapper and profile. Credential creation,
configuration, and inspection remain manual operator work:

```sh
SAIHAI_ALLOW_LIVE_PROVIDERS=1 python3 scripts/configure_organization.py workflow-frontdoor \
  --state-root "$STATE_ROOT" run-provider \
  --run-id <run_id> --adapter-id claude_headless_p0 --live --timeout-seconds 1800
```

`prepare-claude-adapter` remains only as deprecated, non-executable compatibility
output. It grants no provider write authority.

7. Inspect the durable provider result.

On the `run-provider` path, the host runner owns the typed report, normalized
evidence, and owner-only transcript. An explicitly approved host-owned
integration may stage canonical report/evidence for standalone
`validate-report`; an external provider never receives direct canonical-path
write authority.
After a provider attempt succeeds, `run-provider` invokes the report gate and
returns the resulting terminal or next-action state; a successful fake-provider
smoke normally reaches `complete` without a second validation command.
During a long call, `waiting_provider` contains the current attempt/lease,
heartbeat, timeout, retry counters, and last typed outcome. The global workflow
lock is not held during the provider subprocess. A single invocation defaults to
30 minutes and may be configured up to 24 hours; the harness has no cumulative
deadline. The same retryable failure is automatically retried at most five times.

Inspect current lock ownership without mutating workflow state:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" lock-status
```

8. Explicitly validate an externally produced report or replay validation.

Input: `run-id`; optional `--report-path` must match the canonical work-order
report path and stay under the state root reports directory.

Use this command when an approved host-owned integration has placed externally
produced report/evidence at the canonical paths, or when an operator needs an
explicit idempotent validation replay. `run-provider` already invokes the same
report gate. `pass` and
`findings` reports complete the run; provider-reported `blocked` moves the run
to `waiting_human`; invalid schema/evidence or wrong paths fail the run.

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" validate-report \
  --run-id <run_id>
```

9. Inspect canonical artifacts.

Until a dedicated read-only inspect command exists, inspect the files directly:

```sh
python3 -m json.tool "$STATE_ROOT/requests/req-example.json"
python3 -m json.tool "$STATE_ROOT/runs/<run_id>.json"
python3 -m json.tool "$STATE_ROOT/work-orders/<run_id>/<step_id>.json"
python3 -m json.tool "$STATE_ROOT/reports/<run_id>/<step_id>-external-review-report.json"
python3 -m json.tool "$STATE_ROOT/provider-evidence/<run_id>/<step_id>-provider-evidence.json"
```

For main-agent bridge status, use the implemented projection surface:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" bridge-read-projection \
  --request-id req-example
```

For the task-linked thin view and final completion decision, use:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" task-view \
  --task-id TSK-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" verify-completion \
  --run-id <run_id> \
  --format markdown
```

## State Guide

| State | Artifact | Meaning | Operator action |
|---|---|---|---|
| `draft` | Activation transition source | Prompt/source exists but execution is not approved. | Gather typed classification and bounded refs. |
| `proposed` | Activation envelope in request `proposal` | Deterministic selection succeeded, but execution has not been explicitly approved. | Review approval view and approve or leave waiting. |
| `approved` | Request `status` and run `goal_state` | A human/operator approved the bounded workflow. | Create the run. |
| `created` | Run `run_state` | Durable run exists; no work order has been queued. | Run `drain`. |
| `step_queued` | Run `run_state` | Work order exists and is ready for an adapter. | Run `run-provider`; use fake mode for offline smoke or the explicitly configured live path. |
| `waiting_provider` | Run `run_state` | A provider attempt owns a renewable lease and may be running outside the global lock. | Inspect the run, attempt journal, lease, transcript path, and `lock-status`; use `resume` only when the typed result says recovery is required. |
| `validating` | Run `run_state` | A provider result is ready for typed report/evidence validation. | Run `validate-report`, or `resume` to continue a durable `result_ready` attempt at validation. |
| `waiting_human` | Request or run state | More human input, classification, approval, or remediation is needed. | Inspect request approval view and audit events. |
| `complete` | Run `run_state` and terminal status | Typed report and evidence passed validation. | Preserve artifacts and record final evidence. |
| `failed` | Run `run_state` | Provider execution failed terminally, or report/schema/evidence validation was invalid. A schema-valid provider `blocked` result uses `waiting_human` instead. | Preserve artifacts, inspect errors, and open/follow recovery issue. |
| `aborted` | Run `run_state` | The operator used the typed abort path; the state is terminal. | Preserve artifacts. Replayed aborts do not mutate the terminal run. |

## Canonical Artifacts

All state is rooted at `--state-root`; the default is
`~/.codex/state/itb/frontdoor-orchestrator`.

| Artifact | Path | Authority |
|---|---|---|
| Request record | `requests/<request_id>.json` | Request, bounded refs, proposal, approval, and bridge metadata. |
| Workflow run | `runs/<run_id>.json` | Durable run state, current step, scheduling policy, terminal status, and transition provenance. |
| Work order | `work-orders/<run_id>/<step_id>.json` | Bounded step instruction and report path. |
| Adapter request | `adapter-requests/<run_id>/<step_id>-claude_headless_p0.json` | Provider adapter prompt, evidence path, transcript path, and authority boundary. |
| Typed report | `reports/<run_id>/<step_id>-external-review-report.json` | Canonical provider result for P0 external review. |
| Normalized evidence | `provider-evidence/<run_id>/<step_id>-provider-evidence.json` | Canonical provider evidence checked by validation. |
| Provider transcript | `provider-evidence/<run_id>/<step_id>-provider-transcript.json` | Confined signal only; not authoritative. |
| Provider attempt journals | `provider-evidence/<run_id>/attempts/<attempt_id>-result.json` | Owner-only durable attempt result used for safe result promotion and recovery. Lease, heartbeat, retry, and current attempt state remain in the run record. |
| Transition evidence | `transitions/<run_id>/*` | Canonical lifecycle and report-gate transition records. |
| Scheduler lock | `locks/global-advisory.lock.d/owner.json` | Current mutating operation owner; inspect through `lock-status`. |
| Audit log | `audit/*.jsonl` | Append-only transition, replay, blocked, approval, and bridge events. |
| Idempotency record | `idempotency/key-<digest>.json` | Bridge submit replay protection. |
| Principal keys and channel tokens | `principal-keys/`, `channel-tokens/` | Local signing/authentication material. Preserve permissions and never publish. |

Canonical result authority is `typed_report_file` plus
`normalized_provider_evidence_file`. stdout, tmux pane output, and provider
transcript are signals only.

## Read-Only Viewer And APIs

The local dashboard lists workflow runs and renders work order, report,
provider evidence, transition, corrupt-state, and lock information without
mutating runtime state:

```sh
python3 server.py --port 8799
```

| Endpoint | Purpose |
|---|---|
| `GET /api/workflow-runs?session=<id>&task=<id>&state=<state>` | Filtered thin workflow-run summaries. |
| `GET /api/workflow-run?session=<id>&run=<id>` | Confined run detail, work order, report, evidence, and transition metadata. |
| `GET /api/workflow-lock` | Read-only lock status for discovered orchestrator roots. |

The authenticated localhost frontdoor API exposes host-owned propose, approve,
run, resume, abort, completion, and task-view operations. Start it with:

```sh
python3 scripts/configure_organization.py workflow-frontdoor-server \
  --state-root "$STATE_ROOT" \
  --host 127.0.0.1 \
  --port 8766
```

The dashboard API remains GET-only. It does not approve activation, run a
provider, resume or abort a run, change configuration, or expose raw provider
transcript content.

## Legacy Queue/Tmux Migration

The old `agent-call` runtime writes queue payloads and report files. Typed
workflow runs make the run directory and work-order/report/evidence files the
operator-facing source of truth.

| Legacy concept | Typed-run concept | Migration rule |
|---|---|---|
| `queue/inbox/<role>.yaml` | `work-orders/<run_id>/<step_id>.json` | Treat queue messages as compatibility input only. New orchestrator docs should point to work orders. |
| `queue/tasks/<task>/<message>.yaml` | `requests/<request_id>.json` and `runs/<run_id>.json` | Request and run records hold durable state and provenance. |
| `queue/reports/<role>/<task>/<report>.yaml` | `reports/<run_id>/<step_id>-external-review-report.json` | Typed report schemas are canonical. |
| `agent-call` facade | `workflow-frontdoor` facade | Keep `agent-call` only for organization-internal compatibility until replacement is implemented. |
| tmux pane output | Provider transcript signal | Never use pane output as authoritative completion evidence. |
| tmux worker | `provider_adapter` capability with `tmux_interactive` modeled | Compatibility-only. P0 does not execute tmux workers. |
| report path hidden in queue payload | explicit `report_path` in work order and adapter request | Operators should inspect the canonical report path from the work order. |

Cleanup checklist:

| Item | Required before cleanup | Related issue |
|---|---|---|
| Durable run artifact layout | Atomic store and read/write behavior implemented. | [#20](https://github.com/Saber5656/Saihai/issues/20) |
| Lifecycle commands | Create, resume, abort, and terminal handling implemented. | [#22](https://github.com/Saber5656/Saihai/issues/22) |
| Read-only run APIs | List/detail/evidence APIs implemented. | [#23](https://github.com/Saber5656/Saihai/issues/23) |
| Viewer panels | UI renders workflow runs and status badges. | [#24](https://github.com/Saber5656/Saihai/issues/24) |
| Queue and Vault bridge | Existing queue/evidence views consume typed-run artifacts. | [#12](https://github.com/Saber5656/Saihai/issues/12), [#44](https://github.com/Saber5656/Saihai/issues/44) |
| Failure-mode tests | Regression coverage protects stuck-run recovery. | [#34](https://github.com/Saber5656/Saihai/issues/34), [#35](https://github.com/Saber5656/Saihai/issues/35) |
| Live adapters | Headless Claude/Codex adapters are implemented and tested. | [#10](https://github.com/Saber5656/Saihai/issues/10), [#43](https://github.com/Saber5656/Saihai/issues/43) |

Do not remove queue/tmux terms from schemas or templates when they are present
only to model compatibility or signal-only evidence. Do remove or clarify them
from operator-facing docs when they imply an implemented transport.

## Stuck-Run Recovery

Preserve artifacts first. Copy or archive the full state-root subtree for the
affected `request_id` and `run_id` before remediation. At minimum, preserve
request, run, work order, adapter request, report, provider evidence,
transcript, and audit JSONL files.

| Symptom | Check | Current recovery |
|---|---|---|
| Missing typed classification | Request `status = waiting_human`, proposal reason `typed_classification_required`. | Re-run `propose` with the same immutable request data and a valid typed classification, or create a new request id if immutable inputs changed. |
| Approval challenge mismatch | CLI exits blocked and audit has `approval_challenge_mismatch`. | Read the latest `approval.human_action_id` from the proposal and retry. After rate limit, create a new request id. |
| Unapproved run creation | `create-run` fails with `approved activation envelope required`. | Approve first; do not fabricate `approved_activation`. |
| Run not queueable | `drain` returns `drained = false` and `reason = run_state_not_queueable`. | Inspect run state. If terminal, preserve artifacts and stop. If non-terminal, run `resume --run-id <id>` and follow its typed next action; use `--requeue` only for operator-approved recovery. |
| Adapter blocked | `prepare-claude-adapter` returns `work_order_not_adapter_safe`. | Inspect work order permission mode, allowed ops, context refs, and workflow id. Current P0 adapter supports only readonly `single_step_external_review` step `review`. |
| Provider failure before validation | Missing report/evidence/transcript, provider timeout, or an incomplete attempt leaves a retryable or `waiting_human` run. | Preserve adapter request, transcript, partial report, and evidence. Inspect the typed result and run state. For an approved retry from `waiting_human`, run `resume --run-id <id> --requeue`, then `run-provider`; follow the runner's terminal or next-action result. |
| Provider reported blocked | The report is schema-valid with `result = blocked`; the report gate sets `run_state = waiting_human` and reason `provider_reported_blocked`. | Preserve the report/evidence and resolve the human decision. Resume/requeue only after the blocking condition is addressed; do not treat the run as terminal failed. |
| Invalid report | `validate-report` sets run `run_state = failed`, `goal_state = blocked`, terminal reason `invalid_report`. | Preserve the failed report and validation errors. Create a new request/run for a corrected attempt; do not hand-edit the failed run. |
| Lock contention | A mutating command returns `decision = blocked`, `reason = lock_contention`, and owner metadata. | Run `lock-status`. Wait for a live owner. A stale lock is reclaimed by the next mutating operation only after stale-age and owner-liveness checks pass; never delete the lock directory while the owner may be live. |
| Resume required | Run is non-terminal and provider lease/result state exists. | Run `resume --run-id <id>`. A live lease returns `provider_in_flight`; an expired lease requeues without changing the signed work order; `result_ready` resumes at report validation. |
| Abort required | Operator must stop a non-terminal run. | Run `abort --run-id <id> --reason <reason>`. Heartbeat detects the lost lease/run state and stops the subprocess; stale worker results are never promoted to canonical artifacts. |

The recovery commands are implemented on the compatibility facade:

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" resume \
  --run-id <run_id> \
  --requeue

python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" abort \
  --run-id <run_id> \
  --reason "operator cancelled"
```

Omit `--requeue` when the operator only needs the typed next action. Requeue is
an explicit recovery choice and does not replace or rewrite the signed work
order.

Behavior-changing fixes for recovery, locking, resume, abort, provider runners,
or report validation must update this runbook in the same change.

## Rollback Guidance

Rollback means returning operators to the previous compatible path without
destroying typed-run evidence.

| Scenario | Rollback action |
|---|---|
| Failed typed-run rollout | Stop creating new workflow-frontdoor runs. Keep the state root read-only and route new work through the existing compatibility path. |
| Broken docs or operator command sequence | Restore the previous documented command sequence, then update this runbook with the corrected boundary. |
| Broken provider adapter rollout | Keep request/run/work-order artifacts. Disable provider dispatch and use typed report/evidence files only when they can be validated. |
| Queue bridge regression | Keep `agent-call` compatibility docs and queue views active until [#12](https://github.com/Saber5656/Saihai/issues/12) and [#44](https://github.com/Saber5656/Saihai/issues/44) are fixed. |
| Accidental state mutation | Preserve the mutated state root, audit logs, and shell history. Create a fresh state root for retry rather than editing canonical artifacts in place. |

Rollback must not delete `requests/`, `runs/`, `work-orders/`, `reports/`,
`provider-evidence/`, or `audit/` for affected runs. If deletion is required for
local cleanup, archive the state root first and record evidence in the task or
Vault record.

## Validation Commands

Required after changes to workflow contracts or this runbook:

```sh
python3 scripts/validate_all.py
```

Focused commands for selector/frontdoor investigation:

```sh
python3 organization/runtime/workflows/tests/test_workflow_selector.py
python3 organization/runtime/workflows/scripts/workflow_selector.py validate-contracts
python3 organization/runtime/workflows/tests/test_frontdoor_orchestrator.py
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

## Implementation Issue Map

| Issue | Relevance |
|---|---|
| [#6](https://github.com/Saber5656/Saihai/issues/6) | Recovered baseline P0 workflow contract branch. |
| [#7](https://github.com/Saber5656/Saihai/issues/7) | Durable workflow-run store and invocation-drain scheduler. |
| [#8](https://github.com/Saber5656/Saihai/issues/8) | Explicit activation entrypoints and approval gates. |
| [#9](https://github.com/Saber5656/Saihai/issues/9) | Immutable work orders from workflow templates. |
| [#10](https://github.com/Saber5656/Saihai/issues/10) | Headless provider adapter runner and normalized evidence. |
| [#11](https://github.com/Saber5656/Saihai/issues/11) | Typed report validation and workflow transitions. |
| [#12](https://github.com/Saber5656/Saihai/issues/12) | Existing queue, agent-call, final gate, and Vault evidence integration. |
| [#13](https://github.com/Saber5656/Saihai/issues/13) | Sahai visibility for runs, work orders, evidence, and stuck states. |
| [#14](https://github.com/Saber5656/Saihai/issues/14) | Template coverage beyond P0 external review. |
| [#15](https://github.com/Saber5656/Saihai/issues/15) | End-to-end and failure-mode regression coverage. |
| [#17](https://github.com/Saber5656/Saihai/issues/17) | Typed agent orchestrator implementation tracker. |
| [#18](https://github.com/Saber5656/Saihai/issues/18) | Main-agent frontdoor protocol. |
| [#19](https://github.com/Saber5656/Saihai/issues/19) | Takt-style CLI frontdoor and workflow command split. |
| [#20](https://github.com/Saber5656/Saihai/issues/20) | Workflow-run artifact layout and atomic durable store. |
| [#21](https://github.com/Saber5656/Saihai/issues/21) | Workflow-run advisory lock and P0 concurrency. |
| [#22](https://github.com/Saber5656/Saihai/issues/22) | Lifecycle create, resume, abort, and terminal handling. |
| [#23](https://github.com/Saber5656/Saihai/issues/23) | Read-only workflow-run list and detail APIs. |
| [#24](https://github.com/Saber5656/Saihai/issues/24) | Workflow-run viewer panels and status badges. |
| [#25](https://github.com/Saber5656/Saihai/issues/25) | Workflow-run API safety and corrupt-state tests. |
| [#26](https://github.com/Saber5656/Saihai/issues/26) | Selector and schema generalization for non-P0 templates. |
| [#27](https://github.com/Saber5656/Saihai/issues/27) | `standard_code_change` template. |
| [#28](https://github.com/Saber5656/Saihai/issues/28) | `research_only` template. |
| [#29](https://github.com/Saber5656/Saihai/issues/29) | `publication_required` template and result contract. |
| [#30](https://github.com/Saber5656/Saihai/issues/30) | `policy_or_permission_change` template. |
| [#31](https://github.com/Saber5656/Saihai/issues/31) | `security_sensitive_change` template. |
| [#32](https://github.com/Saber5656/Saihai/issues/32) | Offline E2E harness and single validation command. |
| [#33](https://github.com/Saber5656/Saihai/issues/33) | Fake-provider happy-path E2E. |
| [#34](https://github.com/Saber5656/Saihai/issues/34) | Failure-mode regression suite. |
| [#35](https://github.com/Saber5656/Saihai/issues/35) | Viewer and API fixture tests for run artifacts. |
| [#42](https://github.com/Saber5656/Saihai/issues/42) | Invocation-drain runner harness with fake providers. |
| [#43](https://github.com/Saber5656/Saihai/issues/43) | Live Claude/Codex headless adapter execution. |
| [#44](https://github.com/Saber5656/Saihai/issues/44) | ITB task state and queue evidence bridge. |
| [#45](https://github.com/Saber5656/Saihai/issues/45) | Final-gate and Vault evidence checks for completion. |
| [#50](https://github.com/Saber5656/Saihai/issues/50) | Template-coverage acceptance verification and closeout. |
| [#54](https://github.com/Saber5656/Saihai/issues/54) | Enforced frontend-session tool profile and launcher. |
| [#73](https://github.com/Saber5656/Saihai/issues/73) | Fake-provider evidence and completion-verification alignment. |
| [#74](https://github.com/Saber5656/Saihai/issues/74) | Codex frontend exec-policy state-root enforcement. |
| [#81](https://github.com/Saber5656/Saihai/issues/81) | Host-verified scoped worker capabilities and bounded CLI execution. |
