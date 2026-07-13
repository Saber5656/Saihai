# Non-path runtime configuration

Saihai keeps optional non-path runtime tuning in the ignored `.env` file.
Directory locations are not accepted here; configure them through
`directory-path.env` as described in [configuration.md](configuration.md).

The runtime loader resolves process `SAIHAI_ENV_FILE`, then `.env` in the
current or primary checkout. Process values, including explicit empty values,
take precedence. The parser rejects path keys, unknown and duplicate keys,
`export`, command substitution, backticks, and shell expansion.

Create an owner-only template with `python3 scripts/setup_env.py`, or validate
one with `python3 scripts/setup_env.py --check`. `AGENT_ORG_STATE` and the
non-path options below remain supported. Path/session options shown as derived
inputs are process-only.

## Advanced configuration

These settings are intentionally absent from `.env.example`. Defaults are
owned by `itb_bootstrap_builder.py`; ordinary installations should not change
them.

Each supported advanced key has its own row.
“Unset/derived” is the documented default when the consumer derives the value
from hook input, state, or another setting.

| Variable | Type / allowed values | Default | Use location | Normally unchanged? |
|---|---|---|---|---:|
| `ITB_ACTIVE_EXECUTION_CONTEXT_POINTER` | path | unset/derived | builder `final_response_guard` | yes |
| `ITB_CLAUDE_CLI_DISPATCH_TIMEOUT_SECONDS` | positive integer seconds | `ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS`, then `120` | builder Claude CLI runner | yes |
| `ITB_CLAUDE_DEFAULT_EFFORT` | `low`, `medium`, `high`, `max` | `medium` | builder effort resolver | yes |
| `ITB_CLAUDE_EFFORT` | `low`, `medium`, `high`, `max` | model-specific resolver | builder effort resolver | yes |
| `ITB_CLAUDE_HAIKU_SONNET_EFFORT` | `low`, `medium`, `high`, `max` | `medium` | builder effort resolver | yes |
| `ITB_CLAUDE_OPUS_EFFORT` | `low`, `medium`, `high`, `max` | `max` | builder effort resolver | yes |
| `ITB_CLAUDE_SONNET_HAIKU_EFFORT` | `low`, `medium`, `high`, `max` | `medium` | builder compatibility effort resolver | yes |
| `ITB_CLAUDE_TRANSCRIPT_DISCOVERY_MAX_FILES` | positive integer | `24` | builder transcript discovery | yes |
| `ITB_CLAUDE_TRANSCRIPT_STALE_TOLERANCE_SECONDS` | non-negative integer seconds | `300` | builder transcript freshness | yes |
| `ITB_CODEX_APPROVAL_POLICY` | `untrusted`, `on-failure`, `on-request`, `never` | `never` | builder Codex runner | yes |
| `ITB_CODEX_EXEC_DISPATCH_TIMEOUT_SECONDS` | positive integer seconds | provider timeout, then `120` | builder Codex exec runner | yes |
| `ITB_CODEX_MODEL` | non-empty string | `gpt-5.5` | builder Codex runner | yes |
| `ITB_CODEX_REASONING_EFFORT` | `minimal`, `low`, `medium`, `high`, `xhigh` | `xhigh` | builder Codex runner | yes |
| `ITB_CODEX_SERVICE_TIER` | `auto`, `default`, `flex`, `fast` | `fast` | builder Codex runner | yes |
| `ITB_FINAL_GATE_HARD_BLOCK` | bool | `false` | builder final-response guard | yes |
| `ITB_GATE_ENTRY_AUTO_GTC` | bool | `true` | builder gate-entry routing | yes |
| `ITB_GATE_ENTRY_CODEX_EXEC` | bool | `true` | builder gate-entry routing | yes |
| `ITB_GATE_ENTRY_DISPATCH` | bool | `false` | builder gate-entry routing | yes |
| `ITB_GATE_ENTRY_QUEUE` | bool | `true` | builder gate-entry routing | yes |
| `ITB_GATE_ENTRY_TASK_LIKE_MIN_CHARS` | positive integer | `24` | builder gate classifier | yes |
| `ITB_GATE_LATENCY_REPORT_ENRICHMENT_MAX_FILES` | positive integer | `64` | builder latency enrichment | yes |
| `ITB_INBOX_TERMINAL_KEEP` | integer `0..10000` | `50` | builder inbox pruning | yes |
| `ITB_MICRO_FAST_PATH` | bool | `true` | builder pre-GPF classifier | yes |
| `ITB_OS_NOTIFICATIONS` | bool | `false` | builder notification dispatch | yes |
| `ITB_OS_NOTIFICATION_CLASSES` | comma-separated class names | `flow_alert,approval_wait` | builder notification dispatch | yes |
| `ITB_PREFLIGHT_GATE_SKILL_CONTRACT_LINT` | bool | `true` | builder preflight lint | yes |
| `ITB_PRE_GPF_MICRO_MAX_CHARS` | positive integer | `140` | builder pre-GPF classifier | yes |
| `ITB_PROVIDER_ACTIVATION_MAX_BUDGET_USD` | non-negative number | unset | builder activation budget | yes |
| `ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS` | positive integer seconds | `120` | builder provider runners | yes |
| `ITB_PROVIDER_ADD_DIRS` | path list (`:` on Unix) | empty | builder provider command | yes |
| `ITB_PROVIDER_PERMISSION_MODE` | `acceptEdits`, `auto`, `default`, `plan` | `auto` | builder provider permission resolver | yes |
| `ITB_PROVIDER_USAGE_TRANSCRIPT_MAX_BYTES` | positive integer bytes | `2097152` | builder transcript loader | yes |
| `ITB_QUEUE_ROOT` | path | state-root-derived | builder queue resolver | yes |
| `ITB_QUEUE_WATCH_NUDGE_COOLDOWN_SECONDS` | number `0..3600` seconds | `60` | builder queue watcher | yes |
| `ITB_REPO_ROOT` | path | hook-input/working repo | builder repo resolver | yes |
| `ITB_ROLE_AGENT_IDLE_TIMEOUT_SECONDS` | number `0..86400` | `0` | builder role worker | yes |
| `ITB_ROLE_AGENT_MAX_MESSAGES` | non-negative integer (`0` means unlimited) | `1` | builder role worker | yes |
| `ITB_ROLE_AGENT_POLL_INTERVAL_SECONDS` | number `0.1..60` | `2` | builder role worker | yes |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_EVENT_DRIVEN` | bool | profile value (`true`) | builder role queue wait | yes |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_IN_DRY_RUN` | bool | `false` | builder role queue wait | yes |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_POLL_SECONDS` | number `0..30` | profile value; fallback `0.25` | builder role queue wait | yes |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_PROFILE` | `off`, `none`, `hook_light`, `daemon_assisted`, `live_validation` | `off` | builder role queue wait | yes |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_SECONDS` | number `0..300` | profile value; fallback `0` | builder role queue wait | yes |
| `ITB_ROLE_QUEUE_DRY_RUN` | bool | `false` | builder queue dispatch | yes |
| `ITB_TASK_DETAIL_LINE_CAP` | integer `80..2000` | `220` | builder `task_detail_line_cap` | yes |
| `ITB_TASK_DETAIL_PATH` | path | task-state-derived | builder task-detail resolver | yes |

Internal variables such as `ITB_RUNTIME`, `ITB_STATE_ROOT`, `ITB_BUILDER`,
`ITB_AGENT_ID`, and `ITB_PARENT_SESSION_ID` are set dynamically by hooks or
sessions and are rejected in `.env`.

Vault and directory validation is owned by `directory_paths.py`. The runtime
loader runs after that catalog loader at normal entrypoints. Bootstrap-only
guard and bridge consumers retain their narrow fail-closed behavior.

## Path aliases

Path aliases are rejected from runtime `.env`. Canonical path names and legacy
read aliases are documented in [configuration.md](configuration.md) and must
be configured through `directory-path.env` or the process environment.

## Recovery

For directory or Vault recovery, follow the bootstrap procedure in
[configuration.md](configuration.md) and run
`python3 scripts/setup_directory_paths.py --check`.

For non-path runtime settings, do not delete or overwrite `.env`. Correct the
invalid key or value, retain mode `0600`, and run
`python3 scripts/setup_env.py --check`. To recover from a bad explicit runtime
selector, unset `SAIHAI_ENV_FILE` so normal checkout discovery can resume.
