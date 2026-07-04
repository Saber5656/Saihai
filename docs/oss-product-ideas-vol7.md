# OSS プロダクト提案書 vol.7 — スター王者の実測分析と 50 案

**きっかけ:** 「スター数トップの OSS をもっと調べて、傾向分析して、もっと面白い
プロダクトを考えて」という指示に対し、2026-07-04 時点の GitHub 全体ランキングを
実測し、型を抽出してから発想した。

---

## 0. 実測: スター数トップ 30(2026-07-04, EvanLi/Github-Ranking)

| 順 | リポジトリ | ★ | 正体 |
|---|---|---|---|
| 1 | build-your-own-x | 522k | **読む**(再実装ガイド集) |
| 2 | awesome | 481k | **カタログ** |
| 3 | freeCodeCamp | 451k | **教材** |
| 4 | public-apis | 446k | **カタログ**(自動検証つき) |
| 5 | free-programming-books | 391k | **カタログ** |
| 6 | openclaw | 382k | AI アシスタント(現象) |
| 7 | developer-roadmap | 359k | **教材** |
| 8 | system-design-primer | 356k | **教材** |
| 9 | coding-interview-university | 355k | **教材** |
| 10 | awesome-python | 306k | **カタログ** |
| 11 | awesome-selfhosted | 303k | **カタログ** |
| 12 | 996.ICU | 276k | 運動 |
| 13 | project-based-learning | 272k | **教材** |
| 14 | react | 246k | プラットフォーム |
| 15 | superpowers | 246k | **エージェント skills 基盤**(現象) |
| 16 | linux | 238k | プラットフォーム |
| 17 | the-book-of-secret-knowledge | 232k | **カタログ** |
| 18 | ECC | 226k | エージェント最適化(現象) |
| 19 | TheAlgorithms/Python | 222k | **実装コレクション** |
| 20 | vue | 210k | プラットフォーム |
| 21 | hermes-agent | 209k | AI エージェント(現象) |
| 22 | computer-science | 206k | **教材** |
| 23 | javascript-algorithms | 196k | **実装コレクション** |
| 24 | tensorflow | 196k | プラットフォーム |
| 25 | n8n | 195k | 万人ツール |
| 26 | claw-code | 195k | AI エージェント(現象) |
| 27 | ohmyzsh | 188k | 万人ツール(設定資産) |
| 28 | **andrej-karpathy-skills** | **187k** | **単一の CLAUDE.md ファイル** |
| 29 | vscode | 187k | プラットフォーム |
| 30 | AutoGPT | 185k | AI エージェント |

## 1. 傾向分析

### 型の集計(トップ 30 中)

| 型 | 件数 | 代表 | ソロ開発者の到達可能性 |
|---|---|---|---|
| A. 読む教材・カリキュラム | 7 | build-your-own-x 522k | **◎ 最も再現可能** |
| B. カタログ(自動検証つきが上位) | 6 | public-apis 446k | ◎ |
| C. AI エージェント現象 | 6 | openclaw 382k | ○(波は本物) |
| D. プラットフォーム | 6 | react 246k | ✕(組織級) |
| E. 実装コレクション | 2 | TheAlgorithms 222k | ◎ |
| F. 万人ツール・設定資産 | 2 | ohmyzsh 188k | ○ |
| G. 運動 | 1 | 996.ICU 276k | △(狙えない) |

### 3 つの発見

1. **GitHub の頂点は「実行しない OSS」**。トップ 5 は全部「読む・参照する」リポジトリ。
   これらは (a) 制作コストが実装系より低い、(b) PR 貢献の敷居が最低
   (=**改廃が最も賑わう**)、(c) 古びにくい。
2. **単一ファイルが 187k★ の時代**。karpathy-skills は 1 個の CLAUDE.md。
   superpowers(skills 基盤)も 246k。**「エージェント設定・知識の資産化」が
   史上最速のスター獲得ジャンル**になっている(検索でも「数ヶ月で 156k」の報告)。
   → あなたの skillet(P03)・skillsmith(P77)・agentci(P07)への投資は正しい。
3. **バックログ 91 件のギャップ**: ほぼ全てが「実行する型」。
   王者ゾーンの「読む型」「自動検証カタログ型」「単一ファイル型」が**ほぼ空白**
   (learn-mcp P89・buildyourown P22 のみ)。ここが未採掘の鉱脈。

