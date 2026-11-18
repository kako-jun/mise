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
| `bulk_import` | 台帳などからの一括投入の印（1）。直近登録の集計（`/api/activity`）・NEW タグ・既定の並びの先頭から外す。この場合の `registered_at` は出典の確認日 | 0015 |
| `last_crawled_at` | 最後に取得に成功した日時 | 0007 |
| `source_updated_at` | 元サイトの更新日（`YYYY-MM-DD`）。確認できた場合のみ | 0012 |
| `source_updated_evidence_url` | 更新日の根拠 URL | 0012 |
| `country_code` | 運営者の所在国（ISO 3166-1 alpha-2 大文字 2 文字、CHECK で形式を縛る） | 0011 |
| `region_code` | 運営者の所在県・州（ISO 3166-2 第 1 階層、例 `JP-13`）。`country_code` が必須で先頭 2 文字が一致すること（CHECK） | 0013 |

### インデックスと検索用列（0018）

- `idx_sites_sort_<ハッシュ>`: 一覧の既定の並びの式インデックス(名前の末尾は列定義のハッシュ。`SORT_KEYS` を変えると名前が変わるので、新しい migration で新名の CREATE と旧名の `DROP INDEX` を書く。`npm run check` が「最新の migration に現在の定義がある」ことを検査する)。**並びの定義は `src/sites.js` の `SORT_KEYS` 1 か所**で、`ORDER BY`・カーソルのキー列・「次のページ」の比較条件・インデックスはそこから生成する。並びのキーを足す/変えるとき（例: 官公庁台帳 #71 で先頭に足した `bulk_import`。旧 `idx_sites_sort_2489145a` は 0019 で DROP し `idx_sites_sort_9b34c1ec` を作った）は、`SORT_KEYS` を直し、`sortIndexSql()` が出す `CREATE INDEX` を新しい migration に書く（`npm run check` が一致を検査する）。画面に出す列が増えるなら `COLUMNS` にも足す。カーソルはキー値の配列なので形式は変わらない。
- **`SORT_KEYS` を変えたときのチェックリスト**: (1) 新しい migration に `sortIndexSql()` の新名の `CREATE INDEX` と、旧名の `DROP INDEX` を書く。(2) `mise-preview` にも適用する。旧名の DROP 漏れは自動検出できないので手で確かめる。
- `idx_site_categories_category`: `(category_id, site_id)`。カテゴリ別一覧・件数の逆引き。
- `sites.search_text`: 検索用の正規化済みテキスト（名前・日英の紹介文・URL・タグ（日本語名・英語名。`label_ja` 昇順（BINARY、大文字が先）で「日本語名 英語名」を並べる。migration のトリガと refresh は同じ順）を空白区切りで続けて NFKC + 小文字化。#31）。`/api/sites?q=` はこの列を `instr()` で引く。migration は暫定値（SQL の `lower()` = ASCII のみ）を入れ、INSERT 時と、名前・紹介文・URL の UPDATE 時のトリガも同じ暫定値を入れる（`search_text` を明示して渡した INSERT/UPDATE は上書きしない）。`site_tags` の INSERT/UPDATE/DELETE にもトリガ（0020）があり、そのサイトの `search_text` を暫定値で作り直す（タグ側の操作は `search_text` を明示できないので常に作り直す。NFKC 版へは埋め直しで戻す）。比較は `instr()`（LIKE は D1 でパターンが約 50 バイトを超えると too complex になるため使わない）。全角半角・非 ASCII の大小を正しく扱うには、サイトを足した/直した後に `node scripts/refresh-search-text.mjs --local|--remote` で埋め直す。 `instr()` は全件スキャンなので、サイト数が今より一桁増えたら FTS などの索引方式を見直す。

### `categories` / `site_categories` / `site_tags`

- `categories`: `id`、`parent_id`（トップ階層は NULL）、`name_ja`、`name_en`、`sort_order`。木の中身と育て方は下の「カテゴリの木」。
- `site_categories`: `(site_id, category_id)` が主キー。同じサイトを複数カテゴリに登録できる。
- `site_tags`（0007）: `(site_id, label_ja)` が主キー、`label_en` は NULL 可。表示と、`/api/sites?tag=` の絞り込み（`label_ja` か `label_en` の完全一致。複数は AND）に使う。タグ名（日英）は `q` の部分一致の対象でもある（`search_text` に含める。0020・#31 第1段）。

### カテゴリの木（#114 第1段、0021）

