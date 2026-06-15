---
type: policy
status: active
owner: gate-task-creator
source_task: TSK-1053
last_updated: 2026-06-14
---

# Gate I/O Contract

このノートは `gate-prompt-formatter`、`gate-task-creator`、`teams-project-manager`、`gate-task-evaluator`、`finalization-check`、main transport renderer が共通して使う入出力契約を定義する。

`infra-task-dispatcher` の Vault 状態同期、採番、Kanban 連携は [[03-Contexts/Policies/Dispatcher-IO-Contract]] を正とする。  
この契約は、その前段と後段で Gate がどの情報を渡すかを定義する。
Completion Gate の機械検証用チェーン、pre-final 必須 section、main-agent 禁止 role 集合は `/Users/takagiyasushi/skills-repo/skills/infra-team-bootstrap/config/completion-chain.yaml` を正本とする。このノート内の flow 表は人間向け説明であり、ITB builder / Gate role は config を参照する。

## Scope

| 項目 | 内容 |
|---|---|
| In | 人間起点の依頼整形、タスク化可能単位への分解、承認要否の一次判定、起票前後の handoff |
| Out | Task ID 採番規則の詳細、Kanban 同期の実装、各専門チームの作業手順 |
| Source of Truth | Gate 間の受け渡しはこのノート、Vault 状態管理は [[03-Contexts/Policies/Dispatcher-IO-Contract]] |

## Canonical Flow

| Step | Actor | Input | Output |
|---|---|---|---|
| 1 | `gate-prompt-formatter` | `queue/inbox/gate-prompt-formatter.yaml` の人間起点 message | `Gate Intake Envelope` report |
| 2 | `gate-task-creator` | `Gate Intake Envelope` report または GTC inbox message | Task Detail、Task Index 行、Kanban 初期 entry、Project Manager Handoff |
| 3 | 実行前チェック | GPF/GTC 成果物 | 全必須チェック true、または `GTC 未実施のため実行不可` |
| 4 | `teams-project-manager` | Task Detail、routing fields、Project Manager Handoff、Resident Team Roster | 主担当チーム、支援チーム、レビュー証跡要件、実行順序、Branch Plan、Active Set |
| 5 | 主担当チーム / Director | Task Detail、Team Routing Decision、Active Set | team task、実施ログ、成果物、レビュー結果、Completion Report、Invocation Evidence |
| 6 | `team-completion-check` | Completion Report、Task Detail、各 `<team>/tasks.md`、Director structured completion signal | Completion Assessment command artifact |
| 7 | `gate-task-evaluator` | Completion Assessment command artifact、成果物、review evidence / validation 結果 | Quality Evaluation、Task Change Manifest、Git Publication Manifest、publication 要否 |
| 8 | `git-publisher` | Quality Evaluation、Git Publication Manifest、Task Change Manifest、Branch Plan | Git Publication Result |
| 9 | Vault final update | Git Publication Result、評価結果、残リスク | 完了証跡付き Task Detail |
| 10 | `finalization-check` | Task Detail、Index、Kanban、関連 task、Git Publication Result、Vault final update | Finalization Check、Completion Envelope |
| 11 | `final-transport-render-check` / main transport renderer | finalization complete 済み Completion Envelope | `Final Transport Render Check` + 人間向け最終応答 |

## Queue-First Gate Delivery

TSK-1244 以降、Gate 間 I/O は「ロールを読んだ」「その Skill の内容を参照した」だけでは完了扱いにしない。次ロールに渡す依頼は、ITB runtime queue の inbox message として記録し、担当 role-agent worker が report を返す。

| Queue Artifact | Path Pattern | Owner | Meaning |
|---|---|---|---|
| Role inbox | `<ITB_STATE_ROOT>/<session>/queue/inbox/<role_id>.yaml` | target role | pending / processing / done / failed message list |
| Task payload | `<ITB_STATE_ROOT>/<session>/queue/tasks/<task_id>/<message_id>.yaml` | sender role | instruction、expected output、handoff notes |
| Role report | `<ITB_STATE_ROOT>/<session>/queue/reports/<role_id>/<task_id>/<report_id>.yaml` | target role | result、status、provider evidence、instruction preview |
| Queue event log | `<ITB_STATE_ROOT>/<session>/queue-events.jsonl` | ITB | claim / done / failed の runtime event |

