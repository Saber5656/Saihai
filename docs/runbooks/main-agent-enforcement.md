# Main-Agent Action Enforcement

This runbook defines the A-prime (`A′`) integration model.  The portable model
does not require Saihai to own every product's normal UI, authentication, or
session lifecycle.  Each concrete adapter must nevertheless identify the
launch boundary it can actually enforce.  The first supported target is only
the release-pinned stock Codex CLI 0.144.1 process started by the root-owned
Saihai launcher.  Codex App and IDE sessions are outside that claim.  Only a
surface for which the runtime `action_enforced` gate succeeds may be described
as having no ambient direct-action authority. Its Saihai-owned frontend gateway
may submit a typed request but does not execute a work order. A configured or
suppressed target has no such claim.

## Authority Model

```text
main agent                    Saihai host boundary
-----------                   --------------------
reason and read        --->   submit_request
read redacted state    <---   read_projection
acknowledge output     --->   ack_output

direct write/shell/network/Git/publication: denied
typed submit -> waiting_human; capability/worker/downstream: none

separate worker policy domain
approved work order -> single-use capability -> bounded worker: host-owned
automatic frontend-to-worker cross-domain transport: not shipped
```

The frontend agent type and the worker backend are separate choices. A Codex
frontend does not imply a Codex worker, and adding a Claude/Cursor/Grok frontend
must not change backend selection. The machine-wide Codex policy makes the
frontend domain read-only, so a write-capable worker must run in a separately
governed policy domain. The v0.1.0 action gateway is local-only and does not
automatically transport an approved work order into that other domain.

## Assurance States

| Level | Meaning |
|---|---|
| `advisory` | Instructions, logging, or mutable/fail-open controls only. |
| `ingress_enforced` | Every declared prompt entry reaches Saihai or the original turn stops. This alone says nothing about side effects. |
| `action_enforced` | Every declared direct side-effect path is denied; the target's typed Saihai gateway is the only positive path. For the frontend target that path stops at `waiting_human` and does not create a capability or worker. An approval prompt is not a denial. |
| `managed_worker` | Saihai owns process launch, pinned binary/config, environment, timeout, output, and evidence for the bounded worker; each capability binds the actual repository/worktree separately. |

These are independent states, not a ranking. The machine-readable assurance
registry and the active generation/attestation are the source of truth. A
missing, failed, stale, or drifted generation suppresses the targeted claim and
blocks that mode; it does not convert the failed target into an `advisory`
success and must not silently continue as an unrestricted agent.

An authority check also requires a typed live-process context. The central gate
reopens the launch record and compares process/start identities, supervisor
ancestry, executable, argv, profile, checkout, and the current deployment epoch
with the live OS state. A context-free generation inspection is informational
only and cannot grant authority.

## Shipped Surface Status

| Surface | Target | Effective status before a current active generation | Notes |
|---|---|---|---|
| Fixed-launcher Codex CLI 0.144.1 main-agent frontend | `action_enforced` only | suppressed | Promotes only after the root-owned deployment and CLI binary, reviewed effective inventory, all direct-action denials, and the exact-one typed-submit path to `waiting_human` pass. No `ingress_enforced` claim is made. |
| Codex App / IDE | none | unsupported for enforcement | Their app-server client can inject dynamic tools that Codex 0.144.1 requirements cannot deny. Do not reuse the CLI attestation. |
| Claude main-agent frontend | none | `advisory` | Existing settings are migration guidance, not a mechanical target claim. |
| Cursor frontend | future `ingress_enforced` + `action_enforced` | suppressed candidate | No supported installed integration or current evidence exists. |
| Grok frontend | future `ingress_enforced` + `action_enforced` | suppressed unavailable | No supported installed integration or current evidence exists. |
| Scoped Codex worker | `managed_worker` | suppressed | Same-rootfs Codex 0.144.1 cannot prove generic external-mutation, absolute local `git_push`, or credential denial. The exact `external_mutation`, `git_commit`, `git_push`, and `credential_access` facts remain failed/inconclusive and non-promotable; only stronger evidence from a separately isolated worker domain may activate this runtime-global claim. |

Codex is the first concrete frontend target.  Other products reuse the same
bridge and assurance contract; they are not claimed as enforced merely because
their names appear in a registry.

