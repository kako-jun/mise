# データモデル

D1 の現行スキーマ（実装済み）と、将来案（未実装）を分けて書く。実装済みの正本は `migrations/`。日時は UTC の ISO 8601 文字列（例 `2026-11-12T00:00:00Z`）、不明値は `NULL`。

## 実装済み（現行 D1）

1 サイトを 1 レコードとし、1 サイトは複数カテゴリに所属できる。サイト内の個々のページを検索するインデックスはない。URL はサイトの識別材料であり、運営者の実体や所有権を保証しない。

### `sites`

| 列 | 意味 | 追加 |
|---|---|---|
| `id` | 主キー | 0001 |
| `url` | 訪問先 URL、必須・一意 | 0001 |
| `name` | 表示名（言語共通） | 0001 |
| `description_ja` | 日本語の一文紹介、必須 | 0001 |
| `description_en` | 英語の一文紹介、NULL 可。日本語に書かれた事実だけを訳し、情報を足さない | 0010 |
| `favicon_path` | 元サイトで確認したファビコンのローカルパス | 0002 |
| `registered_at` | mise への登録日時。INSERT 時に DB のトリガが記録する | 0003, 0006 |
| `last_crawled_at` | 最後に取得に成功した日時 | 0007 |
| `source_updated_at` | 元サイトの更新日（`YYYY-MM-DD`）。確認できた場合のみ | 0012 |
| `source_updated_evidence_url` | 更新日の根拠 URL | 0012 |
| `country_code` | 運営者の所在国（ISO 3166-1 alpha-2 大文字 2 文字、CHECK で形式を縛る） | 0011 |
| `region_code` | 運営者の所在県・州（ISO 3166-2 第 1 階層、例 `JP-13`）。`country_code` が必須で先頭 2 文字が一致すること（CHECK） | 0013 |

### インデックスと検索用列（0018）

- `idx_sites_sort_<ハッシュ>`: 一覧の既定の並びの式インデックス(名前の末尾は列定義のハッシュ。`SORT_KEYS` を変えると名前が変わるので、新しい migration で新名の CREATE と旧名の `DROP INDEX` を書く。`npm run check` が「最新の migration に現在の定義がある」ことを検査する)。**並びの定義は `src/sites.js` の `SORT_KEYS` 1 か所**で、`ORDER BY`・カーソルのキー列・「次のページ」の比較条件・インデックスはそこから生成する。並びのキーを足す/変えるとき（例: 官公庁台帳 #71 の `bulk_import` を先頭に足す）は、`SORT_KEYS` を直し、`sortIndexSql()` が出す `CREATE INDEX` を新しい migration に書く（`npm run check` が一致を検査する）。画面に出す列が増えるなら `COLUMNS` にも足す。カーソルはキー値の配列なので形式は変わらない。
- `idx_site_categories_category`: `(category_id, site_id)`。カテゴリ別一覧・件数の逆引き。
- `sites.search_text`: 検索用の正規化済みテキスト（名前・日英の紹介文・URL を NFKC + 小文字化）。`/api/sites?q=` はこの列を `instr()` で引く。migration は暫定値（SQL の `lower()` = ASCII のみ）を入れ、INSERT 時と、名前・紹介文・URL の UPDATE 時のトリガも同じ暫定値を入れる。比較は `instr()`（LIKE は D1 でパターンが約 50 バイトを超えると too complex になるため使わない）。全角半角・非 ASCII の大小を正しく扱うには、サイトを足した/直した後に `node scripts/refresh-search-text.mjs --local|--remote` で埋め直す。

### `categories` / `site_categories` / `site_tags`

- `categories`: `id`、`parent_id`（トップ階層は NULL）、`name_ja`、`name_en`、`sort_order`。
- `site_categories`: `(site_id, category_id)` が主キー。同じサイトを複数カテゴリに登録できる。
- `site_tags`（0007）: `(site_id, label_ja)` が主キー、`label_en` は NULL 可。表示のみで、検索対象ではない。

### 運営者の所在国（`country_code`）

