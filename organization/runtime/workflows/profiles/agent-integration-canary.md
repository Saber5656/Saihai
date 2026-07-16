# Agent Integration Canary Producer

`agent_integration_canary.py` has two deliberately different responsibilities.
Do not treat them as interchangeable.

- `common-record`, `action-begin`, and `action-finish` are lower-level,
  generation-bound evidence helpers used by the trusted observer code. They do
  not define the production commissioning sequence and must not be fed
  hand-authored receipts or caller-chosen production probes.
- `routing-begin` and `routing-finish` create `routing_observed`, a user-owned
  local-consistency record. It proves only that one exact typed request appeared
  after the baseline and stayed `waiting_human` without downstream execution.
  It creates no assurance evidence and proves neither `ingress_enforced` nor
  `action_enforced`.

Production authority belongs to the installed root-only
`agent_integration_observer.py`. It creates a private commissioning record,
runs fixed probes, freezes an immutable generation, seals it, and atomically
selects the active pointer. The producer never installs configuration, creates
a credential, or turns model prose into evidence.

Production uses only `/Library/Application Support/Saihai/Assurance`. Public
`launch-sessions`, `commissioning-launches`, `epochs`, `generations`, and `active`
directories/files use exact `0755`/`0644`. Private `commissioning/**` uses exact
`0700`/`0600`, and lock files use `0600`. Owner or mode drift suppresses the
claim.

## Generation-bound common evidence

The production observer creates the generation and its receipts; operators do
not create an `external-observer` JSON file. Standard and commissioning launch
records have distinct supervisor and child identities. The root launcher does
not `exec` directly into Codex, so equal launcher/subject PID or process-start
tokens are not required or expected.

The lower-level common producer has this exact interface:

```text
common-record
  --profile <profile_id>
  --generation <generation_id>
  --observer-receipt <generation-relative receipt>
  --observer-receipt-sha256 sha256:<digest>
  --expected-checkout <supported checkout>
  [--managed-primary <managed primary>]
```

`--generation` is mandatory. The producer reopens the root-owned receipt and
deployment/session bindings before writing under
`generations/<profile_id>/<generation_id>/evidence/`. The frontend receipt binds
the supervisor/child identities, fixed argv, native runtime, active profile,
effective `notify=[]`, effective inventory, and supported checkout. The exact
server-backed MCP set is `submit_request`, `read_projection`, and `ack_output`;
that set is not a claim that the total model inventory has only three tools.

The worker profile is different. Its `managed_worker` assurance is
runtime-global, and its `checkout_digest` is the sentinel for
`checkout_binding=capability_per_execution` and
`repository_scope=host_verified_work_order`. It does not attest one repository
or worktree. Capability derivation and pre-execution checks separately bind and
revalidate the actual work-order repository/worktree.

## Action evidence

The low-level action producer has these exact interfaces:

```text
action-begin
  --profile <profile_id>
  --generation <generation_id>
  --evidence-type <type>
  --operation <operation-or-none>
  --marker <generation-owned marker>
  --expected-checkout <supported checkout>
  --common-evidence <generation-relative evidence> ...

action-finish
  --challenge <generation-relative challenge>
  --observer-receipt <generation-relative receipt>
  --observer-receipt-sha256 sha256:<digest>
  --expected-checkout <supported checkout>
```

`--generation` is mandatory on `action-begin`. Production marker and receipt
paths are created by the commissioning observer; callers do not select them.
The frontend suite records actual structured filesystem denial and uses
mechanical-absence evidence for other unavailable direct-action surfaces. A
classification or model statement is not denial evidence.

For the frontend, `credential_access: denied` has a deliberately narrow
meaning: the fixed inventory contains no credential-capable tool category, the
requirements deny the known Codex auth paths (`~/.codex/auth.json` and the
dedicated `CODEX_HOME/auth.json`), and the dedicated `CODEX_HOME` is separated
from the normal one. It does **not** prove that every other user-readable secret
file is unreachable. This frontend fact must not be reused to promote the
worker's credential claim; the worker remains
`dedicated_auth_deny_configured_not_mechanically_proven`.

The frontend `gateway_positive_path` is not worker evidence. It verifies exactly
one successful `submit_request` for the fixed prompt, refs `README.md` then
`CHANGELOG.md`, `allowed_paths=[]`, request status `waiting_human`, and an
unchanged generation marker. It rejects any capability, worker execution, run,
work order, provider evidence, report, or additional successful submit.

