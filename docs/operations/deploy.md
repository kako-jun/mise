# 本番環境・デプロイ・マイグレーション

2026-11-13 時点の観測を含む。設定の正本は `wrangler.jsonc` と `package.json`。

## 本番の構成

- Cloudflare Workers の Worker 名は `mise`。Custom Domain `mise.llll-ll.com`（`wrangler.jsonc` の `routes`）で公開している。workers.dev は無効化したと Issue #7 に記録されている（2026-11-13。本書では再確認していない）。
- データベースは D1 `mise`（バインディング名 `DB`）。`wrangler.jsonc` の `database_id` は本番 DB の識別子で、秘密ではない。ローカル開発では同じバインディングがローカルの D1（`.wrangler/`、コミットしない）に向く。
- 静的ファイルは `public/` を `ASSETS` バインディングで配信し、`/api/*`・`/`・`/sitemap.xml` は Worker が先に処理する（[architecture.md](../design/architecture.md)）。
- 現在のアプリに本番 secret は無い（`src/` は `DB` と `ASSETS` のみ使う）。

## ローカル

```sh
npm install
npm run db:migrate   # ローカル D1 に migrations を適用（--local）
npm run dev          # wrangler dev --local
npm run check        # node --check による構文確認
npm run build:globe  # src/client/globe.js から public/globe.js を生成
```

プロキシ環境で Node 製 CLI（wrangler）が `fetch failed` になる場合は、CA バンドルを `NODE_EXTRA_CA_CERTS` で渡す。

## 本番へのデプロイ

手動で行う。順序は、**マイグレーション適用 → デプロイ**。

```sh
npx wrangler d1 migrations apply mise --remote
npx wrangler deploy
```

- 本番 DB への適用は取り返しがつかないため、migration の内容とローカルでの確認結果を見てから行う。スキーマ変更は、適用後も旧 Worker が動き続けられる後方互換の形にする。
- 適用済みの migration は書き換えない。データの修正は新しい番号の migration で行う。番号は他の作業と衝突しないよう、最新を確認してから振る。
- 収集データの投入も migration（`migrations/0005`・`0009`・`0014` など）。根拠は `data/collection-*.json`（[procedures.md](../collection/procedures.md)）。
- 注意: `migrations/0006_registration_clock.sql` は `d1_migrations` の履歴に依存する書き方で、履歴の無い環境では何もしない。本番の `registered_at` の値はこのため環境で異なる（Issue #13 の指摘）。

## 未整備

- Workers Builds による GitHub 連携の自動デプロイは、2026-11-13 時点で未設定（Cloudflare ダッシュボードの OAuth が必要）。自動化するなら「マイグレーション適用に成功した場合だけデプロイ」の順序、トークンの最小権限、失敗時に旧 Worker が動き続ける条件を先に決める（Issue #7）。
- 失敗時の通知・ロールバック手順は未確認。
- セキュリティヘッダ（CSP など）やアクセシビリティの指摘は Issue #13 を参照。
