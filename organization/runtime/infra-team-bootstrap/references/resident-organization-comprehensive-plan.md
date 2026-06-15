# Resident Organization Comprehensive Implementation Plan

Updated: 2026-06-10

## Summary

チャットセッション単位で Organization Instance を作成し、全組織ロールを resident agent として登録し、SessionStart hook で ITB-owned lightweight resident shell の独立 tmux process を ensure する。

`infra-team-bootstrap` が組織起動、再開、終了、Resident Roster 検証を担当する。ユーザープロンプトは常に `gate-prompt-formatter` へ渡し、ITB への自然文直通分岐は作らない。

Gate / Infra は全タスク横断の運行基盤として常時 `metadata_ready` にする。Tech / Contents / Business は resident として登録し、軽量 process は起動しておくが、Claude / Codex provider client と tool sidecar は TPM または Director が必要と判定したタスクだけ ITB が activation して `response_active` に切り替える。

モデル設定は各 Team Role `SKILL.md` ではなく、ITB references の model registry で一元管理する。各 `SKILL.md` はロール契約、Input / Output、責務境界に集中させる。

## Goals

| Goal | Decision |
|---|---|
| 組織フローの矯正 | GPF 入口、`gtc-scaffold` 起票 command、TPM routing、Director / worker、`team-completion-check`、Evaluator、`vault_final_update`、`finalization-check`、`final-transport-render-check`、main transport renderer の順序を必須化する |
| エージェント起動の安定化 | SessionStart / Resume で ITB が roster metadata を検証し、不足 agent を lightweight resident shell の独立 tmux process として ensure する |
| 並行チャット対応 | GPF をグローバル singleton にせず、chat session ごとの agent instance として起動する |
| モデル証跡 | intended model は ITB model registry、effective model は transcript / session log を正本にする |
| モデル変更容易性 | Team Role `SKILL.md` からモデル設定を外し、registry の1箇所変更で切り替え可能にする |
| Claude / Codex 差分吸収 | 共通契約は ITB references、実行環境差分は adapters に分離する |
| 終了処理 | SessionArchive / Close で handoff summary、shutdown、archived roster を残す |

## Current Problems To Resolve

| Problem | Impact | Resolution |
|---|---|---|
| model / provider / execution mode が各 Team Role skill に分散している | モデル品質やコスト状況に応じた切り替えが重い | ITB `model-registry.md` に一元化する |
| ITB が `SKILL.md frontmatter` のモデル設定を読む前提になっている | model registry 方針と矛盾する | ITB と adapter の参照先を registry に変更する |
| legacy `main-agent` 記述が残っている | Claude / Codex を単一メイン実行者として扱う旧運用が混ざる | main-agent 方針を削除し、どちらの入口でも同じ Organization Instance 方針に寄せる |
| legacy Claude `team-config` が残っている | Claude 固有設定が共通運用の正本に見える | 必要な adapter 情報だけ ITB references へ移し、旧ファイルは廃止する |
| GPF 起動トリガーが曖昧 | 複数チャット時に入口処理が混線する | SessionStart は ITB、PromptSubmit は session-local GPF と定義する |
| 道具スキルが resident agent と混同される | commit / save / Obsidian 操作が常駐対象に混入する | tool skills は resident 対象外、明示時のみ一時実行とする |

## Conversation Decisions To Preserve

| Topic | Decision |
|---|---|
| 全エージェント常駐 | 組織ロールはチャットセッションごとに resident metadata として登録する |
| Gate / Infra | 全タスク横断の運行基盤なので常時 metadata_ready |
| Tech / Contents / Business | resident metadata と独立 process は持つが、タスクごとに必要なチームだけ response_active |
| idle | resident metadata はあるが現タスクでは作業しない状態 |
| 起動トリガー | ユーザー自然文ではなく SessionStart / Resume / Archive lifecycle |
| GPF | どんなユーザープロンプトも最初に通る入口。`チームを起動して` も例外にしない |
| ITB | チーム起動、roster 確認、shutdown を担当する別スキル |
| 起動済み判定 | ITB は初期処理で roster と tmux process readiness を確認し、揃えば `ready` / `already_ready` で終了 |
| 複数チャット | GPF は chat session ごとの instance。全チャット横断 singleton にはしない |
| プロジェクト運用 | 原則 1 project = 1 chat session。並行 project は複数 Organization Instance |
| Archive | chat session archive / close 時に resident agent を shutdown し、archived roster を残す |
| Flow Contract | 各 agent skill 前半に Input Agents / Output Agents を置き、次 hop を明確化する |
| main-agent 旧運用 | Claude / Codex のどちらをメインにするかという旧変数運用は廃止 |
| model 設定 | 各 Team Role skill ではなく ITB model registry に一元化 |
| team-config | Claude 固有配置から外し、ITB references に統合。旧ファイルは削除または廃止 |
| Completion | `finalization-check` complete と `final-transport-render-check` 記録が揃うまで完了扱いにしない |

