# Agent-Teams-Viewer

ITB Organization Instance（チャットセッションごとに起動する AI エージェント組織）の
稼働状況と、AI 組織運用の設定・Policy・Role 定義を管理するローカルダッシュボード。

![dark theme dashboard]

## 機能

| 機能 | 説明 |
|---|---|
| セッション切替 | ヘッダのプルダウンでチャットセッション（= Organization Instance）を切り替え。チャット名称・org id・live/offline を表示 |
| 稼働中ハイライト | 作業中（tmux pane に直近出力あり）のエージェントカードに **黄色の枠** + 発光パルスを付与。黒ベース UI |
| ペインビューア | エージェントカードをクリックすると右ドロワーに tmux pane の内容（作業中の画面そのもの）、inbox メッセージ、roster メタデータを表示 |
| 組織構造表示 | Gate / Engineering / Contents / Business / Infrastructure のチーム別グルーピング（roster.json の `team` を正本とする） |
| 組織設定表示 | `organization/settings.json` を読み、組織運用 state、fast/strict mode、Hook observer 方針を表示 |
| Task mode 判定 | `/api/decide` と `scripts/configure_organization.py` で prompt を fast / strict / maintenance に判定 |

## 起動

```sh
python3 server.py            # http://127.0.0.1:8765/
python3 server.py --port 8799
```

依存は Python 3.9+ と tmux のみ（pip install 不要）。127.0.0.1 にのみ bind する。

## データソース（正本）

| ソース | 用途 |
|---|---|
| `~/.claude/hooks/state/itb/<session_id>/bootstrap.json` | session ⇔ organization_instance ⇔ tmux_session の紐付け |
| `~/.claude/hooks/state/itb/<session_id>/roster.json` | 常駐 37 role の team / tmux_target / process・activation status |
| `~/.claude/hooks/state/itb/<session_id>/active-task.json` | 現在の Task ID / flow_phase |
| `~/.claude/hooks/state/itb/<session_id>/queue/inbox/*.yaml` | role 宛 message の pending / processing 状態 |
| `tmux list-panes` / `tmux capture-pane` | window 活動時刻・pane 内容（読み取りのみ） |
| `~/.claude/projects/*/<session_id>.jsonl` | チャット名称の best-effort 抽出（summary / 最初のユーザープロンプト） |
| `~/.codex/state/itb/` | Codex 側 state（存在すれば同様に読む） |
| `organization/settings.json` | 組織運用 enabled / disabled / maintenance、fast / strict、Hook observer 方針 |
| `organization/policies/*.md` | 組織運用 Policy のミラー正本 |
| `organization/roles/*.md` | Team Role 定義のミラー正本（既存 skill は削除せず互換保持） |
| `organization/runtime/*` | role registry / model registry / team config の runtime 参照 |

## 稼働判定（status）

| status | 条件 | 表示 |
|---|---|---|
| `working` | tmux window の最終活動が 20 秒以内 | 黄色枠 + パルス |
| `processing` | queue inbox に `processing` message | 黄色破線枠 |
| `pending` | queue inbox に `pending` message | 黄色ドット |
| `ready` | resident process は起動済みだが無活動 | 灰色枠 |
| `lazy` | lazy_activation で未起動の resident | 低コントラスト |
| `offline` | tmux session / window が存在しない | 赤系淡色 |

判定は tmux 活動 × queue 状態の突合で行い、閾値は `server.py` の
`ACTIVE_WINDOW_SECONDS` で変更できる。

## 安全性

- 完全読み取り専用。tmux へは `list-sessions` / `list-panes` / `capture-pane` のみ使用し、
  `send-keys` / `paste-buffer` は一切呼ばない。
- bind は `127.0.0.1` 固定。認証なしのためリモート公開しないこと。
- pane 内容には作業中の機微情報が含まれ得る。画面共有時は注意。

## API

| Endpoint | 説明 |
|---|---|
| `GET /api/sessions` | 監視可能なセッション一覧（live 優先・作成日時降順） |
| `GET /api/org?session=<id>` | チーム別エージェント稼働状況 |
| `GET /api/pane?session=<id>&role=<role_id>` | 対象エージェントの tmux pane 内容 + inbox |
| `GET /api/config` | 組織運用設定、role count、policy count、policy index |
| `GET /api/decide?prompt=<text>` | prompt を fast / strict / maintenance に判定 |

## 組織設定 CLI

```sh
python3 scripts/configure_organization.py status
python3 scripts/configure_organization.py classify --prompt "最近の天気予報を調べる"
AGENT_ORG_MAINTENANCE=1 python3 scripts/configure_organization.py classify --prompt "Hookを直す"
```

全ての作業は task record を持つ。`fast` は task 化を省略する mode ではなく、
ごく簡単な作業を main agent が軽量 task record と Vault 記録で処理する mode。
`strict` は role dispatch / review / final evidence を要求する通常 mode。

## ライセンス

MIT License（[LICENSE](LICENSE) を参照）。
