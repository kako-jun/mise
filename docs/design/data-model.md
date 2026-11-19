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
| `favicon_path` | 自前取得した 32px WebP の内容ハッシュ名（`<hash>.webp`）。取れていなければ NULL。取得・更新は [collection/favicons.md](../collection/favicons.md)（配信先の R2 は未実装） | 0002 |
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
- `idx_site_tags_label_ja`（0023）`(label_ja, site_id)` と `idx_site_tags_label_en`（0024）`(label_en, site_id)`: タグ名 → サイトの逆引き（#126 第2段）。稀なタグの絞り込みを、全サイトの走査（10 万件で 85 万行）でなくタグの索引から引く（約 300 行）。**1 本 1 migration**（書き込みは索引の行数ぶん = `site_tags` の行数。10 万件で各約 24 万行、無料プランに戻した場合の 1 日 10 万行を超えるので、その場合は日を分けて適用する。Workers Paid（契約済み）なら書き込みは月 5,000 万行込みで 1 日に分ける必要はない）。**索引はコードより先に入れる**（索引なしでコードだけ出すと、結果は同じでも読み取りが main の約 4 倍。索引どうしの順序には依存しない）。派生物（消すには `DROP INDEX`）。
- `idx_sites_activity`（0025）`(bulk_import, date(registered_at))`: `/api/activity` の直近 7 日の日別登録数（10 万件で 8 万行 → 約 270 行）。書き込みは `sites` の行数ぶん。派生物。
- `sites.search_text`: 検索用の正規化済みテキスト（名前・日英の紹介文・URL・タグ（日本語名・英語名。`label_ja` 昇順（BINARY、大文字が先）で「日本語名 英語名」を並べる。migration のトリガと refresh は同じ順）を空白区切りで続けて NFKC + 小文字化。#31）。`/api/sites?q=` はこの列を `instr()` で引く。migration は暫定値（SQL の `lower()` = ASCII のみ）を入れ、INSERT 時と、名前・紹介文・URL の UPDATE 時のトリガも同じ暫定値を入れる（`search_text` を明示して渡した INSERT/UPDATE は上書きしない）。`site_tags` の INSERT/UPDATE/DELETE にもトリガ（0020）があり、そのサイトの `search_text` を暫定値で作り直す（タグ側の操作は `search_text` を明示できないので常に作り直す。NFKC 版へは埋め直しで戻す）。比較は `instr()`（LIKE は D1 でパターンが約 50 バイトを超えると too complex になるため使わない）。全角半角・非 ASCII の大小を正しく扱うには、サイトを足した/直した後に `node scripts/refresh-search-text.mjs --local|--remote` で埋め直す。 `instr()` は全件スキャンで、**総件数は上限付き**（`TOTAL_CAP`。ヒットが上限を超える語は途中で数えるのをやめる）にしてあるが、ヒットが少ない・0 の語は全件走査 1 回が残る（10 万件で 10 万行）。これを減らす FTS5（trigram）は #126 第3段。

### `categories` / `site_categories` / `site_tags`

- `categories`: `id`、`parent_id`（トップ階層は NULL）、`name_ja`、`name_en`、`sort_order`。木の中身と育て方は下の「カテゴリの木」。
- `site_categories`: `(site_id, category_id)` が主キー。同じサイトを複数カテゴリに登録できる。
- **カテゴリ所属の正規形（#137）**: 1 サイトの所属は、親と子の両方が付いたら**子だけ**（祖先の除去。親の件数は集計表が子孫を数えるので変わらない）。**退避先（`business` / `society` / `business-services` / `community`。0021 が旧カテゴリから仮置きしたもの。定義の正本は `data/category-map.csv` の `fallback` 行）は外さない**: 新しい木ではこれも LLM が選べる正規のカテゴリ（`assign=llm`）で、再判定済みの付与は LLM の選択だから。旧 id の fallback が付けた退避先は他に何も残らないときだけ（単独）なので、もともと実カテゴリと並ばない。官公庁 `pb-` は `government` 直下の種別葉だけで、この規則の対象外。新しい判定は `judge.normalize_categories` で正規形にしてから台帳・`site_categories` に入り、既存分は一度きりの `scripts/fix-redundant-categories.sql` で整理する。破れていないかは `scripts/check-data-integrity.py`（検査一覧は [llm-judge.md](../collection/llm-judge.md#データ整合チェック137)）。
- `site_tags`（0007）: `(site_id, label_ja)` が主キー、`label_en` は NULL 可。表示と、`/api/sites?tag=` の絞り込み（`label_ja` か `label_en` の完全一致。複数は AND）に使う。タグ名（日英）は `q` の部分一致の対象でもある（`search_text` に含める。0020・#31 第1段）。