### vol.7 の生成規則

検証済みの型(A/B/E/F+C の波)× あなたの 5 軸(CLI/AI/ゲーム/遊び心/MCP)。
各案に「どの実測フォーミュラの再現か」を明記する。
「読む型」でも中身は決定論の検証(CI・実測・自動テスト)で担保し、
プロンプトラッパー禁止の哲学は維持する。

---

## I. 読む教材型 × 5 軸(12)— build-your-own-x 522k / system-design-primer 356k の型

| # | 名前案 | ピッチ | 再現する型 |
|---|---|---|---|
| 1 | agent-design-primer | エージェント設計の体系教科書(パターン・アンチパターン・演習・面接問題)。図とコード付き | system-design-primer(356k)のエージェント版。**王者の型×最熱ドメイン** |
| 2 | you-dont-need-an-llm | 「それ LLM 要らなくない?」— 決定論で解けるタスクのパターン集+速度/コスト実測ベンチ | you-dont-need-* 系譜。**あなたの採用哲学そのものを旗にする** |
| 3 | debugging-primer | デバッグ技術の体系教科書(bisect・rr・ログ設計・事例演習)。正典が存在しない普遍スキル | primer 型の空白地帯。debugdungeon(P80)が演習場になる |
| 4 | the-terminal-book | 端末のしくみ(PTY・エスケープシーケンス・シェル)を動くデモで学ぶ教科書 | craftinginterpreters 型の愛され方。Dev CLI 星系の理論書 |
| 5 | cli-design-patterns | CLI UX パターン百科(GIF+多言語実装コード)。help の書き方から進捗表示まで | 原則集(clig.dev)は既存、**実装パターン+実例**が空席 |
| 6 | context-engineering-primer | コンテキスト工学の体系(実測データつき: 何を入れると何が起きるか) | 2026 年のホットワードに正典を置く |
| 7 | mcp-the-hard-parts | MCP の落とし穴・実戦知識集(認証・スキーマ肥満・バージョニング) | learn-mcp(P89)の読み物対。星系の理論書 |
| 8 | agent-incident-database | AI エージェント事故の匿名 postmortem 集(原因分類・再発防止) | 参照価値+報道性。事故のたびに流入が再燃する |
| 9 | terminal-trickshots | シェル芸・ワンライナー技の GIF 図鑑(全て検証スクリプト付き) | 「動く awesome」。投稿文化=改廃の賑わい |
| 10 | game-feel-cookbook | ゲームの「気持ちよさ」レシピ 100(埋込デモで体感できる) | 教材型×ゲーム軸。ジャム民の定番参照になる |
| 11 | local-llm-primer | ローカル LLM 運用の体系教科書(量子化・VRAM・KV キャッシュを演習で) | roadmap 型×ローカル AI の波 |
| 12 | hoshiatsume | 「バズる OSS の作り方」日本語プレイブック — **本プロジェクトの方法論自体を公開** | メタな一手。vol.1〜7 の知見が初期コンテンツになる |

## II. 自動検証カタログ型(8)— public-apis 446k / awesome-selfhosted 303k の型

| # | 名前案 | ピッチ | 再現する型 |
|---|---|---|---|
| 13 | public-mcps | 公開 MCP サーバーのカタログ — **全掲載サーバーを CI で自動ヘルスチェック・適合検証**(create-mcp P86 のバッジ連携) | public-apis(446k)の MCP 版。静的リスト(awesome-mcp-servers)に対し検証で勝つ |
| 14 | llm-price-watch | 全プロバイダの価格・コンテキスト・性能の毎日自動更新表+推移チャート | 自動更新カタログ。tokenmeter(P06)のデータ供給源 |
| 15 | can-i-run-this-llm | ハードウェア別「どのモデルが動く?」対応表+診断 CLI(実測報告を PR で収集) | カタログ×診断。報告=貢献の設計 |
| 16 | skill-registry | 検証済み Agent Skills のカタログ(skill-lint 通過バッジ・動作 CI つき) | superpowers 246k が需要を証明。skillet(P03)のデータ側 |
| 17 | agent-model-matrix | エージェント CLI × モデル × 機能の「何がどこで動く?」自動テスト対応表 | caniuse の agent 版。互換性は永遠の関心事 |
| 18 | awesome-terminal-toys | ターミナル玩具・演出の**動く**カタログ(全部 GIF・即試せる) | keycat/terrarium 星系の集客装置を兼ねる |
| 19 | game-asset-index | ライセンス安全なゲーム素材の横断カタログ(ライセンス自動検証つき) | ゲーム開発の恒常痛×検証カタログ |
| 20 | jp-dev-catalog | 日本語開発資源(形態素辞書・住所・祝日・法人番号)の生きたカタログ | awesome-jp の空白。国内の定番参照を取る |

