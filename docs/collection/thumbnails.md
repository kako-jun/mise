# サムネイルの撮影・配信（Issue #41 段階3・#130）

サイトのカード（縦フィード）に出す縮小画像を、巡回と同じ作法で**事前に撮り、R2 から配信する**。閲覧のたびには撮らない（#41 方針コメント 2026-11-18。都度撮影は小さな個人サイトへの負荷・速度・SSRF の入口になる）。**取れなければ何も出さない。代替画像は作らない**（ファビコンと同じ）。

## 実装の範囲

| 実装済み（#130 第1段） | 未実装（後続） |
|---|---|
| 量産ジョブ `scripts/thumbnail/`（台帳・キュー・1 日の上限・除外・保存先の抽象・`sql`・`upload`）、配信ルート `/thumb/<hash>.webp`、API の `thumb_url` / `thumb_lqip`、migration 0028（**本番未適用**）、20 サイトの試行（ローカル保存のみ） | 全件の撮影、R2 本番への投入と D1 への反映（[deploy.md](../operations/deploy.md)）、更新検知・閲覧トリガの再撮影、成人向け属性による自動除外、取り下げ申請（#29）での R2 削除、夜間ジョブ（`nightly.py`）への組み込み、カード側の見た目（#52・#160） |

## 設計（決定）

- 撮るのは**PC 画面（viewport 1280x800）の最初の画面を、横長（16:10）のまま幅 320px に縮めた 1 枚**。スマホ・PC の両方でこの 1 枚を使い回す（スマホ版は持たない）。カード側は縦長カードの上部にこの画像を置き、同じ画像を強く拡大・ぼかしてカード全体の背景に敷く。
- 形式は WebP（画質 40）、ファイル名は**出力画像の SHA-256 先頭 32 桁 + `.webp`**（同じ画像は 1 ファイルに重複排除）。あわせて**極小プレースホルダ（LQIP）**: 16x10 の WebP を base64 にした `data:image/webp;base64,...`（約 130 文字、上限 180。収まらなければ段階的に小さく・粗くする。外部依存なし）。
- **D1 に持つのは表示に要るものだけ**: `sites.thumb_path`（ハッシュ名）・`thumb_lqip`・`thumb_captured_at`（[migration 0028](../design/data-model.md)）。**撮影の状態（成否・失敗の回数・期限・obscured・除外）は手元の台帳 `var/thumbnails.sqlite` に持つ**（ファビコンと同じ。D1 の書き込み行数を使わない）。
- 置き場は R2 の `thumb/<hash>.webp`（バケット `mise-media`、ファビコンと共用）。配信は Worker の `/thumb/<hash>.webp`（`src/media.js`。`<hash>` は 32 桁 16 進だけ許可。immutable・1 年・`ETag`/304・無ければ 404 `no-store`。共有キャッシュ［`caches.default`］には入れない）。API の site 項目は `thumb_url`（`/thumb/<hash>.webp`。`thumb_path` の形が違えば null）と `thumb_lqip`（画像が無ければ null。形が違う値も null）を返す。`thumb_path` 自体は返さない。
- 取り下げ申請（#29）が通ったら R2 の画像も消す（ファビコンと同じ設計。未実装。画像は重複排除されるので、他サイトが同じ名前を指していないことを確かめてから消す）。
- **成人向け**: `sites` に成人向けの属性が**まだ無い**ため、自動では除外できない。台帳の `exclude` フラグ（`import --exclude`）で**手動で**除外する。除外は撮らず、`sql` にも出さない。**撮った画像の内容を自分で性的・不適切と判定しない**（除外は属性・手動指定だけ）。属性ができたら撮影前の除外をここに足す。

## 使い方

