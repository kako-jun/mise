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

### 空ページの却下（`empty_content`、LLM 無し）

本文が空で**載せるものがまだ無い**ページだけを却下する。サイトの種類（個人・小規模・成人向け・ブログ）では判断しない。規模・人気も見ない。`text_len` は可視本文だけを数え、title・meta description・画像の alt・noscript・frame は数えない。SPA の殻・frameset・画像ギャラリー・画像の中に文字がある店・リンクだけのトップ・ENTER ページ・Flash ページは本文が短くても空ではないので、**次を全部満たす行だけ**を却下し、それ以外は従来どおり LLM に回す（`judge_stage.is_empty_content`）:
- `text_len` が分かっていて `EMPTY_TEXT_LEN`（80）未満（NULL は空とみなさない）
- 取得した meta description（`url_candidates.description`）と `description_ja` が空
- title が空、または汎用のプレースホルダ（定数 `PLACEHOLDER_TITLES_EXACT`: 無題・untitled・no title・welcome・home・index・top・トップ・トップページ・ホーム・ようこそ・new page・my site・test・テスト（正規化後に完全一致）／`PLACEHOLDER_TITLE_PARTS`: index of・まだ記事がありません・記事がありません・準備中・coming soon・under construction・工事中・default web site page・it works・welcome to nginx・apache2 default（含む））
- 外部参照が無い（`ext_refs` が 0 または NULL）
- `approve_policy.public_looking` でない（公的機関らしい行は従来の `public_body` の道）。成人向けも他と同じく `empty_content` になるだけ（成人向けは属性）。パーキング・売り物・noindex は別の箇所で扱うので重ねない。

判定段は LLM を呼ぶ前にこの行を取り分け（`llm_enabled=False` でも実行）、claim してから `url_progress.state='rejected'`、`url_candidates.review_state='rejected'`、`review_reason='empty_content'`、`next_fetch_at`＝D1 の時計で 30 日後（`EMPTY_REOPEN_DAYS`）を書き、状態を読み戻す。統計は `empty_rejected`（dry-run は `would_reject_empty`）。

**バックフィル**（判定済みの `candidate`/`pending` 行。LLM 無し）: `uv run python scripts/enrich_online.py --backfill-empty [--apply]`（既定 dry-run は件数だけ。リースを取り 1 回で終了。やり直しても同じ結果）。同じ判定に加え、救える行は除外する: `spam_verdict` が pass/suspect の行（承認後の説明生成の対象）、`review_reason` が `public_body` または `deliberation:%` の行、`deliberations` に approve が 2 票以上ある行、合議中（実行中のリースがある行）。ハートビートは無いので、リースの有効時間（30 分）に収まる件数を前提にする（数百件）。

**再開**: `next_fetch_at` は**印にすぎず、自動では再開されない**。期限が来た行を取得段へ戻すのは `uv run python scripts/enrich_online.py --reopen-empty [--apply]`（既定 dry-run は件数だけ。リースを取り 1 回で終了）。手で行うなら次の 2 文を**必ず 1 つのバッチ（トランザクション）で**、この順に実行する。取得段が間に claim しても、2 文目は `discovered`/`queued`/`fetched` のどれでも判定の印を外すので取りこぼさず、再実行で残りを拾える（`url_candidates` 側を先に消さない）:
```sql
UPDATE url_progress SET state='discovered', updated_at=datetime('now') WHERE state='rejected' AND identity_key IN
  (SELECT identity_key FROM url_candidates WHERE review_reason='empty_content' AND review_state='rejected' AND next_fetch_at<=strftime('%Y-%m-%dT%H:%M:%SZ','now'));
UPDATE url_candidates SET review_state=NULL, review_reason=NULL, reviewed_at=NULL, updated_at=datetime('now')
  WHERE review_reason='empty_content' AND review_state='rejected' AND identity_key IN (SELECT identity_key FROM url_progress WHERE state IN ('discovered','queued','fetched'));
```
再取得で本文が増えていれば通常どおり判定へ進み、まだ空なら再び `empty_content`（さらに 30 日後）。

## 合議段（`scripts/enrich/deliberate_stage.py`、migration 0039）

判定段の審査（`approve_policy`）で決まらず `url_progress.state='candidate'` / `url_candidates.review_state='pending'` に残った行（`review_reason` が空・`not_pass`・`stale_individual_*`・`no_description` など）を、3 匹の猫が投票して終点へ進める。`run(client, *, limit, apply, log, llm, budget_month, concurrency, heartbeat, llm_enabled)`。ランナーでは判定段と掲載段の間（`--stages fetch,judge,deliberate,requalify,publish`、既定は全段、`--deliberate-limit` 既定 20・上限 200）。