## Organization Runtime Model

| Concept | Rule |
|---|---|
| Chat Session | 1 Organization Instance |
| Organization Instance | その chat session 内の Resident Roster 全体 |
| Skill | 共有されるロール定義 |
| Agent Instance | chat session ごとに起動される実体 |
| Project | 原則 1 chat session に紐づける |
| Parallel Projects | 複数 chat session / 複数 Organization Instance で扱う |
| GPF | chat session ごとの session-local agent として扱う |
| Shared Vault | 複数 Organization Instance から触られる共有資源 |

GPF は全チャット横断の singleton にしない。複数チャットから同時に prompt が来た場合は、それぞれの Organization Instance 内の GPF が処理する。

Task Index、Kanban、Vault final update などの共有資源は `gate-task-creator` と `infra-task-dispatcher` が整合性を監視する。

## Lifecycle

| Event | Handler | Result |
|---|---|---|
| `SessionStart` | `infra-team-bootstrap` | Organization Instance bootstrap |
| `SessionResume` | `infra-team-bootstrap` | Roster check and partial recovery |
| `PromptSubmit` | `gate-prompt-formatter` | Normal Gate intake |
| `TaskRouting` | `teams-project-manager` | task active set and director handoff |
| `TaskCompletion` | `team-completion-check` / Evaluator / `vault_final_update` / `finalization-check` / `final-transport-render-check` / main transport renderer | Completion Envelope and final response |
| `SessionArchive` / `SessionClose` | `infra-team-bootstrap` shutdown mode | Handoff summary and archived roster |

`SessionStart` は、新しいチャット/作業セッションが作られた直後、最初のユーザープロンプトを GPF が処理する前の初期化タイミングを指す。

もし runtime が明示的な SessionStart hook を持たない場合は、最初の GPF preflight で roster 不在を `bootstrap_missing` として fail させ、manual recovery で ITB を実行する。

## Resident States

| State | Meaning |
|---|---|
| `resident` | 作業セッション内で agent instance metadata が登録済み |
| `metadata_ready` | bootstrap hook が roster と intended model を登録済み。provider response evidence はまだ無い |
| `process_ready` | ITB-owned lightweight resident shell が tmux 上の独立 process として起動済み。provider client、tool sidecar、provider response evidence はまだ無い |
| `provider_ready` | ITB-directed activation により Claude / Codex CLI process へ昇格済み。response evidence はまだ別扱い |
| `tool_sidecar_ready` | MCP / browser / computer-use 等の tool sidecar が必要時に起動し、証跡がある |
| `response_active` | 現タスクで判断、作業、レビュー、同期を担当し、provider session / model / usage evidence がある |
| `idle` | resident metadata はあるが現タスクでは発言、作業、Vault 更新をしない |
| `resetting` | タスク切替のため context reset / handoff 中 |
| `unavailable` | 起動失敗、session 不明、provider 障害などで利用不可 |
| `archived` | SessionArchive / Close により終了済み |

Gate / Infra は常時 `metadata_ready`。Tech / Contents / Business は resident metadata と lightweight independent process を持つが、タスク対象外なら `idle` に戻す。`process_ready` は `provider_ready` / `tool_sidecar_ready` / `response_active` ではなく、`response_active` は Invocation Evidence が揃った場合だけ使う。

## Always Active Roles

