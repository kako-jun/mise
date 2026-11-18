# 夜間ジョブ（運用の正本）

発見から掲載候補の SQL 作りまでを毎夜自動で回す仕組み（Issue #124）。実装は `scripts/nightly.py`、状態と成果物は `var/nightly/YYYY-MM-DD/`。`mise-ops` スキルはこの文書の手順をそのまま使う。

方針（決定の全文は Issue #124）: 夜間ジョブは**決定論のスクリプト + cron**。LLM は判定の 1 工程（`scripts/llm/pipeline.py`、Anthropic API のバッチ）だけで、Claude Code の対話トークンは使わない。月の LLM 予算は $100。段階化は 1,000 → 1 万 → 10 万。**最初は本番に触れない運用で数夜回し、人が確認してから `--apply-prod` に切り替える。**

## 構成（段）

```
1 discover    discover-links（常に）+ discover-cc（1 夜の上限つき。失敗しても続行）
2 triage      取得前フィルタ
3 fetch       取得（上限つき）
4 judge       取得後フィルタ -> candidate
5 candidates  今夜の判定対象 = 未判定の candidate だけを candidates.jsonl に書く
6 llm         pipeline.py（第1段 haiku・第2段 sonnet、既定 batch）。予算ガードはここ
7 import      import_results.py で結果を台帳へ
8 approve     approve_policy.py --apply（自動承認 / 保留 / 古い個人サイトの却下）
9 sql         承認済みの未出荷を key 順に --max-add 件まで -> approved-sites.sql（0 件なら作らない）
10 gate       生成 SQL をメモリ DB に流す整合チェック（integrity.gate）。違反なら停止
11 prod       --apply-prod のときだけ: bookmark 取得 -> 本番投入 -> 整合チェック(--remote)
```

- 各段は冪等。状態の正本は frontier 台帳（`var/frontier.sqlite`）と `var/nightly/<date>/`。`<date>` は UTC の日付。
- **同じ `--date` で再実行すると、成功済みの段は飛ばして失敗した段から続ける**。LLM バッチの未精算も同コマンドの再実行で再開される（`var/llm-batches/`。別の日付で再開しない）。
- 失敗した段があればそれ以降は進めず、`status.json` に記録して exit 1。多重起動は `var/nightly/.lock` の flock で防ぎ、取れなければ exit 75。
- 本番 D1 に触れるコードは prod 段の 1 か所だけで、`--apply-prod` が無ければ `wrangler` は一切呼ばれない（テストで固定）。`--dry-run` と `--apply-prod` は併用できない。
- 「出荷済み」は `var/nightly/shipped.txt`（本番反映に成功した key）。承認済みで未出荷のものを 1 夜 `--max-add` 件まで出す。本物の台帳は変えず、`export.sqlite`（台帳のコピー）で上限をかけて書き出す。

## コマンド

```sh
uv run python3 scripts/nightly.py --dry-run                 # 計画と見積りだけ(何も書かない)
uv run python3 scripts/nightly.py                           # SQL と報告を作る(本番には触れない)
uv run python3 scripts/nightly.py --apply-prod              # 本番反映まで
uv run python3 scripts/nightly.py --date 2026-11-18         # 失敗した夜の再開
```

