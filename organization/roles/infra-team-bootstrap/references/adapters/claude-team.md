# Claude Adapter

Updated: 2026-06-23

## Boundary

Claude adapter は Sahai headless runtime の一 provider adapter であり、hook lifecycle の owner ではない。

| Concern | Contract |
|---|---|
| Initial hooks | `SessionStart` metadata-only, `Stop` final gate |
| Role execution | `claude --print --output-format json` |
| Output | typed report / provider evidence |
| Config safety | live `~/.claude` settings are not changed without explicit approval |

## Dispatch

Claude role dispatch must be one-shot and evidence-backed. The caller supplies a bounded prompt, allowed context refs, report path, and task metadata. The adapter returns provider session id, effective model, usage, duration, and response text or typed failure.

The adapter must not be used from hook code to progress queues, create tasks, or repair blockers.

## Archive

Archive is an explicit state command. It records shutdown evidence and marks the session archived without provider lifecycle side effects.
