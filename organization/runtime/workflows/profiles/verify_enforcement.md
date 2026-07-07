# Saihai Frontend Session Enforcement Canary

Run this canary at the start of every orchestrator-frontend main-agent session
before submitting any request through the bridge.

Supported launcher:

```sh
organization/runtime/workflows/profiles/saihai-frontend-session.sh
organization/runtime/workflows/profiles/saihai-frontend-session.sh --codex
```

Do not start enforced sessions by invoking `claude` or `codex` directly.

| Step | Action | Expected | If violated |
|---|---|---|---|
| 1 | Ask the session to edit a scratch file. | The mutation tool is refused. | Profile not loaded or bypass mode active. Terminate the session immediately. |
| 2 | Ask the session to run `python3 scripts/saihai.py frontdoor --help`. | It runs without an approval prompt. | Allowlist is broken. Fix the profile/rules before use. |
| 3 | Ask the session to run `git status`. | An approval prompt appears. | Default ask mode is not active. Terminate the session immediately. |

Bypass detector: if step 1 succeeds silently, the session is not enforced. Do
not submit bridge requests from that session.

Claude-specific forbidden launch flags:

- `--dangerously-skip-permissions`
- `--allow-dangerously-skip-permissions`
- `--permission-mode bypassPermissions`
- `--permission-mode dontAsk`

Codex-specific forbidden launch flags:

- `--dangerously-bypass-approvals-and-sandbox`
- any `--sandbox` or `-s` override
- `--ask-for-approval never` or `-a never`
- `--config` / `-c` overrides
- `--profile` / `-p` overrides

Hooks may log enforcement attempts for audit, but hooks must not be treated as
the blocking authority. Blocking authority stays with the permission profile,
read-only sandbox, and launcher flag gate.
