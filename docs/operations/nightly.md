# 夜間ジョブ（運用の正本）

発見から掲載候補の SQL 作りまでを毎夜自動で回す仕組み（Issue #124）。実装は `scripts/nightly.py`、状態と成果物は `var/nightly/YYYY-MM-DD/`。`mise-ops` スキルはこの文書の手順をそのまま使う。

方針（決定の全文は Issue #124）: 夜間ジョブは**決定論のスクリプト + cron**。LLM は判定の 1 工程（`scripts/llm/pipeline.py`、Anthropic API のバッチ）だけで、Claude Code の対話トークンは使わない。月の LLM 予算は $100。段階化は 1,000 → 1 万 → 10 万。**最初は本番に触れない運用で数夜回し、人が確認してから `--apply-prod` に切り替える。**

## 構成（段）

```
1 discover    discover-links（常に）+ discover-cc（1 夜の上限つき。失敗しても続行）+ discover-wikidata（1 夜 N 葉。失敗しても続行）
2 triage      取得前フィルタ
3 fetch       取得（上限つき）
4 judge       取得後フィルタ -> candidate
5 candidates  今夜の判定対象 = 未判定の candidate だけを candidates.jsonl に書く
6 llm         pipeline.py（第1段 haiku・第2段 sonnet、既定 batch）。予算ガードはここ
7 import      import_results.py で結果を台帳へ
8 approve     approve_policy.py --apply（自動承認 / 保留 / 古い個人サイトの却下）
9 sql         承認済みの未出荷をすべて（`--max-add N` を付けたときだけ key 順に N 件まで。既定は上限なし） -> approved-sites.sql（0 件なら作らない）
10 gate       生成 SQL をメモリ DB に流す整合チェック（integrity.gate）。違反なら停止
11 prod       --apply-prod のときだけ: bookmark 取得 -> 本番投入 -> 整合チェック(--remote)
12 favicon    --favicon-limit N のときだけ(既定は無効): ファビコン取得(#125。`uv run --with pillow` で起動。Pillow が無ければ失敗として guards に出る) -> favicons.sql。本番には触れず、失敗しても夜間全体は失敗にしない
```

- **discover-wikidata（#128）**: 本番の `/api/categories`（読み取りのみ）で `site_count` が 100 未満の葉（対応表 `data/wikidata-leaf-map.csv` にある葉）のうち、対応表の行がまだ全部は `done` でない葉を少ない順に `--wikidata-leaves N`（既定 3、0 で無効）葉まで取る。不足の葉が無い・全葉が完了ならこの段はスキップし（guards に記録）、対応表の順（固定）になるのは本番 API が取れないときだけ。WDQS が 429/503 で中断すると `discover-wikidata` は終了コード 3 で終わり、guards に「Wikidata が中断した（Retry-After）」を残して夜間は続行する。行ごとの続きの OFFSET は台帳の `source_cursor` に残り、夜をまたいで進む。WDQS のルール（5 秒間隔・429/503 で中断・キャッシュ）は [pipeline.md](../collection/pipeline.md#発見元)。
- 各段は冪等。状態の正本は frontier 台帳（`var/frontier.sqlite`）と `var/nightly/<date>/`。`<date>` は UTC の日付。
- **同じ `--date` で再実行すると、成功済みの段は飛ばして失敗した段から続ける**。LLM バッチの未精算も再開される（`var/llm-batches/` の未精算の `pipeline-<段>-*` 状態ファイルは**日付に関係なく**拾われるので、翌夜の通常実行でも続きから進む。未精算バッチの候補は今夜の `candidates.jsonl` に上限の外でも必ず含める）。
- **LLM 段が未解決を残した夜（pipeline.py が exit 2）は段の状態が `partial`**（失敗ではない。exit 0）。同じ `--date` で再実行すると llm 段から続きを再開し、import 以降もやり直す。`partial` の夜は本番反映（prod 段）を止める。
- 途中の段からやり直したいときは `--from-stage <段>`（例: `--from-stage sql`）。`status.json` を消さずに、その段以降を成功済みでも実行し直す。
- 失敗した段があればそれ以降は進めず、`status.json` に記録して exit 1。多重起動は `var/nightly/.lock` の flock で防ぎ、取れなければ exit 75。
- 本番 D1 に触れるコードは prod 段の 1 か所だけで、`--apply-prod` が無ければ `wrangler` は一切呼ばれない（テストで固定）。`--dry-run` と `--apply-prod` は併用できない。
- 「出荷済み」は `var/nightly/shipped.txt`（本番反映に成功した key のうち、**`export_approved_sql.py` が実際に SQL へ出した key だけ**。カテゴリ無し・重複で外れた key は記録しない。`keys.txt` も同じ）。承認済みで未出荷のものを 1 夜 `--max-add` 件まで出す。本物の台帳は変えず、`export.sqlite`（台帳のコピー）で上限をかけて書き出す。

