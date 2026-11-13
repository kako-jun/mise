# 申請の未実装部分（構想）

Issue #4・#29。**受付・保存・拒否リストの台帳は実装済み**（正本: [operations/requests.md](../operations/requests.md)）。ここには未実装の部分だけを置く。

## 未実装

- 承認の反映の自動化（現状は管理者が SQL と台帳を手で更新する）。
- 発見パイプライン（`scripts/pipeline/`）が D1 の `blocked_hosts` を読んで、拒否リストのホストを再収集しない接続。現状はパイプライン側のローカル拒否リスト（`scripts/pipeline/blocklist/`）と別。
- 巡回時の robots.txt Disallow・noindex の検出から `blocked_hosts` への自動登録。
- 管理画面（認証つき）。当面は `wrangler d1 execute` で足りる。
- 申請者が自分で取り消す窓口、申請状況の照会（いずれも公開 API を増やすため、必要になるまで作らない）。
- 連絡先のメール返信の自動化（当面は管理者が手で返信する）。
