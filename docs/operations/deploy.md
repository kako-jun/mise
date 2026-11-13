# 本番環境・デプロイ・マイグレーション

2026-11-13 時点の観測を含む。設定の正本は `wrangler.jsonc` と `package.json`。

## 本番の構成

- Cloudflare Workers の Worker 名は `mise`。Custom Domain `mise.llll-ll.com`（`wrangler.jsonc` の `routes`）で公開している。workers.dev は無効化したと Issue #7 に記録されている（2026-11-13。本書では再確認していない）。
- データベースは D1 `mise`（バインディング名 `DB`）。`wrangler.jsonc` の `database_id` は本番 DB の識別子で、秘密ではない。ローカル開発では同じバインディングがローカルの D1（`.wrangler/`、コミットしない）に向く。
- 静的ファイルは `public/` を `ASSETS` バインディングで配信し、`/api/*`・`/`・範囲ページ・`/sitemap.xml` は Worker が先に処理（静的ファイルの拡張子は `run_worker_first` で除外）する（[architecture.md](../design/architecture.md)）。
- 現在のアプリに本番 secret は無い（`src/` は `DB` と `ASSETS` のみ使う）。

## ローカル

```sh
npm install
npm run db:migrate   # ローカル D1 に migrations を適用（--local）
npm run dev          # wrangler dev --local
npm run check        # 構文確認（node --check）+ API ヘッダ自己検査
npm run build:globe  # src/client/globe.js から public/globe.js を生成
```

プロキシ環境で Node 製 CLI（wrangler）が `fetch failed` になる場合は、CA バンドルを `NODE_EXTRA_CA_CERTS` で渡す。

## 本番へのデプロイ

main へのマージで Workers Builds が本番へ自動デプロイする。migration は自動適用されないため、**マージ前に**手動で本番へ適用する（順序は、**migration 適用 → マージ**）。スキーマは旧 Worker でも動く後方互換の形にする。

```sh
npx wrangler d1 migrations apply mise --remote   # マージ前に実行
```

手動での再デプロイや緊急時は `npx wrangler deploy` を使う。

- 本番 DB への適用は取り返しがつかないため、migration の内容とローカルでの確認結果を見てから行う。スキーマ変更は、適用後も旧 Worker が動き続けられる後方互換の形にする。
- 適用済みの migration は書き換えない。データの修正は新しい番号の migration で行う。番号は他の作業と衝突しないよう、最新を確認してから振る。
- 収集データの投入も migration（`migrations/0005`・`0009`・`0014` など）。根拠は `data/collection-*.json`（[procedures.md](../collection/procedures.md)）。
- 注意: `migrations/0006_registration_clock.sql` は `d1_migrations` の履歴に依存する書き方で、履歴の無い環境では何もしない。本番の `registered_at` の値はこのため環境で異なる（Issue #13 の指摘）。

## プレビュービルド（ブランチごと）

Cloudflare の自動ビルドは、ブランチ（PR）ごとに `npx wrangler preview` でプレビュー用の Worker（Worker Previews。本番の Worker の枝）をデプロイする（ダッシュボードで確認済み、2026-11-14: プレビューブランチのビルドも Build コマンドは「なし」、プレビューコマンドは `npx wrangler preview`、ルートディレクトリ `/`。プレビュー URL は現在「無効」で、ビルドは実行されるが URL は有効化するまで作られない）。設定は `wrangler.jsonc` の `previews` ブロックで、**ブロックが無いと `Your Wrangler configuration is missing a previews block` でビルドが失敗する**（コードの不具合ではない）。

- `previews.d1_databases` で、プレビューの `DB` バインディングを**プレビュー専用の D1 `mise-preview`**（`database_id` は `wrangler.jsonc` 参照）に向ける。**本番 DB `mise` をプレビューから触らせない**ため（ブランチの未レビューのコードが本番データを読み書き・破壊しないように）。静的ファイル（`assets`）はトップレベルの設定が使われる。
- `mise-preview` は本番と同じ migrations（`migrations/` の全部）を適用して作る。**新しい migration を足したら `mise-preview` にも手動で適用する**（自動では適用されない。適用し忘れるとプレビューがスキーマ違いで落ちる）:

```sh
npx wrangler d1 migrations apply mise-preview --remote
```

  `wrangler d1 migrations apply` はバインディング名または `database_name` を取るので、対象に `mise-preview` を指定すれば本番には触れない（`mise` と取り違えない）。`mise-preview` のデータは確認用の複製で、本番とのずれは許容する。
- プレビューの URL とビルド結果は PR のチェック（Cloudflare のコメント）に出る。ローカルでの設定確認は `npx wrangler preview --help` と `npx wrangler dev --local`（デプロイはしない）。

## CSP を含むデプロイ後の確認手順

CSP（[architecture.md](../design/architecture.md#セキュリティヘッダ)）は、Worker が返す HTML のインライン script のハッシュで許可している。Cloudflare が配信途中で HTML を書き換えると（Rocket Loader、Web Analytics の自動注入、Auto Minify、メールアドレス難読化など）、ハッシュが合わず script が止まる、または余計な script が違反になる。ローカルでは再現しないので、**デプロイのたびに本番で確認する**。

```sh
# 実サイトへ 4 ページだけ。違反が 1 件でもあれば終了コード 1
PLAYWRIGHT_DIR=<playwright の入った node_modules> CHROMIUM=<chrome の実行ファイル> \
  bwrap --dev-bind / / --tmpfs /dev/dri node scripts/check-prod-csp.mjs https://mise.llll-ll.com
```

`bwrap` は GPU デバイス（`/dev/dri`）を隠してヘッドレス chromium の固まりを避けるためのもの（不要な環境では外してよい）。出力は各ページの CSP ヘッダの有無・ロゴ描画・地球の canvas・インライン script 数・`securitypolicyviolation` とコンソールの違反。ローカルは URL を `http://localhost:8787` などに変えれば同じ確認ができる。**渡すのはオリジン（`https://mise.llll-ll.com`、`http://localhost:8787`）だけ**で、確認するページは固定の 4 ページ（`/` `/us/` `/all/?category=science` `/jp/?q=a`。jp・us・all を網羅）。パス付きで渡してもパス部分は使わず捨てる。

問題があるとき:

1. 利用者への影響が大きければ、先に `npx wrangler rollback` で直前の版に戻す。
2. 原因の切り分けには `src/security.js` の `CSP_REPORT_ONLY` を `true` にして再デプロイする（強制せず、違反をコンソールに出すだけになる）。
3. Cloudflare ダッシュボード（`mise.llll-ll.com` のゾーン）で、次を確認して無効にする: Speed の Rocket Loader、Auto Minify（HTML）、Web Analytics の自動注入（Web Analytics 側の Automatic setup）、Scrape Shield のメールアドレス難読化。
4. 直ったら `CSP_REPORT_ONLY` を `false` に戻して再デプロイし、もう一度スクリプトで違反 0 を確認する。

`/api/*` の `Cache-Control` は成功応答（400 未満）だけ `public, max-age=30`。エラー応答は `no-store`。自己検査は `node scripts/check-api-headers.mjs`。

## 未整備

- Workers Builds の GitHub 連携は設定済み（main へのマージで本番へ自動デプロイされ、PR ブランチでは preview ビルドが走る）。ダッシュボードで確認済み（2026-11-14）: Git リポジトリ `kako-jun/mise`、プロダクションブランチ `main`、ルートディレクトリ `/`。本番ビルドの Build コマンドは「なし」、Deploy コマンドは `npx wrangler deploy`。したがって main へのマージは migration を適用せず Worker だけをデプロイする（migration は事前に手動適用）。プレビュー（ブランチ）側は Build コマンドなし・プレビューコマンド `npx wrangler preview`・ルート `/` で、プレビュー URL は無効（上の「プレビュービルド」参照）。
- migration は自動適用されない。新しい migration を含む PR を main にマージすると、スキーマが旧いまま新 Worker が本番に出る恐れがある。**先に本番へ migration を適用してから、その後マージする**（後方互換の形にする。上の「本番へのデプロイ」参照）。
- 失敗時の通知は未整備。ロールバックは上の手順（`wrangler rollback`）。
- アクセシビリティの指摘は Issue #13 を参照。