- **正本は `data/categories.csv`**（`id, parent_id, name_ja, name_en, sort_order, assign`。現在 153 カテゴリ: トップ 15・第2階層 122・第3階層 16）。D1 の `categories` はこの写しで、`tests/test_taxonomy.py` が「全 migration を適用した結果 = CSV」を検査する。**木を変える migration を足すときは CSV も同じ変更にする**。LLM 判定の選択肢（`scripts/llm/judge.py` の `load_categories`）もこの CSV から読む。
- 旧 59 → 新の対応表は `data/category-map.csv`（`keep` 同じ id を引き継ぐ / `map` 常にその新 id へ / `fallback` 主題でなく運営主体・地域だった旧カテゴリで、他に新カテゴリが無いサイトにだけ退避先を付ける / `drop` 0 件で廃止 / `rule` 官公庁）。0021（`scripts/taxonomy_sql.py` が生成した一回限りの migration。適用後は凍結）が使い、LLM 側は旧 id の判定を新 id に写すのに使う（[llm-judge.md](../collection/llm-judge.md#カテゴリの木と指示文)）。
- **主題の木**: カテゴリは「そのサイトが何について書いているか・何を扱っているか」。運営主体（企業・団体・個人サイト・店）は木に置かず、`site_kind` とタグで扱う。店は扱う品・サービスの主題、学校は教育、病院は健康に入れる。国・地域は木に入れない（国は範囲、県は第2段で範囲の第2階層）。1 サイトは最大 3 カテゴリ。合う葉が無いときだけ親に置く。
- **id 規則**: 英語の kebab-case の短い slug。**親の id を前に付けない**（親子は `parent_id` だけで持つ。ノードを移しても id と URL が変わらない）。一意にならないときだけ語を足す。**`all` と 2 文字の id は使わない**（範囲の URL `/all/` `/jp/` と衝突する）。トップのうち `arts business reference education science government health society` は旧 id を引き継ぎ、それ以外の旧 id（`親-子` 形式）は木に残らない。日本語名は 12 字程度まで、「・」で並べるのは 2〜3 語まで。英語名は sentence case。
- **官公庁**: `government`（行政・公的機関）の下に種別 7 葉（`national-agencies` `independent-agencies` `prefectures` `cities` `special-wards` `towns` `villages`）。台帳の `type`（0015 が各サイトにタグとして付けたもの）から決定論で振り、LLM には渡さない（`assign=rule`）。台帳に無い公的機関（公社など）は LLM が `government`（トップ）か主題を選ぶ。以後の官公庁の差分 migration（`public_bodies_sql.py --since`）も種別の葉を付ける。
- **成長則**（葉の件数は、範囲を「全世界」にしたときの `/api/categories?scope=all` の `site_count`、つまり配下を含む件数で見る）:
  - 葉が **50 件を超えたら分割を検討**する。**100 件を超えたら次の追加ラウンドの前に必ず子を足す**。
  - 分けるときは、その葉の中で多い主題を 3〜8 個の子にする。子に入らない残りは親に置いたままでよい。
  - 深さは現在最大 3 だが、**3 を超えてもよい**（件数が必要とする所だけ深くする。トップは 15 のまま保つ）。UI・URL・件数の再帰集計は任意の深さで動く。
  - 0 件の葉は残してよい（画面は薄く表示する）。3 か月以上 0 件のままなら統合の候補にする。
  - 木を変えるときは、新しい migration で子を INSERT し、既存サイトの `site_categories` を移す（移す前に、移す先の件数を確認する）。同時に `data/categories.csv` と LLM の指示文の一覧が自動で追従することを確かめる。
  - 件数の見方（例）: `curl -s 'https://mise.llll-ll.com/api/categories?scope=all'` の `site_count` が 50・100 を超える葉を探す。

### 運営者の所在国（`country_code`）

一覧の国旗と地球の連動、および範囲（`/jp/` など。[ui.md](ui.md#範囲国)）のための値。`NULL` のサイトは範囲 `all` にだけ出る。（座標は持たない。国ごとの代表座標は静的表 `public/countries.js`）。取得内容・公開情報で運営者の所在国を確認できたものだけを入れる。**ドメイン（.jp 等）・言語・ホスティング・IP の所在だけでは決めない**。確認できなければ `NULL`（旗を出さない）。根拠は収集 JSON の `country_code` / `country_evidence` と migration のコメントに残す。

### 運営者の所在区画（`region_code`）

県・州まで分かったサイトだけ、旗の右に区画名を出し、地球を区画へ向けるための値。判定基準は国と同じく公開情報に明記されたもの（サイトの記載、公的機関の公式所在地）だけで、推測しない。確認できなければ `NULL`（旗のみ）。根拠は `data/region-evidence.json`（id・region_code・出典 URL・根拠・確認日）。区画名・座標の表は `public/regions.js`（生成物）。

### 日時の扱い

`source_updated_at` は元サイトの更新日で、巡回日時（`last_crawled_at`）とも登録日時（`registered_at`）とも別。一覧の既定の並びは `COALESCE(source_updated_at, registered_at)` の新しい順（[ui.md](ui.md#一覧検索結果の既定の並び)）。

### 制約と注意

- カテゴリ未所属のサイトも `GET /api/sites`（`category` 指定なし）と検索には出る（`category_ids` が空配列。画面では「未分類」）。カテゴリ別の一覧には出ない。
- 官公庁の台帳（`data/public-bodies/`）が正本で、D1 へは `scripts/public_bodies_sql.py` が生成する migration（`0015`）で入れる（[public-bodies.md](../collection/public-bodies.md)）。frontier 台帳（`var/frontier.sqlite`）は D1 に入っていない。D1 に入るのは掲載したサイトだけで、LLM 判定の承認済み候補は `scripts/llm/export_approved_sql.py` が出す SQL で入れる（[llm-judge.md](../collection/llm-judge.md#台帳への取り込みと承認)）。

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

掲載の状態（`listed` / `down` / `long_down` / `suspect` …）のデータは未設計。申請は `requests`（`contact` 列は連絡先の廃止後は未使用で常に NULL）、拒否リストは `blocked_hosts`（[operations/requests.md](../operations/requests.md)）。[ideas/exchange-and-deliberation.md](../ideas/exchange-and-deliberation.md)、[ideas/requests.md](../ideas/requests.md)。

## 未決事項

- 利用 DB の ID 方式、スキーマ移行、サイトとページの境界、正規 URL・重複・リダイレクトの扱い。
- カテゴリ名変更時の共有 URL 互換性（#114 の決定: 旧カテゴリの id を含む URL は無効値として扱い、トップに戻す。転送表は持たない）、複数分類や更新時の確認方法。
- 紹介文の生成タイミング、複数出典、承認・差し替え・削除の運用。
- 所在地の取得対象、推定の許容範囲、精度、位置情報の公開基準と削除対応。
- 巡回頻度、削除やアクセス拒否の扱い。