### カテゴリ件数の集計表（#126 第1段、0022）

- **正本は `sites` / `site_categories` / `categories`。次の 3 表は派生物で、トリガが常に追従させる**（ずれたら 0022 の「初期集計」と同じ文で作り直せる。`tests/category-counts.test.mjs` が「集計表 = 再帰 CTE」を検査する）。
  - `category_closure(ancestor_id, descendant_id)`: カテゴリの祖先 → 子孫（自分自身を含む）。307 行（現在 153 カテゴリ・深さ 3）。
  - `category_counts(category_id, scope, n)`: **そのカテゴリ自身と全子孫の所属サイト数**（同じサイトが複数の子孫に属しても 1 件）。`scope` は `'all'`（全世界。`ALL_SCOPE`）・`country_code`（その国のサイトだけ。`country_code` が NULL のサイトは `'all'` のみ）・**`region_code`（その区画のサイトだけ。例 `JP-13`。0026・#127）**。区画は国の部分集合（`region_code` は `country_code` が必須）で、国だけ分かって `region_code` が NULL のサイトは `'all'` と国にだけ数える。国コード（2 文字）と区画コード（ハイフンを含む）は形が違うので衝突しない。旧 `loadCategories` の再帰 CTE（`COUNT(DISTINCT s.id)`）と同じ意味。行が無い・`n = 0` は「空」で、読み側が 0 とみなす。
  - `scope_counts(scope, n)`: 範囲（`'all'`・国・区画）ごとのサイト数（`/api/scopes`・`/api/activity` の `total`・範囲だけを指定した `/api/sites` の `total`）。`n = 0` の行は読み側が除く。
- **読み取り**: `loadCategories`（`/api/categories`・SSR・sitemap）は `categories LEFT JOIN category_counts`、`loadScopes` は `scope_counts`。sitemap は範囲ごとに数え直さず、`category_counts` の `n > 0` を 1 回で読む。`/api/sites` の `total` は、範囲だけなら `scope_counts`、**直下が葉のカテゴリ**だけなら `category_counts`（葉は子孫が無いので直下の所属数と同じ）から正確に読む。葉でないカテゴリ（直下の所属を集計表で出せない）・`q`・`tag` は上限付きで数える（`TOTAL_CAP`。第2段）。
- **トリガ**（書き込み量の見積りは migration 冒頭のコメント）: `site_categories` の INSERT / DELETE（「そのサイトの、祖先の部分木に入る所属行がちょうど 1 件」の祖先だけ +1、「消した後に 0 件」の祖先だけ -1 にして二重カウントを避ける）・UPDATE（旧・新の祖先を再計算）、`sites` の INSERT / DELETE / `country_code`・`region_code` の UPDATE（国も区画も、新旧で変わった範囲だけ付け替える）、`categories` の INSERT / `parent_id` の UPDATE（閉包表を付け替え、旧親・新親の祖先の件数を再計算）/ DELETE。1 サイトの投入は通常 +3〜+14 行の書き込み（国あり・カテゴリ 1〜3 個）。区画ありはさらに最大 +3 行（0026）。夜間の一括投入（`export_approved_sql.py` の `INSERT` と `--update-categories` の `DELETE` + `INSERT OR IGNORE`）でも同じトリガが働く。
- **作り直し**: ずれたら `scripts/rebuild-category-counts.sql`（3 表を `DELETE` → 0022 と 0026（区画）の初期集計と同じ `INSERT`）で正本から作り直せる。手順・戻し方・一致確認のクエリは `docs/operations/deploy.md`。
- **カテゴリの木を変えるときの制約**（migration で `categories` を INSERT / UPDATE / DELETE すれば閉包表と件数は追従するが、次を守る）:
  - **親を先に INSERT する**（子の INSERT トリガは、親の閉包表の行を引いて祖先を作るため。親が無いと子の祖先が欠ける）。
  - **カテゴリ id の変更（`UPDATE categories SET id`）は非対応**（閉包表・件数表・`site_categories` が旧 id を指したままになる）。作り直す場合は新 id を INSERT して所属を移し、旧 id を DELETE する。
  - **親の変更や `site_categories` 行の UPDATE は、旧親・新親の祖先の部分木を数え直す**ので、10 万件規模の一括移行（#127 第2段など）では重い。大量の付け替えは、トリガに任せず、`scripts/rebuild-category-counts.sql` で最後に 1 回作り直す形を検討する。
  - **`INSERT OR REPLACE` は使わない**（`sites` / `site_categories` / `categories` のどれも。再帰トリガが無効な既定（`recursive_triggers=0`）では、REPLACE が消す行の DELETE トリガが発火せず、集計がずれる）。`INSERT OR IGNORE` か `DELETE` + `INSERT`、`ON CONFLICT DO UPDATE` を使う。
