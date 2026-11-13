# 登録済みサイトの再巡回

Issue #2 の第1段。**手元実行のスクリプト**（Worker の cron にはしない・LLM/API は使わない・本番 D1 には直接書かない）。実装は `scripts/recrawl/recrawl.py`、テストは `tests/test_recrawl.py`（`uv run python3 -m unittest tests.test_recrawl`）。状態機械の全体構想（down/long_down/suspect/合議）は Issue #53。

## 使い方

```sh
uv run python3 scripts/recrawl/recrawl.py import                 # data/collection-*.json の掲載サイトを台帳 var/recrawl.sqlite へ
uv run python3 scripts/recrawl/recrawl.py run --limit 50         # 期限が来たものを優先度順に巡回(実サイトにアクセスする)
uv run python3 scripts/recrawl/recrawl.py candidates             # 要確認・非表示候補(人が見る一覧。--json 可)
uv run python3 scripts/recrawl/recrawl.py changes                # タイトル・本文ハッシュの変化の記録
uv run python3 scripts/recrawl/recrawl.py sql --out var/site_checks.sql   # site_checks 向けの SQL(適用は人が行う)
```

## 取得作法

発見パイプラインの `scripts/pipeline/fetcher.py` を再利用（UA は `MiseResearchBot`、robots.txt 遵守、内部 IP 拒否、本文 512KB・20 秒上限）。違いは次の 3 点。
- 登録 URL のトップだけを取る。同一登録ドメイン内のリダイレクトのみ追従し、**別ドメインへの転送は追従せず `redirect_offsite`**。
- 同一ホストへのリクエスト間隔は既定 5 秒（`--host-wait`）。同一ホストのサイトは直列、全体の並列は `--workers`（既定 3）。
- robots.txt で拒否されたページは取得しない（`robots_disallowed`）。
- robots.txt が取れない（5xx・タイムアウト・接続失敗）ときは、サイトの死活を見るため登録 URL を**1 回だけ**取得して HTTP 結果で判定する（リダイレクトは追わない）。全パスが 503 の落ちたサーバーは `http_5xx`（失敗）になる。robots.txt（または本体）が**別ドメインへ転送**されたら `redirect_offsite`。
- 「同一ドメイン」は登録ドメインで比べるが、共有ホスティング（github.io・blogspot.com・netlify.app・pages.dev・sakura.ne.jp など。`blocklist/platforms.txt` と `recrawl.py` の `EXTRA_SHARED`）配下は完全なホスト名一致で比べる。
- 実行の最初に健全なホスト（`HEALTH_URLS`）へ到達できるか確かめ、どれにも届かなければ実行機のネットワーク障害とみなして**何も記録せず**警告して終了する（終了コード 2）。

## 結果の種別と状態遷移

| 種別 | 扱い |
|---|---|
| `ok`（2xx） | 成功。連続失敗を 0 に戻す |
| `http_404` `http_410` `http_4xx` `http_5xx` `dns` `timeout` `connect` `tls` `too_many_redirects` | 失敗（連続失敗 +1） |
| `robots_disallowed` `robots_unavailable`（robots 未確認でリダイレクトされた場合）`http_denied`（401/403/429）`error` | 中立。サイトの生死が分からないので失敗に数えず、状態も据え置き |
| `redirect_offsite` `ssrf_blocked`（内部アドレスへ解決） | 即 `needs_review`（移転・売却・乗っ取りの疑い）。flag が付き、**人の確認まで維持**する |

状態は `listed` → `needs_review`（連続失敗 3 回以上）→ `hide_candidate`（連続失敗 6 回以上、かつ最初の失敗から 14 日以上）。**自動削除はしない**。`candidates` が人の確認用の一覧で、非表示・削除の判断は人が行う。flag 付きの `needs_review` は、その後に失敗や中立の結果が来ても `listed` に戻らず注記も消えない。戻るのは同一ドメインで `ok` になったときだけ（その旨を `note` に残す。本番側での復帰は人が確認）。閾値は `recrawl.py` 冒頭の定数。

## 優先度と間隔

期限（`next_check_at`）が来たものを「失敗中（連続失敗が多い順）→ 最終確認が古い順（未確認が先頭）」で処理する。次回確認は、成功なら 30 日後、失敗中は 1・2・4・7 日（上限 7 日）、中立は 7 日、別ドメイン転送は 3 日後。

## 変更検出

成功時にタイトルと本文テキスト（script/style/タグを除いた先頭 2 万字）のハッシュを前回と比べ、変化を `changes` に記録する。**記録のみ**で、紹介文などの掲載内容は書き換えない。

## 出力と migration

台帳は `var/recrawl.sqlite`（`.gitignore`、再生成可能）。`sql` が `migrations/0017_recrawl.sql` の `site_checks` 向け `INSERT OR REPLACE` を出す。表示側（フィード・一覧からの除外）はまだ `site_checks` を読まない。本番適用は人が行う。

## 未実装（#2 の残り）

- `site_checks` を読んだ表示側の除外・復帰、監理ポスト（審議コーナー）の画面（#53）。
- パーキング・売却ページ検出、言語・主題の大きな変化による乗っ取り判定（現状はタイトル/ハッシュの記録とドメイン転送のみ）。
- 本番 D1 の掲載サイト一覧からの取り込み（現状は `data/collection-*.json`）。
- 新規サイトの継続発見（[pipeline.md](pipeline.md) 側）、cron 登録と実行上限の運用、実ネットワークでの長期運用検証。
