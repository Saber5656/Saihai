# Main-Agent Enforcement Runbook

This runbook describes the static enforcement profile for Saihai
orchestrator-frontend sessions.

It applies only to dedicated main-agent sessions whose job is to submit,
inspect, and acknowledge frontdoor bridge requests. It does not apply to normal
development sessions.

## What It Enforces

| Area | Enforcement |
|---|---|
| Scope | Orchestrator-frontend main-agent sessions only |
| Mutation tools | Denied in Claude; read-only sandbox in Codex |
| Normal bridge flow | Allowed without approval for supported frontdoor/bridge commands |
| Other shell commands | Ask mode; the human approves or denies |
| Bypass modes | Refused by the launcher and detected by canary |
| Hooks | Observer/advisory only; not the blocking authority |

This is not the R57 action gateway. R57 will provide dynamic,
work-order-scoped tool grants. This profile is a static v0.1.0 precursor that
keeps frontend sessions from doing direct work.

## Start Method

The launcher is the only supported start method:

```sh
organization/runtime/workflows/profiles/saihai-frontend-session.sh
organization/runtime/workflows/profiles/saihai-frontend-session.sh --codex
```

Do not start `claude` or `codex` by hand for enforced frontend sessions. Manual
flags are unsupported because they can bypass or replace the intended profile.

## Profile Files

| File | Purpose |
|---|---|
| `organization/runtime/workflows/profiles/claude-main-agent.settings.example.json` | Claude Code deny/allow settings |
| `organization/runtime/workflows/profiles/codex-main-agent.config.example.toml` | Codex profile example with read-only permissions and approval prompts |
| `organization/runtime/workflows/profiles/codex-main-agent.rules.example` | Codex rules that allow the bridge/frontdoor prefixes |
| `organization/runtime/workflows/profiles/saihai-frontend-session.sh` | Launcher that refuses bypass flags and pins runtime settings |
| `organization/runtime/workflows/profiles/verify_enforcement.md` | Canary procedure |

The files were authored against Claude Code `2.1.172` and Codex CLI
`0.141.0`. Re-check `claude --help`, `codex --help`, and the Codex manual when
upgrading either tool.

## Codex State-Root Policy

Codex execpolicy rules do not interpret `"*"` as an argument wildcard. The
frontend profile therefore auto-allows bridge commands for exactly two
state-root forms:

| Form | Behavior |
|---|---|
| No `--state-root` argument | Uses the runtime default `~/.codex/state/itb/frontdoor-orchestrator`; bridge submit/read/ack are allowed |
| `--state-root /tmp/saihai-frontdoor-canary` | Uses the fixed disposable canary root; bridge submit/read/ack are allowed |

Any other explicit state root is deliberately unmatched and falls through to
the normal human ask/deny boundary. Do not add wildcard-like path tokens to the
rules file. Operators that need another disposable root must use a normal
maintenance session, not broaden the enforced frontend profile.

## Three-Tier Behavior

| Tier | Behavior | Examples |
|---|---|---|
| Allow | Runs without an approval prompt | bridge submit/read/ack commands |
| Ask | Human approval prompt appears | non-bridge shell commands not denied by profile |
| Deny | Refused without asking | Edit/write tools, `git status`, sensitive reads, bypass flags, direct sandbox/profile overrides |

The goal is low friction only on the supported bridge path. Deviations stop at
the human approval boundary or are refused outright.

## Bypass Prohibition

Never use these for enforced frontend sessions:

| Surface | Forbidden |
|---|---|
| Claude | `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--permission-mode bypassPermissions`, `--permission-mode dontAsk`, `--settings`, `--allowedTools` / `--allowed-tools` |
| Codex | `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `--sandbox` / `-s`, `--ask-for-approval never` / `-a never`, `--config` / `-c`, `--profile` / `-p` |

The launcher refuses those flags before starting the session. The canary then
checks that the resulting session is not in a bypass mode.

## Canary Procedure

Run the canary before submitting any bridge request:

| Step | Action | Expected | If violated |
|---|---|---|---|
| 1 | Ask the session to edit a scratch file. | The mutation tool is refused. | Profile not loaded or bypass mode active. Terminate the session immediately. |
| 2 | Ask the session to run `python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/saihai-frontdoor-canary bridge-read-projection --request-id req-canary`. | It reaches the bridge path without an approval prompt and returns a typed missing/blocked response if no projection exists. | Allowlist is broken. Fix the profile/rules before use. |
| 3 | Ask the session to run `git status`. | An approval prompt appears or the command is explicitly refused by the profile. | Default ask/deny enforcement is not active. Terminate the session immediately. |

Positive bypass detector: if step 1 succeeds silently, the session is not
enforced.

## Limits

- This profile does not grant edit, commit, push, provider-dispatch, or network
  authority.
- This profile does not allow direct proposal approval/status polling or broad
  sensitive file reads from frontend sessions.
- This profile does not auto-approve arbitrary explicit state roots.
- This profile does not prove that every future CLI version preserves the same
  settings semantics. Re-run static tests and canary checks after upgrades.
- This profile does not replace PR review, final-gate checks, Vault evidence,
  or release approval.
- This profile does not use hook-based blocking.

## P1 / R57 Relation

The post-v0.1.0 action gateway should replace this static profile with dynamic
tool grants scoped to approved work orders. Until then, orchestrator-frontend
sessions are intentionally narrow: bridge requests in, redacted projections
out, and no direct work.
