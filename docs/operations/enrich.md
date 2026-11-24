
## 取得段

`scripts/enrich/fetch_stage.py`（`run(client, *, limit, apply, log)`）。`url_progress.state='discovered'` の行を `identity_key` の機械順に取り、トップページを取得して `url_candidates` に特徴（title / description / lang / kana_count / text_len / ad_hits / ext_refs / flags / text_head / personal_score / 成人属性 / 種別 / 更新の兆候）を書く。取得・抽出は `scripts/pipeline` の fetcher / features をそのまま使う（公開 IP のみ・IP 固定・オフサイトのリダイレクト拒否・512KB・20 秒・robots.txt 遵守・同一ホスト 2 秒以上）。ローカルファイルは作らない。

- claim: `discovered -> queued` を `updated_at` の比較交換で行う（2 台が同じ行を取らない）。30 分を超えた `queued` は再 claim できる。
- 結果: 成功 `fetched` / robots 不許可 `blocked`(reject_reason `robots`) / 恒久エラー（NXDOMAIN・トップが 404・410・HTML でない）`failed` / 入口・駐車・noindex・共有サービス配下・行政ドメインなど `rejected`。成人向けは拒否せず `fetch_adult` に残すだけ（本文冒頭と description は保存しない）。
- 一時エラー（タイムアウト・5xx・429・一時的な DNS 失敗）は `discovered` に戻し、`fetch_attempts` を増やして `next_fetch_at` を 1h × 2^(試行-1) 後にする。5 回で `failed`。
- 薄い本文・日本語でない等は判定段の仕事で、ここでは拒否しない。
- `apply=False` は対象件数を出すだけで claim も書き込みもしない。書き込みは read-back で検証する。
- テスト: `tests/test_enrich_fetch_stage.py`（偽 D1 と偽 fetcher）。
