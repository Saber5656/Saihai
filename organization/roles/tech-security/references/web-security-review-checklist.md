# Web Security Review Checklist

This reference preserves the deep Web security review capability formerly provided by the standalone predecessor skill.
Load it when the task includes a Web application, API, authentication/session handling, browser security, framework-specific attack surface, or an explicit deep security review.

## Contents

- [1. IPA 11脆弱性チェックリスト](#1-ipa-11脆弱性チェックリスト)
- [2. サイト全体の安全性向上](#2-サイト全体の安全性向上ipa-第2章準拠)
- [3. モダンWeb脅威](#3-ipa未掲載のモダンweb脅威)
- [4. セキュリティレビュー実施手順](#4-セキュリティレビュー実施手順)
- [5. フレームワーク別クイックリファレンス](#5-フレームワーク別クイックリファレンス)
- [6. CWE対応表](#6-cwe-対応表クイックリファレンス)
- [7. 参照ガイドライン](#7-参照ガイドライン)

> [!important]
> このチェックリストは防御的レビュー用であり、ペネトレーションテストの代替ではない。

## 1. IPA 11脆弱性チェックリスト

IPA「安全なウェブサイトの作り方」に準拠した11種の脆弱性。各項目に **根本的解決** と **保険的対策** を示す。

### 1.1 SQL インジェクション (CWE-89)

**脅威**: 非公開情報の閲覧、データ改ざん・消去、認証回避、OSコマンド実行

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | SQL文の組み立てにプレースホルダ（バインド変数）を使用する |
| 根本的解決 | プレースホルダが使用できない場合、SQL文を構成する全要素にエスケープ処理を行う |
| 保険的対策 | DBアカウントに最小限の権限を設定する |
| 保険的対策 | エラーメッセージにDB情報を含めない |

**モダンフレームワークでの対応**:
- ORM（Prisma, Drizzle, TypeORM等）を使用すれば基本的にプレースホルダが自動適用される
- Prismaでは通常のmodel APIまたはTypedSQLを優先し、raw queryが必要なら `$queryRaw` / `$executeRaw` のtagged template、またはtrusted query textだけで構成した `Prisma.sql` とbind valueを使用する
- `$queryRawUnsafe` / `$executeRawUnsafe` は原則禁止とする。API名だけで脆弱性を断定せず、untrusted string interpolation、文字列連結、動的identifier / keyword、`Prisma.raw` や偽装template objectの誤用を危険条件として確認する
- `Unsafe` APIでもbind parameterは利用できるが、review policyとして安全なAPIへの置換を優先し、query textとidentifierが完全にtrustedであることを別途立証する

### 1.2 OS コマンド・インジェクション (CWE-78)

**脅威**: サーバ上の任意コマンド実行、情報漏洩、システム乗っ取り

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | シェルを起動できる言語機能の利用を避ける |
| 根本的解決 | やむを得ず使用する場合、引数を構成する全変数をチェックし許可された処理のみ実行 |

**モダンフレームワークでの対応**:
- Node.js の `child_process.exec` は使用禁止 → `child_process.execFile` を使用（シェル経由しない）
- ユーザー入力をコマンド引数に渡さない設計を徹底

### 1.3 パス名パラメータ未チェック / ディレクトリ・トラバーサル (CWE-22)

**脅威**: サーバ上の任意ファイル閲覧・改ざん

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | 外部パラメータでファイル名を直接指定する実装を避ける |
| 根本的解決 | ファイルを開く際は固定ディレクトリを指定し、ファイル名にディレクトリ名を含めない |
| 保険的対策 | ファイルへのアクセス権限を正しく設定する |
| 保険的対策 | ファイル名のチェックを行う（`../` 等の排除） |

### 1.4 セッション管理の不備 (CWE-330/522/614/384)

**脅威**: セッションハイジャック、なりすまし

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | セッションIDを推測困難なものにする |
| 根本的解決 | セッションIDをURLパラメータに格納しない |
| 根本的解決 | HTTPS通信のCookieには `secure` 属性を付与 |
| 根本的解決 | ログイン成功後に新しいセッションを開始する（セッション固定攻撃対策） |
| 保険的対策 | セッションIDを固定値にしない |
| 保険的対策 | Cookie の有効期限を適切に設定する |

**モダンフレームワークでの対応**:
- Clerk, NextAuth 等の認証ライブラリを使用する場合、セッション管理は自動化される
- ただし **カスタムセッション管理を実装する場合** は上記全項目を手動で確認

### 1.5 クロスサイト・スクリプティング: XSS (CWE-79)

**脅威**: Cookie窃取、偽ページ表示、任意スクリプト実行

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | 出力する全要素にエスケープ処理を施す |
| 根本的解決 | URLを出力する際は `http://` `https://` で始まるURLのみ許可 |
| 根本的解決 | `<script>` 要素の内容を動的に生成しない |
| 根本的解決 | スタイルシートを任意のサイトから取り込めないようにする |
| 根本的解決 | Content-Type に charset を指定する |
| 保険的対策 | 入力値の内容チェックを行う |
| 保険的対策 | Cookie に `HttpOnly` 属性を付与 |
| 保険的対策 | CSP ヘッダでスクリプト実行を制限 |

**モダンフレームワークでの対応**:
- React/Next.js は JSX でデフォルトエスケープされる
- **`dangerouslySetInnerHTML`** は XSS の温床 → 使用箇所を全て特定し、DOMPurify 等でサニタイズ
- `href` に `javascript:` スキームが入らないようバリデーション

### 1.6 CSRF: クロスサイト・リクエスト・フォージェリ (CWE-352)

**脅威**: ログイン済みユーザーの意図しない操作実行

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | hidden パラメータに秘密トークンを埋め込み、リクエスト時に検証する |
| 根本的解決 | 重要操作前に再認証を要求する |
| 根本的解決 | Referer ヘッダで正規のリンク元を確認する |
| 保険的対策 | 重要操作の実行を登録メールアドレスに通知する |

**モダンフレームワークでの対応**:
- Next.js Server Actions はCSRF tokenを使用しない。POST-onlyと `Origin` 対 `Host` / `X-Forwarded-Host` の一致検証を確認する
- reverse proxyや複数origin構成ではforwarded hostの信頼境界と `serverActions.allowedOrigins` の最小許可を確認する
- SameSite Cookie 属性（`Lax` or `Strict`）で CSRF リスクを大幅軽減
- cookie認証のCustom Route Handler / REST APIは Origin/Referer検証と、必要に応じてCSRF tokenなど追加防御を実装する

### 1.7 HTTP ヘッダ・インジェクション (CWE-113)

**脅威**: 偽ページ表示、Cookie 偽造、キャッシュ汚染

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | ヘッダ出力にはフレームワーク提供のAPIを使用する |
| 根本的解決 | 改行コードを適切に処理する |
| 保険的対策 | 外部入力から改行コードを削除する |

### 1.8 メールヘッダ・インジェクション (CWE-93)

**脅威**: メール第三者中継、スパム送信の踏み台

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | メールヘッダを固定値にし、外部入力は全て本文に出力する |
| 根本的解決 | メール送信用APIを使用する |
| 保険的対策 | HTML で宛先を指定しない |
| 保険的対策 | 外部入力から改行コードを削除する |

### 1.9 クリックジャッキング

**脅威**: iframe を利用した意図しない操作の誘導

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | `X-Frame-Options` ヘッダで iframe 読み込みを制限する |
| 根本的解決 | 重要操作前に再認証を要求する |
| 保険的対策 | 重要操作をマウスのみで完結できないようにする |

**モダン対応**: `Content-Security-Policy: frame-ancestors 'self'` が推奨（X-Frame-Options の後継）

### 1.10 バッファオーバーフロー (CWE-119)

**脅威**: 任意コード実行、サービス停止

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | 直接メモリにアクセスできない言語で記述する（JavaScript/TypeScript は該当） |
| 根本的解決 | C/C++ 部分を最小限にする |
| 根本的解決 | 脆弱性が修正されたバージョンのライブラリを使用する |

### 1.11 アクセス制御・認可制御の欠落 (CWE-264/287)

**脅威**: 非公開情報への不正アクセス、権限昇格

| 対策種別 | 実施内容 |
|---------|---------|
| 根本的解決 | アクセス制御が必要なページには認証機能を設ける |
| 根本的解決 | 認証に加えて認可制御を実装し、他ユーザーへのなりすましアクセスを防止する |

---

## 2. サイト全体の安全性向上（IPA 第2章準拠）

| # | 対策領域 | 主な実施内容 |
|---|---------|------------|
| 2.1 | Webサーバ | OS・ミドルウェアの脆弱性パッチ適用、不要サービスの停止 |
| 2.2 | DNS | ゾーン転送制限、DNSSEC 対応 |
| 2.3 | ネットワーク盗聴 | TLS 1.2 以上の強制、HSTS 設定 |
| 2.4 | フィッシング対策 | ドメイン正当性の証明、EV SSL 検討 |
| 2.5 | パスワード管理 | bcrypt/Argon2 でのハッシュ化、パスワード強度ポリシー |
| 2.6 | WAF | Webアプリケーションファイアウォールの導入検討 |
| 2.7 | モバイル対応 | モバイル固有のセキュリティ考慮事項 |

---

## 3. IPA未掲載のモダンWeb脅威

IPA ガイドは 2021年改訂のため、以下のモダンな脅威はカバーされていない。

### 3.1 SSRF: Server-Side Request Forgery (CWE-918)

**脅威**: サーバー側から内部ネットワークへの不正リクエスト、クラウドメタデータ取得

| 対策 |
|------|
| ユーザー入力をURLとして使用しない |
| やむを得ない場合、許可リスト（allowlist）でホスト・スキームを制限 |
| クラウド環境では IMDS v2 を有効化（AWS の場合） |
| URLをcanonicalizeしてから検証し、alternative IP notation、user-info、混同しやすいhost表現を拒否する |
| IPv4/IPv6のloopback、private、link-local、ULA、IPv4-mapped IPv6、クラウドmetadata宛をブロックする |
| DNS リバインディング / TOCTOU対策: DNS解決結果を検証し、検証済みIPへ接続先を固定する。再解決するclientでは接続時の宛先も検証する |
| redirectは無効化を優先する。許可する場合は各hopでscheme、host、port、解決IPを再検証し、redirect回数を制限する |
| connect/read/total timeout、最大response size、streaming上限を設定する |
| network egress policy / proxyで内部networkとmetadata serviceへの到達を多層的に制限する |

### 3.2 API セキュリティ（OWASP API Security Top 10 2023）

| # | 脆弱性 | 対策 |
|---|--------|------|
| API1 | BOLA (Broken Object Level Authorization) | オブジェクトIDに対してアクセス権限を検証。`/api/videos/:id` で他ユーザーの非公開動画にアクセスできないこと |
| API2 | Broken Authentication | トークン有効期限、リフレッシュトークンのローテーション、ブルートフォース保護 |
| API3 | BOPLA (Broken Object Property Level Authorization) | レスポンスに不要なフィールドを含めない（`select` で必要カラムのみ取得） |
| API4 | Unrestricted Resource Consumption | Rate Limiting、ペイロードサイズ制限、ページネーションの強制 |
| API5 | BFLA (Broken Function Level Authorization) | 管理者API と一般ユーザーAPIのアクセス制御分離 |
| API6 | Unrestricted Access to Sensitive Business Flows | ボット対策、不正利用パターンの検出 |
| API7 | SSRF | §3.1 参照 |
| API8 | Security Misconfiguration | 不要なHTTPメソッド無効化、CORS設定の最小化、エラーメッセージの制限 |
| API9 | Improper Inventory Management | 未使用APIエンドポイントの削除、APIバージョン管理 |
| API10 | Unsafe Consumption of APIs | 外部API呼び出し時の入力検証、タイムアウト設定 |

### 3.3 JWT / OAuth セキュリティ

| リスク | 対策 |
|--------|------|
| alg: none 攻撃 | JWT 検証時にアルゴリズムをサーバー側で固定し、`none` を拒否 |
| 鍵混同攻撃 | RS256 の公開鍵を HS256 の秘密鍵として悪用される。アルゴリズムをサーバー側で固定し、鍵タイプを検証する |
| 秘密鍵の漏洩 | 環境変数で管理、定期ローテーション |
| トークン有効期限 | アクセストークンは短命（15分以内）、リフレッシュトークンで更新 |
| JWTのペイロードに機密情報 | JWT は Base64 デコードで内容が見える → 機密情報を含めない |
| PKCE の不使用 | SPA/モバイルアプリでの OAuth は必ず PKCE を使用 |

### 3.4 CORS 設定ミス

| リスク | 対策 |
|--------|------|
| `Access-Control-Allow-Origin: *` | 認証付きAPIでは絶対に使用しない |
| Origin の動的反映 | リクエストの Origin をそのまま反映しない → 許可リストで検証 |
| credentials との組み合わせ | `Access-Control-Allow-Credentials: true` 時は Origin をワイルドカードにできない |

### 3.5 サプライチェーン攻撃（依存関係の脆弱性）

| 対策 |
|------|
| `npm audit` / `yarn audit` を CI/CD に組み込み、定期実行 |
| Dependabot / Renovate でセキュリティアップデートを自動化 |
| `package-lock.json` / `yarn.lock` をコミットし、依存関係を固定 |
| 不要な依存関係を定期的に削除 |
| typosquatting 対策: パッケージ名を公式ドキュメントから確認 |
| SBOM（Software Bill of Materials）の生成・管理で推移的依存関係を可視化 |

### 3.6 シークレット管理

| リスク | 対策 |
|--------|------|
| `.env` のコミット | `.gitignore` に `.env*` を追加（`.env.example` は除く） |
| APIキーのハードコード | 環境変数またはシークレット管理サービスを使用 |
| クライアントサイドへの漏洩 | `NEXT_PUBLIC_` プレフィックスの変数に秘密情報を入れない |
| シークレットのローテーション | 定期的なキーローテーションの仕組みを構築 |
| ログへの出力 | エラーログにAPIキーやトークンが含まれないよう注意 |
| デプロイ環境のスコープ分離 | Vercel 等の Preview / Production / Development 環境で本番シークレットが Preview に漏洩しないこと |

### 3.7 Security Headers

| ヘッダ | 設定値 | 目的 |
|--------|--------|------|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self' 'nonce-{per-response-random}'`。nonceはCSPRNGでresponseごとに一意・推測不能に生成し、許可する要素だけへ付与 | XSS 防止 |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | HTTPS 強制 |
| `X-Content-Type-Options` | `nosniff` | MIME スニッフィング防止 |
| `X-Frame-Options` | `DENY` or `SAMEORIGIN` | クリックジャッキング防止 |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | ブラウザ機能の制限 |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | リファラ情報の制限 |

### 3.9 例外処理の不備（OWASP 2025 新規）

**脅威**: エラーメッセージへの機密情報露出、不完全なエラーハンドリングによるセキュリティバイパス

| 対策 |
|------|
| エラーハンドリングが未実装のコードパスを排除する |
| 例外発生時に "Fail Secure" な状態に遷移する（デフォルト拒否） |
| エラーメッセージに DB スキーマ、スタックトレース、システム情報を含めない |
| finally ブロックで必ずリソース解放・セッション終了等のクリーンアップを実行 |
| 本番環境では詳細エラーを抑制し、ログにのみ記録する |

### 3.10 Server Actions の公開エンドポイント化リスク（Next.js 固有）

**脅威**: `'use server'` 関数が自動的に公開 HTTP エンドポイントとなり、認証なしで呼び出し可能

| 対策 |
|------|
| `'use server'` 関数内で必ず認証・認可チェックを実装する |
| ユーザー入力は Zod 等で厳密にバリデーションする |
| Data Access Layer で再度認可を確認する（二重チェック） |
| Server Component → Client Component への props で秘密情報を渡さない |
| `redirect(userInput)` によるオープンリダイレクトを防止（ホワイトリスト検証） |

### 3.11 脆弱性管理（Zero-Day 対応）

**脅威**: 未知の脆弱性を突いた攻撃

| 対策 |
|------|
| CVE 情報の継続的監視（CISA KEV, NVD, JVN） |
| パッチ管理の自動化（Dependabot / Renovate） |
| WAF ルールの定期更新 |
| Security Headers による一般的攻撃の緩和（多層防御） |

### 3.8 AI 固有のセキュリティ

AI機能を含むアプリケーション固有の脅威:

| リスク | 対策 |
|--------|------|
| プロンプトインジェクション | instructionとuntrusted contentを明示的に分離する。入力サニタイズや検出は補助対策であり、完全防止策として扱わない |
| 間接プロンプトインジェクション | 外部データ（Web取得、DB）をuntrustedとしてlabel付けし、その内容だけでtool実行や権限昇格を決定しない |
| Tool authorization | toolごとにserver-side authorizationを再実施し、user / tenant境界と最小権限credentialを強制する |
| データ流出 | 機密data access、tool result、network egress、最終outputを制限し、cross-tenant / unauthorized destinationへの送信を拒否する |
| 高影響操作 | 送信、削除、購入、権限変更などは実行直前にhuman approvalを要求し、承認対象と最終parameterを表示する |
| 監査可能性 | tool call、authorization decision、承認、egress先をsecretを除外して監査logへ残す |
| データポイズニング | 学習・ファインチューニングデータの品質管理 |
| モデル出力の信頼 | AI出力を信頼しない（出力をそのまま SQL やコマンドに使用しない） |
| API キーの従量課金悪用 | Rate Limiting + 予算上限の設定 |
| PII（個人情報）の漏洩 | AI プロンプトに個人情報を含めない設計 |

---

## 4. セキュリティレビュー実施手順

### 4.1 レビュー準備

1. レビュー対象のアーキテクチャ図・データフロー図を確認
2. 使用技術スタックに応じた脅威モデルを選択（§1〜§3 から該当項目を抽出）
3. 認証・認可フロー図を確認
4. 外部サービス連携の一覧を確認

### 4.2 チェック実施順序

優先度の高い順にチェックする:

| 優先度 | チェック対象 | 理由 |
|--------|------------|------|
| 最優先 | 認証・認可（§1.11, §3.2 API1/5） | 権限不備はシステム全体に影響 |
| 最優先 | インジェクション系（§1.1, §1.2, §1.5） | 直接的なデータ漏洩・改ざんリスク |
| 高 | セッション管理（§1.4） | なりすましリスク |
| 高 | CSRF（§1.6）/ CORS（§3.4） | ログイン済みユーザーの不正操作 |
| 高 | シークレット管理（§3.6） | APIキー・認証情報の漏洩 |
| 中 | Security Headers（§3.7） | 多層防御の一環 |
| 中 | サプライチェーン（§3.5） | 間接的な脆弱性混入 |
| 中 | Rate Limiting（§3.2 API4） | リソース枯渇・不正利用 |
| 低 | その他（§1.7-1.10, §2） | 特定条件下で発生 |

### 4.3 指摘フォーマット

セキュリティ指摘は以下の形式で記録する:

```markdown
### SEC-ISSUE-{番号}: {脅威名}

- **CWE**: CWE-{番号}
- **重大度**: Critical / High / Medium / Low
- **該当箇所**: {ファイルパス}:{行番号} or {設計ドキュメント}:{セクション}
- **現状**: {現在の実装/設計の状態}
- **脅威**: {発生しうる被害}
- **根本的解決**: {推奨する修正方法}
- **保険的対策**: {補助的な対策}（該当する場合）
- **検証方法**: {修正が正しいことを確認する手順}
```

### 4.4 重大度の判定基準

| 重大度 | 基準 | 対応 |
|--------|------|------|
| Critical | リモートコード実行、認証バイパス、大規模データ漏洩 | 即座に修正。リリースブロッカー |
| High | SQLi, XSS, CSRF, アクセス制御不備 | 次フェーズ前に必須修正 |
| Medium | 情報漏洩（限定的）、Security Headers 不備、Rate Limiting 不足 | 開発中に対応 |
| Low | ベストプラクティスからの逸脱、将来的なリスク | 将来フェーズで対応 |

---

## 5. フレームワーク別クイックリファレンス

### Next.js (App Router) セキュリティチェックポイント

| チェック項目 | 確認内容 | 参照 |
|-------------|---------|------|
| Server Actions | CSRF tokenは使われない。POST-only、`Origin` と `Host` / `X-Forwarded-Host` の一致、`allowedOrigins` の最小化、proxy境界を確認する。入力バリデーション（Zod等）と関数内の認証・認可チェック必須 | §1.6, §3.10 |
| Server → Client 境界 | Server Component の props で Client Component に秘密情報を渡していないこと。Server Component は秘密情報にアクセス可能だがクライアントに露出するリスクあり | §3.6 |
| `redirect()` の安全性 | Server Actions 内の `redirect(userInput)` でオープンリダイレクトが発生しないこと。ホワイトリスト検証必須 | §1.5 |
| Route Handler GET キャッシュ | 対象Next.js versionとroute configを確認する。Next.js 15以降のGET Route Handlerは既定で非cacheだが明示的なcache opt-inは可能。旧versionを含め、認証依存dataが共有cacheへ入らないことを検証する | — |
| API Routes | 認証ミドルウェアの適用、Rate Limiting の実装 | §3.2 |
| `dangerouslySetInnerHTML` | 使用箇所の全特定、DOMPurify によるサニタイズ | §1.5 |
| 環境変数 | `NEXT_PUBLIC_` に秘密情報が含まれていないこと | §3.6 |
| `next.config.js` | Security Headers の設定、`remotePatterns` が最小限であること | §3.7 |
| Middleware | 認証チェック、リダイレクトの安全性 | §1.11 |

### Prisma セキュリティチェックポイント

| チェック項目 | 確認内容 | 参照 |
|-------------|---------|------|
| `$queryRawUnsafe` / `$executeRawUnsafe` | 原則禁止。untrusted interpolation、文字列連結、動的identifierがないかを確認し、安全なAPI / TypedSQLへ置換する | §1.1 |
| `$queryRaw` / `$executeRaw` | tagged templateまたはtrusted `Prisma.sql` + bind valueを使い、`Prisma.raw` や偽装templateでuntrusted textを混入していないか | §1.1 |
| アクセス制御 | クエリに必ず `where: { userId: currentUser.id }` 等の条件があるか | §1.11 |
| `select` の使用 | 不要なフィールド（パスワードハッシュ等）がレスポンスに含まれていないか | §3.2 API3 |
| `include` の過剰取得 | `include: { user: true }` で関連テーブルの機密フィールドが漏洩しないこと。フィールド制限を確認 | §3.2 API3 |
| Mass Assignment | `prisma.create({ data: req.body })` のようにユーザー入力を直接渡していないこと。Zod でバリデーション後に渡す | §3.2 API3 |
| DB接続文字列 | 環境変数で管理されているか、ログに出力されていないか | §3.6 |

### Clerk（認証）セキュリティチェックポイント

| チェック項目 | 確認内容 | 参照 |
|-------------|---------|------|
| Middleware | 保護すべきルートが `clerkMiddleware()` + `createRouteMatcher()` でカバーされているか（v5+ では `authMiddleware` は非推奨） | §1.11 |
| Server Component の `auth()` | 保護ルートの Server Component で `auth()` を呼び userId を取得しているか（Middleware だけでは不十分な場合あり） | §1.11 |
| Webhook 検証 | Clerk Webhook の署名検証（`svix` ライブラリ）が実装されているか | §1.6 |
| ロールベースアクセス | 管理者機能に適切なロールチェックがあるか | §3.2 API5 |
| セッション管理 | セッション有効期限が適切か | §1.4 |

---

## 6. CWE 対応表（クイックリファレンス）

| # | 脆弱性 | CWE-ID | セクション |
|---|--------|--------|-----------|
| 1 | SQL インジェクション | CWE-89 | §1.1 |
| 2 | OS コマンド・インジェクション | CWE-78 | §1.2 |
| 3 | ディレクトリ・トラバーサル | CWE-22 | §1.3 |
| 4 | セッション管理の不備 | CWE-330, CWE-522, CWE-614, CWE-384 | §1.4 |
| 5 | クロスサイト・スクリプティング | CWE-79 | §1.5 |
| 6 | CSRF | CWE-352 | §1.6 |
| 7 | HTTP ヘッダ・インジェクション | CWE-113 | §1.7 |
| 8 | メールヘッダ・インジェクション | CWE-93 | §1.8 |
| 9 | クリックジャッキング | — | §1.9 |
| 10 | バッファオーバーフロー | CWE-119 | §1.10 |
| 11 | アクセス制御・認可制御の欠落 | CWE-264, CWE-287 | §1.11 |
| 12 | SSRF | CWE-918 | §3.1 |
| 13 | 安全でないデシリアライゼーション | CWE-502 | §3.2 |
| 14 | CORS 設定ミス | CWE-942 | §3.4 |
| 15 | 機密情報の露出 | CWE-200 | §3.6 |
| 16 | 例外処理の不備 | CWE-755 | §3.9 |
| 17 | Server Actions 公開エンドポイント | — | §3.10 |

---

## 7. 参照ガイドライン

### 日本語

- [IPA「安全なウェブサイトの作り方」改訂第7版](https://www.ipa.go.jp/security/vuln/websecurity.html) — 本スキルの基盤
- [JPCERT/CC 脆弱性ハンドリング](https://www.jpcert.or.jp/vh/) — 脆弱性情報の収集・対応プロセス
- [JVN (Japan Vulnerability Notes)](https://jvn.jp/) — 日本語の脆弱性データベース

### 英語（グローバル標準）

- [OWASP Top 10](https://owasp.org/www-project-top-ten/) — Web アプリケーション脅威のグローバル基準
- [OWASP API Security Top 10 2023](https://owasp.org/www-project-api-security/) — API 固有の脅威
- [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) — SSRF の入力・network境界
- [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) — indirect prompt injection とcapability containment
- [Next.js Server Actions security](https://nextjs.org/blog/security-nextjs-server-components-actions) — Server Actions の公開surfaceとCSRF境界
- [Next.js `serverActions` configuration](https://nextjs.org/docs/app/api-reference/config/next-config-js/serverActions) — `allowedOrigins` とframework設定
- [Next.js Route Handlers](https://nextjs.org/docs/app/getting-started/route-handlers) — version-awareなcache挙動
- [Prisma Raw Queries](https://www.prisma.io/docs/orm/prisma-client/using-raw-sql/raw-queries) — tagged template、bind parameter、unsafe construction
- [MDN Content Security Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP) — responseごとのnonce要件
- [CISA Known Exploited Vulnerabilities](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) — 実際に悪用されている脆弱性