実装は `scripts/thumbnail/thumbnail.py`（台帳・キュー・robots・保存・SQL）と `scripts/thumbnail/capture.mjs`（ブラウザの中で守ること。1 サイトにつき 1 プロセス）、テストは `tests/test_thumbnail.py`・`tests/thumb-migration.test.mjs`。画像の縮小に Pillow を使う（リポの依存には足さず、**必ず `uv run --with pillow python3` で起動する**）。Chromium 撮影には playwright（リポには入れない。環境変数 `PLAYWRIGHT_DIR` で `node_modules/playwright` を指す）と chrome-headless-shell（`~/.cache/ms-playwright` の最新、`CHROMIUM_PATH` で上書き）、`bwrap` が要る。

```sh
uv run --with pillow python3 scripts/thumbnail/thumbnail.py import --sites sites.json              # [{id,url}] の JSON を台帳へ(状態は保つ)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py import --exclude id1,id2               # 手動の除外(id のカンマ区切り、または id の JSON ファイル。--unexclude で解除)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py run --limit 50                         # 計画だけ(ネットワークなし。台帳もディレクトリも作らない)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py run --limit 50 --apply                 # 撮る(実サイトにアクセスする。保存先は var/thumbnails/)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py run --limit 50 --apply --store r2       # R2 のローカル模擬へ(wrangler dev --local と同じ .wrangler/state)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py upload --apply                         # ローカルに撮った画像を R2(模擬)へ置く。サイトにはアクセスしない
uv run --with pillow python3 scripts/thumbnail/thumbnail.py sql --out var/thumbnails.sql           # sites の UPDATE(適用は人。既定は --store r2-remote の印がある画像だけ)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py status
```

- 起動時のオプション: `--limit N`（1 回の上限）、`--per-day N`（1 日［UTC］の試行数の上限、既定 **3,000**。成功・失敗とも数え、台帳の `day_counts` に持つ）、`--concurrency N`（同時に撮る数、既定 **2**。同じホストは常に直列）、`--db`・`--store-dir`（試行用に場所を替える）、`--no-bwrap`（GPU で固まらない環境）。
- **ブラウザ側の自己検査**（実サイトにはアクセスしない。ローカルの小さなサーバーの `*.localhost` だけが相手）: `bwrap --dev-bind / / --tmpfs /dev/dri node scripts/thumbnail/selftest.mjs`（要 playwright・chrome-headless-shell）。別サイト・リテラル IP への 302／meta refresh／JS の遷移、同一サイト内の origin 変更（許可なし・あり）、noindex の meta・X-Robots-Tag・404、ダイアログ、ダウンロード、リソース上限（400 枚のうち 150 件で止まる）、WebSocket、内部 IP のサブリソースが期待どおりで、サーバーに届いてはいけないリクエストが来ていないことを確かめる。
- **既定は dry-run**。`--apply` を付けたときだけ実サイトにアクセスする。R2 へ書くのは `--store r2`（または `upload`）を明示したときだけで、**本番バケットへは `--r2-remote` を明示したときだけ**（`--r2-remote` が無ければ常に `--local`）。この段の作業では `--r2-remote` を実行していない。
- 台帳（`var/thumbnails.sqlite`、`.gitignore`、再生成可能）は 1 サイト 1 行: `site_id`・`url`・`state`（`pending`/`ok`/`failed`）・`last_error`・`http_status`・`final_url`・`thumb_hash`・`lqip`・`bytes`・`obscured`/`obscured_ratio`・`exclude`/`exclude_reason`・`consecutive_failures`・`captured_at`・`changed_at`・`last_checked_at`・`next_check_at`。保存先ごとの「置いた印」は `thumb_stored`（画像名 × `local`/`r2-local`/`r2-remote`）。
- **`sql` の出力**: `UPDATE sites SET thumb_path='<hash>.webp', thumb_lqip='...', thumb_captured_at='...' WHERE id=... AND thumb_path IS NOT '<hash>.webp';`（`thumb_path` が同じなら書かない）。取れていないサイト・除外のサイトには触れない。**既定は `--store r2-remote` の印がある画像だけ**（ローカルにしか無い画像を D1 が指す状態を作らない）。`--since` は `changed_at`（画像が変わった日）基準。

## キュー（再撮影）

