# Isolated Worker Policy Domain and Transport Contract

Status: design and contract only. This document does not claim that an
isolated domain is deployed or that `managed_worker` can currently be sealed.

## Decision

Use a dedicated, single-execution, hardware-virtualized VM as the worker policy
domain. The VM has a read-only measured base image, one ephemeral writable
work disk, no host filesystem mounts, no network interface, no inherited
environment, no credential material, and no host service socket. The host
delivers an approved work order and repository snapshot on a read-only input
device. The guest returns exactly one typed result over a dedicated
guest-to-host byte stream. The host treats those bytes as inert data until both
schema and semantic validation pass.

This is a policy-domain boundary, not an extension of the primary-host
workspace sandbox. A container on the primary host is not the selected
boundary. The hypervisor, VM image, VM launch manifest, transport controller,
and assurance observer become reviewed, digest-bound artifacts for the worker
generation.

## Existing contract and current suppression

The shipped scoped worker already derives a single-use capability from a frozen
work order, binds the frontend `action_enforced` and worker `managed_worker`
claims, creates a task worktree, launches a pinned runtime with bounded argv and
environment, and verifies the resulting diff. Its runtime-global worker
generation uses the sentinel facts
`checkout_binding=capability_per_execution` and
`repository_scope=host_verified_work_order`; the real repository and worktree
remain per-capability bindings.

The current same-rootfs commissioning facts are deliberately insufficient:

| Operation | Current fact | Current result |
|---|---|---|
| `external_mutation` | `workspace_profile_and_network_disabled_not_same_rootfs_isolation` | `fail`; host observation is inconclusive |
| `git_commit` | `no_head_after_probe` | `fail`; a clean postcondition is not a mechanical denial |
| `git_push` | `network_disabled_only_local_push_not_denied` | `fail`; local push remains possible on the same rootfs |
| `credential_access` | `dedicated_auth_deny_configured_not_mechanically_proven` | `fail`; configured auth-path denial is not generic credential denial |

`agent_integration_assurance.py` also places all four operations in
`WORKER_PROMOTION_BLOCKED_OPERATIONS`. Consequently, the current
`commission-seal` path must stop with
`worker_denial_facts_not_promotable`. A VM design, schema, or successful local
test does not remove that blocker. The claim stays suppressed until an
isolated-domain evidence producer and verifier are implemented, independently
reviewed, and commissioned.

## Substrate comparison

| Denial fact | Container on primary host | Dedicated ephemeral VM | Separate physical host |
|---|---|---|---|
| `external_mutation` | Inconclusive by default. A shared kernel, mount namespace mistakes, daemon sockets, or a writable bind mount can restore host mutation. Rootless/container policy is defense in depth, not a distinct kernel boundary. | Provable when the launch inventory shows no shared filesystem, device passthrough, host sockets, clipboard, guest agent, or bidirectional host service; only a read-only input device and inert result stream exist. | Provable with equivalent network and media controls. Physical separation reduces the shared hypervisor TCB but still needs a typed transfer station and measured host configuration. |
| `git_commit` | Inconclusive if host Git metadata is bind-mounted or reachable through the shared rootfs. | The transported snapshot excludes `.git`; the image contains no VCS client or credential/helper inventory; guest MAC policy denies VCS metadata creation; the host observer proves a fixed commit attempt cannot create repository metadata or change the base revision. | The same guarantees are possible, but need separately maintained OS policy and evidence collection. |
| `git_push` | Network disablement alone is insufficient because a local filesystem push can target another host path or bind mount. | There is no Git metadata, VCS client/helper, NIC, shared filesystem, host socket, or writable device other than the disposable work disk. The fixed probe covers network and local destinations and proves no destination mutation. | Strong when all transfer media and network links are physically or administratively gated; otherwise removable media and management networks become additional destinations to prove absent. |
| `credential_access` | Inconclusive when the shared kernel/rootfs can expose home directories, keychains, agents, metadata sockets, or runtime secrets through a mounting/configuration error. | The measured image and typed input contain no secrets; environment inheritance is empty; no host home/keychain/agent/cloud-metadata device or socket exists; scratch is new and destroyed after the run. The inventory and fixed probes prove absence, not merely a deny setting. | Strong if the worker host is freshly imaged and never receives credentials. Long-lived administration credentials and management agents otherwise enlarge the evidence scope. |
| Operational fit | Lowest cost, but does not satisfy this claim without another isolation boundary. | Reproducible image and launch manifest, per-run disposal, bounded evidence surface, and practical host-side diff verification. | Strongest physical separation, but higher provisioning, patching, inventory, transfer, and evidence cost. |

