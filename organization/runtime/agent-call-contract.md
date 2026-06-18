---
type: runtime-contract
status: active
owner: infra-team-bootstrap
source_task: TSK-1290
last_updated: 2026-06-16
---

# Agent Call Runtime Contract

`co agent-call` is the public organization dispatch facade.
It hides provider, tmux, queue, and report-path details from callers while keeping
the durable queue and report YAML files as the source of truth.

This contract defines the active minimum runtime behavior. The implemented
facade commands are `agent-call`, `agent-switch`, `provider-failover`,
`agent-surfaces`, and `transport-status`.

## Scope

| Item | Rule |
|---|---|
| In scope | Organization-internal role / agent dispatch |
| Out of scope | Personal ad hoc cross-agent chat, cmux surface chat, remote-server transport |
| Public work command | `co agent-call` |
| Public switch commands | `co agent-switch`, `co provider-failover` |
| Public diagnostic commands | `co agent-surfaces`, `co transport-status` |
| Internal primitives | `role-queue`, `agent-dispatch`, `agent-dispatch-batch`, `provider-activate` |

`role-queue` remains the canonical durable queue primitive. `agent-dispatch` is
a direct / synchronous fallback or specialized helper, not the public primary
API.

## Authority Boundary

| Area | Authority |
|---|---|
| Normal organization work | Agents may decide within their assigned role and task scope |
| Organization meta-control | Human approval is required |

Organization meta-control includes policy, registry, provider assignment,
authority model, flow contract, and facade contract changes.

## External Provider Transport Approval

Organization-internal provider transport may send bounded task context to the
configured provider without asking the human user on every Codex-Claude turn.
The standing approval is recorded in `organization/settings.json` under
`provider_transport_policy.external_context_transmission`.

| Item | Rule |
|---|---|
| Approved scope | `co` / ITB organization-internal role dispatch, queue manifests, bounded task instructions, context refs, report paths, and provider evidence |
| Commands in scope | `agent-call`, `agent-dispatch`, `agent-dispatch-batch`, `role-queue`, `provider-activate`, `agent-switch`, `provider-failover` |
| Still forbidden | secrets, credentials, auth tokens, private keys, unrelated personal data, unbounded Vault/repository/home/transcript dumps |
| Separate approval required | persistent provider/model registry changes, authority model changes, policy changes outside the approved transport scope |
| Host boundary | Repository policy does not bypass Codex host-level sandbox, escalation, or external-provider safety review |

Use explicit input files such as `--input-json-file` for transport commands when
possible. This keeps command approval narrow and makes the transmitted payload
auditable.

## Agent Call Flow

```text
caller manifest
-> co validates and supplements context_refs
-> co writes queue/inbox/<role>.yaml and queue/tasks/<task>/<message>.yaml
-> co nudges tmux with a short "read the YAML" signal
-> target provider reads YAML and writes queue/reports/<role>/<task>/<report>.yaml
-> co returns a receipt by default, or waits for terminal report when --wait is explicit
```

The tmux pane is a transport and visibility surface only. Report YAML terminal
status is authoritative.

## Agent Call Manifest

The manifest is the canonical input. CLI flags are only shorthand and must be
normalized to this shape before enqueue.

```yaml
agent_call_manifest_version: "1"
task_id: "TSK-1290"
from_role: "tech-director"
to_role: "tech-backend"
assignment_role: "implementer"
instruction: "Implement the approved API change."
expected_output: "implementation_report"
wait: false
context_refs:
  - type: task
    path: "01-Projects/AI-Agent-Organization/TSK-1290-agent-bridge-unification-research/task.md"
  - type: source
    path: "src/api/example.ts"
```

| Field | Required | Rule |
|---|---:|---|
| `agent_call_manifest_version` | yes | Current value: `"1"` |
| `task_id` | yes | Parent task or entry task id |
| `from_role` | yes | Logical caller role |
| `to_role` / `to_agent` | yes | Logical target. tmux / provider / model are registry-resolved |
| `assignment_role` | conditional | Required for Director -> team member calls |
| `instruction` | yes | Bounded instruction for this call |
| `expected_output` | yes | Contract for the target report |
| `wait` | no | Default `false`; `true` waits for terminal report |
| `context_refs` | no | Caller-provided refs; `co` may supplement by preset |

## Generated Receipt

`co agent-call` returns a receipt by default. It does not wait for the work result
unless `wait: true` or `--wait` is explicit.

```yaml
agent_call_receipt_version: "1"
decision: "ok"
result: "queued"
task_id: "TSK-1290"
to_role: "tech-backend"
message_id: "msg-..."
report_id: "rep-..."
queue_root: "..."
inbox_path: "queue/inbox/tech-backend.yaml"
payload_path: "queue/tasks/TSK-1290/msg-....yaml"
report_path: "queue/reports/tech-backend/TSK-1290/rep-....yaml"
queue_status: "pending"
nudge_status: "sent"
```

## Target Resolution

