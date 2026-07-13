# Directory path configuration

Saihai keeps machine-specific directory locations in the primary checkout's
`directory-path.env`. The file is parsed as data by `directory_paths.py`; it is
never executed as shell code.

## Files

| File | Git | Purpose |
|---|---|---|
| `directory-path.env` | ignored | Local source of truth with concrete paths |
| `directory-path.env.example` | tracked | Public variable catalog without local values |
| `.directory-path.md` | tracked | Human-readable alias and compatibility reference |

The local file should use mode `0600`. Do not place credentials, tokens, or
other non-path configuration in it.

Non-path runtime options remain in the separately ignored `.env` file and are
validated by `saihai_env.py`. Directory keys are rejected from `.env`; this
keeps `directory-path.env` as the only file-backed source of directory aliases.
Use `scripts/setup_env.py` only for optional runtime settings.
The complete advanced option table and internal-variable notes remain in
[Non-path runtime configuration](runtime-configuration.md); the preserved
audit is in
[Historical runtime environment audit](runtime-environment-variable-inventory.md).

## Setup and validation

Create the file in the primary checkout, or validate an existing file:

```sh
python3 scripts/setup_directory_paths.py \
  --agents-vault /absolute/path/to/Agents-Vault \
  --user-vault /absolute/path/to/personal-vault \
  --skills-repo-root /absolute/path/to/skills-repo \
  --skills-root /absolute/path/to/skills \
  --dotfiles-root /absolute/path/to/dotfiles \
  --dev-root /absolute/path/to/dev \
  --dev-worktrees-root /absolute/path/to/dev/worktrees \
  --task-worktree-root /absolute/path/to/dev/worktrees

python3 scripts/setup_directory_paths.py --check
```

The setup command refuses to overwrite an existing file.

## Resolution and precedence

The loader locates the catalog in this order:

1. path supplied by process variable `SAHAI_DIRECTORY_PATH_ENV`;
2. `directory-path.env` below process `SAHAI_ROOT` or legacy `SAIHAI_ROOT`;
3. `directory-path.env` in the current Saihai checkout;
4. `directory-path.env` in the primary checkout of a linked Git worktree;
5. no file.

For each path, precedence is `process environment > directory-path.env`.
An explicitly empty process value still wins. This allows an individual test
or subprocess to override one path without copying the entire catalog.

The primary-checkout lookup is what lets code running in another Saihai
worktree, branch, or project reuse the same local path source of truth.

## Format and safety

The parser accepts blank lines, comments, and one `KEY=VALUE` assignment per
line. Single- and double-quoted values are supported. Relative paths are
resolved from the directory containing the catalog; `~` and `${HOME}` are
supported.

The parser rejects unknown and duplicate keys, `export`, command substitution,
backticks, arbitrary shell expansion, and `SAHAI_DIRECTORY_PATH_ENV` inside the
catalog. Diagnostics expose key names and error classes, not configured path
values.

Normal Saihai entrypoints fail closed unless all nine canonical directory paths
listed as required below exist and are readable. `AGENTS_VAULT_ROOT` must also
be writable. Bootstrap-only consumers may load the catalog without enforcing
the complete contract so that recovery remains possible.

## Canonical variables

| Variable | Requirement | Purpose |
|---|---|---|
| `SAHAI_ROOT` | required | Primary Saihai checkout and catalog locator |
| `AGENTS_VAULT_ROOT` | required, read/write | Shared organization Vault |
| `USER_VAULT_ROOT` | required | Personal Obsidian Vault |
| `SKILLS_REPO_ROOT` | required | Skills source repository |
| `SKILLS_ROOT` | required | Skill definitions consumed by agents |
| `DOTFILES_ROOT` | required | Local agent/runtime configuration repository |
| `DEV_ROOT` | required | Local source repositories |
| `DEV_WORKTREES_ROOT` | required | Standard development worktrees |
| `TASK_WORKTREE_ROOT` | required | Task-specific worktrees |
| `SAHAI_ORCH_STATE_ROOT` | optional | Optional orchestrator state override |
| `SAHAI_ITB_STATE_ROOTS` | optional | Optional path-list override for ITB state roots |
| `SENSITIVE_ACCESS_GUARD_STATE_ROOT` | optional | Optional guard state override |

## Compatibility aliases

The loader accepts legacy process names, maps them to canonical names, and
emits value-free deprecation diagnostics.

| Legacy name | Canonical name |
|---|---|
| `SAIHAI_ROOT` | `SAHAI_ROOT` |
| `AGENT_TEAMS_VIEWER_ROOT` | `SAHAI_ROOT` |
| `YASU_VAULT_ROOT` | `USER_VAULT_ROOT` |
| `SKILLS_REPO_SKILLS_ROOT` | `SKILLS_ROOT` |
| `DEV_REPO_ROOT` | `DEV_ROOT` |
| `SAIHAI_ORCH_STATE_ROOT` | `SAHAI_ORCH_STATE_ROOT` |
| `SAIHAI_ITB_STATE_ROOTS` | `SAHAI_ITB_STATE_ROOTS` |

New code and configuration must use canonical names.

## Path aliases in configuration

`expand_path_aliases()` expands only known catalog variables such as
`${AGENTS_VAULT_ROOT}` and `${SKILLS_ROOT}`. It does not run general shell
expansion. ITB path configuration uses this common expansion instead of its
former hand-maintained replacement table.

## Recovery

If the catalog is invalid, correct it manually and run:

```sh
python3 scripts/setup_directory_paths.py --check
```

To recover from an invalid explicit selector, unset
`SAHAI_DIRECTORY_PATH_ENV` so normal primary-checkout discovery can resume.
