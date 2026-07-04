# OSS プロダクト制作バックログ(正式記録)

**記録日:** 2026-07-04
**方針:** ここに載せたプロダクトは、形はどうあれ制作に着手する。
出典は [vol.1](./oss-product-ideas.md) / [vol.2](./oss-product-ideas-vol2.md) / オリジナル案。

## 制作決定リスト(23 件)

| ID | 名称 | 出典 | 一言ピッチ | 束(スイート) | 状態 |
|---|---|---|---|---|---|
| P01 | Saihai 国際化 | vol.1-#1 | コーディングエージェントの管制塔(k9s for AI agents) | Agent Ops | 未着手 |
| P02 | mcp-doctor | vol.1-#2 | MCP 設定の健康診断・セキュリティ Lint | Agent Ops | 未着手 |
| P03 | skillet | vol.1-#3 | Agent Skills のパッケージマネージャ | Agent Ops | 未着手 |
| P04 | blackbox | vol.1-#4 | エージェントのフライトレコーダー(記録・リプレイ・共有) | Agent Ops | 未着手 |
| P05 | agentgate | vol.1-#5 | エージェントのツール実行ファイアウォール | Agent Ops | 未着手 |
| P06 | tokenmeter | vol.1-#6 | 全エージェント・全プロバイダ横断のコスト計測 | Agent Ops | 未着手 |
| P07 | agentci | vol.1-#8 | CLAUDE.md・skills・プロンプトの回帰テスト CI | Agent Ops | 未着手 |
| P08 | undo | vol.2-#1 | シェルに Ctrl+Z(破壊的操作の巻き戻し) | Dev CLI | 未着手 |
| P09 | whydid | vol.2-#2 | 直前の失敗コマンドの検死官 | Dev CLI | 未着手 |
| P10 | cidebug | vol.2-#5 | 落ちた CI をローカルで再現・タイムトラベルデバッグ | Dev CLI | 未着手 |
| P11 | i18n-agent | vol.2-#6 | 翻訳苦役を肩代わりして PR を出す垂直エージェント | 垂直エージェント | 未着手 |
| P12 | onboard | vol.2-#8 | コードベースの対話型ツアー自動生成 | 垂直エージェント | 未着手 |
| P13 | a11y-bot | vol.2-#9 | アクセシビリティ違反を「直す PR」まで出す | 垂直エージェント | 未着手 |
| P14 | reaper | vol.2-#10 | 言語横断デッドコードハンター | 垂直エージェント | 未着手 |
| P15 | dbbranch | vol.2-#12 | git ブランチごとのローカル DB 分岐(ローカル Neon) | Web Dev | 未着手 |
| P16 | kotodama | vol.2-#15 | 日本語特化ローカル音声入力 | 音声・メディア | 未着手 |
| P17 | dubstudio | vol.2-#16 | 動画の多言語吹き替えパイプライン | 音声・メディア | 未着手 |
| P18 | openkaraoke | vol.2-#18 | 任意の曲→カラオケ動画 | 音声・メディア | 未着手 |
| P19 | earmark | vol.2-#19 | 「あとで読む」を毎朝の TTS ポッドキャストに | 生活 | 未着手 |
| P20 | hearthboard | vol.2-#20 | 家族の壁タブレット用ハブ | 生活 | 未着手 |
| P21 | keepsake | vol.2-#21 | デジタル終活(デッドマンスイッチ+暗号化緊急キット) | 生活 | 未着手 |
| P22 | buildyourown | vol.2-#29 | テスト駆動「X を自作する」オープン教材ハーネス | 学習 | 未着手 |
| P23 | scanmd | オリジナル | あらゆるテキストの取り込み口 →「すべてを Markdown に」 | 入力基盤 | 未着手 |

## P23 scanmd — 構想の肉付け

