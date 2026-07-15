# Sahai

[English](README.md) | [Japanese](README.ja.md)

Sahai is a local orchestrator and organization-runtime repository for running
AI-agent work through typed artifacts, explicit approval, durable state, and
auditable evidence instead of treating a prompt as execution authority.

The repository still includes the local status viewer inherited from
Agent-Teams-Viewer (ATV), but the primary product surface is now the
deterministic frontdoor, durable workflow runs, the constrained main-agent
bridge, and typed report and evidence gates. Pre-release records may still use
the ATV name for historical artifacts and compatibility aliases.

Sahai uses only the Python 3.10+ standard library for normal operation. No
`pip install` step is required.

## Release

Release history and the merged pull requests included in v0.1.0 are recorded
in the [changelog](CHANGELOG.md). Tagging and GitHub Release publication are
human-owned operations and are separate from merging a release-preparation PR.

The v0.1.0 certainty and authority boundary is deliberately narrow:

- The Saihai bridge accepts only typed-request submission, redacted-projection
  reads, and output acknowledgement. It rejects frontend-supplied
  classification, approval, run creation, raw commands or paths, and
  publication authority. This API restriction alone does not remove ambient
  authority from an independently launched agent.
- The redacted projection exposes an idempotency digest for deterministic
  correlation, never the raw key. Child-thread and worker summaries are shown
  only when request, task, owner-principal, and checkout bindings all match;
  missing or legacy-unbound summaries remain hidden.
- The only shipped enforcement target is the release-pinned stock Codex CLI
  started through the root-owned zero-argument launcher. With a current
  `action_enforced` generation, its frontend-positive path is exactly one typed
  submit that stops at `waiting_human`; it creates no capability, worker, run,
  provider dispatch, or other downstream execution. Codex App, IDE, and direct
  Codex launches are outside this claim, and no `ingress_enforced` claim is made.
- An authority decision is bound to the current live launcher process and the
  current deployment epoch. Activation, rollback, and uninstall revoke the
  prior epoch before changing deployment targets, so restored bytes still
  require fresh commissioning and sealing.
- Frontend `credential_access = denied` is limited to the two known Codex auth
  paths, the dedicated `CODEX_HOME`, and the absence of credential-capable tool
  classes in the fixed inventory. It does not claim that every user-readable
  file which might contain a secret is inaccessible.
- A host-owned executor can derive a capability from an approved work order and
  launch a pinned, bounded Codex CLI worker only while current
  `action_enforced` and `managed_worker` generations both verify. The worker
  requires a separately governed policy domain. v0.1.0 ships no automatic
  cross-domain transport from the frontend gateway to that worker domain. No
  active `managed_worker` generation is currently claimable on the same rootfs.
- The shipped scoped-worker executor rejects all network and provider grants.
  The opt-in live provider adapters are a separate host-owned readonly path.
- Commit, push, and pull-request publication remain behind separate review,
  approval, and publication gates.
- The supported checkout is the host-managed primary checkout at `~/dev/Saihai`
  or one of its linked worktrees. An arbitrary fresh clone does not satisfy the
  checkout identity contract.

Daemon scheduling, tmux worker execution, package distribution, automatic
publication, credential provisioning, and release publication are outside the
v0.1.0 runtime boundary.

## Requirements

- Python 3.10 or newer
- Git 2.37 or newer when the live scoped-worker backend is enabled
- A writable Agents Vault configured through the primary checkout
- The checkout is the host-managed primary checkout at `~/dev/Saihai` or a
  linked worktree whose primary is that checkout; `directory-path.env` and
  `--state-root` do not make an arbitrary clone valid
- The runtime user's home is a canonical, non-symlink directory owned by that
  user and is not group- or world-writable
- Provider CLIs and credentials only when an operator intentionally enables a
  live provider; the offline path does not require them

## Local environment

Configure local paths in the primary checkout's untracked
`directory-path.env`. Do not add them to a shell profile or commit this file.
Linked worktrees reuse the primary checkout's catalog.

```sh
python3 scripts/setup_directory_paths.py --help
# Supply all nine required directory options, then validate the catalog.
python3 scripts/setup_directory_paths.py --check
```

The setup command is non-destructive and writes an owner-only file. Process
environment values generally take precedence over catalog values, including
values that are explicitly empty. The orchestrator state-root key
`SAIHAI_ORCH_STATE_ROOT` is the deliberate exception: process environment
values cannot override it, and an explicit CLI `--state-root` must exactly
match the catalog/default canonical root. See
[Local environment configuration](docs/configuration.md) for resolution and
recovery rules, and
[Directory path variable inventory](docs/environment-variable-inventory.md)
for the complete path audit.

