# オンライン補強パイプライン（enrich）

収集機から本番 D1 だけを正本として URL を終端状態まで進める。ローカルの SQLite・CSV・JSONL・キャッシュ・チェックポイントは作らない（メモリのみ）。

## 取得段

`scripts/enrich/fetch_stage.py`（`run(client, *, limit, apply, log)`）。`url_progress.state='discovered'` の行を優先順（下の「優先順」。`--priority none` なら `identity_key` の機械順）に取り、トップページを取得して `url_candidates` に特徴（title / description / lang / kana_count / text_len / ad_hits / ext_refs / flags / text_head / personal_score / 成人属性 / 種別 / 更新の兆候）を書く。取得・抽出は `scripts/pipeline` の fetcher / features をそのまま使う（公開 IP のみ・IP 固定・オフサイトのリダイレクト拒否・512KB・20 秒・robots.txt 遵守・同一ホスト 2 秒以上）。ローカルファイルは作らない。

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

## 合議段（`scripts/enrich/deliberate_stage.py`、migration 0039）

判定段の審査（`approve_policy`）で決まらず `url_progress.state='candidate'` / `url_candidates.review_state='pending'` に残った行（`review_reason` が空・`not_pass`・`stale_individual_*`・`no_description` など）を、3 匹の猫が投票して終点へ進める。`run(client, *, limit, apply, log, llm, budget_month, concurrency, heartbeat, llm_enabled)`。ランナーでは判定段と掲載段の間（`--stages fetch,judge,deliberate,publish`、既定は全段、`--deliberate-limit` 既定 20・上限 200）。

- 3 匹（性格は Issue #53 で確定、指示文は `PROMPTS`）: **白猫** = 用心深い番猫（安全と法。なりすまし・著作権・個人情報・マルウェア/誘導。厳しめ、迷えば保留/却下。モデル sonnet）/ **黒猫** = 潔癖な審査員（独立性と質。広告中心・量産・パーキング・更新停止。中庸。haiku）/ **よもぎ猫** = 好奇心旺盛な探検家（手作り感・個性・ニッチ・長続き。寛容、迷えば承認。haiku）。各猫は `{vote: approve|hold|reject, reason, evidence_quote?}` を返す。理由は事実の短い日本語 1 文（「スパム」などのレッテルは検証で弾く）。成人向けは属性にすぎず、それだけで reject しない（指示文に明記。成人向けの行への reject で理由が成人向けを挙げていれば無効票として 1 回再試行）。人気・流入・規模・フォロワー・被リンクは基準にしない（各指示文に明記）。入力は取得・判定段が保存した材料（`<candidate>` と `<judge_notes>`）を「信用できないデータ」として渡す。LLM にツールは無い。`evidence_quote` はサイト本文の逐語であること（`judge.quote_source` で検査）。
- 決定: 承認 2 以上 → `review_state='approved'`・`review_reason='deliberation:approve'`・`url_progress.state='approved'`（掲載段が拾う）/ 却下 2 以上 → `rejected` / `deliberation:reject` / それ以外（保留多数・1/1/1・1 匹でも 1 回の再試行後に失敗）→ `candidate`/`pending` のまま `deliberations.status='meeting'`（会議中。人の判断待ち）。書き込みは claim（`deliberations` の `running` 行と `lease_until` 30 分）を条件にした 1 行 1 バッチで、読み戻して検証する。再実行しても同じ入力（`input_hash`）の `decided`/`meeting` 行は選ばれない。
- 対象外: `review_reason` が `public_body` と `gate:*`（掲載段の保留。データの修復が要るので投票しない）。並びは `identity_key` の機械順。
- 費用: 1 行 3 呼び出し。判定段の `Ledger`（`llm_ledger` への reserve → settle、月の上限）をそのまま使い、月の上限は判定段と共有。上限に達したら claim を戻して `budget_stopped` で止まる（ランナーは合議段だけ外して続ける、exit 0）。API 全体のエラー（認証・課金・過負荷）は reserve を release して止まる（行の試行回数は動かさない）。応答が曖昧（通信断）な呼び出しは再送せず reserve を残し、その猫は失敗扱い（その行は会議中）。ランナーの停止・終了コード・費用ガード・リースは判定段と同じ（合議段も `judge_unapproved_window` が立てば LLM を呼ばない）。異常ガード: 選んだ行が 10 件以上で猫の失敗が半分を超えたら exit 1。
- 概算費用: 1,000 行で約 US$8（白猫 sonnet が 1 行約 US$0.007、黒猫・よもぎ猫 haiku が各約 US$0.0004。本文は 4,000 文字まで送る）。

