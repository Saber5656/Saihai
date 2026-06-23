# Headless Team Runtime Contract

このファイルは ATV headless organization runtime の共通契約である。

## Runtime Boundary

| Area | Contract |
|---|---|
| Hook set | Initial hooks are `SessionStart` and `Stop` only |
| Session metadata | `SessionStart` writes only session pointer metadata |
| Final gate | `Stop` reads task-owned `execution_context` JSON read-only |
| Role execution | Explicit headless CLI worker dispatch |
| Evidence | Task Detail, typed reports, provider evidence, and Vault task record |

## Role Status

| Status | Meaning |
|---|---|
| `status: active` | role can be selected by organization flow |
| `status: reference` | policy/reference role only |
| `status: deprecated` | historical compatibility entry only |

Tool skills such as commit, save, bridge, browser, and Obsidian operations are not organization role instances.

## Dispatch

Directors and TPM may request role work through typed queue payloads or explicit `agent-dispatch` calls. Provider execution is one-shot CLI based:

| Provider | Runtime |
|---|---|
| Anthropic | `claude --print --output-format json` |
| OpenAI | `codex exec --ephemeral --json` |

Fan-out must be represented as independent dispatch items with explicit dependency metadata. Shared file and repository writes require the shared serializer / lock commands.

## Archive

Archive is an explicit command path. It records shutdown evidence and marks the session state archived; lifecycle hooks do not perform shutdown work.