- 対象は台帳で `exclude=0` かつ `next_check_at` が来た（または未撮影の）サイト。**未撮影を先、次に期限の古い順**。`--limit` と、その日の残り枠（`--per-day` − 今日の試行数）の小さい方だけ撮る。
- 次回: 成功は撮影の **30 日後（月次）**。失敗は恒久的なもの（robots 不許可・内部 IP・別サイトへのリダイレクト・`noindex` 等・HTTP 4xx・白紙・壊れた画像）が 30 日後、一時的なもの（タイムアウト・5xx・429・robots 取得不能・ブラウザの異常）は **3 日後**で、連続 3 回失敗したら 30 日後にする。
- **失敗しても前回の画像は消さない**（`thumb_hash` は残り、`sql` にも出る。一時的な不具合で絵が消えないことを優先。消すのは取り下げ申請）。撮り直して画像が同じなら保存先に書かず `changed_at` も更新しない。
- 更新検知（`source_updated_at`・ETag）による優先再撮影、閲覧トリガ（#130 本文の 4 項）は**後続**。今回は期限だけ。キューの消化は手元ジョブのみで、公開サーバーから外部へ撮影要求は送らない。

## 作法（量産ジョブ）

- UA は `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`。**robots.txt は発見パイプラインの `fetcher.RobotsCache` を再利用**（ファビコンと同じ。200 で不許可なら撮らない、404/410 は規則なし、401/403 は拒否、5xx・取得失敗は撮らない）。**同一ホストは 2 秒以上**（robots 取得・ページ読み込みの前に待つ）。1 サイト 1 回（1 サイトの撮影は 1 プロセス・毎回まっさらなブラウザ）。
- **SSRF**: 撮影前に名前解決して内部・ループバック・リンクローカル等なら撮らない（`fetcher.check_public_host`）。ポートは 80/443 のみ。ブラウザ側は CDP の Fetch で全リクエストを**送信前に止めて検査**し、内部 IP（名前解決の結果を含む）へのサブリソース・主フレームの遷移は送らない。WebSocket は無効化。
- **リダイレクト**: 主フレームの遷移は撮影開始時に許可した origin だけ。別サイトは `cross-site-redirect`、同一サイトで origin が変わるもの（`www` の有無・http→https）は、遷移先へのリクエストを**送る前に**止めて `origin-changed:<origin>` とし、Python 側が遷移先の robots・SSRF を確かめてから最大 2 回だけやり直す（試験用スクリプトのように遷移先 robots を確かめる。最初の試行はリクエストを送らず止まるので、サイトの本体を読むのは最後の 1 回だけ）。
- **ブラウザの中**: cookie・ログインなし（まっさらなコンテキスト）、JS ダイアログ（alert/confirm/prompt/beforeunload）は拒否、ダウンロードは拒否、新しいウィンドウは閉じる、権限は与えない、Service Worker 不可。リソース上限（1 ページ 150 リクエスト・12MB を超えたら以降のリクエストを止め、読めた分で撮る）。ナビゲーションのタイムアウト 20 秒＋描画待ち 1.5 秒、1 サイトの全体は 90 秒で強制終了（自分が起動したプロセスグループだけ）。
- `X-Robots-Tag` と `<meta name="robots">`（`MiseResearchBot` 指定も）の `noarchive`・`noimageindex`・`noindex`・`none` は撮らない。obscured（画面の過半を fixed/sticky 要素が覆う＝cookie 同意バナー・全面モーダルの疑い）は台帳に記録するだけで撮り直さない。
- **白紙・無地**（画素のばらつきがほぼ 0）は失敗（`blank`）にして画像を出さない。HTTP 4xx/5xx の応答（エラーページ）も撮らない。
- **Chromium の置き場**: この環境の headless chromium は GPU デバイスを開く所（drm_open）で固まるため、`bwrap --dev-bind / / --tmpfs /dev/dri` で包んで起動する（`--disable-gpu` では直らない）。手元 PC か Pi の常駐ジョブを想定。Chromium は 1 サイトにつき 1 回起動・終了する（同時 2。メモリ使用量は未計測。本格運用前に測る）。