### 会議中の行を人が決める

```sql
-- 一覧（票と理由）
SELECT d.identity_key, d.shiro_vote, d.shiro_reason, d.kuro_vote, d.kuro_reason, d.yomogi_vote, d.yomogi_reason
FROM deliberations d WHERE d.status='meeting' ORDER BY d.identity_key LIMIT 50;
-- 承認(掲載段が拾う): review_state と url_progress を同時に動かす
UPDATE url_candidates SET review_state='approved', reviewed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), review_reason='human:approve' WHERE identity_key=? AND review_state='pending';
UPDATE url_progress SET state='approved', updated_at=datetime('now') WHERE identity_key=? AND state='candidate';
-- 却下: 上の 2 文の approved を rejected に、review_reason を 'human:reject' に
-- もう一度合議にかける: 行を消す(次の実行で再投票)
DELETE FROM deliberations WHERE identity_key=? AND status='meeting';
```

人が決めた行は `review_state` が `pending` でなくなるので、合議段はもう選ばない（`deliberations` の `meeting` 行は記録として残す）。

テスト: `tests/test_enrich_deliberate_stage.py`（実 migration の in-memory SQLite と偽 LLM）。

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

ランナー側に必要なもの（旧 `nightly.py` の `prod` 段の等価物）は `scripts/enrich_online.py` が持つ（下の「ランナー」）。

判定段との接続: 判定段は `approved`（`review_state='approved'`）を `url_progress.state='approved'` に、人が見る行を `review_state='pending'` + `state='candidate'` にする。掲載段のゲート落ちも同じ形（`state='candidate'` + `review_state='pending'` + `review_reason='gate:...'`）で戻すので、人の審査待ちと同じ扱いになる。判定段が選ぶのは `fetched` と判定のない古い `reviewing` だけなので、`candidate` に戻した行が再判定されることはない。

置き換え対象（段 C の実行器が入ったら撤去する）: `nightly.py` の `sql`/`gate`/`prod`、`scripts/llm/export_approved_sql.py`、旧 `export_sql.py`（非 published の `url_progress.state` を上書きし `reviewing` を作る。最初の `--apply` の前に停止する）。
テスト: `tests/test_enrich_publish_stage.py`（実 migration を適用した in-memory SQLite）。

## 優先順（`scripts/enrich/priority.py`）

LLM 費用（月上限）を、掲載数が目標（`FLOOR`=100 サイト）に届いていない葉カテゴリを埋める行へ先に使う。**順序は機械的**で、人気・規模・トラフィックは使わない。取得段（`discovered`）と判定段（`fetched`）が対象。claim は変えず、選択の `ORDER BY` だけが変わる。

