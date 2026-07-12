# Environment variable inventory

This inventory was generated from Python `os.environ` / `os.getenv` calls,
shell variable references, hook examples, README/runbooks, validation scripts,
and both copies of `infra-team-bootstrap`. JavaScript template literals and
ordinary shell locals were excluded after manual review.

| Variable | Classification | Canonical / status | Main consumers | `.env.example` decision |
|---|---|---|---|---|
| `AGENTS_VAULT_ROOT` | 1. install-time required | canonical; fail-closed | ITB, ITD, source sync, policies | included, required |
| `USER_VAULT_ROOT` | 2. optional user setting | canonical | ITB, ITD | included, commented |
| `SKILLS_ROOT` | 2. optional user setting | canonical | ITB roles, source sync | included, commented |
| `DOTFILES_ROOT` | 2. optional user setting | canonical | ITD monitor | included, commented |
| `SAIHAI_ORCH_STATE_ROOT` | 2. optional user setting | canonical | viewer server | included, commented |
| `SAIHAI_ITB_STATE_ROOTS` | 2. optional user setting | canonical path list | workflow task-state bridge | included, commented |
| `SENSITIVE_ACCESS_GUARD_STATE_ROOT` | 2. optional user setting | canonical | sensitive-access guard | included, commented |
| `AGENT_ORG_STATE` | 2. optional user setting | canonical enum | organization classifier | included, commented |
| `SAIHAI_ROOT` | 2. optional user setting | canonical install-root override | loader resolution, ITB | documented; normally auto-discovered |
| `SAHAI_ROOT` | 6. deprecated alias | read-compatible → `SAIHAI_ROOT` | ITB, settings compatibility | excluded; warning |
| `AGENT_TEAMS_VIEWER_ROOT` | 6. deprecated alias | read-compatible → `SAIHAI_ROOT` | historical ITB | excluded; warning |
| `YASU_VAULT_ROOT` | 6. deprecated alias | read-compatible → `USER_VAULT_ROOT` | historical ITB/ITD | excluded; warning |
| `SKILLS_REPO_SKILLS_ROOT` | 6. deprecated alias | read-compatible → `SKILLS_ROOT` | historical ITB/source sync | excluded; warning |
| `SKILLS_REPO_ROOT` | 6. deprecated alias | read-compatible → `SKILLS_ROOT` | historical ITD/settings | excluded; warning |
| `ITB_RUNTIME`, `ITB_STATE_ROOT`, `ITB_BUILDER`, `ITB_BOOTSTRAP_BUILDER`, `ITB_PYTHON` | 4. session/hook internal | process-only; forbidden in `.env` | hook wrappers | excluded |
| `ITB_AGENT_CHILD`, `ITB_PARENT_SESSION_ID`, `ITB_AGENT_ID`, `ITB_FLOW_PHASE`, `ITB_ORGANIZATION_INSTANCE_ID` | 4. session/hook internal | process-only; forbidden in `.env` | provider/role sessions | excluded |
| `SAIHAI_VALIDATE_ALL_CHILD` | 5. test-only | process-only; forbidden in `.env` | `validate_all.py`, e2e harness | excluded |
| `SAIHAI_ALLOW_LIVE_PROVIDERS` | 7. documentation/test only | no production enablement check found | README and validation child env only | excluded; not advertised as available |
| `AGENT_ORG_ENABLED`, `AGENT_ORG_MAINTENANCE` | 7. documentation examples only | code uses CLI flags / `AGENT_ORG_STATE` | README | excluded; migrate examples |
| `CODEX_HOME`, `HOME`, `PATH` | 4. host/session environment | external runtime contract | profiles and subprocess lookup | excluded |
| `SAIHAI_ENV_FILE` | loader bootstrap selector | process-only; forbidden inside `.env` | common loader | excluded |
| `ITB_GATE_ENTRY_*`, provider/model/effort, timeout/poll/wait, transcript, queue, notification, and remaining `ITB_*` tuning keys | 3. advanced runtime tuning | canonical where implemented | ITB builder | excluded; see configuration guide |

