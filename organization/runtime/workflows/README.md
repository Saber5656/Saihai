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
| `schemas/provider-adapter-capability.schema.json` | Adapter capability descriptor, including future `tmux_interactive` transport |
| `schemas/external-review-report.schema.json` | Authoritative typed report for external review |
| `schemas/research-report.schema.json` | Authoritative typed report for research-only work |
| `schemas/code-change-report.schema.json` | Authoritative typed report for code-change work |
| `schemas/publication-result.schema.json` | Publication gate result schema |
| `schemas/policy-change-report.schema.json` | Policy or permission change evidence schema |
| `schemas/security-review-report.schema.json` | Security-sensitive review evidence schema |
| `scripts/workflow_selector.py` | Deterministic selector and activation-envelope helper |
| `tests/test_workflow_selector.py` | Unit/static contract tests |

The `.yaml` files in this directory are JSON-compatible by design, matching the
existing runtime config convention in `organization/runtime/infra-team-bootstrap`.

## Runtime Principles

| Principle | P0 Contract |
|---|---|
| Prompt does not start orchestration | `frontdoor_prompt` activation can only produce `proposed` state. |
| Explicit activation only | `orchestrator-start`, `human_ui`, or `manual_cli` can approve a selected bounded workflow. |
| Workflow selection is deterministic | The selector consumes typed classification; it does not read free-form prompt text. |
| Agent output is not authoritative | `typed_report_file` and normalized evidence are canonical. stdout, tmux pane output, and provider transcript are signals only. |
| Context sharing is typed | Shared run state is durable typed state; step snapshots are immutable; provider transcripts remain confined evidence paths. |
| Common gates are centralized | `registry.yaml` defines reusable `entry.*` and `exit.*` gate profiles; templates reference them by id. |
| Safety class cannot downgrade | Selector validation rejects routing `policy` or `security` classifications into weaker templates. |
| Publication is a separate axis | Publication is represented by `publication_gate` and `exit.publication_result_recorded`, not by duplicating every workflow. |
| Scheduler is bounded | Policy is invocation-drain, durable state, global advisory lock, concurrency 1. |
| Provider is an adapter | `headless_cli` is the default transport. `tmux_interactive` is modeled but not implemented. |

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
  --classification '{"classification_version":"1","task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

Create a proposed envelope for an ordinary prompt source:

```sh
python3 scripts/configure_organization.py workflow-selector activation-envelope \
  --activation-source frontdoor_prompt \
  --task-id TSK-example \
  --request-id req-example \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

Create an approved envelope only from explicit invocation:

```sh
python3 scripts/configure_organization.py workflow-selector activation-envelope \
  --activation-source orchestrator-start \
  --task-id TSK-example \
  --request-id req-example \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'
```

## Non-Scope

| Non-scope | Reason |
|---|---|
| Provider live execution | Runner is a later phase and must be user-owned. |
| LaunchAgent/watch daemon | P0 fixes the contract first; daemon mode is future work. |
| tmux worker | The adapter schema can represent it, but there is no execution path in P0. |
| Viewer UI | Artifacts are structured for later Viewer consumption. |
| deploy/push/PR automation | Publication requires a separate gate. |