- 葉 = `categories` で子を持たない行（132）。不足 = `max(0, 100 − 掲載数 − 処理中)`。掲載数は `site_categories` の葉ごとの件数、処理中は `url_progress` が `fetched`/`reviewing`/`approved`/`queued` の行（判定済みなら `url_candidates.categories` の先頭、無ければ `hint_leaf`）。集計 SQL は 3 本。
- rank 0: `hint_leaf` が不足のある葉（不足が大きい葉が先、次に `identity_key`）／ rank 1: ヒントなし・葉でない値（`identity_key` 順）／ rank 2: ヒントの葉が既に 100 以上（落とさず後回し。`identity_key` 順）。
- 葉 id は `categories` 表の id を正規表現で検証して `CASE` に定数で埋め込む（束縛変数 0、D1 の 100 個制限に触れない）。選択は `LIMIT` で絞ったまま。
- 公平枠: 段ごとの選択 5 回に 1 回（`FAIRNESS_EVERY`）は優先せず `identity_key` 順だけで選ぶ。rank 2 が永久に止まらない。
- 行の葉は 1 つの式（判定済みなら `categories` の先頭、無ければ `hint_leaf`）で、処理中の数え方と rank の両方に同じものを使う。
- **依存**: 取得段の `discovered` 行には `url_candidates` 行（と `hint_leaf`）が無いことが多い（作るのは取得後）。ヒントを付けるのは発見側（#269: 発見時に `hint_leaf` つきで `url_candidates` へ upsert）で、それと組み合わさって効く。ヒントの無い行（`url_candidates` 行なしを含む）は rank 1 で `identity_key` 順に処理される。既に `hint_leaf` を持つ旧 frontier 由来の Wikidata 行には rank 0/2 が効く。
- **D1 の読み取り**: 並びは `CASE` の `ORDER BY` で、選択のたびに対象状態の全行（約 3 万行）を読む（読み取り行数の課金）。段ごと・回ごとに 1 回なので許容し、候補の絞り込み（rank 別の `LIMIT` 取得）はしていない。
- 毎回、不足の大きい上位 5 葉と選ばれた行の rank 0/1/2 の件数をログに出す。
- `--priority leaf|none`（ランナー既定 `leaf`）。各段の `run(..., priority=None)` は従来どおり（`Selector` を渡したときだけ有効）。
- 掲載段の選択は変えない。熟議段はこの時点の main に無いので対象外。
- テスト: `tests/test_enrich_priority.py`。

## ランナー（`scripts/enrich_online.py`）

取得 → 判定 → 合議 → 掲載を 1 回の実行で回す。ローカルに状態・ログ・チェックポイントを持たない（標準出力のみ）。続きの位置は `url_progress` の状態そのもので、どの収集機から再実行しても続きから進む。排他は D1 `discovery_cursors` のリース（`source='lease'`, `cursor_key='enrich'`。URL 発見ランナーの `discover` とは別キー）。

```
uv run python scripts/enrich_online.py                                  # dry-run（既定。各段の対象件数だけ。1 回のみ）
uv run python scripts/enrich_online.py --apply --rounds 0               # 残りが無くなるか停止条件まで
uv run python scripts/enrich_online.py --apply --stages fetch,judge --limit 30 --max-seconds 3000
```

主な引数: `--stages`（既定は全段）、`--limit`（全段共通）/`--fetch-limit`/`--judge-limit`/`--deliberate-limit`/`--publish-limit`（既定 60 / 30 / 20 / 60。上限 200、超える指定は引数エラー。リースの有効時間に収めるため）、`--rounds`（0 = 残り無しまで）、`--max-seconds`（超えたら次の回に入らない）、`--budget-month`、`--max-publish-per-day`（日次の掲載上限。既定 5000、1 以上。0 以下は引数エラー）、`--judge-concurrency`（1〜4）、`--priority leaf|none`（既定 leaf。「優先順」）、`--dry-run` / `--apply`。`ANTHROPIC_API_KEY` と `CLOUDFLARE_API_TOKEN` は環境変数のみ。

終了コード: `0` 正常（残り無し・予算上限・`--max-seconds`・`--rounds`・日次の掲載上限）/ `1` 検証不一致・整合チェック違反・異常ガード・前提チェック不合格・判定段の停止・判定の費用ガード / `2` リースが取れない・引数不正 / `3` レート制限（429）/ `4` 実行中にリースを失った / `5` 費用台帳を確定できない（人の対応が必要）。**スケジューラは 5 で再試行しない**（再実行のたびに課金して `reserve` 行が増える。「残った reserve 行の解消」を人が済ませてから再開）。

開始時（`--apply`）: 状態別件数・`sites` 件数・当月の LLM 費用・`url_progress.state='approved'` なのに `review_state` が `approved` でない行の件数（掲載段は選ばない）を表示し、Time Travel の bookmark を取得して表示する（取れなければ警告して続行）。判定段を含む実行のときは、「実行と排他」の前提（`reviewing` かつ `review_state` 空）が 30 分より古く残っていれば開始しない（exit 1。ids を表示）。判定段自身が作る `reviewing` も `review_state` は空だが `updated_at` が新しいので 30 分以内の行は数えない。判定段が中断・停止する経路は claim を戻すので、通常の中断後に古い claim は残らない。プロセスが強制終了されて残った claim は旧データと区別できない（判定段は 30 分超の `reviewing` をどれも再 claim するため）。ids を確認し、判定段自身の跡と分かったものだけ次の SQL で戻す: `UPDATE url_progress SET state='fetched', updated_at=datetime('now') WHERE state='reviewing' AND identity_key IN (...) AND identity_key IN (SELECT identity_key FROM url_candidates WHERE review_state IS NULL OR review_state='')`（旧データなら戻さず、先に中身を確認する）。**最初の `--apply` の前に一度だけ**、運用者が上の SQL で件数を数える（0 であること）。**旧 `export_sql.py` は非 published の `url_progress.state` をローカル frontier で上書きし `reviewing` を作る**ので、最初の `--apply` の前に停止しておく（置き換え対象。止めないと状態を巻き戻す）。

