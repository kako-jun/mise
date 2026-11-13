# アーキテクチャ

## 技術構成

- **Cloudflare 内**で配信、API、データベースを扱う。
- API とサーバー側処理は **Cloudflare Workers + Hono**（`src/index.js`、`src/seo.js`）。
- データベースは **Cloudflare D1**（`migrations/`）。
- 画面は **HTML / CSS / JavaScript**（`public/`）。UI フレームワークは使わない。Astro は採用しない。
- 3D は **Three.js**。ソースは `src/client/globe.js`、three 本体は `vendor/three.module.js`（同梱）。`npm run build:globe`（esbuild で minify・tree-shake）が `public/globe.js` を生成する。globe を変えたら再ビルドして生成物もコミットする（直接編集しない）。`public/land-110m.geojson` は `scripts/slim-land.mjs` で軽量化済み（小数 2 桁・property 除去）。
- 配信: `wrangler.jsonc` の `assets.run_worker_first` に `/api/*`・`/`・`/sitemap.xml` を指定し、これらは Worker が返す。それ以外（`/robots.txt` など）は `public/` の静的ファイル。

## API

| エンドポイント | 内容 |
|---|---|
| `GET /api/categories` | カテゴリの木。サイト件数は自身と全子孫の所属サイトを数える |
| `GET /api/sites` | 掲載サイト全件。`site_categories` と `LEFT JOIN` するため、カテゴリ未所属のサイトも `category_id = NULL` で返る（画面では「未分類」として検索に出る。カテゴリ一覧には出ない）。並びは [ui.md](ui.md#一覧検索結果の既定の並び) |
| `GET /api/activity` | 掲載総数と直近 7 日の日別登録数（HUD 用） |
| `GET /` | `index.html` に URL に応じた内容を差し込んで返す（[seo.md](seo.md)） |
| `GET /sitemap.xml` | D1 から動的生成 |

`/api/*` は全件返しで、`Cache-Control: public, max-age=30` を付ける（更新が速やかに反映されるよう短く。ページング・カーソル化は構想: [ideas/scope-and-search.md](../ideas/scope-and-search.md)）。ブラウザ用で、外部向け公開 API としては設計していない。

## セキュリティヘッダ

`src/security.js` が定義し、Worker の全応答に付ける: `Content-Security-Policy`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`。静的ファイルには `public/_headers` で nosniff と Referrer-Policy を付ける（CSP は文書だけに意味があるため）。

- HTML（`/`）の CSP は `script-src 'self'` に、`index.html` のインライン `<script>`（実行されるもの。JSON-LD のデータブロックは対象外）の sha256 を足したもの。ハッシュは `seo.js` がアセットの HTML から毎回計算するので、インラインスクリプトを編集してもハッシュの更新は要らない。`unsafe-inline` は使わない。`style-src 'self'`（HTML にインライン style なし。JS からの CSSOM 操作は対象外）、`img-src 'self' data:`、`connect-src 'self'`、`frame-ancestors 'none'`、`object-src 'none'`、`base-uri 'none'`。
- HTML 以外の応答は `default-src 'none'`。
- 外部の画像・スクリプト・フォントを足すときは `security.js` の許可も足す。新しい CSP を試すときは `CSP_REPORT_ONLY` を `true` にして、コンソールの違反を確認してから強制に戻す。

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

### 連動の挙動

- PC は行・旗のホバー/フォーカスで約 300ms 静止してから、スマホは旗のタップで、`app.js` がイベント `mise-fly-country`（`detail: {lat, lon, hold, level}`、`level` は `'region'` / `'country'`）を発火し、`globe.js` が地球をその国・区画へ向ける。region があれば区画の代表点へ、無ければ国の代表点へ。行本体のクリックは従来どおり遷移のみ。
- ホバーを離す/フォーカスが外れると `mise-fly-release`。最後の操作から 4 秒無操作で既定の向き・距離へ戻り、自動回転に復帰する（手動ドラッグも同様。実際に動かした時だけ最終操作時刻を更新し、背景クリックだけでは止めない）。
- カメラ距離は `globe.js` の定数: 国 `COUNTRY_DISTANCE = 4.2`、区画 `REGION_DISTANCE = 3.2`。向きは `aimAt` で求める。
- `prefers-reduced-motion` では滑らかに動かさず即時切替・自動回転なし。
