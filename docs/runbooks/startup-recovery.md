# Host startup, recovery, and output acknowledgement

## Startup diagnostics

Use the source and immutable commit recorded in the approved task. For example:

```sh
python3 scripts/saihai.py startup --checkout /absolute/task-checkout \
  --expected-commit APPROVED_FULL_COMMIT_SHA \
  --expected-origin APPROVED_SOURCE_URL --role tech-backend \
  --execution-profile trusted_local_v1 --surface codex --task-id TSK-20260907-example
```

The startup route runs before the normal frontdoor import. It can therefore
report a broken catalog without attempting an ordinary workflow command.
It loads only the primary checkout's `~/dev/Saihai/directory-path.env` with an
empty environment and `require_catalog=True`. It never rewrites that catalog,
creates a substitute Vault, changes credentials, or installs a managed host.

The diagnostic separately reports catalog parsing/availability, host Vault
access, the absence of a proven sandbox grant, checkout origin/commit/tree,
working-tree dirtiness, and each selected role's SHA-256 against the committed
role definition. Commit identity is not a claim that unrelated dirty files are
validated. Missing, mismatched, linked or oversized role definitions block work.
The command does not fetch or install missing roles automatically.

`trusted_local_v1` reports readiness for **separate explicit host task
authorization**. It never reports formal managed-harness assurance. For an
already commissioned `legacy_managed` launch, also provide `--profile-id`,
`--principal-id`, and `--workspace-id owner/repo`. The existing host launch
verifier reopens the root-owned session, validates actual parent-process and
supervisor identity, native/profile digests, checkout binding, expiry and
standard launch kind. A commissioning or direct launch remains diagnostic-only.
A successful repository merge does not commission any host or surface.

Every diagnostic writes bounded private pre-registration and final audit
receipts under the existing host state root's `startup-recovery/` directory.
An inaccessible audit directory is a typed failure, not a success. These
receipts contain diagnostics and digests, not credentials or provider transcripts.

When the original Vault is available but the bootstrap task is missing, an
explicitly approved `--bootstrap-brief` JSON object with `objective`, `scope`,
and `acceptance_criteria` can register it using the existing non-destructive
Vault scaffold. `--project` defaults to `Saihai-Bootstrap`. The task includes
its prior diagnostic audit path and digest, preserving retroactive bootstrap
traceability. Repeated registration cannot overwrite an existing task.
Without that typed brief, missing task registration remains a diagnostic blocker.

## Output acknowledgement monitoring

```sh
python3 scripts/saihai.py output status --stale-seconds 900
```

The host-only status operation inventories bridge-backed runs. Its age starts
at the host's first observation of the current projection; a changed projection
starts a new interval. Schedule/poll this host command at the desired cadence.
Run state and step/iteration are included in the projection digest so an old
acknowledgement cannot acknowledge a later result. The response lists run ID,
age, acknowledgement and typed `stale_output` flags. The authenticated operator
server endpoint is `GET /orchestrator/output-status` (default 900 seconds).

The first stale observation emits `stale_output_raised`. The existing verified
`ack_output` operation clears a matching active condition and emits
`stale_output_cleared`; changed output also records a clear of the old condition.
Neither operation schedules work or grants any new bridge action. The existing
bridge tool inventory remains submit/read/ack only.

Notification hooks are intentionally disabled. No command is accepted from
state-root configuration or executed by this feature, and there is no network
notification side effect. Hook enablement would be a separate host-owned feature.

## Migration from the interim bootstrap exception

| Interim clause | Saihai counterpart |
|---|---|
| Empty-environment primary catalog load | `startup_recovery.inspect_startup`, catalog diagnostic |
| Vault exists and is readable/writable; no substitute | Availability diagnostic, fail-closed result, no create/repoint path |
| Fresh bootstrap requires human-confirmed recovery | `fresh_bootstrap_required` / `human_confirmation_required`; explicit typed registration only after the original Vault recovers |
| Register bootstrap actions after Vault recovery | Pre-registration audit receipt linked into the new canonical task by the Vault scaffold |
| Required roles must be readable | Selected role files checked by no-follow bounded reads |
| Trusted role source and immutable commit | Explicit expected origin/commit, role bytes checked against that commit |
| Actual execution context, not merely valid catalog | Separate execution profile and standard host-launch verifier; direct managed launch is diagnostic-only |
| Do not replace unavailable roles with generic agents | Typed identity failure; no fallback implementation |
| Resume normal work after prerequisites | `ordinary_work_allowed` only when checks and task binding pass; separate execution authorization remains required |

## COMMON-AGENTS replacement text

The following is the Saihai-side replacement for the already approved usage-first
startup policy. The parent task applies it through the dotfiles PR workflow;
this document does not modify dotfiles or commission a host.

> Before ordinary work, run Saihai's `startup` diagnostic for the actual surface
> and explicitly selected `trusted_local_v1` or `legacy_managed` profile, using
> the task's trusted source, immutable checkout commit, selected roles and task
> ID. The canonical command and recovery steps are documented in
> `docs/runbooks/startup-recovery.md` in the Saihai repository.
>
> The primary `~/dev/Saihai/directory-path.env`, loaded with an empty environment,
> remains the sole directory source. A failed catalog, original Vault access,
> role identity or required managed launch check stops ordinary execution.
> Never hide a failure by creating a substitute Vault, repointing the catalog,
> installing credentials, or claiming that a direct launch is commissioned.
>
> Preserve startup audit evidence. After human-directed recovery of the original
> Vault, register any bootstrap activity through an explicitly approved typed
> task brief and retain the prior audit reference. Missing roles may be recovered
> only from the separately approved source and immutable commit; no generic-role
> substitution is permitted.
>
> Trusted-local readiness is distinct from formal managed assurance and still
> requires the existing host task authorization. Normal approved changes use
> focused validation and feature-level integrated validation without mandatory
> duplicate reviews; permission/authentication/data-loss changes receive the
> single scoped review required by the approved policy. This startup flow adds
> no cumulative retry stop, release authorization or credential-management grant.
