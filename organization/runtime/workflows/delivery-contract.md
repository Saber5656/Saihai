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

## Actual workflow inventory (U2A)

`scripts/delivery_workflow_inventory.py` parses actual `.github/workflows/*.yml`
and `*.yaml` sources and produces a content-bound inventory. The repository
snapshot is `profiles/delivery-inventory/saihai.v1.json`. It records what the
reviewed workflows say, including their unresolved limitations; it does not
make the observed jobs authoritative required checks or adopt a delivery policy.
This inventory supplements the configuration-only profile interface above.
Flaky attempt evidence remains a separate unit.

### Local validation setup

Use CPython 3.11 in an isolated environment. The lock contains only the official
PyYAML 6.0.3 wheels for macOS 11+ arm64 and Linux glibc x86_64. Other platforms
are not implicitly supported by this lock. Do not substitute an unhashed wheel,
source distribution, global installation or different interpreter on failure.

```sh
python3.11 -m venv /tmp/saihai-delivery-venv
/tmp/saihai-delivery-venv/bin/python3 -m pip install --require-hashes --only-binary=:all: --no-deps -r .github/requirements-delivery.lock
/tmp/saihai-delivery-venv/bin/python3 scripts/validate_all.py
```

Choose a fresh task-owned temporary directory. CI uses the same lock in its
runner temporary directory and invokes the isolated interpreter for the full
suite. Package integrity and exact Python build/OS integrity are different:
U2A pins the parsing dependency, but the existing `ubuntu-latest` runner and
Python `3.11` selection are still reported as floating. No immutable runtime
acceptance follows from this setup. Actual toolchain alignment remains U2B.