- 3 匹（性格は Issue #53 で確定、指示文は `PROMPTS`）: **白猫** = 用心深い番猫（安全と法。なりすまし・著作権・個人情報・マルウェア/誘導。厳しめ、迷えば保留/却下。モデル sonnet）/ **黒猫** = 潔癖な審査員（独立性と質。広告中心・量産・パーキング・更新停止。中庸。haiku）/ **よもぎ猫** = 好奇心旺盛な探検家（手作り感・個性・ニッチ・長続き。寛容、迷えば承認。haiku）。各猫は `{vote: approve|hold|reject, reason, evidence_quote?}` を返す。理由は事実の短い日本語 1 文（「スパム」などのレッテルは検証で弾く）。成人向けは属性にすぎず、それだけで reject しない（指示文に明記。成人向けの行への reject で理由が成人向けを挙げていれば無効票として 1 回再試行）。人気・流入・規模・フォロワー・被リンクは基準にしない（各指示文に明記）。入力は取得・判定段が保存した材料（`<candidate>` と `<judge_notes>`）を「信用できないデータ」として渡す。LLM にツールは無い。`evidence_quote` はサイト本文の逐語であること（`judge.quote_source` で検査）。
- 追加の歯止め（すべて自動決定を止めて人へ回す向き。#53 の素の 2/3 多数決からの変更）:
  - **白猫が approve でなければ自動承認しない（`SAFETY_VETO = True`）**: 白猫が hold か reject の行は、他の 2 匹が承認でも会議中にする。黒猫・よもぎ猫は安全面を見ない指示なので、その 2 票は安全の情報を含まないため。安全側に倒す変更で、元に戻すなら定数 1 つ（`False`）。reject 多数による却下は従来どおり。
  - **自動却下は `adult='no'` かつ `fetch_adult` が `yes` でない行だけ**: `adult` が `yes`・NULL・`unknown`（判定段が属性を付け損ねた行を含む）の reject 多数は必ず会議中にする（承認多数はそのまま通す）。語の列挙では言い換えを塞ぎきれないので構造で守る。補助として、reject の理由に成人向けを示す語（成人・アダルト・R18・性的・わいせつ・露骨・ヌード・風俗・卑猥・sexual・explicit 等）があれば無効票（1 回再試行し、なお直らなければその猫は失敗 = 会議中）。
  - **reject には根拠の引用が必須**（`evidence_quote`。サイト本文の逐語）。無い reject は無効票で 1 回再試行、なお無ければその猫は hold 扱い。理由は 80 字以内、「スパム」「詐欺サイト」「悪質」などの断定語は無効。
  - **承認後の説明生成（describe-after-approve）**: 承認多数（白猫の承認を含む）なのに、掲載段のゲートに落ちる理由が説明・カテゴリ・tags の欠落／不正だけ（`gate:no_description` `gate:no_valid_category` `gate:bad_categories_json` `gate:bad_tags_json`）の行は、会議中にする前に判定段の describe を 1 回だけ呼ぶ（`judge_stage.describe_row`。指示文・検証・費用台帳（reserve → settle・月の上限）は判定段のものをそのまま使う。保存されているカテゴリが掲載ゲートの検査で使えない行だけ（最初のゲート理由ではなく実際の有効性で判断）、同じ triage（haiku）を先に 1 回呼んでカテゴリを得る。説明もカテゴリも無い行は 1 回の describe（＋triage）で両方が付く。判定段と同じく、サイト本文・title・description の山括弧は全角に置き換えて渡す）。得た `description_ja/en`・長い説明・tags・国/地域（既存値は消さず埋めるだけ・カテゴリは必要なときだけ）は、判定結果と同じ 1 つのバッチで claim の条件つきで書き、その結果でゲートをもう一度当てる。通れば `approved`（`review_reason='deliberation:approve+described'`）、通らなければ会議中（`deliberation:needs_review:<ゲート理由>`）。describe の失敗・予算上限・API 全体のエラーも会議中（元のゲート理由。API エラーと予算上限は run を止める）。`spam_verdict` が pass/suspect でない行（`gate:verdict_*`）と reject/hold 多数の行は対象外で、これまでどおり会議中。成人向けの行は長い説明を作らない（判定段と同じ方針）。判定段で「pass だが説明なし」だった行（`no_description`）も、この経路で説明が付く。費用は 1 行あたり describe（sonnet）1 回（＋カテゴリ欠落のとき triage 1 回）で、統計に `described`（再ゲートを通って承認した行だけ）/ `describe_failed`（失敗、または描いたがゲートを通らなかった行）/ `describe_cost_usd` を出す。並列実行でも describe は 1 件ずつ（ロック）で、停止の印と費用の差分は混ざらない（`cost_usd` は猫と describe の合計。月の上限は共有）。
  - **掲載段のゲートを通らない行は承認しない**: 承認多数でも `publish_stage.gate_reason`（掲載段が使う `plan` と同じ判定。コピーではなく共通の関数。`spam_verdict` が pass/suspect のみ・説明文・カテゴリ・tags の JSON など）に引っかかる行は会議中にし、`review_reason='deliberation:needs_review:<ゲート理由>'`（例 `...:gate:verdict_review`）を付ける。掲載段の `gate:*` 保留と保留率ガードを作らないため。reject 多数による却下はゲートと無関係にそのまま可。
  - **公的機関は投票しない**: 選択後に `approve_policy.public_looking` を決定論で当て、該当行は LLM を呼ばず `review_reason='public_body'` に付け替える（以後の選択に出ない。窓を広めに取って繰り返し、公的機関の行が続いても後ろの通常行を取りこぼさない。打ち切ったときは取れた分だけ投票し残りは次回）。dry-run は書かず、対象から外すだけ。
  - **本文の山括弧を無害化**: サイト本文と `<judge_notes>` の値の `<` `>` は全角にして、本文でタグを閉じて偽の指示を差し込めないようにする（system には user メッセージ全体がデータだと明記）。
  - 黒猫の基準: 「量産・使い回し（同一内容の大量ページ）」に絞り、共通のブログテーマ・無料ホスティング・既製テンプレートの利用は該当しない。放置は決定論の日付証拠がある場合のみ reject、無ければ hold まで。