## 量産の見積もり

- **R2 保存**: 試験 37 枚と試行 20 枚の平均は約 5〜6KB（最大 14KB）。10 万枚でも約 0.5〜1GB。余裕を見て 1 枚 20〜40KB（画質を上げた場合）としても 2〜4GB で、**無料枠（10GB）に収まる**。
- **書き込み系操作（Class A）**: 無料枠は月 100 万。月次で 10 万枚を撮り直すと put が最大 10 万回／月（実際は同じ画像なら put しない）で、枠の 1 割。**読み出し（Class B）は月 1,000 万回**で、配信は immutable・1 年キャッシュ＋CDN なので初回だけが R2 に届く。
- **1 日の枚数**: 10 万件 ÷ 30 日 ≒ **3,300 枚／日**。`--per-day` 既定 3,000 は月 9 万枚で、全件を月 1 回回すには足りない（10 万件になったら 3,500 以上にする）。同時 2・1 枚あたり実測 約 5 秒（下記）なら 1 日 3,300 枚は約 4.6 時間。
- **試行の所要時間（同時 2、20 サイト）**: 1 サイトあたり 実時間 約 5 秒（robots 取得と待ち 2 秒×2、Chromium の起動・読み込み・描画待ち 約 5 秒を同時 2 で重ねた値）。現在の掲載 約 2,200 件の全件撮影は **約 3〜3.5 時間**（失敗の再試行は別）。
- D1 への反映は `UPDATE` 1 サイト 1 行（`thumb_path` が変わったときだけ）。10 万件の初回で 10 万行の書き込み（D1 の書き込み枠 月 5,000 万行の 0.2%）。

## 試行（2026-11-19、20 サイト）

本番 D1 から読み取りだけ（SELECT）で選んだ 20 サイト（官公庁 `pb-` 5・個人/団体 10・企業/店 5）。**ローカル保存のみ**（R2・D1 には書いていない）。成功 20/20（初回は 13 件。タイムアウト 1 件［20 秒］と、robots 取得が不調になった 6 件［`robots_unavailable`。5xx 等を一時失敗として 3 日後に再試行する経路］は台帳の期限を戻して撮り直し、全て成功）。obscured は 0 件（最大比率 0.18）。サイズは 2.0〜13.8KB（平均 約 5.6KB）、LQIP は 95〜179 文字（平均 約 137）。目視（contact sheet）では、歪み・真っ白・クッキー同意バナーだけの画面は無し。スライドショーの切替の途中らしい薄い画面（王子駅前サンスクエア）と、導入アニメーションの途中らしい画面（ピアノヴィレッジ）が各 1 件あった（描画待ちが固定時間のため。下の「限界」）。

## 結果（試験: 2026-11-13、37件）

- 成功 37/37。失敗0（robots不許可・noindex類・タイムアウト・リダイレクト逸脱は0件で、これらの分岐が実サイトで働いた例は無い）。リダイレクトの検査（別サイト・内部IP・同一サイト内の遷移先 robots）は自己検査で確認済み。
- obscured は1件（nutritionfacts.org。全面のモーダル）。比率0.15〜0.4で覆われているが閾値未満のものが3件（NDLのcookieバナーなど）。
- 1枚あたり（幅320・画質40）: 平均約5.0KB、中央値4.9KB、最小0.9KB、最大8.6KB。37枚で約180KB。所要時間は1サイトあたり平均6.5秒、最大9.6秒（robots取得・待機2回の4秒を含む）。プレースホルダは平均128文字（最大175）。
- 見た目: 暗い背景・小さい文字のサイトでも、サイトの雰囲気（配色・ロゴ・写真）は判別できる。本文は読めないが、カード背景にぼかして使う前提なら問題ない。

### 幅の比較（240/320/400、カードいっぱいに拡大して目視）

