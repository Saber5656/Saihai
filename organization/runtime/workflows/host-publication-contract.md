# Trusted-local host publication

`host_publication_adapter.publish(report, authorization, state_root)` connects a
completed trusted-local execution to commit, branch push, PR creation, required
CI observation, and a head-pinned GitHub merge. The trusted parent calls this API;
the usage CLI is host-facing and never converts a worker report into authority. Existing
Git/GitHub CLI authentication is used. No credentials are created or copied.

This usage-first route is distinct from the legacy merge-gate runtime. It does
not claim that runtime's unavailable all-gate atomic broker, sandbox isolation,
live provider acceptance, or release authorization. Native GitHub protection
remains effective; the adapter never uses an admin bypass or pushes main.

## Inputs

`HostAuthorization` is an independently constructed host object containing
`task_id`, `request_id`, `run_id`, `execution_id`, `repository`, absolute `worktree`,
`branch` (`codex/...`), pre-publication `head`, `base`, `allowed_paths`, nonempty
`required_checks`, `policy_digest`, and `authority_evidence_ref`. Destination is
`main`. Ordinary changes need no review field. `permission_expansion`,
`credentials`, and `data_loss` risk kinds require `scope_review_receipt` from the
parent's one scoped review. This reference is not a fabricated human signature.

The flat report contains the matching identity fields plus:

- `version: "1"`, `profile: "trusted_local_v1"`, `result: "completed"`;
- `approved_scope_digest: authorization.scope_digest`;
- `changed_paths`, `tree`, `diff_digest` from `snapshot(worktree, changed_paths)`;
- `publication_allowed: false`;
- `execution.actor_kind: "agent_under_explicit_user_task_authority"`, matching
  `authority_evidence_ref`, absolute `process_evidence_path`, and
  `process_evidence_digest`;
- `validation.status: "passed"`, absolute `evidence_path`, and `evidence_digest`.

Digests use `sha256:` plus 64 lowercase hex characters. The host process receipt
must contain matching `execution_id` and `exit: 0`; real argv, timing and process
identity evidence can also be retained. The host validation receipt must contain
`status: "passed"`, matching `execution_id`, `tree`, and `diff_digest`, with actual
validation commands/results. These files are host-produced evidence. A matching
file supplied by an untrusted worker is not authorization; the caller must own
the report, authorization and private state/evidence locations.

`snapshot` uses a temporary index, preserving the real index. Its digest is the
full-index binary cached patch against HEAD with no external diff or rename
folding. Existing staged changes, unrelated dirty files, unsupported symlink
changes and scope escape fail closed. The host must freeze the task worktree
against concurrent writers while validating and publishing it.

## Progress and recovery

Each call makes one CI observation, without an unbounded sleep loop. The host
scheduler calls again for `ci_pending`; `ci_failed` requires an authorized source
repair/validation and a new execution report. `conflict_pending` returns control
to the host's intent-preserving conflict repair flow; this adapter does not run
an untrusted resolver or silently retain a stale validation identity.

The required inventory combines the host versioned policy, active native branch
rules, and `gh pr checks --required` (including legacy protection). Enumeration
errors block. Required checks must succeed for the actual PR head; skipped or
failed checks do not count as success. Fresh head/base observations precede a
GitHub merge API request carrying the expected head SHA. GitHub enforces native
protection at mutation time. This is not an atomic assertion of every client-side
observation. The parent owns effective repository settings and serializes
publications per repository.

Mutation intent is durably saved before each operation. Re-entry reconciles an
uncertain commit/push/PR/merge using actual Git or GitHub identity and does not
blindly repeat an ambiguous mutation. An unconfirmed outcome remains
`*_uncertain` for the host to resolve. Successful merge returns its actual merge
commit. Integrated validation, canonical-main synchronization and release are
separate downstream responsibilities; a merge result alone does not complete
those tasks.

Tests use real disposable local Git repositories and synthetic GitHub responses.
They do not prove live GitHub operation or effective settings. Parent integration
validation and the authorized boundary review remain separate evidence.

## Host integration continuation

`publish(..., integrated_parent=prior_published_head)` accepts a host-created,
already committed merge only when both the prior published head and fresh base
are actual parents and `committed_snapshot(worktree, base)` matches the newly
validated report. The argument is supplied independently by the host, never by
worker output. It resumes the same feature branch and PR without force push.
The [usage coordinator](trusted-local-contract.md) owns conflict repair and
resumable required CI observation on the actual merge SHA; only its `complete`
state completes integrated validation.