- 決定: 承認 2 以上 → `review_state='approved'`・`review_reason='deliberation:approve'`・`url_progress.state='approved'`（掲載段が拾う）/ 却下 2 以上 → `rejected` / `deliberation:reject` / それ以外（保留多数・1/1/1・1 匹でも 1 回の再試行後に失敗）→ `candidate`/`pending` のまま `deliberations.status='meeting'`（会議中。人の判断待ち）。書き込みは claim（`deliberations` の `running` 行と `lease_until` 30 分）を条件にした 1 行 1 バッチで、読み戻して検証する。再実行しても同じ入力（`input_hash`）の `decided`/`meeting` 行は選ばれない。
- 出力枠: 各猫の呼び出しは `max_tokens` 1,200・思考を無効（`pipeline.thinking_param`）。回答は短い JSON（理由 80 字・引用 100 字まで）で数百トークンに収まる。初回の 500 は、思考が出力枠を食って haiku の出力が `stop_reason=max_tokens` で途中で切れ、猫が失敗した（30 行中 29 行が会議中）。出力が切れたときの再試行は、同じ設定では同じ所で切れるので `max_tokens` を 3,000 に上げて 1 回だけ。それでも切れたらその猫は hold 扱い（失敗ではなく棄権）。引用の検査は判定段と同じ空白の正規化に NFKC（全角半角）と前後の括弧引用符の除去を足し、「本文の連続した部分文字列であること」は維持する。
- 対象外: `review_reason` が `public_body` と `gate:*`（掲載段の保留。データの修復が要るので投票しない）。並びは `identity_key` の機械順。
- 費用: 1 行 3 呼び出し。判定段の `Ledger`（`llm_ledger` への reserve → settle、月の上限）をそのまま使い、月の上限は判定段と共有。上限に達したら claim を戻して `budget_stopped` で止まる（ランナーは合議段だけ外して続ける、exit 0）。API 全体のエラー（認証・課金・過負荷）は reserve を release して止まる（行の試行回数は動かさない）。応答が曖昧（通信断）な呼び出しは再送せず reserve を残し、その猫は失敗扱い（その行は会議中）。ランナーの停止・終了コード・費用ガード・リースは判定段と同じ（合議段も `judge_unapproved_window` が立てば LLM を呼ばない）。異常ガード: 選んだ行が 10 件以上で猫の失敗が半分を超えたら exit 1。
- 概算費用: **実測は 30 行で約 US$0.10（1,000 行で約 US$3.4）**。当初の机上計算（約 US$8）は白猫の入力量を多く見積もりすぎていた。切り詰めの再試行（下）や無効票の再試行が増えれば、その分だけ上振れする。

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

合議の最中に人が先に決めた行は、行の移動（`review_state` / `url_progress`）は claim と `pending`/`candidate` の条件で効かず、票だけが `deliberations` に残る（実行の統計では `lost_claim`）。人が決めた行は `review_state` が `pending` でなくなるので、合議段はもう選ばない（`deliberations` の `meeting` 行は記録として残す）。

テスト: `tests/test_enrich_deliberate_stage.py`、`tests/test_enrich_empty_and_describe.py`（空ページの却下・補正・承認後の説明生成。実 migration の in-memory SQLite と偽 LLM）。

## 掲載段（`scripts/enrich/publish_stage.py`）

