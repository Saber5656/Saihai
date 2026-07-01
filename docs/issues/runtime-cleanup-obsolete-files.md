# Audit and remove obsolete runtime/mirror files after deterministic orchestrator migration

## Context

The project is moving toward a zero-based deterministic orchestrator. Before the
codebase grows, remove or deprecate legacy runtime/mirror files that no longer
match the current P0 frontdoor/orchestrator direction.

Do not delete blindly: several files are still referenced by the current facade,
dashboard, tests, and policy docs.

## Current Cleanup Candidates

| Area | Current Finding | Candidate Action |
|---|---|---|
| `organization/runtime/infra-team-bootstrap/` | Large legacy bootstrap/hook/role-worker runtime; likely not aligned with the current deterministic frontdoor design | Deprecate/remove after references are migrated |
| `organization/runtime/infra-team-bootstrap/*` vs `organization/roles/infra-team-bootstrap/*` | Major files are hash-identical copies, including `SKILL.md`, registry, model/team config, builder, and tests | Keep one canonical copy only |
| `organization/runtime/model-registry.md` | Hash-identical mirror of `organization/runtime/infra-team-bootstrap/references/model-registry.md` | Consolidate canonical location |
| `organization/runtime/team-config.md` | Hash-identical mirror of `organization/runtime/infra-team-bootstrap/references/team-config.md` | Consolidate canonical location |
| `organization/runtime/role-agent-registry.yaml` | Hash-identical mirror of `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml` | Consolidate canonical location |
| `scripts/sync_organization_sources.py` | Migration-era mirror script from skills-repo/Vault into this repo | Remove once repo-native orchestrator replaces mirror workflow |
| `scripts/configure_organization.py` | Still used as facade for `workflow-frontdoor` and `workflow-frontdoor-server` | Do not remove until a new orchestrator-native entrypoint replaces it |
| `organization/runtime/infra-task-dispatcher/scripts/itd_monitor.py` | Still useful for task/Vault monitoring and likely independent of ITB | Keep unless task/Vault monitoring is replaced |
| `__pycache__` under `scripts/` and `organization/runtime/workflows/` | Generated files, untracked | Safe cleanup |

## Known References To Migrate First

- `server.py` currently reads `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml`.
- `scripts/configure_organization.py` still exposes ITB paths and delegates `itb ...` facade commands.
- `tests/test_configure_organization.py` asserts ITB runtime paths exist.
- Policy docs still describe ITB as canonical for gate/finalization behavior.
- `organization/README.md` still documents `scripts/sync_organization_sources.py` as the mirror generator.

## Acceptance Criteria

- [ ] Decide the new canonical location for role/model/team registry data.
- [ ] Replace frontdoor startup dependency on `scripts/configure_organization.py`, or explicitly rename/keep it as the orchestrator facade.
- [ ] Migrate `server.py` away from ITB runtime paths if ITB is removed.
- [ ] Update tests so they assert only the new runtime surface.
- [ ] Update policy/README docs to stop naming removed ITB paths as canonical.
- [ ] Remove duplicate ITB/mirror files in one scoped change.
- [ ] Keep `infra-task-dispatcher` unless its task/Vault monitoring role is intentionally replaced.
- [ ] Record final deletion evidence in Agents-Vault.

## Related Audit Note

Agents-Vault audit note:
`01-Projects/Agent-Teams-Viewer/tasks/2026-07-02-runtime-cleanup-audit.md`