## Codex Files

| File | Purpose |
|---|---|
| `organization/runtime/workflows/scripts/main_agent_bridge_mcp.py` | MCP server that exposes exactly `submit_request`, `read_projection`, and `ack_output`. |
| `organization/runtime/workflows/profiles/codex-main-agent.config.example.toml` | Source for the rendered, digest-bound installed profile. A separately copied mutable user profile is only a development/routing aid and is not claim evidence. |
| `organization/runtime/workflows/profiles/codex-main-agent.requirements.example.toml` | Required administrator policy for an `action_enforced` claim; selects only managed `default_permissions = "saihai_frontend"` and pins the exact MCP process identity. |
| `organization/runtime/workflows/scripts/codex_main_agent_install.py` | Builds a reviewable deployment stage and install plan without running `sudo`. |
| `organization/runtime/workflows/scripts/codex_main_agent_supervisor.py` | Root supervisor for standard and fixed commissioning sessions; records launch identity before dropping the Codex child to the runtime user. |
| `organization/runtime/workflows/scripts/codex_main_agent_verify.py` | Revalidates the root-owned deployment and its digest bindings. |
| `organization/runtime/workflows/scripts/agent_integration_observer.py` | Root-only commissioning monitor that runs fixed frontend probes, consumes the fixed worker probe, freezes a generation, and selects its active pointer. |
| `organization/runtime/workflows/profiles/verify_enforcement.md` | Deployment and runtime verification entrypoint. |
| `organization/runtime/workflows/profiles/agent-integration-canary.md` | Lower-level generation-bound evidence contract and final routing-only acceptance procedure. |
| `organization/runtime/workflows/profiles/saihai-frontend-session.sh` | Optional compatibility launcher; not the authority boundary. |
| `organization/runtime/workflows/profiles/codex-main-agent.rules.example` | Legacy command-bridge migration rules; not used by the MCP action-enforced profile. |

Codex hooks are not an enforcement boundary.  A hook may fail to start, time
out, be untrusted, or be disabled, and current tool hooks do not provide the
same fail-closed guarantee as the read-only sandbox.  Do not claim
`ingress_enforced` or `action_enforced` from a hook-only setup.

## Prepare the Codex User/Profile Layer

1. Copy the profile and replace `/Users/YOU` plus the Python executable with
   paths on the host.  Keep `notify=[]` and every current-binary feature pin.
2. Keep the MCP script in the supported primary checkout at `~/dev/Saihai`.
3. Restart Codex or create a new thread; a running thread keeps its existing
   tool and permission snapshot.
4. Treat this layer as a development/routing aid only.  It cannot promote an
   assurance claim.

The ordinary user/profile layer is intentionally compatible with the existing
scoped worker: the worker starts Codex with `--ignore-user-config`, a separate
`CODEX_HOME`, a frozen execution plan, and a capability-bound task worktree.
The main agent cannot weaken its already-running read-only sandbox, but a human
can deliberately choose another launch configuration.  Therefore this mutable
layer is never sufficient evidence for `action_enforced`.

## Required Administrator Deployment for `action_enforced`

`/etc/codex/requirements.toml` is the Codex discovery alias for the settings it
can constrain; Saihai manages and verifies only canonical
`/private/etc/codex/requirements.toml`. Codex 0.144.1 requirements cannot
constrain the legacy
top-level `notify` command, so the fixed launcher also supplies a final
`notify=[]` override and its absence is tested with a process/file sentinel.
The fixed argv, rendered profile, and requirements select only the managed
`default_permissions = "saihai_frontend"` profile. They do not combine that
permission profile with legacy `--sandbox`, `sandbox_mode`, or
`allowed_sandbox_modes` selectors, because Codex 0.144.1 does not compose those
two mechanisms.
On macOS the reviewed artifact is installed and
verified through canonical `/private/etc/codex/requirements.toml`.  Installing
the supplied deployment requires a human administrator and a restart/new
thread. Saihai's preparer only creates a user-owned stage and prints a JSON
plan; it never runs `sudo` or writes the production paths.

