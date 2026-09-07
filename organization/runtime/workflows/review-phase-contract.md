# Review phases under usage-first development

Ordinary changes require relevant host validation and required integration CI; no reviewer is mandatory. For permission expansion, authentication/secrets or data-loss risk, select one scoped reviewer before dispatch. A role definition must not turn this into stacked internal and PR reviews.

| Phase | Required identity and evidence | Not required yet |
|---|---|---|
| Precommit scoped review | base commit, intended tree/diff digest, relevant host validation | PR number, future QA role verdict |
| Original finding resolution | original review/finding IDs, corrected tree and affected validation | fresh broad review |
| Explicit PR review | repository, PR, head/base and selected scope | second internal review |
| Non-Git audit | bounded content identities and scope | invented Git/PR identity |
| Merge preflight | actual current Git identity and required CI | an extra review caused by readiness recalculation |

The optional `requires_prior_steps` field declares named producers. A producer must dominate the consumer in the template graph: no path from the initial step may reach that consumer without the producer. Missing, later, bypassed and self-dependent producers are rejected by contract validation and work-order creation. Retry edges do not erase the completed initial producer.

`standard_code_change` and `publication_required` declare implementation before their explicitly selected review step. Their post-review QA remains a later verification stage. Its existence is not a prerequisite for precommit review, and test evidence is not mislabelled as a specialist QA verdict. These retained review workflows do not make reviews mandatory for the trusted-local ordinary path.

The role eval comparison measures deterministic instruction-contract assertions against the old and revised text. It is not a claim about live model behavior or review quality. Runtime tests independently reject unreachable/bypassed producers.