| 幅 | 平均 | 最大 | 見た目 |
|---|---|---|---|
| 240 | 3.0KB | 5.4KB | 拡大すると細部が崩れるが、ぼかし背景なら十分 |
| 320 | 5.0KB | 8.6KB | 小さい見出しがかろうじて読める。拡大してもブロックノイズは目立たない |
| 400 | 7.5KB | 13.2KB | 320との差は小さい |

採用は**320・画質40**。理由: 文字に重ねる前提で細部は不要、240は拡大時の崩れが気になり始める、400は容量に見合う差が無い。10〜20KBという目標より大幅に小さいので、細部が欲しくなったら画質を上げる余地がある（`THUMB_W`・`WEBP_QUALITY`。その場合の容量は未計測）。

### スマホ画面（390x844）との比較（12サイト、参考）

- 方法: PC画面を撮った後、同じページの viewport だけを390x844にして再描画して撮る（モバイルUAは使わない）。幅195pxで保存。
- (1) 縦長カードに収まる見た目: レスポンシブなサイト（結城浩、kottke、国立国会図書館など）は自然に縦長で収まる。主の横長画像より情報量が多い。
- (2) 非レスポンシブなサイトの崩れ: 12件中6件（sample-5 の青空文庫、ELM、玄松子、古墳マップ、ぼうずコンニャク、amasci）で横幅が390を超え（615〜1000px）、右側が切れる、極小の文字になる、などの崩れが出た。
- (3) 容量: 12件でPC版の平均約5.5KBに対し、スマホ版は平均約9.2KB。2枚持つと約2.7倍になる。
- 推奨: **主の1枚（PC・横長）だけ**を持つ。スマホ版は崩れるサイトが半数あり、容量も増え、撮影の安定性も下がる。カード側の縦長への適応はぼかし背景の拡大で担う。

## 試験用スクリプト（`scripts/capture-thumbnails.mjs`）

量産ジョブの前に作った試験用（Node 単体・ImageMagick 使用・固定の 37 件）。設計の根拠と幅の比較に使った。量産は上の `scripts/thumbnail/` を使う。

```
node scripts/capture-thumbnails.mjs [--only id,id] [--limit N] [--out var/thumbs]
  [--width 320] [--quality 40] [--compare 240,320,400] [--mobile id,id|all]
```

- 対象は `data/embeddability.json` の25件と `migrations/0014_us_batch1.sql` の12件（計37件）。画像実体と `manifest.json` は `var/thumbs/`（`.gitignore`）。
- 自己検査: `node scripts/capture-thumbnails.mjs --selftest`（ローカルの小さなサーバーだけを相手にする。別サイト・内部IP・同一サイト内の `www` への遷移・meta refresh・JS の遷移・WebSocket・遷移先 robots 不許可を確かめる）。量産の `capture.mjs` の検査は同じ方式を踏襲している。
- 取得作法の詳細（試験時）: ナビゲーションのタイムアウト20秒、読み込み後1.5秒待つ、リダイレクトは同一サイトのみ、obscured は24x18の格子で最前面が fixed/sticky の割合、`X-Robots-Tag`/meta robots の扱い。量産ジョブも同じ値を使う。

## 限界

- 撮影は 37 件＋20 件のみ。大手・企業サイト・動的なサイトでは取得失敗やバナーの比率が上がるはず。
- 描画待ちは固定時間のため、遅延読み込みの画像・アニメーション・スライドショーの途中が写ることがある（試行で 2/20）。撮り直しはしない（月次で次の機会に替わる）。
- 内部IPの検査は撮影前の名前解決の結果で行う。検査後にDNSの答えが変わる（DNS rebinding）場合までは防げない。プロキシ経由では実際の名前解決はプロキシ側で行われるため、手元での検査結果と接続先が食い違うことがありうる。Web Worker 内の WebSocket は塞いでいない。
- noarchive等の判定は、読み込んだページの最初の応答ヘッダとトップページの meta のみ。
- 成人向けの自動除外は無い（上の「設計」）。
- 画像を使うことの著作権上の整理（サイト運営者の許諾・取り下げ運用）は #29 で扱う。試験の画像は公開していない。