The VM is selected because it gives a separate kernel and address space while
remaining reproducible enough to bind every generation to exact image,
hypervisor, launch, and transport-controller digests. A separate physical host
is an acceptable future high-assurance variant of this contract. A container
is acceptable only inside the selected VM as defense in depth; it is not the
policy-domain boundary.

## Selected VM invariants

The host must fail closed before launch unless all of these facts are observed
from host-owned configuration and live VM state:

1. The base image, kernel, init, worker runtime, policy, and probe binaries
   match the generation manifest.
2. The input is a fresh read-only block device. It contains only the three
   artifacts named by the input envelope: approved work order (at most 1 MiB),
   repository snapshot (at most 256 MiB), and result schema (at most 1 MiB).
   The host verifies every artifact's declared byte count and SHA-256 against
   the actual bytes before constructing the device.
3. The repository snapshot excludes Git metadata, symlinks escaping the
   snapshot, special files, sockets, and device nodes. Its digest and
   `base_revision` are host computed from the capability-bound task worktree.
4. The VM has no shared-folder device, writable host block device, host home,
   Vault mount, assurance state mount, agent socket, SSH agent, keychain bridge,
   clipboard, guest agent, USB passthrough, NIC, cloud metadata route, or
   general-purpose vsock endpoint.
5. The only guest-to-host channel is a result byte stream bounded to 24 MiB.
   The host checks that byte bound before UTF-8 decoding or JSON parsing. The
   channel has no RPC methods and cannot name or open a host file. Host code
   captures bytes into private staging; it does not deserialize into
   filesystem calls.
6. The guest runtime uid cannot change mounts, policy, the measured image, or
   the input device. Its writable locations are the disposable work disk and
   bounded scratch only.
7. No credential file, token, secret environment variable, login state, or
   credential helper enters the image, input, runtime environment, or scratch.
   A backend requiring guest credentials is unsupported and keeps
   `managed_worker` suppressed. Model execution must be credential-free in the
   guest; any future host inference broker needs a separate approved typed
   contract and new commissioning evidence.
8. VM termination destroys the writable disk and scratch after the host has
   captured the result. No generation, capability, or worktree state is reused
   from a previous VM.

## Denial-fact matrix for the selected domain

| Required operation | Required coverage | VM mechanism | Promotable evidence requirement |
|---|---|---|---|
| `filesystem_write` | `capability_scoped` | Writable disposable work disk only; input and base image are read-only | Fixed positive marker appears only in the guest worktree; mount inventory shows no other writable target |
| `shell_exec` | `capability_scoped` | Pinned runtime, empty inherited environment, bounded process/timeout policy | Runtime binary/config/argv and process-start identity are host observed and generation bound |
| `process_spawn` | `capability_scoped` | VM PID/cgroup limit and executable inventory | Exact runtime child is observed; unbound process/agent launch probes fail |
| `network_egress` | `denied` | No NIC, network backend, metadata route, or general-purpose host socket | Hypervisor live-device inventory plus fixed IPv4, IPv6, Unix-socket, and metadata probes show no route and no side effect |
| `external_mutation` | `denied` | Separate kernel; no shared FS, host devices, sockets, clipboard, guest agent, or writable input | Host/hypervisor inventory plus attempts against every declared external surface; primary-host markers remain unchanged |
| `provider_dispatch` | `denied` | No network and no provider/client credential or plugin inventory | Provider client/tool inventory is mechanically absent and dispatch probes have no route |
| `git_commit` | `denied` | Snapshot excludes `.git`; VCS executable/helper inventory is absent; MAC denies VCS metadata creation | Structured commit and metadata-creation attempts are denied; base revision and snapshot metadata markers do not change |
| `git_push` | `denied` | No Git metadata/client/helper, external route, shared FS, socket, or additional writable device | Structured network and local-destination push attempts are denied; all destination markers remain unchanged |
| `pr_create` | `denied` | No network, provider tool, browser, plugin, or credential | Mechanical absence inventory and fixed attempt produce no external event |
| `release_publish` | `denied` | Same controls as PR creation; no signing/release credentials | Mechanical absence inventory and fixed attempt produce no external event |
| `credential_access` | `denied` | Secret-free measured image/input/environment; no keychain, agent, metadata, host home, or credential mount | Image/input/env/device inventory and fixed known-path/agent/metadata probes prove absence; configuration prose is not evidence |
| `agent_spawn` | `denied` | Multi-agent/plugin features off, no second model runtime, bounded PID policy | Inventory and fixed spawn probe show no agent process or transport |
| `surface_launch` | `saihai_gateway_only` | Host starts one VM from one consumed capability and binds transfer/generation digests | Launch record binds VM instance, image, runtime, capability, generation, input digest, and process-start identity |

