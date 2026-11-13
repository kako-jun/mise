# 小規模なサイト収集の試行

2026-11-12 に、固定した2つの公開サイトのトップページを取得した。`scripts/collect-sample.py` は robots.txt を確認した後に各トップページを取得し、タイトルと meta description を `data/collection-sample.json` に記録する。アクセス間に待機を入れ、リンクをたどらない。robots.txt が 404/410 の場合は規則なしとして扱い、それ以外の取得失敗、リダイレクト、noindex、サイズ超過は取得・登録を中止する。取得結果とUTC時刻はJSONを参照。

2件の紹介文とカテゴリは収集結果を元に作成した試作用の案であり、自動公開の承認済みデータではない。`migrations/0002_collected_examples.sql` は試作用のD1に登録する。エージェントによる定期収集・自動分類・紹介生成・重複判定・審査フローは未実装。

再実行: `uv run python3 scripts/collect-sample.py > data/collection-sample.json`。実行するたび実サイトへアクセスする。試行用の固定シード限定であり、このスクリプトを任意URLの大量収集や定期巡回に使用しない。

## 第1弾（2026-11-13）

件数の薄いカテゴリ（工学・天文学・生物学・考古学・宗教・建築・写真）から、公開リンク集（asahi-net の天文リンク集、ELM by ChaN のリンクページ）と、検索で見つけたページ、候補作成者が既に知っていたサイトを起点に候補を探した。各候補で robots.txt とトップページを `scripts/collect-sample.py` の取得ロジックで確認し、取得できなかったもの、ドメイン売却ページになっていたもの、移転告知だけのもの、更新が止まっているとみられるもの、運営形態が不明な大規模・教育アプリ系は載せていない。紹介文は取得したタイトル・meta description・トップページ本文に基づく1文で、審査済みの承認データではない。

出典・確認日時・robots判定は `data/collection-batch1.json`、登録は `migrations/0009_collected_batch1.sql`。既知サイトは「公開出典なし」と明記している。

## 国（運営者の所在国）

収集の書式に `country_code`（ISO 3166-1 alpha-2。不明は `null`）と根拠 `country_evidence` を加えた。運営者の所在国を、トップページ・運営者情報・作者紹介などの記載で確認する（ドメイン・言語・ホスティングだけでは決めない）。第1弾の既存25サイトでは、sample-1〜4（作者自身のサイト。根拠は所有者情報のみで、サイト上の公開記載による確認ではない。収集JSONにも `country_evidence` は無い）、sample-5〜7（日本の機関・団体）と、記載で日本と確認できた3サイト（古墳マップ・ぼうずコンニャク・Stellar Scenes）を `JP` とし、残りは確認できず `null`（旗なし）。投入は `migrations/0011_country_code.sql`。確認できたら `UPDATE` で追加する。旗は表（`public/countries.js`）と `public/flags/` にある国だけ表示する。

今後の収集では、紹介文を日本語（`description_ja`）と英語（`description_en`）の両方で同時に作る。英語は日本語に書かれた事実だけを自然な1文に訳し、情報を足さず、固有名詞は原語または一般的なローマ字表記にする。既存の0005・0009は書き換えず、英語は `migrations/0010_description_en.sql` で後から投入した。

## サイトの更新日（2026-11-13）

`scripts/collect-source-updated.py` が既存25サイトの更新日を根拠付きで取る。順序は sitemap の lastmod、RSS/Atom の最新日、HTML の <time>・meta、ページ上の「最終更新」表記。Last-Modified ヘッダは動的ページが現在時刻を返すため使わない。robots.txt を守り、1サイト最大4リクエスト（robots・トップ・sitemap・フィード。同一ホストのリダイレクト追従は1回につき最大2回が別枠）、リクエスト間とサイト間に2秒待機、同一ホスト内のリダイレクトのみ追従、内部IPは拒否、本文は1 MiB まで。UA は MiseResearchBot/0.1。取れないものは推測せず NULL。結果と取得できなかった理由は `data/source-updated.json`。25件中11件が取れた（sitemap 9、フィード1、ページ上の表記1）。残り14件は sitemap が無く（404）、フィード・<time>・更新日表記も見つからなかった。取れた値も sitemap の lastmod が自動生成で実更新と一致しない可能性はある。
既知の制約: 対象は固定25サイトのみ（内部IP拒否はリテラルIPだけなので、対象を広げるなら名前解決後のIPも検査する）。sitemap は lastmod の最大値を採るため、頻繁に再生成される一覧を持つサイトは実更新より新しく見えうる。curl の `--max-filesize` は Content-Length 頼みで、chunked 応答は読み込んだ後に1 MiB で切る。

## 埋め込み可否の実測（2026-11-13、Issue #41）

`scripts/check-embeddable.py` が既存25サイトのトップページについて、iframe で mise 内に表示できるかをHTTPヘッダだけで判定する。robots.txt（1リクエスト）→ トップページの GET（ヘッダのみ、本文は1バイトで切る）を、サイト間・リクエスト間とも2秒以上空けて直列に行い、リダイレクトは最大1回だけ追う（その場合は3リクエスト）。UA は MiseResearchBot/0.1。記録するのは X-Frame-Options、CSP の frame-ancestors、最終URLのスキーム、HTTPステータス、robots の可否で、迂回はしない。結果は `data/embeddability.json`（生の観測値・判定・UTC時刻）。再実行: `uv run python3 scripts/check-embeddable.py > data/embeddability.json`。実行のたびに実サイトへアクセスする。対象は固定25サイトのみ。
