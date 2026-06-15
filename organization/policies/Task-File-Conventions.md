---
type: policy
status: active
owner: gate-task-creator
co_owners:
  - infra-task-dispatcher
source_task: TSK-1054
last_updated: 2026-06-14
---

# Task File Conventions

このノートは Agents-Vault におけるタスク管理ファイルの **配置・命名・粒度判断** の正本だよ。

`gate-task-creator` が起票するとき、`infra-task-dispatcher` が採番・同期するとき、他のエージェントがタスクノートを読み書きするときに、
このポリシーに従う。

採番ルール・状態同期・完了条件は [[03-Contexts/Policies/Dispatcher-IO-Contract]] を、
Gate 間の I/O 契約は [[03-Contexts/Policies/Gate-IO-Contract]] を正本とする。
このノートはそれらと衝突せず、ファイル取扱いの一段下のレイヤを定義する。

## Scope

| 項目 | 内容 |
|---|---|
| In | タスクノートの配置先、ファイル名規約、Task Detail の章立て、個別ノート vs バックログ束ねの判断、Project フォルダ選定、ID 衝突の扱い |
| Out | Task ID 採番ルール（Dispatcher）、Status 値の定義（Dispatcher）、Gate 間 Envelope の中身（Gate-IO-Contract）、各専門チームの実施手順 |
| Primary Readers | `gate-task-creator`、`infra-task-dispatcher` |
| Secondary Readers | `project-owner`、`infra-local-qa`、各専門チームの reviewer |

## Source of Truth Map

| Artifact | Path | Role |
|---|---|---|
| 受付口 | `00-Inbox&Tasks/Task-Gateway.md` | 人間の自然文依頼を受ける入口 |
| 索引 | `00-Inbox&Tasks/Task-Index.md` | 全タスクの一覧 |
| 状態別ビュー | `00-Inbox&Tasks/Kanban.md` | Status 別セクションへ wikilink を1回だけ配置 |
| 雛形 | `00-Inbox&Tasks/Templates/Task-Detail-Template.md` | Task Detail の章立て雛形 |
| 個別タスク詳細（folder-based） | `01-Projects/<Project>/TSK-####-<slug>/task.md` | 新規タスクの基本配置。親タスク本体 |
| チーム内タスク文脈 | `01-Projects/<Project>/TSK-####-<slug>/<team>/tasks.md` | Director が管理するチーム内細分化タスク |
| 個別タスク詳細（legacy） | `01-Projects/<Project>/TSK-####-<slug>.md` | 既存互換。即時移行しない |
| バックログ集約 | `01-Projects/<Project>/<Backlog-Name>.md` | 同種・小粒タスクを内包する例外形式 |
| Runtime queue | `<ITB_STATE_ROOT>/<session>/queue/` | role-agent 間 I/O の runtime state。Vault には置かず、Task Detail へ要約と report path を記録する |

Task Detail の `status` を正とし、Task-Index と Kanban はそこから同期する。
完了タスクを Task-Index から削除しない。Kanban に同タスクを重複配置しない。

## File Layout

| 配置先 | 用途 | 例 |
|---|---|---|
| `00-Inbox&Tasks/` 直下 | 索引・受付・状態ビューなど Vault 共通の運用ファイルのみ | `Task-Gateway.md`、`Task-Index.md`、`Kanban.md` |
| `00-Inbox&Tasks/Templates/` | 再利用テンプレート | `Task-Detail-Template.md` |
| `01-Projects/<Project>/TSK-####-<slug>/task.md` | 新規 Task Detail の親タスク本体 | `01-Projects/AI-Agent-Organization/TSK-1072-team-task-folder-layout/task.md` |
| `01-Projects/<Project>/TSK-####-<slug>/<team>/tasks.md` | Director 管理のチーム内細分化タスク | `01-Projects/AI-Agent-Organization/TSK-1072-team-task-folder-layout/infra/tasks.md` |
| `01-Projects/<Project>/TSK-####-<slug>.md` | 既存互換の単一ファイル Task Detail。即時移行しない | `01-Projects/AI-Agent-Organization/TSK-1001-dispatcher-io-contract.md` |
| `01-Projects/<Project>/<Backlog>.md` | 同種小粒タスクのバックログ束ねノート | `01-Projects/AI-Agent-Organization/Skill-Implementation-Backlog.md` |
| `03-Contexts/Policies/` | 再利用される運用ルール | このノート、`Dispatcher-IO-Contract.md` など |

