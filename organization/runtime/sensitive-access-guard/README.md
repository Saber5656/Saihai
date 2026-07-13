# Sensitive Access Guard

This guard blocks tool input that references secret-bearing files or names and
latches the current Codex or Claude Code session. It is intentionally strict:
`.env.example`, public-key files, SSH configuration, and token templates are
blocked alongside live secrets.

## Deny policy

| Class | Examples |
|---|---|
| Environment files | `.env`, `.env.local`, `.env.example`, any `.env.*`, and local `directory-path.env` |
| Tokens and auth | `token`, `gh-token`, `github_pat`, `oauth.json`, `access_token`, `auth.json`, credentials, passwords, passphrases, and secrets |
| SSH / auth directories | any path containing `.ssh`, `.gnupg`, `.aws`, `.azure`, or `.kube` |
| Keys | `id_rsa`, `id_ed25519`, `private_key`, `service-key`, `keys.json`, `api-key`, `*.key`, `*.pem`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx` |
| Other auth stores | `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.config/gh/hosts.yml`, `.config/gcloud/*`, `.docker/*`, `authorized_keys`, `known_hosts` |

The guard applies to reads, writes, edits, copies, searches, and commands. It
does not open a referenced file and never stores the attempted command or path.
The latch stores only a reason class and a SHA-256 hash of the session ID.

## Runtime configuration

Use `codex-hooks.example.json` for Codex and
`claude-settings.example.json` for Claude Code. Merge them with existing
settings instead of replacing unrelated hooks or permission rules. Do not
activate a live hook until the policy and runtime-specific configuration have
been reviewed.

Codex must use the canonical `[features] hooks = true` setting. Claude Code
also needs the example's Read deny rules because `@file` references do not
trigger `PreToolUse`. These rules are intentionally broad and have no
example/template exception.

The hook matcher is deliberately `.*` so Bash, file tools, and MCP tools are
evaluated. `UserPromptSubmit` is also registered: after a latch is set, later
prompts are rejected until a human clears it.

For explicit path fields, the guard resolves an existing symlink without
opening its target and evaluates both the supplied name and resolved target.
For Bash, broad indirect read forms such as read commands with globs, shell
variables, command substitution, `find -exec`, or `xargs` are denied. This is
deliberately conservative. Copy/archive tools including `cp`, `tar`, `zip`,
`rsync`, `scp`, and similar commands are subject to the same indirect-path
check. The taxonomy still cannot model every custom program that may read
files.

## Human unlock

Run from a terminal outside the blocked agent session:

```sh
python3 ~/dev/Saihai/organization/runtime/sensitive-access-guard/sensitive_access_guard.py \
  --runtime codex --clear --session-id '<session-id>'
```

Use `--runtime claude` for Claude Code. Clearing a latch is an explicit human
operation; the agent must not invoke this command for itself.

Because the hook and agent run as the same OS user, this human-only rule is a
procedural boundary, not a cryptographic identity check. The latched session
cannot call tools or submit another prompt through the guarded runtime, but a
separate same-user process could still alter the state. A separate supervisor
or OS privilege boundary is required to eliminate that residual risk.

## Fail-closed behavior

Malformed JSON, a missing session ID, state-write failure, and internal policy
errors exit with status 2. Supported `PreToolUse` command hooks interpret that
as a denial. A successful policy denial returns structured JSON understood by
both runtimes.

The state directory must be owned by the current user, be a real directory
rather than a symlink, and have mode `0700`. Per-session lock files must be
regular, owned by the current user, and mode `0600`. Any mismatch fails closed.

## Enforcement boundary

This is a guardrail, not an OS security boundary.

- Codex currently documents incomplete interception for richer `unified_exec`
  shell handling and does not intercept every non-shell or non-MCP tool.
- Claude Code does not send prompt-level `@file` references through
  `PreToolUse`; runtime Read deny rules are required as a second layer.
- A process that already holds a secret in memory or environment variables is
  outside this file-reference guard.
- Encoded or programmatically constructed paths and custom file-reading
  programs can evade lexical inspection unless the OS sandbox blocks the
  underlying path.
- OS sandboxing and least-privilege filesystem permissions remain required for
  complete enforcement.

## Verification

Run:

```sh
python3 -m unittest organization/runtime/sensitive-access-guard/test_sensitive_access_guard.py
```
