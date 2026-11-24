
## 取得段

`scripts/enrich/fetch_stage.py`（`run(client, *, limit, apply, log)`）。`url_progress.state='discovered'` の行を `identity_key` の機械順に取り、トップページを取得して `url_candidates` に特徴（title / description / lang / kana_count / text_len / ad_hits / ext_refs / flags / text_head / personal_score / 成人属性 / 種別 / 更新の兆候）を書く。取得・抽出は `scripts/pipeline` の fetcher / features をそのまま使う（公開 IP のみ・IP 固定・オフサイトのリダイレクト拒否・512KB・20 秒・robots.txt 遵守・同一ホスト 2 秒以上）。ローカルファイルは作らない。

- claim: `discovered -> queued` を `updated_at` の比較交換（`UPDATE ... RETURNING` で勝敗判定）で行い、2 台が同じ行を取らない。30 分を超えた `queued` は再 claim できる。5 行ずつ claim -> 取得 -> 書き込みと進め、書き込み（upsert・状態更新）は自分の claim と一致するときだけ効く。`client.batch` は 1 回 5 文以内。
- 取得前に `rejected` にするのは、無効なホスト、サービスのトップ・サービス内ページ（`platform_page`）、公的機関（`public_body_other_pipeline`: 公的機関は別パイプライン public-bodies で扱う）だけ。共有サービス上の自作サイト・ISP の `/~user/`・名前の形が変わったドメインは「保留であって拒否ではない」ので、他と同じように取得する。
- 結果: 成功 `fetched` / robots 不許可と 403 `blocked` / 恒久エラー（この行自身のホスト名が NXDOMAIN・トップが 404・410・HTML でない）`failed` / 入口・駐車・noindex `rejected`。成人向けは拒否せず `fetch_adult` に残すだけ（本文冒頭と description は保存しない）。
- 一時エラー（タイムアウト・5xx・429・www/apex の片方だけの DNS 失敗・1 行の想定外の例外）は `discovered` に戻し、`fetch_attempts` を 1 増やして `next_fetch_at` を 1h × 2^(試行-1) 後にする。5 回で `failed`。`fetch_attempts` は実際に取得を試みた回数で、取得前の拒否では増やさない。
- `url_candidates` への upsert は、特徴を抽出できたときだけ特徴列を更新し、一時エラーや取得前の拒否では既存の特徴列を消さない（取得前の拒否は `last_fetched_at` も書かない）。
- 薄い本文・日本語でない等は判定段の仕事で、ここでは拒否しない。
- `apply=False` は対象件数を出すだけで claim も書き込みもしない。書き込みは read-back で検証する。
- 既知の制限: 取得するのはホスト + パス（URL のクエリ文字列とポートは落ちる）。
- テスト: `tests/test_enrich_fetch_stage.py`（偽 D1 と偽 fetcher）。
