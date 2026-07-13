# Organization Knowledge

Sahai (formerly Agent-Teams-Viewer) is the canonical repository for AI
organization operating knowledge. Agents-Vault remains the context, task,
evidence, and work-history store.

Rename note: historical migration records and legacy compatibility aliases may
still use Agent-Teams-Viewer while new user-facing references use Sahai.

## Layout

| Path | Purpose |
|---|---|
| `settings.json` | Organization enablement, fast/strict mode policy, Hook observer policy, provider transport approval policy |
| `policies/` | Mirrored organization policies formerly read from Agents-Vault |
| `roles/<role>/skill.md` | Mirrored Team Role entrypoint formerly held as `skills/<role>/SKILL.md` |
| `roles/<role>/references/`, `roles/<role>/evals/`, `roles/<role>/scripts/` | Role-local skill support artifacts copied with each role when present |
| `runtime/` | Runtime registry and model/startup references |
| `runtime/agent-call-contract.md` | Active `co agent-call` / provider switch manifest and context contract |
| `runtime/workflows/` | Orchestrator P0 workflow contracts, schemas, deterministic selector, and initial workflow template |
| `policy-index.json` | Policy file source, checksum, and byte index |
| `role-index.json` | Role source, checksum, team, and migration-stage index |

## Migration Rule

Team Role skills remain compatibility sources until each role is promoted to
repository-native authority. Role mirrors use a directory layout:
`organization/roles/<role>/skill.md` is the skill entrypoint, and sibling
directories preserve role-local references, evals, scripts, config, and tests.
`tech-security` is repository-native: role sync indexes its Saihai directory and
must not overwrite it from an external skills repository.

`scripts/sync_organization_sources.py` regenerates mirrored role and policy
files. It copies, indexes, and preserves source paths so drift can be audited.

## Execution Modes

| Mode | Meaning |
|---|---|
| `fast` | Lightweight task record plus main-agent execution for very simple work |
| `strict` | Full role dispatch, review, and final evidence flow |
| `maintenance` | Organization repair mode; task record remains required, but strict flow does not block the repair path |

Hooks are observers. They may record and surface state, but they do not hard
block normal agent work when queue/provider/bootstrap components are unhealthy.

## Orchestrator P0 Workflow Contracts

`runtime/workflows/` stores the first typed agent orchestrator contract surface.
The selector consumes typed classification candidates and deterministically
selects from installed templates. A normal front-door prompt can only produce a
draft/proposed activation envelope; explicit `orchestrator-start`, `human_ui`,
or `manual_cli` activation is required before a bounded workflow can be
approved. P0 intentionally excludes provider execution, tmux workers, daemon
mode, and Viewer UI.