| Rule | Detail |
|---|---|
| Logical target | Callers specify `to_role` / `to_agent`, not tmux target or provider |
| Registry source | `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml` |
| Missing role | Block |
| Missing `role_layer` | Block as registry metadata error |
| Provider / model override | Not accepted by `agent-call` |

`agent-call` is allowed to call active resident roles that are lazy or not raw
`role-queue` consumers. In that case `co` supplies a per-call queue consumer
override so the public facade can still use the durable YAML queue. Raw
`role-queue` keeps its existing `queue_consumer` behavior.

## Role Layer

Static `role_layer` values live in
`organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml`.

| Layer | Meaning |
|---|---|
| `gate` | Gate fixed flow roles |
| `tpm` | Team-level routing |
| `director` | Team-internal orchestration |
| `worker` | Specialist member / execution role |

`reviewer` is not a static layer. It is a task-local assignment role.

## Context Presets

`co` supplements context refs based on the static role layer and optional
assignment role. It should reduce each agent's cognitive and search workload.

| Layer | CO-supplemented refs |
|---|---|
| `gate` | user prompt, task policy, Gate I/O contract, existing active task when needed |
| `tpm` | Task Detail, GTC handoff, routing hint, approval status, Branch Plan hints, completion-chain, role/team catalog |
| `director` | Task Detail, TPM routing decision, team role catalog, team task path, review requirements, approval status |
| `worker` | Director assignment, target subtask, needed file refs, expected output, report contract |

## Assignment Role

`assignment_role` describes the target's task-local role. It is separate from
static `role_layer`.

| Value | Meaning |
|---|---|
| `none` | Explicitly no assignment overlay |
| `implementer` | Main execution / code-writing role |
| `reviewer` | Review role |
| `qa` | QA / acceptance role |
| `approver` | Approval / decision role |
| `observer` | Reference, advisory, or monitoring role |

| Rule | Detail |
|---|---|
| Authority | Only Directors assign `assignment_role` to team members |
| Director -> team member | `assignment_role` is required, including explicit `none` |
| TPM -> Director | `assignment_role` is not used |
| Missing required assignment | Block as manifest validation error |
| Merge behavior | Additive context overlay |
| Permission behavior | Does not override allowed tools, provider, model, or permission boundary |

## Assignment Overlays

| Assignment role | Added context |
|---|---|
| `none` | No overlay |
| `implementer` | Target files, edit scope, verification request, report checklist |
| `reviewer` | Artifact / diff refs, review criteria, risk checklist |
| `qa` | Acceptance criteria, test evidence refs, QA verdict requirements |
| `approver` | Decision material, approval criteria, impact scope |
| `observer` | Watch target, advisory scope, reporting cadence |

## Busy And Pending Handling

| Condition | Behavior |
|---|---|
| Existing pending message | Queue the new message but defer nudge by default |
| Existing terminal report | Recover and finalize before deciding whether to nudge |
| SLA breach | Run queue watch / recovery / re-nudge |
| Provider busy or approval wait | Keep message pending and return receipt with status |

Head-of-line order is preserved. `agent-call` must not send concurrent prompts
to the same role pane unless a batch manifest explicitly proves independence and
targets distinct roles.

## Wait Semantics

| Item | Rule |
|---|---|
| Default | Return receipt only |
| `--wait` / `wait: true` | Wait for terminal report at `report_path` |
| Authoritative source | Report YAML terminal status |
| Fallback signal | tmux marker / capture-pane / stdout |

## Provider Switch Contract

`agent-call` does not accept model or provider override. Limit and capacity
handling use `agent-switch` or `provider-failover`.

```yaml
agent_switch_manifest_version: "1"
target_role: "tech-lead"
reason: "anthropic_weekly_limit"
from:
  provider: "anthropic"
  model: "claude-sonnet-4-6"
to:
  provider: "openai"
  model: "gpt-5.5"
persist: false
```

| Rule | Detail |
|---|---|
| Primitive granularity | Role-level switch |
| Bulk failover | `co` orchestrates multiple role-level switches |
| Pending / processing target | Block until completed, closed, or explicitly requeued |
| Default persistence | Session-local roster only |
| `--persist` | Requires human approval and Vault evidence |

The current implementation updates the session-local roster for non-persistent
switches. Persistent provider assignment changes remain registry edits and must
be handled as human-approved organization meta-control.

## Diagnostics

| Command | Purpose |
|---|---|
| `agent-surfaces` | List callable roles, layers, assignment support, queue consumer state, provider/runtime status |
| `transport-status` | Check tmux, provider CLI, auth/readiness, queue root, and prompt readiness |

These commands are read-only diagnostics. They do not enqueue work.

## Validation Checklist

| Check | Result |
|---|---|
| Unknown target blocks | Required |
| Missing static `role_layer` blocks | Required |
| Director -> team member missing `assignment_role` blocks | Required |
| `assignment_role` outside allowed vocabulary blocks | Required |
| `assignment_role` does not alter permissions | Required |
| `agent-call` returns receipt by default | Required |
| `--wait` waits on report YAML, not pane text | Required |
| provider/model switch is separate from `agent-call` | Required |
