# v0.1.0 release readiness

Status at the 2026-09-07 preparation checkpoint: **unpublished; not release-ready**.
This checklist prepares issue [#53](https://github.com/Saber5656/Saihai/issues/53).
It authorizes no tag, release, credentials, privileged installation, or VM setup.
A merged preparation PR is not a released product.

## Evidence and outstanding acceptance

| Requirement | Existing evidence | Remaining check at the release commit |
|---|---|---|
| Milestone and #17 mapping | Last readback: only #53 open in v0.1.0; #17 closed with 12 checked items | Reconcile linked evidence, not only checkbox counts; refresh immediately before release |
| Offline contracts, fake happy/failure paths | Repository suites and required integrated CI exist | Match the final main SHA to successful required CI and full/contract results; reuse matching evidence |
| README clean-checkout smoke | Procedure below; not executed by this documentation task | Record exact checkout/catalog/task/state identity and transcript; arbitrary clone is not a supported managed checkout |
| Changelog | Historical entries retained; continuation PRs through #181 listed | Reconcile every included merged PR against the actual release range |
| Frontdoor security | #100 retains unresolved challenge custody; #101/#113 retain uncommissioned managed boundaries | Explicitly assess current P0/P1 release impact; do not silently waive findings or claim enforced operation |
| Ordinary live workflow | Issue #161 produced PR165, merge `788e622977933719c65a55f8282e832b88a42207`, with repair/integration and later replay evidence | New full intake-to-worker-to-publication acceptance remains pending; worker startup HTTP 400 is tracked in #182 |
| App and managed surfaces | Host adapter source/fixtures exist | Pending App fork has no confirmed actual ID; no new App success or managed commissioning claim |
| Tag/GitHub Release | No published Release at the recorded readback | Explicit release authorization, exact immutable commit, collision check, reviewed notes, then publication evidence |

Canonical historical evidence is in the registered Agents-Vault parent task:
`01-Projects/AI-Agent-Organization/TSK-PENDING-harness-implementation-20260905/`.
The relevant relative records are
`evidence/review-closeout-20260906/live-status-161/completed/publication.json`,
`integration.json` and `validation-repair.json` in that same completed directory,
and `evidence/completion-20260907/resume161-live-replay.json`.
Those records prove their recorded runtime identities, not a new run on the
release candidate. Keep private evidence private; publish only safe references.

## Minimum smoke procedure

1. Freeze the intended main SHA and required-check inventory. Use a clean
   registered linked checkout of the existing primary; do not repoint the
   directory catalog to a fresh clone. Record this qualification of the
   original clean-clone criterion in #53 instead of silently claiming parity.
2. Follow [delivery setup](../../organization/runtime/workflows/delivery-contract.md#local-validation-setup)
   for the pinned validation environment. Run the existing offline fake happy
   and failure tests (`test_e2e_happy_path.py` and `test_e2e_failure_modes.py` under
   `organization/runtime/workflows/tests/`) against temporary fixture state.
   Use matching full CI evidence for the same candidate; no redundant broad
   review or full repeat is required solely to prepare a release.
3. A host operator follows the [README offline quickstart](../../README.md#offline-quickstart)
   verbatim, registering the canonical smoke task first. This intentionally
   writes its Vault task/completion and must use the registered task owner.
   Record typed completion, evidence references and any deviation. Never use a
   production Vault as the target of filesystem failure tests.
4. Follow the [viewer startup instructions](../../README.md#local-status-viewer),
   choose the documented available local port, and record dashboard startup and
   the fake run's terminal display. Stop only the process started for this smoke.
5. Keep the ordinary live #182 recovery/new-task run separate from fake smoke.
   Record its actual worker process, validation, PR/head, merge CI, canonical
   sync and Vault receipt before calling that path complete. Preserve pending
   App and managed-profile status independently.
6. Before tagging, reconcile all #53 conditions and unresolved frontdoor risk,
   finalize notes and the exact commit, and obtain explicit release authority.
   Do not manufacture a security waiver from ordinary merge authorization.

Ordinary trusted-local work uses existing explicit host authority, required CI,
and host-owned PR publication. Permission expansion, authentication secrets,
and data-loss risk get one scoped review. Old blanket repeated-review wording
in historical records does not add new ordinary gates. Root-managed enforcement,
VM commissioning and release each retain their distinct acceptance boundaries.
