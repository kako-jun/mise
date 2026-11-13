# 小規模なサイト収集の試行

2026-11-12 に、固定した2つの公開サイトのトップページを取得した。`scripts/collect-sample.py` は robots.txt を確認した後に各トップページを取得し、タイトルと meta description を `data/collection-sample.json` に記録する。アクセス間に待機を入れ、リンクをたどらない。robots.txt が 404/410 の場合は規則なしとして扱い、それ以外の取得失敗、リダイレクト、noindex、サイズ超過は取得・登録を中止する。取得結果とUTC時刻はJSONを参照。

2件の紹介文とカテゴリは収集結果を元に作成した試作用の案であり、自動公開の承認済みデータではない。`migrations/0002_collected_examples.sql` は試作用のD1に登録する。エージェントによる定期収集・自動分類・紹介生成・重複判定・審査フローは未実装。

再実行: `uv run python3 scripts/collect-sample.py > data/collection-sample.json`。実行するたび実サイトへアクセスする。試行用の固定シード限定であり、このスクリプトを任意URLの大量収集や定期巡回に使用しない。

## 第1弾（2026-11-13）

件数の薄いカテゴリ（工学・天文学・生物学・考古学・宗教・建築・写真）から、公開リンク集（asahi-net の天文リンク集、ELM by ChaN のリンクページ）と、検索で見つけたページ、候補作成者が既に知っていたサイトを起点に候補を探した。各候補で robots.txt とトップページを `scripts/collect-sample.py` の取得ロジックで確認し、取得できなかったもの、ドメイン売却ページになっていたもの、移転告知だけのもの、更新が止まっているとみられるもの、運営形態が不明な大規模・教育アプリ系は載せていない。紹介文は取得したタイトル・meta description・トップページ本文に基づく1文で、審査済みの承認データではない。

出典・確認日時・robots判定は `data/collection-batch1.json`、登録は `migrations/0009_collected_batch1.sql`。既知サイトは「公開出典なし」と明記している。

## 国（運営者の所在国）

収集の書式に `country_code`（ISO 3166-1 alpha-2。不明は `null`）と根拠 `country_evidence` を加えた。運営者の所在国を、トップページ・運営者情報・作者紹介などの記載で確認する（ドメイン・言語・ホスティングだけでは決めない）。第1弾の既存25サイトでは、sample-1〜7（作者自身のサイトと日本の機関・団体）と、記載で日本と確認できた3サイト（古墳マップ・ぼうずコンニャク・Stellar Scenes）を `JP` とし、残りは確認できず `null`（旗なし）。投入は `migrations/0011_country_code.sql`。確認できたら `UPDATE` で追加する。旗は表（`public/countries.js`）と `public/flags/` にある国だけ表示する。

今後の収集では、紹介文を日本語（`description_ja`）と英語（`description_en`）の両方で同時に作る。英語は日本語に書かれた事実だけを自然な1文に訳し、情報を足さず、固有名詞は原語または一般的なローマ字表記にする。既存の0005・0009は書き換えず、英語は `migrations/0010_description_en.sql` で後から投入した。
