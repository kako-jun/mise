# オンライン補強パイプライン（enrich）

収集機から本番 D1 だけを正本として URL を終端状態まで進める。ローカルの SQLite・CSV・JSONL・キャッシュ・チェックポイントは作らない（メモリのみ）。

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

## 判定段

`scripts/enrich/judge_stage.py`。取得段が `fetched` にした行（`url_candidates` に特徴列がある）だけを読む。取得段の `discovered` / `queued` / `failed` / `blocked` / `rejected` には触れない。取得段は `reviewing` を使わないので、判定段の claim（下記）と衝突しない。成人向けは取得段が本文冒頭と description を保存しないため、判定は title などの少ない材料になり、多くは `pending`（人が見る）に落ちる（拒否はしない）。

`run(client, limit=, apply=, log=, llm=)`。`client` は `query(sql, params)` / `batch([(sql, params)...])`（D1 REST、1 文 100 パラメータまで）。

- 入力: `url_progress.state='fetched'` と `url_candidates`（取得済みメタデータ）の結合。並びは `identity_key`（人気・規模では選ばない）。
- 判定: 既存の `scripts/llm/pipeline.py` の 2 段（triage = haiku、describe = sonnet、5 件ずつ）と `judge.py` の検証をそのまま使い、`url_candidates` の判定列を `UPDATE ... WHERE identity_key=?` で書く。`review_state` が既にある行は再判定しない（状態の移動だけ）。
- 審査: 既存の決定論ポリシー（`scripts/llm/approve_policy.py`、基準は変えない）。`spam_verdict=reject` は `rejected`（`review_reason=spam:<kind>`）。
  `approved` → state `approved` / `pending`（人が見る）→ `candidate` / `rejected` → `rejected`。
- 失敗: LLM の硬い失敗は `fetched` のまま次回再試行（回数は `notes` の `judge_attempts=N`）。3 回で `failed`（`review_reason=judge_failed`）。
- `apply=False` は読むだけ（LLM も書き込みも無し）。`ANTHROPIC_API_KEY` は環境変数のみ。値は表示しない。

### 費用台帳（D1 `llm_ledger`、migration 0038）

月（UTC）の上限は `MONTHLY_CAP_USD = 100.0`（`nightly.py --budget-month` と同じ）。リクエストごとに
(1) 最悪見積りを `kind='reserve'` で INSERT（月合計 + 見積り ≤ 上限の時だけ、1 文で原子的に）→ (2) 呼び出し → (3) 同じ行を `kind='call'`（実費・トークン数）へ確定。
確定に失敗したら reserve のまま残して実行を止める（黙って解放しない）。API が明示的に拒否した時だけ `release`（0 USD）。通信断など応答が曖昧なら reserve を残す（確認は人間）。
判定の書き込みと確定は別の呼び出し（確定が先）。確定後・書き込み前に落ちると、次回その分を再度払う（行は `fetched` のまま）。

### 実行と排他

**初回実行前の前提**: 旧 `export_sql.py` の写像は `deferred` や `review_state='pending'` を `reviewing` にしうる。次の件数が 0 であること。

`SELECT COUNT(*) FROM url_progress p JOIN url_candidates c USING(identity_key) WHERE p.state='reviewing' AND (c.review_state IS NULL OR c.review_state='')`

0 でない時、判定段はそれらを「判定を持たない古い `reviewing`」として取り直し、旧データを LLM に送ってしまう。先に中身を確認する。`review_state` を既に持つ行は claim せず、`fetched` のまま決定的に移すだけ（途中で落ちても次回の実行が拾う）。

**ランナーは 1 つだけ動かすのを推奨**。それでも二重に動いた時のために、行を取る前に `url_progress.state` を `fetched`→`reviewing`（条件付き UPDATE。取れた行だけ処理）にして取り合いを避ける。判定を持たないまま 30 分を超えた `reviewing` は取り直せる（落ちたランナーの跡）。人の審査待ち（`review_state` あり）の `reviewing` は触らない。書き込み後に状態を読み戻し、他が動かした行は成功に数えない。

### 失敗の扱い

行固有の失敗（出力不正・途中切れ・応答なし・書き込み失敗）だけ試行回数に数える（3 回で `failed`。モデルの拒否 `stop_reason=refusal` が 3 回続いた行は `candidate` へ送り人が見る）。認証・請求・過負荷などの API 全体の拒否は予約を解放して run を止め、行には触れない。

### 残った reserve 行の解消

通信断などで `reserve` のまま残った行は、請求（Anthropic のコンソール）で実費を確認し、`UPDATE llm_ledger SET kind='call', cost_usd=<実費>, note='manual' WHERE id=? AND kind='reserve'`（請求が無ければ `kind='release', cost_usd=0`）で手動確定する。

