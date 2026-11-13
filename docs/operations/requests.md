# 申請の受付と管理レビュー

推薦（`add`）・取り下げ（`remove`）・訂正（`correct`）の受付の仕様と運用手順。Issue #4・#29。**受付と保存だけが実装済み**。承認の反映と巡回側の拒否リスト利用は未実装（[ideas/requests.md](../ideas/requests.md)）。方針の決定は [decisions.md](../decisions.md)。

## 原則

- 3 種は同じ「申請」で、1 つのテーブル `requests` と同じ状態遷移（`pending` → `approved` / `rejected`）で扱う。
- **申請は依頼であって、叶うとは限らない**。画面にもそう書く。承認・却下は管理者が決める。
- **確認は管理者（mise の運営者）がサイト自身の公開情報で判断する**。mise にトークン設置などの確認機構は作らない。申請者の連絡先は集めず、**結果は申請者に通知しない**（不採用でも伝えない。通知機能は作らない）。
- **確認が取れて承認するまで、掲載の状態は何も変えない**（推薦は未掲載のまま、取り下げ・訂正は掲載のまま）。一時非表示にはしない（他人が競合サイトを外す悪用を防ぐため。運営者の保護は、確認を急ぐ運用で担う）。
- 公開 API は作らない。`POST /api/requests` で受けるだけで、申請の読み出し API は無い。
- X への自動投稿はしない。
- robots.txt の Disallow・noindex などサイトの運営者が自サイトに出す意思表示は申請ではなく収集時の遵守事項（[discovery-policy.md](../collection/discovery-policy.md)）。

## 受付 API

`POST /api/requests`（同一オリジンの `fetch` のみ）。`INTAKE_SALT` が未設定なら 503 で受付を止める。`Origin` が自サイトでなければ 403、`Content-Type`（`;` より前）が `application/json` でなければ 415（JS なしのフォーム送信で HTML を求める場合は、「JavaScript を有効にして送信してください」の簡単な HTML を 415 で返す）、本文は 8192 バイトまで（日本語の理由 1000 字でも収まる）（`Content-Length` で先に拒否し、読み込みも上限つき。超過は 413）。

| 項目 | 内容 |
|---|---|
| `type` | `add` / `remove` / `correct`（必須） |
| `url` | 対象サイト（必須）。`https://` を省略可（`example.com:443` も可）。http/https 以外・認証情報・ポート指定・IP アドレス・`localhost`・ドットなしのホスト・`.local` / `.internal` 等は拒否。クエリとフラグメントは捨て、ホストは小文字にして保存。別に比較キー `normalized_key`（スキーム・`www.`・末尾スラッシュ・大文字小文字・`%7E` と `~` など unreserved の %エンコードを同一視）を保存し、グルーピングに使う |
| `reason` | 理由・詳細。1000 字まで。**全種別で必須**（空・空白のみは 400 `reason_required`）。制御文字（C0/C1・双方向制御・ゼロ幅文字）を除去し、HTML のタグらしいもの（`<` の直後が英字・`/`・`!`・`?`）は拒否（`a < b` は可）。プレーンテキストとして保存 |
| `contact` | 受け付けない。送られてきても無視し、保存しない（`requests.contact` 列は未使用で常に NULL） |
| `website` | ハニーポット（人間には見えない欄）。値があれば保存せず成功を返す |

応答は成功 `202 {status:'pending'}`、検証エラー `400 {error}`、レート超過 `429`。すべて `Cache-Control: no-store`。**同一 URL の申請は全件保存する**（本物の運営者の申請を、先に出された他人の申請が握りつぶさないため）。新しい行を積まずに成功を返すのは、同じ IP ハッシュから同種別・同じ比較キーの `pending` が既にある再送だけ（連打対策）。同一 URL への複数の申請は、レビュー時にグルーピングして見る（下）。

**SSRF**: サーバーは申請された URL を一切取りに行かない（検証は文字列だけ）。管理者が確認で開くときは、自分のブラウザで開く。

### スパム・レート制限