`url_progress.state='approved'` の URL を `url_candidates` と結合し、1 サイトごとに **1 回の `client.batch`（D1 では 1 トランザクション）** で掲載する。
`run(client, *, limit, apply, log=print)`。`apply=False` は読み取りだけで、何も書かない。

1 バッチの中身:

1. `sites` を INSERT（既掲載 URL（正規化 URL と `progress_key` の両方）・拒否ホスト・拒否済み申請・同じ id がある場合は入れない。`ON CONFLICT DO NOTHING`）
2. `site_tags`（最大 5。`label_ja` / `label_en` は別々。品質ゲートは下の「品質の規則」）
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

## 品質の規則（`scripts/enrich/quality.py`、Issue #273）

公開される項目（サイト名・日本語の説明・タグ・カテゴリ）の規則を **1 か所（`quality.py`、副作用なしの関数）** に置き、判定段・合議段（describe）・掲載段のゲート・整合チェックが同じ関数を使う。人気・規模の基準は無い。意味を書き換えない・事実を足さない（修復は形だけ）。

| 項目 | 規則（定数） | 違反コード |
|---|---|---|
| サイト名 | **掲載段が付ける名前は `derive_name`**（下の「`names`」の規則: title のまま・定型の節だけ外す・60 字 `NAME_SOFT_MAX` まで節の境界で。最初の節は 80 字 `NAME_LONG_MAX` まで）。次の `check_name` は、判定段・合議段の LLM が返す名前（`sn`）と `url_candidates.site_name` の検査、是正ツールの選定に使う: 40 字以内（`NAME_MAX`）、空でない（**例外**: 区切りの無い 1 節の title は 80 字以内（`NAME_LONG_MAX`）まで、長い正式名称としてそのまま載せる。宣伝語・文を含まない場合）。区切り（`｜` `|` ` - ` ` – ` ` / ` `／`）で 3 つ以上に分かれる、または 2 つのうち短い方が 12 字超・長い方が 24 字超・文になっている、は一覧／キャッチコピーとして不可。宣伝語（`PROMO`: 公式通販・オンラインショップ・を提案 …）は区切りがある／20 字超のときだけ合図として違反。文（「。」を含む）も不可 | `name_empty` `name_too_long` `name_list` `name_promo` `name_sentence` |
| 説明（日本語） | 体言止め、または「です・ます・でした・ました」。常体（である・だ・ている・する・れる・った・ない …、および「。」で終わる辞書形（行う・描く・届ける）と過去形（載せた・集めてきた））で終わる文が 1 つでもあれば違反（辞書形・過去形は、漢字／カタカナの語幹＋送り仮名の動詞だけを見る。おやつ・ほたる・みんなのうた・世界のおやつのような名詞は検査外。「〜からだ。」型の文末は常体として残す） | `description_style` |
| タグ | 1 つが `"日本語ラベル\|english label"`（辞書形式でも可）。`label_ja` にかな・漢字が必要、`label_en` は日本語を含まない別のラベル。日本語 20 字・英語 40 字以内（半角カナは NFKC で日本語として扱う）、最大 5 つ、`市` `町` `県` などの行政区分語だけのタグは不可（`tag_admin_only`）、ラテン表記の固有名詞は `data/tag-proper-nouns.csv`（許可リスト。FPGA・iOS・Linux など。読み込みで検証）にあれば `label_ja` に置ける、`normalize_tag_key`（全角半角・大小・単複・ハイフン／空白）と `data/tag-synonyms.csv` で同じになるものは重複 | `tag_label_ja_not_japanese` `tag_admin_only` `tag_label_en_not_english` `tag_duplicate` `tag_too_long` `tag_count` `tag_empty` `tag_malformed` |
| カテゴリ | 葉（子を持たない id）だけ、1 つ以上。**親の `business` `society` も不可**（旧 id の退避先として `modernize_categories` が返しても、オンライン経路では `gate:quality_categories` で止め、triage の指示文にも「親・退避の指示より優先して葉だけ」と書く。旧 SQLite 経路は変えない） | `category_non_leaf` `category_none` |

