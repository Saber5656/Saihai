# Isolated Worker Policy Domain and Transport Contract

Status: design and contract only. This document does not claim that an
isolated domain is deployed or that `managed_worker` can currently be sealed.

## Decision

Use a dedicated, single-execution, hardware-virtualized VM as the worker policy
domain. The VM has a read-only measured base image, one ephemeral writable
work disk, no host filesystem mounts, no network interface, no inherited
environment, no credential material, and no host service socket. The host
delivers an approved work order and repository snapshot on a read-only input
device. Model inference uses one narrowly typed host-mediated inference broker;
the guest receives no credential and has no general network route. The guest
returns exactly one typed result over a separate dedicated guest-to-host byte
stream. The host treats those bytes as inert data until both schema and
semantic validation pass.

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
   general-purpose vsock endpoint. The only host-service exception is the
   generation-bound, narrowly typed inference-broker channel defined below.
5. The outbound result channel is a byte stream bounded to 24 MiB. The host
   checks that byte bound before UTF-8 decoding or JSON parsing. It is distinct
   from the inference channel, has no RPC methods, and cannot name or open a
   host file. Host code captures bytes into private staging; it does not
   deserialize into filesystem calls.
6. The guest runtime uid cannot change mounts, policy, the measured image, or
   the input device. Its writable locations are the disposable work disk and
   bounded scratch only.
7. No credential file, token, secret environment variable, login state, or
   credential helper enters the image, input, runtime environment, scratch, or
   broker response. The host-owned inference broker alone holds provider
   credentials and makes provider calls. Until that broker and its dedicated
   channel are implemented, reviewed, generation-bound, and commissioned with
   a real `openai-codex` run, `managed_worker` remains suppressed.
8. VM termination destroys the writable disk and scratch after the host has
   captured the result. No generation, capability, or worktree state is reused
   from a previous VM.

## Denial-fact matrix for the selected domain

| Required operation | Required coverage | VM mechanism | Promotable evidence requirement |
|---|---|---|---|
| `filesystem_write` | `capability_scoped` | Writable disposable work disk only; input and base image are read-only | Fixed positive marker appears only in the guest worktree; mount inventory shows no other writable target |
| `shell_exec` | `capability_scoped` | Pinned runtime, empty inherited environment, bounded process/timeout policy | Runtime binary/config/argv and process-start identity are host observed and generation bound |
| `process_spawn` | `capability_scoped` | VM PID/cgroup limit and executable inventory | Exact runtime child is observed; unbound process/agent launch probes fail |
| `network_egress` | `denied` | Guest-directed general network egress is denied: no NIC, network backend, metadata route, destination selector, or general-purpose host socket; the sole exception is host-owned dispatch of typed inference text to the pinned provider | Hypervisor live-device inventory and fixed IPv4, IPv6, Unix-socket, metadata, destination-selection, and invalid broker-message probes show no guest-controlled general network route; broker audit accounts for the separately authorized host inference calls |
| `external_mutation` | `denied` | Separate kernel; no shared FS, host devices, sockets, clipboard, guest agent, or writable input | Host/hypervisor inventory plus attempts against every declared external surface; primary-host markers remain unchanged |
| `provider_dispatch` | `denied` | The guest has no provider client, provider selector, credential, or route; it can request only a generation-pinned inference turn from the host broker | Guest inventory and direct-dispatch probes prove absence; broker audit proves every provider call was host-owned, execution-bound, and produced only by a valid typed inference request |
| `git_commit` | `denied` | Snapshot excludes `.git`; VCS executable/helper inventory is absent; MAC denies VCS metadata creation | Structured commit and metadata-creation attempts are denied; base revision and snapshot metadata markers do not change |
| `git_push` | `denied` | No Git metadata/client/helper, external route, shared FS, socket, or additional writable device | Structured network and local-destination push attempts are denied; all destination markers remain unchanged |
| `pr_create` | `denied` | No network, provider tool, browser, plugin, or credential | Mechanical absence inventory and fixed attempt produce no external event |
| `release_publish` | `denied` | Same controls as PR creation; no signing/release credentials | Mechanical absence inventory and fixed attempt produce no external event |
| `credential_access` | `denied` | Secret-free measured image/input/environment; provider credentials remain inside the host broker and are excluded from requests, responses, errors, and logs returned to the guest | Image/input/env/device inventory, broker redaction tests, and fixed known-path/agent/metadata probes prove the guest cannot obtain credential material |
| `agent_spawn` | `denied` | Multi-agent/plugin features off, no second model runtime, bounded PID policy; broker contract has no tool, subagent, session, or arbitrary model-dispatch method | Inventory and fixed spawn/broker-message probes show no agent process or agent-capable transport; broker audit shows one pinned inference stream for the execution |
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

