# ITB Hook Wrappers

This directory is the canonical in-repo hook bundle for `infra-team-bootstrap`.
The scripts are thin wrappers around `scripts/itb_bootstrap_builder.py` and pass
hook JSON from stdin through to the builder.

## Scripts

| Script | Builder command |
|---|---|
| `itb-session-start.sh` | `session-start` metadata-only |
| `itb-final-response-guard.sh` | `final-response-guard` execution-context gate |
| `itb-hook-common.sh` | shared wrapper loader |

## Initial Set

Phase0/Phase1 examples register only two hook events.

| Event | Behavior |
|---|---|
| `SessionStart` | Writes only the session-local pointer metadata. |
| `Stop` | Reads the typed execution context and returns a deterministic allow/block verdict. |

The hook bundle does not start role workers, call providers, progress queues, create tasks, or mutate live dotfiles by itself.

## Settings Examples

| File | Target |
|---|---|
| `codex-hooks.example.json` | Example Codex hook registration body |
| `codex-config.example.toml` | Example Codex config fragment |
| `claude-settings-hooks.example.json` | Example Claude settings hook fragment |

## Environment

| Variable | Purpose |
|---|---|
| `ITB_RUNTIME` | `codex` or `claude`. |
| `ITB_STATE_ROOT` | Session state root. |
| `ITB_BUILDER` | Optional override for the builder path. |
| `ITB_PYTHON` | Optional Python executable override. |

## Install Boundary

These files do not modify `~/.codex`, `~/.claude`, or dotfiles by themselves.
The `hook-install` builder command remains dry-run by default and writes only
when `apply: true` is supplied.

Use `hook-health-check` to verify wrapper reachability, builder path resolution,
metadata-only SessionStart evidence, and Stop final gate evidence.
