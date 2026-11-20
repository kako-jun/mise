# サムネイルの撮影・配信（Issue #41 段階3・#130）

サイトのカード（縦フィード）に出す縮小画像を、巡回と同じ作法で**事前に撮り、R2 から配信する**。閲覧のたびには撮らない（#41 方針コメント 2026-11-18。都度撮影は小さな個人サイトへの負荷・速度・SSRF の入口になる）。**取れなければ何も出さない。代替画像は作らない**（ファビコンと同じ）。**1 サイトに許すサムネイルは 1 枚だけ**（世代管理なし。撮り直しは同じキーへの上書きで、古い画像は R2 に残らない）。

## 実装の範囲

| 実装済み（#130 第1段） | 未実装（後続） |
|---|---|
| 量産ジョブ `scripts/thumbnail/`（台帳・キュー・1 日の上限・除外・`sql`・`upload`・`delete`）、検査付きフィルタリングプロキシ（`netfilter.py`）とサンドボックス（bwrap）での撮影、配信ルート `/thumb/<k>.webp`、API の `thumb_url` / `thumb_lqip`、migration 0028、デプロイ前の適用確認（`scripts/check-migrations-applied.mjs`）、[夜間ジョブでの新規掲載分の取得・配信](../operations/nightly.md#新規掲載の画像取得) | 全件の撮影、更新検知・閲覧トリガの再撮影、既掲載サイトの成人向け属性による自動除外、取り下げ申請（#29）の画面・API からの `delete` 呼び出し、カード側の見た目（#52・#160）、ファビコンの孤児掃除（#163） |

## 設計（決定）

- 撮るのは**PC 画面（viewport 1280x800）の最初の画面を、横長（16:10）のまま幅 320px に縮めた 1 枚**。スマホ・PC の両方でこの 1 枚を使い回す（スマホ版は持たない）。カード側は縦長カードの上部にこの画像を置き、同じ画像を強く拡大・ぼかしてカード全体の背景に敷く。
- 形式は WebP（画質 40）。あわせて**極小プレースホルダ（LQIP）**: 16x10 の WebP を base64 にした `data:image/webp;base64,...`（約 130 文字、上限 180。収まらなければ段階的に小さく・粗くする。外部依存なし）。
- **キーはサイト固定**: R2 の `thumb/<k>.webp`、`<k>` = `sha256(site_id)` の先頭 32 桁 16 進（例: `sha256("collected-genbu")` → `thumb/2ba21f05….webp`。サイト ID から決定的に計算できる）。**撮り直しは同じキーへ上書き**で、古い画像は残らない（1 サイト 1 枚・世代管理なし。R2 の掃除は要らない）。内容ハッシュによる共有・重複排除はしない。
  - **根拠**: サムネイルは他サイトと同じ画像にならないので、共有の利点が無い。ファビコンも同じ規則にそろえた（#163。共有の利点は実測でほぼ無かった。[favicons.md](favicons.md)）。サイト固定キーなら、取り下げ・除外でサイトを外すときも、サイト ID からキーが決まるので**他サイトの参照確認なしで R2 から 1 本消せる**（`thumbnail.py delete`）。
  - **画像の内容ハッシュ**（`thumbs.thumb_hash`）は保存先のキーとは別に、台帳で**変更検知用**にだけ持つ（同じ内容なら R2 へ書き直さず `changed_at` も更新しない）。
- **D1 に持つのは表示に要るものだけ**（[migration 0028](../design/data-model.md)）: `sites.thumb_path` = `<k>.webp`（サイト固定。撮り直しで値は変わらない）、`thumb_lqip`、`thumb_captured_at`（撮影時刻。**表示のバージョン**）。**撮影の状態（成否・失敗の回数・期限・obscured・除外）は手元の台帳 `var/thumbnails.sqlite` に持つ**（ファビコンと同じ。D1 の書き込み行数を使わない）。例: `thumb_path='2ba21f054c932a5e914061b3d78fd45a.webp'`、`thumb_captured_at='2026-11-19T06:49:13Z'`。
- **配信**: Worker の `/thumb/<k>.webp?v=<v>`（`src/media.js`）。API の site 項目 `thumb_url` は `/thumb/<k>.webp?v=<撮影時刻のエポック秒の 36 進>`（例: `/thumb/2ba21f054c932a5e914061b3d78fd45a.webp?v=toqlm1`。撮影時刻が読めなければ `?v` なし）。`thumb_path`・`thumb_captured_at` 自体は API に出さない。`<k>` は 32 桁 16 進だけ許可（それ以外は R2 を見ずに 404）、**Worker は query を無視して R2 を引く**。応答は `v` が正しい形（英数字 12 文字まで）なら `Cache-Control: public, max-age=31536000, immutable`（URL ごとに不変の扱い。撮り直すと `?v` が変わり、ブラウザは新しい画像を取る）、`v` 無し・不正な `v` でも 404 にならず 200 で、短いキャッシュ（`max-age=3600`）。`ETag`/304 は R2 のものを維持。無ければ 404 `no-store`。共有キャッシュ（`caches.default`。`src/usage.js` の `isCacheable`）には入れない（query の有無にかかわらず `/thumb/` を除外）。縦フィードの `thumbOf` は `?v=` 付きの同一オリジンの絶対パスを通す（`tests/feed-view.test.mjs`）。
- **取り下げ（#29）・除外**: **アップロード済みのサイトの除外には `delete` が必須**（除外しても R2 の `/thumb/<k>.webp` は公開で取れたまま残る。`import --exclude` が R2 に置いた印のあるサイトを見つけたら警告し、`--delete-uploaded` で delete の dry-run のコマンドを案内する）。**手順はこの順**: ① `import --exclude` → ② `sql`（D1 の thumb 列を NULL にする）を適用 → ③ `thumbnail.py delete --site <id> --apply --d1-done`（R2 から消す。D1 が先、R2 が後。`--d1-done` は ② を済ませた確認で、無いと `--apply` は R2 に触れず止まる）。`delete` は既定 dry-run で、**本番 R2 へは `--r2-remote` を明示したときだけ**。キーがサイト ID から決まるので 1 本だけ消し、台帳ではそのサイトを除外にして `thumb_hash`・`lqip` を空にする。除外サイトには `sql` が `UPDATE sites SET thumb_path=NULL, thumb_lqip=NULL, thumb_captured_at=NULL WHERE id=… AND thumb_path IS NOT NULL` を出す（**除外が成人向けの唯一のフィルタ**なので、撮影後に除外したサイトの表示も D1 から消える。何度流しても安全）。順序は D1（NULL 化）→ R2 削除（逆だと一覧が一瞬 404 になる）。取り下げ申請の画面・API からの呼び出しは別 Issue（#29）。
- **成人向け**: 夜間ジョブは出荷時の判定台帳で `adult=yes` のサイトを撮影キューから除外する。`sites` に成人向けの属性は無いため、それ以外は台帳の `exclude` フラグ（`import --exclude`）で手動で除外する。除外は撮らず、手動の `sql` は thumb 列を NULL にする。除外は台帳の属性・手動指定に基づく。

## 使い方

実装は `scripts/thumbnail/thumbnail.py`（台帳・キュー・robots・保存・SQL）・`netfilter.py`（検査付きフィルタリングプロキシ）・`capture.mjs`（ブラウザの中で守ること。1 サイトにつき 1 プロセス）・`selftest.py`（実 Chromium での自己検査）、テストは `tests/test_thumbnail.py`・`tests/test_netfilter.py`・`tests/thumb-migration.test.mjs`。画像の縮小に Pillow を使う（リポの依存には足さず、**必ず `uv run --with pillow python3` で起動する**）。撮影には playwright（リポには入れない。環境変数 `PLAYWRIGHT_DIR` で `node_modules/playwright` を指す）、chrome-headless-shell（`~/.cache/ms-playwright` の最新、`CHROMIUM_PATH` で上書き）、`bwrap`、（任意）`systemd-run --user` が要る。

```sh
uv run --with pillow python3 scripts/thumbnail/thumbnail.py import --sites sites.json              # [{id,url}] の JSON を台帳へ(状態は保つ)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py import --exclude id1,id2               # 手動の除外(id のカンマ区切り、または id の JSON ファイル。--unexclude で解除。台帳に無い id は警告)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py run --limit 50                         # 計画だけ(ネットワークなし。台帳もディレクトリも作らない)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py run --limit 50 --apply                 # 撮る(実サイトにアクセスする。保存先は var/thumbnails/)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py run --limit 50 --apply --store r2       # R2 のローカル模擬へ(wrangler dev --local と同じ .wrangler/state)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py upload --apply                         # ローカルに撮った画像を R2(模擬)へ置く(同じキーへ上書き)。サイトにはアクセスしない
uv run --with pillow python3 scripts/thumbnail/thumbnail.py sql --out var/thumbnails.sql           # sites の UPDATE(適用は人。既定は --store r2-remote の印がある画像だけ)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py delete --site id1 --site id2           # 取り下げ・除外: R2 から 1 本ずつ消す(既定 dry-run。--apply、本番は --r2-remote)
uv run --with pillow python3 scripts/thumbnail/thumbnail.py status
PLAYWRIGHT_DIR=... uv run --with pillow python3 scripts/thumbnail/selftest.py                      # 実サイトにアクセスしない自己検査(攻撃ページ・遷移・上限・サンドボックスの隔離)
```

- オプション: `--limit N`（1 回の上限）、`--per-day N`（1 日［UTC］の試行数の上限、既定 **3,000**。成功・失敗とも数え、台帳の `day_counts` に持つ）、`--concurrency N`（同時に撮る数、既定 **2**。同じサイトは常に直列）、`--db`・`--store-dir`（試行用に場所を替える）、`--no-bwrap`（**サンドボックスなし**。信頼できる環境・テスト用。既定は使う）、`--no-limits`（メモリ・CPU の上限なし。上限を掛けられない環境でだけ明示）。
- **既定は dry-run**。`--apply` を付けたときだけ実サイトにアクセスする。R2 へ書くのは `--store r2`（または `upload`）を明示したときだけで、**本番バケットへは `--r2-remote` を明示したときだけ**（`--r2-remote` が無ければ常に `--local`）。この段の作業では `--r2-remote` を実行していない。
- 台帳（`var/thumbnails.sqlite`、`.gitignore`、再生成可能）は 1 サイト 1 行: `site_id`・`url`・`state`（`pending`/`ok`/`failed`）・`last_error`（失敗コード。`robots_unavailable` には HTTP ステータスと上流プロキシの有無が付く）・`http_status`・`final_url`・`thumb_hash`（画像の内容ハッシュ。変更検知用）・`lqip`・`bytes`・`obscured`/`obscured_ratio`・`exclude`/`exclude_reason`・`consecutive_failures`・`captured_at`・`changed_at`・`last_checked_at`・`next_check_at`。保存先ごとの「置いた印」は `thumb_stored`（キー × `local`/`r2-local`/`r2-remote` → 置いた内容。キーごとに最後の内容 1 つ）。
- **`sql` の出力**: `UPDATE sites SET thumb_path='<k>.webp', thumb_lqip='...', thumb_captured_at='<撮影時刻>' WHERE id=... AND thumb_captured_at IS NOT '<撮影時刻>';`。**`thumb_path` は撮り直しでも変わらないので、冪等判定は `thumb_captured_at` の変化**（同じ撮影時刻なら書かない。撮り直すと `?v` が変わり、ブラウザが新しい画像を取る）。取れていないサイトには触れない。**既定は `--store r2-remote` の印があり、その内容が現在の内容と一致する画像だけ**（R2 が古い内容のまま D1 だけ新しくならない）。`--since` は撮影時刻基準。

## キュー（再撮影）

- 対象は台帳で `exclude=0` かつ `next_check_at` が来た（または未撮影の）サイト。**未撮影を先、次に期限の古い順**。`--limit` と、その日の残り枠（`--per-day` − 今日の試行数）の小さい方だけ撮る。
- 次回: 成功は撮影の **30 日後（月次）**。失敗は恒久的なもの（robots 不許可・内部 IP・別サイトへのリダイレクト・`noindex` 等・HTTP 4xx・白紙・壊れた画像・受信量の上限超過・上流プロキシ環境で IP を固定できない平文 HTTP［`upstream_dns_unpinned`］）が 30 日後、一時的なもの（タイムアウト・5xx・429・robots 取得不能・ブラウザの異常）は **3 日後**で、連続 3 回失敗したら 30 日後にする。
- **失敗しても前回の画像は消さない**（`thumb_hash` は残り、`sql` にも出る。一時的な不具合で絵が消えないことを優先。消すのは取り下げ・除外）。撮り直して画像が同じなら保存先に書かず `changed_at` も更新しない（`captured_at` は更新する）。
- 更新検知（`source_updated_at`・ETag）による優先再撮影、閲覧トリガ（#130 本文の 4 項）は**後続**。今回は期限だけ。キューの消化は手元ジョブのみで、公開サーバーから外部へ撮影要求は送らない。

## 作法（量産ジョブ）

任意の外部サイトを開く仕組みなので、**つぎはぎの検査ではなく、構造で出口を 1 つにする**。

- UA は `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`。**robots.txt は発見パイプラインの `fetcher.RobotsCache` を再利用**（ファビコンと同じ。200 で不許可なら撮らない、404/410 は規則なし、401/403 は拒否、5xx・取得失敗は撮らない）。**Crawl-delay を尊重する**（`fetcher` の上限 10 秒、さらに 30 秒で打ち切り）。**同一ホストは 2 秒以上**（robots 取得・ページ読み込みの前に待つ）。待ちと直列化のロックは「サイト（`www.` を除いたホスト名）」ごとで、別サイトは同時に進む。1 サイト 1 回（1 サイトの撮影は 1 プロセス・毎回まっさらなブラウザ）。
- **経路（出口は検査付きフィルタリングプロキシだけ）**: ブラウザの**全通信**（ワーカー・SharedWorker・ポップアップ・iframe/OOPIF・WebSocket・EventSource・sendBeacon・prefetch を含む）は、撮影ごとに起動する `netfilter.FilterProxy`（Python 標準ライブラリのみ。CONNECT と平文 HTTP）を通る。Chromium は `--proxy-server` だけが出口で、`--proxy-bypass-list=<-loopback>`（ループバック宛ても迂回させない）、`--host-resolver-rules=MAP * ~NOTFOUND`（ブラウザ自身は名前解決しない）、`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`（WebRTC の直接 UDP なし）、`--disable-quic`、`--no-pings`、`chromiumSandbox: true`（`--no-sandbox` なし）。
- **フィルタの検査**（CONNECT の宛先も平文 HTTP の宛先も同じ）: ポートは **80/443 だけ**（サブリソースも）。宛先ホストを自分で名前解決し、**全アドレスが公開アドレス**であること。**引けない名前は拒否（fail closed）**、IP リテラル（`[::1]`・IPv4 射影・NAT64・6to4・IPv4 互換を含む）は直接検査。**アドレス判定の表は `scripts/pipeline/netaddr.py` の 1 か所**（`netfilter` も、robots 取得に使う `fetcher.check_public_host` も同じ表。Python の `is_global` に揃え、`is_global` が真でも通さない IPv4 互換の IPv6〔`::/96`・`64:ff9b:1::/48` 等〕とマルチキャストを加える。NAT64（`64:ff9b::/96`）・6to4（`2002::/16`）は埋め込まれた IPv4 を取り出して検査し、公開 IPv4 のときだけ通す（`ipv4_mapped` と同じ扱い。DNS64/NAT64 網で全ホストが拒否されないように）。ゾーン付き（`8.8.8.8%x`）は拒否。Node 側には判定を持たない。入力表は `tests/test_netfilter.py`）。上流プロキシが無い環境では**検査した IP に固定して接続**（DNS リバインディングも塞ぐ。上流がある環境は次項）。1 本の接続につき 1 リクエスト（`Connection: close`）で、同じ接続で別ホスト宛の 2 本目を通さない。
- **上流プロキシ環境（この環境。本番の撮影もここで回す）**: 環境変数 `http_proxy`/`https_proxy` がある環境では、フィルタはそこへ連鎖する。
  - **HTTPS（CONNECT）は検査した IP に固定する**: `CONNECT <検査済みの IP>:443`（IPv6 は `[ip]:443`）を上流へ送る（名前解決を上流に渡さない。複数 IP は順に試す。TLS の SNI・証明書検証はトンネルの中で Chromium がホスト名で行う）。**名前の CONNECT へのフォールバックは試行ごとにはしない**（攻撃者が 5xx を強制できるため。IP の CONNECT が失敗したらその宛先は失敗）。代わりに**ジョブの開始時に 1 回、上流が IP 宛て CONNECT を受け付けるかをプローブで確かめる**: 公開 IP リテラル（既定 `1.1.1.1:443`。`--upstream-probe` で変更可）への CONNECT の応答コードだけを見て（データは送らない。実在の特定サイトに依存しない）、受け付けない上流では**終了コード 3 で止まる**（理由を表示）。名前の CONNECT を許すのは明示フラグ `--allow-unpinned-upstream` のときだけで、その接続は名前解決が上流＝固定できず、台帳の `notes` にも残る。IP リテラル宛てはフォールバックしない。**実機の上流プロキシ経由で、プローブが 200（IP 宛て CONNECT を受け付ける）で、HTTPS サイトの撮影が通ることを確認した**（2026-11-19）。
  - **平文 HTTP は上流あり環境では撮らない（既定）**: 絶対 URI を IP にして `Host` を元の名前にすると、実機の上流（Squid 系）は `Host` を URI のホスト（IP）に書き換えるため、仮想ホストのサイトが既定ページ（403 等）を返す。IP に固定できず名前解決を上流に渡すしかないので、**フィルタは平文 HTTP を既定で拒否する**（主フレーム・iframe・サブリソースとも）。影響範囲: **HTTP のサイト自体**は撮影が `upstream_dns_unpinned`（恒久的な失敗として 30 日後に再判定）で失敗する。**HTTPS を含む任意の掲載ページが、攻撃者の用意した DNS rebinding 名の `http://…` へ遷移・埋め込みできても、拒否されるので内部の HTTP コンテンツは撮られない**（掲載時の検査では防げない経路をここで塞ぐ）。HTTPS ページ内の平文サブリソース（画像など）も拒否するだけで、撮影は成功する。**HTTP のサイトも撮るなら `--allow-plain-http-upstream` を明示**する（名前のまま上流へ渡し、上流が名前解決する＝残余リスク。上流とローカルで解決が食い違う名前だと、内部の HTTP が撮られて R2 に載りうる。撮影に `unpinned` が 1 件でもあれば、フラグが無い限り**画像は保存もアップロードもしない**）。
  - **残余リスク（フラグで許したときだけ）と、減らさない緩和策**: `precheck`（公開 IP のみ）はフィルタと同じローカル解決・同じ判定表なので、上流で食い違う名前に対しては残余リスクを**減らさない**。`upload` を明示する運用・`exclude` の維持も、**全件を `upload` する運用では止まらない**。実際に効くのは、(1) ローカルで引けない名前の拒否、(2) HTTPS の IP 固定、(3) 上流あり環境の平文 HTTP の既定拒否。
  - 上流プロキシが無い環境では、フィルタが検査した IP に固定して接続する（HTTP・HTTPS とも。DNS リバインディングも塞ぐ）。
  - ブラウザ内の送信前検査（CDP の Fetch。主フレームの遷移先 URL の検査）は二重防御として残す。
- **リクエスト密輸の防止**（上流連鎖では 1 接続 1 リクエストを保つ）: リクエスト行・ヘッダの CR・LF・NUL・制御文字、obs-fold、token でないメソッド名・ヘッダ名、`Content-Length` の重複・不正（`+`・非 ASCII の数字）、`Transfer-Encoding`、長い行（8KB）・多いヘッダ（100）を拒否し、ヘッダは作り直して送る（`_` を含むヘッダ名［`Transfer_Encoding` 等］と、ページの JS が設定できる `X-Forwarded-For`・`Forwarded`・`Via`・`X-Real-IP` 等は拒否・削除。値は iso-8859-1 のまま再送し二重エンコードしない）。
- **サンドボックス**（bwrap。既定）: **ネットワークなし**（`--unshare-net`。外へ出る道はフィルタへの Unix ソケットだけで、サンドボックス内のループバックの小さな中継［`capture.mjs`］が橋渡しする）、ルートは読み取り専用（`--ro-bind / /`）、`/home`・`/root`・`/mnt`・`/media`・`/run`・`/tmp`・`/var/tmp`・`/opt`・`/srv`・`/sys` は空の tmpfs で隠す（**認証情報 `~/.ssh`・`~/.config`・`~/.npmrc`・gh/wrangler の設定が見えない**。必要な node・playwright・chromium と `/sys/devices/system/cpu`〔CPU 数を読む所〕だけを読み取り専用で重ねるので、`/sys/class/net` からホストのネットワーク IF 名も見えない）、**実体のあるディスクへの書き込みは撮影用の一時ディレクトリだけ**（tmpfs は撮影の終わりに消える領域）、環境変数は空にして必要なものだけ渡す（`--clearenv`）、`--die-with-parent`・`--unshare-pid/ipc/uts`・`--new-session`、`--dev /dev`（GPU デバイスも見えないので drm_open の固まりも起きない）。隔離は `selftest.py` が実測する。**既知の限界**: 読み取り専用で重ねるパス（node・playwright・chromium・リポジトリの `scripts/thumbnail`）の親ディレクトリ名（`/home/<user>/…`）はサンドボックスの中から見える（中身は重ねたものだけ）。
- **リソース**: メモリ・CPU は `systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0 -p CPUQuota=150% -p TasksMax=1024`。**memory コントローラが委譲されて上限が実際に効くこと**（cgroup の `memory.max` を読んで確認）を起動時に確かめ、効かない環境では**黙って続けず終了コード 3 で止まる**（bwrap が無いときと同じ方針。上限なしで撮るなら `--no-limits` を明示する）。Unix ソケットのパスは 108 字までなので、一時ディレクトリは短い場所（`TMPDIR` が長ければ `/tmp`）に作る。受信量と接続数はフィルタが数え、**1 ページ 48MB・300 接続**を超えたら全接続を切ってブラウザごと止め、`resource-limit`（失敗。画像は出さない）にする（試行 20 件の最初の画面は 2〜32MB で、通常のページを止めず暴走・無限ストリームだけを止める値）。1 サイトの全体は 90 秒で強制終了（自分が起動したプロセスグループだけ。親が死んでもブラウザが残らない）。Python の Ctrl-C でも子を確実に止める。撮影の一時ディレクトリ（Chromium のプロファイルを含む）は撮影ごとに作って消す。
- **遷移**: 主フレームのネットワーク遷移（リダイレクト先を含む）は、Python が robots・公開アドレスを確かめた URL だけ。別サイトは `cross-site-redirect`、同一サイトで未検査の URL（`www` の有無・http→https・パスの違い）は、リクエストを**送る前に**止めて `redirect-unchecked:<完全な URL>` とし、Python が**その URL 全体**（パスを含む）の robots と公開アドレスを確かめて、最大 4 回やり直す（未検査の URL へのリクエストは送られない。途中の遷移応答は繰り返し読む）。読み込み後の URL は origin だけ照合（`pushState` でパスが変わるため）。
- **ブラウザの中**: cookie・ログインなし（まっさらなコンテキスト）、JS ダイアログ（alert/confirm/prompt/beforeunload）は拒否、ダウンロードは拒否、新しいウィンドウは閉じる、権限は与えない、Service Worker 不可。ナビゲーションのタイムアウト 20 秒＋描画待ち 1.5 秒。
- `X-Robots-Tag` と `<meta name="robots">`（`MiseResearchBot` 指定も）の `noarchive`・`noimageindex`・`noindex`・`none` は撮らない（`googlebot: noindex` のような別ボット宛ての指示は自分への指示ではないので数えない）。obscured（画面の過半を fixed/sticky 要素が覆う＝cookie 同意バナー・全面モーダルの疑い）は台帳に記録するだけで撮り直さない。
- **白紙・無地**（画素のばらつきがほぼ 0）は失敗（`blank`）にして画像を出さない。HTTP 4xx/5xx の応答（エラーページ）も撮らない。
- 同時 2 のメモリ使用量は上限（2GB/プロセス）を設けたが、実測は未了。本格運用前に測る。

## 量産の見積もり

- **R2 保存**: 試験 37 枚と試行 20 枚の平均は約 5〜6KB（最大 14KB）。**1 サイト 1 枚で上書きなので、10 万サイトでも常に 10 万枚**（約 0.5〜1GB。画質を上げて 20〜40KB 想定でも 2〜4GB で、無料枠 10GB に収まる）。古い画像は残らない。
- **書き込み系操作（Class A）**: 無料枠は月 100 万。月次で 10 万枚を撮り直すと、内容が変わったものだけ put するので最大 10 万回／月（枠の 1 割）。**読み出し（Class B）は月 1,000 万回が無料枠**。配信は Worker の応答で、カスタムドメインの Worker 応答は自動ではエッジにキャッシュされず、`/thumb/` は共有キャッシュ（`caches.default`）にも入れていない。**新規の閲覧者（ブラウザのキャッシュが空）ごとに、サムネイル 1 枚あたり Worker のリクエスト 1 回＋R2 の読み取り 1 回**がかかる。同じブラウザは `?v=` 付きの immutable で 1 年キャッシュするので再訪は無料。目安: 1 人が 1 回の閲覧で 20 枚見るなら、月 10 万人の新規閲覧で 200 万回（R2 の枠の 2 割）、月 50 万人で 1,000 万回（枠いっぱい）。Worker のリクエストもアカウント合計の枠（月 1,000 万）に数えるので、閲覧が増えたら使用量ガード（[usage-guard.md](../operations/usage-guard.md)）で見る。足りなくなったら `caches.default` に入れる・R2 のカスタムドメインに CDN キャッシュを効かせる等を別途検討（現状は入れない）。
- **1 日の枚数**: 10 万件 ÷ 30 日 ≒ **3,300 枚／日**。`--per-day` 既定 3,000 は月 9 万枚で、全件を月 1 回回すには足りない（10 万件になったら 3,500 以上にする）。同時 2・1 枚あたり実時間 約 4 秒（下記）なら 1 日 3,300 枚は約 4 時間。
- **試行の所要時間（同時 2、20 サイト、サンドボックス・フィルタ経由）**: 20 サイトの 1 回目は約 80 秒（1 サイトあたり実時間 約 4 秒。robots 取得と待ち、Chromium の起動・読み込み・描画待ちを同時 2 で重ねた値）。現在の掲載 約 2,200 件の全件撮影は **約 2.5〜3.5 時間**（失敗の再試行は別）。
- D1 への反映は `UPDATE` 1 サイト 1 行（撮影時刻が変わったとき。月次の撮り直しで毎回 1 行）。10 万件の月次で 10 万行の書き込み（D1 の書き込み枠 月 5,000 万行の 0.2%）。

## 試行（2026-11-19、20 サイト）

本番 D1 から読み取りだけ（SELECT）で選んだ 20 サイト（官公庁 `pb-` 5・個人/団体 10・企業/店 5）。**ローカル保存のみ**（R2・D1 の本番には書いていない）。

- 1 回目（初版）: 成功 20/20（初回 13 件。タイムアウト 1 件［20 秒］と `robots_unavailable` 6 件［原因は特定できず。一時失敗として 3 日後に再試行する経路］は期限を戻して撮り直し、全て成功）。
- 2 回目（フィルタ・サンドボックス・サイト固定キーの現行構成）: 成功 15/20、`resource-limit` 5 件。上限が 12MB だったためで（試行の最初の画面は 2〜32MB）、48MB に直して 5 件を撮り直し、**20/20 成功**。
- obscured は 0 件。サイズは 2.0〜13.8KB（平均 約 5.8KB）、LQIP は 95〜179 文字（平均 約 137）。目視（contact sheet）では、歪み・真っ白・エラーページ・ログイン壁・cookie 同意バナーだけの画面は無く、スライドショーの切替・導入アニメの途中らしい画面が 2 件（描画待ちが固定時間のため）。

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
- 上流プロキシが無い環境では、フィルタが検査した IP に固定して接続するので DNS リバインディングも塞ぐ。**上流プロキシがある環境でも、HTTPS は検査済みの IP 宛ての CONNECT で固定し、平文 HTTP は既定で拒否する**（検査と接続先が食い違う余地が残るのは、`--allow-unpinned-upstream` または `--allow-plain-http-upstream` を明示したときだけ。上の上流プロキシの節を参照）。WebSocket・ワーカーを含む全通信がフィルタを通るので、WebSocket の無効化はしていない（使うサイトは動いたまま撮られる）。
- noarchive等の判定は、読み込んだページの最初の応答ヘッダとトップページの meta のみ。
- 成人向けの自動除外は無い（上の「設計」）。
- 画像を使うことの著作権上の整理（サイト運営者の許諾・取り下げ運用）は #29 で扱う。試験の画像は公開していない。
