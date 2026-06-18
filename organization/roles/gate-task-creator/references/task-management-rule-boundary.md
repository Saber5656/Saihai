# gate-task-creator Task Management Rule Boundary

This file is a non-authoritative helper reference.

Source of truth:

| Rule | Source |
|---|---|
| Gate I/O and handoff flow | `Agents-Vault/03-Contexts/Policies/Gate-IO-Contract.md` |
| Task ID numbering, status values, Kanban sync | `Agents-Vault/03-Contexts/Policies/Dispatcher-IO-Contract.md` |
| Task Detail layout, naming, placement, granularity | `Agents-Vault/03-Contexts/Policies/Task-File-Conventions.md` |
| gate-task-creator execution steps | `skills/gate-task-creator/SKILL.md` |

Last synced: 2026-05-16

## Boundary

`gate-task-creator` consumes the normalized `Gate Intake Envelope` from `gate-prompt-formatter`, creates initial Vault task artifacts, and must hand off the result to `teams-project-manager`.

It does not own or redefine task numbering, Kanban synchronization, status semantics, or Task Detail file conventions.

## Required Handoff

Every task created by `gate-task-creator` must include a `Project Manager Handoff` section and must be handed to `teams-project-manager`.

`project-owner` is treated as a legacy alias in older notes. New handoff text should use `teams-project-manager` or `project-manager`.
