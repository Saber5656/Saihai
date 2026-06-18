---
name: git-publisher
description: >
  Git publication flowを順序制御するスキル。gate-task-evaluator が quality_ok 後に
  Git Publication Manifest を作成したとき、またはユーザーが「publishして」「commit/push/PRまで進めて」
  と依頼したときに使う。commit、push、将来のPR作成を一つのPublication Resultに束ねるが、
  commitやpushの具体判断はそれぞれの専用スキルに委譲する。
user-invocable: true
allowed-tools: Bash, Read, Grep
category: Dev
created: 2026-06-02
status: active
purpose: Git Publication Manifest に基づき commit / push / PR 作成を安全な順序で委譲し、結果を記録する
argument-hint: "[Git Publication Manifest or task context]"
---

# Git Publisher

`git-publisher` は Git 管理対象の task-owned diff を公開可能な状態へ進める調整役である。
Gate role ではなく、`commit`、`push`、PR 作成 capability を順番に呼ぶ Dev 系の道具スキルとして扱う。

## Gate Precondition

人間起点で直接呼ばれた場合でも、まず GPF/GTC の実行前チェックが揃っていることを確認する。
組織フロー内では、`gate-task-evaluator` の `evaluation_status: quality_ok` と `Git Publication Manifest` を入力にする。

次が揃うまで git 操作に進まない。

| Check | Required |
|---|---|
| `gate_intake_envelope_created` | Yes |
| `task_detail_created_or_updated` | Yes |
| `task_index_synced` | Yes |
| `kanban_synced` | Yes |
| `project_manager_handoff_created` | Yes |
| `review_line_defined` | Yes |
| `gate_task_evaluator_quality_ok` | Yes |
| `git_publication_manifest_present` | Yes |
| `branch_plan_present` | Yes |

未起票または manifest 不足の場合は、`GTC 未実施のため実行不可` または `publication_manifest_missing` として停止する。

## When I Activate

- ✅ `gate-task-evaluator` から `Git Publication Manifest` を受け取ったとき
- ✅ ユーザーが GTC 済み task について「publish」「commit/push/PRまで」と依頼したとき
- ✅ commit / push / PR の順序制御と結果記録が必要なとき
- ❌ 差分品質を評価したいだけのとき
- ❌ branch plan を決めたいとき
- ❌ push だけ、commit だけを単独実行したいとき

## Responsibility Boundaries

| In | Out |
|---|---|
| Publication Manifest の検証 | 成果物品質評価 |
| commit / push / PR 要否の順序制御 | branch plan 決定 |
| 専用スキルへの handoff | commit 差分分割の詳細判断 |
| Git Publication Result の記録 | push policy の正本定義 |

`git-publisher` は commit や push の責務を吸収しない。
具体実行は `commit` skill、`push` skill、将来の PR skill に委譲する。
ただし repo の git index / branch / remote 状態は組織横断の共有可変状態として扱う。`commit` / `push` / main 統合 / PR 作成へ進む前に、ITB builder の `shared-resource-lock` で `repo:<repo_root>` lease を取得し、Git Publication Result には `serializer_resource_id`、`serializer_lease_id`、`serializer_event_path` を記録する。publication が blocked / complete / deferred のどれで終わっても、取得した lease は lease_id 一致で release する。

## Git Publication Manifest

入力 manifest は次を持つ。

| Field | Required | 内容 |
|---|---:|---|
| `task_id` | Yes | 親 task |
| `repo_root` | Yes | 対象 repo |
| `branch_plan` | Yes | TPM が決めた Branch Plan |
| `task_change_manifest` | Yes when commit required | GTE が承認した task-owned diff |
| `commit_required` | Yes | commit 要否 |
| `push_required` | Yes | push 要否 |
| `pr_required` | Yes | PR 要否 |
| `publication_policy` | Yes | repo profile / branch policy |
| `publication_flow` | Yes | `commit_only` / `commit_and_push` / `merge_to_main_and_push` / `pull_request` / `not_required` など |
| `handoff_to` | Yes | `git-publisher` |

## Workflow

1. Manifest と Branch Plan を確認する。
   - 現在 branch が `branch_plan.working_branch` と一致しない場合は停止する。
   - Controlled Micro-Flow の publication-only task で `branch_action: none` / `publication_only_existing_diff` が記録されている場合は、現在 workspace の approved diff snapshot を Task Change Manifest と照合できる時だけ続行する。
   - repo / branch policy が欠ける場合は `publication_policy_missing` とする。
   - `${DEV_ROOT}/*` では `publication_flow: create_pr_from_task_branch` と `pr_required: true` を要求する。
   - `main-push-repos.md` listed repo では通常 `publication_flow: merge_to_main_and_push` とし、task branch を main に統合して default branch を push する。

