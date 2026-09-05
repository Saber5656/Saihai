# Repository delivery configuration, version 1

`scripts/delivery_contract.py` validates configuration for a repository-owned
delivery profile. The seven JSON files in `profiles/delivery/` are **synthetic
configuration examples**, with synthetic runtime, dependency and action hashes.
They are not executable CI workflows, verified downloads, adopted repository
policies, or evidence of tests passing. Do not use their hashes for a real build.

Configuration validation and repository adoption are separate. A profile must
be adopted by the repository's trusted policy owner before feature delivery can
claim readiness. No such adoption producer is implemented by this module.
Agent-authored `approved`, `waiver`, or other undeclared fields are rejected.
Passing this validator never grants credentials, execution, publication, or
merge authority. Release owners, environment names and artifact-source fields
are requirements to be checked by downstream trusted systems, not attestations
that those systems already enforce them.

## Public Python interface

| Function | Result | Interpretation |
|---|---|---|
| `validate_profile(profile)` | List of deterministic, path-qualified errors | Empty means configuration is structurally and semantically valid |
| `assess_profile(profile)` | Versioned assessment described below | Configuration receipt; never execution authorization |

Input is a decoded JSON object. The caller must reject duplicate JSON keys when
decoding bytes; a Python dictionary cannot preserve duplicate-key evidence.
The validator does not read files, fetch references, run commands, or resolve
secrets. It rejects unknown fields at each object boundary, wrong types,
unbounded collections, malformed references and missing required relationships.

An assessment contains exactly these fields:

| Field | Contract |
|---|---|
| `assessment_version` | String `1` |
| `configuration` | `valid` or `invalid` |
| `profile_digest` | SHA-256 of valid canonical profile JSON; `null` for invalid input |
| `readiness` | `pending_policy_adoption` for valid input; `blocked` for invalid input |
| `authorizes_execution` | Always `false` |
| `errors` | Configuration error strings, empty for valid input |
| `pending` | Trusted repository-policy adoption, actual workflow parity and actual run evidence |

Canonical JSON uses sorted keys, compact separators, ASCII escaping and rejects
NaN. Array order is preserved. Changing any valid profile value changes the
digest; reordering dictionary keys does not. The input is not mutated.
The digest identifies content. It does not authenticate the author or prove
human approval. Consumers must bind repository identity and independently
trusted adoption to this exact digest. Copying a valid receipt is not adoption.

## Closed profile structure

All listed object fields are mandatory; optional states are explicit values
such as `environment: null`, an empty credentials list or disabled cache.
Unknown fields are rejected. Text is nonempty, trimmed and control-character
free. Identifiers have at most 96 characters. Generic text has at most 512
characters; references have additional format constraints.

| Object | Fields and constraints |
|---|---|
| Root | `profile_version: "1"`, `profile_id`, `repository: owner/name`, `project_type`, `runtimes`, `dependencies`, `layers`, `jobs`, `release` |
| Runtime (1–16) | Unique `name`, exact three-component `version` (optional prerelease), immutable artifact `sha256` (64 lowercase hex) |
| Dependencies | `mode: locked/none`, nonempty `reason`, `lockfiles` (0–32) |
| Lockfile | Unique repository-relative `path` without `.` or `..` segments; exact content `sha256` |
| Layer | `required` boolean, nonempty applicability `reason`; all five layers must be present |
| Job (1–64) | Unique `id` and stable `check_name`, `layer`, `required`, declared `runtime`, `trust`, `events`, `branches`, `source`, `permissions`, `credentials`, `environment`, `actions`, `timeout_minutes`, `concurrency`, `cache`, `evidence_retention_days` |
| Action (0–32/job) | Unique `repository` (including optional action subdirectory), immutable 40-hex `commit` |
| Concurrency | `group` containing `{repository}`, `{job}`, `{trust}`, `{ref}`; boolean `cancel_in_progress` |
| Cache | Boolean `enabled`, trust `namespace`, `key`, `restore_keys` (0–8); every key includes `{trust}`, `{runtime_digest}`, `{lock_digest}` |
| Release | `owner: owner/team`, `targets` (1–16), `rollback_prerequisites` (1–16 nonempty statements) |
| Target | Unique `name`, reference to a privileged publication `job`; every publication job must have a target |

The token notation expresses isolation requirements for a future workflow
mapping; it is not GitHub Actions expression syntax. The parity consumer must
verify the rendered workflow actually preserves the declared isolation. Even
disabled caches must declare safe keys so enabling one does not remove the
trust contract. The `none` dependency mode explicitly requires an empty
lockfile inventory and a reason, for example a standard-library-only project.
A locked dependency mode requires at least one lockfile. Hashes are checked for
format here; actual file/reference content equality belongs to parity checks.

### Required coverage

| Project type | Required layers |
|---|---|
| Web | static, unit, feature, E2E |
| API | static, unit, feature, E2E |
| Mobile | static, unit, feature, E2E, device |
| Library | static, unit, feature |
| CLI | static, unit, feature, E2E |
| IaC | static, unit, feature |
| Docs | static, feature |

These version-1 representative minimums cannot be downgraded in a profile.
Additional layers can be required. Every required layer must have a required,
unprivileged job. An omitted layer remains explicit with an applicability
reason; absence of the layer object is invalid. Passing a job definition does
not prove a test ran, a real device was used, or all required cases were covered.
Deployment-specific changes to these minimums require a policy revision, not
an agent-authored exception.

### PR and publication separation

Untrusted jobs use `trust: untrusted_pr`, `source: untrusted_head`, no release
credentials, no environment, and at most `contents: read`. Supported events
are `pull_request`, `push`, and `merge_group`. Publication is not a test layer.

Privileged jobs use `trust: privileged_publish`, `layer: publish`,
`required: false`, `source: verified_artifact`, a named environment and at least
one literal branch. They allow only `push` or `workflow_dispatch`. They must
not cancel an in-progress publication. Supported permission names are limited
to `contents`, `packages`, and `id-token`; OIDC only has `write`. Credential
names are references, never secret values. Permission ceilings are necessary
configuration constraints; trusted adoption must still justify the minimum
permissions needed by the actual publisher and enforce protected environments,
branches and verified artifact provenance. `pull_request_target` is unsupported.

Every job has an explicit timeout (1–1440 minutes) and evidence retention
(1–90 days). Cache namespaces cannot cross the two trust classes. A correct
declaration alone does not prove runner, cache, or secret isolation.

## Validation and remaining integration

Run `python3 organization/runtime/workflows/tests/test_delivery_profiles.py`
for focused profile coverage. The repository's `python3 scripts/validate_all.py`
automatically discovers this test suite and compiles the new module.
Negative tests cover missing profiles/layers/jobs, malformed nested types,
duplicate identifiers, trust-boundary violations, mutable references, unsafe
paths, missing ownership, and resource bounds.

This first unit does not implement workflow inventory/parity or flaky-run
evidence. Those are separate units of #144. It does not implement #140's final
pre-PR execution/snapshot gate or #128's authoritative current-identity merge
checks. Those consumers must not treat this configuration receipt as a run
result or merge authorization. Real adoption producers, workflow alignment,
remote CI and device evidence remain pending until independently established.
