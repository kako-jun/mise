# ファビコンの取得と更新

Issue #125。**自前で取得し、32px の画像にして保存し、月 1 回更新する**。表示時に第三者のサイトへ問い合わせない（訪問者の IP・閲覧の事実・Referer を漏らさない。MISE の「人気や閲覧数を扱わない」方針）。**取れなければ何も出さない。代替アイコン（頭文字・猫模様など）は作らない**（kako-jun 決定 2026-11-18）。

## 実装の範囲

| 実装済み | 未実装（後続） |
|---|---|
| 取得・縮小・台帳・ローカル保存・`sites.favicon_path` 向け SQL・月次更新・条件付き GET・`nightly.py` の任意の段 | **R2 への配信**（サムネイル #41 と共用する別 Issue #130。配信ルート・CSP の `img-src`・キャッシュ設定）、既存全サイトの一括取得、取り下げ申請（#29）での削除、一覧の UI での表示 |

保存先は `IconStore`（`put` / `exists`）の抽象で、今回の実装はローカルディレクトリ `var/favicons/<hash>.webp` だけ。R2 実装は同じ形で足せる。**今は R2・本番 D1 のどちらにも書かない**。

## 使い方

実装は `scripts/favicon/favicon.py`、テストは `tests/test_favicon.py`。画像の検証・縮小に Pillow を使う（リポの依存には足さず、**必ず `uv run --with pillow python3` で起動する**）。Pillow が無いと `run --apply` は終了コード 3 で止まる（1 サイトの失敗として握りつぶさない）。`nightly.py` の `favicon` 段も同じ起動方法で呼ぶ。

```sh
uv run --with pillow python3 scripts/favicon/favicon.py import --recrawl var/recrawl.sqlite   # 再巡回台帳の listed かつ取得成功済みのサイトを対象へ(--sites JSON も可)
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50            # 計画だけ(ネットワークなし。台帳もディレクトリも作らない)
uv run --with pillow python3 scripts/favicon/favicon.py run --limit 50 --apply    # 期限が来たものを取得(実サイトにアクセスする)
uv run --with pillow python3 scripts/favicon/favicon.py sql --since 2026-11-20T00:00:00Z --out var/favicons.sql   # sites.favicon_path の UPDATE(適用は人が行う)
uv run --with pillow python3 scripts/favicon/favicon.py status
```

台帳は `var/favicons.sqlite`（`.gitignore`、再生成可能）。出力 SQL は `UPDATE sites SET favicon_path='<hash>.webp' WHERE id=... AND favicon_path IS NOT '<hash>.webp'`（値が同じなら書かない。D1 の書き込み行数を使わない）。取れていないサイトには触れない。`run`（`--apply` なし）・`sql`・`status` は台帳を読み取り専用で開き、無ければ何も作らない。

**`--since` の SQL を適用し忘れたとき**（夜間ジョブが作った `var/nightly/<date>/favicons.sql` を適用しないまま次の夜が来た等）は、`--since` なしで全件を出し直す: `sql --out var/favicons-all.sql`。`AND favicon_path IS NOT ...` があるので、すでに合っている行は書かれず、何度適用しても安全。migration は不要（`favicon_path` は 0002 から存在）。

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
- ファイル名は出力画像の SHA-256 の先頭 32 桁 + `.webp`。**同じ画像（既定アイコン等）は 1 ファイルに重複排除**される（`sites.favicon_path` が同じ名前を指す）。

## 更新

- 次回確認は、成功・画像なしなら確認の 30 日後（月次）、取得失敗（接続・DNS・タイムアウト等）は 3 日後、robots 拒否・内部 IP などは 30 日後。
- 前回の画像 URL・`ETag`・`Last-Modified` を台帳に持ち、**ページを取り直さず画像へ条件付き GET**（`If-None-Match` / `If-Modified-Since`）。304 なら何もしない。200 でも**出力のハッシュが前回と同じなら保存先に書かず**、`changed_at` も更新しない。画像 URL が 404 などになったらページから候補を探し直す。
- 失敗しても、画像が見つからなくなった（`none`）ときも、前回の画像は消さない（`favicon_path` を NULL に戻す SQL は出さない）。サイトの一時的な不具合で消えないことを優先する仕様で、**撤去されても古い絵は残る。消すのは取り下げ申請（#29）**。
- 保存先に書けない（ディスク満杯等）ときは、そのサイトだけ `store_failed` として記録して続行する。保存先から画像ファイルが消えていたら、ハッシュが同じでも置き直し、304 の場合は条件なしで取り直す。
- 再巡回との連動: 対象は `scripts/recrawl` の台帳で `state='listed'` かつ `last_ok_at` があるサイトだけ（死んだ・要確認のサイトは取りに行かない）。

## 夜間ジョブ

`scripts/nightly.py` の最終段 `favicon`。**既定は無効**（`--favicon-limit 0`）。`--favicon-limit N` を付けたときだけ、再巡回台帳の取り込み → 期限が来た N 件の取得 → `var/nightly/<date>/favicons.sql` の生成までを行う。本番 D1 には触れず（SQL の適用は人）、段が失敗しても夜間ジョブ全体は失敗にしない（guards に残す）。実サイトにアクセスするので、有効にするのは最初は少数で様子を見てから。
