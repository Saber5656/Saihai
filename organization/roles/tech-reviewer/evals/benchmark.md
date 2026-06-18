# tech-reviewer Requirements Benchmark

## Scope

This benchmark validates `tech-reviewer` as the Engineering-team-wide reviewer, not as a replacement for specialist review agents.

## Expected Behaviors

| Check | Expected |
|---|---|
| Engineering scope | Reviews tech team tasks, implementation diffs, design diffs, validation evidence, and specialist review evidence |
| Specialist boundary | Detects specialist risks and delegates to the proper specialist agent |
| Cross-team boundary | Does not review Business / Contents / Infra / Gate artifacts as TR-owned work |
| Verdicts | Uses only `approve`, `request_changes`, or `blocked` |
| Ambiguous verdict | Never uses `approve_with_notes` |
| Completion handoff | Produces review evidence usable by `tech-director` and `gate-task-evaluator` |

## Static Validation Commands

```bash
python3 -m json.tool skills/tech-reviewer/evals/evals.json
rg -n "approve_with_notes" skills/tech-reviewer/SKILL.md skills/tech-reviewer/evals/evals.json
rg -n "Review Scope|Review Preparation|Specialist Handoff|approve / request_changes / blocked" skills/tech-reviewer/SKILL.md
git diff --check -- skills/tech-reviewer/SKILL.md skills/tech-reviewer/evals/evals.json skills/tech-reviewer/evals/benchmark.md
```

## Result

Pass.

| Command | Result |
|---|---|
| `python3 -m json.tool skills/tech-reviewer/evals/evals.json` | pass |
| static requirements assertion | pass: required sections, specialist handoff, 3 verdicts, and `approve_with_notes` prohibition found |
| `git diff --check -- skills/tech-reviewer/SKILL.md skills/tech-reviewer/evals/evals.json skills/tech-reviewer/evals/benchmark.md` | pass |

Residual risk: this is a static benchmark. Live reviewer behavior should be checked on the next real Engineering task.
