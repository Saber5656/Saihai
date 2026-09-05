# Effective artifact observations (U1)

`scripts/effective_bundle.py` reads and compares artifact bytes. Its output is
informational and cannot authorize execution, adopt a policy, select a model,
activate a runtime generation, install files, or establish merge readiness.
This is the observation unit of #141; execution consumers and distribution
acceptance are separate, unfinished units.

## Actual CLI contract

```sh
python3 organization/runtime/workflows/scripts/effective_bundle.py observe --surface codex-app
python3 organization/runtime/workflows/scripts/effective_bundle.py observe --surface claude-cli
python3 organization/runtime/workflows/scripts/effective_bundle.py validate < observation.json
python3 organization/runtime/workflows/scripts/effective_bundle.py compare < comparison-input.json
```

`observe` loads the primary directory catalog through `directory_paths` using
an empty mapping, requires `loaded`, applies the returned mapping to its process,
and validates the Vault. It reads a fixed **audit slice**: the four canonical
Vault policies (AI-Organization, Gate-IO-Contract, Dispatcher-IO-Contract,
Task-File-Conventions), primary tech-architect and tech-reviewer definitions,
canonical COMMON and the selected surface's documented COMMON discovery path.
The only approved installation link is that exact discovery path to canonical
COMMON. A missing or symlinked surface directory does not authorize alternate
discovery. No install directory crawl, worker-supplied path, role dispatch,
stateful ITB import/dry-run, policy sync or configuration mutation occurs.

This fixed slice is not a host-produced run manifest. It intentionally reports
missing workflow, skill and runtime-configuration categories, unknown source
revision, null task/run IDs, `membership: asserted_unverified`, and
`trusted_selection: unknown`. Surface names only label the observation; reading
Codex App's COMMON file does not prove the App or any running process consumed it.
The historical count of differing installed skills is not remeasured by this CLI.

`compare` accepts one JSON object with exactly `expected` and `observed`, each a
valid observation. `validate` accepts one observation. Both read standard input,
bounded to 1 MiB, reject duplicate keys/nonfinite numbers/unknown fields and
versions, and never dereference input paths. Invalid input or unavailable CLI
environment exits 2 with a public-safe diagnostic. A successful read, validation
or comparison exits 0, including detected drift: inspect comparison reasons and
observation statuses. Exit 0 never means a run is authorized or a policy is active.

## Python collection contract

`observe_bundle(*, catalog_roots, member_spec, target_surface,
expected_source_identity, approved_symlinks=None)` accepts mappings supplied by a
trusted local caller for **observation only**. It does not authenticate that
caller, manifest, applicability, task, or runtime. It has no complete/verified
switch. The API is not an untrusted-path service; the production CLI deliberately
does not expose its path arguments.

```python
member_spec = {
    "members": [{
        "id": "COMMON", "category": "common",
        "source": {"root": "dotfiles", "path": "COMMON-AGENTS.md"},
        "installed": {"root": "surface", "path": "AGENTS.md"},
    }],
    "policy_snapshots": [],
}
```

Root keys and member IDs use bounded ASCII identifiers. Roots must be absolute,
canonical existing directories. Locations have exactly `root` and a relative
`path`, with no absolute path or dot/dot-dot traversal. Member IDs and role/policy
pairings must be unique. There are at most 128 members and 128 policy comparisons;
each regular file is bounded to 1 MiB. The required categories are common, role,
policy, workflow, skill, runtime_config. Including all six still cannot prove
completeness or applicability; the missing-category list is only diagnostic.

`approved_symlinks` maps an exact lexical file location to its exact allowed
canonical target. All resolved targets must remain inside supplied roots.
Approved directory symlinks and final-file symlinks are observed through every
path component. Resolved paths are opened with directory descriptors and
`O_NOFOLLOW`; nonregular files, oversized files and observed path/metadata
replacement during a read fail closed. Initial/final descriptor metadata and
path-component snapshots are compared. Root/target/path/link identities are
hashed in public output; callers retain any private mapping separately. Actual
file/config contents and OS error strings never appear in observations.