- 効果（10 万件、ローカル D1 の実測）: `/api/categories` 82 万 → 459 行、トップ（SSR）112 万 → 499 行、sitemap 1,670 万 → 3,262 行。

### カテゴリの木（#114 第1段、0021）

- **正本は `data/categories.csv`**（`id, parent_id, name_ja, name_en, sort_order, assign`。現在 153 カテゴリ: トップ 15・第2階層 122・第3階層 16）。D1 の `categories` はこの写しで、`tests/test_taxonomy.py` が「全 migration を適用した結果 = CSV」を検査する。**木を変える migration を足すときは CSV も同じ変更にする**。LLM 判定の選択肢（`scripts/llm/judge.py` の `load_categories`）もこの CSV から読む。
- 旧 59 → 新の対応表は `data/category-map.csv`（`keep` 同じ id を引き継ぐ / `map` 常にその新 id へ / `fallback` 主題でなく運営主体・地域だった旧カテゴリで、退避先でない新カテゴリ（keep/map/rule）を 1 つも持たないサイトにだけ、退避先をすべて付ける / `drop` 0 件で廃止 / `rule` 官公庁。LLM 側の旧 id 互換では行政・公的機関 `government`（トップ）に写す）。0021（`scripts/taxonomy_sql.py` が生成した一回限りの migration。適用後は凍結）が使い、LLM 側は旧 id の判定を新 id に写すのに使う（[llm-judge.md](../collection/llm-judge.md#カテゴリの木と指示文)）。
- **主題の木**: カテゴリは「そのサイトが何について書いているか・何を扱っているか」。運営主体（企業・団体・個人サイト・店）は木に置かず、`site_kind` とタグで扱う。店は扱う品・サービスの主題、学校は教育、病院は健康に入れる。国・地域は木に入れない（国は範囲、県は第2段で範囲の第2階層）。1 サイトは最大 3 カテゴリ。合う葉が無いときだけ親に置く。
- **id 規則**: 英語の kebab-case の短い slug。**親の id を前に付けない**（親子は `parent_id` だけで持つ。ノードを移しても id と URL が変わらない）。一意にならないときだけ語を足す。**`all` と 2 文字の id は使わない**（範囲の URL `/all/` `/jp/` と衝突する）。トップのうち `arts business reference education science government health society entertainment`（9 個）は旧 id を引き継ぎ、それ以外の旧 id（`親-子` 形式）は木に残らない。日本語名は 12 字程度まで、「・」で並べるのは 2〜3 語まで。英語名は sentence case。
- **官公庁**: `government`（行政・公的機関）の下に種別 7 葉（`national-agencies` `independent-agencies` `prefectures` `cities` `special-wards` `towns` `villages`）。台帳の `type`（0015 が各サイトにタグとして付けたもの）から決定論で振り、LLM には渡さない（`assign=rule`）。台帳に無い公的機関（公社など）は LLM が `government`（トップ）か主題を選ぶ。以後の官公庁の差分 migration（`public_bodies_sql.py --since`）も種別の葉を付ける（政令市の行政区は既定では投入せず、投入した場合は 0021・`--since` とも `cities` に寄せる）。既知の限界: 台帳で団体の種別が変わったとき（町→市など）の葉・タグの付け替えは未対応（#121）。
- **成長則**（葉の件数は、範囲を「全世界」にしたときの `/api/categories?scope=all` の `site_count`、つまり配下を含む件数で見る）:
  - **例外: `assign=rule` の葉（官公庁の種別 7 葉）は成長則の対象外**。件数は台帳で決まり、分け方は主題でなく地域なので、子は足さない。多くなった種別（`cities` `towns` など）は、第2段の都道府県の範囲（`/jp/` の第2階層）で分ける。
  - 葉が **50 件を超えたら分割を検討**する。**100 件を超えたら次の追加ラウンドの前に必ず子を足す**。
  - 分けるときは、その葉の中で多い主題を 3〜8 個の子にする。子に入らない残りは親に置いたままでよい。
  - 深さは現在最大 3 だが、**3 を超えてもよい**（件数が必要とする所だけ深くする。トップは 15 のまま保つ）。UI・URL・件数の再帰集計は任意の深さで動く。
  - 0 件の葉は残してよい（画面は薄く表示する）。3 か月以上 0 件のままなら統合の候補にする。
  - 木を変えるときは、新しい migration で子を INSERT し、既存サイトの `site_categories` を移す（移す前に、移す先の件数を確認する）。同時に `data/categories.csv` と LLM の指示文の一覧が自動で追従することを確かめる。
  - 件数の見方（例）: `curl -s 'https://mise.llll-ll.com/api/categories?scope=all'` の `site_count` が 50・100 を超える葉を探す。

### 運営者の所在国（`country_code`）

一覧の国旗と地球の連動、および範囲（`/jp/` など。[ui.md](ui.md#範囲国都道府県)）のための値。`NULL` のサイトは範囲 `all` にだけ出る。（座標は持たない。国ごとの代表座標は静的表 `public/countries.js`）。取得内容・公開情報で運営者の所在国を確認できたものだけを入れる。**ドメイン（.jp 等）・言語・ホスティング・IP の所在だけでは決めない**。確認できなければ `NULL`（旗を出さない）。根拠は収集 JSON の `country_code` / `country_evidence` と migration のコメントに残す。

### 運営者の所在区画（`region_code`）

県・州まで分かったサイトだけ、旗の右に区画名を出し、地球を区画へ向けるための値。**範囲（scope）として県で絞る軸でもある（#127、0026）**: `/api/*?scope=jp-13`（`JP-13` も可）・URL `/jp/13/` は `sites.region_code = 'JP-13'` のサイトだけ。新しい列は無く、集計表の scope 値に区画が増えただけ（上の「カテゴリ件数の集計表」）。一覧の絞り込みは部分索引 `idx_sites_region_sort`（0026。`(region_code, bulk_import, 新しい順, id) WHERE region_code IS NOT NULL`。並びは `SORT_KEYS` と同じで、区画は全体の一部なので並びの索引を走査して絞ると他の区画の行を読み飛ばす。`regionSortIndexSql()` が定義の正本で、`scripts/check-api-headers.mjs` が migration との一致を検査）。区画が NULL のサイトは国の範囲にだけ出る（区画で絞る価値が出るのは `region_code` を持つサイト。官公庁の名簿 `pb-` は大半が区画を持つ）。ピックアップは区画内の一括投入でないサイトから、`s.region_code = ?` の等値だけの `ORDER BY RANDOM()`（`randomPickSql`。`(? IS NULL OR …)` の形にすると索引が使われない。tests/region-scope.test.mjs が EXPLAIN で検査）で選ぶので、区画に一括投入しか無ければ空になる。判定基準は国と同じく公開情報に明記されたもの（サイトの記載、公的機関の公式所在地）だけで、推測しない。確認できなければ `NULL`（旗のみ）。根拠は `data/region-evidence.json`（id・region_code・出典 URL・根拠・確認日）。区画名・座標の表は `public/regions.js`（生成物。ブラウザと Worker の SSR が共有するので `globalThis.MISE_REGIONS`）。

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