Task Detail を `00-Inbox&Tasks/` 直下に置かない。
Task Detail を `02-Ideas/` や `03-Contexts/` 直下に置かない（実施ログの正本は常に Project 配下）。

## Runtime Queue Layout

Role-agent の inbox / task payload / report は Task Detail フォルダ配下に作らない。runtime queue は ITB state root 配下に置き、Vault には再利用可能な要約、判断理由、report path、provider evidence だけを記録する。

```text
<ITB_STATE_ROOT>/<session>/queue/
├── enqueue.lock
├── inbox/
│   └── <role_id>.yaml
├── tasks/
│   └── <task_id>/<message_id>.yaml
└── reports/
    └── <role_id>/<task_id>/<report_id>.yaml
```

| Queue Path | Owner | Rule |
|---|---|---|
| `inbox/<role_id>.yaml` | target role | `pending`、`processing`、`done`、`failed` の message 状態を持つ |
| `tasks/<task_id>/<message_id>.yaml` | sender role | 依頼本文、期待出力、handoff notes を保持する |
| `reports/<role_id>/<task_id>/<report_id>.yaml` | target role | 結果、status、provider evidence、error を保持する |
| `enqueue.lock` | ITB | queue write の競合回避に使う |

Vault 側の Task Detail / team `tasks.md` には、runtime queue の中身を全文コピーしない。必要な場合は、対象 report の path、要約、判定、残リスクだけを記録する。

### Folder-Based Task Layout

新規 Task Detail は、親タスク単位のフォルダを基本形にする。
既存の `TSK-####-<slug>.md` は legacy として読み続け、明示的な移行タスクなしに移動しない。

```text
01-Projects/<Project>/TSK-####-<slug>/
├── task.md
├── tech/tasks.md
├── contents/tasks.md
├── business/tasks.md
├── infra/tasks.md
└── gate/tasks.md
```

| Path | Owner | Role |
|---|---|---|
| `TSK-####-<slug>/task.md` | GTC / Dispatcher | 親タスク本体。Task Index / Kanban と同期する正本 |
| `TSK-####-<slug>/<team>/tasks.md` | 各 team director | チーム内の細分化タスク、依存、レビュー、統合ログの正本 |

Director は、チーム内の細かい進捗を親 `task.md` に直接書き込まない。
親タスクの status、Task Index、Kanban に影響する更新は GTC / Dispatcher の責務として扱う。

## Team Task Context

`<team>/tasks.md` は、親 Task Detail にぶら下がるチーム内タスク管理コンテキストであり、Task Index / Kanban には載せない。
各 team task は必ず親 `task.md` または親 `TSK-####` への wikilink を持つ。

### Team Task ID

```text
TT-TSK-####-<team>-NNN
```

| 要素 | 規約 |
|---|---|
| `TT` | Team Task の固定 prefix |
| `TSK-####` | 親タスク ID |
| `<team>` | `tech` / `contents` / `business` / `infra` / `gate` |
| `NNN` | チーム内の3桁連番 |

### Team Tasks Structure

`tasks.md` は最低限、次の項目を持つ。