一覧の国旗と地球の連動、および範囲（`/jp/` など。[ui.md](ui.md#範囲国)）のための値。`NULL` のサイトは範囲 `all` にだけ出る。（座標は持たない。国ごとの代表座標は静的表 `public/countries.js`）。取得内容・公開情報で運営者の所在国を確認できたものだけを入れる。**ドメイン（.jp 等）・言語・ホスティング・IP の所在だけでは決めない**。確認できなければ `NULL`（旗を出さない）。根拠は収集 JSON の `country_code` / `country_evidence` と migration のコメントに残す。

### 運営者の所在区画（`region_code`）

県・州まで分かったサイトだけ、旗の右に区画名を出し、地球を区画へ向けるための値。判定基準は国と同じく公開情報に明記されたもの（サイトの記載、公的機関の公式所在地）だけで、推測しない。確認できなければ `NULL`（旗のみ）。根拠は `data/region-evidence.json`（id・region_code・出典 URL・根拠・確認日）。区画名・座標の表は `public/regions.js`（生成物）。

### 日時の扱い

`source_updated_at` は元サイトの更新日で、巡回日時（`last_crawled_at`）とも登録日時（`registered_at`）とも別。一覧の既定の並びは `COALESCE(source_updated_at, registered_at)` の新しい順（[ui.md](ui.md#一覧検索結果の既定の並び)）。

### 制約と注意

- カテゴリ未所属のサイトも `GET /api/sites`（`category` 指定なし）と検索には出る（`category_ids` が空配列。画面では「未分類」）。カテゴリ別の一覧には出ない。
- 官公庁の台帳（`data/public-bodies/`）と frontier 台帳（`var/frontier.sqlite`）は D1 に入っていない。D1 に入るのは掲載したサイトだけ。

## 将来案（未実装）

以下は SQLite/D1 を想定した論理設計の案で、現行スキーマには存在しない。

### `sites` への追加列（案）

| 列 | 意味 |
|---|---|
| `active_description_id` | 現在表示する承認済み紹介文への参照 |
| `last_crawl_attempt_at` / `last_crawl_status` | 最後に巡回を試みた日時と結果（`success` / `failed` / `blocked` 等） |
| `created_at` / `updated_at` | mise 内のレコードの作成・更新日時 |

URL 正規化はスキームとホストの小文字化など安全な範囲に限り、パスの大小文字やクエリは削らない。リダイレクト後の URL・ミラー・同一サイトの判定規則は未決。

### `site_descriptions`（案）— 紹介文と生成・確認履歴

`site_id`、`text`、`lang`（ja/en）、`origin`（`manual` / `agent`）、`status`（`draft` / `approved` / `rejected` / `superseded`）、`source_url`、`source_checked_at`、`generator`（`origin=agent` なら必須）、`created_at`、`reviewed_at`。公開用に選べるのは `approved` のみ。確認者の記録や自動生成物をどの条件で公開するかは未決。この状態名は仕組みを設計するための案で、既存サイトにレビュー済みのものがあるという意味ではない。現行の紹介文は収集した内容に基づく案を手元で作ったもので、審査済みの承認データではない。

### `site_locations`（案）— 場所の主張と根拠

サイトに関連する場所を 0 件以上、根拠付きで持つ案。`location_role`（`operator` / `service_area` / `unknown`）、`country_code`、`latitude` / `longitude`（両方 NULL か両方有効値）、`basis`（`explicit` / `inferred` / `unknown`）、`evidence_url`、`evidence_note`、`checked_at`。`basis=unknown` では国・座標・根拠を NULL とし、不明を 0 や架空の座標で埋めない。サイトのドメイン・言語・サーバーや IP の所在だけで運営者の所在地を確定しない。非公開の個人住所や精密座標をむやみに収集しない。旗の表示は `sites.country_code` を使い、`site_locations` は使わない。

### 画像（サムネイル）

[ideas/browser-and-thumbnails.md](../ideas/browser-and-thumbnails.md) を参照。

### 状態遷移・申請

掲載の状態（`listed` / `down` / `long_down` / `suspect` …）と申請（`add` / `remove` / `correct`）のデータは未設計。[ideas/exchange-and-deliberation.md](../ideas/exchange-and-deliberation.md)、[ideas/requests.md](../ideas/requests.md)。

## 未決事項

- 利用 DB の ID 方式、スキーマ移行、サイトとページの境界、正規 URL・重複・リダイレクトの扱い。
- カテゴリ名変更時の共有 URL 互換性、複数分類や更新時の確認方法、カテゴリ削除時の扱い。
- 紹介文の生成タイミング、複数出典、承認・差し替え・削除の運用。
- 所在地の取得対象、推定の許容範囲、精度、位置情報の公開基準と削除対応。
- 巡回頻度、削除やアクセス拒否の扱い。
