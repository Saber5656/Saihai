# Constrain main agent to orchestrator output confirmation UI

## Context

The current P0 frontdoor/harness prevents prompt-only workflow activation and
blocks run creation until approval. That is necessary, but it does not yet prove
that the main agent is prevented from reasoning or acting.

During manual verification, the main agent still ran `python3` / `curl`,
interpreted outputs, and decided the next checks. That means the current flow is
still main-agent-led outside the runtime API.

## Target Positioning

The main agent should behave like an output confirmation surface for the
orchestrator:

1. Submit a user request or selected action to the orchestrator/frontdoor.
2. Receive typed orchestrator output.
3. Render or relay that output.
4. Wait for explicit human approval when required.
5. Never classify, select workflow, execute shell commands, mutate state, call
   providers, or decide next work outside the orchestrator contract.

## Required Control Boundary

| Boundary | Required Behavior |
|---|---|
| Classification | Main agent must not infer typed classification. Classification must be human-supplied, deterministic, or a bounded provider step with evidence. |
| Workflow selection | Only deterministic selector may choose workflow. |
| Activation | Prompt-originated requests can only become `proposed` or `waiting_human`. |
| Execution | Actual work starts only after explicit approval and through harness API. |
| Main agent output | Main agent renders typed orchestrator output; it does not continue the task by reasoning. |
| Provider output | Provider output is signal until evidence gate accepts typed report/evidence. |
| Shell/tool use | No arbitrary shell/tool calls by the main agent as a substitute for orchestrator execution. |

## Previous Gap

The current implementation has strong frontdoor gates but not a full
main-agent bridge:

- The browser UI still exposes a typed classification input.
- A main agent can still call local scripts directly.
- Tests can verify API behavior, but they do not model a restricted main-agent
  bridge.
- `scripts/configure_organization.py` remains a broad facade, not a dedicated
  orchestrator-only control surface.

## Acceptance Criteria

- [x] Define a `main-agent-bridge` contract that allows only typed request
      submission and typed output rendering.
- [x] Add an API mode where the main agent can submit prompt/context but cannot
      submit inferred classification.
- [x] Move classification into one of:
      human-confirmed input, deterministic fixture/parser, or bounded provider
      step with report/evidence.
- [x] Update the browser UI so the default path is orchestrator output review,
      not manual hidden classification editing.
- [x] Add tests proving prompt submission returns only `waiting_human` /
      `proposed` until a non-main-agent classification/approval path exists.
- [x] Add tests proving run creation and provider preparation cannot be reached
      through a main-agent-only bridge without approval.
- [x] Document that main-agent reasoning cannot be used as a runtime authority.

## Implemented Resolution

| Area | Resolution |
|---|---|
| Bridge contract | Added `schemas/main-agent-bridge-request.schema.json` and CLI/HTTP `bridge-submit-request`, `bridge-read-projection`, `bridge-ack-output`. |
| Payload smuggling | Bridge submit uses a strict allowlist and rejects classification, workflow, approval, run, adapter, report, and workflow-definition fields. |
| Output projection | Added redacted `orchestrator-projection` shape; main-agent reads omit raw prompt, internal paths, report/evidence paths, provider session data, and principal keys. |
| Ack semantics | `ack_output` writes only an ack/audit event and returns `transition_effect = none`. |
| Classification | `typed-classification` now requires source, confidence, and evidence; `frontdoor_llm_proposal` is not an authority source. |
| Approval | Approval uses orchestrator-owned structured summary and digest-derived challenge id with failure rate-limit state. |
| Principal model | Execution-class transitions require signed non-bridge principal; `main_agent_bridge` is rejected for create-run, drain, adapter prepare, report validation, and workflow-definition change. |
| Work order | Work orders include issuer principal, signature, and unclaimed runner lease metadata. Raw user prompt is not embedded as executable provider instruction. |
| Audit | Request, projection, ack, approval, execution, replay, and rejection decisions append principal-scoped audit events. |
| Tests | Added negative/idempotency/redaction/property-style tests in `test_frontdoor_orchestrator.py` and `test_workflow_selector.py`. |
| Path safety | Added safe artifact ID validation for request/run/step/adapter IDs and negative tests for path traversal. |
| Runtime/schema parity | Bridge `refs` are required and non-empty at runtime and in schema. |

## Related Files

- `organization/runtime/workflows/frontdoor-orchestrator-protocol.md`
- `organization/runtime/workflows/scripts/frontdoor_orchestrator.py`
- `organization/runtime/workflows/scripts/frontdoor_server.py`
- `organization/runtime/workflows/schemas/typed-classification.schema.json`
- `organization/runtime/workflows/tests/test_frontdoor_orchestrator.py`
