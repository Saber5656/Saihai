# Directory path variable inventory

This inventory is the tracked reference for paths that Saihai may resolve from
`directory-path.env`.

| Variable | Status | Main consumers | Catalog |
|---|---|---|---|
| `SAIHAI_ROOT` | canonical | loader, organization settings | required |
| `AGENTS_VAULT_ROOT` | canonical, fail-closed | ITB, ITD, source sync, policies | required |
| `USER_VAULT_ROOT` | canonical | ITB, ITD | required |
| `SKILLS_REPO_ROOT` | canonical | settings, repository references | required |
| `SKILLS_ROOT` | canonical | ITB roles, source sync | required |
| `DOTFILES_ROOT` | canonical | ITD monitoring | required |
| `DEV_ROOT` | canonical | managed repository policy | required |
| `DEV_WORKTREES_ROOT` | canonical | standard worktree planning | required |
| `TASK_WORKTREE_ROOT` | canonical | task worktree planning | required |
| `SAIHAI_ORCH_STATE_ROOT` | optional | viewer | optional |
| `SAIHAI_ITB_STATE_ROOTS` | optional path list | workflow state views | optional |
| `SENSITIVE_ACCESS_GUARD_STATE_ROOT` | optional | sensitive-access guard | optional |
| `SAHAI_ROOT` | legacy alias | historical consumers | excluded from example |
| `AGENT_TEAMS_VIEWER_ROOT` | legacy alias | historical consumers | excluded from example |
| `YASU_VAULT_ROOT` | legacy alias | historical consumers | excluded from example |
| `SKILLS_REPO_SKILLS_ROOT` | legacy alias | historical consumers | excluded from example |
| `DEV_REPO_ROOT` | legacy alias | historical policy text | excluded from example |
| `SAHAI_ORCH_STATE_ROOT` | legacy alias | viewer compatibility | excluded from example |
| `SAHAI_ITB_STATE_ROOTS` | legacy alias | workflow compatibility | excluded from example |
| `SAIHAI_DIRECTORY_PATH_ENV` | process-only catalog selector | loader bootstrap | forbidden in catalog |
| `SAHAI_DIRECTORY_PATH_ENV` | legacy process-only selector alias | loader compatibility | forbidden in catalog |

Runtime/session variables such as `ITB_STATE_ROOT`, `ITB_QUEUE_ROOT`, provider
paths, and hook-owned paths are deliberately process-only because they are not
stable directory aliases. Non-path model choices, timeouts, feature flags, and
organization state remain supported in `.env` through `saihai_env.py`.

`organization/runtime/infra-team-bootstrap/**` is executable source and
`organization/roles/infra-team-bootstrap/**` is its mirrored role artifact.
The builder copies must remain byte-identical.
