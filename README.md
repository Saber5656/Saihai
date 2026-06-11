# Agent-Teams-Viewer

ITB Organization Instance（チャットセッションごとに起動する AI エージェント組織）の
稼働状況を可視化する、読み取り専用のローカルダッシュボード。

![dark theme dashboard]

## 機能

| 機能 | 説明 |
|---|---|
| セッション切替 | ヘッダのプルダウンでチャットセッション（= Organization Instance）を切り替え。チャット名称・org id・live/offline を表示 |
| 稼働中ハイライト | 作業中（tmux pane に直近出力あり）のエージェントカードに **黄色の枠** + 発光パルスを付与。黒ベース UI |
| ペインビューア | エージェントカードをクリックすると右ドロワーに tmux pane の内容（作業中の画面そのもの）、inbox メッセージ、roster メタデータを表示 |
| 組織構造表示 | Gate / Engineering / Contents / Business / Infrastructure のチーム別グルーピング（roster.json の `team` を正本とする） |

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

## ライセンス

MIT License（[LICENSE](LICENSE) を参照）。