| Section | 内容 |
|---|---|
| `Parent Task` | 親 `task.md` / `TSK-####` への wikilink |
| `Team Mission` | TPM / Director から渡されたチーム単位の目的 |
| `Team Task Board` | `todo` / `in_progress` / `blocked` / `internal_review` / `done` |
| `Team Tasks` | team task ID、担当、状態、依存、成果物、相互レビュー担当、親タスクリンク |
| `Dependency Map` | blocked by / unlocks |
| `Review Matrix` | 作業担当、相互レビュー、統合レビュー |
| `Integration Log` | Director による統合判断、未解決事項 |
| `Completion Report Summary` | Director structured completion signal として TPM の `team-completion-check` へ渡す担当作業、レビュー証跡、Vault 更新、残リスクの要約 |
| `Vault Updates` | チーム内で更新・参照した Vault 記録 |

### Escalation To Parent Flow

次のいずれかに該当する team task は、Director が親 `task.md` へ直接追記せず、GTC / Dispatcher に正式 Task 化または親タスク更新を戻す。

| 条件 |
|---|
| 親 Task の Scope を超える |
| 複数日に渡る独立した実施ログが必要 |
| 別チームの review line が必要 |
| 人間承認が必要 |
| 破壊的操作、権限変更、方針転換を含む |
| Task Index / Kanban の status 変更が必要 |


## File Naming

### Task Detail

新規タスクの基本形は folder-based とする。

```text
TSK-####-<slug>/task.md
```

既存互換として、すでに存在する単一ファイル形式も読み続ける。

```text
TSK-####-<slug>.md
```

| 要素 | 規約 |
|---|---|
| `TSK-####` | Dispatcher が採番した4桁連番のタスクID |
| `<slug>` | kebab-case、英小文字、半角数字、ハイフン区切り |
| slug 語数目安 | 3〜6 語。長すぎる場合は要点を残して短縮 |
| 禁止 | 全角文字、スペース、アンダースコア、大文字、日付（必要なら slug 末尾に `-YYYY-MM-DD`） |

例: `TSK-1072-team-task-folder-layout/task.md`、legacy 例: `TSK-1053-gate-io-contract.md`

### Backlog / Project Note

| 種別 | 命名 | 例 |
|---|---|---|
| バックログ束ね | `<Topic>-Backlog.md` または `<Topic>-Implementation-Backlog.md` | `Skill-Implementation-Backlog.md` |
| プロジェクト Index | `README.md` または `<Project>-Index.md` | `01-Projects/README.md` |
| 調査・参照ノート | `<Topic>-<context>.md`（task_id を持たない） | `World-ID-research-2026-05-14.md` |

### Policy / Context Note

| 種別 | 命名 | 配置 |
|---|---|---|
| Policy | PascalCase + ハイフン区切り | `03-Contexts/Policies/Task-File-Conventions.md` |
| Report | `<Topic>-YYYY-MM-DD.md` | `03-Contexts/Reports/` |
| Template | `<Topic>-Template.md` | `00-Inbox&Tasks/Templates/` または `03-Contexts/Templates/` |

## Task Granularity

タスクを **個別ノート化するか、バックログノート内に束ねるか** を次の基準で判定する。

### 個別ノート化（既定）

次のいずれかに該当する場合、`TSK-####-<slug>/task.md` として独立した親タスクフォルダを作る。

| 条件 |
|---|
| 成果物の種類が独自（Policy、SKILL、Code、Report のいずれかで他タスクと異なる） |
| 実施ログ・判断履歴・レビュー結果が複数日に渡る見込み |
| 担当チームが複数にまたがる、またはレビュー線が他と異なる |
| 人間承認が必要、または破壊的操作を含む |
| 想定本文（Execution Log + Reviews + Deliverables）が10行を超える見込み |

### バックログ束ね（例外）

次の **すべて** を満たす場合のみ、既存または新規のバックログノート内にセクション `## TSK-#### Title` として束ねる。

| 条件（AND） |
|---|
| 同種・同テンプレートで処理できる（例: 各スキルの SKILL.md 実装） |
| 1タスクあたり本文が2〜3行で済む |
| 同 Project に属する |
| 同種タスクが3件以上連続して見込まれる、または1タスクあたりが2〜3行で済む小粒タスク（C: 3件以上ルール OR 小粒ルール） |