**原案(発案者メモ):** ファイル読み込みは PDF, Word, 画像と拡張子がさまざまで、
ダウンロード→エージェントに貼り付けが面倒。システム起動して画面スキャン、
カメラ起動して読み取り、のようにあらゆる形態のテキストを md に変換したい。

**ピッチ:** 「エージェント時代の万能取り込み口」。どんな形で存在するテキストでも
ワンアクションでクリーンな Markdown になる。

- **入力経路(ここが独自価値):**
  1. ファイル: PDF / Office / 画像 / EPUB / HTML / メール(.eml)
  2. **画面スキャン:** ホットキー→範囲選択→ OCR(ウィンドウ/スクロールキャプチャ対応)
  3. **カメラ:** 書類の自動検出・台形補正・影除去 → OCR(スマホ連携含む)
  4. クリップボード監視: コピーした瞬間に md 化して置き換え(任意)
  5. URL: 本文抽出 → md
- **出力:** クリーンな Markdown(表→md 表、数式→LaTeX、コード→フェンス、
  frontmatter に出典・日時・ハッシュ)。出力先はクリップボード / ファイル / stdout /
  **MCP サーバー経由でエージェントに直接**。
- **配布形態:** CLI + 常駐ホットキーアプリ + MCP サーバーの三形態。
  MCP モードにより「画面のこれ読んで」がエージェントから直接できる
  → P01〜P07 の Agent Ops スイートとの接続点になる。
- **既存との関係(重要):** 変換エンジン単体は markitdown(Microsoft)、
  docling(IBM)、pandoc、tesseract / ローカル VLM が既に強い。
  **scanmd はこれらを下に敷く「キャプチャ UX + ルーティング層」に徹する。**
  既存はどれも「ファイルを渡してもらう」前提で、画面・カメラ・クリップボードという
  取り込み口を持たない。ここが空席。
- **生態系ポテンシャル:** 後続候補の papertrail(家庭書類の構造化保管)、
  kakeibo(レシート OCR)、meishi(名刺取り込み)は scanmd を土台に作れる
  (vol.3 参照)。scanmd はプロダクトであると同時にプラットフォームになりうる。

## 束(スイート)戦略の再掲

- **Agent Ops(P01–P07):** transcript/state 読み取りの共通 adapter 層と、
  設定探索の共通層をコアライブラリ化。blackbox の記録形式を agentci が再利用。
- **Dev CLI(P08–P10):** 「事故っても戻せる(undo)・原因が分かる(whydid)・
  CI も手元で解ける(cidebug)」の三部作として README を相互リンク。
- **垂直エージェント(P11–P14):** 「苦役を肩代わりして PR を出す」共通ランタイム
  (リポジトリ監視 → 差分生成 → PR 作成 → レビュー UI)を 1 つ作って 4 製品で共有。
- **音声・メディア(P16–P18):** 音声認識・整形・合成のローカルモデル基盤を共有。
- **入力基盤(P23):** scanmd は全スイートの入力口。最優先着手候補。

## 推奨着手ウェーブ(状態更新はこの表で行う)

| Wave | 対象 | ねらい |
|---|---|---|
| 1 | P02 mcp-doctor / P06 tokenmeter / P09 whydid | 1〜2 週で出せる小粒を連発し「毎月何か出す人」の認知を作る |
| 2 | P01 Saihai 国際化 / P23 scanmd | 看板(管制塔)と入力口(プラットフォーム)を立てる |
| 3 | P04 blackbox → P07 agentci / P03 skillet / P05 agentgate | Agent Ops スイート完成。blackbox の記録形式を agentci に流用 |
| 4 | P11 i18n-agent / P14 reaper / P12 onboard / P13 a11y-bot | 垂直エージェント共通ランタイムの上に 4 連発 |
| 5 | P08 undo / P10 cidebug / P15 dbbranch | Dev CLI の大物 |
| 6 | P16–P22(kotodama, dubstudio, openkaraoke, earmark, hearthboard, keepsake, buildyourown) | 生活・メディア・学習系。ここは情熱駆動で順不同 |