### Required Queue Message Fields

| Field | Required | Meaning |
|---|---|---|
| `message_id` | Yes | role inbox 内で一意な message ID |
| `task_id` | Yes | 親 Task ID。起票前は provisional ID を使い、GTC 後に正式 Task ID を記録する |
| `from_role` | Yes | 送信元 role |
| `to_role` | Yes | 宛先 role |
| `status` | Yes | `pending` / `processing` / `done` / `failed` |
| `payload.instruction_ref` | Yes | queue root からの相対 task payload path |
| `payload.report_path` | Yes | queue root からの相対 report path |
| `payload.expected_output` | Yes | 原則 `role_report` |

### Required Report Evidence

| Field | Required | Meaning |
|---|---|---|
| `from_role` | Yes | report を作成した target role |
| `task_id` | Yes | 対象 Task ID |
| `message_id` | Yes | 消費した queue message |
| `status` | Yes | `done` / `failed` |
| `result` | Yes | 完了、失敗、blocker など |
| `evidence.provider` | Yes | `anthropic` / `openai` |
| `evidence.intended_model` | Yes | model-registry の primary model |
| `evidence.effective_model` | Provider実行時 | transcript / session log で確認した実モデル |
| `evidence.provider_session_id` | Provider実行時 | Claude tmux session、Codex session id など |
| `evidence.request_id` | Provider実行時 | request / turn を識別できる ID |
| `evidence.usage_source` | Yes | `claude_tmux_interactive`、`claude_print_json`、`codex_exec` など |
| `evidence.transcript_path` | Provider実行時 | transcript / output file / session log への path |

`role_agent_worker_local_stub` は旧 local queue 検証用の使用源であり、委譲成立、role 実行済み、完了 evidence として扱わない。
新規 queue report では禁止する。

## Execution Preflight

人間起点の依頼では、次の全項目が true になるまで個別スキル、調査、編集、git 操作、レビュー、コミット、スキル更新を開始しない。

| Check | Required | Evidence |
|---|---|---|
| `organization_instance_bootstrapped` | Yes | ITB により Organization Instance が ready または already_ready である |
| `gate_intake_envelope_created` | Yes | `Gate Intake Envelope` が元依頼、意図、成果物、承認要否、task units を含む |
| `task_detail_created_or_updated` | Yes | Task Detail が作成または既存 Task Detail が更新されている |
| `task_index_synced` | Yes | Task Index に Task Detail への wikilink がある |
| `kanban_synced` | Yes | Kanban の status 対応セクションに同一 Task が 1 回だけある |
| `project_manager_handoff_created` | Yes | Task Detail に `Project Manager Handoff` がある |
| `review_line_defined` | Yes | レビュー証跡要件、レビュー担当、人間承認要否が記録されている |
| `team_roster_recorded` | Yes | Resident Team Roster / role-agent registry evidence が記録されている |
| `active_set_declared` | Yes | 実作業 role、review role、Gate role の Active Set が記録されている |
| `queue_evidence_recorded` | Yes | queue inbox / payload / report path または provider transcript evidence が記録されている |
| `team_roster_recorded` | Yes | Resident Team Roster が Task Detail または Project note に記録されている |
| `active_set_declared` | Yes | Gate core / Infra core の運行責務、lazy role、タスク別 active set が記録されている |

いずれかが false の場合、実作業は禁止する。Plan Mode、権限不足、環境制約で GTC を実行できない場合は、起票計画または不足条件だけを返す。

## Gate Intake Envelope

`gate-prompt-formatter` は、原則として次の構造を `gate-task-creator` に渡す。