| Team | Agents |
|---|---|
| Gate entry / TPM | `gate-prompt-formatter`, `teams-project-manager` |
| Gate command | `gtc-scaffold` as `gate-task-creator` responsibility boundary |
| Gate lazy | `gate-task-evaluator` |
| Gate reference | `gate-task-assessor`, `gate-task-guardian`（runtime resident / queue consumer にしない） |
| Infra | `infra-team-bootstrap`, `infra-director`, `infra-task-dispatcher`, `infra-local-qa` |

Gate entry / TPM は入口、routing、completion command gate を担う。起票は GPF report から `gtc-scaffold` command が直接作成し、`gate-task-creator` provider は SessionStart で起動しない。`gate-task-evaluator` は必要時に lazy activation される。旧 `gate-task-assessor` / `gate-task-guardian` は legacy read compatibility の参照 role であり、bootstrap metadata の active resident 集合に含めない。Infra は roster、Vault、Task Index、Kanban、dispatcher、local QA を担う。

## Task Active Roles

| Team | Activation Rule |
|---|---|
| Tech | コード、設計、テスト、セキュリティ、性能、技術文書などが必要なときに active |
| Contents | 調査、要約、記事、説明文、表現品質が必要なときに active |
| Business | 戦略、要件整理、情報設計、提携、法務観点が必要なときに active |

TPM はチーム単位の active set と director handoff を決める。各 director はチーム内 worker / reviewer の active 化を決める。

active ではない resident agent は、成果物作成、レビュー、Vault 更新、ユーザー応答をしない。

## Tool Skills

| Skill Type | Rule |
|---|---|
| bridge | resident agent ではなく、一時実行ツール |
| commit | evaluator の commit 判定後だけ一時実行 |
| save | ユーザー明示または保存提案承認後だけ一時実行 |
| Obsidian CLI | Infra / Gate の必要時に一時実行 |
| browser / chrome | 調査や検証が必要なときだけ一時実行 |

tool skills は Resident Roster に混ぜない。明示例外がある場合も `execution_mode: tool` として Invocation Evidence に残し、resident / active set には入れない。

## Gate Flow

必須順序:

```text
ITB bootstrap
-> GPF
-> gtc-scaffold command
-> TPM
-> active set
-> director / worker
-> team-completion-check
-> evaluator
-> commit or commit_not_required
-> vault_final_update
-> finalization-check
-> final-transport-render-check
-> main_transport_renderer
```

main transport renderer は `finalization-check` が `Finalization Status: complete` を出し、`final-transport-render-check` が facts-preserved / no-new-task-judgment を記録した Completion Envelope がない限り実行しない。旧 `gate-task-assessor` / `gate-task-guardian` は互換参照に限る。

## Flow Contract Rule

各 Team Role `SKILL.md` の前半には Flow Contract を持たせる。

| Field | Purpose |
|---|---|
| `Input Agents` | この agent を active 化できる移譲元 |
| `Output Agents` | この agent が次に渡せる移譲先 |
| `Required Handoff Artifact` | 次工程へ渡す必須成果物 |
| `Return Policy` | メイン transport への返却を workflow output と混同しないための制約 |
| `Forbidden Outputs` | その role が作ってはいけない成果物 |

agent は Output Agents 以外へ workflow output を返さない。チャットセッションのメイン transport へ返す場合は、搬送のための表示に限定し、組織フロー上の正式 output とは扱わない。

## Model Registry Centralization

モデル設定は ITB references に一元化する。

追加する正本:

```text
Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/model-registry.md
```

registry fields:

| Field | Meaning |
|---|---|
| `agent_id` | 正式ロール ID |
| `team` | `gate` / `tech` / `contents` / `business` / `infra` |
| `provider` | `anthropic` / `openai` |
| `primary_model` | 起動時の第一候補 |
| `fallback_models` | fallback 候補 |
| `execution_mode` | `agent` / `codex` / `chat` / `long-run` |
| `cost_tier` | `low` / `medium` / `high` |
| `quality_tier` | role に要求する推論品質 |
| `startup_profile` | `resident_shell` / `active_full` / `long_run` |
| `long_run_preferred` | 長期実行時の推奨モデル |
| `last_reviewed` | 最終確認日 |
| `notes` | fallback、alias、障害メモ |

Team Role `SKILL.md` から削除する fields:

