# Changelog

This file records shipped repository changes. The v0.1.0 section is prepared
for the human-owned tag and GitHub Release; its presence does not mean the
release has been published.

## [0.1.0]

### Contracts

- [#5](https://github.com/Saber5656/Saihai/pull/5) added the baseline typed orchestrator contracts, registry, schemas, templates, and deterministic selector.
- [#39](https://github.com/Saber5656/Saihai/pull/39) activated the standard code, research, publication, policy/permission, and security-sensitive workflow templates.
- [#40](https://github.com/Saber5656/Saihai/pull/40) bound orchestrator channels and bounded context references into the control contract.
- [#41](https://github.com/Saber5656/Saihai/pull/41) authenticated the main-agent bridge channel and bound acknowledgements to result digests.
- [#46](https://github.com/Saber5656/Saihai/pull/46) implemented the deterministic main-agent frontdoor proposal, approval, and redacted projection protocol.

### Runtime

- [#1](https://github.com/Saber5656/Saihai/pull/1) added the organization-runtime agent dispatch facade and public path aliases.
- [#2](https://github.com/Saber5656/Saihai/pull/2) migrated organization role mirrors into per-role skill directories.
- [#49](https://github.com/Saber5656/Saihai/pull/49) added the atomic durable workflow-run store and canonical artifact layout.
- [#57](https://github.com/Saber5656/Saihai/pull/57) added the repository-wide offline validation harness.
- [#58](https://github.com/Saber5656/Saihai/pull/58) enforced the global workflow-run advisory lock and stale-owner handling.
- [#61](https://github.com/Saber5656/Saihai/pull/61) added task-linked run and queue-shaped evidence views.
- [#62](https://github.com/Saber5656/Saihai/pull/62) added durable create, resume, abort, and terminal lifecycle handling.
- [#65](https://github.com/Saber5656/Saihai/pull/65) added typed report validation and transition evidence.
- [#66](https://github.com/Saber5656/Saihai/pull/66) added validated, signed, immutable workflow work orders.
- [#68](https://github.com/Saber5656/Saihai/pull/68) restored repository validation after the policy-gate merge.
- [#70](https://github.com/Saber5656/Saihai/pull/70) added completion verification and the thin Vault evidence result.
- [#71](https://github.com/Saber5656/Saihai/pull/71) added the validated issue-scoped child-thread action record.
- [#72](https://github.com/Saber5656/Saihai/pull/72) added the headless provider runner, fake adapters, attempt leases, and normalized evidence.
- [#76](https://github.com/Saber5656/Saihai/pull/76) aligned fake-provider evidence with the completion-verification contract.
- [#78](https://github.com/Saber5656/Saihai/pull/78) loaded host-managed local configuration from the primary checkout catalog.
- [#80](https://github.com/Saber5656/Saihai/pull/80) centralized and validated Saihai directory-path resolution.
- [#82](https://github.com/Saber5656/Saihai/pull/82) added host-verified scoped-worker capabilities and the bounded Codex CLI executor.
- [#85](https://github.com/Saber5656/Saihai/pull/85) added the fake-provider happy-path end-to-end suite.
- [#88](https://github.com/Saber5656/Saihai/pull/88) added confined, opt-in live Claude and Codex provider adapters.
- [#89](https://github.com/Saber5656/Saihai/pull/89) added the orchestrator failure-mode regression matrix.
- [#90](https://github.com/Saber5656/Saihai/pull/90) added focused acceptance tests for the standard, research, policy, and security templates.
- [#95](https://github.com/Saber5656/Saihai/pull/95) added the pinned Codex CLI deployment, exact process assurance, and deterministic read-only main-agent frontdoor.

### Safety

- [#3](https://github.com/Saber5656/Saihai/pull/3) removed tmux transport from the active viewer/runtime path while retaining explicit compatibility modeling.
- [#55](https://github.com/Saber5656/Saihai/pull/55) added deterministic tool allowlists for main-agent frontend sessions.
- [#60](https://github.com/Saber5656/Saihai/pull/60) added the minimal offline GitHub Actions validation workflow with pinned action revisions.
- [#64](https://github.com/Saber5656/Saihai/pull/64) enforced explicit activation sources, principals, and approval challenges.
- [#75](https://github.com/Saber5656/Saihai/pull/75) fixed Codex frontend exec-policy state-root enforcement.
- [#77](https://github.com/Saber5656/Saihai/pull/77) added fail-closed guards for sensitive file access.
- [#79](https://github.com/Saber5656/Saihai/pull/79) consolidated web-security review duties into the `tech-security` role.
- [#83](https://github.com/Saber5656/Saihai/pull/83) modeled trusted frontdoor state-root flows for CodeQL.
- [#84](https://github.com/Saber5656/Saihai/pull/84) moved the CodeQL model extension to its discoverable package layout.
- [#86](https://github.com/Saber5656/Saihai/pull/86) hardened workflow-run API reads against path, symlink, size, and corrupt-state failures.

### Viewer

- [#63](https://github.com/Saber5656/Saihai/pull/63) added read-only workflow-run list, detail, evidence, transition, and lock APIs.
- [#69](https://github.com/Saber5656/Saihai/pull/69) added workflow-run and stuck-state panels to the local viewer.
- [#87](https://github.com/Saber5656/Saihai/pull/87) added generated run artifacts and viewer/API fixture coverage.

### CLI

- [#48](https://github.com/Saber5656/Saihai/pull/48) separated the narrow `saihai` frontdoor commands from approved workflow execution commands.

### Docs

- [#47](https://github.com/Saber5656/Saihai/pull/47) added the day-1 operator, legacy migration, stuck-run recovery, and rollback runbooks.
- [#56](https://github.com/Saber5656/Saihai/pull/56) rebranded the repository surface and retained compatibility aliases for historical names.
- [#59](https://github.com/Saber5656/Saihai/pull/59) documented the current repository capabilities and boundaries in the root README.
- [#91](https://github.com/Saber5656/Saihai/pull/91) synchronized the root README with shipped behavior and added Japanese documentation parity.
- [#92](https://github.com/Saber5656/Saihai/pull/92) synchronized the workflow README and operator runbook with the implemented runtime.
- [#93](https://github.com/Saber5656/Saihai/pull/93) completed the operator runbook's orchestrator issue map.
- [#94](https://github.com/Saber5656/Saihai/pull/94) added the v0.1.0 changelog and synchronized English and Japanese release guidance.

### v0.1.0 authority boundary

- The Saihai bridge API lets a frontend/main agent submit a typed request, read a redacted projection, and acknowledge output; that API cannot classify, approve, create runs, choose raw commands or paths, or publish changes. This bridge boundary does not remove ambient authority from an independently launched agent.
- The projection exposes an idempotency digest but never its raw key. Child-thread and worker summaries are visible only under an exact request, task, owner-principal, and checkout binding.
- The only shipped enforcement target is the release-pinned stock Codex CLI started by the root-owned zero-argument launcher. Codex App, IDE, direct Codex launches, and universal prompt ingress are not claimed.
- Every authority check is rebound to the live launcher process and current deployment epoch. Activation, rollback, and uninstall revoke the previous epoch before target mutation; restored deployments require fresh commissioning and sealing.
- Frontend `credential_access = denied` covers only the known Codex auth paths, dedicated `CODEX_HOME`, and the mechanically verified absence of credential-capable tool classes from the fixed inventory. It is not a claim that all user-readable secret-bearing files are inaccessible.
- A commissioned frontend positive path creates exactly one typed request in `waiting_human` and no capability, worker execution, run, provider dispatch, report, or other downstream side effect.
- A host-owned executor can derive a capability from an approved work order and launch a pinned, bounded Codex CLI worker in the planned task worktree only when independent frontend and worker assurance generations verify.
- The frontend and worker require separately governed policy domains. v0.1.0 does not ship automatic cross-domain transport between the frontend gateway and the worker domain.
- Same-rootfs Codex 0.144.1 worker evidence is not promotable because generic external mutation, absolute local `git push`, and credential denial cannot be proved there; `external_mutation`, `git_commit`, `git_push`, and `credential_access` remain failed/inconclusive, and worker `commission-seal` stays fail-closed until isolated-domain evidence exists.
- The shipped scoped-worker executor rejects every network and provider grant. Live provider adapters are a separate host-owned, opt-in, readonly path.
- Commit, push, and pull-request publication remain behind separate review, approval, and publication gates.
- The supported checkout is the host-managed primary checkout at `~/dev/Saihai` or one of its linked worktrees. An arbitrary fresh clone does not satisfy the checkout identity contract.
- Daemon scheduling, tmux worker execution, package distribution, automatic publication, credential provisioning, and release publication are not part of the v0.1.0 runtime.