| Field | Required | Meaning |
|---|---|---|
| `source_type` | Yes | `human_prompt`、`task_gateway`、`dispatcher_candidate` のいずれか |
| `received_at` | Yes | 受領日時 |
| `original_request` | Yes | 元の依頼文。意味を変えずに保存する |
| `normalized_request` | Yes | タスク化しやすい形に正規化した依頼 |
| `intent_summary` | Yes | ユーザーが達成したい状態の短い要約 |
| `desired_outcome` | Yes | 完了時に何ができていればよいか |
| `constraints` | No | 明示された制約、禁止事項、環境条件 |
| `assumptions` | No | 進行のために置いた仮定 |
| `missing_information` | No | 不足情報。作業不能またはリスクが高い場合のみ質問に戻す |
| `approval_required` | Yes | 人間承認が必要かどうか |
| `approval_reason` | When applicable | 承認が必要な理由 |
| `task_units` | Yes | 起票可能な作業単位の一覧 |
| `routing_hint` | Yes | 想定される主担当、支援、レビュー担当 |
| `review_requirements` | Yes | `team-completion-check` / GTE が確認するレビュー証跡要件と人間承認の要否 |
| `vault_update_targets` | Yes | 記録先候補。Task Detail、Project note、Policy note など |
| `risks` | No | 既知のリスク、未確定事項、衝突しそうな既存ルール |
| `handoff_notes` | No | 次ロールへの注意事項 |

## Task Unit Schema

`task_units` は、1 つ以上の作業単位として記録する。

| Field | Required | Meaning |
|---|---|---|
| `unit_id` | Yes | Envelope 内の一時 ID。例: `unit-1` |
| `title` | Yes | Task Index に載せられる粒度のタイトル |
| `main_team` | Yes | `gate`、`tech`、`contents`、`business`、`infra` のいずれか |
| `assignee` | Yes | 想定担当エージェント |
| `priority` | Yes | `P0`、`P1`、`P2` |
| `scope_in` | Yes | この作業で扱う範囲 |
| `scope_out` | Yes | この作業で扱わない範囲 |
| `deliverables` | Yes | 成果物 |
| `done_criteria` | Yes | 完了条件 |
| `dependencies` | No | 先行タスク、承認、外部情報 |
| `requires_human_approval` | Yes | 人間承認が必要か |

## Approval Rules

次に該当する場合、`approval_required: true` として `waiting_human` に置く。

| Trigger | Example |
|---|---|
| 設計変更 | 組織フロー、権限、責務境界の変更 |
| 要件追加 | 既存タスク範囲を超える新機能や新運用 |
| 権限モデル変更 | ファイル削除、外部サービス連携、認証情報、公開範囲 |
| 方針転換 | 既存の正本ポリシーと矛盾する変更 |
| 破壊的操作 | 削除、上書き、履歴消去、復旧困難な移動 |
| 費用や時間の増加 | 長時間実行、外部 API 利用、追加ツール導入 |

次の場合は、質問に戻さず合理的な仮定を置いて進めてよい。

| Case | Handling |
|---|---|
| 表記ゆれ | 既存 Vault の命名規則に寄せる |
| 担当が明らか | ルーティング原則に従って担当を仮置きする |
| 成果物形式が明らか | 既存テンプレートに合わせる |
| リスクが低い追記 | 既存ノートへの参照追加や補足追記として扱う |

## Task Splitting Rules

| Situation | Split? | Reason |
|---|---|---|
| 成果物が別種類 | Yes | Policy、Skill、Code、Report はレビュー線が異なる |
| 承認待ちと実行可能作業が混在 | Yes | 実行可能部分を止めないため |
| 複数チームにまたがる | Usually | 主担当とレビュー線を明確にするため |
| 単一ノートの軽微な追記 | No | 起票とレビューの負担を増やさないため |
| 調査と実装が連続する | Usually | 調査結果で実装方針が変わるため |

## Review Requirements

`review_requirements` は独立した `domain_review` / `independent_review` ステージを要求するものではない。
Director 作業内の相互レビュー、別観点レビュー、人間承認要否を記録し、TPM の `team-completion-check` command と `gate-task-evaluator` が確認するレビュー証跡要件として扱う。
レビュー証跡がない team task は完了扱いにしない。

## Routing Hint

`routing_hint` は確定判断ではなく、`teams-project-manager` が最終決定するための材料として扱う。

| Field | Meaning |
|---|---|
| `recommended_main_team` | 想定主担当チーム |
| `recommended_assignee` | 想定担当エージェント |
| `supporting_agents` | 支援候補 |
| `domain_review_agent` | 既存互換のレビュー候補。新規ではレビュー証跡担当として読む |
| `independent_review_agent` | 既存互換の別観点レビュー候補。新規では独立ステージではなくレビュー証跡担当として読む |
| `human_approval` | 人間承認の要否と理由 |


## Organization Instance Contract