```text
primary_model
fallback_models
execution_provider
execution_mode
model_rationale
upgrade_policy
cost_tier
long_run_preferred
```

Team Role `SKILL.md` に残す fields:

```text
team
agent_id
status
purpose
Flow Contract
role instructions
```

ITB 自身の bootstrap model は例外として最小設定を残してよい。これは registry を読む前に ITB を起動するための seed であり、組織ロール全体のモデル正本ではない。

## Roster Schema

Task Detail または Project note に Resident Team Roster を記録する。

| Field | Meaning |
|---|---|
| `agent_id` | 正式ロール ID |
| `team` | 所属チーム |
| `resident_status` | `resident` / `unavailable` / `archived` |
| `activation_status` | `metadata_ready` / `response_active` / `idle` / `resetting` |
| `always_active` | Gate / Infra は `true` |
| `provider` | registry 由来 |
| `intended_model` | model registry 由来 |
| `effective_model` | transcript / session log 由来 |
| `execution_mode` | registry 由来 |
| `session_id` | transcript / Codex session id |
| `last_request_id` | 最後に確認した request id |
| `usage_source` | Claude transcript JSONL、Codex session log など |
| `active_for_task` | 担当中 Task ID |
| `last_reset_at` | タスク切替時の reset 時刻 |
| `notes` | fallback、alias 解決、障害メモ |

## Task Detail Additions

Task Detail には次の章を追加する。

```markdown
## Resident Team Roster

[Team Roster table]

## Active Set

| Task Phase | Always Active | Task Active | Idle Resident | Reason |
|---|---|---|---|---|

## Invocation Evidence

| Agent | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Result |
|---|---|---|---|---|---|---|
```

## Claude / Codex Adapter Policy

| Runtime | Rule |
|---|---|
| Claude | Claude Team adapter が registry の provider / model を Claude alias に解決する |
| Codex | Codex adapter が registry の provider / execution mode を参照し、bootstrap では lightweight resident shell を起動し、activation 時だけ Claude 対象は `claude`、Codex 対象は `codex` に昇格する |
| Remote Claude | Claude アプリ / remote-control 経由でも Organization Instance 方針は同じ |
| Unknown hook | ITB が自動起動できない場合、GPF preflight で `bootstrap_missing` として止める |

Claude / Codex のどちらからチャットセッションを始めても、入口は同じ Organization Instance lifecycle に寄せる。

## Claude Hook Integration

Claude / Codex では、Organization Instance lifecycle を adapter-specific hook scripts で起動する。Hook はメインエージェント配下の subagent を作らず、ITB 共通 builder の `--launch-agents` で lightweight resident shell を tmux 上の独立 process として ensure する。Provider CLI を直接起動したい activation / 検証は ITB が `ITB_AGENT_PROCESS_MODE=provider` を明示して実行する。

| Runtime | Hook Event | Script | Purpose |
|---|---|---|---|
| Claude | `SessionStart` | `$HOME/.claude/hooks/itb-session-start.sh` | Claude state root に bootstrap state と process evidence を作る |
| Claude | `UserPromptSubmit` | `$HOME/.claude/hooks/itb-prompt-preflight.sh` | readiness が無くても prompt は block せず、not-ready 状況を advisory context として注入する（詰み回避、ゲートは注入文で維持） |
| Claude | `SessionEnd` | `$HOME/.claude/hooks/itb-session-end.sh` | hook state を archived にする |
| Codex | `SessionStart` | `$HOME/dotfiles/codex/hooks/itb-session-start.sh` | Codex state root に bootstrap state と process evidence を作る |
| Codex | `UserPromptSubmit` | `$HOME/dotfiles/codex/hooks/itb-prompt-preflight.sh` | readiness が無くても prompt は block せず、not-ready 状況を advisory context として注入する（詰み回避、ゲートは注入文で維持） |
| Shared builder | command hook | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py` | `team-config.md` / `model-registry.md` から Roster / Bootstrap Report / tmux process evidence を生成する |

hook script は Task tool / Agent tool の subagent spawn を直接実行しない。`ITB_AGENT_CHILD=1` を子 process に渡して再帰 hook を避け、`SessionStart` / `UserPromptSubmit` の additional context と block decision を使って、ITB readiness が無いまま GPF / `gtc-scaffold` / 実作業へ進むことを防ぐ。

`TeammateIdle` は tmux layout 用途として維持し、bootstrap trigger として扱わない。

## Main Agent Policy Removal

旧 `main-agent` 方針は、組織エージェント化前に Claude と Codex をそれぞれ1体の大きな実行主体として扱っていた時代の互換記述である。Resident Organization では、実行主体は `{main-agent}` 変数ではなく Organization Instance 内の role agent と Gate flow で決まる。

削除対象:

| Target | Required Action |
|---|---|
| `COMMON-AGENTS.md` の `## メイン実行エージェント` 章 | 全文削除 |
| `{main-agent}` 変数の説明 | 全文削除 |
| `{main-agent}=Codex` の固定値 | 削除 |
| Claude が `{main-agent}=Codex` のため `claude-codex-bridge` へ委譲する記述 | 削除 |
| Claude remote-control でも `{main-agent}` に従う記述 | 削除 |
| 将来 `{main-agent}` を変更できるという記述 | 削除 |

