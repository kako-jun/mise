# アーキテクチャ

## 技術構成

- **Cloudflare 内**で配信、API、データベースを扱う。
- API とサーバー側処理は **Cloudflare Workers + Hono**（`src/index.js`、`src/seo.js`）。
- データベースは **Cloudflare D1**（`migrations/`）。
- 画面は **HTML / CSS / JavaScript**（`public/`）。UI フレームワークは使わない。Astro は採用しない。
- 3D は **Three.js**。ソースは `src/client/globe.js`、three 本体は `vendor/three.module.js`（同梱）。`npm run build:globe`（esbuild で minify・tree-shake）が `public/globe.js` を生成する。globe を変えたら再ビルドして生成物もコミットする（直接編集しない）。`public/land-110m.geojson` は `scripts/slim-land.mjs` で軽量化済み（小数 2 桁・property 除去）。
- 配信: `wrangler.jsonc` の `assets.run_worker_first` に `/*` と、静的ファイルの拡張子ごとの除外（`!/*.js` など）を指定する。`/api/*`・`/`・範囲ページ（`/jp/` `/us/` `/all/`、末尾スラッシュなしは `/jp/` へ 301）・`/sitemap.xml` は Worker が返し、それ以外（`/robots.txt`・`/style.css` など）は `public/` の静的ファイル。`public/` に新しい拡張子の静的ファイルを足すときは除外も足す（[seo.md](seo.md)）。範囲ページは `/jp/` のように階層の下から返るため、`index.html` は CSS・JS・地球儀の資源をすべて絶対パス（`/style.css` など）で参照する。

## API

