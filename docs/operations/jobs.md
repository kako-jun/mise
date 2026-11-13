# 定期処理・鍵・LLM 予算・巡回作法

## 定期処理

現状、**自動巡回・定期処理は稼働していない**（cron は未登録）。収集は手元で実行し、結果を migration として手動で本番に適用している。

登録するなら、手元 PC または Raspberry Pi で、リポジトリを置いたユーザーの crontab に次のような形にする想定（登録は台帳の規模・実行時間を見てから）。Workers やエージェントでは回さない。

```cron
# 毎日 03:10: 取得前→取得(上限つき・failed の再試行込み)→判定。発見は週1で別ジョブ
10 3 * * *  cd /path/to/mise && flock -n var/pipeline.lock sh -c 'uv run python3 scripts/pipeline/run.py triage && uv run python3 scripts/pipeline/run.py fetch --limit 300 --workers 3 && uv run python3 scripts/pipeline/run.py judge' >> var/pipeline.log 2>&1
30 2 * * 0  cd /path/to/mise && flock -n var/pipeline.lock uv run python3 scripts/pipeline/run.py discover-cc --sample-every 1000 --limit 2000 >> var/pipeline.log 2>&1
```

1 回の `fetch` は `--limit` で打ち切り、残りは翌日以降に `queued` から引く。`failed` はバックオフ（[pipeline.md](../collection/pipeline.md)）で再試行される。同時に複数起動しない（`flock`）。`var/` は再生成可能なので、バックアップは任意。

再巡回（[recrawl.md](../collection/recrawl.md)）は週 1 程度で `recrawl.py run --limit 50` を回す想定（cron は未登録。`flock` で単独起動）。

官公庁の名簿は月 1 回程度、差分追従を手動で回す（[public-bodies.md](../collection/public-bodies.md#廃止合併新設名称変更url-変更への追従)）。

## 巡回作法（共通）

[procedures.md](../collection/procedures.md#共通の作法) と [pipeline.md](../collection/pipeline.md#取得作法) が正本。要点: UA 明示、robots.txt 遵守、ホスト別に待機、低い同時実行数、内部 IP 拒否、取得上限。

## 鍵・クレジット・LLM 予算

LLM 判定工程（紹介文・スパム一次判定・分類候補）は**未実装**で、方針と設計案は [ideas/llm-judging.md](../ideas/llm-judging.md)。工程を動かすときに守る運用ルール:

- LLM は手元の PC/Raspberry Pi から API で呼ぶ。**公開サーバーからは呼ばない**。Claude Code の対話利用のトークンは使わない。
- API キーはリポジトリの外のユーザー設定ファイル（権限 600）に置き、環境変数として読む。**キーの値をログ・出力・コミット・Issue・PR・docs に書かない**。
- 費用は 1 件ごとの usage から記録し、実行ごとの上限と全体の上限（予算ガード）を超えたら停止する。入力ハッシュで結果をキャッシュし、再実行で課金しない。
- 月額クレジットは期限で失効する（2026-11-13 時点の観測: 期限は 2026-12-08 UTC、繰り越しなし）。