- **判定段**: 描画段（describe）の指示文に文体・タグ形式の規則、triage の指示文に「葉だけ」の規則を足した（`pipeline.describe_system/triage_system(cats, quality=True)`。**旧 SQLite 経路の指示文は変えない**）。出力検証（`pipeline.check_item_ex`）が上の違反を見つけたら、その項目だけ**違反の内容を添えて 1 回だけ再依頼**し、それでも直らなければ出力は残したまま `review_state='pending'`・`url_progress.state='candidate'`・`review_reason='quality:<description|tags|categories>'` にして人へ回す（自動承認しない）。カテゴリ違反の行は費用をかけないため describe を呼ばない。統計: `quality_retries`、`quality_held`。合議段の describe（`describe_row`）で違反が残った行は、承認せず会議中のまま。
- **掲載段のゲート**（`gate_reason` / `plan`。合議段も同じ関数）: 次の順で再検査し、通らなければ掲載せず `candidate` に戻す（`review_reason`）。
  - 名前: `quality.suggest_name`（= `derive_name` の `name`。title に出ているままを採り、定型の節（トップページ・ホーム・公式サイト・共有サービス名・エラーページ …）と重複と電話番号などだけを外し、残りを順に 60 字まで節の境界でつなぐ。選別・言い換えはしない。詳細は「是正ツール → `names`」）。何も残らない（定型だけ・境界の無い 80 字超）ときは LLM が付けた `url_candidates.site_name`（migration 0040・非公開。describe の `sn`。title か本文からの逐語・40 字以内を検証し、無ければ null）を使い、それも無ければ `gate:quality_name`。直した（title と違う）ときは `repaired.name` に数える。成人向け・title 無しはホスト名。`name_ja` / `name_en` は掲載段では書かず、是正ツール `names` が入れる。
  - 説明: 常体なら `to_polite`（です・ます化。「している→しています」「である→です」「する→します」「した→しました」「れる→れます」「ある→あります」など、**確実なものだけ**。「行った」「読んだ」のように語幹が変わるものは None）で直す。直せなければ `gate:quality_description`。
  - タグ: `gate:quality_tags`（直さない）。保存形式は `'日本語|english'` の文字列で、掲載時に `label_ja` / `label_en` に別々に入れる。**従来の英語だけのタグを持つ承認済みの行は、このゲートで保留になる**（下の `requalify` 段が describe を 1 回やり直して日本語ラベルを付ける）。
  - カテゴリ: 旧 id の変換後に `gate:quality_categories`。
  - 統計 `repaired`（`{name, description}`）は、直して掲載した（dry-run では掲載できる）件数。
- **再資格化の方針**: 判定段が品質違反を見つけても、承認方針が既に人／合議へ回す行（`pending`）は**元の `review_reason` を上書きせず**、`notes` の末尾に `[quality:<項目>]` を足すだけ。方針が承認する行だけ `quality:<項目>` の理由で保留にする。`requalify` はその行を直したあと `approve_policy.decide` を再実行し、方針が承認するときだけ `approved` にする（それ以外は方針の理由のまま人／合議の列へ）。掲載ゲートで止まった行（`gate:quality_*`）は承認済みだったので再承認する。
- **再資格化（`requalify` 段、`judge_stage.requalify`）**: 掲載ゲートが品質の理由（`gate:quality_*`）で止める承認済みの行（`state='approved'`）と、`state='candidate'` で `review_reason` が `gate:quality_%` / `quality:%` の行を対象に、違反内容を指示に書いて describe を 1 回（カテゴリが使えないときは triage も）呼び直す（判定段と同じ台帳・月の上限・山括弧の無害化。`describe_row` を使う。LLM の `site_name` は、名前を直せない行では必須）。結果に掲載ゲートをもう一度当て、通れば `approved`（`review_reason='requalified'`）、通らなければ `candidate`/`pending` のまま理由を更新する。失敗した行は `notes` の**先頭**に `requalify_attempts=N; ` を足し（元の notes はその後ろに残す。SQL の GLOB で試行を使い切った行を選択の前に除く）、 `url_progress.updated_at` を更新し列の後ろに回す（先頭で詰まらない）。1 行 2 回（`REQUALIFY_MAX_ATTEMPTS`）で打ち切り、以後は人だけが動かす。無応答（タイムアウト）は試行に数えず、使えない結果が 3 回続いたら停止する（判定段と同じ）。承認済みの行はページ単位で古い順に走査する（クリーンな行が続いても後ろの対象に届く。1 回 2,000 行まで）。ゲートが既に通る行（形はゲートが直す）は LLM を呼ばず承認する。`gate:quality_*` の保留は掲載段の保留率ガード（`PUBLISH_MAX_HELD_RATIO`）に数えず、`requalify` の選択件数はラウンドの選択件数に入る。事前確認（preflight）で `url_candidates.site_name` が無ければ開始しない（migration 0040 が先）。ランナーでは合議段と掲載段の間（`--stages …,requalify,publish`、`--requalify-limit` 既定 20・上限 200）。合議段の describe-after-approve も `gate:quality_*` を扱う（`DESCRIBABLE`、同じ `describe_row`）。統計: `selected` `requalified` `still_held` `describe_failed` `cost_usd`。
- **整合チェック**（`scripts/check-data-integrity.py`、warn のみ・終了コードに影響しない）: `quality:name_too_long`（`sites.name` の 80 字超 = `NAME_LONG_MAX`。`derive_name` が書く名前は最大 80 字）`quality:description_style` `quality:tag_label_ja_not_japanese` `quality:category_non_leaf_only` の件数を出す。基準値（`QUALITY_BASELINE`、2026-11-25 本番の実測。是正後は 0 に下げる）を超えたら `ABOVE BASELINE` と出す。
- **タグの同義語**: `data/tag-synonyms.csv`（`canonical_ja,canonical_en,variants`。variants は `|` 区切り）。読み込み（`quality.load_synonyms`）が全行を検証し（日本語の正準名・日本語を含まない英語名・variants あり・同じ語が複数の正準に属さない）、`tests/test_quality.py` が実ファイルを検査する。語を足すときは 1 行足してテストを通す。
- **migration 0040**: `url_candidates.site_name TEXT`（非公開）。コードより先に本番へ適用する（判定段の UPDATE がこの列を書く）。
- テスト: `tests/test_quality.py`（関数・同義語ファイル・整合チェック）、`tests/test_enrich_quality_stages.py`（判定の再依頼・保留、掲載の修復・保留）。
- 既存の掲載済みデータの是正は下の「是正ツール」（この規則そのものは新しく載る行だけを止める）。