| エンドポイント | 内容 |
|---|---|
| `GET /api/scopes` | 範囲として選べる国（サイトが 1 件以上ある国、件数の多い順）・区画（サイトが 1 件以上ある区画、コード順。#127）と全体の件数。`{total, countries:[{code, count}], regions:[{code, count}]}`。集計表 `scope_counts` から読む（[data-model.md](./data-model.md) の「カテゴリ件数の集計表」） |
| `GET /api/categories?scope=` | カテゴリの木。サイト件数は自身と全子孫の所属サイトを数える（集計表 `category_counts` から読む。再帰 CTE で数え直さない）。`scope` は `jp` `us` などの国コード（小文字）、区画 `jp-13` `us-ca`（`JP-13` のように区画だけは大文字も可。#127）、または `all`（省略は `all`）。件数はその範囲内だけ。範囲内に 0 件のカテゴリも返す。不正な `scope`（`jp-` `jp-1234` `JP` など）は 400。サイトの無い区画（`jp-99`）はエラーにせず 0 件で返す |
| `GET /api/sites?scope=&category=&q=&tag=&limit=&cursor=&page=` | サイトの 1 ページ分（`{ items, next_cursor, total, total_capped }`）。カーソル方式: `limit`（既定 40、1〜100。100 超は 100 に丸め、0・負数・数字以外は 400）と、前の応答の `next_cursor`（並びのキー値の配列を JSON → base64url にした不透明な文字列。不正は 400）。`next_cursor` が `null` なら末尾。`total` と `total_capped` は先頭の応答（`cursor` なし）にだけ入る。**総件数は上限付き**: `q`・`tag`・葉でないカテゴリの絞り込みでは `TOTAL_CAP`（1,000。`src/sites.js`）件までしか数えず、超えると `total = 1000`・`total_capped = true`（画面は「1,000+」）。末尾まで届いた先頭ページ（0 件を含む）、範囲（国）だけの絞り込み（集計表 `scope_counts`）、直下が葉のカテゴリだけの絞り込み（集計表 `category_counts`）は常に正確（`total_capped = false`）。詳しくは下の「読み取りの設計」。`category` はそのカテゴリ直下（存在しない id は 404）、`q` は名前・紹介文・URL・タグ名（日英）の部分一致（検索はサーバー側。NFKC + 小文字化、100 文字まで）。`tag` はタグの日本語名か英語名のどちらでも完全一致で絞る（複数指定は AND、最大 5 個。6 個以上、または 1 個が 101 文字以上は 400）。`page`（1〜999999、範囲外は 400）は開始位置の指定で `cursor` が無いときだけ有効（`?page=N` の直リンク用）。範囲で絞る（`country_code` が NULL のサイトは `all` にだけ出る）。各サイトに所属カテゴリ `category_ids`（空配列は「未分類」）。実装は `src/sites.js`（SSR と共有）。並びは [ui.md](ui.md#一覧検索結果の既定の並び) |
| `GET /api/picks?scope=` | ピックアップ用に範囲内からランダム 3 件（`/api/sites` と同じ形） |
| `GET /api/activity?scope=` | 掲載総数と直近 7 日の日別登録数（HUD 用。範囲内だけ） |
| `POST /api/requests` | 申請（推薦・取り下げ・訂正）の受付のみ。同一オリジンの JSON（連絡先は受け付けない）。常に `no-store`。仕様・レート制限は [operations/requests.md](../operations/requests.md) |
| `GET /` `/:scope/` `/:scope` `/:scope/:sub/` `/:scope/:sub` | `index.html` に範囲と URL に応じた内容を差し込んで返す（[seo.md](seo.md)）。`/` は既定範囲（日本）と同内容。区画は `/jp/13/`。サイトが 1 件も無い国・区画・不正な値は 404、大文字は小文字へ 301 |
| `GET /sitemap.xml` | D1 から動的生成 |

`/api/*` は（範囲内の）全件返しで、成功応答に `Cache-Control: public, max-age=30`、4xx/5xx に `no-store` を付ける（更新が速やかに反映されるよう短く。ページング・カーソル化は構想: [ideas/scope-and-search.md](../ideas/scope-and-search.md)）。ブラウザ用で、外部向け公開 API としては設計していない。

## 読み取りの設計（D1 の課金と無料枠。#126）

D1 は**スキャンした行数**（`rows_read`）で課金し、Free は 1 日 500 万行・書き込み 1 日 10 万行までで超えるとクエリが失敗する。Cloudflare のプランは未確認なので、有料前提にせず**無料枠でも破綻しない**ことを目標にする。原則は「**1 画面あたりの読み取り行数が、サイトの件数に依らず一定**」:

1. **集計表**（0022）: カテゴリ・範囲の件数は全件を数えず表から読む（[data-model.md](data-model.md)）。
2. **キーセットのページング**: 次ページは `cursor`（並びのキー値）で続きから。1 つの `A OR B OR C` の条件にすると SQLite は並びの索引を先頭から走査してしまう（深さに比例。実測 9,000 件目の次で 3.4 万行）ので、`cursorTerms()`（`src/sites.js`）が項ごとの範囲条件（前のキーが等しい + このキーが後ろ）に分け、項を並びの順に前から取って連結する（各項は索引を直接引ける）。各項の `ORDER BY` はその項で決まっていないキーだけにする（全キーで並べると、同じ日時が大量に続く一括投入の塊で一時 B-tree になり、同値の行を毎回全部読む）。IN で引く計画（下の 4）のときだけ、範囲条件を足すと索引の走査に切り替わって IN が効かなくなるので、1 本の `A OR B OR C`（`afterCursor()`）を使う（ただし稀なタグでも該当が `TAG_IN_MAX` = 300 件を超えるときは、続きのページでは IN を使わず `EXISTS` に切り替える。ページごとに該当サイトの全件を取り出し直すより安い）。計画は先頭ページで決めて isolate のメモリに 10 分覚え、続きのページは使い回す（ページごとにタグの該当数を数え直さない）。残るオフセット方式は SSR の `?page=N`（クロール用のページ番号 URL。深いページほど読む）だけ。
3. **総件数は上限付き**: `TOTAL_CAP = 1000`。これを超える `q`・`tag` は `LIMIT 1001` で数えるのをやめる（`total_capped`）。末尾まで届いたときは読んだ行数から正確に分かる（追加の読み取りなし。q の 0 ヒットは、1〜2 文字なら全件走査 1 回、3 文字以上は下の 7 の索引で数行）。
4. **件数で計画を切り替える**（`planQuery()`。結果と並びはどの計画でも同じで、読み取りだけが変わる。`tests/search-plan.test.mjs` が参照実装と全ページ連結の一致を検査）:
   - カテゴリ: `category_counts`（集計表）から件数を点で読み、費用の見積り（IN 形 ≒ 所属数 × 7、EXISTS 形 ≒ 41 × 母数 ÷ 該当数 × 3）が小さい方を選ぶ。小さいカテゴリは `s.id IN (所属サイト)`、大きいカテゴリは並びの索引を走査して `EXISTS` で確かめ 1 ページ分で止まる。
   - タグ: タグごとの該当サイト数を `TAG_DRIVE_MAX + 1`（1,001）まで数え、**1,000 以下の一番稀なタグ**はタグの索引（0023・0024）から `IN (… UNION …)` で引く（一覧・件数とも該当数に比例）。超えるものは並びの索引を走査して `EXISTS`。単独のタグが 1,000 を超えるときは総件数を数えない（上限付き）。
5. **ピックアップ**: `ORDER BY RANDOM()` をやめ、乱数の rowid を 40 個（足りなければ 80 個で 1 回だけ引き直す）まとめて `rowid IN (…) AND bulk_import = 0 AND 範囲` で引く棄却抽選にする。範囲内の件数（`scope_counts`）が 50 以下なら従来の `ORDER BY RANDOM()`。「乱数位置の次の該当行」方式は、一括投入の塊の直後の 1 件に当たりが偏った（本番相当データで 1 件が 86%）ため採らない。
6. **活動**: `(bulk_import, date(registered_at))` の索引（0025）で直近 7 日だけを引く。
7. **検索語は全文索引（FTS5 trigram。0027）で引く**（#126 第3段。`planQuery()` が `q` も計画する）:
   - 3 文字以上の語は、まず索引の該当数を `TOTAL_CAP + 1`（併用ありは `FTS_EXTRA_PROBE` + 1 = 3,001）まで数える。**1,000 件以下なら件数も一覧も索引**（`s.rowid IN (SELECT rowid FROM sites_fts WHERE sites_fts MATCH ?)` + `instr` の確認。読み取りは該当数に比例。10 万件で 0 ヒット 10 万行 → 数行、プログラミング 4.6 万行 → 約 2,700 行）。**超えたら一覧は従来の「並びの索引を走査して `instr` で確かめる」形**（該当が多い語はこの形が 41 件で止まるので安い。索引で一覧を引くと該当数の約 4.4 倍読んで悪化する）で、総件数は数えず `1,000+`（`total_capped`）。範囲・カテゴリ・タグを併用していて該当が 3,000 件以下のときだけ、総件数を索引から数える（一覧は走査のまま）。
   - 1〜2 文字は instr（trigram の `MATCH` は 3 文字未満をエラーにせず 0 件で返す）。
   - **索引を使わない**: 稀なタグで引ける（タグの索引から ≤1,000 件。そちらが既に小さい）・小さいカテゴリ（IN 形で所属 ≤1,000 件）・索引が未構築（`fts_state.ready = 0`）・表が無い（ロールバック後。`planQuery()` が例外を握って instr）。
   - 判断（`qMode`）は計画に含めて先頭ページで決め、続きのページは使い回す（`planKey` に `q` を含める）。結果と並びは経路によらず同じ（`tests/search-fts.test.mjs` が索引の経路・instr の経路・独立した参照実装の全ページ連結と total を比べる）。
   - 1〜2 文字の検索は 10 万件で最悪 約 10 万行/回のまま残る（検索ログで割合を見てから、日本語の 1+2gram の補助索引などを判断する）。

10 万件の実測（rows_read。第2段の PR 本文の表）は `scripts/bench/`（合成データの投入 `gen-synthetic-sites.mjs`（`PROD_SHAPE=1` で一括投入が連続・日時同値の本番の形）と計測 `bench-search.mjs`・`bench-traverse.mjs`（全ページ合計と 1 ページの最大・ピックアップの偏り）。ローカルの D1 だけ）で再現できる。**同値の多いデータで測ること**（合成データだけの表は誤解を招く）。新しい形のクエリを足したら、**D1 本番で一度試す**（複合 SELECT の項数上限などローカルで出ない失敗がある。手順は [deploy.md](../operations/deploy.md#検索計画と索引023025を本番へ適用する手順issue-126-第2段)）。

## セキュリティヘッダ

`src/security.js` が定義し、Worker の全応答に付ける: `Content-Security-Policy`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`。静的ファイルには `public/_headers` で nosniff と Referrer-Policy を付ける（CSP は文書だけに意味があるため）。

- HTML（`/` と範囲ページ）の CSP は `script-src 'self'` に、`index.html` のインライン `<script>`（実行されるもの。JSON-LD のデータブロックは対象外）の sha256 を足したもの。`index.html` のインラインスクリプトは、テーマ用・初期言語判定用（`i18n-pending`、中身は静的）・globe/Service Worker の読込・ロゴ用で、いずれも中身は静的。ハッシュは `seo.js` がアセットの HTML から毎回計算するので、インラインスクリプトを編集してもハッシュの更新は要らない。`unsafe-inline` は使わない。`style-src 'self'`（HTML にインライン style なし。JS からの CSSOM 操作は対象外）、`img-src 'self' data:`、`connect-src 'self'`、`font-src 'self'`（自己ホストのロゴ書体用）、`manifest-src 'self'`、`worker-src 'self'`、`frame-ancestors 'none'`、`object-src 'none'`、`base-uri 'none'`。
- HTML 以外の応答は `default-src 'none'`。
- 外部の画像・スクリプト・フォントを足すときは `security.js` の許可も足す。本番では Cloudflare の HTML 加工でハッシュが合わなくなることがあるため、デプロイ後は [deploy.md](../operations/deploy.md#csp-を含むデプロイ後の確認手順) の手順で確認する。新しい CSP を試すときは `CSP_REPORT_ONLY` を `true` にして、コンソールの違反を確認してから強制に戻す。

## PWA

ホーム画面に追加でき、standalone で起動する。構成は 3 点だけ。

- **manifest**: `public/manifest.webmanifest`（`<link rel="manifest">` で参照）。`start_url` は `/`（既定範囲の日本）。**色は既定のライト背景に揃えて固定**: manifest の `theme_color`・`background_color` と `index.html` の `theme-color` はすべて `#ffffff`。このアプリは OS の配色設定に追従せず、既定はライトでアプリ内トグル（`localStorage`）でだけダークになるため、`prefers-color-scheme` によるダーク指定は外した（OS ダークのユーザーが白いページに暗いバーを見る不一致を避ける）。アプリをダークにしたときもバーは白のまま（許容。JS で meta を書き換えて追従する案は、必要になったら足す）。オフライン画面も同じくライト固定。アイコンは `public/pwa/`（サイトのファビコン置き場 `public/icons/` と分ける）: `icon-180.png`（apple-touch-icon）・`icon-192.png`・`icon-512.png`・`icon-maskable-512.png`（maskable は M を中央 76% に縮小）。正本は `mise-icon.svg`（赤地に白の `M`。**文字でなく path** なのでフォントに依存しない）で、`M` はロゴと同じ自己ホストの Anton サブセット（`public/fonts/anton-mise-v1.woff2`）のグリフ輪郭を fontTools（`brotli` も要る）で取り出して、512 の中央・高さ 260 に収めたもの。PNG は `node scripts/build-pwa-icons.mjs`（playwright と Chromium が必要）で書き出した生成物。ロゴの書体を替えたら、SVG の path を取り直して同じコマンドで PNG を作り直す。`.webmanifest` は静的配信させるため `wrangler.jsonc` の `run_worker_first` の除外に入れ、`_headers` で `Content-Type: application/manifest+json`・`no-cache` を付ける。CSP には `manifest-src 'self'` と `worker-src 'self'`（古いブラウザで `default-src 'none'` に落ちて SW 登録が弾かれるのを防ぐ）を足してある。
- **service worker**: `public/sw.js`（`index.html` の load 後に登録、失敗しても画面は使える）。**キャッシュを持たない**。ページ移動（navigate）がネットワークで失敗したときだけ、sw.js に埋め込んだ「オフラインです」（日英併記・503）を返す（このとき失敗した読み込みのエラーがコンソールに出るのは設計上のもので異常ではない）。HTML・API・静的ファイルは常にネットワーク経由で、`/api/*` の `Cache-Control`（[API](#api)）は従来どおり。オフライン画面は SW が作る応答なので、CSP（`style-src 'unsafe-inline'` のみ・script 不許可）もその応答に自分で付ける。別ファイルにしないのは、静的 HTML を置くと Worker の経路で 404 になる・CSP が配信経路ごとにずれるのを避けるため。
- **ナビゲーションプリロード**: `activate` で有効化し、起動時の SW 待ちでページ移動が遅れないようにする（応答の保存ではなくネットワーク応答そのもの）。
- **更新**: `skipWaiting` + `clients.claim` で新しい SW を即時有効化する。キャッシュが無いので古い画面が残らない（`activate` で既存の Cache Storage も消す）。`sw.js` と manifest は `_headers` で `Cache-Control: no-cache`（更新確認を遅らせない）。

- **インストール提案**: ブラウザ標準の提案は `public/app.js` が `beforeinstallprompt` を握って止め、下部の自前バナーに置き換える（SW・キャッシュ・CSP には関与しない。仕様は [ui.md](ui.md#インストール提案バナー)）。抑制判定の純関数は `public/install.js`（`index.html` で app.js より先に読む）。

配信設定は `npm run check` の `scripts/check-pwa.mjs` が検査する（`_headers`・`wrangler.jsonc`・CSP・manifest とアイコンの寸法・theme-color の一致。静的ファイルは Worker を通らないため設定ファイルそのものを読む）。

オフラインでアプリを使えるようにしない（古いカテゴリ・サイトを現行と誤認させない）。将来オフライン閲覧を足す場合は、日付・「古い可能性」の表示と更新方針を決めてから。

## 地球儀

- `globe.js` はページの load 後に遅延読込する。読込に失敗しても画面は地球なしで使える。
- タブ非表示中は描画を止める。
- 自動回転は、ドラッグ終了から約 4 秒の無操作で現在の向きから再開する（速度は 0 から滑らかに立ち上げ）。ドラッグ中・飛行中・`prefers-reduced-motion: reduce` では回転しない。
- 海岸線は簡略化した地形であり、正確な地図ではない。

## 国旗と地球の連動

各サイト行に、運営者の所在国（`sites.country_code`）の国旗 SVG を常時表示する（国コードが無いサイトは出さない。`aria-label` は「日本 / Japan」）。旗は絵文字でなく SVG（Windows では絵文字の国旗が国コード文字になるため）。[flag-icons](https://github.com/lipis/flag-icons)（MIT、[ライセンス全文](../../public/licenses/flag-icons-MIT.txt)）の 4x3 版から必要な国だけを `public/flags/<小文字コード>.svg` に置く（全旗は配信しない）。国を足すときは、SVG・`public/countries.js` の座標表に 1 行・同ファイルの旗リスト `MISE_FLAGS` に国コード。リストに無い国は旗要素を作らず 404 も出さない。`public/countries.js` は国名（日英）と代表座標の静的表で、`app.js` と `globe.js` が共有する。

### 区画（県・州）

県・州が公開情報で分かったサイトだけ `sites.region_code`（ISO 3166-2 第1階層。例 `JP-13`）を持つ（[data-model.md](data-model.md#運営者の所在区画region_code)）。表示は旗の右に区画名（言語に応じ ja/en）を小さく出す。行の高さは動かさない。region が無い、または区画表に無いコードのサイトは旗のみ。

区画の名前（ja/en）と代表座標の表は `public/regions.js`（`window.MISE_REGIONS`、生成物）。生成は `scripts/collect-regions.py`（[procedures.md](../collection/procedures.md#区画表の再生成)）。出典は Wikidata（CC0）。

### 範囲の選択（地球儀）

選べる国（サイトが 1 件以上ある国）の代表点に、`globe.js` が小さな点を置く（現在の範囲の国は大きく暖色）。`app.js` が `window.MISE_SCOPE = {current, countries:[{code,lat,lon}]}` を置き（`current` は国単位。区画の範囲ではその国。区画を選ぶと地球は `mise-fly-country`（`level: 'region'`）で区画の代表点へ向く）、変わるたびに `mise-scope-changed` を発火する。何も載っていない背景（`html` / `body` と本文の入れ物の余白）をクリック/タップすると、レイキャストで当たった緯度経度から角距離 12° 以内で最も近い選べる国を求め、`mise-globe-pick`（`detail: {code}`）を発火して `app.js` が範囲を切り替える。国境の多角形は持たない（海岸線データに国情報がない）ので、代表点からの距離で選ぶ。リンク・ボタン・文章の上のクリックは地球儀の入力にしない。ポインターが選べる国の上にあるとき（PC のみ）は `html.globe-hot` でカーソルを手の形にする。切替ボタン（国旗リンク）が同じ操作のキーボード・スクリーンリーダー用の経路で、地球儀は補助。

### 連動の挙動

- PC は行・旗のホバー/フォーカスで約 300ms 静止してから、スマホは旗、または行のリンク以外（説明文・メタ情報・余白）のタップ（`click` のみ。スクロール/スワイプでは出ない）で、`app.js` がイベント `mise-fly-country`（`detail: {lat, lon, hold, level}`、`level` は `'region'` / `'country'`）を発火し、`globe.js` が地球をその国・区画へ向ける。region があれば区画の代表点へ、無ければ国の代表点へ。タイトルのリンクのタップは通常の遷移のみで、地球は動かさない。
- ホバーを離す/フォーカスが外れると `mise-fly-release`。最後の操作から 4 秒無操作で既定の向き・距離へ戻り、自動回転に復帰する（手動ドラッグも同様。実際に動かした時だけ最終操作時刻を更新し、背景クリックだけでは止めない）。フォーカスで動くのはキーボード操作（`:focus-visible`）のときだけで、リンクのタップ/クリックによるフォーカスでは動かさない。スマホの行タップは、`click` の `pointerType` が無い古いブラウザがあるため、行の `pointerdown`/`touchstart` で記録した入力種別（直前 500ms 以内。pen は touch と同じ扱い）で判定する。一覧の行を消す共通関数 `clearListRows`（追記読み込みでは呼ばれない）が置換前にホールドを解く（地球側は保持中でなければ何もしない）。ホバー用の 300ms タイマーは、行が DOM から消えていたら何もしない。ロゴ・「トップ」パンくず・範囲を `all` に切り替えたとき、戻る/進むで範囲が変わったときは `mise-fly-reset` で待たずに既定の向き・距離（世界全体）へ戻し（国別範囲は従来どおり、その国へ向ける）、一覧を描き直すときは `mise-fly-release` でホールドも解く（消えた行からは pointerleave/focusout が来ないため）。`#globe-background` の `data-distance` / `data-target` / `data-peek` は計測用の読み取り表示。
- カメラ距離は `globe.js` の定数: 国 `COUNTRY_DISTANCE = 4.2`、区画 `REGION_DISTANCE = 3.2`。向きは `aimAt` で求める。
- `prefers-reduced-motion` では滑らかに動かさず即時切替・自動回転なし。