チャットセッションを 1 Organization Instance として扱う。GPF は Organization Instance ごとの agent instance であり、全チャット横断 singleton にしない。

| Field | Required | Meaning |
|---|---|---|
| `role_id` | Yes | 論理ロール ID |
| `agent_instance_id` | Yes | チャットセッションごとの実 agent instance ID |
| `organization_instance_id` | Yes | チャット単位の Organization Instance ID |
| `roster_scope` | Yes | 既定は `session` |
| `chat_session_id` | Yes | 紐づくチャット/作業セッション ID |
| `project_id` | When available | 紐づく Project ID または Vault project slug |
| `lifecycle_status` | Yes | `bootstrapping` / `ready` / `active` / `shutting_down` / `archived` / `failed` |
| `queue_root` | Yes | `<ITB_STATE_ROOT>/<session>/queue` |
| `inbox_path` | Yes | 対象 role の inbox path |
| `report_dir` | Yes | 対象 role の report directory |
| `last_seen_at` | When available | 最後に起動または稼働証跡を確認した時刻 |

`SessionStart` / `SessionResume` / `SessionArchive` は `infra-team-bootstrap` が扱う。`PromptSubmit` は必ず `gate-prompt-formatter` に渡す。

## Resident Team Roster Contract

Resident Roster は、起動済みロールと現タスクで実際に動く active set を分離して記録するための契約である。`idle` は未起動ではなく、resident だが現タスクでは作業しない状態を指す。

| Field | Required | Meaning |
|---|---|---|
| `role_id` | Yes | 論理ロール ID |
| `agent_instance_id` | Yes | チャットセッションごとの実 agent instance ID |
| `organization_instance_id` | Yes | チャット単位の Organization Instance ID |
| `roster_scope` | Yes | `session` |
| `chat_session_id` | Yes | 紐づくチャット/作業セッション ID |
| `project_id` | When available | 紐づく Project ID または Vault project slug |
| `lifecycle_status` | Yes | `bootstrapping` / `ready` / `active` / `shutting_down` / `archived` / `failed` |
| `agent_id` | Yes | 正式ロール ID |
| `team` | Yes | `gate` / `tech` / `contents` / `business` / `infra` |
| `resident_status` | Yes | `resident` / `unavailable` |
| `activation_status` | Yes | `metadata_ready` / `idle` / `response_active` / `resetting` |
| `metadata_status` | Yes | `metadata_ready` など、roster metadata の作成状態 |
| `process_status` | Yes | `not_launched` / `process_ready` / `launch_failed` |
| `provider_status` | Yes | `not_started` / `deferred` / `provider_process_ready` / `provider_response_ready` |
| `tool_sidecar_status` | Yes | `not_started` / `deferred` / `ready` / `not_verified` |
| `response_status` | Yes | `not_invoked` / `invoked` |
| `always_active` | Yes | Gate core / Infra core の運行責務。provider response ready を意味しない |
| `provider` | Yes | `anthropic` / `openai` |
| `intended_model` | Yes | `infra-team-bootstrap/references/model-registry.md` の `primary_model` |
| `effective_model` | When available | transcript / session log で確認した実モデル |
| `execution_mode` | Yes | `agent` / `codex` / `chat` / `long-run` |
| `queue_consumer` | Yes | role-agent-worker が inbox を consume する常駐対象か |
| `inbox_path` | Yes | queue root からの相対 inbox path |
| `report_dir` | Yes | queue root からの相対 report directory |
| `session_id` | When available | Claude transcript sessionId、Codex session id など |
| `last_request_id` | When available | 最後に確認した requestId |
| `usage_source` | Yes | Claude transcript JSONL、Codex session log など |
| `active_for_task` | When active | 現在担当中の Task ID |
| `last_reset_at` | When reset | タスク切替時のリセット時刻 |
| `last_seen_at` | When available | 最後に起動または稼働証跡を確認した時刻 |
| `notes` | No | fallback、alias 解決、障害メモ |

Always active は運行上の core role を指し、process readiness や provider response readiness を意味しない。lazy activation role は registry / model-registry に従い、必要時に active 化する。

| Team | Core Always Active Roles |
|---|---|
| Gate | `gate-prompt-formatter`, `gate-task-creator`, `teams-project-manager`, `gate-task-evaluator` |
| Infra | `infra-team-bootstrap`, `infra-director` |

