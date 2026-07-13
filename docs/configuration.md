# Local environment configuration

Saihai reads repository-local configuration from `.env` with a stdlib-only
parser. It never executes the file as shell code.

## Setup

From the primary Saihai checkout:

```sh
python3 scripts/setup_env.py --agents-vault /absolute/path/to/Agents-Vault
python3 scripts/setup_env.py --check
```

Omit `--agents-vault` for an interactive prompt. Optional directories can be
provided with `--user-vault`, `--skills-root`, and `--dotfiles-root`.
The command validates configured directories, creates `.env` with mode `0600`,
and refuses to overwrite an existing file. It does not create credentials,
tokens, keys, or edit shell profiles.

Never commit `.env` or `.env.local`: they contain machine-specific paths and
may later contain sensitive local configuration. Commit only `.env.example`,
which contains no real values.

## Resolution and precedence

The loader resolves one file in this order:

1. process `SAIHAI_ENV_FILE`;
2. process `SAIHAI_ROOT` plus `.env`;
3. current Saihai checkout `.env`;
4. primary checkout `.env` when running from a linked Git worktree;
5. no file.

`SAIHAI_ENV_FILE` is forbidden inside `.env`. For each key, precedence is
`process environment > .env > documented default`; a process variable that is
present but empty still wins. Relative paths are resolved against the
directory containing `.env`; `~` and `${HOME}` are supported.

The parser accepts blank lines, comments, and one `KEY=VALUE` assignment per
line with optional single or double quotes. It rejects `export`, unknown or
duplicate keys, command substitution, backticks, arbitrary `$` expansion, and
internal session keys. Diagnostics contain status codes and key names only.

`AGENTS_VAULT_ROOT` has no personal-path default. Commands requiring the Vault
fail closed when it is missing, empty, absent, not a directory, or not readable
and writable.

## User configuration

| Variable | Type | Required | Purpose |
|---|---|---:|---|
| `AGENTS_VAULT_ROOT` | path | yes | canonical shared Agents-Vault |
| `USER_VAULT_ROOT` | path | no | personal Vault used by applicable roles/monitoring |
| `SKILLS_ROOT` | path | no | external role/skill root; bundled roles are the default |
| `DOTFILES_ROOT` | path | no | ITD monitoring root |
| `SAIHAI_ORCH_STATE_ROOT` | path | no | viewer orchestrator-state override |
| `SAIHAI_ITB_STATE_ROOTS` | path list (`:` on Unix) | no | ITB state roots for derived views |
| `SENSITIVE_ACCESS_GUARD_STATE_ROOT` | path | no | guard state override |
| `AGENT_ORG_STATE` | enum | no | `enabled`, `maintenance`, or `disabled` |

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

Vault-independent exceptions are deliberately narrow: the setup command must
be able to create configuration, the sensitive-access guard must protect tool
calls even before Vault recovery, and the task-state bridge is an importable
library whose parent entry point validates the Vault. User-facing Saihai CLI,
viewer, validation, source sync, ITB, and ITD entry points validate the Vault
before evaluating their configuration-dependent module constants.

## Compatibility aliases

The loader still reads `SAHAI_ROOT` and `AGENT_TEAMS_VIEWER_ROOT` as aliases for
`SAIHAI_ROOT`, `YASU_VAULT_ROOT` for `USER_VAULT_ROOT`, and
`SKILLS_REPO_SKILLS_ROOT` / `SKILLS_REPO_ROOT` for `SKILLS_ROOT`. It emits a
value-free deprecation warning. New configuration must use canonical names.

## Recovery

Before setup, export only `AGENTS_VAULT_ROOT` in the launching process as a
temporary bootstrap measure, or pass `--agents-vault`. After setup, remove the
temporary shell-profile export and run `python3 scripts/setup_env.py --check`.

If validation fails, do not delete or overwrite `.env`. Correct its path or
permissions manually, retain mode `0600`, and rerun `--check`. To recover from
a bad explicit override, unset `SAIHAI_ENV_FILE` so normal checkout discovery
can resume.
