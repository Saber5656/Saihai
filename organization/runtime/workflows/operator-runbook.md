# Orchestrator Operator Runbook

This runbook is the day-1 operating guide for typed workflow runs.
It covers the current CLI surface in this directory and marks future commands
as planned when they are modeled but not implemented.

Related issues:

| Issue | Scope |
|---|---|
| [#16](https://github.com/Saber5656/Saihai/issues/16) | Parent runbook, migration, artifact, and cleanup tracker |
| [#36](https://github.com/Saber5656/Saihai/issues/36) | Day-1 operator workflow |
| [#37](https://github.com/Saber5656/Saihai/issues/37) | Queue/tmux legacy migration and compatibility cleanup |
| [#38](https://github.com/Saber5656/Saihai/issues/38) | Stuck-run recovery and rollback procedures |

## Status Boundary

| Area | Current status |
|---|---|
| Current harness | `scripts/configure_organization.py workflow-frontdoor ...` delegates to `organization/runtime/workflows/scripts/frontdoor_orchestrator.py`. |
| Supported provider path | P0 prepares a bounded `claude_headless_p0` adapter request; live provider execution is outside the harness command set. |
| Current execution transport | `headless_cli` is the implemented adapter capability. |
| Legacy tmux transport | `tmux_interactive` is modeled for compatibility only and has no execution path here. |
| Current scheduler | Invocation-drain, durable state, concurrency 1. The lock policy is represented as `global_advisory_lock` in contracts. |
| Planned commands | Dedicated `resume`, `abort`, run detail/list, and evidence inspection commands are planned by the implementation issues below. Do not document them as available CLI commands until implemented. |

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

6. Prepare the adapter request.

Input: `run-id` whose current step has a work order.

Output: adapter request JSON under `adapter-requests/`, with canonical report,
provider evidence, transcript, and prompt paths.

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" prepare-claude-adapter \
  --run-id <run_id>
```

7. Run the provider outside this harness.

The provider must write the typed report, normalized evidence, and confined
transcript files named in the adapter request. The transcript is signal-only
provider evidence, not completion authority. It must not select workflows,
approve activation, mutate run state, edit the repo, commit, push, or publish.

8. Validate the report.

Input: `run-id`; optional `--report-path` must match the canonical work-order
report path and stay under the state root reports directory.

Output: run terminal state. `pass` and `findings` reports complete the run;
`blocked`, `invalid`, schema errors, missing evidence, or wrong report paths
block or fail the run.

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

## State Guide

| State | Artifact | Meaning | Operator action |
|---|---|---|---|
| `draft` | Activation transition source | Prompt/source exists but execution is not approved. | Gather typed classification and bounded refs. |
| `proposed` | Activation envelope in request `proposal` | Deterministic selection succeeded, but execution has not been explicitly approved. | Review approval view and approve or leave waiting. |
| `approved` | Request `status` and run `goal_state` | A human/operator approved the bounded workflow. | Create the run. |
| `created` | Run `run_state` | Durable run exists; no work order has been queued. | Run `drain`. |
| `step_queued` | Run `run_state` | Work order exists and is ready for an adapter. | Prepare the adapter and dispatch provider work outside the harness. |
| `waiting_provider` | Run schema state | Modeled state for future provider runner integration. | Inspect adapter/report/evidence paths; planned runner commands must update the runbook when implemented. |
| `validating` | Run schema state | Modeled state for future validation transition. | Use current `validate-report` command; update runbook when validation state is explicitly persisted. |
| `waiting_human` | Request or run state | More human input, classification, approval, or remediation is needed. | Inspect request approval view and audit events. |
| `complete` | Run `run_state` and terminal status | Typed report and evidence passed validation. | Preserve artifacts and record final evidence. |
| `failed` | Run `run_state` | Validation failed or provider report returned a blocking result. | Preserve artifacts, inspect errors, and open/follow recovery issue. |
| `aborted` | Run schema state | Terminal abort is modeled but no dedicated abort command exists yet. | Planned. Do not hand-edit terminal state except as an approved break-glass recovery. |

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
| Provider transcript | `provider-evidence/<run_id>/<step_id>-claude-transcript.json` | Confined signal only; not authoritative. |
| Audit log | `audit/*.jsonl` | Append-only transition, replay, blocked, approval, and bridge events. |
| Idempotency record | `idempotency/key-<digest>.json` | Bridge submit replay protection. |
| Principal keys and channel tokens | `principal-keys/`, `channel-tokens/` | Local signing/authentication material. Preserve permissions and never publish. |

Canonical result authority is `typed_report_file` plus
`normalized_provider_evidence_file`. stdout, tmux pane output, and provider
transcript are signals only.

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
| Run not queueable | `drain` returns `drained = false` and `reason = run_state_not_queueable`. | Inspect run state. If terminal, preserve artifacts and stop. If a future runner state is stuck, follow the future resume command once implemented. |
| Adapter blocked | `prepare-claude-adapter` returns `work_order_not_adapter_safe`. | Inspect work order permission mode, allowed ops, context refs, and workflow id. Current P0 adapter supports only readonly `single_step_external_review` step `review`. |
| Provider failure before validation | Missing report/evidence/transcript, provider timeout, or an incomplete provider attempt before `validate-report` terminalizes the run. | Preserve adapter request, transcript, partial report, and evidence. Re-run provider only if the work order and context refs are unchanged. Then run `validate-report`. |
| Provider report terminalized blocked or invalid | `validate-report` has already set terminal `run_state = failed`, `goal_state = blocked`, and a `provider_report_blocked` / `provider_report_invalid` reason. | Preserve the terminal run and provider artifacts. Create a new request/run for a corrected attempt; rerunning `validate-report` on the terminal run only replays `terminal_run_already_set`. |
| Invalid report | `validate-report` sets run `run_state = failed`, `goal_state = blocked`, terminal reason `invalid_report`. | Preserve the failed report and validation errors. Create a new request/run for a corrected attempt; do not hand-edit the failed run. |
| Lock contention | Contract says `global_advisory_lock` and concurrency 1, but current P0 has no separate lock-inspection CLI. | Ensure only one operator drains/validates a state root at a time. If a future lock file/API is introduced, this runbook must be updated by that implementation issue. |
| Resume required | Run is non-terminal and all canonical artifacts for the current step exist. | Current CLI has no `resume` command. Manual resume means rerun the idempotent next command (`drain`, `prepare-claude-adapter`, or `validate-report`) after inspecting state. Dedicated resume is planned in [#22](https://github.com/Saber5656/Saihai/issues/22). |
| Abort required | Operator must stop a non-terminal run. | Dedicated abort is planned in [#22](https://github.com/Saber5656/Saihai/issues/22). Until then, preserve artifacts and record the stop decision outside the run state unless an approved break-glass procedure explicitly updates state. |

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
python3 organization/runtime/workflows/tests/test_workflow_selector.py
python3 organization/runtime/workflows/scripts/workflow_selector.py validate-contracts
```

Recommended when CLI/frontdoor behavior changes:

```sh
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