## 是正ツール（`scripts/cleanup_quality.py`、Issue #273 第 2 段）

掲載済みのサイトを、上の規則（`quality.py`）に合わせて本番 D1 で直接直す。`names`（サイト名）と `descriptions`（日本語の説明の文体）。`tags` / `categories` は次の PR。

```
uv run python scripts/cleanup_quality.py names        [--limit N] [--dry-run | --apply] [--recompute] [--llm-fallback] [--restart] [--retry-skipped]
uv run python scripts/cleanup_quality.py descriptions [--limit N] [--dry-run | --apply] [--restart] [--budget-month USD]
```

- **既定は `--dry-run`**: 何も書かず LLM も呼ばない。件数（規則で直せる・LLM が要る・LLM の費用の上限見積り）と、前後の例 20 件を出す。`--limit` は 1 回に直す行数（既定 200、最大 1000）。
- **ローカルに状態を持たない**: ファイル・ログを作らない。対象は毎回 SQL で導き直す（直った行は規則を通るので選ばれない = 冪等・どの PC からでも続きができる）。状態は D1 `discovery_cursors` だけ: 走査の位置（`source='cleanup'`, `cursor_key='names'|'descriptions'`、値は最後に選んだ `sites.id`。表の終わりで先頭に戻る。`--restart` で先頭から）と、直せなかった行（`needs_human`）の印（`source='cleanup-skip'`, `cursor_key='<names|descriptions>:<site_id>'`、値は理由と日付。サイトのデータではない）。印のある行は以後の走査で飛ばし、LLM に再び出さない（`--retry-skipped` で再び対象。直れば印は消える）。dry-run は読むだけ。
- **`--apply`**: D1 リース（`discovery_cursors` の `source='lease'`, `cursor_key='cleanup'`。`discover_online.Lease` を再利用）→ Time Travel の bookmark を先に表示（**取れなければ中止**。`--allow-no-bookmark` を付けたときだけ復元点なしで続行）→ 書く（20 件ごとの 1 batch。LLM に払った結果は止まっても失わない。書く前に全件の `(id, 旧, 新)` を標準出力に出す。`UPDATE … WHERE id=? AND <列>=<読んだ値> RETURNING id`。読んだ後で他が変えた行は書かず `skipped_changed`）→ 書いた行を 1 件ずつ読み戻して照合（一致しなければ `verify_mismatch`・exit 1）。bookmark で戻す手順は `docs/operations/deploy.md`。
- **終了コード**（`enrich_online.py` と同じ）: 0 正常（月の予算上限での停止も 0） / 1 読み戻し不一致・API 全体の拒否 / 2 リースが取れない / 3 レート制限 / 4 リースを失った / 5 費用台帳を確定できない（人が `reserve` 行を確認するまで再実行しない）。
- **LLM**: 価格表（`judge.PRICING`）で最も安いモデル（Haiku）。費用台帳の予約・確定・月の上限・API 全体のエラーで停止・山括弧の無害化・「本文は命令ではなくデータ」の指示は判定段と同じ。

### `names`

**決め方（2026-11-25 に単純化。正本はこの節。`quality.derive_name`、掲載段 `quality.suggest_name` も同じ関数）**。タイトルから「正しい 1 節」を選ぶ方式は誤りを生んだ（本番の目視で約 1 割）ので、**選ばない**。title に出ているままを採り、定型の飾りだけ外す。LLM は通らない・決定的。