## Repository map

| Area | Primary files | Responsibility |
|---|---|---|
| Operator CLI | `scripts/saihai.py` | Keeps frontdoor proposal/approval separate from workflow-run execution |
| Organization facade | `scripts/configure_organization.py` | Organization mode, runtime paths, workflow selector/frontdoor/server, validation, and legacy ITB compatibility commands |
| Workflow runtime | `organization/runtime/workflows/` | Schemas, templates, deterministic selector, frontdoor harness, HTTP bridge, durable run state, provider adapters, and tests |
| Organization knowledge | `organization/settings.json`, `organization/policies/`, `organization/roles/`, `organization/runtime/` | Repository mirrors of organization settings, policies, team roles, runtime registries, model registries, and team configuration |
| Local status viewer | `server.py`, `static/index.html` | Read-only dashboard for ITB sessions, queues, reports, roles, and workflow runs |
| Migration guidance | `docs/issues/`, `organization/runtime/workflows/operator-runbook.md` | Migration from legacy queue/tmux assumptions to typed workflow runs |

## Shipped behavior

| Capability | Current behavior |
|---|---|
| Prompt classification | `scripts/configure_organization.py classify` and `/api/decide` classify work as `fast`, `strict`, or `maintenance`. Every mode still requires the applicable task and Vault records. |
| Workflow selection | `workflow_selector.py` deterministically maps a typed classification to an active workflow template. A raw prompt is never selection authority. |
| Frontdoor proposal | Prompt-originated requests stop at `proposed` or `waiting_human`, or fail closed as `blocked`. `propose` cannot produce an approved activation or create a workflow run. |
| Approval | `approve` verifies a challenge derived from the proposal digest. Accepted activation sources are `human_ui`, `manual_cli`, and `orchestrator-start`, with trusted execution principals `human_operator`, `manual_operator`, and `orchestrator_start`. The narrow CLI defaults to `human_operator` / `human-ui` / `local_ui`. |
| Workflow runs | An approved request creates a durable `runs/<run_id>.json`; `drain` produces a bounded, immutable work order. |
| Recovery | The compatibility harness provides typed `resume`, `abort`, `task-view`, and `lock-status` operations over durable state. |
| Provider runner | `run-provider` dispatches the deterministic fake provider or a pinned `claude_headless_p0` / `codex_cli_openai_p0` live adapter and writes runner-owned typed reports, normalized evidence, and confined transcripts. |
| Report and completion gates | Typed reports and normalized provider evidence are canonical. `verify-completion` separately checks terminal artifacts and produces the thin Vault evidence block. |
| Main-agent bridge | The bridge accepts request submission, redacted-projection reads, and output acknowledgement, while rejecting authoritative classification, approval, run creation, adapter preparation, and report paths. The projection exposes an idempotency digest, never the raw key. This API contract alone does not remove ambient authority from an independently launched agent. |
| Child-thread action gateway | `child-thread-create` records a validated issue-scoped child-worktree plan and result. Main-agent projections contain only a redacted summary whose request, task, owner-principal, and checkout bindings all match the current request. |
| Scoped worker executor | The executor contract and commissioning scaffolding are implemented, but `managed_worker` is suppressed because current same-rootfs external-mutation/git/credential facts are non-promotable. Derive and execute require both claims and recheck the per-capability repository/worktree binding. Only an isolated worker domain with stronger evidence may activate the claim, and no automatic transport to that domain is shipped. |
| Status viewer | The local dashboard reads ITB sessions, queues, reports, role metadata, organization settings, workflow runs, and lock state without mutating runtime state. |

## Explicit non-goals

| Non-goal | Boundary |
|---|---|
| Provider credential provisioning | Operators create and configure credentials manually. The runner accepts neither credential values nor arbitrary argv, shell, model, cwd, or endpoint overrides. |
| tmux worker execution | `tmux_interactive` remains a compatibility model but is not used by the P0 execution path. |
| Daemon or LaunchAgent scheduling | The scheduler is invocation-drain with durable state and global concurrency 1. |
| Implicit commit, push, or PR automation | Publication is a separate gate. A normal P0 workflow does not publish changes directly. |
| Workflow control from the status viewer | The viewer is read-only. Workflow control belongs to the operator CLI or the authenticated frontdoor HTTP API. |