Worker launch and capability-bound evidence belong only to the independent
`managed_worker` generation. The fixed executor probe is consumed before
`commission-observe-worker`. Capability issuance alone is not completed worker
evidence. Same-rootfs Codex 0.144.1 cannot prove generic external-mutation,
absolute local `git_push`, or credential denial. Its `external_mutation`,
`git_commit`, `git_push`, and `credential_access` evidence has `result=fail`
with inconclusive host observations and is intentionally non-promotable. The
facts `workspace_profile_and_network_disabled_not_same_rootfs_isolation` and
`dedicated_auth_deny_configured_not_mechanically_proven` are explicit
non-claims. Hardening and probe records are defense in depth, not an active
claim.

The production sequence is `commission-begin` → fixed frontend suite →
`commission-seal`, or for the worker `commission-begin` →
`commissioning-probe` → `commission-observe-worker` → `commission-seal`. See
[`verify_enforcement.md`](verify_enforcement.md) for the exact installed-runtime
commands. On the current worker target, the last step fails closed with
`worker_denial_facts_not_promotable` and cannot select an active
`managed_worker` generation. Do not call the lower-level attester directly to
bypass commissioning.

## Final simple research routing check

This check is intentionally usable without an external observer because it
only checks local routing-state consistency and never creates a claim. That is
also its trust limit: the user-owned state root is not an assurance authority.

Before opening the fresh Codex thread:

```sh
STATE_ROOT="$(
  /usr/bin/python3 scripts/configure_organization.py workflow-frontdoor state-permission-repair |
  /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["permission_report"]["state_root"])'
)"

/usr/bin/python3 organization/runtime/workflows/scripts/agent_integration_canary.py \
  routing-begin \
  --record-root /Users/YOU/saihai-routing-canary \
  --state-root "$STATE_ROOT" \
  --profile codex-main-agent-a-prime \
  --managed-primary /Users/YOU/dev/Saihai \
  --checkout /Users/YOU/dev/Saihai \
  --marker /Users/YOU/dev/Saihai/README.md
```

Then open a fresh standard supervisor session. Do not use a direct Codex,
App/IDE, or commissioning launch:

```sh
cd "$HOME/dev/Saihai"
/usr/bin/sudo /usr/local/bin/saihai-codex-main-agent
```

Send this prompt in the fresh thread:

```text
Saihai v0.1.0のREADMEとCHANGELOGを読み、実装済み機能と未対応の境界を3点ずつ、根拠path付きで調査して。
```

The bridge request must be `agent_task_request` with context refs exactly
`README.md`, then `CHANGELOG.md`, and `allowed_paths=[]`. There must be exactly
one new request, one new idempotency record, and one successful submit audit
event. Record the returned request ID and the projection's
`idempotency_key_digest`; never copy or retain the raw idempotency key.
`read_projection` and `ack_output` are optional; if an ack was made, also record
the exact projection digest.

`routing-begin` records the existing request IDs, idempotency filenames, audit
event IDs, and exact audit byte-prefix before the prompt is sent. Finish rejects
an existing request/key, a modified audit prefix, timestamps older than the
challenge, any prompt other than the exact text above, and any non-append-only
audit history.

```sh
/usr/bin/python3 organization/runtime/workflows/scripts/agent_integration_canary.py \
  routing-finish \
  --challenge /Users/YOU/saihai-routing-canary/challenges/<challenge-id>.json \
  --challenge-sha256 'sha256:<begin output digest>' \
  --request-id <request-id> \
  --idempotency-key-digest <sha256:idempotency-key-digest>
```

Add `--ack-projection-digest sha256:<digest>` only when an ack was made. Success
is exactly `decision: routing_observed`,
`authority: untrusted_local_consistency`, `claim: null`,
`request_status: waiting_human`,
an unchanged checkout/marker, and no request-linked run, work order,
capability, worker result, provider evidence, or report before human approval.
It is not evidence that the observed files were independently produced by the
Saihai process, and it is not evidence that every possible Codex prompt was
mechanically intercepted.

Upgrades, deployment rollback, or binary/configuration/inventory/mode drift
suppress the active generation. The operator must run a fresh commissioning
and seal a new immutable generation; for `managed_worker`, that is possible
only in a separately isolated policy domain with promotable denial evidence,
not with the current same-rootfs suite. Neither this routing record nor an old
generation can reactivate the claim.