## III. 単一ファイル・設定資産型(6)— karpathy-skills 187k / ohmyzsh 188k の型

| # | 名前案 | ピッチ | 再現する型 |
|---|---|---|---|
| 21 | the-claude-md | **CI でテストされ続ける** CLAUDE.md / AGENTS.md ベースライン(agentci P07 で回帰検証つき) | karpathy-skills(187k)の型+「テスト済み」の差別化。あなたにしか出せない信頼性 |
| 22 | the-mcp-json | 用途別の検証済み MCP 構成プリセット集(mcp-doctor P02 で自動診断済み) | 設定資産型×MCP。導入 10 秒 |
| 23 | dotfiles-zero | 「最小で最強」の検証済み dotfiles ベースライン(起動 ms・機能を実測表で担保) | ohmyzsh の対極(軽量・計測主義)。shellcheckup(P28)連携 |
| 24 | gitconfig-goldmine | 効果を GIF で示す検証済み gitconfig / alias 集 | 設定資産型×git。1 項目=1 PR の貢献設計 |
| 25 | prompt-partials | 再利用可能なプロンプト部品の標準(組合せ構文+回帰テスト) | 設定資産型。agentci でテストされる「生きた部品」 |
| 26 | editor-zero | エディタの「ゼロから最強」検証済み最小構成(起動速度・機能の実測比較つき) | kickstart.nvim 系の型を実測主義+多エディタで |

## IV. 実装コレクション型(6)— TheAlgorithms 222k / javascript-algorithms 196k の型

| # | 名前案 | ピッチ | 再現する型 |
|---|---|---|---|
| 27 | the-agent-algorithms | エージェント中核アルゴリズム(ReAct・ツール選択・メモリ・計画)の依存ゼロ最小実装を全言語で | TheAlgorithms(222k)の型×最熱ドメイン。**教科書コードの正典を取る** |
| 28 | game-algorithms | ゲーム古典アルゴリズム(A*・ロールバック・手続き生成)の動くデモ付き最小実装集 | 実装コレクション×ゲーム軸 |
| 29 | protocol-zoo | プロトコルの最小実装動物園(HTTP・DNS・MCP を各 100 行で) | build-your-own-x 隣接。learn-mcp と接続 |
| 30 | shaders-101 | シェーダー技法の最小実装集(1 技法 1 ファイル、ブラウザで即動く) | 実装コレクション×ビジュアル。デモが SNS を回る |
| 31 | regex-engines | 正規表現エンジンの実装比較コレクション(バックトラック vs NFA、可視化つき) | regexdojo(P71)の理論側 |
| 32 | procgen-gallery | 手続き生成の技法図鑑(地形・ダンジョン・名前生成、パラメータをその場で弄れる) | 実装コレクション×遊び。ゲームジャム民の参照定番へ |

## V. デスクトップ演出 — keycat(P91)星系(8)— あなたが自案で示した新方向

| # | 名前案 | ピッチ | 再現する型 |
|---|---|---|---|
| 33 | deskmate | デスクトップペット基盤の現代版(Wayland/mac 対応、スキン投稿文化)— Shimeji 後継の空席 | ohmyzsh 型(基盤+コミュニティ資産)。keycat のエンジンを兼ねる |
| 34 | keyfx | キー入力エフェクト・キーキャスト統合(コンボ表示・OBS 対応。keycat と同エンジン) | 配信文化=自己拡散。screenkey 系の老朽後継 |
| 35 | wobbly | ウィンドウ操作に物理演出(閉じると落下・最小化で吸い込み)— Compiz の魂の現代版 | ノスタルジー×演出。デモ動画が勝手に回る |
| 36 | livewall | 動く壁紙のクロスプラットフォーム OSS(Wallpaper Engine 代替。Linux/mac が空白) | 万人ツール型。壁紙=コミュニティ投稿資産 |
| 37 | deskcrowd | 画面の隅に小さな観客が住み着き、作業を応援(テスト成功で歓声)— keycat の観客席 | keycat 星系の拡張。tokenhp(P74)と併用可 |
| 38 | notifybird | 通知を鳥が咥えて届ける演出レイヤー(通知システムのスキン化) | 演出スイート。小さく可愛い枠 |
| 39 | cursorpets | カーソルを追いかける古典 oneko の現代版(deskmate 最初のプリセット) | ネタ×郷愁。deskmate への導線 |
| 40 | defrag-zen | 懐かしのデフラグ画面を「待ち時間の禅」に(あらゆる進捗表示の演出スキン) | ノスタルジー小品。buildbreak 不採用の教訓から「眺めるだけ」に軽量化 |

