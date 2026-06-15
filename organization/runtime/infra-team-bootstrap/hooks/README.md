# ITB Hook Wrappers

This directory is the canonical in-repo hook bundle for `infra-team-bootstrap`.
The scripts are thin wrappers around `scripts/itb_bootstrap_builder.py` and pass
hook JSON from stdin through to the builder.

## Scripts

| Script | Builder command |
|---|---|
| `itb-session-start.sh` | `session-start --launch-agents` |
| `itb-prompt-preflight.sh` | `prompt-preflight` |
| `itb-final-response-guard.sh` | `final-response-guard` |
| `itb-pretooluse-guard.sh` | `pretooluse-guard` |
| `itb-session-end.sh` | `session-end` |

## Settings Examples

| File | Target |
|---|---|
| `codex-hooks.example.json` | Example `.codex/hooks.json` body for Codex hook registration |
| `codex-config.example.toml` | Example config fragment enabling Codex hooks |
| `claude-settings-hooks.example.json` | Example Claude `settings.json` hook fragment |

The `Stop` / `SubagentStop` examples call `itb-final-response-guard.sh`, not
archive shutdown. Archive shutdown remains an explicit command path.

## Environment

| Variable | Purpose |
|---|---|
| `ITB_RUNTIME` | `codex` or `claude`. Defaults to `codex`, or `claude` when invoked from a `.claude` path. |
| `ITB_STATE_ROOT` | Session state root. Defaults to `$HOME/.codex/state/itb` or `$HOME/.claude/state/itb`. |
| `ITB_BUILDER` | Optional override for the builder path. |
| `ITB_PYTHON` | Optional Python executable override. Defaults to `python3`. |

## Install Boundary

These files do not modify `~/.codex/hooks.json`, Claude settings, or dotfiles by
themselves. Adapter-specific installation should symlink or copy these wrappers
into the paths documented in `references/adapters/` and keep the builder command
output as the source of truth.

For deterministic planning or application, run the builder `hook-install`
command. It defaults to dry-run and only writes when `apply: true` is present in
the hook input. The installer preserves existing hook events, updates only
matching `itb-*.sh` commands, enables `codex_hooks = true` for Codex, and writes
through settings symlinks to their resolved targets. When wrappers are copied
outside this skill directory, installer-managed settings commands include
`ITB_BUILDER` so the copied wrapper does not look for `scripts/` under dotfiles.

After installation, run `hook-health-check` to verify that adapter settings point
at the copied wrappers, include the expected runtime / state root / `ITB_BUILDER`
environment, and still resolve to this builder. With `run_smoke: true`, it runs
the safe Stop and PreToolUse wrappers against a temporary hook payload by
default. To smoke the startup/preflight lifecycle wrappers, pass
`smoke_scripts: ["startup_preflight"]` or equivalent `smoke_events` entries and
set `smoke_state_root`; SessionStart is forced through dry-run launch and
UserPromptSubmit is checked through the controlled micro-flow path. Use
`smoke_state_root` to send those smoke events to a temporary state directory
without touching the configured live session state.
Standalone UserPromptSubmit smoke requires a prior ready SessionStart smoke in
the same `smoke_state_root`; otherwise the health check blocks with
`session_start_smoke_required`.

Use `check_live_evidence: true` when you need a read-only report of whether the
currently configured live state has actually recorded lifecycle events. The
report reads the settings command `ITB_STATE_ROOT`, `last-session`,
`bootstrap.json`, `invocation-evidence.jsonl`, `preflight-events.jsonl`,
`final-response-guard-events.jsonl`, and `pretooluse-guard-events.jsonl` into
`hookHealthCheck.live_evidence`. Missing live evidence is informational by
default. Add `require_live_evidence: true` to make missing required events block
the health check; the default required set is SessionStart and UserPromptSubmit,
or pass `required_live_events` explicitly. For reload or new-session validation,
also pass `max_live_evidence_age_seconds`; stale required events block with
`live_evidence:<event>:stale`, and events without a readable timestamp block with
`live_evidence:<event>:timestamp_unknown`.
