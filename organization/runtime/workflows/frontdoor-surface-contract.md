# Frontdoor Surface Registration Contract

The frontdoor accepts requests from a registered frontend surface, not from a
hard-coded product branch. Codex is the first concrete enforced surface. The
same registry can describe Claude, Cursor, Grok, or another frontend without a
change to workflow selection, approval, run/report/completion gates, or Vault
gates.

## Contract Boundary

| Artifact | Responsibility | Authority |
|---|---|---|
| `profiles/frontdoor-surface-registry.json` | Binds a `frontend_kind` to its submit, launcher, requirements, and assurance descriptors | Static registration only |
| `schemas/frontdoor-surface-registry.schema.json` | Closes descriptor shapes and enums | Validation only |
| `scripts/frontdoor_surface_registry.py` | Validates registrations and derives a typed `surface_identity` | Informational; never grants an assurance claim |
| `profiles/agent-integration-assurance.registry.json` | Defines per-profile target claims and evidence requirements | Static target only |
| Host assurance generation | Proves current configuration, launch, tools, checkout, and required operations | Informational until the live gate revalidates it |
| `agent_integration_assurance.require_claim` | Reopens evidence and binds it to the exact live process | The only frontend assurance authority |

The bridge client submits through a host instance pinned to one registered
`frontend_kind`. An HTTP body may repeat that value, but a conflicting value is
rejected; the client cannot select a different surface or submit
`surface_identity`, `assurance_state`, or claims. The host derives and stores an
identity snapshot, binds request identity to a canonical digest of the complete
closed static descriptor, and re-evaluates only the effective assurance state
and claims when it returns each redacted projection. Static descriptor drift
fails closed on replay and projection.

```json
{
  "identity_version": "1",
  "frontend_kind": "codex",
  "descriptor_digest": "sha256:<digest-of-complete-static-descriptor>",
  "assurance_state": "advisory",
  "target_assurance_state": "action_enforced",
  "assurance_profile_id": "codex-main-agent-a-prime",
  "commissioned_claims": [],
  "suppressed_claims": ["action_enforced"],
  "submit_contract_version": "1"
}
```

This example is the expected fail-closed identity when the Codex target is
registered but no current active assurance generation verifies its claim.

## Per-Surface Enforcement Ladder

The ladder is a surface registration summary. Assurance claims remain
orthogonal: `action_enforced` does not imply `ingress_enforced`, and the shipped
Codex profile intentionally targets only `action_enforced`.

| Surface state | Registration and evidence required | Effective behavior |
|---|---|---|
| `advisory` | A valid surface descriptor conforming to submit contract v1. The assurance profile may have no target claims, or targeted claims may be uncommissioned/suppressed. | Typed requests may enter the same human-controlled harness. No enforced claim is available. Deterministic approval and execution gates remain unchanged. |
| `ingress_enforced` | The descriptor targets `ingress_enforced`; the assurance profile requires all three ingress operations to be `saihai_gateway_only`; current administrator-owned evidence verifies the claim. | The identity reports `ingress_enforced`. Any authority consumer must still revalidate the claim; the identity itself is not authority. |
| `action_enforced` | The descriptor targets `action_enforced`; the profile requires denial of every direct action operation and the gateway positive path; current administrator-owned evidence verifies the claim. | The identity reports `action_enforced`. Scoped-worker capability derive/execute still call the existing live-bound claim gate and also require independent `managed_worker`. |

Assurance is promoted per claim, not only as one all-or-nothing profile result.
Each independently passing targeted claim remains in `commissioned_claims`,
while each failing claim is listed in `suppressed_claims`; the highest passing
claim determines `assurance_state`. For example, when ingress evidence passes
but action evidence fails, `ingress_enforced` remains commissioned and reported
while `action_enforced` is suppressed. If no targeted claim passes, missing,
stale, malformed, drifted, candidate, unavailable, or otherwise uncommissioned
evidence produces `assurance_state = advisory`, an empty `commissioned_claims`,
and all target claims in `suppressed_claims`. It never falls forward to a
requested state. A surface whose descriptor requires a launch session also
suppresses all current claims to `advisory` when that request has no launch
session, even if an unrelated current assurance generation exists.

## Registering a New Frontend

Add data and the surface-specific implementation; do not add a product branch
to the harness core.

1. Add a `main_agent` profile to
   `profiles/agent-integration-assurance.registry.json`. Its `agent_family`
   must equal the new `frontend_kind`. Define truthful target claims,
   operation requirements, configuration artifacts, evidence policy, tool
   validator, and launch validator. An advisory profile has no target claims.
2. Provide a requirements bundle identified by `requirements_id`. For an
   enforced target, the assurance profile must bind its complete deployment
   and requirements artifacts; a name in the surface registry is not proof.
3. Provide a launcher descriptor. If `launch_session_required` is true,
   `verifier_module` and `verifier_class` must construct an object implementing
   `verify_parent_session(...)` and `revalidate(...)`. The assurance profile's
   launch validator must normalize its output to the common host bindings: `principal_id`,
   `workspace_id`, `checkout_realpath`, `checkout_identity_digest`, and
   `record_digest`.
4. Add one surface entry to `profiles/frontdoor-surface-registry.json`. Bind
   `main-agent-bridge-submit` version `1`, the launcher and requirements ids,
   the assurance profile id, and the profile's highest target state. The
   registry validator rejects unknown profiles, family mismatches, unsupported
   submit contracts, target mismatches, duplicates, and incomplete launchers.
5. Start a dedicated HTTP frontdoor with `--frontend-kind <frontend_kind>` or
   make the surface launcher invoke `main_agent_bridge_mcp.py --frontdoor
   <frontend_kind>`, with a host-pinned principal, workspace, checkout, and
   state root. The CLI choices are derived from the registry; no core product
   list is maintained, and request payloads cannot override the host pin.
6. Commission the assurance profile using its host-observed evidence flow.
   Registration alone remains advisory. Promotion appears only while current
   evidence passes, and action consumers independently revalidate live process
   identity.
7. Add positive and negative tests: registration and same-harness routing,
   unknown surface rejection, uncommissioned suppression, submit-field
   rejection, launcher binding failure, and any new assurance claim evidence.

The public test registration path is `SurfaceRegistry.register(descriptor)`.
It applies the same cross-contract validation as the shipped JSON registry, so
a fake or future surface can exercise ingress without editing orchestrator
control flow.

## Deterministic Core Independence

Only ingress resolves the registered surface and only explicit assurance gates
consume an assurance profile. Once the request record is created, the selector,
approval challenge, run creation, scheduler, report validation, completion
gate, and Vault gate consume their existing typed artifacts. They do not branch
on `frontend_kind` or `assurance_state`.

Unknown surfaces are rejected before request creation. A stored request is
bound to its frontend kind; read and ack calls using another surface are
rejected even if a caller presents the same correlation id.

## Shipped Registrations

| Frontend kind | Assurance target | Launcher status | Effective state without current evidence |
|---|---|---|---|
| `codex` | `action_enforced` only | Root-supervised launch session required | `advisory`, claim suppressed |
| `claude` | none | Uncommissioned descriptor | `advisory` |
| `cursor` | future `ingress_enforced` and `action_enforced` | Uncommissioned descriptor | `advisory`, claims suppressed |
| `grok` | future `ingress_enforced` and `action_enforced` | Unavailable descriptor | `advisory`, claims suppressed |
| `manual` | none | Operator-only compatibility registration | `advisory` |

The Codex launcher, requirements, assurance profile, target claim, live gate,
allowed operations, and approval boundaries are unchanged by this abstraction.
