# 本番環境・デプロイ・マイグレーション

2026-11-13 時点の観測を含む。設定の正本は `wrangler.jsonc` と `package.json`。

## 本番の構成

- Cloudflare Workers の Worker 名は `mise`。Custom Domain `mise.llll-ll.com`（`wrangler.jsonc` の `routes`）で公開している。workers.dev は無効化したと Issue #7 に記録されている（2026-11-13。本書では再確認していない）。
- データベースは D1 `mise`（バインディング名 `DB`）。`wrangler.jsonc` の `database_id` は本番 DB の識別子で、秘密ではない。ローカル開発では同じバインディングがローカルの D1（`.wrangler/`、コミットしない）に向く。
- 静的ファイルは `public/` を `ASSETS` バインディングで配信し、`/api/*`・`/`・範囲ページ・`/sitemap.xml` は Worker が先に処理（静的ファイルの拡張子は `run_worker_first` で除外）する（[architecture.md](../design/architecture.md)）。
- 必須の secret は `INTAKE_SALT`（申請の IP ハッシュの塩。未設定だと申請の受付が 503 で止まる。[requests.md](requests.md)）。設定は [秘密情報](#秘密情報)。

## ローカル

```sh
npm install
npm run db:migrate   # ローカル D1 に migrations を適用（--local）
npm run dev          # wrangler dev --local
npm run check        # 構文確認（node --check）+ API ヘッダ自己検査
npm test             # 申請の検証・SSRF 対策・拒否リストの単体テスト
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
- **0018（`sites.search_text`）を適用した後**は、検索用の列を NFKC + 小文字化で埋め直す。migration の暫定値は ASCII の小文字化だけで、全角半角や非 ASCII の大小は埋め直すまで合わない。サイトを migration で足した後も同じ。**0020（タグを `search_text` に含める）の適用後も同じ**（**`refresh-search-text.mjs` は 0020 を含むブランチ（マージ後は main）のものを使う。古い版を回すとタグが search_text から消える**。migration の暫定値は ASCII のみ。タグの追加・削除のたびにトリガが暫定値へ戻すので、まとめてタグを入れた後にも実行する）。本番 → プレビューの順に実行する（`--db` は `mise` / `mise-preview` のみ。`--local` は `mise` 専用）:
  ```
  node scripts/refresh-search-text.mjs --remote --db mise
  node scripts/refresh-search-text.mjs --remote --db mise-preview
  ```
- **連絡先の廃止（#88）のマージ後に 1 回だけ**、本番とプレビューの両方で、廃止前に保存された連絡先を消す（`contact` 列は未使用で常に NULL とする。定期の掃除からは外してある）:

  ```sh
  npx wrangler d1 execute mise --remote --command "UPDATE requests SET contact = NULL WHERE contact IS NOT NULL;"
  npx wrangler d1 execute mise-preview --remote --command "UPDATE requests SET contact = NULL WHERE contact IS NOT NULL;"
  ```

- 適用済みの migration は書き換えない。データの修正は新しい番号の migration で行う。番号は他の作業と衝突しないよう、最新を確認してから振る。
- 収集データの投入も migration（`migrations/0005`・`0009`・`0014` など）。根拠は `data/collection-*.json`（[procedures.md](../collection/procedures.md)）。
- 注意: `migrations/0006_registration_clock.sql` は `d1_migrations` の履歴に依存する書き方で、履歴の無い環境では何もしない。本番の `registered_at` の値はこのため環境で異なる（Issue #13 の指摘）。

## 官公庁の名簿（0015・0019）を本番へ適用する手順（Issue #37、PR #71）

**この手順は承認を取ってから実行する。** 0015 は約 1850 件のサイトを追加し、0019 は一覧の並びのインデックスを作り直す。ローカルでの確認結果（件数・ページング・HUD）は PR #71 を見る。順序は **マージ前に migration → 検索用列の埋め直し → マージ（デプロイ）**。0015 の `bulk_import` 列と 0019 のインデックスを、旧 Worker は使わない（旧 Worker は列を見ないので、先に適用しても壊れない。新 Worker は列が無いと 500 になる）。

1. バックアップ（本番とプレビューのそれぞれ。取り直せるよう日付を付ける）:
   ```sh
   npx wrangler d1 export mise --remote --output backup-mise-$(date +%Y%m%d).sql
   npx wrangler d1 export mise-preview --remote --output backup-mise-preview-$(date +%Y%m%d).sql
   ```
   バックアップはリポジトリにコミットしない。
   **0027（検索の全文索引）の適用後は仮想表ができるので `d1 export` は使えない**。そのときのバックアップは Time Travel の bookmark（`npx wrangler d1 time-travel info <DB>`）を主にする（[検索の全文索引（0027）](#検索の全文索引0027を本番へ適用する手順issue-126-第3段)）。
2. 適用。本番は `npx wrangler d1 migrations apply mise --remote`（事前に `npx wrangler d1 migrations list mise --remote` で未適用の一覧を確認する）。プレビュー DB は「プレビュー DB を本番に追従させる手順」の 1・2・4（一時設定・未適用の確認・適用。バックアップは上の手順 1 で取得済み）。
3. 検索用列の埋め直し（0015 の INSERT はトリガが ASCII の小文字化だけの暫定値を入れる。全角半角の検索がヒットするよう NFKC に直す）: 本番は `node scripts/refresh-search-text.mjs --remote --db mise`。プレビューは同手順の 5。
4. 確認（適用後、マージ前に本番 DB を直接読む）:
   ```sh
   npx wrangler d1 execute mise --remote --command "SELECT bulk_import, COUNT(*) FROM sites GROUP BY bulk_import"
   npx wrangler d1 execute mise --remote --command "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_sites_sort%'"
   ```
   期待: `bulk_import=1` が約 1850、インデックスは `idx_sites_sort_9b34c1ec` のみ（旧 `idx_sites_sort_2489145a` は 0019 で DROP 済み）。
5. マージ後の確認: `/api/scopes` の日本の件数、`/jp/?category=government&category=cities` の初回表示（0021 適用後。0015 だけの時点では旧カテゴリ）、`/api/activity` に台帳分が入っていないこと（HUD の直近登録）。
6. 戻し方: 0015 の投入分は `DELETE FROM site_tags/site_categories/sites WHERE id LIKE 'pb-%'`（`bulk_import` 列は残ってよい）。**既存の sample-6（国立国会図書館）に 0015 が追加した政府機関カテゴリも消す**: `DELETE FROM site_categories WHERE site_id='sample-6' AND category_id='government-government-agencies'`（元の `reference-libraries` は残す。0021 適用後は旧カテゴリが無いので `category_id='national-agencies'`、`reference-libraries` は `libraries`）。全体はバックアップから `d1 execute --file` で戻す前に、影響範囲を確認する。台帳が変わったら `public_bodies_sql.py --since` で差分 migration を足す（[public-bodies.md](../collection/public-bodies.md)）。

## カテゴリの木の入れ替え（0021）を本番へ適用する手順（Issue #114 第1段）

**承認を取ってから実行する。** 0021 は `categories` を旧 59 → 新 153 に入れ替え、`site_categories` を対応表で移す（`sites`・`site_tags`・`search_text` には触れない。検索用列の埋め直しは不要）。

**順序は 1 つ**: 適用前の確認 → バックアップ → migration 適用 → 集計表と再帰 CTE の一致確認 → マージ。**新しいコードは `category_counts` / `scope_counts` が無いと `/api/categories` `/api/scopes` などがエラーになる**ので、migration を先に適用してからマージする（旧コードは新しい表を使わないので、migration だけ先に入っても壊れない）。

0. **適用前の確認**: 適用は **PR ブランチ（`126-category-counts`）をチェックアウトした状態で行う**（`main` にはまだ 0022 のファイルが無い）。`npx wrangler d1 migrations list mise --remote` で**未適用が 0022 だけ**であることを確かめる（他に未適用があれば、それも一緒に流れてしまうので止めて原因を調べる）。
1. **バックアップ**: 上の手順と同じ `npx wrangler d1 export mise --remote --output=…` と、`npx wrangler d1 time-travel info mise` の時点（bookmark）を控える。
2. **適用**: `npx wrangler d1 migrations apply mise --remote`。続けて**プレビュー DB も**（「プレビュー DB を本番に追従させる手順」）。
3. **一致確認**（`--remote` の SELECT だけ）: `uv run python3 scripts/check-data-integrity.py --remote`。**検査ごとに結果 0 行**が正で、1 つでも FAIL なら終了コードが 0 以外になる（`warn` は表示のみ）。検査の定義の正本は `scripts/llm/integrity.py`（検査一覧と意味は [llm-judge.md](../collection/llm-judge.md#データ整合チェック137)）。**1 本のクエリに UNION ALL を多数連ねない**（D1 本番は複合 SELECT の項数の上限が低く、1 本にまとめた版は 2026-11-18 に `too many terms in compound SELECT: SQLITE_ERROR [code: 7500]` で実際に失敗した。ローカルでは通る）ので、スクリプトは検査ごとに 1 本ずつ流す。このうち集計表の確認は `category_counts_all` / `category_counts_country` / `category_counts_region`（0026）/ `scope_counts_country`（国・区画とも）/ `scope_counts_all` / `category_closure` の 6 本（`expected` が数え直した値、`cached` が表の値）。ローカルの検証には `tests/category-counts.test.mjs`（全 migration 適用後のメモリ SQLite で、投入・削除・カテゴリ変更・親子変更の各操作の後に突き合わせる。上の 6 本そのものも、わざとずらした DB で検出できることを検査している）と、`tests/test_integrity.py`、`tests/test_update_categories.py` / `tests/test_llm_ledger.py`（夜間の一括投入 SQL を流したあと）がある。
4. **画面の確認**: 適用前に控えた `/api/categories?scope=all` の `site_count` と、適用後の値・`/jp/` のカテゴリ件数が同じ。`/api/scopes` の `total` が `SELECT COUNT(*) FROM sites` と同じ。
5. **マージ**: 手順 3・4 が想定どおりなら PR をマージする（自動デプロイ）。マージ後、**手元の dev DB でも `npm run db:migrate` を流す**（流さないと手元の `/api/categories` が表なしでエラーになる）。
6. **失敗したとき**: 手順 2 が失敗したら 0022 は 1 つのトランザクションなので全体が取り消される（`SELECT name FROM d1_migrations WHERE name LIKE '0022%'` が空であることを確認）。

## 冗長なカテゴリ所属の一度きりの整理（Issue #137）を本番へ適用する手順

**承認を取ってから実行する。** `scripts/fix-redundant-categories.sql`（単純な DELETE 1 文）で、親と子の両方が付いたサイトの**親の行だけ**を消す。退避先（`business` / `society` / `business-services` / `community`）は新しい木で LLM が選ぶ正規のカテゴリなので消さない。`pb-` は対象外、サイトは最後のカテゴリを失わない、2 回流しても 2 回目は何も消さない。本番相当の再現（公開 API の 2,158 件）では 102 サイト・103 行が消え、`site_categories` は 2,429 行 → 2,326 行。

1. **適用前**: `uv run python3 scripts/check-data-integrity.py --remote`（`ancestor_duplicate` 以外の error 検査は 0 行であること。`retreat_with_real` は warn の情報で、整理の対象ではない）。`npx wrangler d1 time-travel info mise` の bookmark を控える。集計表の写しを取る: `npx wrangler d1 execute mise --remote --json --file scripts/category-counts-snapshot.sql > before.json`。
2. **適用**（ローカル D1 で先に同じ手順を通す）: `npx wrangler d1 execute mise --remote --file scripts/fix-redundant-categories.sql`。
3. **適用後**: 同じ `check-data-integrity.py --remote` が **全 error 検査で 0 行**（`ancestor_duplicate` も 0 に。`retreat_with_real` の warn は残ってよい）。写しをもう一度 `after.json` に取って `diff before.json after.json`。**差は 1 行も出ないのが正**（外すのは親の行だけで、親の件数は子孫を数えるので `category_counts` も `scope_counts` も完全に不変）。差が出たら止めて原因を調べる。
4. **戻し方**: Time Travel の bookmark へ戻す（`npx wrangler d1 time-travel restore mise --bookmark=…`）。

### 0022 の戻し方と作り直し方

**(i) 戻す**（適用後に不具合が出たとき。正本は変えていないので件数は失われない）: **先にコードを戻す**（PR を revert してデプロイ。新しいコードは集計表を読むので、表を先に消すとエラーになる）。そのあと次を流す（トリガ 9 本 → 表 3 つの順）:

```sh
npx wrangler d1 execute mise --remote --command "DROP TRIGGER category_closure_ai; DROP TRIGGER category_closure_au; DROP TRIGGER category_closure_ad; DROP TRIGGER category_counts_sc_ai; DROP TRIGGER category_counts_sc_ad; DROP TRIGGER category_counts_sc_au; DROP TRIGGER category_counts_sites_ai; DROP TRIGGER category_counts_sites_bd; DROP TRIGGER category_counts_sites_au; DROP TABLE category_counts; DROP TABLE scope_counts; DROP TABLE category_closure"
```

DROP しても `d1_migrations` には `0022_category_counts.sql` が残る。**同じ 0022 は二度と流れない**ので、もう一度入れ直すには、表・索引・初期集計・トリガを作る**別の migration（0023 以降。0022 の中身を元にする）**が要る。

**(ii) ずれたときの作り直し**（表は残したまま、正本から集計し直す。コードは戻さなくてよい）: `scripts/rebuild-category-counts.sql`（3 表を `DELETE` → 0022 の初期集計と同じ `INSERT`。逐語一致と結果の一致は `tests/category-counts.test.mjs` が保証）を、夜間投入を流していない時間に実行し、そのあと手順 3 のクエリで 0 行を確かめる:

```sh
npx wrangler d1 execute mise --remote --file scripts/rebuild-category-counts.sql
```

> 0022 は適用後に凍結する。カテゴリの木や集計の意味を変えるときは新しい migration で表とトリガを作り直す。

## 検索計画と索引（0023〜0025）を本番へ適用する手順（Issue #126 第2段）

**承認を取ってから実行する。** 0023（タグ日本語名の索引）・0024（タグ英語名の索引）・0025（活動の索引）は、`CREATE INDEX IF NOT EXISTS` だけの migration で、**1 本 1 索引**に分けてある。書き込み行数 = 索引の行数（0023 = `site_tags` の行数、0024 = `label_en` が NULL でない `site_tags` の行数、0025 = `sites` の行数。10 万件で各約 24 万・24 万・10 万行）。

**書き込みの小分け（無料プランに戻した場合の 1 日 10 万行。Workers Paid 契約済みで月 5,000 万行込みのため、現状は分けなくてよい）**: 現在の件数なら 3 本まとめても小さい（本番の件数は適用前に下の手順 1 で調べる）。**合計が 1 日の枠（10 万行）に収まらないときは、1 日 1 本ずつ適用する**（枠を超えるとクエリが失敗する。失敗した本がどこまで取り消されるかは未検証なので、枠に収まる見積りで適用する）。`migrations apply` は未適用を全部流すので、1 本だけ流すには、その番号までの migration を並べた一時ディレクトリを `migrations_dir` にした一時設定を使う（「プレビュー DB を本番に追従させる手順」の一時設定と同じ考え方。リポジトリの外に置く）:

```sh
rm -rf /tmp/mise-step && mkdir -p /tmp/mise-step/migrations && for f in migrations/00{01..23}_*.sql; do ln -s "$(pwd)/$f" /tmp/mise-step/migrations/; done   # 0023 まで（次の日は {01..24}、その次は {01..25}）。毎回ディレクトリごと作り直す（前の日のリンクが残ると、適用したくない migration まで流れる）
cat > /tmp/mise-step/wrangler.jsonc <<'JSON'
{ "name": "mise-step-tmp", "compatibility_date": "2025-01-01",
  "d1_databases": [{ "binding": "DB", "database_name": "mise", "database_id": "<wrangler.jsonc の database_id>", "migrations_dir": "migrations" }] }
JSON
npx wrangler d1 migrations apply mise --remote -c /tmp/mise-step/wrangler.jsonc
```

**順序: 索引（0023〜0025）が先、コードは後。** 索引なしでコードだけ先に出してはいけない（結果は同じだが、本番相当のデータ 2,158 件で全ページをたどる合計の読み取りが main の 4.3 倍になる。タグの該当数のプローブがページごとに `site_tags` を全件走査するため）。現在の本番の件数（`site_tags` 4,528 行・サイト 2,158 件）なら 3 本合計の書き込みは約 1.1 万行（`site_tags` 4,528 + 4,528 以下 + `sites` 2,158）で、**1 日に 3 本まとめて入れてよい**（小分けが要るのは件数が数万を超えてから）。0022 は適用済みであること（新しいコードは `category_counts` / `scope_counts` を読む）。

1. **適用前**: 件数を調べて書き込み行数を見積もる: `npx wrangler d1 execute mise --remote --command "SELECT (SELECT COUNT(*) FROM sites) AS sites, (SELECT COUNT(*) FROM site_tags) AS tags, (SELECT COUNT(*) FROM site_tags WHERE label_en IS NOT NULL) AS tags_en"`。`npx wrangler d1 migrations list mise --remote` で未適用を確かめる。バックアップ（`d1 export` と Time Travel の bookmark）は 0022 と同じ手順。索引は派生物なので失っても正本は無事。
2. **適用**: 上の手順（または `migrations apply`）。続けて**プレビュー DB も**（「プレビュー DB を本番に追従させる手順」）。
3. **索引の確認**: `npx wrangler d1 execute mise --remote --command "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_site_tags_label_ja','idx_site_tags_label_en','idx_sites_activity')"`（適用した本数ぶん出る）。
4. **新しい形のクエリを本番で一度試す**。**D1 本番は複合 SELECT の項数上限が低く、ローカルで通るクエリが `too many terms in compound SELECT`（code 7500）で落ちた前例がある。** 第 2 段で足した形を、マージ前に本番の読み取りで通す。下は値をリテラルにした同じ形で、どれも `--remote` の SELECT だけ。**エラーなく結果が返れば合格**。`--json` を付けると `meta.rows_read` も見える（索引適用後は数百行以下のはず）。各行を `npx wrangler d1 execute mise --remote --command "<SQL>"` に渡す:
   - タグの該当数のプローブ（`planQuery`。UNION は 2 項、`LIMIT` は複合の外側）:
     `SELECT COUNT(*) AS n FROM (SELECT site_id FROM site_tags WHERE label_ja = '個人サイト' UNION SELECT site_id FROM site_tags WHERE label_en = '個人サイト' LIMIT 1001)`
   - カテゴリの件数の点読み（スカラー副問い合わせ 3 つと `EXISTS`）:
     `SELECT (SELECT n FROM category_counts WHERE category_id = 'libraries' AND scope = 'all') AS n_all, (SELECT n FROM category_counts WHERE category_id = 'libraries' AND scope = 'JP') AS n_scope, (SELECT n FROM scope_counts WHERE scope = 'JP') AS n_base, EXISTS (SELECT 1 FROM categories WHERE parent_id = 'libraries') AS has_child`
   - 稀なタグを索引から引く一覧（`IN (… UNION …)`）:
     `SELECT s.id FROM sites s WHERE s.id IN (SELECT site_id FROM site_tags WHERE label_ja = '個人サイト' UNION SELECT site_id FROM site_tags WHERE label_en = '個人サイト') ORDER BY s.bulk_import ASC, datetime(COALESCE(s.source_updated_at, s.registered_at)) DESC, s.id ASC LIMIT 41`
   - カテゴリの EXISTS 形の一覧:
     `SELECT s.id FROM sites s WHERE EXISTS (SELECT 1 FROM site_categories sc WHERE sc.category_id = 'libraries' AND sc.site_id = s.id) ORDER BY s.bulk_import ASC, datetime(COALESCE(s.source_updated_at, s.registered_at)) DESC, s.id ASC LIMIT 41`
   - 上限付きの件数（`LIMIT 1001` の副問い合わせ）:
     `SELECT COUNT(*) AS n FROM (SELECT 1 FROM sites s WHERE instr(s.search_text, 'a') > 0 LIMIT 1001)`
   - カーソルの項（等値 + 範囲だけの単純な条件）:
     `SELECT s.id FROM sites s WHERE (s.bulk_import = 0 AND datetime(COALESCE(s.source_updated_at, s.registered_at)) < '2026-01-01 00:00:00') ORDER BY s.bulk_import ASC, datetime(COALESCE(s.source_updated_at, s.registered_at)) DESC, s.id ASC LIMIT 41`
   - ピックアップ（乱数の rowid をまとめて引く棄却抽選と、足りないときの補充）:
     `SELECT MAX(rowid) AS m FROM sites` と
     `SELECT s.id FROM sites s WHERE s.rowid IN (5, 100, 900, 1500, 2000) AND s.bulk_import = 0 AND (NULL IS NULL OR s.country_code = NULL)` と
     `SELECT s.id FROM sites s WHERE s.bulk_import = 0 AND (NULL IS NULL OR s.country_code = NULL) AND s.id NOT IN ('a','b') ORDER BY RANDOM() LIMIT 1`
5. **マージ後の確認**（自動デプロイ後）: `/api/sites?q=a`（`total` が 1000 以下・上限を超えるなら `total_capped` が `true`）、`/api/sites?tag=個人サイト`、`/api/sites?category=libraries`、`/api/sites`（`total` が `/api/scopes` の `total` と同じ・`total_capped` が `false`）、`/api/picks`（3 件・重複なし）、`/api/activity` がすべて 200。`next_cursor` を使った 2 ページ目も 200。
6. **戻し方**: 索引は派生物。`DROP INDEX idx_site_tags_label_ja;`（0024・0025 も同様）で消せる（結果は同じだが、コードが索引前提の形のままだと読み取りが main より増える。消すなら先にコードを戻す）。`d1_migrations` には残るので同じ migration は再実行されない。入れ直すときは `CREATE INDEX` を直接実行する。

## 区画（都道府県）の範囲（0026）を本番へ適用する手順（Issue #127）

**承認を取ってから実行する。** 0026 は、集計表の scope 値に区画（`region_code`。例 `JP-13`）を足し、集計トリガ 7 本を区画も数える形に作り直し、区画の部分索引 `idx_sites_region_sort` を作る。**新しい列は無い**。トリガの名前は 0022 のまま（DROP → CREATE）なので、上の「0022 の戻し方」の SQL（トリガ 9 本の DROP）はそのまま使える。

- **書き込み量**: 初期集計 = 区画の `category_counts`（区画数 × 所属のあるカテゴリ数。官公庁の名簿だけなら 47 × 約 6 行）+ `scope_counts`（区画数）、索引 = `region_code` を持つサイトの行数（現在約 1,900 行。10 万件で最悪 10 万行）。無料プランの 1 日 10 万行の枠にも現在の件数なら十分収まる（Workers Paid は月 5,000 万行込み）。
- **順序は 1 つ**: 0. **PR ブランチ（`127-region-prefecture`。マージ後は `main`）をチェックアウトした状態で行う**（0022 の手順 0 と同じ。`main` の旧 `scripts/llm/integrity.py` は区画の行を知らず、0026 適用後に `category_counts_country` / `scope_counts_country` が区画の行を残骸として FAIL する）。`npx wrangler d1 migrations list mise --remote` で未適用が 0026 だけであることを確かめる（他にあれば一緒に流れるので止める）。読み取りの実行確認（書き込みなし）: `npx wrangler d1 execute mise --remote --command "SELECT 'all' AS s UNION ALL SELECT 'JP' UNION ALL SELECT 'JP-13'"`（0026 のトリガは `UNION ALL` を 3 項まで使う。本番 D1 の複合 SELECT の項数上限に収まることの確認）。→ 1. バックアップ（`npx wrangler d1 time-travel info mise` の bookmark を控える）→ 2. `npx wrangler d1 migrations apply mise --remote` → 3. 一致確認（下）→ 4. **直後にコードをマージ**。**新しいコードは区画の行が無いと区画のページが 404・件数 0 になる**（旧コードは区画の行を読まない。国の一覧は 2 文字のコードだけに絞る）ので、migration だけ先に入っても画面は壊れない。ただし**旧 `integrity.py` は壊れる**: 適用からマージまでの間に夜間ジョブ（投入前に `check-data-integrity --remote` を流す）や `export_approved_sql.py` のゲートが旧 `integrity.py` で動くと、区画の行を理由に止まる。だから**夜間ジョブを止めた時間帯に適用し、適用直後にマージする**（夜間ジョブの停止方法は [nightly.md](nightly.md) / `mise-ops`。止められないなら、適用前に cron を外し、マージ後に戻す）。トリガの作り直し自体は 1 回の migration として実行されるので投入が数え落とされることは無い（D1 は migration を 1 バッチで流す）が、旧コードの投入パイプラインが新旧の境目をまたぐと上の理由で止まるので、投入を止める助言は変わらない。
- **一致確認**: `uv run python3 scripts/check-data-integrity.py --remote`（**PR ブランチ（新 integrity）で**）が **全 error 検査で 0 行**（`category_counts_country`・`scope_counts_country` に加え、0026 から `category_counts_region` が区画の行を検査する）。ずれたら `scripts/rebuild-category-counts.sql`（区画の分を含む）で作り直す。確認の例: `SELECT scope, n FROM scope_counts WHERE scope LIKE 'JP-%' ORDER BY scope` が、サイトのある区画の数だけ並ぶ。`SELECT n FROM scope_counts WHERE scope = 'JP-01'` が `SELECT COUNT(*) FROM sites WHERE region_code = 'JP-01'` と同じ。
- **戻し方**: **先にコードを戻す**。そのあと **必ず `scripts/rollback-region-counts.sql` を流す**（新トリガ 7 本を消して 0022 のトリガに戻し、区画の行 `scope LIKE '%-%'` と索引 `idx_sites_region_sort` を消す。`npx wrangler d1 execute mise --remote --file scripts/rollback-region-counts.sql`。夜間ジョブを止めた時間に）。これで旧 `integrity.py` も 0 error になる（`tests/region-scope.test.mjs` が in-memory SQLite で「0026 適用 → 戻し → 0022 だけの状態と同じ集計表・旧 integrity の国別検査 0 行・以後の区画つき投入も国と all だけ数える」を検査している）。**コードだけ戻して区画の行を残してはいけない**（旧 `integrity.py` が FAIL して夜間ジョブが止まる）。別の道として、新 `integrity.py`（区画対応）を残してコードだけ戻す手もあるが、区画の行とトリガが残り続けるので推奨しない。0026 は適用後に凍結する（`d1_migrations` には残るので、再び区画を使うには 0026 の内容を元にした新しい migration が要る）。
- **プレビュー DB**: 本番の適用後に「プレビュー DB を本番に追従させる手順」で追従させる。

## 検索の全文索引（0027）を本番へ適用する手順（Issue #126 第3段）

**承認を取ってから実行する。** 0027 は `sites_fts`（`search_text` の FTS5 trigram・外部コンテンツ表）・`fts_state`・同期トリガ 3 本を作る。**migration は空の表とトリガを作るだけ**（既存行の構築は含めない）で、構築は `scripts/build-search-fts.mjs` が 1 万行ずつ分けて流す（10 万件の一括構築は 12〜24 秒で D1 の 1 クエリ 30 秒の上限に近い）。構築が終わる（`fts_state.ready = 1`）までは、検索コードは従来の instr の経路を使い、同期トリガは何もしない。

**FTS5 の trigram が本番 D1 で使えるかは本番では未確認**（本番の `pragma_module_list` は読めない）。プレビュー DB（`mise-preview`）では 2026-11-19 に `CREATE VIRTUAL TABLE … USING fts5(…, tokenize='trigram')`・`MATCH`・`integrity-check`・`INSERT … SELECT` での構築がすべて通った（Issue #126 に記録）。本番も同じ D1 のはずだが、**本番へ入れる前に必ずプレビューで 0027 を通す**。

**仮想表ができると `wrangler d1 export` は使えなくなる**（公式の回避策は、仮想表を DROP → export → 作り直し）。**バックアップは Time Travel の bookmark（Paid 30 日・Free 7 日）を主にする**（`npx wrangler d1 time-travel info mise`）。SQL の写しが要るときだけ、次の順に流す（スクラッチ DB で通して確認済み）: (1) `UPDATE fts_state SET ready = 0; DROP TRIGGER sites_fts_ai; DROP TRIGGER sites_fts_ad; DROP TRIGGER sites_fts_au; DROP TABLE sites_fts;`（**`ready = 0` を先に**。検索は instr に落ちる）→ (2) `d1 export` → (3) **作り直し**: `npx wrangler d1 execute <DB> --remote --file migrations/0027_search_fts.sql`（`IF NOT EXISTS` / `INSERT OR IGNORE` / `DROP TRIGGER IF EXISTS` だけなので、何度流しても同じ。表・トリガが戻る。`fts_state` は残るので `ready = 0` のまま）→ (4) `node scripts/build-search-fts.mjs --remote --db <DB>`。**`ready = 1` のまま (3) を流してはいけない**（トリガが空の索引に対して動き、削除・更新の `'delete'` で索引が壊れ、`sites` の削除・更新が失敗する）。**0027 の適用より前に**通常の export のバックアップを取っておく（適用後は上の手順でしか取れない）。

**書き込み量**: 構築 = 索引の行数（`sites` の件数ぶんの `INSERT`。内部表への書き込みが多く、ローカルの `total_changes()` で 1 サイトあたり数行〜十数行。D1 の課金がこれを数えるかは未検証）。Workers Paid 契約済み（書き込み 月 5,000 万行込み）なので、10 万件の構築（数十万〜百万行規模）でも込みの枠に収まる。無料プランに戻した場合（1 日 10 万行）は 1 日の枠を超えうるので、その場合は**件数が少ないうちに入れる**（構築のやり直しも同じ量を書く）。登録・紹介文の修正・タグの追加のたびに、索引の delete と insert が走る（タグ 3 つのサイトの登録で 17 → 約 47 行。夜間ジョブの 1 夜の書き込みの見積りにはこの増分を足す）。

**構築の前に** `node scripts/refresh-search-text.mjs --remote --db <DB>` で `search_text` を NFKC + 小文字に埋め直しておく（0018・0020 の暫定値は ASCII の小文字化だけで、非 ASCII の大文字が残っているほど、索引（大小を畳む）と instr の該当件数の差が広がる。索引の行にも instr を重ねるので結果は同じだが、計画の判断が索引の件数に寄るぶん読み取りが増える）。

**順序は 1 つ**: 0. 未適用が 0027 だけであること（`npx wrangler d1 migrations list mise --remote`）。→ 1. バックアップ（`d1 export` と Time Travel の bookmark。**適用前に**）。→ 2. **プレビュー DB で先に通す**（「プレビュー DB を本番に追従させる手順」の 1〜4 → `node scripts/build-search-fts.mjs --remote --db mise-preview` → 同 `--check`。代表クエリの確認はこの節の手順 4）。→ 3. 本番に適用: `npx wrangler d1 migrations apply mise --remote` → `node scripts/build-search-fts.mjs --remote --db mise`（構築中は夜間ジョブ・登録を止める。構築の最後に `ready = 1` にしてから件数の一致と `integrity-check` を検査する（以降の登録はトリガが拾う。検査に落ちたら `ready = 0` に戻って止まる）。落ちたら原因を直して同じコマンドを再実行する。何度流しても同じ結果）。→ 4. **新しい形のクエリを本番で読み取り実行する**（D1 本番は複合 SELECT の項数上限が低く、ローカルで通っても `too many terms in compound SELECT` で落ちた前例がある。第2段と同じ。下は `--remote` の SELECT だけで、エラーなく結果が返れば合格）:
   - 索引の該当数（`planQuery`）: `SELECT (SELECT ready FROM fts_state WHERE name = 'sites_fts') AS ready, (SELECT COUNT(*) FROM (SELECT rowid FROM sites_fts WHERE sites_fts MATCH '"tokyo"' LIMIT 1001)) AS n`
   - 一覧（索引の経路）: `SELECT s.id FROM sites s WHERE s.rowid IN (SELECT rowid FROM sites_fts WHERE sites_fts MATCH '"tokyo"') AND instr(s.search_text, 'tokyo') > 0 ORDER BY s.bulk_import ASC, datetime(COALESCE(s.source_updated_at, s.registered_at)) DESC, s.id ASC LIMIT 41`
   - タグの併用（タグ駆動の UNION と同じ文に入る形。q を索引にしない計画だが、タグが稀でないときは索引の IN と EXISTS が同じ文に入る）: 上の一覧に `AND EXISTS (SELECT 1 FROM site_tags t WHERE t.site_id = s.id AND (t.label_ja = '市' OR t.label_en = '市'))` を足したもの、および `AND s.id IN (SELECT site_id FROM site_tags WHERE label_ja = '村' UNION SELECT site_id FROM site_tags WHERE label_en = '村')` を足したもの。
   - `SELECT COUNT(*) FROM sites_fts_docsize` が `SELECT COUNT(*) FROM sites` と同じ。
   → 5. **マージ**（自動デプロイ）。マージ後、`/api/sites?q=…` の応答（`total`・`total_capped`・`next_cursor` で 3 ページ）が適用前と同じ並びであること。旧コードは索引を使わない（instr）ので、migration だけ先に入っても画面は壊れない。**新しいコードは `fts_state` が無いと instr に落ちる**（壊れない）。
   → 6. 手元の dev DB でも `npm run db:migrate` と `node scripts/build-search-fts.mjs --local`（流さなくても検索は instr で動く）。

**日常の運用**:
- **紹介文・名前・タグを SQL で直接直した/大量に足した後**: `node scripts/refresh-search-text.mjs --remote --db mise`（今の値と違う行だけ UPDATE する。同値の UPDATE は書き込みを増やさないよう省いてある）。索引はトリガが追従する。
- **索引を疑うとき**: `node scripts/build-search-fts.mjs --remote --db mise --check`（件数の一致と `integrity-check`。SELECT と検証コマンドだけで書き込みなし）。ずれていれば同じコマンドから `--check` を外して作り直す（`ready = 0` の間は検索が instr に落ちるだけで、結果は変わらない）。**定期的に `--check` を流す**（外部コンテンツ表は `sites` の暗黙の rowid で結ぶ。D1 が VACUUM で rowid を変えるかは未検証なので、ずれの検出が要る）。
- **夜間ジョブ**: 構築の最中は止める（`ready = 0` の間に登録・削除されたサイトは索引に入らない/残る。最後の検査が検出して失敗する）。

**戻し方**（失敗したとき・使わないとき）: **先にコードを戻さなくてよい**（表が無い・`ready = 0` なら、先頭ページも続きのページも instr に落ちる。続きのページは覚えた計画を使う前に `ready` を確かめ、索引の実行時の失敗は instr でやり直す。`tests/search-fts.test.mjs` が検査）。次を流す（トリガ 3 本 → 表の順。トリガを残して表だけ消すと `sites` の登録・更新が失敗する）:

```sh
npx wrangler d1 execute mise --remote --command "DROP TRIGGER sites_fts_ai; DROP TRIGGER sites_fts_ad; DROP TRIGGER sites_fts_au; DROP TABLE sites_fts; DROP TABLE fts_state;"
```

DROP しても `d1_migrations` には `0027_search_fts.sql` が残り、**同じ 0027 は二度と流れない**。入れ直すには、同じ内容の新しい migration（0028 以降）が要る。正本（`sites.search_text`）は変わらないので件数は失われない。Time Travel の bookmark でも戻せる。検索コードの instr の経路は残してある（消さない）。

## プレビュー DB を本番に追従させる手順

`mise-preview` は `wrangler.jsonc` の `d1_databases` に無く（`previews` ブロックにしか無い）。**`migrations list` / `migrations apply` だけは、一時的な wrangler 設定ファイルを作って `-c` で指定する（コミットしない）。`d1 execute`・`d1 export`・`refresh-search-text.mjs` は `mise-preview` を名前で指定すればそのまま動く。** 本番 `mise` には触れない（コマンドごとに対象が `mise-preview` であることを確かめる）。順序は 1 つ:

1. **一時設定**（リポジトリの外に置く。`<tmp>` は任意のディレクトリ）:
   ```sh
   rm -rf <tmp>/mise-preview-cfg && mkdir -p <tmp>/mise-preview-cfg && cp -r migrations <tmp>/mise-preview-cfg/migrations   # 毎回作り直す（古いコピーや migrations/migrations の入れ子を残さない）
   cat > <tmp>/mise-preview-cfg/wrangler.jsonc <<'EOF'
   { "name": "mise-preview-tmp", "compatibility_date": "2025-01-01",
     "d1_databases": [{ "binding": "DB", "database_name": "mise-preview", "database_id": "<wrangler.jsonc の previews の database_id>", "migrations_dir": "migrations" }] }
   EOF
   ```
2. **未適用の確認**: `npx wrangler d1 migrations list mise-preview --remote -c <tmp>/mise-preview-cfg/wrangler.jsonc`（`-c` なしだと本番 `mise` の一覧と取り違えやすい）。
3. **バックアップ**: `npx wrangler d1 export mise-preview --remote --output <tmp>/backup-mise-preview.sql`（リポジトリにコミットしない）。
4. **適用**: `npx wrangler d1 migrations apply mise-preview --remote -c <tmp>/mise-preview-cfg/wrangler.jsonc`。
5. **検索用列の埋め直し**（0018・0020 以降の migration を入れたとき、またはサイトを足したとき）。**`refresh-search-text.mjs` は適用した migration を含むブランチのものを使う**。リポジトリ直下で実行する。実行中は DB が一時的に応答しない:
   ```sh
   node scripts/refresh-search-text.mjs --remote --db mise-preview
   ```
6. **確認**（SELECT だけ。リポジトリ直下）: `uv run python3 scripts/check-data-integrity.py --remote --db mise-preview` で error 検査がすべて 0 行（集計表の一致確認 6 本を含む）。カテゴリ数・孤児・未分類は `npx wrangler d1 execute mise-preview --remote --command "..."` で（期待: カテゴリ 153・孤児 0・未分類 0。数は migration で変わる）。

## プレビュービルド（ブランチごと）

Cloudflare の自動ビルドは、ブランチ（PR）ごとに `npx wrangler preview` でプレビュー用の Worker（Worker Previews。本番の Worker の枝）をデプロイする（ダッシュボードで確認済み、2026-11-14: プレビューブランチのビルドも Build コマンドは「なし」、プレビューコマンドは `npx wrangler preview`、ルートディレクトリ `/`。プレビュー URL は現在「無効」で、ビルドは実行されるが URL は有効化するまで作られない）。設定は `wrangler.jsonc` の `previews` ブロックで、**ブロックが無いと `Your Wrangler configuration is missing a previews block` でビルドが失敗する**（コードの不具合ではない）。

- `previews.d1_databases` で、プレビューの `DB` バインディングを**プレビュー専用の D1 `mise-preview`**（`database_id` は `wrangler.jsonc` 参照）に向ける。**本番 DB `mise` をプレビューから触らせない**ため（ブランチの未レビューのコードが本番データを読み書き・破壊しないように）。静的ファイル（`assets`）はトップレベルの設定が使われる。
- `mise-preview` は本番と同じ migrations（`migrations/` の全部）を適用して作る。**新しい migration を足したら `mise-preview` にも手動で適用する**（自動では適用されない。適用し忘れるとプレビューがスキーマ違いで落ちる）。手順は次の節。`mise-preview` のデータは確認用の複製で、本番とのずれは許容する。
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

## PWA の確認手順

実装は [architecture.md](../design/architecture.md#pwa)。ローカルで確認できるのは manifest の取得・SW 登録・オフライン表示（開発サーバーを止めてページ移動する）まで。デプロイ後に本番 HTTPS（`mise.llll-ll.com`）で次を確認する。

- `/manifest.webmanifest` が 200・`application/manifest+json`、`/sw.js` が 200・`Cache-Control: no-cache`（ローカル設定の整合は `npm run check` が検査）（Cloudflare を通っても変わらないこと）。
- DevTools の Application で manifest にエラーがなく、SW が activated で Cache Storage が空であること。
- PC の Chrome でインストールでき、スマホ（Android Chrome・iOS Safari）でホーム画面に追加できて、アイコン（maskable の切り抜き含む）が崩れず standalone で起動すること。
- 機内モードで起動すると「オフラインです」が出て、復帰後に通常表示へ戻ること。
- SW を変えるデプロイ後、再訪問で新しい `sw.js` に切り替わること（`skipWaiting` + `clients.claim`）。

## 未整備

- Workers Builds の GitHub 連携は設定済み（main へのマージで本番へ自動デプロイされ、PR ブランチでは preview ビルドが走る）。ダッシュボードで確認済み（2026-11-14）: Git リポジトリ `kako-jun/mise`、プロダクションブランチ `main`、ルートディレクトリ `/`。本番ビルドの Build コマンドは「なし」、Deploy コマンドは `npx wrangler deploy`。したがって main へのマージは migration を適用せず Worker だけをデプロイする（migration は事前に手動適用）。プレビュー（ブランチ）側は Build コマンドなし・プレビューコマンド `npx wrangler preview`・ルート `/` で、プレビュー URL は無効（上の「プレビュービルド」参照）。
- migration は自動適用されない。新しい migration を含む PR を main にマージすると、スキーマが旧いまま新 Worker が本番に出る恐れがある。**先に本番へ migration を適用してから、その後マージする**（後方互換の形にする。上の「本番へのデプロイ」参照）。
- 失敗時の通知は未整備。ロールバックは上の手順（`wrangler rollback`）。
- アクセシビリティの指摘は Issue #13 を参照。
- PWA（Issue #10）は実装済みだが、実スマホ・本番 HTTPS での確認は未実施（上の「PWA の確認手順」）。

## 秘密情報

`INTAKE_SALT`（申請の IP ハッシュの塩）は、**値をリポジトリ・ログ・Issue に書かない**。十分長いランダム文字列を作って Worker の secret に入れる（本番 Worker とプレビューの両方）:

```sh
openssl rand -hex 32 | npx wrangler secret put INTAKE_SALT          # 本番
```

ローカルは `.dev.vars`（`.gitignore` 済み）に `INTAKE_SALT=任意の文字列` を置く。塩を変えると既存の `ip_hash` は新しいハッシュと一致しなくなる（レート制限の履歴が切れるだけで害はない）。
