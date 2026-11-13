# 申請の受付と管理レビュー

推薦（`add`）・取り下げ（`remove`）・訂正（`correct`）の受付の仕様と運用手順。Issue #4・#29。**受付と保存だけが実装済み**。承認の反映と巡回側の拒否リスト利用は未実装（[ideas/requests.md](../ideas/requests.md)）。方針の決定は [decisions.md](../decisions.md)。

## 原則

- 3 種は同じ「申請」で、1 つのテーブル `requests` と同じ状態遷移（`pending` → `approved` / `rejected`）で扱う。
- **申請は依頼であって、叶うとは限らない**。画面にもそう書く。承認・却下は管理者が決める。
- **本人証明はサイトの外で行う**。mise にトークン設置などの確認機構は作らない。管理者が、連絡先への返信や公開されている運営者の連絡手段で確認する。
- **確認が取れて承認するまで、掲載の状態は何も変えない**（推薦は未掲載のまま、取り下げ・訂正は掲載のまま）。一時非表示にはしない（他人が競合サイトを外す悪用を防ぐため。運営者の保護は、確認を急ぐ運用で担う）。
- 公開 API は作らない。`POST /api/requests` で受けるだけで、申請の読み出し API は無い。
- X への自動投稿はしない。
- robots.txt の Disallow・noindex など運営者が自サイトに出す意思表示は申請ではなく収集時の遵守事項（[discovery-policy.md](../collection/discovery-policy.md)）。

## 受付 API

`POST /api/requests`（同一オリジンの `fetch` のみ。`Origin` が自サイトでなければ 403、`Content-Type: application/json` 以外は 415、本文は 4 KB まで）。

| 項目 | 内容 |
|---|---|
| `type` | `add` / `remove` / `correct`（必須） |
| `url` | 対象サイト（必須）。`https://` を省略可。http/https 以外・認証情報・ポート指定・IP アドレス・`localhost`・ドットなしのホスト・`.local` / `.internal` 等は拒否。クエリとフラグメントは捨て、ホストは小文字にして保存 |
| `reason` | 理由・詳細。1000 字まで。`correct` は必須。制御文字を除去し、HTML のタグを含むものは拒否。プレーンテキストとして保存 |
| `contact` | 連絡先（必須）。メールアドレスか `https://` の URL |
| `website` | ハニーポット（人間には見えない欄）。値があれば保存せず成功を返す |

応答は成功 `202 {status:'pending'}`、検証エラー `400 {error}`、レート超過 `429`。すべて `Cache-Control: no-store`。同じ種別・同じ URL が `pending` の間は、新しい行を積まずに成功を返す。

**SSRF**: サーバーは申請された URL を一切取りに行かない（検証は文字列だけ）。管理者が確認で開くときは、自分のブラウザで開く。

### スパム・レート制限

D1 の `requests` で、INSERT と同時に判定する（競合しても上限を超えない）。IP は生で保存せず、SHA-256 の先頭 24 文字（`ip_hash`）だけを残す。塩は任意の secret `INTAKE_SALT`（未設定でも動く。設定すると IP の逆引きがさらに難しくなる）。

| 上限 | 値 |
|---|---|
| 同一 IP | 1 時間に 3 件、24 時間に 10 件 |
| 全体 | 24 時間に 300 件 |

超えた申請は 429 で保存しない。拒否リスト（`blocked_hosts`）にあるホストへの推薦は、`rejected`・`note='blocklisted'` で記録し、承認候補に載せない。取り下げ・訂正は拒否リストにあっても `pending` で記録する。

## データ

migration `0016_intake.sql`。

- `requests`: `type`, `status`, `url`, `host`, `reason`, `contact`, `ip_hash`, `note`, `created_at`, `decided_at`。
- `blocked_hosts`: `host`（主キー）, `reason`（`removal_request` / `robots` / `noindex` / `manual`）, `request_id`, `created_at`。取り下げを承認した、または robots/noindex で外したホストを入れる。巡回・推薦で再追加しないための台帳。

## 個人情報（連絡先）の保持期間

- 連絡先は確認のためだけに使う。**決定（承認・却下）の 90 日後に NULL にする**。
- 未処理のまま 180 日たった申請は、却下にして連絡先を NULL にする。
- `ip_hash` は 7 日後に空文字にする。
- 誤申請・取り消しは、申請者が連絡先に返信して伝えてもらう（画面に取り消しの窓口は無い）。管理者は `rejected`・`note='withdrawn'` にする。

定期の掃除（手動。月 1 回程度）:

```sh
npx wrangler d1 execute mise --remote --command "
UPDATE requests SET contact=NULL WHERE contact IS NOT NULL AND decided_at < datetime('now','-90 days');
UPDATE requests SET status='rejected', note='expired', decided_at=datetime('now'), contact=NULL WHERE status='pending' AND created_at < datetime('now','-180 days');
UPDATE requests SET ip_hash='' WHERE ip_hash<>'' AND created_at < datetime('now','-7 days');"
```

## 管理レビューの手順

管理画面は無い。`wrangler d1 execute` で行う（本番は `--remote`、ローカルは `--local`）。連絡先・理由は**利用者の入力**なので、画面やメール本文に貼るときはプレーンテキストとして扱い、記載のリンクを不用意に開かない。

1. 未処理を見る（古い順）:

   ```sh
   npx wrangler d1 execute mise --remote --command "SELECT id,type,url,reason,contact,created_at FROM requests WHERE status='pending' ORDER BY created_at LIMIT 50"
   ```

2. 本人確認は**サイトの外**で行う。連絡先へ返信する、サイト上に公開されている運営者の連絡手段で照合する、など。返信が来るとは限らず、確認できない申請は `pending` のまま置くか却下する（掲載の状態は変えない）。
3. 決定を記録する:

   ```sh
   # 却下
   npx wrangler d1 execute mise --remote --command "UPDATE requests SET status='rejected', note='not verified', decided_at=datetime('now') WHERE id=12"
   # 承認（取り下げ）: 記録 + 拒否リスト。サイトの削除は別途（下）
   npx wrangler d1 execute mise --remote --command "UPDATE requests SET status='approved', decided_at=datetime('now') WHERE id=12; INSERT OR IGNORE INTO blocked_hosts (host,reason,request_id) SELECT host,'removal_request',id FROM requests WHERE id=12"
   ```

4. 承認後の反映は現状手動:
   - `remove`: 掲載を外す（`sites` の該当行と `site_categories`・`site_tags` を削除。`data/` の台帳も同期）。
   - `correct`: `sites` の該当列を更新（[procedures.md](../collection/procedures.md)）。
   - `add`: 通常の候補として発見パイプラインに投入し、[listing-criteria.md](../concept/listing-criteria.md) で審査する（推薦は承認しても自動で載らない）。
5. robots.txt / noindex で外したホストは、申請なしで `blocked_hosts` に入れる:

   ```sh
   npx wrangler d1 execute mise --remote --command "INSERT OR IGNORE INTO blocked_hosts (host,reason) VALUES ('example.com','robots')"
   ```

## 確認

`npm test`（検証・SSRF 対策・拒否リストの分岐）。画面は `npm run dev` で開き、申請フォームの成功・不正入力・レート制限を確認した。