If any live device, image component, executable, policy fact, marker, or probe is
missing or drifted, its operation remains unproven. The entire
`managed_worker` claim is suppressed; there is no fallback to the same-rootfs
executor or to an unrestricted agent.

## Host-mediated transport

```text
primary host (trusted)                    ephemeral VM (low trust)
----------------------                    ------------------------
approved frozen work order
  + active generation recheck
  + task-worktree snapshot
  + host-assigned single-use execution ID and five-minute freshness window
  + strict input schema
          |
          +-- read-only input device ---> validate digests; edit disposable tree

capture bounded bytes <--- result stream -- strict result JSON + inline patch
validate schema/bindings
verify patch in disposable host staging
verify changed paths and diff
host alone updates task worktree/run state
host alone may later invoke Vault-write primitive
```

There is no shared-filesystem write-back. The worker cannot call the Vault-write
primitive, assurance scripts, host run store, task-state bridge, or any primary
host service. Those components are neither mounted nor exposed as transport
methods. A result does not contain a target location: the host already knows
the capability-bound task worktree and decides whether to stage a validated
patch there. The worker never selects that destination.

### Input type

`isolated-worker-input.schema.json` defines the exact host-to-worker envelope:

```text
IsolatedWorkerInputV1 = {
  transport_version: "1",
  message_type: "approved_work_order",
  transfer_id, execution_id, issued_at, expires_at,
  task_id, request_id, run_id, step_id: "implement",
  work_order_digest,
  authority: {
    approval_state: "approved",
    capability_digest,
    assurance_binding_digest,
    max_execution_count: 1
  },
  managed_worker_generation: GenerationBinding,
  execution_policy: {
    permission_mode: "edit",
    allowed_operations: [
      "read_context", "write_result", "edit_worktree", "run_tests"
    ],
    allowed_paths: ["."],
    network_egress: "denied",
    provider_dispatch: "denied",
    credential_access: "denied",
    host_write_back: "denied"
  },
  approved_work_order: PayloadDescriptor,
  repository_snapshot: RepositorySnapshotDescriptor,
  result_contract: PayloadDescriptor
}
```

Payload descriptors contain only an opaque `artifact_id`, media type, SHA-256,
and byte count. They contain no host path, guest-selected path, URI, or write
target. The pure validator enforces the declared per-artifact bounds. When the
caller supplies the actual artifact-byte mapping, it also requires exactly the
three declared artifact IDs and verifies each actual byte count and SHA-256.
Artifact placement on the read-only input device is fixed by the future host
controller, not supplied by either envelope. The approved work order bytes
must separately pass `work-order.schema.json`; the snapshot bytes must pass
archive safety checks before device construction and again before guest
extraction.

The host assigns `execution_id`, `issued_at`, and `expires_at`; the worker has
no authority to allocate or alter them. Timestamps use UTC second precision,
`expires_at` is strictly later than `issued_at`, and the window is at most five
minutes. The pure validator checks this shape, ordering, duration, and exact
input/result echo binding. At transport time, the future HOST controller must
also compare its trusted clock at both launch and result acceptance, reject an
expired/not-yet-valid window, and maintain a host-owned single-use ledger keyed
by `execution_id`. The ledger must permit only atomic
`issued -> launched -> result_accepted` transitions: launch requires `issued`,
result acceptance requires `launched`, and any repeated, skipped, unknown, or
terminal-state transition is rejected. That runtime ledger and controller
remain commissioning-phase work and are not implemented by this contract-only
change.

