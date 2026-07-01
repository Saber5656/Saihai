# Orchestrator P0 Workflow Contracts

This directory is the P0 contract surface for the typed agent orchestrator.
It defines process data only. It does not run Claude, Codex, tmux, a daemon, or
any provider.

## Scope

| File | Purpose |
|---|---|
| `registry.yaml` | Active workflow registry, deterministic selector policy, P0 scheduler policy |
| `templates/single_step_external_review.yaml` | Initial single-step readonly external review workflow |
| `schemas/typed-classification.schema.json` | Required fields an LLM or human may propose before deterministic selection |
| `schemas/activation-envelope.schema.json` | Gate envelope for draft/proposed/approved/blocked activation state |
| `schemas/workflow-run.schema.json` | Durable per-task workflow run state contract |
| `schemas/work-order.schema.json` | Bounded work order contract for one workflow step |
| `schemas/main-agent-bridge-request.schema.json` | Restricted submit request accepted from the main-agent confirmation surface |
| `schemas/orchestrator-projection.schema.json` | Redacted output projection safe for main-agent rendering |
| `schemas/audit-event.schema.json` | Append-only principal/provenance event contract |
| `schemas/provider-adapter-capability.schema.json` | Adapter capability descriptor, including future `tmux_interactive` transport |
| `schemas/external-review-report.schema.json` | Authoritative typed report for the P0 workflow |
| `scripts/workflow_selector.py` | Deterministic selector and activation-envelope helper |
| `scripts/frontdoor_orchestrator.py` | Host-owned frontdoor and invocation-drain P0 harness |
| `scripts/frontdoor_server.py` | Local HTTP wrapper for Agent UI integration |
| `tests/test_workflow_selector.py` | Unit/static contract tests |
| `frontdoor-orchestrator-protocol.md` | Implementation boundary for Agent UI, host frontdoor, harness, and Claude adapter control |

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
| Execution requires a signed non-bridge principal | Run creation, drain, adapter preparation, report validation, and workflow-definition changes reject `main_agent_bridge`. |
| Audit is append-only | Frontdoor, bridge, approval, execution, replay, and rejection decisions write principal-scoped audit events. |
| Agent output is not authoritative | `typed_report_file` and normalized evidence are canonical. stdout, tmux pane output, and provider transcript are signals only. |
| Context sharing is typed | Shared run state is durable typed state; step snapshots are immutable; provider transcripts remain confined evidence paths. |
| Scheduler is bounded | P0 policy is invocation-drain, durable state, global advisory lock, concurrency 1. |
| Provider is an adapter | `headless_cli` is the default transport. `tmux_interactive` is modeled but not implemented. |

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

## Frontdoor Harness CLI

The host-owned frontdoor/harness is available through the organization facade:

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
```

The `human_action_id` is a proposal-digest challenge returned by `propose`.
It is not arbitrary UI text. Execution commands accept `--principal-type`,
`--principal-id`, and `--authn-method`; `main_agent_bridge` is rejected for
execution-class transitions.

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
pure acknowledgement and has `transition_effect = none`.

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
| `POST /main-agent/submit-request` | Restricted bridge submit; no classification or execution fields |
| `GET /main-agent/projections/{request_id}` | Redacted typed projection for main-agent rendering |
| `POST /main-agent/ack-output` | No-op acknowledgement; no state transition |
| `POST /frontdoor/propose` | Operator path for `workflow-frontdoor propose`; requires `principal_type` |
| `POST /frontdoor/approve` | Operator path for `workflow-frontdoor approve`; requires `principal_type` and challenge id |
| `POST /orchestrator/runs` | Operator/harness path for `workflow-frontdoor create-run`; requires `principal_type` |
| `POST /orchestrator/runs/{run_id}/drain` | Operator/harness path for `workflow-frontdoor drain`; requires `principal_type` |
| `POST /provider/claude/prepare` | Operator/harness path for `workflow-frontdoor prepare-claude-adapter`; requires `principal_type` |
| `POST /provider/reports/validate` | Harness gate path for `workflow-frontdoor validate-report`; requires `principal_type` |

## P0 Non-Scope

| Non-scope | Reason |
|---|---|
| Provider live execution | Runner is a later phase and must be user-owned. |
| LaunchAgent/watch daemon | P0 fixes the contract first; daemon mode is future work. |
| tmux worker | The adapter schema can represent it, but there is no execution path in P0. |
| Viewer UI | Artifacts are structured for later Viewer consumption. |
| deploy/push/PR automation | Publication requires a separate gate. |