The plan has two administrator gates. In phase 1, the human runs each
`phase_1_freeze_commands` entry to copy data into the root-owned quarantine,
then runs every `human_post_freeze_check_commands` digest/metadata check. Only
after comparing those results with the reviewed stage does the human run
`trusted_post_freeze_seal_command`. In phase 2, the human runs
`phase_2_activate_command`; activation imports only the sealed frozen payload.
The plan also contains independent `rollback_command` and `uninstall_command`.
`freeze_copy_failure_cleanup_command` is allowed only after every generated
`freeze_copy_failure_precheck_commands` entry proves that neither a seal nor an
activation journal exists.

Login happens only after phase-2 activation. Activation creates the dedicated
`CODEX_HOME` at exact `0700`; the human then performs read-only type,
non-symlink, owner, exact-mode, and base-`config.toml` absence checks before
running the native Codex login manually. Agents must not run login or handle its
output, and the runbook does not use `mkdir` or `chmod` as a repair step.

The file is machine-wide.  Do **not** install the read-only requirements
on a host where the current Codex scoped-worker backend must use
`workspace-write`: `--ignore-user-config` does not bypass administrator
requirements.  First place frontend and worker execution in separate managed
policy domains (for example, separate hosts/VMs or a separately governed
worker backend), then install and attest the system policy.

The requirements file is read by other Codex surfaces, but it does not make
Codex App or IDE action-enforced.  Their app-server protocol accepts
client-supplied dynamic tools after the current requirements filter.  This
limitation is represented as an assurance scope, not hidden by claiming
that a local profile is administrator-immutable. In addition to the static
deployment check, current root-observed commissioning and an immutable active
assurance generation are required. Configuration alone never activates the
claim. Public assurance directories/files are exact `0755`/`0644`; private
`commissioning/**` directories/files are exact `0700`/`0600`, and lock files
are `0600`. The root-owned `epochs/<profile_id>.json` record is also public
read-only assurance metadata. Owner or mode drift suppresses the claim.

Activation, rollback, and uninstall rotate the deployment epoch to
`transitioning` before the first target mutation. A completed activation or
rollback finalizes that new epoch as uncommissioned; uninstall finalizes it as
`uninstalled`. A failed transition never restores the previous epoch, and no
old generation can become authoritative merely because old deployment bytes
were restored. The three transaction types share one root-owned, nonblocking
deployment lock. Epoch, journal, and backup metadata become durable through a
file `fsync`, same-directory descriptor-relative rename, and parent-directory
`fsync`; target mutation starts only after the transitioning epoch has passed
that durability boundary. Fresh commissioning and sealing are required after
every completed transition.

The normal human launch is exactly:

```sh
cd "$HOME/dev/Saihai"
/usr/bin/sudo /usr/local/bin/saihai-codex-main-agent
```

The launcher accepts no arguments. A standard session record is valid for at
most 24 hours and is also rechecked against live supervisor/child identities,
process start tokens, ancestry, executable, exact argv, profile, checkout, and
native-binary digests. Each authority call reads that complete live-process
snapshot before the artifact checks and again immediately before returning;
both snapshots and the stored/context binding must be identical. This
read-check-recheck narrows but cannot eliminate the scheduler-sized interval
after the second sample, so the claim describes the exact checked decision and
does not assert permanent process immutability. The returned claim is bound to
that subject process.
On the supported macOS target, argv identity comes from the kernel's
`KERN_PROCARGS2` vector and preserves argument boundaries; a rendered `ps`
command line is never accepted as identity evidence. Linux `/proc/<pid>/cmdline`
support exists for development/CI parity, while every other platform suppresses
the claim as `claim_live_argv_platform_unsupported`.
Commissioning instead uses a fixed 15-minute supervisor probe. It accepts no
caller prompt, argv, or path, and its `commissioning-launches` record cannot be
substituted for a standard launch record.

## Required Direct-Action Coverage

An `action_enforced` generation must contain root-observed evidence for each
declared operation:

- `filesystem_write`, `shell_exec`, and `process_spawn`;
- `network_egress`, `external_mutation`, and `provider_dispatch`;
- `git_commit`, `git_push`, `pr_create`, and `release_publish`;
- `credential_access` and `agent_spawn`.

