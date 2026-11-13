# 個人・小規模サイトの発見パイプライン

Issue #39（#2 の層2）の第1段。個人・小規模サイトを、**LLM/エージェントのトークンを使わず決定論のスクリプトだけで**見つけ、「候補」まで進める。候補は掲載ではない（紹介文・スパム一次判定・カテゴリは別工程、掲載は審査後。D1 への投入はこの段ではしない）。

## 構成

```
発見 (discover)  →  取得前フィルタ (triage)  →  取得 (fetch)  →  取得後フィルタ (judge)  →  候補
 台帳に追加          discovered→queued/rejected   queued→fetched/failed/...   fetched→candidate/rejected
```

| ファイル | 役割 |
|---|---|
| `scripts/pipeline/run.py` | CLI（`init` を除く全段）。`uv run python3 scripts/pipeline/run.py <cmd>` |
| `scripts/pipeline/schema.sql` | frontier 台帳のスキーマ（正本はコミット済みのこの定義） |
| `scripts/pipeline/frontier.py` | SQLite ラッパー。台帳の実体は `var/frontier.sqlite`（`.gitignore`、再生成可能） |
| `scripts/pipeline/discover.py` | 発見元 |
| `scripts/pipeline/hostnames.py` | ホスト名の正規化と取得前フィルタ |
| `scripts/pipeline/fetcher.py` | 行儀のよい取得（robots・UA・内部IP拒否・上限） |
| `scripts/pipeline/features.py` | 取得後の特徴抽出と判定ルール（閾値はこのファイルの定数） |
| `scripts/pipeline/blocklist/` | ブロックリスト（データ。出典明記） |
| `tests/test_pipeline.py` | ネットワーク不要のテスト: `uv run python3 -m unittest tests.test_pipeline` |

## frontier 台帳

単位は**ホスト**（キーは先頭の `www.` を除いた小文字ホスト名。`www.` 有無の重複を畳む）。主な列: `source`（`commoncrawl-webgraph` / `links`）、`source_detail`（グラフのリリース名・リンク元URL）、`discovered_at`、`depth`、`state`、`next_fetch_at`、`priority`、`last_http_status`、`reject_reason`、`robots_status`、取得後の `title` / `description` / `lang` / `kana_count` / `text_len` / `ad_hits` / `flags` / `personal_score`。`source_cursor` 表に発見元ごとの「どこまで探したか」を残す。

`state` の遷移: `discovered` → `queued`（取得前を通過）/ `rejected` → 取得後 `fetched`（特徴を抽出済み）/ `failed`（DNS・接続・5xx など。`next_fetch_at` が翌日になり再試行）/ `blocked_by_robots` / `rejected` → `judge` で `candidate` または `rejected`。`judge` は台帳の特徴だけを見るので、閾値を変えたら再取得せず再判定できる。

## 発見元

調査の詳細（出典・利用条件・規模・限界）は Issue #39 のコメント参照。要点:

- **`discover-cc`（実装済み）**: Common Crawl のホストレベルのウェブグラフ（`cc-main-2026-jul-aug-sep`）の vertices から `.jp` ホストを取り、ホスト名の CRC32 で 1/N を決定論的に抽出する（既定 N=1000）。パートは約40MBをキャッシュ（`var/cache/`）。実行はアクセスなし（データ取得は `data.commoncrawl.org` のみ）。
- **`discover-links`（実装済み）**: 既存掲載サイト（`data/collection-*.json`）のトップページから外部リンク先ホストを深さ1で発見する。取得は他と同じ作法。`.jp` 以外も許し、日本語かは取得後に判定する。
- Wayback CDX / CT ログ（crt.sh）: 大量発見には不向き（TLD 全体の走査が遅い・途中で拒否される／巨大）。個別ドメインの裏取りや補完用として、`discover.py` に関数を足す口だけがある（未実装）。

## フィルタ

**取得前（`hostnames.prefilter`、理由は `reject_reason`）**: `government`（`.go.jp`・`.lg.jp`・地域ドメインの市町村。層1の担当）、`corporate_domain`（`.co.jp`）、`institution_domain`（`.ac.jp`/`.ed.jp`/`.ad.jp`）、`platform`（大手ブログ/ショップ/ホスティング）、`ad_domain`、`subdomain`（共有サービス配下の可能性。将来「別単位」で扱う候補）、`adult_host`、`commercial_host`（shop/clinic 等のトークン）、`spam_like_name`（量産ドメインの形）、`not_jp`、`invalid_host`。データは `blocklist/*.txt`（各ファイル先頭に出典。運用者の手作業の編集で、外部リストの複製ではない）。

