---
type: policy
status: active
owner: infra-task-dispatcher
source_task: TSK-1001
last_updated: 2026-06-10
---

# Dispatcher I/O Contract

このノートは `infra-task-dispatcher` が Obsidian Vault を正本としてタスク受付、採番、進行管理を行うための再利用ポリシーだよ。
詳細な実施ログは [[01-Projects/AI-Agent-Organization/TSK-1001-dispatcher-io-contract]] を参照する。

## Source of Truth

Completion Gate の機械検証用チェーン、pre-final 必須 section、main-agent 禁止 role 集合は `organization/runtime/infra-team-bootstrap/config/completion-chain.yaml` を正本とする。このノート内の completion gate 記述は Vault 運用向け説明であり、ITB builder / Gate role は config を参照する。

| Artifact | Role |
|---|---|
| `Task-Gateway.md` | 人間からの自然文依頼の入口 |
| `Task Detail` | 各タスクの実施、判断、レビュー、Vault 更新の正本 |
| `Task-Index.md` | 全タスクの索引 |
| `Kanban.md` | 状態別作業ビュー |
| `AI-Organization.md` | 組織共通ルールの要約 |
| 各 `SKILL.md` Flow Contract | 担当ロールと次 hop の正本 |
| ITB `model-registry.md` | モデル、provider、実行モードの正本 |
| ITB role-agent registry | role metadata / queue consumer / inbox / report directory の正本 |
| ITB runtime queue | role 間 I/O の runtime evidence。Vault には要約と report path を記録する |

## Canonical Flow

1. 人間が `Task-Gateway.md` に依頼を書く。
2. `infra-task-dispatcher` が依頼を `triage` する。
3. 正式タスク化できる場合、`TSK-####` を採番して Task Detail を作る。
4. `Task-Index.md` に行を追加する。
5. `Kanban.md` の該当状態へリンクを置く。
6. 実行担当が queue report と Task Detail に実施ログを残す。
7. `実行`、queue evidence、レビュー証跡、`ドキュメント更新`、Completion Gate を通過してから `done` にする。

## Status Values

| Status | Meaning | Kanban Section |
|---|---|---|
| `inbox` | 未正規化依頼 | Inbox |
| `triage` | 分類、採番、情報補完中 | Inbox |
| `ready` | 着手可能 | Ready |
| `in_progress` | 実行中 | In Progress |
| `domain_review` | 既存互換のレビュー状態。新規タスクでは独立ステージとしては原則使わず、レビュー証跡は Completion Gate で確認する | Review |
| `independent_review` | 既存互換のレビュー状態。新規タスクでは独立ステージとしては原則使わず、別観点レビュー証跡は Completion Gate で確認する | Review |
| `waiting_human` | 人間承認、判断、権限付与待ち | Waiting Human |
| `blocked` | 解除条件付きで停止中 | Waiting Human |
| `done` | 成果物、レビュー、commit / commit 不要判断、Vault final update、`finalization-check` complete、`Final Transport Render Check` が完了 | Done |
| `archived` | 履歴化済み | Done |

## Minimum Task Detail Metadata

| Field | Required |
|---|---|
| `type: task-detail` | Yes |
| `task_id` | Yes |
| `main_team` | Yes |
| `assignee` | Yes |
| `status` | Yes |
| `source` | Yes |
| `last_updated` | Yes |
| `blocked_by` | When applicable |
| `requires_human_approval` | When applicable |

## Done Criteria

タスクを `done` にするには、次をすべて満たす。

| Gate | Required Evidence |
|---|---|
| Execution | 実施ログと成果物リンク |
| Queue evidence | role inbox/report、provider evidence、message status |
| Review evidence | 主担当領域と別観点のレビュー証跡。独立ステージではなく Director 作業、`team-completion-check`、GTE の確認対象 |
| Documentation update | Vault 更新先一覧 |
| Completion assessment | TPM `team-completion-check` command の ready_for_evaluation |
| Quality evaluation | `gate-task-evaluator` の quality_ok |
| Git publication | Git Publication Result または publication 不要判断 |
| Vault final update | 完了証跡、評価結果、残リスクの記録 |
| Finalization check | `finalization-check` complete と `Final Transport Render Check` |
| Human approval | 必要な場合のみ、承認内容と日付 |

## Review State Compatibility

`domain_review` と `independent_review` は既存タスク互換の status として残す。
新規タスクでは `domain_review` / `independent_review` を独立した通過ステージとして増やさない。原則として `in_progress` で担当作業、レビュー証跡、Vault 更新を揃え、`team-completion-check -> gate-task-evaluator -> git-publisher -> vault_final_update -> finalization-check -> final-transport-render-check` の Completion Gate で done 可否を決める。

## Operating Rules

- `Task Detail` の `status` を正とし、`Task-Index.md` と `Kanban.md` を同期する。
- role 間の依頼は queue message / report path を Task Detail または team `tasks.md` に記録する。
- queue runtime state は git 管理対象にせず、Vault には要約、report path、provider evidence、残リスクを残す。
- `done` へ進める前に `finalization-check` complete と `Final Transport Render Check` を必須とする。
- Git 管理対象の変更がある task は、Git Publication Result または publication 不要判断が記録されるまで `done` にしない。
- `Task-Index.md` から完了タスクを削除しない。
- `Kanban.md` に同じタスクを重複配置しない。
- `Task-Gateway.md` の依頼ブロックは採番後も削除せず、採番済み情報を追記する。
- 設計変更、要件追加、権限モデル変更、方針転換は `waiting_human` に置き、人間承認を得る。

## Related Notes

タスクノートの配置・命名・粒度判断は [[03-Contexts/Policies/Task-File-Conventions]] を正本とする。
Gate 間の I/O 契約は [[03-Contexts/Policies/Gate-IO-Contract]] を参照する。

## Review Status

| Review | Status | Note |
|---|---|---|
| Domain review | pending | `infra-task-dispatcher` 契約として確認が必要 |
| Independent review | pending | `infra-local-qa` または `tech-reviewer` による確認が必要 |
| Human approval | not required for draft | 既存ルールの整理であり、権限変更は含まない |