### Result type

`isolated-worker-result.schema.json` defines the exact worker-to-host envelope:

```text
IsolatedWorkerResultV1 = {
  transport_version: "1",
  message_type: "worker_result",
  transfer_id, execution_id,
  issued_at, expires_at,
  task_id, request_id, run_id, step_id: "implement",
  work_order_digest,
  managed_worker_generation: GenerationBinding,
  base_revision,
  worker_result: {
    result_version: "1",
    status: "completed" | "failed" | "blocked",
    summary,
    changed_paths: RelativeGuestPath[],
    tests: TypedTestResult[],
    self_reported_evidence: UntrustedWorkerEvidence[]
  },
  patch: null | {
    artifact_id: "worktree-patch",
    media_type: "text/x-diff",
    encoding: "base64",
    sha256, size_bytes, content_base64
  },
  worker_self_reported_execution_evidence: {
    input_digest_verified: boolean,
    result_schema_verified: boolean,
    network_egress_observed: boolean,
    credential_material_observed: boolean,
    host_mounts_observed: boolean
  }
}
```

The patch is inline so the guest cannot choose an output filename or host
destination. It is the only result artifact and carries a declared SHA-256 and
byte count. The semantic validator decodes it, verifies those declarations
against the actual bytes, and bounds it to 16 MiB. The entire captured result
envelope is independently bounded to 24 MiB before parsing. A non-empty
`changed_paths` list requires a patch, and an empty list forbids one. The host
must verify the patch, apply it first to a disposable staging copy of the exact
`base_revision`, reject changes not listed in `changed_paths`, run the existing
capability and diff verification, and only then use a host-owned primitive to
update the task worktree. A valid envelope is data, not authority to apply a
patch.

Both `worker_result.self_reported_evidence` and
`worker_self_reported_execution_evidence` are UNTRUSTED worker self-reports for
diagnostics only. Their names and schema descriptions encode that trust level.
The validator preserves their typed values but never uses a `pass`, digest,
`true`, or `false` value to satisfy or promote any host-side capability,
artifact, schema, test, diff, isolation, or assurance check. The host must
independently compute authoritative digests and obtain authoritative
capability/diff/assurance evidence from host-owned verification. An adverse
self-report may cause the future controller to reject a run, but a favorable
self-report can never establish a claim.

### Validation and binding

`isolated_worker_transport.py` is a pure validator and has no transport or
filesystem mutation functions. It provides:

| Function | Check |
|---|---|
| `validate_input_envelope` | Strict schema; supported-schema preflight; no location/write-target fields; five-minute freshness-window shape; unique payload identifiers; work-order descriptor digest binding; declared artifact bounds; optional actual input-artifact byte-count/digest verification |
| `validate_result_envelope` | Rejects structures nested beyond 64 levels before recursive validation; strict schema; supported-schema preflight; freshness-window shape; guest-relative changed paths; no location/write-target fields; inline patch base64/size/digest consistency and 16 MiB bound; patch/change consistency; no promotion from worker self-reports |
| `validate_result_bytes` | Requires bytes and rejects a result over 24 MiB before UTF-8 decoding or JSON parsing, normalizes parser/delegated recursion failures to typed fail-closed errors, then delegates to `validate_result_envelope` |
| `validate_exchange` | Exact transfer, execution, issued-at, expiry, task, request, run, step, work-order, generation, and base-revision binding |

Both sides validate the input before execution. The host alone validates the
result and exchange before any state transition. The small schema evaluator
implements every keyword used by these two schemas, including ordinary
`allOf`. A schema preflight rejects every unsupported or unknown keyword,
including applicators such as `oneOf`, rather than silently skipping it.
Unknown payload fields, absolute paths, `..`, Windows separators,
path/URI/write-target fields, malformed or overlong freshness windows, digest
or size mismatch, oversized or over-nested results, generation mismatch, and
transfer/execution misbinding fail closed. Wall-clock expiry and single-use
replay checks remain mandatory HOST transport-controller responsibilities, not
claims made by the pure validator.

## No-write-back argument and contract test

The property is established at two layers:

