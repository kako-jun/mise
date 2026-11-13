# 恒常的な収集手順

スクリプトの再実行方法と、収集データの書式。実行のたびに実サイトや外部の出典へアクセスする。結果の数字は書かず、[trials.md](trials.md) と `data/` を見る。Python は常に `uv run python3`。

## 共通の作法

- User-Agent は `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`（区画表の Wikidata 取得のみ別の UA）。
- robots.txt を先に確認する。200 なら規則を読み、404/410 は規則なし、それ以外の取得失敗・リダイレクト・401/403・5xx は取得せず記録して止める。noindex・サイズ超過も取得・登録を中止する。
- 1 サイト数リクエストまで。サイト間・リクエスト間に待機を入れ、直列で行う（`collect-sample.py` は 1 秒、`collect-source-updated.py`・`check-embeddable.py` は 2 秒、官公庁の名簿取得は 1 秒、パイプラインは 1.5 秒）。リダイレクトは同一ホスト内に限る。内部 IP は拒否する。
- 推測で埋めない。取れないものは `NULL`。
- 大量発見・定期巡回は [pipeline.md](pipeline.md) と [operations/jobs.md](../operations/jobs.md)。ここのスクリプトは固定の対象に対する単発の取得用で、任意 URL の大量収集には使わない。

## 収集データの書式（`data/collection-*.json`）

サイトごとに次を持つ。D1 への投入は、この JSON を根拠に手で書く migration（`migrations/0005`・`0009`・`0014` など）で行う。

- `id`、`url`、`name`、`categories`（既存のカテゴリ ID）
- `discovery_source`（発見元）、`checked_at`、robots.txt の URL・HTTP 状態・判定、トップページの HTTP 状態、観測した title / meta description
- `description_basis`、紹介文は日本語（`description_ja`）と英語（`description_en`）を**同時に**作る。英語は日本語に書かれた事実だけを自然な 1 文に訳し、情報を足さず、固有名詞は原語または一般的なローマ字表記にする。紹介文は取得した title・meta description・本文に基づく 1 文で、審査済みの承認データではない
- `country_code`（ISO 3166-1 alpha-2、不明は `null`）と根拠 `country_evidence`。運営者の所在国を、トップページ・運営者情報・作者紹介などの記載で確認する。ドメイン・言語・ホスティングだけでは決めない。`.us` のように登録資格が居住や所在を要件とするドメインは根拠にできる
- 県・州（`region_code`、ISO 3166-2）も同じ基準で、公開情報に明記されたものだけ。根拠は `data/region-evidence.json`（id・region_code・出典 URL・根拠・確認日）に添える

載せない候補: robots.txt で禁止、取得不能、ドメイン売却ページ、移転告知だけ、メンテナンス表示だけ、更新が止まっていると見られるもの、運営者が無期限休止を告知しているもの、運営形態が不明な大規模サイト。基準は [listing-criteria.md](../concept/listing-criteria.md)。

## スクリプト

| スクリプト | 用途 | 再実行 |
|---|---|---|
| `scripts/collect-sample.py` | 固定シードのトップページの title・meta description を robots 確認のうえ取得 | `uv run python3 scripts/collect-sample.py > data/collection-sample.json` |
| `scripts/collect-source-updated.py` | 掲載サイトの更新日を根拠付きで取得 | `uv run python3 scripts/collect-source-updated.py > data/source-updated.json` |
| `scripts/check-embeddable.py` | トップページが iframe で埋め込めるかをヘッダだけで判定 | `uv run python3 scripts/check-embeddable.py > data/embeddability.json` |
| `scripts/collect-regions.py` | 区画表 `public/regions.js` を生成 | `uv run python3 scripts/collect-regions.py` |
| `scripts/capture-thumbnails.mjs` | サムネイル撮影の試験（Node、playwright が別途必要） | [thumbnails.md](thumbnails.md#実行) |
| `scripts/pipeline/run.py` | 独立サイトの発見パイプライン | [pipeline.md](pipeline.md#実行方法) |
| `scripts/public_bodies_*.py` | 官公庁の名簿 | [public-bodies.md](public-bodies.md) |

### サイトの更新日の取得

`collect-source-updated.py` は、sitemap の lastmod → RSS/Atom の最新日 → HTML の `<time>`・meta → ページ上の「最終更新」表記の順で更新日を探す。Last-Modified ヘッダは動的ページが現在時刻を返すため使わない。1 サイト最大 4 リクエスト（robots・トップ・sitemap・フィード）、サイト間とリクエスト間に 2 秒待機、本文は 1 MiB まで。UA は `MiseResearchBot/0.1`。取れないものは NULL で、理由は `data/source-updated.json` に残る。

既知の制約:
- 対象は固定の掲載サイトのみ。対象を広げるなら、名前解決後の IP も検査する（内部 IP 拒否はリテラル IP だけ）。
- sitemap は lastmod の最大値を採るため、頻繁に再生成される一覧を持つサイトは実更新より新しく見えうる。
- curl の `--max-filesize` は Content-Length 頼みで、chunked 応答は読み込んだ後に 1 MiB で切る。

投入は migration（`0012_source_updated_at.sql`）。

### 埋め込み可否の判定

`check-embeddable.py` は robots.txt（1 リクエスト）→ トップページの GET（ヘッダのみ、本文は 1 バイトで切る）で、X-Frame-Options、CSP の `frame-ancestors`、最終 URL のスキーム、HTTP ステータス、robots の可否を記録する。リダイレクトは同一ホスト宛てのみ最大 1 回追う。robots が取れないサイトは測定せず `robots-unknown` と記録する。迂回はしない。JS の frame-busting などヘッダ以外の理由で表示できないケースは判定外。結果は `data/embeddability.json`。この結果の使い道は構想（[ideas/browser-and-thumbnails.md](../ideas/browser-and-thumbnails.md)）。

### 区画表の再生成

`public/regions.js`（`window.MISE_REGIONS`）は生成物なので手で編集しない。Wikidata の SPARQL から、P300（ISO 3166-2）・P625（座標）・ja/en ラベル（en が無ければ mul）を取る。対象は「国の第 1 階層行政区画」(Q10864048) のクラスに属し解散日 (P576) を持たない項目で、`MISE_FLAGS` と同じ国。取得日・件数・欠損は `public/regions.js` の先頭コメントに出力される。Wikidata の網羅は ISO と完全一致しない国がある。国を足したらスクリプトの `COUNTRIES` と `public/countries.js` を揃えて再実行する。TLS 検証の設定は施設内プロキシ向けで、スクリプト冒頭の説明を参照。

### 国旗の追加

`public/flags/<小文字コード>.svg`（flag-icons の 4x3 版）、`public/countries.js` の座標表に 1 行、同ファイルの `MISE_FLAGS` に国コード、区画表の再生成。詳細は [architecture.md](../design/architecture.md#国旗と地球の連動)。
