---
type: runtime-contract
status: active
owner: infra-team-bootstrap
source_task: TSK-1314
last_updated: 2026-06-23
---

# Agent Call Runtime Contract

`co agent-call` is the public organization dispatch facade.
It hides provider, queue, and report-path details from callers while keeping typed
queue payloads and report files as the source of truth.

## Scope

| Item | Rule |
|---|---|
| In scope | Organization-internal role / agent dispatch |
| Out of scope | ad hoc cross-agent chat, remote server transport, hook-side orchestration |
| Public work command | `co agent-call` |
| Public switch commands | `co agent-switch`, `co provider-failover` |
| Public diagnostic commands | `co agent-surfaces`, `co transport-status` |
| Internal primitives | `role-queue`, `agent-dispatch`, `provider-activate` |

## Authority Boundary

Organization meta-control includes policy, registry, provider assignment,
authority model, flow contract, and facade contract changes. Those changes
require human approval.

## Transport Approval

Organization-internal provider transport may send bounded task context to the
configured provider when it stays inside the approved scope in
`organization/settings.json`.

| Item | Rule |
|---|---|
| Approved scope | task instructions, context refs, queue manifests, report paths, provider evidence |
| Commands in scope | `agent-call`, `agent-dispatch`, `role-queue`, `provider-activate`, `agent-switch`, `provider-failover` |
| Forbidden | secrets, credentials, auth tokens, unrelated personal data, unbounded Vault/repository/home/transcript dumps |

## Agent Call Flow

```text
caller manifest
-> co validates and supplements context_refs
-> co writes queue/inbox/<role>.yaml and queue/tasks/<task>/<message>.yaml
-> explicit headless CLI worker processes the payload
-> worker writes queue/reports/<role>/<task>/<report>.yaml
-> co returns a receipt, or waits for terminal report when wait is explicit
```

Report terminal status is authoritative.

## Manifest

```yaml
agent_call_manifest_version: "1"
task_id: "TSK-1314"
from_role: "tech-director"
to_role: "tech-backend"
assignment_role: "implementer"
instruction: "Implement the approved API change."
expected_output: "implementation_report"
wait: false
context_refs:
  - type: task
    path: "01-Projects/.../task.md"
```

| Field | Required | Rule |
|---|---:|---|
| `agent_call_manifest_version` | yes | Current value: `"1"` |
| `task_id` | yes | Parent task or entry task id |
| `from_role` | yes | Logical caller role |
| `to_role` / `to_agent` | yes | Logical target |
| `assignment_role` | conditional | Required for Director -> team member calls |
| `instruction` | yes | Bounded instruction |
| `expected_output` | yes | Contract for target report |
| `wait` | no | Default `false`; `true` waits for terminal report |
| `context_refs` | no | Caller-provided refs; `co` may supplement by preset |

## Target Resolution

| Rule | Detail |
|---|---|
| Logical target | Callers specify role / agent id, not provider internals |
| Registry source | `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml` |
| Missing role | Block |
| Missing role metadata | Block |
| Provider / model override | Not accepted by `agent-call` |

## Assignment Overlays

`assignment_role` tells the callee which evidence and handoff shape is required.
It does not change provider transport or allow hook-side orchestration.

| assignment_role | Required behavior |
|---|---|
| `implementer` | Produce bounded implementation or research output plus changed-artifact evidence |
| `reviewer` | Produce findings, severity, file/line references when applicable, and residual risk |
| `qa` | Produce validation steps, command results, failures, and unresolved test gaps |
| `approver` | Produce approve/block verdict and typed reason |
| `observer` | Produce read-only notes without mutating artifacts |

## Worker Evidence

| Provider | Runtime |
|---|---|
| Anthropic | `claude --print --output-format json` |
| OpenAI | `codex exec --ephemeral --json` |

Provider evidence must include request id, provider session id, effective model,
usage, duration, response/result, and report path when available.