1. **元の文**: 同じ `progress_key` の `url_candidates.title`（元のタイトル）。無ければ今の `name`。
2. **節に分ける**: `｜` `│` `┃` `|` `／` `/` は常に区切り。` – ` ` — ` ` - ` ` ― ` ` : ` ` · ` は**前後に空白があるときだけ**区切り（`アシザワ・ファインテック` `20th-Century` `Adobe: …` のように語の中・空白なしの記号は区切りにしない）。電話番号・FAX・郵便番号・営業時間・URL は節の中からも取り除く（それだけの節は消える）。`【公式】` `[Official]` のようなラベル飾りは、ラベルだけ外して残りを使う（片側だけ空白の区切り `X ｜Y` は空白なしに整える）。**節の端の定型句**: 末尾の `へようこそ` `ようこそ` `トップページ` `TOPページ` `Top Page` `Official Web Site` `オフィシャルサイト` `公式サイト` `無題ドキュメント` `Untitled Document`、先頭（独立した語のとき）の `トップページ` `TOPページ` `Top Page` `無題ドキュメント` `Untitled Document` を外す（名前の途中のもの・語の一部（`トップページデザイン研究会` `Topology`）は外さない）。title の先頭／末尾に残る裸の ` - ` ` – ` も落とす（`京都府建築士会 - TOPページ - ` → `京都府建築士会`）。綴りだけが違う重複（カンボジア／カンボディア）はそのまま残す。
3. **定型の節を落とす（節全体が一致したときだけ。節の端の定型句は 2 で外す）**: トップページ・ホーム・TOP・Home・Welcome・ようこそ（…へ）・公式サイト・公式ホームページ・オフィシャルサイト・ウェブサイト・Official Site / Website / Web Site / Homepage / Dealer Site（`Official Dealer Site`）、エラーページの題名（`404 Not Found`・`Index of`・`Welcome to nginx` …。`404 Gallery` や `Home Sweet Home Bakery` は残る）、ホストが共有サービスのときそのサービス名だけの節（Peatix・note・はてなブログ・Ameba・FC2・Wix・Jimdo・ココログ・Facebook …。`data/platforms.txt` / `platform_pages.txt` / `quality.SHARED_EXTRA`）。節の**一部**に定型語が付いているもの（`Jeep Official Dealer Site`・`not found HGU`）は、2 の端の定型句でなければ節全体ではないので**残す**（言い換えない・推測しない）。
4. **重複を落とす**: 同じ節（NFKC + 大小無視）は最初の 1 つだけ。**並べ替え・言い換え・翻訳・追記はしない**。
5. **残りを順に、60 字（`NAME_SOFT_MAX`）に収まる節の境界まで**、title が使っていた区切り（既定 ` | `）でつなぐ。最初の節は必ず残し、80 字（`NAME_LONG_MAX`）以内ならそのまま全部（長い正式名称）。80 字超なら 80 字より前の最後の `、` `。` `）` `」` か空白で切り、境界が無ければ `None`（変更せず `needs_human`）。収まらない節に当たったらそこで終わり（飛ばして後ろを足さない）。
6. **言語別の名前**: 残った節を、かな・漢字を含む = `ja`、ラテン文字だけ = `en`（数字・記号だけの節はどちらでもない）に分け、**`ja` が 1 つ・`en` が 1 つ**で、しかも両方が**短い名前**のときだけ（`ja` は 30 字以内・`、。！？「」『』〜…` `なら` `を` を含まず・ラテン文字を内包しない。`en` は 50 字以内で文でない。「ブランド＋キャッチコピー」は翻訳対ではないので割らない） `name_ja` = ja の節をつないだもの・`name_en` = en の節をつないだもの を入れる（`name` は 5 の結果なのでどの画面経路でも表示できる）。片方だけなら `name` だけ（`name_ja` / `name_en` に写さない）。**既に値がある `name_ja` / `name_en` は上書きしない**（UPDATE 文の中で `CASE WHEN COALESCE(name_ja,'')=''` と保護）。

リテラルに従った結果が見映えしない例（`朝倉真弓公式サイト｜…` `not found HGU – …` `Jeep Official Dealer Site`）も、推測で直さずそのまま出す。直したい語は定型の一覧（3）に足す。

**検索（`search_text` / FTS）**: `search_text` は migration 0030 から `name` の直後に `name_ja` / `name_en` を含み、`sites_search_text_au` は `UPDATE OF name, name_ja, name_en, …` で発火する（0018/0020/0027/0030、`tests/name-per-language.test.mjs`）。ツールは `name` `name_ja` `name_en` を **1 つの UPDATE** で書くので `search_text` と FTS が追従する（`search_text` を直接書かない。追加の migration は不要。`tests/test_cleanup_quality.py::test_search_text_and_fts_follow_name_ja_en`）。旧名（宣伝文・副題）は新しい名前に残る節があれば検索に当たる。**書いた後は `node scripts/refresh-search-text.mjs --remote`** で暫定値を正規化版（NFKC）に戻す。

