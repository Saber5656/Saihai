# Host-owned Vault task lifecycle

`task scaffold` registers a task before execution. Its typed brief has exactly
`objective`, `scope`, and `acceptance_criteria`, each a non-empty string. Example:

```sh
python3 scripts/saihai.py task scaffold --task-id TSK-20260907-example --project Example --brief '{"objective":"Implement the requested change","scope":"The approved feature","acceptance_criteria":"Focused and integrated validation pass"}'
```

The host loads `~/dev/Saihai/directory-path.env` with an empty environment through
`directory_paths.load_environment(require_catalog=True)`. There is no CLI Vault
root override. Scaffold creates `01-Projects/<project>/<task-id>/task.md` without
replacing an existing record. Text is rendered inert for Obsidian syntax.

A new frontdoor run must resolve one canonical task record before creation. The
run retains its task/path/content identity. Existing run replay is preserved.
Trusted-local execution resolves the same identity before a worker starts; an
absolute host `authority_evidence_ref` identifies the record (an optional fragment
is provenance only). Non-path provenance labels use canonical task discovery.
Neither worker stdout nor an arbitrary path grants permission to create a task.

`verify-completion` appends the verified terminal result before reporting complete.
`usage advance` appends only after the host publication path has merged the pinned
change and observed successful required checks for the merge commit. A Vault
failure returns a blocked state; a successful merge is not undone or concealed.
Retry the same command after repairing the record availability. Legacy execution
receipts remain readable and are resolved against the canonical task at completion.

The host locks and appends to the task record, preserving its existing content.
An exact task/run/result marker makes replay idempotent. The receipt reports the
path, block digest and content digest, with `committed=false` and `published=false`:
this feature does not commit or publish the Vault. A partial/conflicting receipt
is blocked for explicit recovery rather than silently overwritten.

Attachments are at most eight existing regular files, each at most 2 MiB, with
matching SHA-256, no symlink traversal and no hard links. Only escaped references
and digests are appended. Provider transcripts and arbitrary Markdown bodies are
not copied. Evidence remains private at its original host location; the receipt
does not claim cross-host portability or retention beyond that location.

Missing, ambiguous, mismatched, linked or unwritable task records block completion.
This feature does not grant the worker Vault access, expand credentials, release
software, archive tasks, or declare Git publication complete. Those operations
remain separate host responsibilities.

Validation uses real temporary Vault records. Do not run filesystem tests against
the shared Vault. Repository validation: `python3 scripts/validate_all.py`.