`gate-task-assessor` と `gate-task-guardian` は旧フロー互換の参照 role として保持するが、新規 task の resident / queue consumer / provider turn としては使わない。assessor 相当は `team-completion-check` command、guardian 相当は `finalization-check` / `final-transport-render-check` command が担う。

`infra-task-dispatcher` と `infra-local-qa` は Infrastructure の on-call / lazy role として扱い、自動巡回、状態同期、Vault QA が必要な時だけ active 化する。Tech / Contents / Business も resident candidate だが、TPM または各 director がタスクごとに active 化する。bridge、commit、git-publisher、push、git-workspace-prep、save、Obsidian CLI などの道具スキルは resident agent にしない。

Task Detail には次のセクションを置く。

```markdown
## Resident Team Roster

| role_id | agent_instance_id | organization_instance_id | roster_scope | chat_session_id | project_id | lifecycle_status | agent_id | team | resident_status | activation_status | metadata_status | process_status | provider_status | tool_sidecar_status | response_status | always_active | provider | intended_model | effective_model | execution_mode | queue_consumer | inbox_path | report_dir | session_id | last_request_id | usage_source | active_for_task | last_reset_at | last_seen_at | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## Active Set

| Task Phase | Core Active | Task Active | Lazy / Idle Resident | Reason |
|---|---|---|---|---|

## Invocation Evidence

| Agent | Provider | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Transcript Path | Result |
|---|---|---|---|---|---|---|---|---|

## Queue Evidence

| From Role | To Role | Message ID | Inbox Path | Payload Path | Report Path | Message Status | Report Status | Provider Evidence | Notes |
|---|---|---|---|---|---|---|---|---|---|
```

## Goal Tracking Contract

Goal Tracking は、Codex runtime の `goal` を親タスクの運行看板として扱い、Director / Gate / PM の組織フローに接続するための契約である。`goal` は作業主体ではなく、実作業は Active Set に載った resident agent / team member が担当する。

Task Detail には goal を使う場合だけ次のセクションを置く。

```markdown
## Goal Tracking

| Field | Value |
|---|---|
| Goal ID |  |
| Parent Task ID |  |
| Goal Owner Role | teams-project-manager / <team-director> / <gate-role> |
| Delegation Evidence Required | true |
| Goal Completion Gate | `finalization-check` complete + main transport renderer `Final Transport Render Check` |
| Goal Status | not_created / active / blocked / ready_for_final / complete |
| Final Transport Render Check | true / false |
| Notes |  |
```

| Field | Required | Meaning |
|---|---|---|
| `goal_id` | When goal created | Runtime goal identifier or local goal label |
| `parent_task_id` | Yes | GTC が作成した親 Task ID |
| `goal_owner_role` | Yes | `teams-project-manager`、各 Director、または Gate 固定ロール |
| `delegation_evidence_required` | Yes | 実作業を resident agent / team member へ dispatch した証跡が必要 |
| `goal_completion_gate` | Yes | `finalization-check complete + main transport renderer Final Transport Render Check` |
| `goal_status` | Yes | `complete` は `Final Transport Render Check` 後だけ許可 |
| `final_transport_render_check` | Yes before complete | main transport renderer が表示整形を完了した証跡 |

Codex main が直接作業した場合も、`Invocation Evidence` または team task に `acting_role`、対象範囲、review owner を残す。Claude provider の role が必要な場合は Claude の effective model / session / request / usage source を必須とし、Codex/OpenAI evidence で代替しない。

## Gate Task Creator Output

`gate-task-creator` は `Gate Intake Envelope` を受け取り、次を作成または更新する。