束ねた場合、Task-Index と Kanban からは **アンカー付き wikilink** で参照する。

```
[[01-Projects/<Project>/<Backlog>#TSK-#### Title|TSK-#### Title]]
```

### 判断フロー

```
個別ノート化の条件いずれか該当?
  ├─ Yes → 個別ノート
  └─ No → バックログ束ね条件すべて該当?
            ├─ Yes → 既存バックログに追記、無ければ新規作成
            └─ No  → 個別ノート（迷ったら個別を選ぶ）
```

## Project Folder Selection

タスクをどの `01-Projects/<Project>/` フォルダに置くかは次で判定する。

### 既存 Project を使う

次のいずれかに該当する場合、既存 Project フォルダに置く。

| 条件 |
|---|
| 既存 Project の成果物・知識領域と直接関連する |
| 既存 Project の Task と同じ Backlog で管理できる |
| 命名が既存 Project の領域名と一致する |

### 新規 Project フォルダを作る

次を満たす場合のみ、新規 Project フォルダを作る（基準 A: 3件以上関連タスクが見込まれる）。

| 条件 |
|---|
| 関連タスクが3件以上見込まれる |
| 既存のどの Project にも明確に属さない |
| 領域・成果物として独立している |

新規作成時は、Project フォルダ直下に `README.md` を作って Project の目的・スコープ・関連 Task を簡潔に書く。

### 単発タスクの扱い

関連タスクが1〜2件しか見込まれない場合は、最も近い既存 Project に間借りする。
明確に近い既存 Project が無ければ、`01-Projects/Misc/` または同等の汎用フォルダを使う（無ければ作らず、最も近い既存 Project に置く）。

## Task Detail Structure

`Task-Detail-Template.md` を雛形にし、次を満たす。

### Frontmatter（必須）

| Field | Required | 値 |
|---|---|---|
| `type` | Yes | `task-detail` |
| `task_id` | Yes | `TSK-####`。Dispatcher 採番前は `TSK-PENDING-<unit_id>` |
| `main_team` | Yes | `gate` / `tech` / `contents` / `business` / `infra` |
| `assignee` | Yes | 想定担当エージェントID |
| `status` | Yes | `inbox` / `triage` / `ready` / `in_progress` / `domain_review` / `independent_review` / `waiting_human` / `blocked` / `done` / `archived` |
| `source` | Yes | `user-request` / `task-gateway` / `dispatcher-candidate` など |
| `last_updated` | Yes | `YYYY-MM-DD` |
| `requires_human_approval` | When applicable | `true` / `false` |
| `blocked_by` | When applicable | 先行タスクID |

### 本文章立て（必須セクション）

| セクション | 内容 |
|---|---|
| `# TSK-#### Title` | 見出し |
| `## Metadata` | frontmatter と同じ値を表で再掲（人間可読用） |
| `## Original Request` | 元依頼を意味改変せず保存 |
| `## Scope` | `In:` / `Out:` の二分 |
| `## Execution Log` | thin reference。詳細ログ本文は role report に置き、Task Detail には status / 1行 summary / report path / report sha256 を残す |
| `## Reviews` | thin reference。Domain / Independent / Human approval の状態と report path / report sha256 を残す |
| `## Deliverables` | 成果物の wikilink |
| `## Vault Updates` | `Added:` / `Updated:` で更新ファイル列挙 |

### Thin Index Rule

Task Detail は親タスクの索引であり、role report の全文コピー先ではない。
長い実行ログ、レビュー本文、validation output、diff 本文、provider transcript、queue payload は runtime queue または成果物ファイルを正本にし、Task Detail には参照だけを残す。

各 gate section は原則として次の thin table にする。

