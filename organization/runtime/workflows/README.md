# Orchestrator Workflow Contracts

This directory is the contract surface for the typed agent orchestrator.
It defines process data only. It does not run Claude, Codex, tmux, a daemon, or
any provider.

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
| `scripts/frontdoor_server.py` | Local HTTP wrapper for Agent UI integration |
| `tests/test_workflow_selector.py` | Unit/static contract tests |
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
- No extra run index files are maintained by this workflow-run store.

## Workflow Run Lifecycle

`scripts/run_lifecycle.py` is the single source of truth for host-owned
workflow-run state transitions. Each accepted transition appends a normalized
record to `workflow_run.transitions` with `seq`, `from_state`, `to_state`,
`reason_class`, `occurred_at`, principal, signature, and artifact references.
Terminal runs are immutable.

| From state | Allowed next states | Goal state mapping |
|---|---|---|
| `created` | `step_queued`, `aborted` | `approved` |
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
`run-step`, `verify-completion`, `task-view`, or `list`. `resume`, `abort`,
and `lock-status` are currently exposed through the compatibility facade below
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

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state create-run \
  --request-id req-example

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state drain \
  --run-id <run_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state prepare-claude-adapter \
  --run-id <run_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state validate-report \
  --run-id <run_id>

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state resume \
  --run-id <run_id> \
  --requeue

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state abort \
  --run-id <run_id> \
  --reason "operator cancelled"

python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state lock-status
```

The facade is compatibility-preserving and delegates to the same
`frontdoor_orchestrator.py` functions as `saihai`. New operator workflows should
prefer `saihai`; existing automation can keep using `workflow-frontdoor`.

The `human_action_id` is a proposal-digest challenge returned by `propose`.
It is not arbitrary UI text. Execution commands accept `--principal-type`,
`--principal-id`, and `--authn-method`; `main_agent_bridge` is rejected for
execution-class transitions.

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
`create-run`, `drain`, `adapter-capability`, `prepare-claude-adapter`,
`validate-report`, `resume`, `abort`, `bridge-submit-request`,
`bridge-read-projection`, `bridge-ack-output`, `channel-token`, and
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
| `POST /orchestrator/runs/{run_id}/resume` | Operator path for `workflow-frontdoor resume`; body accepts `{"requeue": true}` |
| `POST /orchestrator/runs/{run_id}/abort` | Operator path for `workflow-frontdoor abort`; body accepts `{"reason": "..."}` |
| `POST /provider/claude/prepare` | Operator path for `workflow-frontdoor prepare-claude-adapter`; derives principal from authenticated `operator` channel headers |
| `POST /provider/reports/validate` | Harness gate path for `workflow-frontdoor validate-report`; derives principal from authenticated `harness` channel headers |

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