D1 の `requests` で、再送の判定とレート制限を INSERT と同時に行う（単一の `INSERT … SELECT … WHERE`。競合しても上限を超えない）。IP は生で保存せず、`SHA-256(INTAKE_SALT + IP)` の先頭 24 文字（`ip_hash`）だけを残す（IPv6 は /64 に、`::ffff:a.b.c.d` は IPv4 に直してから。日付は含まない）。`INTAKE_SALT` は本番必須の secret（[deploy.md](deploy.md#秘密情報)）。

| 上限 | 値 |
|---|---|
| 同一 IP（IPv6 は /64） | 1 時間に 3 件、24 時間に 10 件 |
| 同一 /48（IPv6）の緩い上限 | 24 時間に 30 件（`net_hash`） |
| 全体（推薦 `add`） | 24 時間に 300 件 |
| 全体（取り下げ・訂正） | 別枠で 24 時間に 100 件（推薦の洪水で本物の取り下げが止まらないように分ける） |

超えた申請は 429 で保存しない。**全体上限は、大きなアドレス帯（/48 など）を持つ相手に埋められうる**。本物のサイトの運営者が 429 になっても、代替窓口は設けない（画面は「しばらくしてからもう一度」だけで、GitHub Issue などへは誘導しない）。拒否リスト（`blocked_hosts`）に当たる推薦は、`rejected`・`note='blocklisted'` で記録する。照合は `www.` を無視し、登録ドメイン配下（`blog.example.com` は `example.com` の拒否で止まる）も対象にする（`path_prefix` が空の行）。**パス型の行は、そのホスト自身のパス配下だけに効く**（`ameblo.jp/foo` の拒否は `s.ameblo.jp/foo` のようなサブドメインには効かない。サブドメイン型のサービスは別の行として `host` を入れる）。パスは照合前に畳む（`//foo`・`/foo%2F`・`/foo;x`・`/foo%20`・`/foo.` は `/foo` とみなす。`%2F` は `/`、`;params` と `.`・`..` セグメント、末尾の `.`・空白・`/` を除く）。**自動却下は補助**で、拒否リストへの追加（特にホスト全体の拒否）の承認は人が必ず目で確認する。`path_prefix` を持つ行（パス型のユーザー領域。`ameblo.jp/foo`、`note.com/foo`、`sakura.ne.jp/~bob`）は、そのホスト自身のそのパス配下の推薦だけを止め、サービス全体は止めない。共有ホスティング（`github.io`・`pages.dev`・`blogspot.com` など。一覧は `src/intake.js` の `SHARED_SUFFIXES`）はユーザー領域（`user.github.io`）の完全一致とその配下だけで、親には広げない。取り下げ・訂正は拒否リストにあっても `pending` で記録する。

## データ

migration `0016_intake.sql`。

- `requests`: `type`, `status`, `url`, `host`, `normalized_key`, `reason`, `contact`（未使用。常に NULL。廃止前の名残で、migration は足さず残す）, `ip_hash`（NULL 可）, `net_hash`（NULL 可。IPv6 /48 単位のハッシュ）, `note`, `created_at`, `decided_at`。
- `blocked_hosts`: `host` + `path_prefix`（主キー。`''` はホスト全体、それ以外は小文字・先頭 `/`・末尾 `/` なしのパス配下だけ）, `reason`（`removal_request` / `robots` / `noindex` / `manual`）, `request_id`, `created_at`。取り下げを承認した、または robots/noindex で外したホストを入れる。巡回・推薦で再追加しないための台帳。

## 個人情報の保持期間

- 申請者の連絡先は集めない（`contact` 列は未使用）。`migrations/0016_intake.sql` の `contact` 列コメント（90 日後に NULL）と `note` コメント（blocklisted は連絡先を保存しない）は、連絡先の廃止前の記述で、現在は当てはまらない（適用済みのためファイルは変えない）。
- 未処理のまま 180 日たった申請は、却下にする。
- `ip_hash`・`net_hash` は 7 日後に NULL にする。
- 誤申請・取り消しの窓口は画面に無い。管理者が `rejected`・`note='withdrawn'` にする。

定期の掃除（手動。月 1 回程度）:

```sh
npx wrangler d1 execute mise --remote --command "
UPDATE requests SET status='rejected', note='expired', decided_at=datetime('now') WHERE status='pending' AND created_at < datetime('now','-180 days');
UPDATE requests SET ip_hash=NULL, net_hash=NULL WHERE (ip_hash IS NOT NULL OR net_hash IS NOT NULL) AND created_at < datetime('now','-7 days');"
```

## 管理レビューの手順

管理画面は無い。`wrangler d1 execute` で行う（本番は `--remote`、ローカルは `--local`）。理由は**利用者の入力**なので、画面やメール本文に貼るときはプレーンテキストとして扱い、記載のリンクを不用意に開かない。

1. 未処理を、同じ対象（比較キー+種別）ごとに束ねて見る。同じ対象への申請が複数あるときは、**誰が本物のサイトの運営者かを確認で見分ける**（先に出た申請を信用しない）:

   ```sh
   npx wrangler d1 execute mise --remote --command "SELECT normalized_key, type, COUNT(*) AS n, group_concat(id) AS ids, MIN(created_at) AS first_at FROM requests WHERE status='pending' GROUP BY normalized_key, type ORDER BY first_at LIMIT 50"
   # 個々の申請
   npx wrangler d1 execute mise --remote --command "SELECT id,type,url,reason,created_at FROM requests WHERE status='pending' AND normalized_key='example.com/blog' ORDER BY created_at"
   ```

2. 確認は**サイト自身の公開情報**（サイトの運営者の記載、サイトの内容）を見て管理者が行う。申請者へは連絡しない。確認できない申請は `pending` のまま置くか却下する（掲載の状態は変えない）。
3. 決定を記録する:

   ```sh
   # 却下
   npx wrangler d1 execute mise --remote --command "UPDATE requests SET status='rejected', note='not verified', decided_at=datetime('now') WHERE id=12"
   # 承認（取り下げ）: まず対象を確認する(1 行だけ・type='remove'・pending であること。申請 URL を目で見る)
   npx wrangler d1 execute mise --remote --command "SELECT id,type,status,url,normalized_key FROM requests WHERE id=12 AND type='remove' AND status='pending'"
   # 記録 + 拒否リスト。サイトの削除は別途（下）。同じ対象の他の未処理は、確認結果に応じて別途 rejected にする
   # 拒否リストへは、申請の対象の範囲だけを入れる: 申請 URL にパスがあれば path_prefix つき(そのパス配下だけ)、ホストだけならホスト全体。
   # host は小文字・www. なし、path_prefix は小文字・先頭 / ・末尾 / なし(normalized_key から切り出す)。
   # INSERT を先に、UPDATE を後に(UPDATE で pending で無くなると INSERT が空振りするため)。どちらも type='remove' AND status='pending' で歯止め
   npx wrangler d1 execute mise --remote --command "INSERT OR IGNORE INTO blocked_hosts (host,path_prefix,reason,request_id) SELECT substr(normalized_key,1,instr(normalized_key||'/','/')-1), CASE WHEN instr(normalized_key,'/')>0 THEN substr(normalized_key,instr(normalized_key,'/')) ELSE '' END, 'removal_request', id FROM requests WHERE id=12 AND type='remove' AND status='pending'; UPDATE requests SET status='approved', decided_at=datetime('now') WHERE id=12 AND type='remove' AND status='pending'"
   # 追加後に対象行を確認する
   npx wrangler d1 execute mise --remote --command "SELECT host,path_prefix,reason,request_id FROM blocked_hosts WHERE request_id=12"
   # ホスト全体を拒否してよいのは、サイトの運営者がそのドメイン全体の所有者と確認できた場合だけ。パス付きの申請(ブログサービスなどのユーザー領域)で
   # ホスト全体を入れると、そのサービス全体が以後拒否されるので入れない。必要なときだけ手で: INSERT OR IGNORE INTO blocked_hosts (host,path_prefix,reason,request_id) VALUES ('example.com','','removal_request',12)
   ```

4. 承認後の反映は現状手動:
   - `remove`: 掲載を外す（`sites` の該当行と `site_categories`・`site_tags` を削除。`data/` の台帳も同期）。
   - `correct`: `sites` の該当列を更新（[procedures.md](../collection/procedures.md)）。
   - `add`: 通常の候補として発見パイプラインに投入し、[listing-criteria.md](../concept/listing-criteria.md) で審査する（推薦は承認しても自動で載らない）。
5. robots.txt / noindex で外したホストは、申請なしで `blocked_hosts` に入れる:

   ```sh
   npx wrangler d1 execute mise --remote --command "INSERT OR IGNORE INTO blocked_hosts (host,path_prefix,reason) VALUES ('example.com','','robots')"   # host は小文字・末尾ドットなし・www. なしで書く。パス単位の noindex は path_prefix に
   ```

## 確認

`npm test`（検証・SSRF 対策・比較キー・拒否リスト照合・レート制限の分岐）と `npm run check`（POST の `no-store` を含む）。画面の確認は `npm run dev`（`.dev.vars` に `INTAKE_SALT=任意の文字列` を置く。コミットしない）で、申請フォームの成功・不正入力・レート制限を見る。
