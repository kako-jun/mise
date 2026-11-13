# SEO・クローラ向けの方針

実装は `src/seo.js` と `src/index.js`。

- **配信**: `/`（トップ・カテゴリ・検索のすべて）と `/sitemap.xml` は Worker が返す。`/robots.txt` は静的ファイル `public/robots.txt`。`/api/` は JS のレンダリングに必要なため robots では塞がない。JS の取得失敗時も SSR 本文があれば消さない。ブラウザで JS が描画した後も title はカテゴリ名を保つ。
- **軽量 SSR**: UI フレームワークや SSR 基盤は使わず、Worker が `HTMLRewriter` で `index.html` に URL に応じた title・description・canonical・OGP と、カテゴリ一覧・パンくず・直下サイトのリンク入り本文（英語）を差し込む。カテゴリ名は `name_en`。ブラウザでは従来どおり JS が同じ領域を描き直す。検索結果・日本語表示・ピックアップは JS 依存。日本語設定のユーザーには、JS 描画までの一瞬 SSR の英語が見える（既知・許容）。
- **カテゴリ URL と canonical**: カテゴリ URL は `/?category=<id>&category=<id>…`（祖先からの順）を 1 カテゴリ 1 ページとして扱う。canonical は ID に正規化した同形式の URL（`name_ja` 指定や存在しない値は正規化後の URL、解決できなければ `/`）。sitemap には本文のあるカテゴリ（配下に 1 サイト以上）の正規化 URL と `/` を載せ、D1 から動的生成する。`?q=` の検索結果は（空でない場合のみ。`?category=` との併用でも）canonical を `/` にし、`noindex,follow` を付ける。
- **言語**: 既定は英語。`<html lang>`・title・description・OGP・JSON-LD・SSR 本文・`index.html` の静的テキストはすべて英語を既定にする。
- **hreflang**: 言語切替は同一 URL 上の JS 切替で URL が分かれず、クローラは保存値のない状態（英語）しか見ない。そのため `en` と `x-default` のみ同一 URL で宣言し、日本語 URL の分離（`?lang=ja` 等）は行わない（canonical・sitemap・SSR・`app.js` に波及するため、必要になった時に別途設計する）。
- **OGP 画像**: 専用画像は未作成のため `og:image` は未設定（Twitter Card は `summary`）。アイコン確定（Issue #10）後に設定する。
- **JSON-LD**: `WebSite` + `SearchAction`（`/?q={search_term_string}`）。
- **キャッシュと負荷**: `/` は URL ごとに本文が違うため、書き換え後のレスポンスから元アセットの ETag / Last-Modified を除き、`Cache-Control: public, max-age=0, must-revalidate, s-maxage=60`（ブラウザは毎回再検証、共有キャッシュは 60 秒）とする。再帰 CTE のカテゴリ集計は毎リクエスト走るが、現状の件数では軽いと判断している。サイト数・カテゴリ数が増えたら Cache API での URL 単位キャッシュと一覧の件数上限を検討する。