| Artifact | Required | Note |
|---|---|---|
| Task Detail | Yes | `00-Inbox&Tasks/Templates/Task-Detail-Template.md` の項目を満たす |
| Task Index row | Yes | Task Detail への wikilink を含める |
| Kanban entry | Yes | `status` に対応するセクションへ 1 回だけ置く |
| Project note | When applicable | 長期プロジェクトの成果物や判断履歴 |
| Policy note | When applicable | 再利用される運用ルール |
| Project Manager Handoff | Yes | `teams-project-manager` へ必ず渡す routing、review、approval、dispatcher sync 情報 |
| Resident Team Roster | Yes | metadata / process / provider / response readiness、モデル証跡の初期欄 |
| Active Set | Yes | Gate core / Infra core、lazy role、タスク別 active set |
| Invocation Evidence | Yes | intended/effective model、requestId、sessionId、usage source |
| Queue Evidence | Yes | message_id、inbox、payload、report、message/report status、provider evidence の対応 |
| Goal Tracking | When goal is used | parent Task ID、owner role、delegation evidence、finalization-check / Final Transport Render Check complete gate |
| Shared Resource Check | Yes | Task ID、Task Index、Kanban、既存 Task Detail の重複確認 |


## Project Manager Handoff

`gate-task-creator` はタスク作成後、必ず `teams-project-manager` へ handoff する。  
`Project Manager Handoff` がない Task Detail は Gate 起票完了として扱わない。

| Field | Required | Meaning |
|---|---|---|
| `handoff_to` | Yes | `teams-project-manager` |
| `handoff_status` | Yes | `sent_to_project_manager` または `pending` |
| `created_task` | Yes | 作成または更新した Task Detail への wikilink |
| `source_envelope` | Yes | 元依頼、正規化依頼、intent summary の要約 |
| `task_units` | Yes | 起票単位と対応 Task ID |
| `routing_hint` | Yes | 主担当、支援担当、レビュー候補 |
| `review_requirements` | Yes | `team-completion-check` / GTE が確認するレビュー証跡要件と人間承認の要否 |
| `approval_status` | Yes | `not_required` / `waiting_human` / `required_before_execution` |
| `dispatcher_sync_notes` | Yes | 採番状態、Index / Kanban 初期記録、同期上の注意 |
| `branch_plan` | When Git-managed | repo_root、repo_kind、base_branch、working_branch、branch_owner、shared_by_teams、default_branch_work_allowed、branch_action |
| `workspace_prep_handoff` | When branch action required | `checkout_existing` / `create_working_branch` の場合は `git-workspace-prep` への handoff。`branch_action: none` の whitelist default branch では `not_required` |
| `open_questions` | No | `teams-project-manager` が確定すべき残論点 |
| `resident_roster_required` | Yes | `true` |
| `active_set` | Yes | always active と task active の初期指定 |

`project-owner` という旧称が既存ノートに残る場合は互換 alias として読む。新規記録では `teams-project-manager` または `project-manager` を使う。


## Completion Gate Contract

Team Director 完了後の Gate 間 I/O は次を正とする。

| Artifact | Producer | Consumer | Required Fields |
|---|---|---|---|
| Completion Report / Structured completion signal | Team Director | TPM `team-completion-check` command | parent task、team task board、done task、review result、blocker、approval status |
| Completion Assessment command artifact | TPM `team-completion-check` command | `gate-task-evaluator` | all team tasks done、mutual reviews complete、active set satisfied、blockers remaining、assessment status、next phase allowed |
| Quality Evaluation | `gate-task-evaluator` | `git-publisher` or Vault final update | requirements satisfied、reviews satisfied、validation satisfied、invocation evidence satisfied、Goal Tracking satisfied、Task Change Manifest、Git Publication Manifest、approved scope、approved diff snapshot、unrelated dirty paths、commit / push / PR required、main-push whitelist decision、evaluation status |
| Git Publication Result | `git-publisher` | Vault final update / `finalization-check` | commit status、commit hashes、push status、remote branch、PR status、PR URL、blocked reason、next role |
| Finalization Check | `finalization-check` command | `final-transport-render-check` / `main_transport_renderer` | finalization status checked、finalization status、Task Change Manifest present、approved diff closed、git publication status、commit requirement satisfied、push requirement satisfied、PR requirement satisfied、Vault final update complete、Index / Kanban synced、Resident Roster complete、goal ready for final transport render |
| Completion Envelope | `finalization-check` command | `main_transport_renderer` | result、changed artifacts、review status、validation status、publication evidence、resident roster evidence、goal tracking status、risks or limits |
| Final Transport Render Check | `main_transport_renderer` | Human user / goal status update | source envelope、facts preserved、no new task judgment、worker persona leakage false、style profile、safety exception |

