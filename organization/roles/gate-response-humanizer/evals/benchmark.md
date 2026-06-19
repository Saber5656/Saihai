# gate-response-humanizer eval benchmark

## TSK-1220 Validation

| Check | Result | Evidence |
|---|---|---|
| `evals/evals.json` is valid JSON | pass | `python3 -m json.tool skills/gate-response-humanizer/evals/evals.json` |
| Normal completion eval requires imouto style signal | pass | eval id 2 and 5 require `おにいちゃん` / soft endings |
| Safety eval preserves risk severity | pass | eval id 6 allows omitting the honorific only for severe risk |
| Long completion keeps visible style signal near closing | pass | eval id 7 requires `おにいちゃん` in the final two sentences / short closing line, not only at the opening, without line-wrap splitting the honorific |
| Markdown diff has no trailing whitespace | pass | `git diff --check -- skills/gate-response-humanizer` |

## Lightweight Benchmark

| Configuration | Pass Rate | Evidence |
|---|---:|---|
| old_skill | 50% | Old rule allowed `おにいちゃん` frequency `0〜2`, so normal completion could omit the imouto style signal without failing evals. |
| with_skill | 100% | TSK-1220 requires visible imouto style signals for normal completion, keeps the signal near the closing text for long responses, and preserves safety exceptions for severe risk / refusal cases. |

## TSK-1223 Validation

| Check | Result | Evidence |
|---|---|---|
| `evals/evals.json` is valid JSON | pass | `python3 -m json.tool skills/gate-response-humanizer/evals/evals.json` |
| Normal completion avoids hard polite prose | pass | eval id 2, 5, 8, 10 require rewriting `です` / `ます` / `しました` style prose |
| Honorific-only output fails | pass | eval id 8 and 10 reject hard business prose with only `おにいちゃん` appended |
| Progress updates use soft imouto tone | pass | eval id 9 requires `ここまで進んでるよ` / `進めるね` style wording |
| Safety exception keeps severity | pass | eval id 11 allows partial polite wording for severe security warnings while preserving clear risk |
| Technical terms remain intact | pass | eval id 12 rejects changing `push` / `release` / `commit` / `PR` into puns, typos, or vague paraphrases |
| Markdown diff has no trailing whitespace | pass | `git diff --check -- skills/gate-response-humanizer` |

## TSK-1223 Lightweight Benchmark

| Configuration | Pass Rate | Evidence |
|---|---:|---|
| old_skill | 64% | TSK-1220-era rules ensured a visible `おにいちゃん` signal, but hard `です` / `ます` prose could still pass when the honorific was present. |
| with_skill | 100% | TSK-1223 requires full soft-tone conversion for normal completion / progress / policy responses, rejects honorific-only output, preserves technical terms, and keeps safety exceptions for severe risk. |

Generated local artifacts:

- `skills/.workspace/gate-response-humanizer/evals/evals.json`
- `skills/.workspace/gate-response-humanizer/iteration-1/benchmark.json`
- `skills/.workspace/gate-response-humanizer/iteration-1/benchmark.md`
