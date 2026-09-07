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
records the resolution in history. A revised intake uses a new version and digest;
existing runs remain bound to their original request. A mechanical refresh is a
separate host operation: it preserves requirement rows, version, scope, non-goals,
operations, model provenance and dispositions while remapping only base coordinates.

## Canonical ledger and prerequisites

The existing `vault_task_records` writer stores a content-addressed sidecar beside
the canonical task under the task's exclusive record lock. It contains the full
ledger, every input requirement ID, its original row digest and its disposition.
The concise task marker is written last. Both the sidecar and marker are read back
before preparation returns and whenever an execution resolves the reference. A
private ACK flag or an orphan sidecar is insufficient. An interrupted exact prefix
can be completed on replay without another provider invocation; conflicting bytes,
symlinks and replaced directory entries fail closed. These are persistence receipts,
not Git commit/push receipts. The designated task writer still owns publication.

`ledger_lifecycle.require_prerequisites` checks every transitive dependency against
host-produced unit completion receipts and their canonical checkpoints. The unit's
requirements, constraints and exact scope contracts must match, with the same
observed task binding. Global selection/version changes do not invalidate an
untouched unit. A different hunk grant does. Completion is produced only after
publication checks the exact merge SHA's required checks, real host validation and
canonical completion persistence. A verified mechanical continuation satisfies its
original scope grant. Ledger self-assertions such as `completed: true` never count.
Legacy ITB envelopes without this host evidence remain blocked on dependencies.

## Incidental findings through the host lifecycle

The trusted-local flow journals intake, plan, implementation, validation, review,
publication, merge and completion observations through the same canonical writer.
`ledger_lifecycle.record_stage` is also the typed host ingestion point for findings
from those stages. Each finding retains its original content and digest; replay is
idempotent and repeated observations combine their stage provenance. Optional worker
`incidental_findings` are observations, never permission or repair instructions.

Out-of-scope observations remain deferred. In-scope followups, unknown requirement
IDs, material choices and critical risks remain explicitly pending and block the
affected publication. `resolve_findings` requires read-back host validation evidence;
critical risks also need their scoped review. It cannot defer blockers or resolve
requirement choices. `resolve_requirement_findings` instead checks an explicit,
lossless host ledger revision accounting for the original unresolved IDs/decisions.
Ordinary internal recovery does not introduce a user question. No finding is erased
by a later empty stage, and no automatic Issue creation or extra review is introduced.

## Bounded mechanical integration

`requirement_scope.mechanical_refresh` reads immutable old-base, fresh-base and task
Git objects before any checkout mutation. It checks original actual hunks against
the original contracts, maps only disjoint preimages, and returns exact resulting
bytes plus typed before/after contracts. The host uses that plan during normal Git
integration, validates the fresh-base diff, runs the existing host validation, and
commits a continuation linked to the original authorization digest. Publication
recomputes the bounded refresh chain; a renamed requirement, lost non-goal or widened
grant cannot be hidden behind a new brief digest.

Overlap, insertion-anchor ambiguity, upstream binary/mode changes, path collisions
and exhausted comparison/refresh budgets return `intake_scope_refresh_required` as
internal host work. They do not reuse stale line ranges or automatically request
new human approval. File comparisons have a shared finite work budget, blob/path
limits, and at most five linked mechanical refreshes.

## Evidence limits

Private `intakes/<request_id>/` retains source and actual provider process evidence.
Canonical checkpoints retain the full ledger and lifecycle dispositions. The exact
ID/content accounting is structural; it does not claim that an LLM understood every
semantic requirement or that edits inside a permitted range are semantically correct.

Offline tests use real temporary Git repositories, fixture subprocesses and temporary
Vaults. They distinguish actual local validation from simulated GitHub responses;
they do not attest a live approved-model invocation, real publication, managed-domain
isolation or cross-project deployment. The publication owner verifies those outcomes
separately and must not close an Issue on schema coverage alone.