The observation is a sequence of bounded reads, not a filesystem transaction or
a lock on future execution. A future consumer must revalidate its bound artifacts
at the actual dispatch/resume boundary. Equality at collection time does not
prove continued freshness or process reload.

`expected_source_identity` is null when unknown, or exactly
`{"root": "repo", "commit": "<40 lowercase hex characters>"}`. The observer
reads Git HEAD/status before and after collection, with timeouts and optional
locks/fsmonitor disabled. It isolates Git selection/configuration environment
variables and checks that the actual Git top-level directory is the requested
root, so another checkout cannot be substituted through the process environment. Only an unchanged clean checkout at the expected HEAD
reports source `match`. A dirty or different checkout reports `source_changed`;
unavailable Git evidence reports `unknown`. This API does not yet encode a
canonical intended tree for uncommitted changes. Source identity is separate
from selected member content and cannot prove runtime deployment.

## Output and comparison

The closed schema is `schemas/effective-bundle-observation.schema.json`.
`validate_observation(value)` checks the schema, digest, category inventory and
cross-field relations. A structurally valid self-signed observation remains an
assertion; SHA256 is integrity comparison, not authentication.

| Layer | Observation |
|---|---|
| Member | Logical ID/category; private locator/target/link identity digests; mode, size, file identity and content SHA256/SHA1; typed read status |
| Source/install | `matching_copy`, `same_file`, `drift`, `unknown`, `not_observed` |
| Policy snapshot | `match`, `stale_policy_snapshot`, `missing_or_ambiguous_snapshot`, `unknown` |
| Source revision | Expected/observed commit, private repository identity digest, `match`/`source_changed`/`unknown` |
| Membership | Always `asserted_unverified`; trusted selection always `unknown` |
| Deployment | Always `not_observed` |
| Runtime generation | Always `unknown` |
| Target verification | Always `unverified` |

The legacy embedded snapshot format uses SHA1, so SHA1 is retained for that
comparison only. SHA256 covers member bytes and normalized observation content.
The observation time is excluded from `content_digest`; repeated reads of the
same identity/content do not create artificial drift. File timestamps detect
read races but are not included in stable content identity. Exact file/link
replacement may change identity even if bytes match.

Policy comparisons require exactly one ready row for the named canonical policy
in the selected role. A `ready` string is not freshness evidence. Current bytes
are independently hashed and compared to the embedded value. Unknown or ambiguous
rows are never manufactured into matches. `compare_bundle(*, expected, observed)`
returns equality/difference of the two assertions plus typed drift/unavailability
reasons. Equal assertions may themselves contain stale snapshots; neither
equality nor absence of reasons establishes complete/current runtime policy.

## Validation and remaining integration

Run `python3 organization/runtime/workflows/tests/test_effective_bundle.py` for
unit, temporary-file integration and subprocess JSON CLI tests. The hermetic CLI
test collects temporary fixtures through the Python API and passes observations
to the real compare command; it is not live runtime acceptance. The optional
`--legacy-red` diagnostic extracts only the two existing pure ITB policy helper
definitions via AST (without importing ITB), changes a temporary policy after
capturing its digest, and intentionally fails the proposed stale-status assertion.
It demonstrates why readability `ready` cannot substitute for freshness, not a
violation of the legacy helper's documented readability contract. A missing new
module is a separate API Red. The regular test proves the new byte comparison
detects the stale ready snapshot and also tests the unchanged positive case.

Repository full validation remains `python3 scripts/validate_all.py`: discovered
suites, both existing contract checks, and compilation. Counts and unreported or
conditional cases must be preserved accurately in private task evidence.

Later #141 units must bind a **real host selection** to the existing frozen work
order and dispatch/resume consumers, revalidate changed canonical decisions,
consume existing authenticated override/supersession producers, and reuse
deployment/assurance verification for approved rollout/rollback and actual target
surfaces. Missing producers remain blockers there. #128 owns required-check and
reviewer inventories, waivers and atomic merge; this observer adds none of them.
No fabricated generation, host manifest, signing key or active boolean closes
these dependencies.
