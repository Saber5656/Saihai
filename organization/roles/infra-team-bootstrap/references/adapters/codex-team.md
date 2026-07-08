# Codex Adapter

Updated: 2026-06-23

## Boundary

Codex adapter is the OpenAI provider path for Sahai headless runtime.

| Concern | Contract |
|---|---|
| Initial hooks | `SessionStart` metadata-only, `Stop` final gate |
| Role execution | `codex exec --ephemeral --json` |
| Output | typed report / provider evidence |
| Config safety | live `~/.codex` settings are not changed without explicit approval |

## Dispatch

Codex role dispatch must be one-shot and evidence-backed. The caller supplies a bounded prompt, allowed context refs, report path, and task metadata. The adapter returns provider session id, effective model, usage, duration, and response text or typed failure.

The adapter must not be used from hook code to progress queues, create tasks, or repair blockers.

## Final Gate

`Stop` reads only the session pointer and task-owned execution context. It returns a small allow/block schema with typed blocker and typed next action.

## Archive

Archive is an explicit state command. It records shutdown evidence and marks the session archived without provider lifecycle side effects.