## コマンド

```sh
uv run python3 scripts/nightly.py --dry-run                 # 計画と見積りだけ(何も書かない)
uv run python3 scripts/nightly.py                           # SQL と報告を作る(本番には触れない)
uv run python3 scripts/nightly.py --apply-prod              # 本番反映まで
uv run python3 scripts/nightly.py --date 2026-11-18         # 失敗した夜の再開
```

主なオプション: `--from-stage <段>`（途中再開）、`--current-db <sqlite>`（gate 段の土台を本番再現 DB にする。無ければ空 DB）、`--max-links 300`（discover-links の 1 シードあたりの上限）、`--max-add 300`（1 夜の追加上限）、`--discover-limit 200`（0 で discover-cc 無効）、`--wikidata-leaves 3`（0 で Wikidata 無効）、`--fetch-limit 600`、`--favicon-limit 0`（ファビコン段。0 で無効が既定。[collection/favicons.md](../collection/favicons.md)）、`--judge-limit 600`、`--judge-mode batch|sync`、`--budget-month 100`、`--db`、`--var-dir`、`--ledger`。API キーは環境変数 `ANTHROPIC_API_KEY` のみ（[jobs.md](jobs.md#鍵クレジットllm-予算)。値は出力にも status.json にも出ない）。

## 実行機 kako-rog（Windows）と pi-3b の cron

実行機は **kako-rog**（Windows 11）。pi-3b の cron が ssh で起動する（tail-match と同じ型。[tail-match の cron 構成](https://github.com/kako-jun/tail-match/blob/main/scripts/cron/README.md)）。**ライブ版のラッパーは rog のリポ外** `C:\Users\kako-jun\mise-nightly.cmd`（`git reset --hard` が自分自身を書き換える事故を避ける）。`scripts/cron/mise-nightly.cmd` はその参照コピーで、**ASCII のみ**（SSH のコードページで .cmd の非 ASCII コメントが壊れる。tail-match #405）。変更したら両方を揃える。

- ラッパーは `git fetch` + `git reset --hard origin/main`（毎回最新）→ `PYTHONUTF8=1` → `C:\Users\kako-jun\.config\mise\anthropic.env` を環境変数へ → `uv run python scripts\nightly.py --fetch-limit 300 --judge-limit 300 %*`。ログは `C:\Users\kako-jun\mise-nightly.log` に追記。`--apply-prod` は付けない（人が確認してから）。
- リポは `C:\Users\kako-jun\repos\mise`。状態（`var/`）と LLM 台帳（`C:\Users\kako-jun\.config\mise\llm-ledger.jsonl`）は **rog が正本**。他の機で実行しない（予算と出荷済みが二重になる）。
- 本番反映（`--apply-prod`）に進む前に、rog の `npx wrangler whoami` が通ること（Cloudflare 認証）を確認する。
- ロックは `scripts/llm/filelock.py`（POSIX は `fcntl.flock`、Windows は `msvcrt.locking`）。`npx` は `shutil.which` で解決する。

pi-3b の crontab 例（登録は kako-jun が行う。`%` は crontab で特殊文字、Windows パスは絶対パス）:

```cron
37 0 * * * ssh -o ConnectTimeout=10 -o ServerAliveInterval=60 -o ServerAliveCountMax=10 -o StrictHostKeyChecking=accept-new kako-rog 'cmd /c "C:\Users\kako-jun\mise-nightly.cmd"' >> /tmp/mise-nightly.log 2>&1
```

手動の確認は `ssh kako-rog 'cmd /c "C:\Users\kako-jun\mise-nightly.cmd --dry-run"'`（`--dry-run` はラッパーの先頭の `git reset` は走るが、何も書かず API も呼ばない）。

## cron の登録（kako-jun が行う）（Linux の例）

手元 PC か Raspberry Pi の、リポジトリを置いたユーザーの crontab に登録する。**登録は kako-jun が行う。エージェントは登録しない。** 鍵は cron から読めるようにラッパーで読み込む。

```cron
# 毎日 03:10 UTC。最初は --apply-prod を付けない
10 3 * * *  cd /path/to/mise && set -a && . ~/.config/mise/anthropic.env && set +a && uv run python3 scripts/nightly.py >> var/nightly/cron.log 2>&1
```

- 初回は `mkdir -p var/nightly` してから登録する。二重起動の防止は nightly.py 内の flock（`var/nightly/.lock`、LOCK_NB）の**一本だけ**。**外側で `flock` を付けない**（外側が持つロックを nightly.py が別に取りにいき、毎夜 exit 75 になる）。二重起動は後から来た方が即終了（exit 75）する。
- **最初の数夜（目安 3〜7 夜）は `--apply-prod` なしで回し、朝の確認手順で人が確認する。** 問題が無ければ crontab に `--apply-prod` を足す。
- 段階化: 1 夜の量は `--fetch-limit` / `--judge-limit` と予算ガード・前日比の異常ガードで抑える（`--max-add` は既定で上限なし。最初は `--fetch-limit 100 --max-add 50`（1,000 件規模）にしてもよい）。問題が無ければ 1 万、10 万と `--fetch-limit` / `--judge-limit` を上げる。

## 朝の確認手順

1. `cat var/nightly/$(date -u +%F)/report.md`。先頭の「結果」が成功か、「費用」が想定内かを見る。
2. `status.json` を見る。`ok` が false なら `stages` の `status: failed` の段と `note` を読む。`guards` に何か出ていれば理由を読む。`next_action` に次の一手が出る。
3. `added`（追加数）が直近の夜と桁違いでないか（`guards` の「前日比の異常」が出ていれば本番反映は止まっている）。
4. 保留のサンプル確認: `uv run python3 scripts/llm/review_state.py list --state pending --limit 20`。保留の理由が妥当か、本来通すべきものが落ちていないかを見る。
5. 却下のサンプル確認: `report.md` の「却下理由別」で多い理由を選び、台帳から数件を見る（`sqlite3 var/frontier.sqlite "SELECT key,title FROM frontier WHERE state='rejected' AND reject_reason='<理由>' ORDER BY random() LIMIT 10"`）。
6. 出荷 SQL の確認: `var/nightly/<date>/approved-sites.sql` の件数（`keys.txt` の行数）と先頭数件のサイトを実際に開いて見る。
7. 問題があれば、該当する key を `review_state.py` で却下してから再実行する（`--date` を変えず再実行すれば SQL が作り直される。成功済みの段は飛ばされるので、`sql` 以降を作り直したいときは `--from-stage sql` を付ける）。

## 本番反映の前後確認（--apply-prod）

prod 段は次の順に動く。どれかが失敗した時点で止まる。

1. `wrangler d1 time-travel info mise --json` で bookmark を取り、`bookmark.txt` に保存して `status.json` の `bookmark` に記録（取れなければ反映しない）。
2. `wrangler d1 execute mise --remote --yes --file approved-sites.sql`（ログは `prod-execute.log`）。SQL は冪等なので、途中で失敗しても再実行で増えない。
3. `scripts/check-data-integrity.py --remote`（ログは `prod-integrity.log`）。error があれば exit 1。

投入の前に、読み取りだけの `check-data-integrity.py --remote` を走らせる（ログは `prod-integrity-before.log`）。**error なら投入しない**（前の夜の error を放置したまま重ねない）。

再実行の扱い: 投入が失敗した夜（`prod_applied` が false で `bookmark.txt` あり）の再実行は、最初に取った bookmark を**上書きせず**そのまま使って再投入する。**`prod_applied` が true の夜は同じ `--date` での再投入を拒否**する（投入後の整合 error など。bookmark も保持）。手順は下の「ロールバック」。

人が見るもの: 反映前後で `SELECT count(*) FROM sites` の差が `added` と一致するか、サイトの一覧に新規が出るか（`--remote` は SELECT のみで）。

## ロールバック

1. `status.json` の `bookmark` を控える（`bookmark.txt` に原文）。
2. 本番の整合チェックが error のとき、**そのまま再実行しない**。原因を見る（`prod-integrity.log`）。
3. 戻す場合のみ: `npx wrangler d1 time-travel restore mise --bookmark <bookmark>`。復元は反映後の全書き込み（申請など）も巻き戻す。実行は kako-jun が判断する。
4. 復元したら、その夜の出荷済み key を `var/nightly/shipped.txt` から削除する（`keys.txt` の key）。削除しないと翌夜以降も出荷されない。

## トラブルシュート

| 症状 | 見る場所 / 対処 |
|---|---|
| exit 75 | 別の夜間ジョブが動いている。`ps` で確認。残骸なら待つ（flock はプロセス終了で自動解除）。cron に外側の `flock` を付けていると毎夜これになる |
| llm 段が `ANTHROPIC_API_KEY が無い` | cron のラッパーで `anthropic.env` を読み込めていない |
| llm 段が skipped「予算ガード」 | 当月の台帳合計 + 最悪費用が `--budget-month` を超える。台帳は `~/.config/mise/llm-ledger.jsonl`。月が変われば自動で再開 |
| llm 段が `partial`「未解決あり」 | 一部が不正出力。同じ `--date` で再実行すると続きから（キャッシュ済みは課金されない）。それまで prod 段は止まる |
| 開いたままの予約（台帳） | `scripts/llm/ledger_release.py --list` で確認してから解放 |
| sql 段 / gate 段が整合チェック違反 | `report.md` の違反行を見る。ほとんどは台帳側の判定データ。該当 key を却下して再実行 |
| discover-cc が失敗 | 続行される（`guards` に記録）。ネットワークか Common Crawl 側 |
| fetch が遅い | `--fetch-limit` を下げる。cron の間隔を空ける |
| 追加が 0 件 | 承認ポリシーで保留が多い（朝の手順 4）。判定対象が尽きているなら `--discover-limit` を上げる |
| favicon 段が guards に出る | Pillow が無い等。`uv run --with pillow` 起動か確認（[favicons.md](../collection/favicons.md)） |
| `favicons.sql`（`--since` 付き）を適用し忘れた | `--since` なしで全件を出し直す（[手順](../collection/favicons.md#使い方)。何度適用しても安全） |

## 未対応（v1）

- **Issue #124 の段 6（ローカル D1 で検証してから本番）は未対応**。現状の gate 段は生成 SQL を**空 DB（マイグレーションのみ）**に流す整合チェックで、本番の既存データでの検証ではない。`--current-db`（本番再現の sqlite）を渡せば、その上で SQL を流して判定する。cron の既定では未使用。
- 葉（カテゴリ）の目標未達を優先した選抜（各葉 100 件の最低ライン）。今は key 順。
- Wikidata からの発見（#128）。ファビコン取得（#125）は `favicon` 段（既定無効）まで実装済みで、R2 への配信と既存全サイトの一括取得は未対応。
- 再巡回（[recrawl](../collection/recrawl.md)）の夜間組み込み。
- 構想の全体は [ideas/llm-judging.md](../ideas/llm-judging.md) と Issue #124。