## VI. 万人ツール+視覚体感型(10)— thefuck / excalidraw の型

| # | 名前案 | ピッチ | 再現する型 |
|---|---|---|---|
| 41 | pastecheck | ターミナル貼り付け事故ガード(改行入り・不可視文字・homoglyph を実行前に停止) | 万人の恐怖×極小ツール。undo(P08)星系 |
| 42 | whichis | 「今どの node/python が効いてる? なぜ?」— version manager 地獄の交通整理診断 | 万人の毎週の痛み。whydid(P09)兄弟 |
| 43 | waitwhat | 長時間コマンドの残り時間予測(過去実行の統計から「あと 3 分」) | 万人ツール。地味に誰も持っていない |
| 44 | pipeflow | 実行中の Unix パイプラインをリアルタイム可視化(流量・詰まり・落ち物が見える) | excalidraw 型(見える化の魔法)。grepfactory(vol.6)の実用逆輸入 |
| 45 | dbpulse | 生きた ER 図 — クエリ流量が血流のように見える「データベースの心電図」 | 視覚体感型。dbbranch(P15)と同星系 |
| 46 | regexscope | 正規表現のマッチ過程をアニメで見る(バックトラック可視化・ReDoS 発見) | 視覚体感型。regexdojo(P71)の実用側 |
| 47 | commitcity | GitHub 活動が 3D 都市になる(自分の 1 年が街並みに) | スクショ駆動の個人化。gitquest(P79)と相互送客 |
| 48 | prsplit | 巨大 PR をコミットグラフ解析で論理単位に自動分割 | 万人ツール(AI が巨大 PR を量産する時代) |
| 49 | kusa-art | Contribution graph の草アート生成+ギャラリー(「草を生やす」文化の道具) | ネタ×日本語圏ミーム。小さく確実に愛される |
| 50 | 1brc-kit | 「10 億行チャレンジ」型パフォーマンス祭りの常設開催キット+殿堂(検証ハーネス込み) | 1BRC(2024 年の実証済みバズ)の再現可能フォーミュラ化 |

---

## イチ推し 10(実測の型 × あなたの軸)

| 順 | # | 名前 | 理由 |
|---|---|---|---|
| 1 | 1 | agent-design-primer | 王者の型(primer)×最熱ドメイン。522k/356k が需要の証明 |
| 2 | 13 | public-mcps | public-apis(446k)の MCP 版を自動検証付きで。P86 とバッジで連携 |
| 3 | 27 | the-agent-algorithms | TheAlgorithms(222k)の型で「エージェントの教科書コード」正典を取る |
| 4 | 21 | the-claude-md | 単一ファイル 187k の時代に「CI でテスト済み」という唯一の差別化 |
| 5 | 2 | you-dont-need-an-llm | あなたの哲学の旗。挑発的で拡散し、実測ベンチが骨になる |
| 6 | 33 | deskmate | keycat(P91)をエンジン共有で星系化。Shimeji 後継は世界中で待たれている |
| 7 | 16 | skill-registry | superpowers 246k が証明した skills 経済圏のカタログ側 |
| 8 | 44 | pipeflow | 「見えないものが見える」は excalidraw 型の魔法。デモ 10 秒 |
| 9 | 35 | wobbly | ノスタルジー×演出は説明不要で拡散する |
| 10 | 50 | 1brc-kit | 実証済みの祭りを誰でも開催可能にする=賑わいの製造装置 |

## 出典

- https://github.com/EvanLi/Github-Ranking (Top-100-stars, 2026-07-04 参照)
- https://blog.bytebytego.com/p/top-ai-github-repositories-in-2026
- https://www.star-history.com/
- https://grokipedia.com/page/List_of_most-starred_GitHub_repositories