## Duplicated tree result

`organization/runtime/infra-team-bootstrap/**` is the executable source and
`organization/roles/infra-team-bootstrap/**` is its mirrored role artifact.
The builder files are kept byte-identical and validation checks both trees.
Internal hook variables remain process-only in both copies.

## Inclusion changes from the initial candidate list

All eight proposed canonical keys remain in `.env.example`. `SAIHAI_ROOT` was
added to the supported schema because it participates in environment-file
resolution, but it is not listed in the example because checkout discovery is
the normal path. No live-provider switch was added because
`SAIHAI_ALLOW_LIVE_PROVIDERS` has no production enablement implementation.

## Exhaustive per-variable classification

The grouped overview above is supplemented by this one-variable-per-row audit
for every remaining runtime, hook, host, test, and documentation reference.

| Variable | Class | Status / consumer |
|---|---:|---|
| `ITB_ACTIVE_EXECUTION_CONTEXT_POINTER` | 3 | advanced path; ITB final gate |
| `ITB_CLAUDE_CLI_DISPATCH_TIMEOUT_SECONDS` | 3 | advanced timeout; ITB provider runner |
| `ITB_CLAUDE_DEFAULT_EFFORT` | 3 | advanced enum; ITB provider runner |
| `ITB_CLAUDE_EFFORT` | 3 | advanced enum; ITB provider runner |
| `ITB_CLAUDE_HAIKU_SONNET_EFFORT` | 3 | advanced enum; ITB provider runner |
| `ITB_CLAUDE_OPUS_EFFORT` | 3 | advanced enum; ITB provider runner |
| `ITB_CLAUDE_SONNET_HAIKU_EFFORT` | 3 | advanced compatibility tuning; ITB provider runner |
| `ITB_CLAUDE_TRANSCRIPT_DISCOVERY_MAX_FILES` | 3 | advanced integer; transcript discovery |
| `ITB_CLAUDE_TRANSCRIPT_STALE_TOLERANCE_SECONDS` | 3 | advanced integer; transcript freshness |
| `ITB_CODEX_APPROVAL_POLICY` | 3 | advanced enum; Codex provider |
| `ITB_CODEX_EXEC_DISPATCH_TIMEOUT_SECONDS` | 3 | advanced timeout; Codex provider |
| `ITB_CODEX_MODEL` | 3 | advanced string; Codex provider |
| `ITB_CODEX_REASONING_EFFORT` | 3 | advanced enum; Codex provider |
| `ITB_CODEX_SERVICE_TIER` | 3 | advanced enum; Codex provider |
| `ITB_FINAL_GATE_HARD_BLOCK` | 3 | advanced bool; final-response guard |
| `ITB_GATE_ENTRY_AUTO_GTC` | 3 | advanced bool; gate entry |
| `ITB_GATE_ENTRY_CODEX_EXEC` | 3 | advanced bool; gate entry |
| `ITB_GATE_ENTRY_DISPATCH` | 3 | advanced bool; gate entry |
| `ITB_GATE_ENTRY_QUEUE` | 3 | advanced bool; gate entry |
| `ITB_GATE_ENTRY_TASK_LIKE_MIN_CHARS` | 3 | advanced integer; gate classifier |
| `ITB_GATE_LATENCY_REPORT_ENRICHMENT_MAX_FILES` | 3 | advanced integer; report enrichment |
| `ITB_INBOX_TERMINAL_KEEP` | 3 | advanced integer; queue pruning |
| `ITB_MICRO_FAST_PATH` | 3 | advanced bool; pre-GPF classifier |
| `ITB_OS_NOTIFICATIONS` | 3 | advanced bool; notification dispatch |
| `ITB_OS_NOTIFICATION_CLASSES` | 3 | advanced list string; notification dispatch |
| `ITB_PREFLIGHT_GATE_SKILL_CONTRACT_LINT` | 3 | advanced bool; preflight lint |
| `ITB_PRE_GPF_MICRO_MAX_CHARS` | 3 | advanced integer; pre-GPF classifier |
| `ITB_PROVIDER_ACTIVATION_MAX_BUDGET_USD` | 3 | advanced number; provider activation |
| `ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS` | 3 | advanced timeout; provider activation |
| `ITB_PROVIDER_ADD_DIRS` | 3 | advanced path list; provider command |
| `ITB_PROVIDER_PERMISSION_MODE` | 3 | advanced enum; provider permission |
| `ITB_PROVIDER_USAGE_TRANSCRIPT_MAX_BYTES` | 3 | advanced integer; transcript limit |
| `ITB_QUEUE_ROOT` | 3 | advanced path; queue override |
| `ITB_QUEUE_WATCH_NUDGE_COOLDOWN_SECONDS` | 3 | advanced number; queue watcher |
| `ITB_REPO_ROOT` | 3 | advanced path; hook repo context |
| `ITB_ROLE_AGENT_IDLE_TIMEOUT_SECONDS` | 3 | advanced number; role worker |
| `ITB_ROLE_AGENT_MAX_MESSAGES` | 3 | advanced integer; role worker |
| `ITB_ROLE_AGENT_POLL_INTERVAL_SECONDS` | 3 | advanced number; role worker |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_EVENT_DRIVEN` | 3 | advanced bool; role queue wait |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_IN_DRY_RUN` | 3 | advanced bool; role queue wait |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_POLL_SECONDS` | 3 | advanced number; role queue wait |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_PROFILE` | 3 | advanced enum; role queue wait |
| `ITB_ROLE_QUEUE_COMPLETION_WAIT_SECONDS` | 3 | advanced number; role queue wait |
| `ITB_ROLE_QUEUE_DRY_RUN` | 3 | advanced bool; role queue |
| `ITB_TASK_DETAIL_LINE_CAP` | 3 | advanced bounded integer; task detail |
| `ITB_TASK_DETAIL_PATH` | 3 | advanced path; task detail override |
| `ITB_RUNTIME` | 4 | hook-set internal |
| `ITB_STATE_ROOT` | 4 | hook-set internal |
| `ITB_BUILDER` | 4 | hook-set internal |
| `ITB_BOOTSTRAP_BUILDER` | 4 | hook-set internal compatibility name |
| `ITB_PYTHON` | 4 | hook-set internal |
| `ITB_AGENT_CHILD` | 4 | session-set internal |
| `ITB_PARENT_SESSION_ID` | 4 | session-set internal |
| `ITB_AGENT_ID` | 4 | session-set internal |
| `ITB_FLOW_PHASE` | 4 | session-set internal |
| `ITB_ORGANIZATION_INSTANCE_ID` | 4 | session-set internal |
| `CODEX_HOME` | 4 | host runtime; profile launcher |
| `HOME` | 4 | host runtime; supported only for path expansion/defaults |
| `PATH` | 4 | host runtime; subprocess executable lookup |
| `SAIHAI_ENV_FILE` | 4 | process-only loader selector; forbidden in `.env` |
| `SAIHAI_VALIDATE_ALL_CHILD` | 5 | validation child marker |
| `SAHAI_ROOT` | 6 | deprecated alias of `SAIHAI_ROOT` |
| `AGENT_TEAMS_VIEWER_ROOT` | 6 | deprecated alias of `SAIHAI_ROOT` |
| `YASU_VAULT_ROOT` | 6 | deprecated alias of `USER_VAULT_ROOT` |
| `SKILLS_REPO_SKILLS_ROOT` | 6 | deprecated alias of `SKILLS_ROOT` |
| `SKILLS_REPO_ROOT` | 6 | deprecated repo-root alias; migrated to `<value>/skills` |
| `SAIHAI_ALLOW_LIVE_PROVIDERS` | 7 | validation/docs only; no production enablement implementation |
| `AGENT_ORG_ENABLED` | 7 | README-only historical example |
| `AGENT_ORG_MAINTENANCE` | 7 | README-only historical example |
