# 使用量ガード（料金の暴走を防ぐ。#157）

Cloudflare には**金額で止まる上限の機能が無い**。Workers Paid（月 $5）の込みの枠を超えると、Workers も D1 も上限なしで従量課金される（Notifications の Usage Based Billing の対象は Argo・Load Balancing など。Workers/D1 は対象外）。請求書は月額 $5.50 固定（従量超過なし）で、これを崩す暴走（攻撃・バグ）に**早く気づく**ことと、**1 リクエストのコストの上限を設計で抑える**ことが目的。検知は最大 1 日遅れ。

| 層 | 何をするか | 正本 |
|---|---|---|
| 1. 使用量ガード（検知） | Cloudflare の使用量（アカウント全体）を読み、込みの枠に対する割合・月末予測・急増を警告する。夜間ジョブの 1 段 | このページ・`scripts/usage_guard.py` |
| 2. 1 リクエストの読み取りの上限（設計） | リクエストごとの D1 読み取り行数を観測（閾値超えだけログ。診断ヘッダはローカルのみ）、重い GET を短くキャッシュ | [architecture.md](../design/architecture.md#1-リクエストの読み取り行数の観測とキャッシュ157) |
| 3. WAF のレート制限（防御） | 同一 IP の連打を Cloudflare の手前で止める。**当面は設定しない（推奨）**。判断は kako-jun | 下の「WAF のレート制限」 |
| 4. 最終手段 | 無料プランに戻す（上限で停止し、請求は 0 円。サイトは止まる） | 下の「対処」 |

## 込みの枠と超過料金

**この枠は Cloudflare アカウント全体のもの**で、mise だけの枠ではない。同じアカウントの全 Worker と Pages Functions・全 D1 データベースの合計に対して、月 1,000 万リクエスト・250 億行・5,000 万行が込みになる（このアカウントには mise のほか 20 以上のアプリが載っている）。だから使用量ガードは**アカウント合計**で割合・月末予測・急増を判定し、mise の Worker と mise の D1 は**内数**（表の「うち mise」の行）として表示するだけにする（判定には使わない。mise が小さくても、他のアプリの暴走で枠を超えれば請求が増えるため）。

出典（2026-11-19 に確認）: [D1 の料金](https://developers.cloudflare.com/d1/platform/pricing/)・[Workers の料金](https://developers.cloudflare.com/workers/platform/pricing/)。数値は `scripts/usage_guard.py` の `METRICS`（1 か所）。

| 項目 | 込みの枠（月） | 超過 |
|---|---|---|
| Workers リクエスト | 1,000 万 | $0.30 / 百万リクエスト |
| D1 読み取り行 | 250 億行 | $0.001 / 百万行 |
| D1 書き込み行 | 5,000 万行 | $1 / 百万行 |

月は UTC の暦月。D1 の行数は `meta.rows_read` / `rows_written` と同じ単位（インデックス走査の行も数える）。

## 準備（kako-jun のダッシュボード操作。1 回だけ）

トークンは**読み取り専用**で作る。作るまでガードは動かない（夜間ジョブは「未設定」の警告を出して続行する）。

1. Cloudflare ダッシュボード → 右上のプロフィール → **My Profile** → **API Tokens** → **Create Token**。
2. 「Create Custom Token」の **Get started**。
3. トークン名: `mise-usage-guard`。**Permissions**: `Account` / `Account Analytics` / `Read` の 1 行だけ（他は足さない）。
4. **Account Resources**: Include → Specific account → mise のアカウントだけ。
5. **TTL**（有効期限）を設定する（例: 1 年。切れたらこの手順でローテーションし、`~/.config/mise/cloudflare-analytics.env` を書き換える）。IP の制限は夜間ジョブの実行機に合わせて任意。
6. **Continue to summary** → **Create Token**。表示されたトークンをコピーする（この画面でしか見られない）。
7. アカウント ID はダッシュボードの右側（Account home の「⋯」→ Copy account ID）。

実行機（夜間ジョブを動かす機械）に次のファイルを作る（リポジトリに入れない。権限は `chmod 600`）:

```
# ~/.config/mise/cloudflare-analytics.env
CLOUDFLARE_ANALYTICS_TOKEN=（コピーしたトークン）
CLOUDFLARE_ACCOUNT_ID=（アカウント ID）
```

値は**どこにも表示・記録されない**（表・JSON・report・ログ・エラー文に出さない。テストで固定）。

## 使い方

```
uv run python3 scripts/usage_guard.py            # 表を出し、var/usage/<UTC の日付>.json に書く。常に exit 0
uv run python3 scripts/usage_guard.py --env-file PATH --var-dir DIR --date YYYY-MM-DD
```

夜間ジョブでは最後の段 `usage`（[nightly.md](nightly.md)）として**既定で動く**。`--no-usage-guard` で止める、`--usage-env PATH` で env ファイルの場所を変える。トークンが無い・API が 429/5xx/不正な応答のときも**失敗にせず**、警告を `guards`（report.md の「ガードの発動」）に載せて続行する。他の段が失敗した夜も動く。

**初回は実際に流して確認する**: クエリは実 API で未実行（トークンが無かったため）。公式ドキュメントとの突き合わせ（2026-11-19）:
- **Pages Functions**: Pages Functions のリクエストは **Workers のリクエストとして課金され、Workers Paid の枠に数えられる**（[公式の Pages Functions の料金](https://developers.cloudflare.com/pages/functions/pricing/):「Requests to your Pages functions count towards your quota for Workers Paid plans」）。このアカウントの `*.llll-ll.com` の多くは Pages なので、Workers のクエリとは**別のクエリ**（`pagesFunctionsInvocationsAdaptiveGroups`）で日別に取り、Workers のリクエスト数に足して判定する。表と JSON には「内訳: Workers / Pages Functions」を出す。**未確認**: データセット名は公式の [GraphQL datasets 一覧](https://developers.cloudflare.com/data-localization/metadata-boundary/graphql-datasets/)に載っているが、フィールド（`sum { requests }`）は公式ドキュメントで確認できなかった（Workers の例に合わせた推定）。取れない・名前が違うときは「Pages Functions は取得できず合計に含まれていません」と警告だけ出して Workers 分で判定を続ける（終了コードは 0）。**初回の突き合わせ手順**: 実行後、ダッシュボードの Workers & Pages の Overview（アカウント合計のリクエスト数）と表の「Workers リクエスト」の当月累計を同じ期間で比べる。表が小さければ Pages の取得が失敗している（警告が出ていなくても、フィールドが違って 0 になっていないか確認する）。
- Workers: [公式の例](https://developers.cloudflare.com/analytics/graphql-api/tutorials/querying-workers-metrics/)は `workersInvocationsAdaptive` を `datetime_geq` / `datetime_leq` で絞り、次元は `datetime`・`scriptName`・`status`。日の範囲は `datetime_geq` が当日 00:00:00Z、`datetime_lt` が翌日 00:00:00Z（`leq` の 23:59:59Z だと最後の 1 秒が抜ける。`lt` は GraphQL の標準の比較演算子だが、公式の例は `leq` なので初回に確認）。**`date` 次元は例に無い**ので使わず、**日ごとに別名（`w20261119` など）で datetime 範囲を 1 日に絞り、`scriptName` 次元で合計を取る**形にした（日付は別名から UTC の日に決まる）。アカウント合計 = 全 `scriptName` の和、内数 = `mise`。変数の型は公式の例に合わせた（`$acct: string!`・`Date`）。
- D1: [公式の例](https://developers.cloudflare.com/d1/observability/metrics-analytics/)は `d1AnalyticsAdaptiveGroups` を `date_geq` / `date_leq` で絞り、次元 `date`・`databaseId`、合計 `sum { readQueries writeQueries }`。`rowsRead` / `rowsWritten` は同ページの指標の表にあるが、**どのノード（`sum` か）に入るかは例に無く未確認**。`sum { rowsRead rowsWritten }` で取っている。
初回の実行で「応答の形が想定外」「GraphQL エラー」が出たら、メッセージに従って `build_query` を直す。

## 表の読み方

```
項目               当月累計   枠   割合   月末予測   昨日   7日平均   倍率   判定
```

- **割合**: 当月（UTC）の**アカウント合計**の累計 ÷ 込みの枠。表の「うち mise」の行は内数（mise だけの累計と、枠に対する割合）で、判定には使わない。今日の途中経過を含む。**50% で警告、80% で強い警告**。
- **月末予測**: 今月の**完了した日**（今日を除く）の累計 ÷ 日数 × 月の日数。1 日が終わるまでは予測しない。100% 以上で警告。
- **昨日 / 7日平均 / 倍率**: 昨日の値と、その前 7 日の平均。**3 倍以上**で「急増」の警告（小さい数どうしの比は無視する下限あり: Workers 1 万・D1 読み取り 1,000 万行・D1 書き込み 5,000 行）。
- JSON（`var/usage/<日付>.json`）は同じ内容（`scope` にアカウント合計であること、`mise_month_total` / `mise_yesterday` に内数）。mise-ops が読む report の形は変えていない（`guards` に文字列が増えるだけ）。

## 警告が出たときの対処

1. **原因の切り分け**: 急増した項目と日を見る。**まず mise の内数を見る**。mise が小さいなら原因は同じアカウントの別のアプリ（ダッシュボードの Workers & Pages / D1 で Worker・データベース別の使用量を見る）。
   - Workers リクエストだけ急増 → 攻撃（連打・クローラ）か、フロントのバグ（無限ループでの API 呼び出し）。
   - D1 読み取り行だけ急増 → 重いクエリ（1〜2 文字の検索・深いページ番号）の連打。ログの `d1-rows-read-high`（`wrangler tail` またはダッシュボードの Logs。検索語は載せない）でパスを確認する。ローカルでは `.dev.vars` に `DEBUG_ROWS=1` を書くと応答ヘッダ `X-D1-Rows-Read` で 1 リクエストのコストが分かる（本番では出さない: コストの指標を攻撃者に渡さないため）。
   - D1 書き込み行が急増 → 夜間ジョブの大量投入、または `/api/requests` への連打（推薦フォーム）。
2. **攻撃**: 下の WAF のレート制限を入れる／厳しくする。特定の IP・国・User-Agent なら WAF のカスタムルールでブロックする。
3. **バグ**: 直前のデプロイを疑い、ロールバックする（[deploy.md](deploy.md)）。
4. **最終手段**: 無料プランに戻す。枠を超えると止まり、請求は 0 円になる（サイトは止まる）。Free の枠は Workers 1 日 10 万リクエスト・D1 読み取り 1 日 500 万行・書き込み 1 日 10 万行（[出典](https://developers.cloudflare.com/d1/platform/pricing/)）。

## WAF のレート制限（当面は設定しない。判断は kako-jun）

無料プランは**ルール 1 つ・カウント窓 10 秒・ブロック 10 秒・カウント単位は IP のみ**で、**ルール式に使える項目は「Path」と「Verified Bot」だけ**（[公式の Rate limiting rules](https://developers.cloudflare.com/waf/rate-limiting-rules/)。2026-11-19 に再確認。ホストを使えるのは Pro 以上）。つまり `http.host eq "mise.llll-ll.com"` のようにホストで絞ることは**できない**。Path だけのルール（`/api/` で始まる）は、**ゾーン `llll-ll.com` の全サブドメインの `/api/` に効く**。他のサービス（同じゾーンの別アプリ）の `/api/` も、同じ IP が 10 秒に N 回を超えるとブロックされる副作用がある。

設定する場合の手順（ダッシュボード → `llll-ll.com` → **Security** → **WAF** → **Rate limiting rules** → **Create rule**）: 式 `starts_with(http.request.uri.path, "/api/")`、同一 IP が 10 秒に N 回を超えたら 10 秒ブロック（Block）、Deploy。

| 案 | 利点 | 欠点 |
|---|---|---|
| (a) 副作用を受け入れて高めの N（例 100）で設定する | 同一 IP の機械的な連打を Cloudflare の手前で止める。Workers リクエストの課金を抑えられる可能性（下記、未確認） | 無料の唯一のルールを使い切る。同じゾーンの他のサービスにも効く（共有 NAT の裏の複数人、正常な連続操作でも誤ブロックしうる）。N の根拠となる実測が無い（画面の通常操作は 10 秒に 10〜15 本程度の見込みだけ）。IP を変える攻撃には効かない |
| (b) 設定せず、使用量ガード＋共有キャッシュで守る | 他のサービスに副作用が無い。ルールの枠を温存できる | 連打は Worker まで届く（同じ URL はキャッシュで D1 を読まないが、Workers リクエストは数える）。検知は最大 1 日遅れ |

**推奨は (b)（当面設定しない）**。理由: ホストで絞れず、他のアプリに副作用が出る一方、D1 の読み取りは共有キャッシュで繰り返しから守られ、アカウント合計の使用量は夜間ジョブが毎日見る。実際に暴走を観測したら (a) を入れる。**最終的な判断は kako-jun**。

**ブロックされたリクエストが Workers の課金に数えられるか**: WAF は Cloudflare のエッジで Worker より前に評価されるが、**ブロックされたリクエストが Worker に届かず Workers リクエストに数えられないことは、公式ドキュメントで確認できていない（未確認）**。Rate limiting rules のページにも Workers の料金ページにも記載が無い。(a) を入れる場合は、Security → Events の Block 数と Workers のリクエスト数（Workers & Pages → mise → Metrics）を同じ時間帯で比べて確かめる。

## 1 リクエストのコストが大きい経路（kako-jun の判断が要る案）

実測（10 万件・本番の形）で、キャッシュが効かない最初の 1 回が重い経路がある（[architecture.md](../design/architecture.md#1-リクエストの読み取り行数の観測とキャッシュ157)の表）。結果が変わる対策は実装していない。案:

| 案 | 効果 | 結果への影響 |
|---|---|---|
| 1〜2 文字の検索語を拒否（400）または 3 文字以上を必須にする | 最悪 約 10 万行 → 0 | 1〜2 文字で検索できなくなる |
| 1〜2 文字の検索を結果の打ち切り（例: 先頭 N 行を走査して止める） | 最悪を N 行に | 一部の語で該当が出ない |
| `page` の上限を下げる（現在 999,999。深いページで約 10 万行） | 深いページの最悪 約 10 万行 → 上限 | 深いページに行けなくなる（SEO のページ番号リンクにも影響） |
| 同一 IP の検索に WAF のレート制限を別途かける（無料はルール 1 つで Path しか使えない。検索だけの絞り込みは Pro 以上の式が要る） | 連打の抑止 | 正当な連打も止まる |
| タグ 2 個 AND の計画を見直す（実測 約 49 万行/回） | 最悪を数千行へ | 結果は変えずにできる可能性あり（別 Issue で設計） |
