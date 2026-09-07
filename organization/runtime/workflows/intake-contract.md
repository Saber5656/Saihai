# Bounded request intake and requirement transport

The host owns the original task scope and provider plan. Classification and the
work brief are data, never permission or workflow authority. The deterministic
selector remains the only workflow selector. Existing `human_supplied` and
`deterministic_fixture` proposal paths remain compatible; callers cannot claim
`bounded_classifier_step` without the host intake producer.

## Host preparation and execution

1. Keep the verbatim request in its private host request file. Construct the
   canonical requirement ledger from the accepted task scope. The existing
   `scope_contract_version: 1` ledger format is shared with ITB: stable requirement
   IDs, all task units, explicit selection/version, non-goals, acceptance criteria,
   owner/repository, dependencies and observed task/Issue bindings. Unselected
   requirements remain pending. A ledger is not an authority receipt.
2. Use an existing private `TrustedLocalAuthorization` and approved model. Run
   `python3.11 scripts/saihai.py usage prepare --request /absolute/request.json
   --requirement-ledger /absolute/ledger.json --authorization /absolute/authority.json
   --state-root /absolute/private-state` (one shell invocation).
3. The host stores the source, invokes classification and shaping under readonly
   filesystem policy, and records actual process receipts. Each stage allows at
   most three invalid-output attempts; recovery exhaustion is an internal block,
   never an automatic product question. Replaying a persisted stage reuses its
   output; an uncertain process claim needs reconciliation instead of another
   invocation. The configured model is taken from host authority and Max effort
   follows the currently approved active-role policy. There is no model fallback.
4. The result contains a `work_brief_ref`, prepared request and `host_binding`.
   The host checks the brief against the already accepted scope and pins its
   digest as optional `intake_digest` in the independent authorization. This is
   ordinary host binding of existing task scope, not a request for another human
   approval. Preparation does not write or expand authority. Material changes to
   requirements still require the actual user's decision.
5. Run the prepared request through the normal `usage run` entry with that host
   authorization. The worker gets the grounded brief and complete requirement/unit
   accounting, not the private source prompt, history or provider transcript.
   `usage advance` retains the normal publication owner and actual CI requirements.

The programmatic legacy frontdoor uses `proposed_request(intake_provider=...,
requirement_ledger=..., intake_model=...)`. The bridge cannot inject these controls.
Approval material binds both the brief reference and its displayed summary;
create/drain and provider/worker execution re-open the reference. The request,
run, work order and immutable step snapshot carry the same version and digest.
Swapping a summary, artifact, classification, source or selected unit fails closed.

## Grounding and questions

`work-brief.schema.json` defines bounded objective, in/out scope, constraints,
acceptance criteria, risk notes, open questions and provenance bindings. Objective,
scope, constraints and acceptance criteria must match the selected canonical unit;
shaping cannot invent extra work. Every requirement has a selected/pending entry.
An open question is accepted only for a requirement explicitly marked by the host
with `requires_decision: product_requirement | material_scope | security_authority`.
At most one material question is transported; unresolved material questions block
execution. Formatting problems, malformed classifications and missing technical
metadata are internal recovery work, not human confirmation.

Only `source_kind: user_request` is accepted. Explicit private transcript/secret
input is rejected; free-form similarity to a request is not a safety rule. API
names, paths and short exact authorized phrases are allowed. Source classification
is the host's responsibility; these checks do not claim to detect arbitrary secrets
hidden in otherwise authorized prose. Transport allowlists grounded fields while
preserving the full ledger privately.

## Actual diff scope

An edit intake requires selected-unit `change_contracts`. Each contract contains
exact `path`, immutable `base`, selected `requirement_ids`, `operations`, `ranges`
and `insertions`. Example:

```json
{
  "path": "src/example.py",
  "base": "0123456789012345678901234567890123456789",
  "requirement_ids": ["R2"],
  "operations": ["edit"],
  "ranges": [[10, 24]],
  "insertions": [24]
}
```

Ranges are one-based coordinates in the exact base; insertion boundaries are
explicit (0 is the beginning). The host reads the real Git diff, including staged
and unstaged changes. An unrelated hunk in the same file, a dependency file outside
the selected unit, an unapproved operation, symlink or stale base is rejected before
validation/publication. Creation/deletion/mode/binary edits require explicit
operation contracts. The host records requirement IDs and patch digests beside the
actual validation evidence. This enforces declared hunk scope; it is not a proof of
semantic equivalence for every edit inside an approved range. Hosts should keep
ranges narrow. Broader legitimate prerequisites must be explicit selected-unit
requirements and contracts, never inferred from a whole-worktree permission.

`requirement_scope.revise` preserves all untouched requirements, prior requirement
text, unit non-goals and incidental observations. Additions/replacements are
explicit and compare the prior digest. After an actual host-confirmed requirement
decision, `resolved_decisions` explicitly removes its pending question marker and
records the resolution in history. A revised intake uses a new request ID and
version; old worker results stay bound to the old digest. If main integration conflicts,
`intake_scope_refresh_required` asks the host to map and rebind the accepted hunk
contracts to the new base before conflict editing; the old line ranges are never
automatically reused on another base. Existing unit dependencies
without trusted completion evidence remain blocked instead of being guessed done.

## Evidence and remaining integration

Artifacts live under private `intakes/<request_id>/`; source, provider attempts and
process receipts stay host-only. Brief references are content-addressed; persistence
means local read-back, not durable canonical Vault ACK. Return the descriptor to the
single existing task/Vault writer. This feature does not add a second Vault writer.

Offline tests exercise actual fixture subprocesses/Git changes and legacy frontdoor
proposal/approval/drain. They do not attest a live model invocation, managed-domain
isolation, full canonical ACK, incidental-finding lifecycle across every stage, or
all prerequisites across projects. Those producer integrations remain distinguishable
from this intake feature. No Issue should be closed merely because a schema exists.