置き換え後の原則:

| New Principle | Meaning |
|---|---|
| Entry symmetry | Claude / Codex のどちらで chat session を開始しても Organization Instance lifecycle に入る |
| Role execution | 実作業は GPF -> `gtc-scaffold` command -> TPM -> Director / worker -> Completion Gate で決まる |
| Transport only | Claude / Codex の UI や bridge は搬送手段であり、組織上の main agent ではない |
| Bridge as tool | `claude-codex-bridge` / `codex-claude-bridge` は resident agent ではなく一時実行 tool skill |

削除後に追加する最小方針:

```markdown
## Claude / Codex Entry Symmetry

- Claude / Codex のどちらでも、ユーザープロンプト処理前に `infra-team-bootstrap` が Organization Instance readiness を確認する
- readiness 未確認なら、`gate-prompt-formatter`、起票、実作業、レビュー、commit へ進まない
- readiness 確認後、ユーザープロンプトは必ず `gate-prompt-formatter` に渡す
- Claude / Codex 固有の hook・adapter 手順は ITB references を正本とする
```

検証:

| Check | Expected |
|---|---|
| `rg "{main-agent}|main-agent|メイン実行エージェント"` | 現行 policy から検出されない |
| `rg "claude-codex-bridge.*main|Codex.*メイン"` | 旧 main-agent 委譲文脈で検出されない |
| bridge skill references | tool skill としてのみ残る |
| Claude / Codex entry docs | Organization Instance lifecycle に統一されている |

## Legacy Team Config Decommission

旧 Claude `team-config` は Claude / tmux 固有の実験設定であり、Resident Organization の正本にはしない。共通チーム構成は ITB references に移し、Claude 固有の起動詳細は adapter に分離する。

削除または廃止対象:

| Target | Required Action |
|---|---|
| legacy Claude `team-config.md` | ITB references へ移行後、削除または deprecated stub 化 |
| `dotfiles/README.md` の legacy team-config 配置説明 | 削除または ITB references への移行説明に変更 |
| `dotfiles/agents/base/common.md` の legacy team-config 参照 | ITB references 参照へ変更 |
| `COMMON-AGENTS.md` の Claude Code team-config 正本記述 | ITB references と adapter 参照へ変更 |
| `チームを起動して` など自然文 trigger 記述 | 削除。PromptSubmit は常に GPF |
| `project-owner` / `project-manager` など旧 Team 実験ロール | 現行 role agent へ移行または削除 |
| tmux pane layout を共通契約として扱う記述 | Claude adapter の表示詳細へ移動 |

移行先:

| Legacy Content | New Location |
|---|---|
| resident team 構成 | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/team-config.md` |
| model / provider / execution mode | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/model-registry.md` |
| Claude / tmux / TeamCreate 手順 | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/adapters/claude-team.md` |
| Codex 起動差分 | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/adapters/codex-team.md` |
| organization policy | Agents-Vault `03-Contexts/Policies/AI-Organization.md` |

削除判断:

| Condition | Action |
|---|---|
| 全内容が ITB references に移行済み | legacy file を削除 |
| まだ外部 symlink / loader が参照する | deprecated stub にし、ITB references への参照だけ残す |
| runtime が旧 path を必須としている | adapter compatibility として最小 stub を残し、正本ではないと明記 |