2. commit を処理する。
   - `commit_required: true` なら `commit` skill へ渡す。
   - `commit_required: true` の Git Publication Manifest は、組織フロー内の commit 実行指示として扱う。ユーザーが別途「commit して」と言っていないことを理由に停止しない。
   - `commit_required: false` なら理由を `commit_status: not_required` として記録する。
   - `commit_required: false` の理由が `deferred_not_requested`、`not_requested`、`user_did_not_request_commit`、または同等の「人間が明示しなかった」理由なら manifest 不正として停止し、`gate-task-evaluator` へ差し戻す。
   - commit が blocked の場合、push / PR へ進まない。

3. push を処理する。
   - `push_required: true` なら `push` skill へ渡す。
   - `push_required: false` なら理由を `push_status: not_required` として記録する。
   - push が blocked の場合、PR へ進まない。

4. publication flow を処理する。
   - `publication_flow: merge_to_main_and_push` では、task branch の commit を base/default branch に統合し、default branch push を `push` skill へ渡す。conflict、non-fast-forward、dirty default branch では停止する。
   - `publication_flow: create_pr_from_task_branch` では、working branch push 後に PR を作成する。PR URL がない場合は `pr_status: blocked` または `deferred` とし、`finalization-check` が complete にしない前提で記録する。
   - PR を作成済みとして記録する前に `gh pr view <pr_url> --json url --jq .url` で実在確認し、返却 URL を `pr_verified_url` に記録する。`gh pr view` で確認できない PR は `pr_status: created` にしない。
   - `pr_required: true` で PR creation capability が未実装または利用不能の場合は `pr_status: deferred` とし、`finalization-check` が complete にしない前提で記録する。
   - `pr_required: false` なら `pr_status: not_required` とする。

5. `Git Publication Result` を report artifact に記録し、Task Detail には `task-detail-append` の thin section として記録する。
   - report artifact には `git_publication_result` object を置き、下記 Git Publication Result fields を保持する。
   - `task-detail-append` は `section: Git Publication Result`、`status: complete` または `blocked`、`report_path`、`report_sha256`、`owner_role: git-publisher` を指定する。
   - publication gate が complete で次ロールが `vault_final_update` の場合だけ `auto_vault_final_update: true` を指定し、builder に report artifact 検査と `vault-final-update` command 実行を任せる。
   - `role-report` / queue recovery が先に done report を検出した場合も、builder は `next_role` / `handoff_to: vault_final_update` と Task Detail の valid `Git Publication Result` を再検査してから `vault-final-update` command へ進める。
   - blocked / deferred / 差し戻しの場合は `auto_vault_final_update` を指定しない。

6. `vault_final_update` / `finalization-check` / `Completion Envelope` / `Final Transport Render Check` で Task Detail や同期ファイルに追加差分が出る場合、完了応答前に finalization を閉じる。
   - completion artifacts が git 管理下にある場合は finalization commit を作成し、`finalization_status: complete`、`finalization_commit_hashes` を記録する。
   - `push_required: true` の task では finalization commit も push し、`finalization_push_status: complete`、`finalization_remote_branch` を記録する。
   - completion artifacts を git 管理外 report へ分離した場合だけ `finalization_status: separated` とし、`finalization_separation_reason` を記録する。

## Stop Rules

| 状況 | 対応 |
|---|---|
| Manifest がない | `publication_manifest_missing` |
| Branch Plan がない | `branch_plan_missing` |
| 現在 branch が working branch と異なる | `branch_mismatch` |
| controlled micro publication-only で approved diff snapshot と現在 diff を照合できない | `approved_diff_snapshot_missing` |
| commit skill が blocked | push / PR を実行せず停止 |
| push skill が blocked | PR を実行せず停止 |
| PR required だが PR skill 未実装 | `pr_status: deferred` として finalization complete 不可 |
| `${DEV_ROOT}/*` で `pr_required: false` | `publication_policy_mismatch` |
| main-push repo で main 統合前に default branch push しようとした | `main_integration_missing` |
| `commit_required: true` なのにユーザー未依頼を理由に commit を延期しようとした | invalid; commit skill へ進む |
| `deferred_not_requested` / `not_requested` を最終 `Git Publication Result` にしようとした | invalid; blocked または差し戻しとして記録 |