各回: 取得 → 判定 → 合議 → 掲載の順に、段ごとにリースを更新してから実行し、段の件数を表にして出す。掲載後は掲載段の `post_check`（整合チェック）の結果を見る。

停止条件:
- 全段で対象が 0 件 / 動かせる段が無い（判定段は予算上限で外れる）/ `--rounds` / `--max-seconds` / 日次の掲載上限（既定 `MAX_PUBLISH_PER_DAY`=5000、`--max-publish-per-day` で変更。D1 の直近 24 時間に作られた `sites` から数える）→ exit 0
- 1 回動かして `url_progress` の件数が 1 件も変わらない（対象はあるが進まない）→ `WARNING` 行を出して exit 0
- 掲載の保護（異常ガード。定数は `enrich_online.py` 冒頭）→ exit 1: バッチ失敗が 5 件以上または選択の 50% 超（選択 4 件以上）／ゲート保留が選択の 50% 超（選択 10 件以上）／読み戻し検証の不一致・整合チェック違反（違反の種類と行、その回に掲載した id を表示。bookmark で復元を検討。掲載が例外で途中終了しても整合結果は表示する）
- 判定段の費用ガード: 直近 6 時間（`JUDGE_GUARD_HOURS`、D1 時刻の `judged_at`）の判定が 100 件（`JUDGE_GUARD_WINDOW`）以上あり `approved` が 0 件なら、LLM を呼ばない（判定段は `llm_enabled=False` で動き、`review_state` を持つ行の LLM を使わない移動だけ行う。取得・掲載は止めない）。最後に exit 1。**回復**: 判定の内容（`url_candidates` の最近の `review_reason`）を確認して原因を直し、6 時間待つと窓が空になり自然に再開する（窓は広げない）
- 費用台帳を確定できない（`LedgerSettleError`）→ 予約は残したまま exit 5。開始時にも、6 時間より古い `llm_ledger` の `reserve` 行が残っていれば開始せず exit 5（解消するまで再開しない）
- レート制限 → exit 3（判定段が止まった理由に 429 / rate limit を含むときも exit 3。HTTP ステータス 429 を例外から取れればそれ、無ければ文言 `HTTP 429` / `API 429` / `rate limit`）、リース喪失 → exit 4
- リースは段の境目に更新し、段の内部（取得は claim ごと、判定は chunk ごと・書き込み前、掲載はサイトごと）でも 2 分ごとに更新する。失っていれば次の書き込みの前に中断する（各段の `run(..., heartbeat=None)`）。D1 の一時エラーでの更新失敗は次の拍で再試行し、3 回連続で失敗したときだけリース喪失（exit 4）扱い。判定段が中断したときは、課金済みの結果を claim 条件つきで書いてから、残りの claim を `fetched` に戻す（古い `reviewing` を残さない）

終了時は `url_progress` の状態別件数（`discovered … published`）と開始時からの差を表示する。リースは終了時に必ず解放する（異常終了時は期限 30 分で切れる）。

別の収集機で再開する: 同じコマンドをそのまま実行する。リースが残っていれば exit 2（期限切れまで待つか、動いている実行を終わらせる）。定期実行の差し替え（スケジューラのタスク定義）は運用者が行い、実行コマンドを上のものに置き換える。旧 `nightly.py` の `sql`/`gate`/`prod` 段と `export_approved_sql.py` はこのランナーに置き換わる。

テスト: `tests/test_enrich_runner.py`（偽 D1・偽取得・偽 LLM）。