For each operation, the direct path must be mechanically absent or `deny`, not
`ask`. Gateway outage, configuration drift, or a failed probe must leave direct
execution denied. The fixed filesystem probe makes one actual structured
`apply_patch` attempt and verifies that the generation-local marker and checkout
remain unchanged. Operations mechanically absent from the effective inventory
are recorded as such; model prose and fabricated attempts are not evidence.

For the fixed-launcher frontend, `credential_access = denied` is deliberately
narrow: it covers the legacy Codex auth path, the dedicated `CODEX_HOME` auth
path, and the absence of credential-capable tool classes in the fixed,
digest-bound inventory. It does not assert that every user-readable file which
may contain a secret is inaccessible. This frontend-scoped fact cannot be
reused as the worker's generic credential-denial evidence.

The frontend positive observation is deliberately non-executing. It must show
exactly one `submit_request` audit success for the fixed prompt, refs
`README.md` then `CHANGELOG.md`, `allowed_paths=[]`, and request status
`waiting_human`. It must show no capability, worker execution, run, work order,
provider evidence, report, or marker change. Optional projection reads and
acknowledgement do not advance the request.

The worker has its own `managed_worker` commissioning. Its runtime-global
`checkout_digest` is the sentinel for
`checkout_binding=capability_per_execution` and
`repository_scope=host_verified_work_order`; it never stands in for a real
repository/worktree identity. Capability derivation and pre-execution checks
separately bind and revalidate the exact work-order repository and worktree.
Current same-rootfs worker evidence cannot prove generic external-mutation,
absolute local `git_push`, or credential denial, even with network disabled.
Evidence for `external_mutation`, `git_commit`, `git_push`, and
`credential_access` has `result=fail` with an inconclusive host observation and
is intentionally non-promotable, so worker `commission-seal` fails closed with
`worker_denial_facts_not_promotable`. The fact names
`workspace_profile_and_network_disabled_not_same_rootfs_isolation` and
`dedicated_auth_deny_configured_not_mechanically_proven` explicitly state that
the corresponding configuration is not mechanical denial evidence. Hardening
on that rootfs is defense in depth, not an active `managed_worker` claim.

The selected future policy-domain substrate, denial-fact mapping, typed
host-mediated transport, no-write-back contract, and deferred commissioning
procedure are specified in
[Isolated Worker Policy Domain and Transport Contract](../design/isolated-worker-policy-domain.md).
That design and its schema tests do not activate `managed_worker`; the current
promotion blocker remains authoritative until isolated-domain evidence support
is implemented and commissioned.

## Limits

- The first workspace binding remains the supported Saihai primary checkout
  and its linked worktrees.  Another repository is unsupported until a
  host-owned workspace binding is added and attested.
- The bridge does not classify, approve, create runs, derive capabilities,
  execute workers, or publish.
- Redacted child-thread and worker summaries require an exact match on request,
  task, owner-principal digest, and checkout digest. Missing or legacy-unbound
  summaries are not shown. Arbitrary write access to the private orchestrator
  state root could forge those fields and is outside this same-uid trust
  boundary.
- The shipped local action gateway does not automatically cross the required
  frontend/worker policy-domain boundary.
- Current same-rootfs worker commissioning cannot seal `managed_worker`; only a
  separately isolated worker domain with stronger denial evidence may activate
  that claim.
- Commit, push, PR creation, and release publication remain separate gates.
- A supported Codex local policy does not constrain arbitrary non-Codex
  programs running as the same Unix user.
- The CLI attestation does not apply to Codex App, IDE, app-server, remote, or
  directly launched Codex processes.  Those remain unsupported for the claim.
- The server-backed MCP inventory is exactly the three bridge tools.  The
  total model inventory may also contain reviewed read-only/internal tools;
  any visible `apply_patch` must still fail the real write probe.
- Upgrades or binary/config/tool-inventory drift suppress the active generation.
  Renew the current frontend by beginning a new commissioning, running its
  fixed suite, and sealing a new generation. A worker can follow that renewal
  flow only in a separately isolated policy domain whose denial evidence is
  promotable; the current same-rootfs worker suite still cannot seal. Do not
  edit or reuse an old generation.
- Deployment rollback or uninstall does not reactivate a previous assurance
  generation. Drift remains fail-closed, and a restored deployment requires a
  fresh commissioning and seal before the claim can be used again.
