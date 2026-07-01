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
| UI role | Human approval surface and state renderer |
| Orchestrator authority | Host-owned selector, activation, run, work-order, and gate logic |

## Components

| Component | Authority | May Use LLM | Writes Durable State |
|---|---|---:|---:|
| Agent UI | Shows proposal and captures human approval | No | No |
| Frontdoor service | Validates typed input, calls selector, creates activation envelope | Optional classification proposal only | Yes |
| Deterministic selector | Maps typed classification to workflow decision | No | No |
| Harness engine | Creates workflow run and work orders, owns transitions | No | Yes |
| Provider adapter | Executes one bounded work order | Yes | Report/evidence only |
| Evidence gate | Validates typed report and provider evidence | No | Yes |

## Deterministic Flow

1. UI captures a user request and selected context refs.
2. Frontdoor creates or accepts a `typed-classification` candidate.
3. Frontdoor validates the candidate against
   `schemas/typed-classification.schema.json`.
4. Frontdoor calls `workflow_selector.py select`.
5. For ordinary prompts, frontdoor calls `activation-envelope` with
   `activation_source = frontdoor_prompt`.
6. `frontdoor_prompt` can only produce `activation_status = proposed`.
7. UI renders the proposed workflow, permissions, refs, and blocked operations.
8. Human clicks the explicit start control.
9. Frontdoor recomputes the activation envelope with
   `activation_source = human_ui`.
10. Only an approved envelope with bounded refs can create a workflow run.
11. Harness creates the workflow run and one work order from the selected
    template.
12. Provider adapter receives only the work order and bounded context refs.
13. Provider writes a typed report and normalized evidence.
14. Evidence gate validates schemas and evidence.
15. Harness applies the transition. Provider output never transitions the run
    directly.

## Required Invariants

| Invariant | Enforcement Point |
|---|---|
| Prompt cannot start orchestration | `frontdoor_prompt` maps to `proposed` / `keep_draft` |
| Approval requires human action | Approved sources are only `orchestrator-start`, `human_ui`, `manual_cli` |
| Selection is deterministic | Selector consumes typed classification, not raw prompt text |
| UI cannot choose workflow by itself | Frontdoor must call selector after validating classification |
| No unbounded context sharing | `raw_transcript_sharing = forbidden` |
| P0 has no edit side effects | `allowed_ops.edit = false` |
| P0 has no commit/push side effects | `allowed_ops.commit = false`, `allowed_ops.push = false` |
| P0 has no provider network side effect from activation scope | `allowed_ops.network = false` |
| Runner cannot expand scope | Work order must copy activation scope and template constraints |
| Provider result is not authoritative by itself | Evidence gate validates typed report and normalized evidence |
| Harness owns transitions | Provider adapter has `runner_authority = write_report_only` |

## Host API Shape

The Agent UI should call host-owned APIs, not runtime scripts directly.
The current P0 implementation exposes both a JSON CLI
(`scripts/configure_organization.py workflow-frontdoor ...`) and a local HTTP
wrapper (`scripts/configure_organization.py workflow-frontdoor-server ...`).
Both call the same host-owned frontdoor/harness functions.

```text
POST /frontdoor/propose
  input: user_prompt, selected_context_refs, optional typed_classification
  output: proposed activation envelope or blocked/waiting_human reason

POST /frontdoor/approve
  input: request_id, human_action_id
  output: approved activation envelope or blocked reason

POST /orchestrator/runs
  input: approved activation envelope
  output: workflow_run

POST /orchestrator/runs/{run_id}/drain
  input: run_id
  output: updated workflow_run and generated work_orders

POST /provider/claude/prepare
  input: run_id
  output: bounded adapter request, prompt, report path, and evidence paths

POST /provider/reports/validate
  input: run_id, optional report_path
  output: validated workflow_run terminal state or blocked reason
```

| API Shape | Current CLI | Current HTTP |
|---|---|
| `POST /frontdoor/propose` | `workflow-frontdoor propose` | Implemented |
| `POST /frontdoor/approve` | `workflow-frontdoor approve` | Implemented |
| `POST /orchestrator/runs` | `workflow-frontdoor create-run` | Implemented |
| `POST /orchestrator/runs/{run_id}/drain` | `workflow-frontdoor drain` | Implemented |
| `POST /provider/claude/prepare` | `workflow-frontdoor prepare-claude-adapter` | Implemented |
| `POST /provider/reports/validate` | `workflow-frontdoor validate-report` | Implemented |
| `GET /` | Browser UI shell | Implemented |

The UI must not submit an already approved envelope as authority. On approval,
the frontdoor must reload the stored request, recompute selection, verify the
context refs, and then stamp `approved_by = human_ui_action`.

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

The adapter may store a provider transcript path as confined evidence. The
transcript path is a signal for audit only; it is not shared back into general
agent context and is not authoritative.

## Minimal P0 Implementation Plan

| Step | Deliverable | Existing Contract |
|---|---|---|
| 1 | Frontdoor proposal endpoint | `typed-classification`, selector, proposed activation |
| 2 | Frontdoor approval endpoint | `human_ui` activation source |
| 3 | Workflow run creator | `workflow-run.schema.json` |
| 4 | Work order creator | `work-order.schema.json` and selected template |
| 5 | Invocation-drain harness | registry `p0_scheduler_policy` |
| 6 | Claude headless adapter | provider adapter writes report/evidence only |
| 7 | Evidence gate | `external-review-report.schema.json` |
| 8 | UI run state renderer | workflow run state and terminal fields |

Steps 1 through 8 are implemented for P0. The browser UI shell is intentionally
thin: it can call the frontdoor API and render returned state, but it does not
write durable state or create authority outside the host-owned frontdoor.

## P0 Certainty Boundary

The current contracts are implementable for readonly external review.

They are not yet sufficient for edit-capable orchestration. Code-change,
publication, policy-change, and security-sensitive workflows are intentionally
`waiting_human` or planned templates until separate templates, action gateway
rules, and tests exist.

For edit-capable deterministic control, the next required contract is a
host-owned action gateway that withholds write, shell, commit, push, network,
and provider-dispatch tools from the LLM unless an approved work order grants
that exact operation.