## Offline quickstart

Run the following flow only from that managed primary checkout or one of its
linked worktrees. It uses a deterministic fake provider and makes no live
provider call. Complete [Local environment](#local-environment) first; path
configuration alone does not authorize a different clone.

```sh
suffix="$(date +%s)"
request_id="req-readme-smoke-$suffix"
run_id="run-readme-smoke-$suffix"

python3 scripts/saihai.py frontdoor propose \
  --task-id TSK-readme-smoke \
  --request-id "$request_id" \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'

python3 scripts/saihai.py frontdoor status --request-id "$request_id"

nonce="$(
  python3 scripts/saihai.py frontdoor status --request-id "$request_id" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["request"]["approval"]["human_action_id"])'
)"

python3 scripts/saihai.py frontdoor approve \
  --request-id "$request_id" \
  --nonce "$nonce"

python3 scripts/saihai.py workflow create-run \
  --request-id "$request_id" \
  --run-id "$run_id"

python3 scripts/saihai.py workflow drain --run-id "$run_id"

python3 scripts/saihai.py workflow run-provider \
  --run-id "$run_id" \
  --adapter-id claude_headless_p0 \
  --fake-provider-mode success

python3 scripts/configure_organization.py workflow-frontdoor \
  verify-completion --run-id "$run_id"
```

The final command must return a typed completion decision. For deeper
background on blocked states, artifact inspection, recovery, migration, and
rollback, see the
[operator runbook](organization/runtime/workflows/operator-runbook.md). The
command lists in this README have been checked against the current executable
surfaces.

## Operator CLI

`scripts/saihai.py` is the narrow operator-facing CLI.

```sh
python3 scripts/saihai.py --help
python3 scripts/saihai.py frontdoor --help
python3 scripts/saihai.py workflow --help
```

| Group | Commands | Authority boundary |
|---|---|---|
| `frontdoor` | `propose`, `approve`, `status` | Propose or explicitly approve activation artifacts and read request state. These commands do not create workflow runs. |
| `workflow` | `create-run`, `drain`, `run-provider`, `validate-report` | Operate on approved request artifacts, run IDs, work orders, and typed reports. These commands do not accept a raw prompt or classification. |

Recovery and inspection commands are deliberately exposed through the broader
compatibility harness rather than duplicated in the narrow CLI:

```sh
python3 scripts/configure_organization.py workflow-frontdoor resume \
  --run-id <run_id> --requeue
python3 scripts/configure_organization.py workflow-frontdoor abort \
  --run-id <run_id> --reason "operator cancelled"
python3 scripts/configure_organization.py workflow-frontdoor task-view \
  --task-id <task_id>
python3 scripts/configure_organization.py workflow-frontdoor lock-status
```

`run-step`, `resume`, `abort`, `verify-completion`, `task-view`, `lock-status`,
and `list` are not subcommands of `scripts/saihai.py`. Use the compatibility
harness for the implemented recovery, verification, and inspection commands.

## Live provider adapters

Live readonly execution requires both `--live` and the exact environment
guard. Provider authentication and all host bindings are configured manually
by the operator.

```sh
SAIHAI_ALLOW_LIVE_PROVIDERS=1 python3 scripts/saihai.py workflow run-provider \
  --run-id <run_id> \
  --adapter-id claude_headless_p0 \
  --live \
  --timeout-seconds 1800

SAIHAI_ALLOW_LIVE_PROVIDERS=1 python3 scripts/saihai.py workflow run-provider \
  --run-id <run_id> \
  --adapter-id codex_cli_openai_p0 \
  --live \
  --timeout-seconds 1800
```

The live command boundary is host-owned.

| Adapter | Mechanical boundary |
|---|---|
| `claude_headless_p0` | Requires an absolute executable pinned by `SAIHAI_CLAUDE_EXECUTABLE_PATH` and its SHA-256 variable. The runner rechecks owner, mode, and digest; uses `--print --output-format json` and plan/safe mode; and disables tools, slash commands, MCP, and session persistence. |
| `codex_cli_openai_p0` | Requires the pinned executable plus host-owned confinement wrapper and profile paths and digests. It uses an isolated cwd, `exec --ephemeral --json`, approval `never`, a read-only sandbox, and no inherited user rules, configuration, or shell environment. Missing confinement bindings fail closed. |

Host-binding variables contain only paths and digests, never credential values.
Codex requires all of the following bindings:

- `SAIHAI_CODEX_EXECUTABLE_{PATH,SHA256}`
- `SAIHAI_CODEX_CONFINEMENT_WRAPPER_{PATH,SHA256}`
- `SAIHAI_CODEX_CONFINEMENT_PROFILE_{PATH,SHA256}`

Callers cannot choose argv, shell, cwd, model, provider endpoint, or output
paths. Before a provider call, the runner revalidates the signed work order,
iteration-frozen snapshot, run/request/step binding, context-file sizes and
digests, adapter-request digest, lease, and pinned executable.

Live context is limited to 20 files, 256 KB per file, and 1 MB total. It is
passed as canonical inline JSON, so the provider receives no repository-read
authority. Combined stdout/stderr is capped at 4 MiB and stored only in an
owner-only `0700` directory and `0600` transcript. `stdout_sha256` covers raw
stdout; `transcript_sha256` covers the full transcript JSON.

Each provider CLI invocation defaults to 30 minutes and accepts values from 1
second through 24 hours. The harness itself has no cumulative wall-clock
timeout. A durable claim is heartbeated every 30 seconds, and the global
workflow lock is not held during the provider subprocess. Attempt journals and
retry counters survive restarts; the same failure is retried at most five times
after the initial attempt before moving to `waiting_human`. Operators can
continue after a host or process restart with `resume` or another
`run-provider` invocation.

## Organization facade and frontdoor harness

`scripts/configure_organization.py` is the compatibility facade used by
skills, automation, and the existing runtime.

```sh
python3 scripts/configure_organization.py status
python3 scripts/configure_organization.py runtime-paths
python3 scripts/configure_organization.py classify --prompt "Review the latest forecast"
AGENT_ORG_MAINTENANCE=1 python3 scripts/configure_organization.py classify --prompt "Repair a hook"
python3 scripts/configure_organization.py validate-all
python3 scripts/configure_organization.py workflow-selector validate-contracts
python3 scripts/configure_organization.py workflow-frontdoor --help
```

| Command | Purpose |
|---|---|
| `status` | Print organization settings, role and policy counts, and repository root as JSON. |
| `runtime-paths` | Verify the ITB runtime, workflow selector/frontdoor/server, operator CLI, and registry mirrors. |
| `classify` | Classify a prompt as `fast`, `strict`, or `maintenance`. |
| `validate-all` | Run the offline suites, contract validation, and Python compile check. |
| `workflow-selector` | Validate workflow contracts and perform deterministic selection and activation-envelope operations. |
| `workflow-frontdoor` | Provide the complete host-owned frontdoor and recovery surface. |
| `workflow-frontdoor-server` | Run the localhost frontdoor HTTP API. |
| `itb`, `itd-monitor`, `agent-call`, `agent-surfaces`, `agent-switch`, `provider-failover`, `transport-status` | Preserve legacy or compatibility runtime entry points. |

The frontdoor harness currently implements:

| Command | Purpose |
|---|---|
| `propose`, `approve`, `orchestrator-start-approve`, `manual-approve` | Create and explicitly approve bounded activation artifacts through trusted channels. |
| `create-run`, `drain` | Create durable runs and immutable work orders. |
| `resume`, `abort` | Recover or terminate durable non-terminal runs. |
| `adapter-capability` | Print a provider adapter capability descriptor. |
| `prepare-claude-adapter` | Create a deprecated, non-executable compatibility artifact. Live execution is consolidated under `run-provider --live`. |
| `run-provider`, `validate-report` | Execute a bounded fake or pinned readonly adapter and pass runner-owned artifacts through the report gate. |
| `verify-completion` | Verify terminal typed artifacts and produce a thin final-evidence decision. |
| `task-view`, `lock-status` | Read task-linked run evidence and the global lock state. |
| `bridge-submit-request`, `bridge-read-projection`, `bridge-ack-output` | Operate the constrained main-agent bridge. |
| `child-thread-create` | Record a validated child-thread plan and result through the action gateway. |
| `channel-token` | Create an owner-only local HTTP channel-token file. |
| `bridge-retention-purge` | Redact eligible terminal prompts and purge only expired bridge indexes, acknowledgements, rate-limit records, and rotated audit files. |
| `state-permission-repair` | Audit private state modes; add `--apply` only from the local manual-operator path to repair legacy modes and write durable evidence. |

The default orchestrator state root is
`~/.codex/state/itb/frontdoor-orchestrator`. To place it elsewhere, set
`SAIHAI_ORCH_STATE_ROOT` in the primary checkout's owner-only (`0600`)
`directory-path.env`. The catalog must be a regular file owned by the current
user, and the state root must be a validated absolute path. This
security-sensitive key cannot be overridden by the process environment.

Linked worktrees consult only the host-managed primary checkout at
`~/dev/Saihai`; they do not rediscover the catalog through Git metadata or a
fallback path. `--state-root` confirms the configured canonical root and cannot
select an arbitrary location.

### Agent-independent A′ frontend

The portable A-prime (`A′`) model does not require Saihai to own every agent
product's normal UI, authentication, or session lifecycle.  Each adapter must
still declare the concrete boundary it can enforce.  For requests submitted
through the bridge, Saihai owns the typed request and every later approval,
capability, side-effect, and evidence gate.  This does not claim that every
prompt entrypoint is ingress-enforced.

The machine-readable assurance contract keeps four independent states distinct:

| State | Claim |
|---|---|
| `advisory` | Instructions or observations only; no mechanical guarantee. |
| `ingress_enforced` | Every declared prompt entry reaches Saihai or stops. |
| `action_enforced` | Declared direct side-effect paths are denied and the target's typed Saihai gateway is the only positive path. This does not imply worker execution. |
| `managed_worker` | Saihai owns the bounded worker runtime, binary/config, environment, timeout, and evidence; each capability separately binds the actual repository/worktree. |

Codex is the first concrete frontend target. The claim is limited to the
release-pinned stock Codex CLI 0.144.1 process started by the root-owned Saihai
launcher; Codex App and IDE sessions are unsupported for this claim because
their app-server client can inject dynamic tools that the current requirements
cannot deny. The normal human entrypoint is the zero-argument command
`/usr/bin/sudo /usr/local/bin/saihai-codex-main-agent`; a standard supervisor
session and a short-lived fixed commissioning session have different records
and must not be treated as interchangeable. The target is `action_enforced`;
Saihai does not claim that every Codex prompt entrypoint is `ingress_enforced`.

The bridge MCP server exposes exactly three server-backed tools:
`submit_request`, `read_projection`, and `ack_output`. Its commissioned positive
observation is exactly one typed submit whose stored request is
`waiting_human`, with no capability, worker execution, run, provider evidence,
report, or marker change. Reviewed read-only/internal Codex tools may still be
visible, and any visible `apply_patch` must fail the real write canary. The
target remains suppressed until the root-owned deployment and CLI binary,
machine-wide requirements, independent host observations, and an immutable
generation are sealed and selected by the active pointer. A mutable user
profile supplies development and routing defaults only. Claude remains
advisory; Cursor and Grok record future target contracts but remain suppressed
candidate/unavailable integrations.

```sh
python3 organization/runtime/workflows/scripts/agent_integration_assurance.py report
```

See the [main-agent action-enforcement runbook](docs/runbooks/main-agent-enforcement.md)
for the Codex profile, required administrator deployment, canaries, and the
worker policy-domain limitation.  The first workspace binding is
`Saber5656/Saihai`; other repositories fail closed until a host-owned binding
is added and attested.

### Scoped worker backend

The live scoped-worker backend fails closed until a host operator manually
configures the following assets. Sahai never generates keys or credentials.

The executor implementation is shipped, but the live `managed_worker` claim is
suppressed. Codex 0.144.1 cannot prove absolute denial of a same-rootfs local
`git push`; current worker commissioning may record evidence, but
`commission-seal` fails closed with `worker_denial_facts_not_promotable`. An
active worker claim requires evidence from a separately isolated policy domain.
The current `external_mutation`, `git_commit`, `git_push`, and
`credential_access` facts have `result=fail` with inconclusive host
observations. In particular,
`workspace_profile_and_network_disabled_not_same_rootfs_isolation` and
`dedicated_auth_deny_configured_not_mechanically_proven` are explicit
non-claims, not mechanical denial evidence.
The shipped local action gateway does not provide automatic transport to such a
domain, so the current release is not an automatically connected end-to-end
worker system.

| Environment variable | Purpose |
|---|---|
| `SAIHAI_SCOPED_EXECUTOR_KEY_FILE` | Capability HMAC key in a regular, non-symlink, `0600` file containing at least 32 bytes. |
| `SAIHAI_SCOPED_WORKTREE_ROOT` | Canonical root from which the host derives the task/run-bound worktree path. |
| `SAIHAI_SCOPED_REPO_ROOT` | Host-owned absolute repository path. Defaults to the Sahai repository root. |
| `SAIHAI_SCOPED_CODEX_EXECUTABLE` | Absolute pinned Codex CLI path whose digest is bound into the work order and capability. Group/world-writable binaries are rejected. |
| `SAIHAI_SCOPED_CODEX_HOME` | Dedicated worker runtime/auth root; the main-agent profile is not inherited. |
| `SAIHAI_ENABLE_SCOPED_WORKER_LIVE=1` | Explicit live-execution gate. Without it, only the deterministic fake harness is available. |

The initial v1 capability authorizes only the whole task worktree as scope.
Subpath grants, commit, push, PR publication, credential access, worker-tool
network, and arbitrary providers are outside that authorized capability.
Same-rootfs hardening is defense in depth and is not promotable assurance; do
not infer active native enforcement from the schema or probe records. The fixed
Codex model control plane is host transport, not a network or provider grant to
worker tools. Capability issuance and execution are available only through the
credential-bound `action_gateway` HTTP channel, not through a CLI subcommand.

## Offline validation

Run every offline suite with one command:

```sh
python3 scripts/validate_all.py
```

The organization facade exposes the same validation:

```sh
python3 scripts/configure_organization.py validate-all
```

The harness runs standard-library self-runner tests, validates workflow
contracts, compiles Python sources, and prints a final one-line JSON summary.
Child processes clear `SAIHAI_ALLOW_LIVE_PROVIDERS`, so validation never
depends on live provider tokens or network access. Adapter tests use recorded
fixtures and patched subprocess/binary discovery only.

For a quick contract-only check:

```sh
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

## Frontdoor HTTP API

Run the authenticated local API on loopback:

```sh
python3 scripts/configure_organization.py workflow-frontdoor-server \
  --host 127.0.0.1 \
  --port 8766
```

| Endpoint | Purpose |
|---|---|
| `GET /` | Main-agent output-confirmation UI. |
| `GET /healthz` | Health check. |
| `POST /main-agent/submit-request` | Submit a bridge request. |
| `GET /main-agent/projections/{request_id}` | Read a redacted bridge projection. |
| `POST /main-agent/ack-output` | Record a verified, inert acknowledgement. |
| `POST /action-gateway/child-thread-create` | Record a validated child-thread plan/result; `action_gateway` only. |
| `POST /action-gateway/scoped-worker-capabilities` | Derive a capability from a frozen work order; body contains only `run_id` and `step_id`; `action_gateway` only. |
| `POST /action-gateway/scoped-worker-execute` | Consume a capability and start the pinned worker; body contains only `capability_id`; `action_gateway` only. |
| `POST /frontdoor/propose` | Create an operator proposal. |
| `POST /frontdoor/approve` | Record human-UI approval. |
| `POST /orchestrator/runs` | Create a workflow run. |
| `POST /orchestrator/runs/{run_id}/drain` | Drain a run into a work order. |
| `POST /orchestrator/runs/{run_id}/resume` | Resume or requeue a durable run. |
| `POST /orchestrator/runs/{run_id}/abort` | Abort a non-terminal run with an operator reason. |
| `GET /orchestrator/runs/{run_id}/verify-completion` | Verify terminal artifacts as an operator or harness principal. |
| `GET /orchestrator/tasks/{task_id}/runs` | Read the thin task-linked run and evidence view as an operator. |
| `POST /provider/claude/prepare` | Create the bounded compatibility adapter request. |
| `POST /provider/reports/validate` | Validate a typed provider report as a harness principal. |

Raw request and run reads at `/frontdoor/requests/{request_id}` and
`/orchestrator/runs/{run_id}` return `403`. Main agents use redacted
projections; operators use the dedicated task/completion views or inspect
canonical artifacts under the configured state root.

Create a local channel token with:

```sh
python3 scripts/configure_organization.py workflow-frontdoor channel-token \
  --channel bridge
```

The command prints only the path of the owner-only token file. An operator must
explicitly read and configure the token for the intended client. The API derives
principals from `X-Orchestrator-Channel` and `X-Orchestrator-Token`; it rejects
`principal_type`, `principal_id`, and `authn_method` fields in request bodies as
authority.

## Local status viewer

The ATV-derived dashboard remains available as a local, read-only viewer.

```sh
python3 server.py
python3 server.py --port 8799
```

The default URL is `http://127.0.0.1:8765/`. The server binds to loopback and
accepts only `127.0.0.1`, `localhost`, or `::1` Host values. It has no
authentication; never expose it remotely.

ITB session discovery reads `~/.claude/state/itb` and
`~/.codex/state/itb`. Workflow-run discovery defaults to each root's
`frontdoor-orchestrator` child; the viewer may additionally read a
process-level `SAIHAI_ORCH_STATE_ROOT`. This viewer-only discovery behavior is
not execution authority: the host workflow CLI still accepts only the
canonical root loaded from the primary checkout catalog.

### Viewer API

| Endpoint | Response |
|---|---|
| `GET /api/sessions` | Observable sessions under `~/.claude/state/itb` and `~/.codex/state/itb`. |
| `GET /api/org?session=<id>` | Team role state, active task, and busy count. |
| `GET /api/role?session=<id>&role=<role_id>` | Role metadata, inbox, latest report, and provider evidence. |
| `GET /api/config` | Organization settings and role/policy indexes. |
| `GET /api/decide?prompt=<text>` | `fast`, `strict`, or `maintenance` classification. |
| `GET /api/workflow-runs?session=<id>&task=<id>&state=<state>` | Thin read-only workflow-run summaries. |
| `GET /api/workflow-run?session=<id>&run=<id>` | Work order, report, provider evidence, and transition metadata. |
| `GET /api/workflow-lock` | Global workflow-lock status for each configured orchestrator root. |

The UI contains an organization-control summary, the existing team board, and
a Workflow Runs panel with state badges, a stale-lock banner, and read-only
work-order/report/evidence/transition details. It never starts a provider,
changes configuration, or mutates workflow state.

Viewer role states are derived as follows:

| State | Condition |
|---|---|
| `working` | The latest report is less than 120 seconds old, or report/provider evidence is in progress. |
| `processing` | A queue inbox or report is `processing`, `running`, or `invoked`. |
| `pending` | The queue inbox contains a pending message. |
| `ready` | A queue-consumer role is available. |
| `deferred` | A lazy/on-call role is outside the current task. |
| `offline` | Session metadata or the context pointer is missing. |

## Canonical artifacts

| Artifact | Path | Authority |
|---|---|---|
| Organization settings | `organization/settings.json` | Organization mode, strict/fast behavior, hook observer, and provider-transport policy. |
| Policy mirror | `organization/policies/*.md`, `organization/policy-index.json` | Repository policy mirror and checksum index. |
| Role mirror | `organization/roles/<role>/skill.md`, `organization/role-index.json` | Team-role definitions and checksum/team index. |
| Runtime registry | `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml`, `organization/runtime/role-agent-registry.yaml` | Compatible role-registry paths until cleanup completes. |
| Workflow contracts | `organization/runtime/workflows/registry.yaml`, `templates/`, `schemas/` | Deterministic workflow contract source. |
| Request record | `<state_root>/requests/<request_id>.json` | Request, bounded refs, proposal, approval, and bridge metadata. |
| Workflow run | `<state_root>/runs/<run_id>.json` | Durable run state, current step, terminal status, and transition provenance. |
| Work order | `<state_root>/work-orders/<run_id>/<step_id>.json` | Bounded instruction and canonical report path. |
| Adapter request | `<state_root>/adapter-requests/<run_id>/<step_id>-<adapter_id>.json` | Provider prompt, evidence/transcript paths, and authority boundary for adapters such as `claude_headless_p0` and `codex_cli_openai_p0`. |
| Typed report | `<state_root>/reports/<run_id>/<step_id>-external-review-report.json` | Canonical P0 external-review result. |
| Provider evidence | `<state_root>/provider-evidence/<run_id>/*` | Normalized evidence and signal-only transcript. |
| Session run index | `<session_dir>/orchestrator-runs.json` | Rebuildable viewer projection, not canonical run state. |
| Task view | `workflow-frontdoor task-view` / `GET /orchestrator/tasks/{task_id}/runs` | Derived thin links/status and queue-shaped evidence. |
| Role queue files | `<session_dir>/queue/inbox`, `<session_dir>/queue/tasks`, `<session_dir>/queue/reports` | Canonical ITB role-queue evidence; the orchestrator does not write it. |
| Audit log | `<state_root>/audit/*.jsonl` | Principal-scoped transition, replay, rejection, and acknowledgement evidence. |
| Main-agent deployment | `/Library/Application Support/Saihai/Manifests/codex-main-agent.deployment.json`, `/Library/Application Support/Saihai/Config/codex-main-agent.runtime.json` | Root-owned release, wrapper, requirements, profile, instruction, workspace, and policy-domain bindings. |
| Private commissioning record | `/Library/Application Support/Saihai/Assurance/commissioning/<profile_id>/<commissioning_id>.json` | Root-only, single-use commissioning lifecycle record. |
| Deployment epoch | `/Library/Application Support/Saihai/Assurance/epochs/<profile_id>.json` | Root-owned transition/revocation state; every deployment transition invalidates older generation bindings. |
| Immutable assurance generation | `/Library/Application Support/Saihai/Assurance/generations/<profile_id>/<generation_id>/` | Generation manifest, attestation, observer records, observations, evidence, fixed markers, and canaries. Model prose is not authority. |
| Active generation pointer | `/Library/Application Support/Saihai/Assurance/active/<profile_id>.json` | Atomically selected generation revalidated by each action gate. |
| Launch-session records | `/Library/Application Support/Saihai/Assurance/launch-sessions/<session_id>.json`, `/Library/Application Support/Saihai/Assurance/commissioning-launches/<session_id>.json` | Root-owned standard and commissioning launch bindings; the two session types are not interchangeable. |

Public assurance directories/files, including `epochs`, use exact `0755`/`0644`; private
`commissioning/**` uses exact `0700`/`0600`, and lock files use `0600`. Owner or
mode drift suppresses the affected claim.

## Workflow contracts

| Template | Purpose |
|---|---|
| `single_step_external_review` | Read-only external review. |
| `research_only` | No-diff research, design, or source review. |
| `standard_code_change` | Bounded code change without publication. |
| `publication_required` | Code change or publication that requires a separate publication gate. |
| `policy_or_permission_change` | Changes to policy, permissions, hooks, or governance. |
| `security_sensitive_change` | Changes requiring explicit security review and risk evidence. |

The readonly external-review path is the primary end-to-end P0 path. The other
templates have active contracts and deterministic routing, but their presence
does not grant an LLM direct write, shell, commit, push, network, or provider
authority. Those effects remain behind explicit host-owned gates.

## Security boundaries

- A raw prompt is not workflow-selection or execution authority.
- Prompt-originated activation stops at `proposed` or `waiting_human`, or
  fails closed as `blocked`.
- The main-agent bridge cannot supply authoritative classification, approval,
  run IDs, report paths, adapter requests, or workflow definitions.
- Context references must resolve within the repository root. Symlink escape,
  `.git`, `.env*`, and key/token/secret/credential paths are rejected, as are
  count and byte-limit violations.
- Provider output is a signal. Only a validated typed report and normalized
  evidence can authorize completion.
- The local viewer is mechanically loopback-only. The frontdoor HTTP API
  defaults to loopback and should remain bound there, but its `--host` option
  does not mechanically prevent a remote bind. Screen sharing may still expose
  prompts, evidence, or internal paths.

## Focused tests

The one-command validation above is authoritative. Useful focused entry points
include:

```sh
python3 tests/test_configure_organization.py
python3 tests/test_saihai_cli.py
python3 organization/runtime/workflows/tests/test_workflow_selector.py
python3 organization/runtime/workflows/tests/test_run_store.py
python3 organization/runtime/workflows/tests/test_task_state_bridge.py
python3 organization/runtime/workflows/tests/test_frontdoor_orchestrator.py
```

## Additional documentation

| Document | Contents |
|---|---|
| [Organization layout](organization/README.md) | Organization knowledge-mirror layout and migration rules. |
| [Workflow runtime](organization/runtime/workflows/README.md) | Detailed contracts, CLI, HTTP API, bridge, runner, and state behavior. |
| [Frontdoor protocol](organization/runtime/workflows/frontdoor-orchestrator-protocol.md) | Authority boundaries and protocol invariants. |
| [Operator runbook](organization/runtime/workflows/operator-runbook.md) | Day-one operation, blocked-state recovery, artifact inspection, legacy migration, and rollback. |
| [Main-agent output UI](docs/issues/main-agent-output-confirmation-ui.md) | Implementation record for restricting the main agent to output confirmation. |
| [Runtime cleanup](docs/issues/runtime-cleanup-obsolete-files.md) | Legacy ITB and mirror cleanup candidates and migration prerequisites. |

## License

MIT License. See [LICENSE](LICENSE).