## Git Publication Result

Task Detail には次を記録する。

| Field | Required | 内容 |
|---|---:|---|
| `commit_status` | Yes | `complete` / `not_required` / `blocked` |
| `commit_hashes` | When committed | 作成 commit |
| `push_status` | Yes | `complete` / `not_required` / `blocked` |
| `remote_branch` | When pushed | push 先 |
| `pr_status` | Yes | `created` / `not_required` / `blocked` / `deferred` |
| `pr_url` | When created | PR URL |
| `pr_verified` | When PR required | `gh pr view` で URL 実在確認済みなら `true` |
| `pr_verification_source` | When PR required | `gh_pr_view` |
| `pr_verified_url` | When PR required | `gh pr view <pr_url> --json url --jq .url` の返却 URL |
| `finalization_status` | When completion artifacts exist after publication | `complete` / `separated` / `not_required` |
| `finalization_commit_hashes` | When finalization committed | guardian / final update / done 証跡を閉じた commit |
| `finalization_push_status` | When push required and finalization committed | `complete` |
| `finalization_remote_branch` | When push required and finalization committed | finalization push 先 |
| `finalization_not_required_reason` or `finalization_separation_reason` | When finalization not committed | completion artifacts が git dirty として孤立しない理由 |
| `git_publication_status` | Yes | `complete` / `not_required` / `blocked` |
| `blocked_reason` | When blocked | 停止理由 |
| `next_role` | Yes | `vault_final_update` または差し戻し先 |

Task Detail の `Git Publication Result` section は full table ではなく thin section でよい。正本 fields は linked report artifact の `git_publication_result` object に置き、ITB builder の `git_publication_gate_errors` / `finalization-check` は `Report Path` と `Report SHA256` から report artifact を読み込んで検証する。

`deferred_not_requested`、`not_requested`、`publication_deferred_not_requested`、`commit_deferred_not_requested` は最終 status として禁止する。
task-owned Git diff がある task では、`commit_status: not_required` は GTE の `commit_required: false` と正当な不要理由がある場合だけ使える。
`pr_required: true` の完了判定では、`pr_url` の GitHub PR URL 形式に加えて `pr_verified: true`、`pr_verification_source: gh_pr_view`、`pr_verified_url` と `pr_url` の一致が必須になる。
`guardian` / `Completion Envelope` / `Final Transport Render Check` など publication 後の completion artifacts がある場合、`git_publication_status: complete` だけでは完了扱いにしない。`finalization_status` と対応する commit / push / separation reason が必要である。

## Sandboxing Compatibility

**Works without sandboxing:** Yes
**Works with sandboxing:** Depends on delegated git operations

- **Filesystem**: repository read/write through delegated git skills
- **Network**: only when delegated `push` / future PR skill requires it
- **Configuration**: Git remote and branch policy must be available

## Related Skills

- `commit`: task-owned approved diff の commit 実行
- `push`: push 可否判定と `git push`
- `git-workspace-prep`: Branch Plan に基づく作業 branch 準備

<!-- ITB_POLICY_DIGEST_SNAPSHOT_START -->
## ITB Policy Digest Snapshot

This block is generated by `infra-team-bootstrap sync-policy-digest-skills`.
Use the digest for routine freshness checks; read full policy bodies only when this digest changes, required judgment evidence is missing, or human approval is needed.
Narration policy: act on routine flow checks silently; surface only anomaly or approval blockers as `[FLOW-ALERT]`.

| Field | Value |
|---|---|
| policy_digest_status | `ready` |
| policy_digest_sha1 | `3208f43814e1e595e6baf885b6bc3e5641653fc4` |

| Policy | Status | SHA1 | Bytes | Source |
|---|---|---:|---:|---|
| AI-Organization | `ready` | `380b4a2cab2325b88f68993485ae997428265913` | 31825 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/AI-Organization.md` |
| Gate-IO-Contract | `ready` | `7af1c38f0b140feb45a11009ca94f70da542344d` | 34482 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/Gate-IO-Contract.md` |
| Dispatcher-IO-Contract | `ready` | `75cd888d160d7ae0a87640cefd1268ea84b4209e` | 6188 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/Dispatcher-IO-Contract.md` |
| Task-File-Conventions | `ready` | `ac5b009a443216dd7b00ebaa5541eaecfe341176` | 18748 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/Task-File-Conventions.md` |
<!-- ITB_POLICY_DIGEST_SNAPSHOT_END -->
