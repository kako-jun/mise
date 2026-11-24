# ファビコンの取得と更新

Issue #125。**自前で取得し、32px の画像にして保存し、月 1 回更新する**。表示時に第三者のサイトへ問い合わせない（訪問者の IP・閲覧の事実・Referer を漏らさない。MISE の「人気や閲覧数を扱わない」方針）。**取れなければ何も出さない。代替アイコン（頭文字・猫模様など）は作らない**（kako-jun 決定 2026-11-18）。

> **運用更新**: 自動取得は `scripts/nightly_media.py` / `scripts/media_worker.py` の D1/R2 経路を使う。下記 `favicon.py` の SQLite/ローカル保存/SQL 手順は旧来の単体運用で、オンライン移行 importer 以外からは再開しない。移行設計は [夜間ジョブの画像キュー](../operations/nightly.md#画像キューd1r2)。

## 実装の範囲

| 実装済み | 未実装（後続） |
|---|---|
| 取得・縮小・台帳・ローカル保存・`sites.favicon_path` 向け SQL・月次更新・条件付き GET・`nightly.py` の任意の段。R2 への配信ルート `/icon/<k>.webp?v=…`（`src/media.js`）・API の `favicon_url`・一覧での表示・`R2Store`（#125 の R2 部分。**R2 バケット `mise-media` の作成後にマージ**） | 既存全サイトの一括取得、取り下げ申請（#29）の受付フローとの接続（R2 から消す `delete` は実装済み。下の「取り下げ」）、サムネイル（#130）の撮影ジョブは [thumbnails.md](thumbnails.md) |

保存先は `IconStore`（`put` / `exists` / `delete`）の抽象で、実装は既定のローカルディレクトリ `var/favicons/<k>.webp`（`LocalDirStore`。`<k>` はサイト固定のキー）と、R2 へ置く `R2Store`。**既定はローカルのまま**で、R2 へ書くのは `run --apply --store r2` と明示したときだけ。本番 D1 には書かない（SQL の適用は人）。

## 使い方

実装は `scripts/favicon/favicon.py`、テストは `tests/test_favicon.py`・`tests/test_favicon_rekey.py`。画像の検証・縮小に Pillow を使う（リポの依存には足さず、**必ず `uv run --with pillow python3` で起動する**）。Pillow が無いと `run --apply` は終了コード 3 で止まる（1 サイトの失敗として握りつぶさない）。`nightly.py` の `favicon` 段も同じ起動方法で呼ぶ。

```sh
uv run --with pillow python3 scripts/favicon/favicon.py import --recrawl var/recrawl.sqlite   # 再巡回台帳の listed かつ取得成功済みのサイトを対象へ(--sites JSON も可)
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50            # 計画だけ(ネットワークなし。台帳もディレクトリも作らない)
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50 --apply    # 期限が来たものを取得(実サイトにアクセスする)
uv run --with pillow python3 scripts/favicon/favicon.py sql --since 2026-11-20T00:00:00Z --out var/favicons.sql   # sites.favicon_path / favicon_v の UPDATE(適用は人が行う。既定は --store r2-remote の印がある画像だけ。ローカルのみなら --store local)
uv run --with pillow python3 scripts/favicon/favicon.py delete --site <id>   # 取り下げ・除外: 計画だけ(dry-run)。消すのは --apply --d1-done(本番は --r2-remote も明示)
uv run --with pillow python3 scripts/favicon/favicon.py status
```

台帳は `var/favicons.sqlite`（`.gitignore`、再生成可能）。出力 SQL は `UPDATE sites SET favicon_path='<k>.webp', favicon_v='<v>' WHERE id=... AND (favicon_path IS NOT '<k>.webp' OR favicon_v IS NOT '<v>')`（`<k>` = サイト固定のキー、`<v>` = 内容ハッシュの先頭 8 桁。**内容が変わったとき（と path だけ旧名の行）だけ書く**。D1 の書き込み行数を使わない）。取れていないサイトには触れない。除外・取り下げ（`delete`）したサイトには `favicon_path=NULL, favicon_v=NULL` にする UPDATE が付く（何度流しても安全）。`run`（`--apply` なし）・`sql`・`status` は台帳を読み取り専用で開き、無ければ何も作らない。

**`--since` の SQL を適用し忘れたとき**（`--since` 付きで出した SQL を適用しないまま次回が来た等）は、`--since` なしで全件を出し直す: `sql --out var/favicons-all.sql`。`AND (favicon_path IS NOT ... OR favicon_v IS NOT ...)` があるので、すでに合っている行は書かれず、何度適用しても安全。`favicon_v` 列は migration 0029（#163）。

## 取得作法

発見パイプラインの `scripts/pipeline/fetcher.py`（`http_get`・`RobotsCache`・`same_site`）を再利用する。独自の HTTP 層は持たない。`http_get` に足したのは条件付き GET 用の追加ヘッダ引数だけ。

- UA は `MiseResearchBot`、robots.txt 遵守（ページ・画像の各 URL のオリジンごとに判定）、内部 IP 拒否（`ssrf_blocked`）、ポートは 80/443 のみ。リダイレクトは 3 ホップまでで、**ホップごとに robots と SSRF を再判定**する。
- ページ取得のリダイレクトは同一サイト内のみ（別ドメインへ出たら `redirect_offsite`）。画像は CDN など別ホストに置かれることが多いので別ホストへの転送・参照を許すが、そのホストの robots と SSRF 検査は通す。
- 同一ホストへのリクエスト間隔は 2 秒以上。画像は 256KB まで、元画像の一辺は 16〜1024px。1 サイトで試す候補は 3 つまで。
- トップページが 5xx・429 のときは一時的な失敗（3 日後に再試行）とし、`/favicon.ico` へは進まない。404 など他の非 200 は `/favicon.ico` だけ試す。
- 候補のうち内部アドレス・robots 取得不能で試せないものは飛ばして次の候補へ進む。全候補がそれで試せなかったときだけ取得失敗（保留）にする。
- 条件付き GET のヘッダは元の URL にだけ付け（転送先には付けない）、`ETag` / `Last-Modified` の値から改行を除いてから送る。
- 直列実行（並列化は未実装。前回の画像 URL が分かるサイトは robots + 条件付き GET の 2 リクエストで済む）。

## 候補の優先順位

HTML の `<link>`（`<base href>` を考慮、相対 URL は解決）から、次の順に最大 3 つ試し、最初に検証を通ったものを採る。**末尾に必ず `/favicon.ico`**。

1. `rel=icon` で宣言サイズが 32px 以上（小さい方から。縮小して使うため）
2. `apple-touch-icon` / `apple-touch-icon-precomposed`
3. `rel=icon` でサイズ宣言なし
4. `rel=icon` で 32px 未満（大きい方から）
5. `/favicon.ico`

`mask-icon`、`type=image/svg+xml`・拡張子 `.svg` の候補、`data:` URI は候補にしない。

## 検証と縮小

- 受け付ける形式: PNG・ICO・GIF・WebP・JPEG・BMP。**SVG は不採用**（Content-Type・先頭の `<svg`/`<?xml` で弾く。無害化はしない）。HTML を返す soft-404、壊れた画像、完全に透明な画像、16px 未満、上限超過は不採用。
- 形式は `Image.open` の `formats` で限定し、辺の上限・縮小率は**読み込み後の実寸**で判定する（ICO などの埋め込み画像は宣言サイズと実寸が違いうる）。展開爆弾の警告も拒否扱い。
- ICO は入っている中で最大の絵、GIF は最初のフレームを使う。長辺を 32px に合わせ、正方形でなければ透明で余白を足して **32x32 の可逆 WebP**（透明保持）にする。
- **保存先のキーはサイト固定**（#163）: `<k>.webp`、`<k>` = `sha256(site_id)` の先頭 32 桁 16 進（サムネイルと同じ関数・規則。例: `sha256("s1")` → `e8bc163c….webp`。サイト ID から決定的に計算できる）。**1 サイト 1 画像・更新は同じキーへの上書き・世代管理なし**。内容ハッシュによる共有・重複排除はしない（既定アイコンなどが共有されて浮く利点は実測で約 40KB 分＝1,866 枚・合計約 2MB の世界でほぼ無く、一方で更新のたびに孤児が出て掃除が要り、取り下げ時に他サイトの参照確認が要った）。
- **内容ハッシュ**（出力画像の SHA-256 の先頭 32 桁 + `.webp`。台帳の `icon_hash`）は変更検知用にだけ持つ。D1 の `favicon_v`（表示のバージョン）はその先頭 8 桁。同じ内容なら保存先へ書き直さず、変わったときだけ同じキーへ上書きして `?v=` が変わる。

## R2 への保存と配信

配信は Worker の `/icon/<k>.webp?v=<favicon_v>`（`src/media.js` の `serveMedia`。名前は 32 桁 16 進だけ許可、それ以外は R2 を見ずに 404。Worker は query を無視して R2 を引く）。R2 のキーは `icon/<k>.webp`、バケットは `mise-media`（バインディング `MEDIA`）。応答は `ETag`（`If-None-Match` 一致で 304）・`Content-Type: image/webp` と、**`v` が正しい形（英数字 12 文字まで）なら `Cache-Control: public, max-age=31536000, immutable`**（URL ごとに不変）、v 無し・不正な v でも 200 を返すが短いキャッシュ（`max-age=3600`。上書きが反映される）。同じキーへの上書きは `?v=` が変わることでブラウザに伝わる。**過渡期（#163）**: `favicon_v` が NULL の旧データは従来どおり `/icon/<内容ハッシュ名>.webp`（v なし）で返す（どちらも同じ `/icon/` ルート）。R2 に無ければ 404（代替画像は作らない・404 は `no-store`）。`MEDIA` バインディングが無い環境でも Worker は落ちず 404 を返す。自サイト配下なので CSP の `img-src 'self'` のままで足りる。サムネイル（#130）は `MEDIA_KINDS.thumb` と `app.get('/thumb/:file', ...)` で同じ仕組みを使っている（[thumbnails.md](thumbnails.md)）。

```sh
# ドライラン（既定。ネットワークも wrangler も呼ばない）
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50 --store r2
# ローカル模擬の R2 へ（wrangler dev --local と同じ .wrangler/state。実サイトは取得する）
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50 --apply --store r2
# 本番バケットへ（バケット作成後、--r2-remote を明示したときだけ）
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50 --apply --store r2 --r2-remote
```

- `R2Store` は `npx wrangler r2 object put mise-media/icon/<k>.webp --file ... --content-type image/webp --local|--remote` を呼ぶ。**`--r2-remote` が無ければ常に `--local`**（本番に書かない安全側）。put の失敗は 1 サイト分の `store_failed` として続行する。
- **呼び出し数を抑える設計**: wrangler の起動は 1 回数秒かかるので、`exists` は既定で R2 に問い合わせず台帳の印を信じる。同じ実行中に置いた名前は記憶して 2 度 put しない。R2 から画像が消えた・別バケットへ移した疑いがあるときだけ `--r2-verify`（`wrangler r2 object get` で 1 件ずつ確認。遅い。このとき put も印があっても毎回やり直す）。印があれば（`--r2-verify` でない限り）同じ中身の 200 でも put しない（ETag を返さないサイトが毎月 200 でも書き込みは増えない）。画像は 1 件 約 1.5 KB なので 1 件ずつ put する（件数が増えて遅くなったら S3 互換 API での並列化を別 Issue で検討）。
- `--r2-verify` での取り直しと put が起きるのは、`next_check_at` が来たサイトだけ（最大約 30 日後）。期限が来たものから順に戻る。すぐ戻したいときは台帳（`icon_put`）の `r2-remote` の行を消して `--r2-remote` で回す（印が無いので期限前でも対象になる）。
- **台帳は保存先ごとに「置いた印」を持つ**（`icon_put` 表: サイト × `local` / `r2-local` / `r2-remote` → 最後に置いた内容〔`icon_hash`〕。キーはサイト固定で上書きなので 1 サイト 1 行）。旧い台帳（内容ハッシュ名の `icon_stored` 表のみ・`exclude` 列なし）には `connect()` が `icon_put` 表と `exclude` 列を自動で足す（旧 `icon_stored` は内容ハッシュ名の印で、新キーを置いた証拠にならないので引き継がず、消しもしない。`favicon_v` は `icon_hash` から導ける。新キーを置いた印は `rekey.py` か `run` が付ける）。`exists` の判定と期限（`due_sites`）はその保存先の印（と内容の一致）で行う。ローカルで置いたあとに `--r2-remote` を回すと、`next_check_at` が先でも「画像はあるのにこの保存先の印が無い」サイトが対象になり、304 でも条件なしで取り直して put する。nightly（`local`）で取得済みのものも同様に、初めて `--r2-remote` を回したときに R2 へ上がる。旧い台帳は新キーの印が無いので、そのまま `--r2-remote` で回すと全件が対象になる（304 のものは条件なしで取り直す）。**既存分は `rekey.py`（下の「移行」）で一括して置く**。
- 旧い台帳（`icon_put` 表・`exclude` 列が無い）を読み取り専用で開く `run`（dry-run）・`sql`・`status`・`delete`（dry-run）は落ちない。印を判定できず、件数が少なく（`sql --store ...` は 0 行に）出る。そのとき注記が出る。書き込み可で開く（`run --apply` や `import`）と表が足される。
- **`sql` は既定で `--store r2-remote` の印がある画像だけ**を出す（ローカルにしか置いていないものは出さない＝D1 が指す画像が配信元に無い状態を作らない）。`--store local` / `r2-local` も明示すれば選べる。
- 本番へ流す順序: バケット作成 → マージ・デプロイ → `--r2-remote` で画像を置く → `sql`（既定）の出力を D1 へ適用（`favicon_path` / `favicon_v` が入って初めて一覧に出る。画像が先、SQL が後なので、表示が 404 のままになる時間帯が出ない）。`--since` は取得日（`changed_at`）基準で、R2 に置いた日ではない（ローカルで取得して後から `--r2-remote` で置くと、`--since <置いた日>` は 0 行になる）。**R2 へ置いたあとの `sql` は、夜間の台帳を使う限り毎回 `--since` なし**にする。`AND favicon_v IS NOT ...` があるので毎回全件を出しても、すでに合っている行は書かれず D1 の書き込みは増えない。
- プレビュー（`wrangler preview`）の `MEDIA` は別バケット `mise-media-preview`（空のまま。本番バケットを触らせない）。プレビューでは `/icon/` は 404 になり、一覧はファビコンが出ない（サイト名は左端から始まる）。

## 取り下げ（#29）・除外での R2 画像の削除

`favicon.py delete --site <id>`（既定 dry-run）。キーがサイト ID から決まる（`icon/<k>.webp`）ので、**他サイトの参照確認なしで 1 本消せる**（サムネイルの `thumbnail.py delete` と同じ形）。`--apply` で消し、台帳ではそのサイトを除外（`exclude=1`）にして `icon_hash` を空にする。`sql` は除外サイトの `favicon_path` / `favicon_v` を NULL にする UPDATE を出す。**手順はこの順**: ① `favicon.py exclude --site <id> --apply`（台帳に除外の印だけ付ける。既定 dry-run。R2 には触れない）→ ② `sql --out f.sql`（除外サイトの NULL 化 UPDATE が出る）→ ③ f.sql を D1 へ適用 → ④ `delete --site <id> --apply --d1-done --r2-remote`（R2）。**D1（NULL 化）が先、R2 削除が後**（逆だと一覧が一瞬 404 になる）なので、R2 を消す `--apply`（`--store r2`）は `--d1-done`（③ の確認）が無いと実行しない。本番は `--r2-remote` も明示したときだけ（無ければ wrangler の `--local` 模擬）。CDN・ブラウザにキャッシュが残る間は表示され続けるが、immutable・1 年のため即時には消えない（許容。即時に消す必要があればキャッシュのパージを別途）。#29 の申請の受付フローとの接続は #29 側。

## 更新

- 次回確認は、成功・画像なしなら確認の 30 日後（月次）、取得失敗（接続・DNS・タイムアウト等）は 3 日後、robots 拒否・内部 IP などは 30 日後。
- 前回の画像 URL・`ETag`・`Last-Modified` を台帳に持ち、**ページを取り直さず画像へ条件付き GET**（`If-None-Match` / `If-Modified-Since`）。304 なら何もしない。200 でも**出力の内容ハッシュが前回と同じなら保存先に書かず**、`changed_at` も更新しない（内容が変わったときだけ同じキーへ上書きし、D1 の `favicon_v` も変わる）。画像 URL が 404 などになったらページから候補を探し直す。
- 失敗しても、画像が見つからなくなった（`none`）ときも、前回の画像は消さない（`favicon_path` を NULL に戻す SQL は出さない）。サイトの一時的な不具合で消えないことを優先する仕様で、**撤去されても古い絵は残る。消すのは取り下げ申請（#29）**。
- 保存先に書けない（ディスク満杯等）ときは、そのサイトだけ `store_failed` として記録して続行する。保存先のキーから画像が消えていたら（`--r2-verify`・ローカル）、内容が同じでも置き直し、304 の場合は条件なしで取り直す。
- 再巡回との連動: 対象は `scripts/recrawl` の台帳で `state='listed'` かつ `last_ok_at` があるサイトだけ（死んだ・要確認のサイトは取りに行かない）。

## 夜間ジョブ

`--favicon-limit N` はローカル取得用。再巡回台帳を取り込み、期限が来た N 件を `var/favicons/` に保存する。新規掲載分を R2 と D1 まで自動反映するときは `--media-favicon-limit N` を使う。どちらも既定は 0（無効）。設定と再開は [夜間ジョブの手順書](../operations/nightly.md#新規掲載の画像取得) を参照。

## 内容ハッシュ名からサイト固定キーへの移行（#163。一回きり）

既存の本番分（1,918 サイト・旧キー 1,878 枚）を新キーで上げ直して D1 を切り替え、旧画像を消す。`scripts/favicon/rekey.py`（テスト: `tests/test_favicon_rekey.py`）。入力は台帳（`--db` を台帳の数だけ繰り返す。この機は分割した 10 本）と内容ハッシュ名のローカル画像（`--store-dir`、既定 `var/favicons/`）で、サイトには一切アクセスしない。**既定はすべて dry-run。R2 に書く/消すのは `--apply`、本番は `--apply --r2-remote` を明示したときだけ。** 冪等（置いた印があれば飛ばす）で、途中で止まっても同じコマンドを再実行すればよい。失敗は `--retries` 回まで再試行して集計する（終了コード 1）。`--jobs`（既定 8）で並列。

| コマンド | 内容 |
|---|---|
| `plan` | サイト数・重複行・旧キー数・元画像の無い/壊れたものを表示（読み取りのみ） |
| `upload [--apply] [--r2-remote] [--no-local-copy]` | 各サイトの画像を新キーで R2 に置く。置いたら台帳に印を付け、手元の `--store-dir` にも新キーで写す（夜間ジョブが全件を取り直さないため）。元画像が無い/名前と中身のハッシュが合わないものは置かず報告 |
| `sql [--out f] [--revert]` | `UPDATE sites SET favicon_path='<k>.webp', favicon_v='<v>' WHERE id=… AND (favicon_path IS NOT '<k>.webp' OR favicon_v IS NOT '<v>')`（v が同じで path だけ旧名の行も直る）。`--revert` は戻し用（台帳の `icon_hash` の旧名を `favicon_path` に戻し `favicon_v` を NULL にする。旧画像が R2 に残るうちだけ有効）。**R2 に置いた印のあるサイトだけ**出す（D1 が指す先が無い状態を作らない） |
| `list-old [--out f]` | 削除対象の旧キー（台帳の `icon_hash` と、旧台帳が R2 本番に置いた名前の和集合） |
| `delete-old --d1-json f.json [--apply] [--r2-remote] [--d1-done] [--verify]` | 旧キーを消す。**`--d1-json`（D1 のスナップショット）が必須**: `npx wrangler d1 execute mise --remote --json --command "SELECT id,favicon_path,favicon_v FROM sites WHERE favicon_path IS NOT NULL" > f.json`。**D1 の `favicon_path` が今も指す旧名は絶対に消さない**（`favicon_v` が NULL の旧データ行が残っていればその名前も `kept`）。そのうえで、台帳で同じ内容を使っていたサイト（取得が一時失敗中のサイトも含め、status は問わない）が全員新キーに置いた印を持つ名前だけ消す。`--verify` は新キーの実在を R2 に `--jobs` 並列で問い合わせて確かめる（1 件数秒 ÷ 並列数。1,900 件で数分）。スナップショットが古い（24 時間超）・空・行数が台帳の画像ありのサイトの半分未満なら中止（終了コード 2）。残したものがあれば内訳を表示して終了コード 3。`--apply` は `--d1-done` も要る |

実行する順序（本番、詳細は [deploy.md](../operations/deploy.md#ファビコンのサイト固定キー化migration-0029本番への切替順序)）: migration 0029 → 新 Worker のデプロイ（`favicon_v` が NULL の旧データは従来の URL のまま動く）→ `upload --apply --r2-remote` → `sql` を D1 へ適用 → 配信確認 → D1 のスナップショットを取って `delete-old --d1-json f.json --apply --r2-remote --d1-done`（1 回だけ）。以降、夜間・月次の取得は新キーへ上書きする。

- **過渡期の注意**: 切替前の旧 URL（v なし）は immutable でなく `max-age=3600` になるので、切替が終わるまで Worker・R2 の呼び出しが少し増える（終われば全サイト v 付きの immutable）。
- **`upload --apply` は夜間の favicon・media 段（`nightly.py`）と重ねない**（同じ台帳 SQLite に書くのでロックで衝突する）。夜間の実行時間帯を避けるか、両方のファビコン取得上限が 0 の夜に行う。