Message Batches（半額）は未対応。同期呼び出し、`concurrency` は 1〜4。

収集機は本番 D1 だけを正本にして、URL を段階ごとに進める。ローカルファイルは持たない（メモリ上のみ）。

## 掲載段（`scripts/enrich/publish_stage.py`）

`url_progress.state='approved'` の URL を `url_candidates` と結合し、1 サイトごとに **1 回の `client.batch`（D1 では 1 トランザクション）** で掲載する。
`run(client, *, limit, apply, log=print)`。`apply=False` は読み取りだけで、何も書かない。

1 バッチの中身:

1. `sites` を INSERT（既掲載 URL（正規化 URL と `progress_key` の両方）・拒否ホスト・拒否済み申請・同じ id がある場合は入れない。`ON CONFLICT DO NOTHING`）
2. `site_tags`（最大 5、重複・40 字超を除く）
3. `site_categories`（最大 3、旧 id の変換と祖先の除去済み、D1 の `categories` に存在する id のみ）。2・3 は `changes()` を使わず、「その sites 行があり、そのサイトの行がまだ無い」で守る（カテゴリ 0 件の sites 行はこのトランザクション内にしか存在しえない）。バッチの最後に、sites があるのにカテゴリが 0 件なら NOT NULL 違反を起こす文を置き、全体をロールバックする
4. `site_media_jobs`（favicon・thumbnail、`pending`、`identity_key=progress_key`。成人サイトは thumbnail が `excluded`/`eligible=0`。migration 0033/0035 のトリガが作るので、同じ規則の `INSERT OR IGNORE` を保険として添える）
5. `url_progress.state='published'`

件数表（`category_counts`・`scope_counts`）、`search_text`・FTS、`registered_at` は既存トリガが同じトランザクションで更新する。`bulk_import` は既定の 0。

- id は `fr-<key の英数字 40 字>-<sha1(key)[:8]>`。key は `frontier_key`（旧経路と同じ。host と先頭パスの比較キー）。`frontier_key` が NULL のときは `identity_key`（末尾 `/` を含む）を使うので、同じサイトでも hash の元が旧経路と変わる。名前は title（80 字まで）、空または成人なら host。`last_crawled_at` は `last_fetched_at` を UTC に直した値（無ければ NULL）。
- 既に掲載済みのサイトは変更せず、`url_progress` だけ `published` にする。例外は `adult_content` が NULL のときだけ 0/1 を入れること（旧経路と同じ。migration 0035 のトリガが thumbnail の job を合わせる）。値が入っているものは上書きしない。
- URL は空白・制御文字・userinfo を含むものを `gate:no_url` で止める（`scripts/progress_identity.py` の検証）。
- ゲートに落ちた行は `state='candidate'`、`url_candidates.review_reason='gate:<理由>'`、`review_state='pending'`（黙って捨てない）。理由: `no_url` `no_host` `verdict_*`（pass/suspect 以外）`no_description` `bad_*_json` `no_valid_category` `empty_name` `too_many_params` `no_category`（D1 の `categories` に無い）`not_inserted_blocked_or_rejected_request`。
- バッチが失敗したら何も書かれず `approved` のまま残り（結果の `errors`）、`url_progress.updated_at` を更新して列の後ろに回す（失敗する行が先頭に居座らない）。選択は `updated_at` の古い順。
- 読み戻し検証に失敗したら、その id も事後検査の対象に入れ、**そこでループを止める**（`stopped=true`）。
- 各サイトを読み戻して検証し（行・カテゴリ・タグ・media jobs・成人除外・state）、最後に触った id だけ `scripts/llm/integrity.py` と同じ意味の検査（uncategorized・orphan・ancestor_duplicate・empty_text・duplicate_url・カテゴリ別件数 all/国/区・scope_counts）をかける。違反は結果の `integrity` に返る。

ランナー側に必要なもの（旧 `nightly.py` の `prod` 段 586-636 行の等価物。この段には入っていない）: 投入前の本番整合チェック、Time Travel の bookmark の取得と記録、1 夜の追加数の異常ガード、投入後の整合チェック。

判定段との接続: 判定段は `approved`（`review_state='approved'`）を `url_progress.state='approved'` に、人が見る行を `review_state='pending'` + `state='candidate'` にする。掲載段のゲート落ちも同じ形（`state='candidate'` + `review_state='pending'` + `review_reason='gate:...'`）で戻すので、人の審査待ちと同じ扱いになる。判定段が選ぶのは `fetched` と判定のない古い `reviewing` だけなので、`candidate` に戻した行が再判定されることはない。

置き換え対象（段 C の実行器が入ったら撤去する）: `nightly.py` の `sql`/`gate`/`prod`、`scripts/llm/export_approved_sql.py`。
テスト: `tests/test_enrich_publish_stage.py`（実 migration を適用した in-memory SQLite）。
