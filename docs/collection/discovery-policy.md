# 発見・確認の方針

サイトを見つけて載せるまでの方針。載せる基準は [listing-criteria.md](../concept/listing-criteria.md)、層ごとの実装は [pipeline.md](pipeline.md)（独立サイト）と [public-bodies.md](public-bodies.md)（官公庁）、実行手順は [procedures.md](procedures.md)。

## 原則

- **決定論の収集 + LLM の判断**。発見・取得・抽出・URL 正規化・重複検査・robots 確認・フィルタまではスクリプト（LLM なし）で行う。LLM は別工程で、手元から一括処理する（[operations/jobs.md](../operations/jobs.md)）。入力は取得した文章のみで、LLM 自身は Web を探さない。出力は紹介文・タグ・分類候補・スパム一次判定。取得した内容に書かれたことだけを使う。
- 公開サーバーから従量課金 API を自動で呼ばない。
- すべて「候補」として保存し、審査後に公開する。AI の使用自体はマイナス評価にしない。

## 発見と確認

- 信頼できる既存のディレクトリ、公開リンク集、推薦、すでに確認したサイトの外部リンク、Common Crawl のホストグラフなどを起点とし、取得元と発見日時を候補ごとに記録する。リンクを発見したことは掲載承認を意味しない。「アーカイブに無いサイトは無い」とは仮定しない。
- URL の正規化と既存レコード・候補との重複検査を行う。http/https・リダイレクト・canonical を確認し、安易なドメイン統合で別ブログを潰さない。
- HTTP 取得前に robots.txt を確認し、明示した User-Agent、サイト別の低い同時実行数と間隔、容量・深さの上限を設ける。禁止、レート制限、エラー時は停止または再試行待ちにする。URL 候補経由の内部 IP・リダイレクトも制限する。運営者が自サイトに出す意思表示（robots.txt の Disallow、noindex 等）は申請ではなく収集時の遵守事項として尊重する。
- メタデータ抽出・紹介文・分類・所在地は候補として出典を残し、未確認を明示してレビュー後に公開する。取り下げ・訂正・推薦の窓口は [operations/requests.md](../operations/requests.md)（受付のみ。承認の反映と巡回側の拒否リスト接続は [ideas/requests.md](../ideas/requests.md)）。
- 自治体の公式サイトは対象とする。公的機関の信頼できる一覧と独立した 2 つ以上の出典の一致でのみ採用する（[public-bodies.md](public-bodies.md)）。確認できない場合は公式扱い・自動掲載しない。
- 共有サービス配下の個別ユーザー領域の扱いは未決（Issue #50）。

## 再巡回

掲載サイトの到達不能は削除でなく状態遷移で扱う方針（Issue #2・#53）。実装は未着手で、構想は [ideas/exchange-and-deliberation.md](../ideas/exchange-and-deliberation.md)。

## 定期処理

現状、自動巡回は稼働していない。cron の想定手順は [operations/jobs.md](../operations/jobs.md)。候補・取得・審査・公開・再巡回を別状態に分け、各段階のエラー、出典、時刻、robots 判定を残す方針。実行上限・頻度・人手レビューの運用は未決。

## 試行の記録

初回調査や固定シードの試行の結果は [trials.md](trials.md)。