`finalization_status_checked: true` と `finalization_status: complete` がない Completion Envelope は、main transport renderer の入力として扱わない。旧 artifact 互換で `guardian_status_checked` / `guardian_status` を読む場合も、正本上は `finalization-check` の出力として扱う。
`goal_status: complete` は `Final Transport Render Check` 後だけ許可する。`finalization-check` complete は goal complete そのものではなく、final transport render へ進めるための完了保証である。

## Role Execution Evidence

`finalization-check` / final transport render 前の final validation では、Gate role の Invocation Evidence とは別に実作業 role の evidence を必須にする。

| Field | Required | Meaning |
|---|---|---|
| `Role` / `role_id` | Yes | 実作業を担当した non-gate role |
| `Result` | Yes | `complete` / `done` / `passed` などの完了状態 |
| `Usage Source` | Yes | provider-backed usage source。`role_agent_worker_local_stub`、`main_agent_local`、`self_certified` は禁止 |

`main-agent`、`codex-main`、`claude-main`、`entrypoint` は実作業 role として記録してはならない。

## Task-Scoped Commit Contract

Completion Gate は repo 全体の clean state ではなく、Task Change Manifest に含まれる task-owned approved diff の closure を確認する。

Task Change Manifest の最小 fields は次を正とする。

| Field | Required | Meaning |
|---|---|---|
| `repo_root` | Yes | Git repo root |
| `task_id` | Yes | 対象 Task ID |
| `owned_paths` | Yes | task が所有する path |
| `excluded_paths` | Yes | scope 外、別タスク、生成物、一時ファイル |
| `approved_scope` | Yes | commit skill が stage してよい path / hunk 範囲 |
| `approved_diff_snapshot` | Yes | review / validation OK 後に承認された task-owned diff |
| `reviewed_artifacts` | Yes | snapshot を承認した review / validation 証跡 |
| `commit_required` | Yes | task-owned approved diff の有無で判定 |
| `commit_hashes` | When committed | approved diff を閉じた commit hash |
| `unrelated_dirty_paths` | When applicable | repo に残る別タスク由来の dirty diff |

`gate-task-evaluator` は repo dirty 全体ではなく `approved_diff_snapshot` を基準に `commit_required` を判定する。  
`commit` は `approved_scope` / `approved_diff_snapshot` なしの unscoped commit を拒否し、task-owned diff だけを stage / commit する。  
`finalization-check` は repo clean を要求しない。`approved_diff_snapshot` が `commit_hashes` と `committed_diff_matches_snapshot: true`、または明示的な commit 不要理由で閉じているかを確認する。
同じ repo に別タスク由来の dirty diff が残る場合は `unrelated_dirty_paths` として記録し、当該 task の完了を妨げない。

task-scoped commit event の owner は [[01-Projects/AI-Agent-Organization/TSK-1070-task-scoped-commit-event-owner]] の判断どおり `infra-task-dispatcher` とする。

## Git Publication Contract

Git Publication Manifest の最小 fields は次を正とする。

| Field | Required | Meaning |
|---|---|---|
| `task_id` | Yes | 親 task |
| `repo_root` | Yes | Git repo root |
| `branch_plan` | Yes | TPM が決めた Branch Plan |
| `task_change_manifest` | When commit required | task-owned approved diff 契約 |
| `commit_required` | Yes | commit 要否 |
| `push_required` | Yes | push 要否 |
| `pr_required` | Yes | PR 要否 |
| `publication_policy` | Yes | repo profile / branch policy |
| `handoff_to` | Yes | `git-publisher` |

Git Publication Result の最小 fields は次を正とする。

| Field | Required | Meaning |
|---|---|---|
| `commit_status` | Yes | `complete` / `not_required` / `blocked` |
| `commit_hashes` | When committed | 作成 commit |
| `push_status` | Yes | `complete` / `not_required` / `blocked` |
| `remote_branch` | When pushed | push 先 |
| `pr_status` | Yes | `created` / `not_required` / `blocked` / `deferred` |
| `pr_url` | When created | PR URL |
| `blocked_reason` | When blocked | 停止理由 |
| `next_role` | Yes | `vault_final_update` または差し戻し先 |

`git-publisher` は commit / push / PR の順序制御だけを担い、差分品質評価や Branch Plan 決定を行わない。