deprecated stub を残す場合の内容:

```markdown
# Deprecated Claude Team Config

This file is no longer the source of truth.

Use:
- `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/team-config.md`
- `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/model-registry.md`
- `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/adapters/claude-team.md`
```

検証:

| Check | Expected |
|---|---|
| legacy team-config が正本として参照されない | Yes |
| Claude 固有手順が adapter に閉じている | Yes |
| 共通 resident set が ITB `team-config.md` にある | Yes |
| model routing が `model-registry.md` にある | Yes |
| 自然文 trigger で ITB 直通する記述がない | Yes |
| `チームを起動して` も GPF へ渡る | Yes |

## Legacy Cleanup

| Target | Action |
|---|---|
| legacy `main-agent` policy | 削除する。Claude / Codex の単一メイン実行者運用には戻さない |
| legacy Claude `team-config` | ITB references へ必要情報を移し、旧ファイルは廃止する |
| old Team names | `teams-*` 旧実験スキルは互換参照のみ。現行 flow では使わない |
| model fields in Team Role skills | model registry 移行後に削除する |

## Implementation Phases

| Phase | Work |
|---|---|
| 1. Registry creation | `model-registry.md` を作成し、全 Team Role の intended model / provider / execution mode を移す |
| 2. ITB reference update | ITB、team-config、Claude adapter を registry 参照に変更する |
| 3. Policy cleanup | COMMON / Vault policy / Gate I/O / template の model frontmatter 前提を registry 前提に変更する |
| 4. Skill frontmatter cleanup | Team Role `SKILL.md` から旧 model fields を削除する |
| 5. Legacy config cleanup | main-agent 記述と旧 Claude team-config を削除または廃止扱いにする |
| 6. Eval expansion | ITB / `gtc-scaffold` / TPM / `finalization-check` / main transport renderer / tech-tester eval に registry と full gate flow の検証を追加する |
| 7. Validation | JSON eval、coverage check、model field absence、registry coverage、Gate flow E2E を実行する |
| 8. Vault final update | Task Detail、Index、Kanban、Handoff、review line、final update を同期する |

## Implementation File Map