```markdown
## <Section>

| Field | Value |
|---|---|
| Status | pass / block / ambiguous / quality_ok / needs_rework / pending |
| Summary | 1行要約 |
| Report | [[Vault 内 report への wikilink]] または path |
| Report Path | <queue/report/artifact path> |
| Report SHA256 | <sha256> |
| Updated At | <ISO timestamp> |
| Owner Role | <role_id> |
```

Task Detail へ section を追記・更新する場合は、ITB builder の `task-detail-append` command を使う。
手書きで長文 table や本文を貼り付けない。

```bash
python3 skills/infra-team-bootstrap/scripts/itb_bootstrap_builder.py task-detail-append --runtime <runtime> --state-root <state_root>
```

標準入力 JSON には `task_detail_path`、`section`、`status`、`summary`、`report_path`、`report_sha256`（report が存在する場合は省略可）を含める。
builder は既存 section を置換し、同名 section の重複を作らない。

`ITB_TASK_DETAIL_LINE_CAP` の既定値は 220 行とする。
`pre_execution` / `post_routing` では cap 超過を warning とし、`pre_final_response` では完了 lint として block する。

### 任意セクション

| セクション | 用途 |
|---|---|
| `## Next Actions` | 後続タスクID・優先度 |
| `## Risks` | 残るリスク・未対応範囲 |
| `## References` | 関連ノート・外部資料 |

## Three-View Sync

詳細は [[03-Contexts/Policies/Dispatcher-IO-Contract]] を参照。要点だけ再掲する。

| ルール |
|---|
| Task Detail の `status` を正本とする |
| Task-Index の行は Task Detail の wikilink を含める |
| Kanban の同タスクは状態に対応するセクションへ1回だけ配置（移動のみ、重複禁止） |
| Task-Index から完了タスクを削除しない（履歴保持） |
| バックログ束ねのタスクは、Index と Kanban の両方でアンカー付き wikilink を使う |

## ID Collision Handling

採番衝突を発見した場合の対処。

| 状況 | 対処 |
|---|---|
| 同一 `TSK-####` が複数の Task Detail で使われている | 後発のタスクを停止し、`infra-task-dispatcher` に再採番を依頼する |
| Task Detail と Task-Index で `task_id` が食い違う | Task Detail を正本とし、Index を修正する |
| Dispatcher 採番前に `TSK-PENDING-<unit_id>` で起票したノートが残る | Dispatcher が採番した時点で `TSK-####` にリネーム、関連 wikilink を更新 |

衝突に気づいたエージェントは、勝手に再採番せず、Dispatcher にエスカレーションする。
既知の衝突は [[01-Projects/AI-Agent-Organization/]] 配下の専用タスクで追跡する。

## Validation Checklist

タスクノートを作成・更新したエージェントは、少なくとも次を満たす。

| Check | Required |
|---|---|
| 新規 Task Detail は `TSK-####-<slug>/task.md` 規約に合っている | Yes |
| 既存互換の単一ファイル形式は移行タスクなしに移動していない | Yes |
| 配置先が `01-Projects/<Project>/` 配下である | Yes |
| team task は `<team>/tasks.md` に記録され、親 `task.md` / `TSK-####` へリンクしている | When applicable |
| frontmatter の必須項目がすべて埋まっている | Yes |
| 本文の必須セクションがすべてある | Yes |
| Task-Index に行が追加されている | Yes |
| Kanban の status 対応セクションに wikilink が追加されている | Yes |
| 個別ノート / バックログ束ねの判断が本ポリシーに沿っている | Yes |
| ID 衝突がない | Yes |
| gate section は thin reference（status / summary / report path / report sha256）になっている | Yes |
| `pre_final_response` 時点で Task Detail が line cap を超えていない | Yes |

## Related Notes

- [[03-Contexts/Policies/AI-Organization]]
- [[03-Contexts/Policies/Dispatcher-IO-Contract]]
- [[03-Contexts/Policies/Gate-IO-Contract]]
- [[00-Inbox&Tasks/README]]
- [[00-Inbox&Tasks/Templates/Task-Detail-Template]]
- [[01-Projects/README]]