**選ぶ行**:
- `names`: 今の `name` が `check_name` に違反し（かつ区切りの無い 80 字以内の 1 節ではなく）、規則の結果が今と違う（または結果なし）行。
- `names --recompute`: **stored name が title より短い行はすべて再計算の対象になる（説明的な語を残す方針と一致）。dry-run で差分を確認してから `--apply`**。上に加えて、(b) 以前の実行で書き換えた行（`url_candidates.title` があり、`name` が title の一部（真部分）、または規則の結果と違って title より短い）。規則の結果が今と同じ行（`name_ja` / `name_en` の補完も無い）は触らない。
- 結果が出ない行（定型だけ・境界の無い 80 字超）は**変更せず** `needs_human`（件数と id を出し、D1 に印を付ける。`--retry-skipped` で再び対象）。ホスト名そのもの（`100s.jp`）になる結果も名前にしない（`needs_human`）。成人向け（`adult_content=1`）は掲載段（`publish_stage.site_name`）と同じくホスト名。

**dry-run（既定）**: 何も書かず LLM も呼ばない。`names --recompute` は選ばれた**全行**の `(id, 旧, 新, name_ja, name_en)` を出す。`--apply` は D1 リース・bookmark（取れなければ中止）・保存値とのガード・読み戻し（`name` `name_ja` `name_en`）・20 件ごとの書き込み。旧名は `url_candidates.title` に残す（title があれば上書きしない・無ければ旧名で作る）。

**`--llm-fallback`（既定オフ）**: 規則で決まらず `needs_human` になる行**だけ**を、(1) 判定段が支払い済みの `url_candidates.site_name`、(2) LLM（Haiku。`site_name` 40 字以内、title・今の名前・本文の先頭 600 字の**それぞれ単独**に境界つきの完全な節として現れること。述語・呼びかけの形・区切り文字入り・ホスト名・定型の節・所在地や商品説明の節は不可。`llm_name_valid`）の順で試す。付けなければ LLM は一切呼ばない。この経路でも `name_ja` / `name_en` は書かない。

### `descriptions`

対象: `description_ja` が `check_description_style` に違反する（常体）行。`to_polite`（決定的、確実なものだけ）が返せればそれを使い、残りは**10 件まで 1 回の呼び出し**で LLM に書き換えさせる（JSON の入出力）。**自動で行うのは「です・ます」への語尾変換のみ。体言止めへの書き換えは行わず（`stem` の検証で却下される安全側の扱い）、変換できないものは `needs_human`。** **変えてよいのは各文の文末の語尾だけ**（常体→「です・ます」の対応表 `ENDINGS` に載るもの）。検証（規則変換・LLM とも同じ `faithful`）: 文の数が同じ・各文の語幹（語尾を除いた部分）が正規化後に完全一致・否定の有無が文ごとに前後で同じ・数字／ラテン文字の語／「」の中の語が同じ（増減なし）・`check_description_style` を通る・長さが元の 0.6〜1.5 倍。落ちた行は変更せず `needs_human`（無料→有料、否定の反転、語の追加などは落ちる）。規則変換は元の空白を保ち、ひらがなだけの節で終わる文（「また、あした。」）は動詞か名詞か決められないので変換しない（LLM へ回り、検証に落ちれば `needs_human`）。`description_en` は触らない（英語の説明は常体／敬体の区別が無く、`check_description_style` も日本語でないと `[]` を返すので、検出できない）。`description_ja` の更新でも `search_text` はトリガが作り直す（上と同じ）。

テスト: `tests/test_cleanup_quality.py`（実 migration の in-memory SQLite + 偽の LLM。dry-run は何も書かない・LLM を呼ばない、apply の更新と読み戻し、冪等、リース、予算停止、`needs_human`、検索・FTS の整合、束縛 100 以下、ファイルを作らない）。

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
uv run python scripts/enrich_online.py --backfill-empty [--apply]        # 判定済みの空ページを empty_content で却下（1 回で終了。既定 dry-run）
```

主な引数: `--stages`（既定は全段）、`--limit`（全段共通）/`--fetch-limit`/`--judge-limit`/`--deliberate-limit`/`--requalify-limit`/`--publish-limit`（既定 60 / 30 / 20 / 20 / 60。上限 200、超える指定は引数エラー。リースの有効時間に収めるため）、`--rounds`（0 = 残り無しまで）、`--max-seconds`（超えたら次の回に入らない）、`--budget-month`、`--max-publish-per-day`（日次の掲載上限。既定 5000、1 以上。0 以下は引数エラー）、`--judge-concurrency`（1〜4）、`--priority leaf|none`（既定 leaf。「優先順」）、`--dry-run` / `--apply`。`ANTHROPIC_API_KEY` と `CLOUDFLARE_API_TOKEN` は環境変数のみ。

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
