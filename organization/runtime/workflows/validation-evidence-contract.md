# Validation evidence

The bounded read-only consumer in `scripts/validation_evidence.py` reads digest-bound
artifacts, observes actual repository files and workflow cells, and compares native
delivery receipts with current source, profile and dependency identities. It never
executes commands or links supplied by a report. File readers reject traversal,
symlinks, nonregular files and oversized input.

`validation-evidence.schema.json` defines the closed request.
`inspect_request(root, repository_root, request)` is the file-backed entry;
`assess_validation_evidence(...)` is the diagnostic API.
`inspect_native_bundle(ExecutionBundle(...))` validates original #144/U0a bytes.
Legacy receipts without the sanitized-result digest remain incomplete; adjacent
files or a raw-log digest do not establish that binding.

The diagnostic records every static/unit/feature/E2E/build/security/full/device
layer and workflow/job/matrix/step cell. It keeps shell bodies distinct from actual
argv, local execution distinct from remote pending, and intended trees distinct
from commits. Missing, skipped, zero, malformed and changed evidence cannot pass.
No example profile constitutes an adopted waiver or a real project owner.

Behavioral changes retain original Red and Green events, test-file identities and
refactor disposition. Unverified chronology remains unverified. Physical-device
results must match the declared operation, build, image and timing; simulator
results do not establish physical-device behavior. Project-owner evidence must
match the exact repository/profile/owner/target/rollback declaration. Neither
payload equality nor a digest authenticates an issuer.

This legacy diagnostic reports `authorizes_execution=false` and pending producer
dependencies. It is not the trusted-local host authorization contract, publication
permission, merge gate or release approval. The usage-first host validates its
independently selected commands and retains actual process evidence; routine
review and individual-commit full reruns are not implied by this diagnostic.