`push_required` は Branch Plan、repo profile、`push` skill の `references/main-push-repos.md` を根拠に判定する。
whitelist 外 repo の default branch は `push_required: false` とし、working branch は clean / upstream / remote 条件を満たす場合に `push_required: true` にできる。
`push` は whitelist 外 repo の default branch への自動 push を拒否し、`main-push-repos.md` に記載された repo だけ default branch push を許可する。
TPM は `main-push-repos.md` に記載された repo の default branch 作業では `default_branch_work_allowed: true`、`working_branch: <base_branch>`、`branch_action: none` を記録し、task branch を切らない。
`git-workspace-prep` は TPM の Branch Plan に従って作業 branch を準備する。`branch_action: none` の場合は default branch のまま no-op を記録し、Director が独自 branch を作らないようにする。

## Completion Envelope

main transport renderer は、`finalization-check` が complete と判定し、`Final Transport Render Check` が通った Completion Envelope から次の要素だけを人間向けに整える。

| Field | Meaning |
|---|---|
| `result` | 何を完了したか |
| `changed_artifacts` | 作成・更新したファイル、Vault ノート |
| `review_status` | レビュー証跡と人間承認の状態 |
| `validation_status` | eval、test、static validation の状態 |
| `commit_hash_or_commit_not_required` | commit hash または commit 不要判断 |
| `push_status_or_push_not_required` | push 完了または push 不要判断 |
| `pr_url_or_pr_not_required` | PR URL または PR 不要判断 |
| `finalization_status_checked` | Finalization Check が Task Detail の Completion Gate を確認・更新した証跡 |
| `vault_update_status` | Vault final update の状態 |
| `risks_or_limits` | 残るリスク、未対応範囲 |
| `next_actions` | 次に進めるべき作業 |

事実、判断、リスク、ファイルパス、Vault リンクは変更しない。  
文体だけを読みやすく整える。

## Final Transport Render Check

main transport renderer は作業 role ではなく、チャット entrypoint の user interface layer である。
Completion Envelope への新規判断追加は禁止する。

| Field | Required | Meaning |
|---|---|---|
| `Renderer` | Yes | `main_transport_renderer` |
| `Source Envelope` | Yes | 元にした `Completion Envelope` |
| `Facts Preserved` | Yes | `true`。変更内容、検証、リスク、ファイルパスを変えていない |
| `No New Task Judgment` | Yes | `true`。実装、レビュー、完了判定を追加していない |
| `Worker Persona Leakage` | Yes | `false`。作業 role / Gate role に妹文体・人格 context を混ぜていない |
| `Style Profile` | Yes | 例: `user-interface-imouto` |
| `Safety Exception` | Yes | 重大警告・拒否・破壊的操作確認などで文体を抑制したか |

`gate-response-humanizer` は旧 mandatory final role の compatibility / reference として残す。
新規タスクの pre-final validation では GRH provider evidence を必須にしない。

## Validation Checklist

Gate 系の作業は、少なくとも次を満たす。

| Check | Required |
|---|---|
| 元依頼が保存されている | Yes |
| 正規化依頼が元依頼の意味を変えていない | Yes |
| 人間承認の要否が明示されている | Yes |
| Task Detail に主担当、状態、成果物、Vault 更新先がある | Yes |
| Task Index と Kanban が Task Detail と矛盾しない | Yes |
| レビュー証跡要件が先に設定されている | Yes |
| `Project Manager Handoff` があり、`teams-project-manager` へ渡せる | Yes |
| Execution Preflight の organization instance と既存必須項目がすべて true である | Yes |
| Vault 更新が完了条件に含まれている | Yes |
| Completion Gate が handoff に含まれている | Yes |
| Resident Team Roster / Active Set / Invocation Evidence / Queue Evidence がある | Yes |
| Gate core / Infra core、lazy role、道具スキル常駐外が区別して記録されている | Yes |
| `finalization-check` complete と `Final Transport Render Check` 前に main transport renderer へ渡していない | Yes |
| final transport render が事実保持・新規判断なし・worker persona leakage false を記録している | Yes |

## Related Notes

- [[03-Contexts/Policies/AI-Organization]]
- [[03-Contexts/Policies/Dispatcher-IO-Contract]]
- [[03-Contexts/Policies/Task-File-Conventions]]
- [[01-Projects/AI-Agent-Organization/TSK-1053-gate-io-contract]]