The dependency also enables the existing ITB optional PyYAML path. The new
loader changes only a private `SafeLoader` subclass; global YAML resolution
and the existing ITB fallback remain unchanged and have regression coverage.
Metadata and hashes originate from [PyPI](https://pypi.org/project/PyYAML/6.0.3/).
Hash enforcement and binary-only installs follow
[pip's secure installation contract](https://pip.pypa.io/en/stable/topics/secure-installs/).

### Producer and consumers

| Interface | Contract |
|---|---|
| `parse_workflow(raw)` | Bounded UTF-8 YAML/JSON text to mapping; reject duplicate keys, explicit tags, anchors/aliases, non-JSON values and resource exhaustion |
| `observe_workflows(sources)` | Map of workflow path to exact source text; returns parsing status, source hashes, every expanded cell, errors and quality gaps |
| `audit_inventory(...)` | Compare actual sources/locks with an expected inventory and bind the result to repository/head/base/event/policy assertion |
| `audit_repository(root, expected, event_context, target, policy_snapshot=None)` | Discover both workflow extensions from the actual checkout, including unexpected files, and read declared lock content; reject symlinks |
| `applicability(row, context)` | `applicable`, `not_applicable`, or `unknown` with reason and source reference; never a test outcome |

The closed expected inventory has `inventory_version: "1"`, `repository`,
`source_digests`, `jobs`, and `lock_digests`. Each cell has `cell_id`,
`workflow_path`, `job_id`, `matrix`, `check_name`, and `contract`. The contract
retains the complete workflow context and job declaration: commands/actions,
runtime, matrix, services, environment/defaults, working directory, dependencies
between jobs, conditions, permissions and resource settings. Source, lock and
cell differences are checked in both directions. Comment-only source changes
also invalidate the source identity. Workflow fields cannot disappear because
the expected inventory omitted them. Unknown fields/expressions remain visible.

`cell_id` binds workflow/job/matrix values; it is not the policy's status-check
context. Resolvable matrix names are only candidate check names. A policy-named
check must map uniquely, and a required check that does not trigger stays a gap.
Matrices support bounded scalar axes and include/exclude rows; unsupported
dynamic matrices fail closed. Conditions requiring prior job results, complex
branch patterns, path filters and missing event-action evidence remain unknown.
PR applicability uses the base branch; push uses its own branch.

`target` contains `repository`, `head_sha`, and `base_sha`. `event_context`
contains `event`, `ref`, `base_ref`, boolean `fork`, and nullable `action`.
An optional policy assertion contains `policy_version: "1"`, `repository`,
and `required_checks`. Its digest identifies the assertion but its status is
always `unverified`; `approved` flags are rejected. Missing policy is explicitly
`authoritative_policy_missing`. An independently authenticated producer owned
by #128/the repository policy authority must establish adoption. This module
does not invent that authority from PR observations or its own snapshot.

The result separates `parsing`, structural `parity`, `drift`, quality `gaps`,
per-cell applicability/execution state, and content/binding digests. Even exact
structural parity can have blocked readiness. `authorizes_execution` is always
false and `readiness` is blocked until downstream trusted gates establish what
is still missing. #140 consumes the actual inventory and identity to run final
local checks; #128 consumes candidate mappings against authenticated required
checks and current remote results. Neither may treat an inventory receipt as
a successful run, provider approval, waiver, or merge authorization.

Structural errors (including malformed steps, runner lists, permission enums,
defaults, concurrency, service containers and event filter types) make
`parsing: invalid`. Unresolved expression/name semantics are explicitly listed
in `unknowns` and make parsing unknown. This does not depend on words embedded
in quality-gap messages. Such cells keep unknown applicability/execution state,
even if an expected snapshot repeats the malformed declaration. Conversely,
a missing immutable runtime or concurrency setting is an operational gap in
an otherwise structurally valid workflow; it cannot establish readiness.
Only the documented mapping forms of permissions and unparameterized manual
dispatch are supported. Future or unsupported forms fail closed instead of
being treated as adopted contracts.

CodeQL cells are hybrid: local analysis requires a verified CodeQL bundle and
executor, while code-scanning upload requires the remote service. An unavailable
local executor is not reclassified as a genuinely remote-only test. Neither
component ran merely because the parser found its action. Ordinary command
jobs are locally reproducible only after matching their runtime, dependencies,
services and environment; their returned execution state is `not_run`.

### Actual checkout diagnostic

```sh
/tmp/saihai-delivery-venv/bin/python3 organization/runtime/workflows/scripts/delivery_workflow_inventory.py \
  --repository-root . \
  --expected organization/runtime/workflows/profiles/delivery-inventory/saihai.v1.json \
  --head "$(git rev-parse HEAD)" --base "$(git rev-parse HEAD^)"
```

Report mode exits zero when parsing and structural parity checks succeed;
this is diagnostic execution, **not delivery readiness**. `--check` fails while
any adoption/quality gate remains unestablished. The current real workflows
map `validate` and both CodeQL language cells and still expose floating runtime,
missing concurrency/merge-group support, missing bounded evidence retention,
unverified CodeQL bundle/cache behavior, and missing authoritative policy.
These limitations are not waived or considered completion of #144.


## U2B1 native CPython consumer

The repository-owned `.github/delivery-toolchain.lock.json` declares separate immutable
CPython 3.11.16 artifacts for Darwin arm64 and Linux x86_64/glibc. These are different
bytes and different host platforms under a common contract. The lock is not required-check
policy and does not grant execution or waive missing CI evidence.

Run `python3 scripts/verify_delivery_toolchain.py --run full --output /absolute/new-attempt`
with the normal catalog environment available. The output directory must not exist. Bootstrap
Python only fetches/verifies; the archive interpreter creates a new private venv, installs the
hash-locked dependency and runs both focused suites and the repository full suite. No previous
venv/cache/PATH interpreter is used as fallback. Unsupported native platforms fail closed.
Python bootstrap must supply `tarfile.data_filter`; there is no legacy extraction fallback.

Acquisition accepts only the declared upstream tool/platform URLs, bounded bytes/time and
exact size/SHA256 before extraction. Extraction uses a new private directory, the data filter,
member/path/expanded-size budgets, explicit file/link validation and directory-fd traversal
that refuses symlinks. Links are created last and must target declared internal regular files
or directories; chains, outward links, duplicate/colliding entries and special files fail.
Failure leaves a failed attempt receipt, never an installed success marker or a fallback.

`receipt.json` records lock and selected-artifact digests, target file/mode/patch identity,
bootstrap versus consumer interpreter, version/architecture, host/CI image observations,
dependency lock, each stage's command/time/exit and log digest. Full validation and its
identity checks must finish before status becomes success. Nonzero exits, timeout and
cancellation remain failed attempts. A new attempt never overwrites an earlier attempt.
Only receipt.json and the allowlisted validation.json are uploaded by the pinned artifact
action, for 14 days; raw stage logs and the private runtime are excluded. A successful full
summary keeps reported case counts as reported; historical custom-run counts are not added.

The validate workflow retains its existing jobs/events/action SHAs and adds merge_group,
non-cancelling event/ref-scoped concurrency and attempt-specific evidence. `setup-python`
remains bootstrap only. Inventory syntax support for the fixed CI context references and
`always()` is not evaluation or confirmation of runtime context. Conservative floating-runtime
and policy gaps remain until an authenticated consumer adopts actual receipts; source text alone
does not establish execution. Darwin success is never Linux success. CodeQL local analysis and
remote scanning upload remain `local_unavailable` / `remote_pending` until independently
executed. U2B2 CodeQL connection, U3, #128 policy and #140 consumer adoption remain pending.


## U2B2 CodeQL bundle consumer and observation boundaries

The common toolchain lock also fixes CodeQL bundle 2.26.0 Linux64/macOS64 URLs, exact
compressed sizes and SHA256. CodeQL uses independent 1536MiB download, 300-second wall,
200000-member/16GiB actual decompression/1024-byte path and 180-second scan budgets.
A separately terminated worker bounds even blocking decompression; every regular member
is read and hashed. Normalized duplicate paths, traversal, special/sparse files, conflicting
parents and unresolved/escaping links are refused. Python's 64MiB limit stays unchanged.

The fixed CLI phases are `--codeql-phase acquire|probe|observe --bundle linux64|osx64
--language actions|python --output /absolute/new-attempt`. Acquire creates the attempt and
writes its running receipt before work. Only after exact byte/hash and streamed manifest
verification does it expose bundle.tar.gz to pinned init.tools. Partial files never become
verified inputs. `--fetch-only` is acquisition only: it allows a host/artifact mismatch without
extracting or executing CodeQL, and cannot be used for probe/observe. A fetch on Darwin
is not Linux runtime validation. Phase receipts never grant authority or adopt policy.

Pinned init executes CodeQL BEFORE publishing its path/version outputs. Consequently the
pre-init archive verification is the first execution boundary; post-init probe is an additional
check of the installed bundle and its use before analysis. Its output path must have the
expected fresh UUID/bundle layout under runner.temp. All immutable installed members,
including wrapper, JRE launcher, JARs and native libraries, must match the verified archive
manifest, with no extra files. Version/path strings alone are insufficient. Only then is the
fixed shell-free `codeql version --format=json` command invoked, with bounded output/time
and loader/JVM configuration suppression. No existing tool/cache/PATH fallback is allowed.
Private directory and rechecks do not provide atomic custody against a compromised runner
or arbitrary hostile same-UID writer; upstream tar extraction is not replaced by this helper.

Exactly five init/analyze step-output expressions are recognized as syntax. Their values pass
through fixed environment keys and quoted helper arguments; expressions are never inserted
into shell script bodies or treated as trusted producer fields. Existing events, both language
cells, action SHAs, queries/models/category and analyze service upload defaults are preserved.
TRAP/dependency caching inputs are explicitly false; other internal cache behavior remains
unverified. security-events write is limited to the analyze job. No privileged event or token
workaround is introduced.

Each phase binds target, workflow, lock, artifact, language, run ID/attempt and previous
integrity references. Observer cannot replace missing/failed/cancelled/timed-out phases with
success. Bounded SARIF summary and sarif-id/action outcomes are observations only; the
`authenticated_service_state` remains remote_pending and `policy_status` not_adopted until
#128/#140 bind authenticated CI context. A zero-result SARIF is not a zero-test proof.
Additional upload-artifact retains only the three explicit phase JSON receipts for 14 days;
it excludes archive/manifest/runtime/raw logs/environment/full SARIF. This does not disable
the pre-existing CodeQL SARIF/database service upload. always() cannot guarantee retention
on hard runner failure; missing final receipts remain blockers. Linux init/analysis/service
execution and full Issue acceptance remain pending until real authorized CI evidence exists.