| Area | Files / Paths | Required Change |
|---|---|---|
| ITB skill | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/SKILL.md` | frontmatter model 参照を registry 参照に変更し、In / 正本 / 手順を更新 |
| ITB config | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/team-config.md` | ロール構成と lifecycle の正本に限定し、model 正本を registry へ分離 |
| Model registry | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/model-registry.md` | 新規作成。全 Team Role の model routing を収容 |
| Claude adapter | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/adapters/claude-team.md` | SKILL frontmatter model mapping を registry mapping に変更 |
| Codex adapter | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/adapters/codex-team.md` | 新規作成。Codex entry / bridge / execution mode 差分を定義 |
| Team Role skills | `skills/*/SKILL.md` where `category: Team Role` | model fields を削除し、Flow Contract と role behavior に集中 |
| Gate skills | `gate-*`, `teams-project-manager` | Resident Roster / registry / Invocation Evidence の参照元を更新 |
| Infra skills | `infra-*` | roster、dispatcher、Vault sync、shutdown の責務を更新 |
| Template | `skills/TEMPLATE.md` | Team Role model fields を要求しない形に更新 |
| Evals | `skills/*/evals/evals.json` | 既存ケース保持、新仕様ケース追加 |
| Common policy | `COMMON-AGENTS.md` or managed dotfiles source | main-agent 章削除、Claude / Codex entry symmetry へ置換 |
| Legacy team config | legacy Claude team-config path | 移行後に削除または deprecated stub 化 |
| Vault policy | Agents-Vault policies and templates | AI Organization / Gate IO / Task templates を registry 前提へ更新 |

## Implementation Order Details

1. 現状検出を行う。
   - Team Role skill の一覧を取得する。
   - 旧 model frontmatter の分布を確認する。
   - `{main-agent}`、`main-agent`、`メイン実行エージェント` の残存箇所を確認する。
   - legacy `team-config` 参照箇所を確認する。

2. `model-registry.md` を作成する。
   - 既存 Team Role skill frontmatter から model 情報を移す。
   - `agent_id` 単位で一意にする。
   - Gate / Infra は低コストで metadata_ready / activation に向く model を設定する。
   - `tech-lead` のみ最上位 Claude を許可する方針を保持する。
   - code 実装 / diff review / debug 系は OpenAI / Codex 系を優先する方針を保持する。

3. ITB を registry 参照へ切り替える。
   - 起動対象判定は `team-config.md`。
   - model / provider / execution mode は `model-registry.md`。
   - effective model / session / usage は runtime evidence。
   - registry 欠落時は fail。

4. adapters を分離する。
   - Claude 固有の TeamCreate、tmux、transcript JSONL は `claude-team.md`。
   - Codex 固有の session id、spawn / bridge、Codex-native 実行差分は `codex-team.md`。
   - 共通方針を adapter に重複させない。

5. main-agent 旧運用を削除する。
   - `{main-agent}` による実行主体切替を完全に廃止する。
   - Claude / Codex のどちらも Organization Instance lifecycle の入口に揃える。
   - bridge は tool skill として残す。

6. legacy team-config を廃止する。
   - 共通情報は ITB references へ移す。
   - Claude 固有情報は Claude adapter へ移す。
   - runtime 互換が不要なら削除する。
   - runtime 互換が必要なら deprecated stub にする。

7. Team Role skill を整理する。
   - model fields を削除する。
   - Flow Contract は維持する。
   - Input Agents / Output Agents を前半に残す。
   - role 本文は実作業時に読む前提を維持する。

8. Gate flow と tests を拡張する。
   - 既存 eval を保持する。
   - registry coverage、main-agent absence、legacy team-config absence を追加する。
   - Gate flow E2E は main transport renderer の `Final Transport Render Check` まで確認する。

9. Vault final update を行う。
   - Task Detail、Index、Kanban、Handoff、review line、final update を同期する。
   - 実装が完了しても Vault 更新なしでは complete にしない。

## Test Plan

既存テストケースは削除、置換、上書きしない。新仕様は追加ケースとして扱う。

| Test | Expected |
|---|---|
| Roster 対象判定 | 組織ロール全員が resident 候補に入り、tool skills は常駐対象外 |
| Gate 常時 metadata_ready | Gate 系が初期状態から metadata_ready で、依頼受付前後に idle へ落ちない |
| Infra 常時 metadata_ready | Infra 系が常時 metadata_ready で、Task / Vault / Dispatcher 監視責務を持つ |
| 他チーム切替 | Tech / Contents / Business は必要時のみ response_active 化し、完了後 idle に戻る |
| response evidence 必須 | response_active には session / model / usage evidence が必須 |
| 誤常駐防止 | bridge / commit / save / Obsidian CLI が resident active set に混入しない |
| 明示例外 | tool skills は明示時だけ一時実行され、常駐化しない |
| Gate flow 完走 | GPF から main transport renderer まで入力、出力、証跡が欠けずに伝播する |
| Finalization 必須化 | `vault_final_update` 後に `finalization-check` と `final-transport-render-check` が確認し、main transport renderer は両 command evidence complete 後だけ動く |
| Final transport check | main transport renderer が事実、未実施事項、リスク、ファイル参照を改変しない |
| Registry coverage | 全 Team Role `agent_id` が model registry に存在する |
| Model frontmatter absence | Team Role `SKILL.md` に旧 model fields が残っていない |
| Intended model source | Roster の intended model が registry 由来である |
| Effective model source | effective model が transcript / session log 由来である |
| Model mismatch | intended と effective がずれたら `mismatch` として fail |
| Session-local GPF | 複数チャットの GPF がそれぞれ別 agent instance として扱われる |
| Shutdown | archive / close 時に handoff summary と archived roster が残る |
| Main-agent removal | `{main-agent}`、`main-agent`、`メイン実行エージェント` の旧運用が policy から消えている |
| Bridge remains tool | bridge skills は tool skill として残り、main-agent 委譲の正本にはならない |
| Legacy team-config removal | legacy Claude team-config が正本として参照されない |
| Team-config migration | 旧 team-config の必要情報が ITB `team-config.md` / adapter / registry に移っている |
| Natural language trigger removal | `チームを起動して` などの自然文で ITB 直通する記述がない |

## Failure Tests

| Failure | Expected |
|---|---|
| 証跡不足 | Task Detail / Index / Kanban / Handoff / review line 欠落で fail |
| Gate 欠落 | GPF / `gtc-scaffold` / TPM / `team-completion-check` / evaluator / `vault_final_update` / `finalization-check` / `final-transport-render-check` / main transport renderer のいずれかを飛ばすと fail |
| Final render 早期実行 | `finalization-check` complete または `final-transport-render-check` 前の main transport renderer 実行は fail |
| モデル不一致 | intended model と effective model がずれたら `mismatch` として fail |
| 未起動 | 必要 director / worker / reviewer が active 化されなければ fail |
| 誤起動 | 不要チームや tool skills が active 化されたら fail |
| 常駐漏れ | Gate / Infra が metadata_ready ではなく idle 扱いになれば fail |
| response 証跡不足 | response_active に session / model / usage evidence が無ければ fail |
| commit 判定漏れ | commit required / not required が未記録なら fail |
| Vault 更新漏れ | final update 証跡なしの完了宣言は fail |
| registry 欠落 | Team Role が registry に存在しなければ fail |
| 旧 model field 残存 | Team Role `SKILL.md` に旧 model field が残っていれば fail |
| main-agent 残存 | `{main-agent}` 変数や `メイン実行エージェント` 方針が残っていれば fail |
| bridge 誤昇格 | bridge skill が resident agent または main execution path として扱われたら fail |
| legacy team-config 正本化 | legacy Claude team-config が source of truth として参照されていれば fail |
| 移行漏れ | legacy team-config 削除後に必要な Claude adapter 情報が失われていれば fail |
| ITB 直通 trigger | ユーザー自然文から ITB へ分岐する仕様が残っていれば fail |

## Acceptance Criteria

| Criterion | Required |
|---|---|
| ITB が Organization Instance lifecycle の正本になっている | Yes |
| ユーザープロンプトが常に GPF に渡る | Yes |
| SessionStart / Resume で roster check が行われる | Yes |
| 起動済みなら `already_ready` で終了する | Yes |
| 未起動 agent だけ lightweight 独立 tmux process 起動対象になる | Yes |
| process / provider / tool sidecar readiness が別々に記録される | Yes |
| Gate / Infra が always metadata_ready になる | Yes |
| Tech / Contents / Business が task response_active 方式になる | Yes |
| tool skills が resident 対象外になる | Yes |
| Input / Output Agent contract が各 Team Role skill に存在する | Yes |
| model registry が model routing の正本になる | Yes |
| Team Role skill から旧 model frontmatter が除去される | Yes |
| Claude / Codex の入口差分が adapter に閉じる | Yes |
| legacy main-agent 記述が残らない | Yes |
| legacy Claude team-config が正本として残らない | Yes |
| legacy Claude team-config が削除または deprecated stub 化されている | Yes |
| main-agent 削除後も Claude / Codex 入口が同じ Organization Instance lifecycle で説明されている | Yes |
| bridge skills が resident roster に入らない | Yes |
| Completion Gate が main transport renderer まで完走する | Yes |
| Vault final update が完了条件に含まれる | Yes |

## Assumptions

- GPH / GRH は旧 mandatory final role の文脈上 `gate-response-humanizer` と解釈する。現行フローでは main transport renderer の `Final Transport Render Check` が正本である。
- `idle` は未起動ではなく、現タスクでは作業しない resident 状態を指す。
- 完了は成果物完成ではなく、`finalization-check` complete 後に main transport renderer が `Final Transport Render Check` を記録した状態を指す。
- モデル証跡は intended model と effective model を分離して扱う。
- intended model は ITB model registry、effective model は runtime の transcript / session log を正本にする。
- 1プロジェクト1チャットセッションを基本運用とし、複数プロジェクトは複数 Organization Instance で扱う。
- runtime に SessionStart hook が存在しない場合でも、GPF preflight で bootstrap 欠落を検出して逸脱を防ぐ。
- 実装時は既存の未コミット変更を巻き戻さず、関連差分だけを追加する。
