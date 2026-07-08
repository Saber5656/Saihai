# Sahai

Sahai（formerly Agent-Teams-Viewer）は、ITB Organization Instance
（チャットセッションごとに扱う AI エージェント組織）の
状態、AI 組織運用の設定、Policy、Role 定義を管理するローカルダッシュボード。

Rename note: pre-release documentation may still mention Agent-Teams-Viewer
when referring to historical task records or compatibility aliases.

![dark theme dashboard]

## 機能

| 機能 | 説明 |
|---|---|
| セッション切替 | ヘッダのプルダウンでチャットセッション（= Organization Instance）を切り替え、チャット名称・org id・live/offline を表示する |
| タスク状態表示 | execution context、active task、queue report、provider evidence をもとに現在の進行状態を表示する |
| ロール詳細表示 | エージェントカードから inbox message、report、role metadata、provider evidence を確認する |
| 組織構造表示 | Gate / Engineering / Contents / Business / Infrastructure のチーム別グルーピングを表示する |
| 組織設定表示 | `organization/settings.json` を読み、組織運用 state、fast/strict mode、Hook observer 方針を表示する |
| Task mode 判定 | `/api/decide` と `scripts/configure_organization.py` で prompt を fast / strict / maintenance に判定する |

## 起動

```sh
python3 server.py            # http://127.0.0.1:8765/
python3 server.py --port 8799
```

依存は Python 3.9+ のみ（pip install 不要）。127.0.0.1 にのみ bind する。

## データソース（正本）

| ソース | 用途 |
|---|---|
| `<ITB_STATE_ROOT>/<session_id>/active-execution-context.json` | session、runtime、cwd、started_at、session-local pointer |
| `execution_context.json` | hard-block 判定と final gate の typed state 正本 |
| `<ITB_STATE_ROOT>/<session_id>/active-task.json` | 現在の Task ID / flow_phase |
| `<ITB_STATE_ROOT>/<session_id>/queue/inbox/*.yaml` | role 宛 message の pending / processing 状態 |
| `<ITB_STATE_ROOT>/<session_id>/queue/reports/**/*.yaml` | role 実行結果と provider evidence |
| `~/.claude/projects/*/<session_id>.jsonl` | チャット名称の best-effort 抽出（summary / 最初のユーザープロンプト） |
| `~/.codex/state/itb/` | Codex 側 state（存在すれば同様に読む） |
| `organization/settings.json` | 組織運用 enabled / disabled / maintenance、fast / strict、Hook observer 方針 |
| `organization/policies/*.md` | 組織運用 Policy のミラー正本 |
| `organization/roles/<role>/skill.md` | Team Role 定義のミラー正本（既存 skill は削除せず互換保持） |
| `organization/runtime/*` | role registry / model registry / team config の runtime 参照 |
| `organization/runtime/workflows/` | Orchestrator P0 workflow contract、schema、deterministic selector、initial template |

## 稼働判定（status）

| status | 条件 | 表示 |
|---|---|---|
| `working` | queue report または provider evidence が processing / invoked | 黄色枠 + パルス |
| `processing` | queue inbox に `processing` message | 黄色破線枠 |
| `pending` | queue inbox に `pending` message | 黄色ドット |
| `ready` | metadata と dispatch surface が利用可能 | 灰色枠 |
| `deferred` | lazy / on-call role で現タスク対象外 | 低コントラスト |
| `offline` | session metadata または context pointer が存在しない | 赤系淡色 |

判定は typed state と queue/report evidence の突合で行う。Hook は observer /
advisory として扱い、runtime の進行や role dispatch を Hook から開始しない。

## 安全性

- 完全読み取り専用のローカルビューアとして動作する。
- bind は `127.0.0.1` 固定。認証なしのためリモート公開しないこと。
- 表示対象には作業中の指示や provider evidence が含まれ得る。画面共有時は注意。
- live の `~/.claude` / `~/.codex` 設定変更は、明示承認なしに行わない。

## API

| Endpoint | 説明 |
|---|---|
| `GET /api/sessions` | 監視可能なセッション一覧（live 優先・作成日時降順） |
| `GET /api/org?session=<id>` | チーム別エージェント稼働状況 |
| `GET /api/role?session=<id>&role=<role_id>` | 対象エージェントの inbox / report / metadata / provider evidence |
| `GET /api/config` | 組織運用設定、role count、policy count、policy index |
| `GET /api/decide?prompt=<text>` | prompt を fast / strict / maintenance に判定 |

## 組織設定 CLI

```sh
python3 scripts/configure_organization.py status
python3 scripts/configure_organization.py classify --prompt "最近の天気予報を調べる"
AGENT_ORG_MAINTENANCE=1 python3 scripts/configure_organization.py classify --prompt "Hookを直す"
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

全ての作業は task record を持つ。`fast` は task 化を省略する mode ではなく、
ごく簡単な作業を main agent が軽量 task record と Vault 記録で処理する mode。
`strict` は role dispatch / review / final evidence を要求する通常 mode。

## Sahai CLI

`scripts/saihai.py` は operator 向けの deterministic orchestrator CLI である。
`frontdoor` と `workflow` を分け、提案・承認と workflow-run 操作を同じ
namespace に混ぜない。

```sh
python3 scripts/saihai.py frontdoor --state-root /tmp/frontdoor-state propose \
  --task-id TSK-example \
  --request-id req-example \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'

python3 scripts/saihai.py frontdoor --state-root /tmp/frontdoor-state status \
  --request-id req-example

python3 scripts/saihai.py frontdoor --state-root /tmp/frontdoor-state approve \
  --request-id req-example \
  --nonce <approval.human_action_id>

python3 scripts/saihai.py workflow --state-root /tmp/frontdoor-state create-run \
  --request-id req-example

python3 scripts/saihai.py workflow --state-root /tmp/frontdoor-state drain \
  --run-id <run_id>

python3 scripts/saihai.py workflow --state-root /tmp/frontdoor-state validate-report \
  --run-id <run_id>
```

`frontdoor propose` は approved artifact や workflow run を作らない。
`frontdoor approve` は `--nonce` で明示確認を要求する。`workflow` commands は
approved activation artifact や run id を入力に取り、raw prompt / prose を
authority として読まない。

`scripts/configure_organization.py workflow-frontdoor ...` は skills / automation
向けの互換 facade として維持する。`saihai` と facade は同じ
`frontdoor_orchestrator.py` functions を呼ぶ。

## Orchestrator P0 Workflow Contracts

`organization/runtime/workflows/` は typed agent orchestrator の P0 contract
正本である。通常 prompt は draft / proposed までで orchestration を開始せず、
`/orchestrator-start` などの明示 activation だけが bounded scope 内で approved
envelope を作れる。P0 は schema、`single_step_external_review` template、
deterministic selector、unit/static tests までを提供し、provider runner、tmux
worker、daemon、Viewer UI は実装しない。

## ライセンス

MIT License（[LICENSE](LICENSE) を参照）。