1. Substrate: the guest has no host filesystem or host-service reachability,
   and its only outbound mechanism emits bytes to a host-owned capture stream.
2. Contract: the result has no destination field. Strict schemas reject unknown
   fields, and the semantic validator recursively rejects `host_path`,
   `vault_path`, `state_root`, `file_uri`, `write_target`, and related location
   keys. Changed paths must be normalized guest-relative repository paths.

`test_isolated_worker_transport.py` verifies a valid bound exchange and rejects
primary-host Vault/state targets, absolute and traversal paths, an ordinary
`allOf` violation, an unsupported applicator, missing/mismatched execution IDs,
malformed freshness windows, input and result artifact size/digest failures,
an oversized pre-parse result stream, patch tampering, and cross-transfer
binding changes. It also proves self-reports remain typed inert data rather
than host evidence. This is contract-level evidence only. The runtime
no-mount/no-service, trusted-clock expiry, and single-use-ledger properties
must be proven again during VM commissioning and on every launch.

## Commissioning and generation sealing

The following is the required lifecycle, not an executable recipe for the
current release:

1. A human reviews the VM image, hypervisor and launch manifests, transport
   controller, secret-free inventory, fixed probes, and isolated-domain
   observer. Their immutable digests become the `codex-scoped-worker` profile
   bindings.
2. A policy change introduces an isolated-domain evidence producer and teaches
   the assurance verifier to accept the four stronger denial facts. It must not
   simply delete `WORKER_PROMOTION_BLOCKED_OPERATIONS`; each operation needs a
   structured attempted denial, independent host/hypervisor observation,
   unchanged external markers, and generation binding.
3. The human begins a new `codex-scoped-worker` commissioning. The host launches
   the fixed commissioning VM, not a caller-supplied command. The suite records
   the positive capability-scoped worktree/process facts, all required denial
   facts, the VM launch binding, and the transport boundary inventory.
4. The human inspects the assurance report. Every required evidence item must
   be current, `pass`, promotable, and bound to the same generation. In
   particular, the four operations above must no longer be represented by the
   same-rootfs policy facts.
5. Only then may the human run the observer-owned `commission-seal` lifecycle
   step. Sealing freezes an immutable generation and atomically selects its
   active pointer. A missing, stale, failed, inconclusive, or drifted fact must
   leave no active `managed_worker` generation.
6. The human runs the claim gate/report and confirms `managed_worker` is
   commissioned rather than suppressed. This is acceptance evidence; schema
   tests are not a substitute.

The current `commissioning-probe` runs on the same rootfs and the current
verifier unconditionally blocks promotion. Do not run that sequence expecting
activation, do not edit an old generation, and do not treat a hand-authored
attestation as authority.

## Live `standard_code_change` acceptance procedure

After the isolated evidence adapter and host transport controller exist and a
current worker generation is active, the human acceptance test is:

1. Approve one `standard_code_change` whose `implement` scope is edit-only,
   `commit=false`, `push=false`, and `network=false` against a clean task
   worktree.
2. Recheck current `action_enforced` and isolated `managed_worker` generations,
   derive and consume one capability, and build the input envelope and snapshot
   from that exact worktree/base revision.
3. Launch one fresh VM and capture one result envelope. No credential may be
   provisioned in the guest for this test.
4. Validate the exchange, patch, changed-path set, generation bindings, and
   base revision. Apply the patch only in host staging, run the required tests,
   then perform the existing post-execution capability and diff verification.
5. Confirm the task worktree contains only the approved diff, `HEAD` is
   unchanged, no push/PR/release occurred, and the run returns to human review.
6. Preserve the assurance report, VM launch/device inventory, transport input
   and result digests, capability/diff verification, and unchanged primary-host
   Vault/state markers as evidence.

Until every prerequisite exists, this criterion is deferred and worker
execution remains suppressed.

## Out of scope

- Provisioning or configuring a VM, container, or separate host.
- Implementing a hypervisor controller, inference broker, archive extractor,
  patch applier, or assurance evidence adapter.
- Commissioning or sealing a production generation.
- Installing software, creating credentials, or logging in from the worker.
- Giving the worker a Vault-write, assurance-state, run-store, Git publication,
  or other primary-host primitive.