host inference broker <== typed channel ==> bounded inference request/response
  + host-only provider credentials          no provider/network/agent controls
  + generation-pinned provider/model

capture bounded bytes <--- result stream -- strict result JSON + typed changes
validate schema/bindings
verify regular-file changes in disposable host staging
verify exact unique changed-path set and diff
host alone updates task worktree/run state
host alone may later invoke Vault-write primitive
```

There is no shared-filesystem write-back. The worker cannot call the Vault-write
primitive, assurance scripts, host run store, task-state bridge, or any primary
host service. Those components are neither mounted nor exposed as transport
methods. A result does not contain a target location: the host already knows
the capability-bound task worktree and decides whether to stage a validated
file-change set there. The worker never selects that destination.

### Host-mediated inference broker

The selected inference path is one HOST-mediated broker, not in-guest provider
access. This is required because the managed backend is `openai-codex`, while
the selected VM intentionally has no NIC and receives no credential. The broker
is a host-owned control-plane component on a dedicated channel. It is not a
general socket, proxy, shell, filesystem service, tool dispatcher, or agent
gateway.

The guest may submit only this conceptual request type:

```text
InferenceRequestV1 = {
  protocol_version: "1",
  execution_id, managed_worker_generation_id,
  sequence: integer,
  role: "implementation_worker",
  input_utf8: string
}
```

The host returns only:

```text
InferenceResponseV1 = {
  protocol_version: "1",
  execution_id, managed_worker_generation_id,
  sequence: integer,
  status: "completed" | "length_limited" | "failed",
  output_utf8: string,
  usage: { input_tokens: integer, output_tokens: integer }
}
```

The commissioned contract must set finite generation-bound limits for request
bytes, response bytes, turn count, token count, and per-turn/total duration.
The request has no provider/model selector, URL, headers, credential fields,
tool definitions, function calls, filesystem locations, agent/session IDs, or
arbitrary metadata. The host selects one pinned provider/model from the active
generation, verifies execution/generation/sequence binding before dispatch,
and returns bounded UTF-8 output or a typed failure. Provider errors are mapped
to that failure without returning headers, bodies, identifiers, or credential
material. Invalid, oversized, out-of-order, cross-execution, or excess-turn
messages fail closed and cause no provider call.

The four relevant denial facts retain their precise scope:

| Denial fact | Why the broker does not weaken it |
|---|---|
| `provider_dispatch=denied` | The denied authority is guest-directed provider dispatch. The guest cannot choose or address a provider; only the host may translate a valid bound inference turn into the generation-pinned provider call. Broker audit and negative direct-dispatch probes distinguish this host control-plane action from guest authority. |
| `credential_access=denied` | Credentials exist only in the host broker's process/domain. They are absent from the VM and prohibited from request, response, error, and guest-visible log types. Redaction/canary probes must show no credential bytes cross the channel. |
| `network_egress=denied` | The denied authority is guest-directed general network egress. The VM has no NIC or general proxy and cannot choose a destination, protocol, header, or request method. Inference text necessarily leaves through the host's pinned provider call, but that separately authorized host control-plane action is fully broker-audited and exposes no general route. Invalid framing/method/destination probes must fail without an unaccounted external side effect. |
| `agent_spawn=denied` | The broker provides model inference only. Its types contain no tool, subagent, plugin, session creation, arbitrary model selection, or background-job operation, and one execution is bounded to one ordered inference stream. |

The active generation must bind immutable digests for the broker request and
response schemas, broker implementation, provider/model policy, bounds and
redaction policy, channel/device configuration, and broker audit/evidence
collector. The launch record must bind the channel instance and broker process
identity to the same `execution_id` and generation as the VM and transport.
Commissioning must preserve the broker artifacts and audit, a successful real
worker inference transcript digest, request/response byte and turn counters,
provider/model binding, credential-canary results, direct-network/direct-
provider/invalid-message/agent-spawn negative probes, and the live VM device
inventory showing no general network path.

This section defines a design contract only. The broker, channel, schemas, and
commissioning evidence producer are deferred host work; none is implemented by
the current pure transport validator. `managed_worker` must remain suppressed
until that work is independently reviewed and the complete evidence set is
commissioned. A credentialed or network-enabled in-guest fallback is forbidden.

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
input/result echo binding. The freshness predicate is the half-open interval
`issued_at <= trusted_observed_at < expires_at`: equality with `issued_at` is
valid, while equality with `expires_at` is expired and rejected fail-closed.
`validate_exchange` requires the trusted first-result-byte receipt time and
checks this predicate. The future HOST controller must apply the same predicate
to a trusted clock at launch.

The host-owned single-use ledger is keyed by `execution_id` and permits only
`issued -> launched -> consumed -> {accepted | rejected | expired | failed}`.
Launch atomically requires `issued`. On the first result byte—not after JSON,
schema, digest, binding, or semantic validation—the controller atomically
changes `launched` to durable `consumed` and records the trusted receipt time
and captured-stream identity. That first byte consumes the execution even when
the result is malformed, oversized, tampered, cross-bound, or later found
expired. Validation completes with exactly one terminal state: `accepted` for
a valid exchange (including a valid typed worker result whose own status is
`failed` or `blocked`), `rejected` for malformed or invalid bytes,
`expired` when the trusted predicate fails at receipt or an unconsumed launch
reaches expiry, and `failed` for a host capture/validation failure that prevents
a more specific classification. Every later byte stream or result for the same
execution is rejected without parsing or retry, regardless of terminal state.

The `consumed` record and captured bytes/digest must be durable before parsing,
so crash recovery resumes classification of that same capture only; it can
never return the record to `launched` or accept replacement bytes. Unknown,
skipped, repeated, or terminal-state transitions fail closed. The atomic ledger,
capture controller, and recovery implementation remain deferred commissioning-
phase host work and are not implemented by this contract-only change.

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
    artifact_id: "worktree-file-changes",
    media_type: "application/vnd.saihai.regular-text-file-changes+json",
    format_version: "1",
    changes: (
      { operation: "create" | "replace", relative_path,
        file_type: "regular", content_encoding: "base64",
        content_media_type: "text/plain; charset=utf-8",
        sha256, size_bytes, content_base64 }
      | { operation: "delete", relative_path, file_type: "regular" }
    )[]
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

The `patch` field is retained as the envelope's result-artifact slot, but it no
longer contains a unified diff. It contains only complete typed operations on
UTF-8 regular files. `create`, `replace`, and `delete` are the only operations.
Rename, copy, symlink, submodule, special-file, mode, binary-delta, unified-diff,
and other patch constructs are not representable; strict schemas reject their
fields or enum values. Write content is inline base64, and the semantic
validator verifies each decoded byte count and SHA-256, rejects invalid UTF-8
and NUL content, and limits aggregate decoded content to 16 MiB. The entire
captured result envelope is independently bounded to 24 MiB before parsing.
`MAX_FILE_CHANGES` is sealed to 512 for this managed-worker generation and is
enforced on both `changed_paths` and `patch.changes`; changing it requires a new
generation. Each array is also limited to 256 KiB of aggregate UTF-8 path bytes.

Every `relative_path` and every `changed_paths` entry must be one canonical
UTF-8/NFC POSIX repository-relative path. The supported filesystem namespace is
the portable intersection used by POSIX staging and NTFS-safe write-back:
slash-separated components, regular files only, at most 255 UTF-8 bytes per
component and 1024 UTF-8 bytes for the whole relative path. Every path and
component must consist only of Unicode scalar values and round-trip exactly
through strict UTF-8 encode/decode. Absolute forms, empty/dot/dot-dot
components, non-canonical repeated separators, backslashes, colons (including
NTFS alternate-data-stream syntax), C0/C1 controls, Unicode format/bidi control
characters, and reserved VCS control components (`.git`, `.gitmodules`, `.hg`,
`.svn`, `.bzr`, `_darcs`, `.pijul`, `.fossil-settings`, `.jj`, `.sl`, `CVS`,
`RCS`, or `SCCS`, case-insensitive) are forbidden. Windows drive prefixes,
surrogate code points, and non-NFC Unicode are therefore rejected. JSON escaping
is resolved before this check, so a literal quote in a structured path is
unambiguous; there is no diff-header quoting grammar. Both lists must themselves
be unique without case-insensitive filesystem collisions, and their canonical
path sets must match exactly in both directions. A non-empty `changed_paths`
list requires a non-empty file-change payload, and an empty list forbids one.

Before any task-worktree mutation, the future host controller must interpret
the typed operation only in a disposable staging copy of the exact
`base_revision`. It must resolve each component beneath the staging root using
no-follow/descriptor-relative filesystem operations, reject any symlink or
non-regular existing target/ancestor, enforce create/replace/delete existence
semantics, and recompute the resulting diff and exact path set. Only after the
existing capability and diff verification passes may a host-owned primitive
apply the independently reconstructed regular-file result to the task
worktree. A valid envelope is data, not authority to apply a change.

For this generation, staging must budget no more than 512 target-inode
operations and no more than 16 MiB of result-supplied file content. The
disposable staging tree still requires the exact base-revision footprint; above
that baseline, the controller must reserve bounded space for the 16 MiB content
plus filesystem metadata and the recomputed diff, and fail closed on exhaustion.
Commissioning must enforce a host-owned 60-second staging and verification
deadline. These inode, disk, and time budgets are controller obligations; this
contract validator only enforces the input amplification bounds available before
staging exists.

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
| `validate_result_envelope` | Rejects structures nested beyond 64 levels before recursive validation; strict schema; supported-schema preflight; freshness-window shape; at most 512 changes; strict UTF-8/NFC portable guest-relative paths with component, total, and aggregate byte bounds; VCS-control-path exclusion; path duplicate rejection and exact unique path-set equality; typed UTF-8 regular-file operations only; per-content base64/size/digest consistency and 16 MiB aggregate bound; no location/write-target fields or promotion from worker self-reports |
| `validate_result_bytes` | Requires bytes and rejects a result over 24 MiB before UTF-8 decoding or JSON parsing, parses once with recursive duplicate-member rejection, normalizes parser/delegated recursion failures to typed fail-closed errors, then delegates only the normalized object to `validate_result_envelope` |
| `validate_exchange` | Exact transfer, execution, issued-at, expiry, task, request, run, step, work-order, generation, and base-revision binding; required trusted first-byte receipt time satisfying `issued_at <= t < expires_at` |

Both sides validate the input before execution. For results, first-byte capture
necessarily performs the durable `launched -> consumed` transition before JSON,
schema, or exchange validation. The host alone then validates the captured
result and exchange before terminal acceptance and before any write-back. The
small schema evaluator implements every keyword used by these two schemas,
including ordinary `allOf` and the array `maxItems` bounds. A schema preflight
rejects every unsupported or unknown keyword,
including applicators such as `oneOf`, rather than silently skipping it.
Unknown payload fields, absolute paths, `..`, Windows separators, VCS metadata,
duplicate JSON members, duplicate or mismatched change sets, unsupported file
operations/types, binary content, path/URI/write-target fields, malformed or
overlong freshness windows,
trusted-clock expiry (including exact equality), digest or size mismatch,
oversized or over-nested results, generation mismatch, and transfer/execution
misbinding fail closed. Atomic first-byte consumption and replay rejection
remain mandatory HOST transport-controller responsibilities, not claims made by
the pure validator.

## No-write-back argument and contract test

The property is established at two layers:

1. Substrate: the guest has no host filesystem reachability. Its only host
   channels are the narrowly typed inference exchange and the host-owned result
   capture stream; neither exposes a filesystem or write-back method.
2. Contract: the result has no destination field. Strict schemas reject unknown
   fields, and the semantic validator recursively rejects `host_path`,
   `vault_path`, `state_root`, `file_uri`, `write_target`, and related location
   keys. The result artifact is a closed typed regular-text-file operation set,
   not an opaque diff. Its canonical unique paths must exactly equal the unique
   `changed_paths` set, and VCS metadata paths are forbidden.

`test_isolated_worker_transport.py` verifies a valid bound exchange and rejects
the exact `b/../../Agents-Vault/task.md` bypass, primary-host Vault/state-tree
targets, absolute/traversal/backslash/NUL, surrogate, bidi/format-control,
overlong, ADS-like, and VCS paths, duplicate JSON members and path declarations,
path-set mismatches in both directions, old opaque/quoted/rename/copy diff
payloads, symlink/submodule/mode objects, binary content, and tampered file
content. It also covers exact path, operation-count, aggregate-path-byte, and
expiry boundaries, an unambiguous structured quoted path, input and result
bounds, cross-transfer binding changes, and inert self-reports. This is
contract-level evidence only. The runtime no-mount,
safe-staging, broker-channel, atomic-consumption, and replay properties must be
proven again during VM commissioning and on every launch.

## Commissioning and generation sealing

The following is the required lifecycle, not an executable recipe for the
current release:

1. A human reviews the VM image, hypervisor and launch manifests, transport
   controller, safe typed-change staging code, inference broker and its schemas,
   broker provider/model/bounds/redaction policy, dedicated channel
   configuration, secret-free inventory, fixed probes, ledger recovery logic,
   and isolated-domain observer. Their immutable digests become the
   `codex-scoped-worker` profile bindings.
2. A policy change introduces an isolated-domain evidence producer and teaches
   the assurance verifier to accept the four stronger denial facts. It must not
   simply delete `WORKER_PROMOTION_BLOCKED_OPERATIONS`; each operation needs a
   structured attempted denial, independent host/hypervisor observation,
   unchanged external markers, and generation binding.
3. The human begins a new `codex-scoped-worker` commissioning. The host launches
   the fixed commissioning VM, not a caller-supplied command. Through the bound
   broker it completes a real `openai-codex` inference turn and a positive
   capability-scoped worktree change. The suite records the broker artifacts,
   channel/process/execution binding, transcript digest and bounds, provider/
   model binding, credential redaction/canary evidence, all broker negative
   probes, all required denial facts, the VM launch binding, the exact typed
   file-change/staged diff, ledger transitions, and transport/device inventory.
4. The human inspects the assurance report. Every required evidence item must
   be current, `pass`, promotable, and bound to the same generation. In
   particular, the four operations above must no longer be represented by the
   same-rootfs policy facts, and no positive inference evidence may come from a
   credentialed/networked guest or an unbound broker/channel.
5. Only then may the human run the observer-owned `commission-seal` lifecycle
   step. Sealing freezes an immutable generation and atomically selects its
   active pointer. A missing, stale, failed, inconclusive, or drifted fact must
   leave no active `managed_worker` generation.
6. The human runs the claim gate/report and confirms `managed_worker` is
   commissioned rather than suppressed. This is acceptance evidence; schema
   tests are not a substitute.

The broker/runtime/controller/evidence work remains deferred. The current
`commissioning-probe` runs on the same rootfs and the current verifier
unconditionally blocks promotion. Do not run that sequence expecting
activation, do not edit an old generation, and do not treat a hand-authored
attestation as authority. `managed_worker` stays suppressed until all deferred
host work above is implemented, independently reviewed, and commissioned.

## Live `standard_code_change` acceptance procedure

After the isolated evidence adapter and host transport controller exist and a
current worker generation is active, the human acceptance test is:

1. Approve one `standard_code_change` whose `implement` scope is edit-only,
   `commit=false`, `push=false`, and `network=false` against a clean task
   worktree.
2. Recheck current `action_enforced` and isolated `managed_worker` generations,
   derive and consume one capability, and build the input envelope and snapshot
   from that exact worktree/base revision.
3. Launch one fresh VM, complete real worker inference only through the bound
   typed host broker, and capture one result envelope. No credential or general
   network route may be provisioned in the guest for this test.
4. Atomically consume the execution on the first result byte; validate the
   trusted receipt time, exchange, typed regular-file changes, exact unique
   changed-path set, generation bindings, and base revision. Materialize the
   changes only in safe host staging, run the required tests, then perform the
   existing post-execution capability and diff verification.
5. Confirm the task worktree contains only the approved diff, `HEAD` is
   unchanged, no push/PR/release occurred, and the run returns to human review.
6. Preserve the assurance report, VM launch/device inventory, broker artifact
   and channel bindings, provider/model and bounded transcript evidence,
   broker denial probes, ledger transitions, transport input/result digests,
   capability/diff verification, and unchanged primary-host Vault/state markers
   as evidence.

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