主なオプション: `--max-add 300`（1 夜の追加上限）、`--discover-limit 200`（0 で discover-cc 無効）、`--fetch-limit 600`、`--judge-limit 600`、`--judge-mode batch|sync`、`--budget-month 100`、`--db`、`--var-dir`、`--ledger`。API キーは環境変数 `ANTHROPIC_API_KEY` のみ（[jobs.md](jobs.md#鍵クレジットllm-予算)。値は出力にも status.json にも出ない）。

## cron の登録（kako-jun が行う）

手元 PC か Raspberry Pi の、リポジトリを置いたユーザーの crontab に登録する。**登録は kako-jun が行う。エージェントは登録しない。** 鍵は cron から読めるようにラッパーで読み込む。

```cron
# 毎日 03:10 UTC。最初は --apply-prod を付けない
10 3 * * *  cd /path/to/mise && set -a && . ~/.config/mise/anthropic.env && set +a && flock -n var/nightly/.lock uv run python3 scripts/nightly.py >> var/nightly/cron.log 2>&1
```

- 初回は `mkdir -p var/nightly` してから登録する。`flock -n` と nightly.py 内の flock は同じファイルで、二重起動は後から来た方が即終了する。
- **最初の数夜（目安 3〜7 夜）は `--apply-prod` なしで回し、朝の確認手順で人が確認する。** 問題が無ければ crontab に `--apply-prod` を足す。
- 段階化: 最初は `--fetch-limit 100 --max-add 50`（1,000 件規模）、問題が無ければ 1 万、10 万と上限を上げる。

## 朝の確認手順

1. `cat var/nightly/$(date -u +%F)/report.md`。先頭の「結果」が成功か、「費用」が想定内かを見る。
2. `status.json` を見る。`ok` が false なら `stages` の `status: failed` の段と `note` を読む。`guards` に何か出ていれば理由を読む。`next_action` に次の一手が出る。
3. `added`（追加数）が直近の夜と桁違いでないか（`guards` の「前日比の異常」が出ていれば本番反映は止まっている）。
4. 保留のサンプル確認: `uv run python3 scripts/llm/review_state.py list --state pending --limit 20`。保留の理由が妥当か、本来通すべきものが落ちていないかを見る。
5. 却下のサンプル確認: `report.md` の「却下理由別」で多い理由を選び、台帳から数件を見る（`sqlite3 var/frontier.sqlite "SELECT key,title FROM frontier WHERE state='rejected' AND reject_reason='<理由>' ORDER BY random() LIMIT 10"`）。
6. 出荷 SQL の確認: `var/nightly/<date>/approved-sites.sql` の件数（`keys.txt` の行数）と先頭数件のサイトを実際に開いて見る。
7. 問題があれば、該当する key を `review_state.py` で却下してから再実行する（`--date` を変えず再実行すれば SQL が作り直される。成功済みの段は飛ばされるので、`sql` 以降を作り直したいときは `var/nightly/<date>/status.json` を消す）。

## 本番反映の前後確認（--apply-prod）

prod 段は次の順に動く。どれかが失敗した時点で止まる。

1. `wrangler d1 time-travel info mise --json` で bookmark を取り、`bookmark.txt` に保存して `status.json` の `bookmark` に記録（取れなければ反映しない）。
2. `wrangler d1 execute mise --remote --yes --file approved-sites.sql`（ログは `prod-execute.log`）。SQL は冪等なので、途中で失敗しても再実行で増えない。
3. `scripts/check-data-integrity.py --remote`（ログは `prod-integrity.log`）。error があれば exit 1。

人が見るもの: 反映前後で `SELECT count(*) FROM sites` の差が `added` と一致するか、サイトの一覧に新規が出るか（`--remote` は SELECT のみで）。

## ロールバック

1. `status.json` の `bookmark` を控える（`bookmark.txt` に原文）。
2. 本番の整合チェックが error のとき、**そのまま再実行しない**。原因を見る（`prod-integrity.log`）。
3. 戻す場合のみ: `npx wrangler d1 time-travel restore mise --bookmark <bookmark>`。復元は反映後の全書き込み（申請など）も巻き戻す。実行は kako-jun が判断する。
4. 復元したら、その夜の出荷済み key を `var/nightly/shipped.txt` から削除する（`keys.txt` の key）。削除しないと翌夜以降も出荷されない。

## トラブルシュート

| 症状 | 見る場所 / 対処 |
|---|---|
| exit 75 | 別の夜間ジョブが動いている。`ps` で確認。残骸なら待つ（flock はプロセス終了で自動解除） |
| llm 段が `ANTHROPIC_API_KEY が無い` | cron のラッパーで `anthropic.env` を読み込めていない |
| llm 段が skipped「予算ガード」 | 当月の台帳合計 + 最悪費用が `--budget-month` を超える。台帳は `~/.config/mise/llm-ledger.jsonl`。月が変われば自動で再開 |
| llm 段が「未解決あり」 | 一部が不正出力。同じ `--date` で再実行すると続きから（キャッシュ済みは課金されない） |
| 開いたままの予約（台帳） | `scripts/llm/ledger_release.py --list` で確認してから解放 |
| sql 段 / gate 段が整合チェック違反 | `report.md` の違反行を見る。ほとんどは台帳側の判定データ。該当 key を却下して再実行 |
| discover-cc が失敗 | 続行される（`guards` に記録）。ネットワークか Common Crawl 側 |
| fetch が遅い | `--fetch-limit` を下げる。cron の間隔を空ける |
| 追加が 0 件 | 承認ポリシーで保留が多い（朝の手順 4）。判定対象が尽きているなら `--discover-limit` を上げる |

## 未対応（v1）

- 葉（カテゴリ）の目標未達を優先した選抜（各葉 100 件の最低ライン）。今は key 順。
- Wikidata からの発見（#128）、ファビコン取得（#125）。
- 再巡回（[recrawl](../collection/recrawl.md)）の夜間組み込み。
- 構想の全体は [ideas/llm-judging.md](../ideas/llm-judging.md) と Issue #124。
