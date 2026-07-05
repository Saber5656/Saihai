#!/usr/bin/env bash
# Creates all repos from docs/oss-product-backlog.md under the authenticated
# GitHub account. Run this locally/wherever `gh` is logged into the target
# account — Claude Code's GitHub App integration cannot create personal
# repos (POST /user/repos returns 403 "Resource not accessible by
# integration" for App installation tokens), so this has to run with your
# own `gh auth login` session instead.
#
# Usage:
#   gh auth status              # confirm you're logged in as the right account
#   ./scripts/create-oss-repos.sh
#
# Idempotent: already-existing repos are skipped, so it's safe to re-run
# after a partial failure (e.g. GitHub's secondary rate limit on repo
# creation, roughly 50-100/hour on many accounts).
#
# NOTE: all names are tentative (see docs/oss-product-backlog.md). This
# script does not check npm/PyPI/crates namespace availability — do that
# before you start publishing packages under these names.

set -uo pipefail

REPOS=(
  # --- Agent Ops ---
  "mcp-doctor|MCP設定の健康診断ツール。全クライアントのMCP設定を横断スキャンし、壊れたサーバー・重複・セキュリティリスクを診断する"
  "skillet|Agent Skills向けパッケージマネージャ。検索・インストール・バージョン管理・lockfileを備える"
  "blackbox|エージェント実行の記録・リプレイ・共有ツール。プロンプト・ツールコール・diffをタイムラインで検索できる"
  "agentgate|エージェントのツール実行ファイアウォール。allowlist・dry-run・監査ログを備えるローカルポリシーエンジン"
  "tokenmeter|全エージェント・全プロバイダ横断のローカルコストメーター。予算アラート付き"
  "agentci|CLAUDE.md・skills・プロンプトの回帰テストCIハーネス"
  "yourbench|自分の実タスクで複数LLMをブラインドA/B比較する私設リーダーボード"
  "agentarena|複数エージェントに同一タスクを解かせ観戦・比較する競技場"
  "memviz|LLMの文脈ウィンドウの中身と押し出しをリアルタイム可視化するツール"
  "slopwatch|AI量産PR・issueを検知しOSSメンテナのトリアージを守るツール"
  "tokenlens|プロンプトのトークン消費の内訳を可視化し削減案を提示する"
  "tokenhp|トークン予算の残量をHPゲージ風に可視化。消費でダメージ演出、残量僅少で警告"

  # --- Dev CLI ---
  "undo|破壊的シェル操作を自動スナップショットし、undoコマンドで巻き戻す"
  "whydid|直前の失敗コマンドの原因を説明し修正案を提案する"
  "cidebug|落ちたCIをローカルで再現するタイムトラベルデバッガ"
  "peek|cdした瞬間にディレクトリの概要を1行要約する"
  "dryrun|コマンドを砂箱で予行演習し、実行前に変化をdiff表示する"
  "piper|シェルパイプラインの各段を覗けるステップデバッガ"
  "seek|名前・全文・意味のハイブリッドローカルファイル検索"
  "shellcheckup|シェル起動の遅さを診断し原因を特定する"
  "dnslens|ネットワーク接続不良をDNS・証明書・経路まで一発診断する"
  "envdiff|環境スナップショットの比較で「昨日は動いた」を特定する"
  "worklog|shell履歴とgitから日報・standup報告を自動生成する"
  "culprit|どのコミットで壊れたかを全自動bisectで特定する"

  # --- テスト・品質 ---
  "flaky|flakyテストの検出・隔離・統計を言語横断で管理する"
  "testgap|PRで壊れうるのにテストが無い箇所を指摘する"
  "timeback|巻き戻しデバッガrrの人間向けフロントエンド"

  # --- 垂直エージェント ---
  "i18n-agent|新規UI文字列を検知し翻訳してPRを出す、i18n自動化エージェント"
  "onboard|コードベースの対話型ツアーを自動生成するオンボーディングツール"
  "a11y-bot|アクセシビリティ違反を検出し修正PRまで出すボット"
  "reaper|言語横断のデッドコードハンター。確信度付きで自動PRを出す"
  "freshshot|docs内のUIスクリーンショットをE2Eで自動再撮影する"
  "archmap|コードから腐らないアーキテクチャ図を自動生成する"
  "glossary|リポジトリからチーム用語集を自動構築する"

  # --- Web Dev ---
  "dbbranch|gitブランチごとにローカルDBを瞬時に分岐する(ローカルNeon)"

  # --- 知識・ドキュメント ---
  "paperdeck|論文PDFを参照ジャンプ・数式ホバー付きの読書体験に変換する"
  "tablecat|あらゆる表形式を1コマンドで相互変換・結合する"
  "chronicle|git・写真・位置情報を統合した検索可能な個人タイムライン"
  "quotable|読書ハイライトを一元化するローカルReadwise"

  # --- scanmd 星系 ---
  "scanmd|画面・カメラ・ファイルなど、あらゆるテキストをMarkdownに変換する取り込み口"
  "formfill|PDF様式の記入を支援するツール(役所様式対応)"
  "papertrail|家庭の書類を構造化保管し期限をリマインドする"
  "whatsthis|画像をローカルVLMで判定する「これ何?」ボタン"
  "meishi|名刺スキャンからローカル連絡先管理へ"

  # --- 音声・メディア・クリエイティブ ---
  "kotodama|日本語特化のローカル音声入力。口語→文語整形・敬語変換に対応"
  "dubstudio|動画の多言語吹き替えパイプライン(翻訳・声クローン・字幕)"
  "openkaraoke|任意の曲からカラオケ動画を生成する"
  "handsfree|音声だけでコーディングエージェントを運転する"
  "photobook|写真から印刷品質のフォトブックを自動組版する"
  "glyphlab|手書き文字から自分のフォントを作るパイプライン"
  "inkstory|子どもの絵をスキャンして動く絵本にする"
  "stopmotion|スマホ・Webでコマ撮りアニメを制作する"
  "musicmap|再生履歴から音楽の自分史を可視化する"

  # --- 生活・家庭・IoT ---
  "earmark|あとで読む記事を毎朝のTTSポッドキャストに変える"
  "hearthboard|家族の壁タブレット用ハブ(カレンダー・家事・献立)"
  "keepsake|デジタル終活キット(デッドマンスイッチ+暗号化緊急キット)"
  "doorlog|自宅サーバへのアクセスを可愛いUIで通知する"
  "famchat|子ども安全設計の家族専用チャット・掲示板"
  "openintercom|古いスマホを家庭内インターホン網に再生する"
  "tripkit|旅行計画のセルフホストツール"
  "shelfie|蔵書管理(バーコード・貸し借り・積読統計)"
  "medicab|家族の服薬管理・お薬手帳のセルフホスト"
  "kidquest|子どものお手伝いをクエスト化するボード"
  "wakeup|自宅サーバの遠隔起動・電源管理をスマホから行う"
  "iotify|配線不要のカメラ方式で何でも後付けIoT化するキット"

  # --- セキュリティ・プライバシー ---
  "trustlock|依存パッケージ追加前の信用調査ツール"
  "restoredrill|バックアップ復元試験を自動化する定期避難訓練"

  # --- コミュニティ・遊び・学習・地図 ---
  "buildyourown|テスト駆動でRedis・gitなどを自作するオープン教材ハーネス"
  "firstpr|初心者にgood first issueを推薦しPRまで伴走する"
  "partymode|テレビ+スマホの即席パーティゲーム(Jackbox型)"
  "pixelparty|イベント用のr/place型共同キャンバスキット"
  "datastory-kit|SQL殺人事件型の物語調べ学習教材エンジン"
  "regexdojo|正規表現の対戦パズル・ゴルフ場"
  "nengajo|年賀状OSS(宛名印刷・喪中管理・デザインテンプレ)"
  "chronomap|現在地から過去を遡れるタイムトラベル地図アプリ"

  # --- vol.4 採用(Agent Ops / 遊び) ---
  "skillsmith|作業録画(画面・シェル・エディタ)からSKILL.mdを自動生成する"
  "tracegit|エージェントのコミットに推論サマリを残すreasoning付きgit blame"
  "gitquest|リポジトリがRPGダンジョンになる(コミットで経験値・デッドコード討伐)"
  "debugdungeon|わざと壊れた環境を直して脱出するデバッグ脱出ゲーム"
  "terrarium|ターミナルに住む生き物。テストが通ると育つ"
  "musicbattle|自分の音楽ライブラリからイントロクイズを生成して対戦する"
  "8bitme|家族写真をドット絵・レトロゲーム風アバターに変換する"

  # --- vol.5 採用 ---
  "tinkerbox|物理演算の砂場遊び場(水・砂・歯車)"

  # --- vol.6 採用(MCP エコシステム / 遊び心) ---
  "mcplay|MCPサーバーの対話playground(シナリオ録画・再生・共有)"
  "create-mcp|ウィザードでMCPサーバー雛形生成+仕様適合テスト・バッジ発行"
  "mcpsh|Unixパイプ哲学でMCPツールを合成するシェル"
  "mcparcade|AIチャットから遊べるミニゲームMCP集(将棋・人狼・脱出)"
  "learn-mcp|「MCPサーバーを自作する」課題駆動コース"
  "duck|ラバーダック・デバッグ支援(聞いてくれる、相槌だけ)"

  # --- オリジナル(vol.6 タイミングで追加) ---
  "keycat|Enterを叩くと画面下から猫が出てきて一緒に叩いて祝うデスクトップ演出"

  # --- vol.7 採用 ---
  "notifybird|通知を鳥が咥えて届ける演出レイヤー"
  "cursorpets|カーソルを追いかける古典onekoの現代版"
  "pastecheck|ターミナル貼り付け事故ガード(改行・不可視文字・homoglyphを検知)"
)

if ! command -v gh >/dev/null 2>&1; then
  echo "error: GitHub CLI ('gh') not found. Install it first: https://cli.github.com/" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "error: not logged in. Run 'gh auth login' first." >&2
  exit 1
fi

created=0
skipped=0
failed=0

for entry in "${REPOS[@]}"; do
  name="${entry%%|*}"
  desc="${entry#*|}"

  if gh repo view "$name" >/dev/null 2>&1; then
    echo "skip (exists): $name"
    skipped=$((skipped + 1))
    continue
  fi

  echo "create: $name"
  if gh repo create "$name" --public --description "$desc" --add-readme >/dev/null; then
    created=$((created + 1))
  else
    echo "  -> FAILED: $name" >&2
    failed=$((failed + 1))
  fi
  sleep 1
done

echo ""
echo "done: created=$created skipped=$skipped failed=$failed"
if [ "$failed" -gt 0 ]; then
  echo "re-run this script to retry failed/remaining repos (it skips ones that already exist)."
fi
