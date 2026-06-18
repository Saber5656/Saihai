# tech-tester Requirements Benchmark

## Scope

This benchmark validates `tech-tester` as the Engineering testing specialist for test design, test asset creation, execution evidence, failure reproduction, suite improvement, and skill eval / benchmark quality.

## Expected Behaviors

| Check | Expected |
|---|---|
| Test ownership | Designs, creates, executes, improves, and documents tests rather than only running commands |
| Skill eval coverage | Reviews `skill-creator` / `skill-updater` evals for trigger coverage, boundary coverage, assertion quality, and baseline comparison |
| System test coverage | Separates unit, integration, E2E, smoke, repro, regression, and acceptance-support tests |
| Failure reproduction | Records repro status, steps, expected / actual results, logs, and handoff |
| Suite quality | Detects flaky, slow, over-mocked, redundant, brittle, and under-covered tests |
| Specialist boundary | Does not replace `tech-qa` quality gates or `tech-reviewer` integrated review |
| Verdicts | Uses only `test_pass`, `test_needs_more_coverage`, `test_failed`, or `test_blocked` |
| Completion handoff | Produces evidence usable by `tech-qa`, `tech-reviewer`, and `gate-task-evaluator` |

## Static Validation Commands

```bash
python3 -m json.tool skills/tech-tester/evals/evals.json
rg -n "Test Scope|Test Strategy|Test Cases|Execution Results|Failure / Reproduction Notes|Coverage and Gaps|Tester Verdict" skills/tech-tester/SKILL.md
rg -n "test_pass|test_needs_more_coverage|test_failed|test_blocked|tech-qa|tech-reviewer|skill-creator|skill-updater" skills/tech-tester/SKILL.md
git diff --check -- skills/tech-tester/SKILL.md skills/tech-tester/evals/evals.json skills/tech-tester/evals/benchmark.md
```

## Result

Pass.

| Command | Result |
|---|---|
| `python3 -m json.tool skills/tech-tester/evals/evals.json` | pass |
| static requirements assertion | pass: required output sections, 4 verdicts, test asset authority, and QA / reviewer boundaries found |
| `git diff --check -- skills/tech-tester/SKILL.md skills/tech-tester/evals/evals.json skills/tech-tester/evals/benchmark.md` | pass |

Residual risk: this is a static benchmark. Live with-skill / baseline comparison should be run on the next real testing task if stronger evidence is needed.