**取得後（`features.judge`）**: 取得できない/HTTP状態（`http_404` 等、5xx・429 は `failed`）、`noindex`（X-Robots-Tag と meta robots）、`parking_or_default_page`（売却・パーキング・サーバ初期ページ）、`thin_content`（可視テキスト 200 字未満）、`not_japanese`（かな 20 字未満）、`no_title`、`adult`、`ad_heavy`（広告タグ 5 以上、または 2 以上かつ本文 1500 字未満）、`commercial_content`（title/description に 株式会社・法人・クリニック等）。通過したものが `candidate`。`personal_score` は「個人」「趣味」「管理人」などの語の出現数で、候補を並べる目安にすぎない（判定には使わない）。

## 取得作法

- UA は `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`。1ホストあたり robots.txt 1〜2 回 + トップページ 1〜4 回（https→http、`www.` 有無の切替、同一サイト内リダイレクト最大3）。
- robots.txt 遵守（`MiseResearchBot` トークン。Crawl-delay は最大10秒まで尊重）。404/410 は規則なし、401/403 は拒否扱い、5xx 等は `failed`（再試行）。リダイレクトはホップごとに robots を再判定し、**同一サイト（`www.` 有無のみ許容）以外へは追従せず** `redirect_offsite` で除外する。
- ホスト別に 1.5 秒待機。ホストごとに1ジョブなので同一ホストへの同時アクセスはなく、全体の並列は 2〜4（`--workers`、既定3）。
- 本文は先頭 512KB まで、応答待ちは 20 秒まで、リダイレクトは3ホップまで。ポートは 80/443 のみ。
- ホスト名を解決して全アドレスが公開アドレス（`ipaddress.is_global`）であることを確認し、拒否すれば `ssrf_blocked`。プロキシを使わない環境では検査したIPに `--resolve` で固定して接続する。**プロキシ経由の環境ではプロキシ側が名前解決するため固定はできず、検査は事前のみ**（運用機でプロキシなしにするか、egress を制限する）。
- 取得物（本文）は台帳に保存しない。保存するのは特徴量と本文冒頭 300 字のみ。

## 実行方法

```sh
uv run python3 scripts/pipeline/run.py discover-cc --sample-every 1000 --limit 1000   # 発見（約40MBを取得してキャッシュ）
uv run python3 scripts/pipeline/run.py discover-links                                 # 既掲載サイトの外部リンク
uv run python3 scripts/pipeline/run.py triage                                         # 取得前フィルタ
uv run python3 scripts/pipeline/run.py fetch --limit 400 --workers 3                  # 取得（実サイトにアクセスする）
uv run python3 scripts/pipeline/run.py judge                                          # 取得後フィルタ
uv run python3 scripts/pipeline/run.py report --sample 20                             # 内訳と候補サンプル
```

`--db` で台帳の場所を変えられる。443 の通る環境で、この環境では CA/プロキシの設定を curl が拾う。

## cron 化の手順（まだ登録しない）

手元PC または Raspberry Pi で、リポジトリを置いたユーザーの crontab に次のようなジョブを登録する想定（登録は #39 の次段で、台帳の規模・実行時間を見てから）。

```cron
# 毎日 03:10: 取得前→取得(上限つき)→判定。発見は週1で別ジョブ
10 3 * * *  cd /path/to/mise && uv run python3 scripts/pipeline/run.py triage && uv run python3 scripts/pipeline/run.py fetch --limit 300 --workers 3 && uv run python3 scripts/pipeline/run.py judge >> var/pipeline.log 2>&1
30 2 * * 0  cd /path/to/mise && uv run python3 scripts/pipeline/run.py discover-cc --sample-every 1000 --limit 2000 >> var/pipeline.log 2>&1
```

1回の `fetch` は `--limit` で打ち切り、残りは翌日以降に `queued` から引く。`failed` は `next_fetch_at` まで待つ。同時に複数起動しない（`flock -n var/pipeline.lock` で囲む）。`var/` は再生成可能なので、バックアップは任意。

## 試験（試験時点の観測。再実行で変わる）

2026-11-13 に実サイトで小規模試験を実施。発見は Common Crawl ホストグラフ（パート37、`.jp` 1,373,242 件中 1/1000 抽出 = 1,347 件）と既掲載サイトの外部リンク（18 シード中 15 を取得、55 件）。triage 後に queued 668 件。優先度順に 450 ホストを `fetch`（並列3、ホスト間隔 1.5 秒、所要約 20 分）。

| 結果（450 ホスト） | 件数 |
|---|---|
| candidate（候補） | 136（30%。取得できたホストに対しては約 42%） |
| failed（`dns` 73、`robots_unavailable` 31、`connect` 4、`http_500` 1） | 109 |
| blocked_by_robots | 21 |
| rejected: `commercial_content` | 106 |
| rejected: `thin_content` | 43 |
| rejected: `noindex` 8 / `parking_or_default_page` 7 / `http_403` 4 / `http_404` 4 / `redirect_offsite` 4 / `ad_heavy` 3 / `not_japanese` 3 / `adult` 2 | 35 |

取得前の除外（取得はしていない）: 台帳 1,402 件中 734 件。`subdomain` 368、`platform` 328、`commercial_host` 28、`government` 7、`corporate_domain` 3。共有サービス配下が全体の約半数を占める。

候補の上位 20 件（`personal_score` 順。本文冒頭から見て）:
viron.jp（建材会社）、isogami.jp（餃子バル）、k-inbou.jp（印鑑店）、kstac.jp（大学山岳部）、si-izumo.jp（ソロプチミスト）、uetomi.jp（造園）、k-izumi.jp（個人名のみのページ）、livent-expo.jp（展示会）、m-collection.jp（個人のイラスト作品）、nis-ssp.jp（大学の研究センター）、noseri.jp（セラピスト）、queenlake.jp（健康食品）、r-jbr.jp（会）、sankei.jp（新聞社）、sanmeigaku.jp（占い鑑定所）、sendai-akita.jp（県人会）、soeru.jp（パネル販売）、solar-soudan.jp（太陽光）、space-city.jp（WordPress初期サンプルのショップ）、tsuchida-k.jp（建設会社）。

誤判定の傾向:
- **候補の大半は個人サイトではなく、小規模事業者・団体・学会**。上位 20 件で個人らしいのは kstac / k-izumi / m-collection / sendai-akita の 4 件程度。`personal_score`（語の出現数）は個人らしさの指標としては弱い。
- `or.jp` の組織系（社会福祉法人・学会・協同組合など）が初回は多数通った。`blocklist/commercial_markers.txt` に 一般社団法人・NPO法人・学会・研究会・連盟・青年会議所・市立・県立・工務店・注文住宅・就職支援・協会 を足して `judge` を再実行したところ、候補は 166 → 136 に減り、上位 20 件から学会・財団系は消えた。ただし事業者名（建設会社・店舗）は title に決まった語がなく残る。
- `sankei.jp` のような大手法人、`space-city.jp`（WordPress の初期サンプル文章のまま）は決定論では落ち切らない。後者は「これはサンプルページです」をパーキング/初期ページのマーカーに追加する余地がある。
- `failed: dns` が多い（放置・失効ドメイン。グラフは「リンクされた」ホスト名であり生存の証明ではない）。`robots_unavailable`（robots.txt が 5xx/到達不能）が約 7% あり、再試行で救える可能性がある。
- 取得できたのに `thin_content` になるもの（トップが JS 描画・フレーム・画像のみ）は個人サイトに多く、取りこぼしの可能性がある。

結論: 取得前後の決定論フィルタだけで「ゴミ（失効・パーキング・成人・広告）」はかなり落とせるが、「個人か事業者か」は決定論の限界に近い。次段のローカル LLM による一次判定（個人/事業者/団体）が必要。

## 次段の提案

1. 規模拡大: 1/1000 抽出 → `--sample-every` を下げる。取得は1日数百ホスト程度の低速を維持し、数週間で回す。
2. 台帳の `candidate` を JSON/SQL に出す書き出し（D1 投入前の形）と、#29/#4 の申請・拒否リストの除外。
3. 共有サービス配下（`subdomain`/`platform`）の個別ブログを別単位として扱う方針の決定。
4. 紹介文・スパム一次判定・カテゴリ: 手元のローカルLLMでバッチ（#2 の設計方針どおり）。入力は `title`/`description`/`text_head` のみ。
5. 発見元の追加: Wayback/CT による個別補完、公開リンク集。リンク数の多い順に優先度を付けるにはグラフの edges/ranks（巨大）が必要で、必要なら範囲取得の方法を別途調べる。
6. 誤判定の見直し（上記「試験」の傾向）に基づくマーカー・閾値の調整。「個人か事業者か団体か」は次段のローカルLLM一次判定の最重要項目。
