# 独立サイトの発見パイプライン

Issue #39（#2 の層2）の実装。個人・小さな事業者・企業・協会・学会・組合・NPO などの**独立したサイト**を、**LLM/エージェントのトークンを使わず決定論のスクリプトだけで**見つけ、「候補」まで進める。候補は掲載ではない（紹介文・スパム一次判定・カテゴリ・種別の確定は別工程、掲載は審査後。D1 への投入はこの段ではしない）。方針は [discovery-policy.md](discovery-policy.md)、実行手順は下の「実行方法」、試験結果（件数）は [trials.md](trials.md)。

## 載せる基準との対応

何を載せ何を拒否するかの基準は [listing-criteria.md](../concept/listing-criteria.md) が正本。このパイプラインでは次の規則で実装している。

属性（拒否に使わない。台帳に記録する）: `site_kind`（個人|事業|団体|企業|不明。決定論で分かる範囲のみ、基本は後段の LLM 工程で埋める）、`adult`（yes|no|unknown。決定論は、meta の rating/RTA、強い手がかり〔年齢確認・18歳未満・18禁・R-18・成人向け〕1 つ、または弱い語〔アダルト・風俗・出会い系 等とホスト名の手がかり〕2 種類以上で yes、他は unknown。弱い語の単独一致では決めない）。共有サービス（ブログ・ショップ・ISP 等）は**本体のトップは載せ**、配下の個別ユーザー領域は拒否でなく**保留(`deferred`)**にして基準の決定を #50 に委ねる。公立学校など自治体の配下のサイトは、層1の自治体トップとは別の独立サイトとして扱う（重複は URL/ホストの正規化で判定する）。

## 構成

```
発見 (discover)  →  取得前フィルタ (triage)  →  取得 (fetch)  →  取得後フィルタ (judge)  →  候補
 台帳に追加          discovered→queued/rejected/deferred   queued・failed→fetched/failed/...   fetched→candidate/rejected
```

| ファイル | 役割 |
|---|---|
| `scripts/pipeline/run.py` | CLI。`uv run python3 scripts/pipeline/run.py <cmd>` |
| `scripts/pipeline/schema.sql` | frontier 台帳のスキーマ（正本はコミット済みのこの定義） |
| `scripts/pipeline/frontier.py` | SQLite ラッパー。台帳の実体は `var/frontier.sqlite`（`.gitignore`、再生成可能。旧い台帳は接続時に追従） |
| `scripts/pipeline/discover.py` | 発見元 |
| `scripts/pipeline/hostnames.py` | ホスト名の正規化と取得前フィルタ |
| `scripts/pipeline/fetcher.py` | 行儀のよい取得（robots・UA・内部IP拒否・上限） |
| `scripts/pipeline/features.py` | 取得後の特徴抽出と判定ルール（閾値はこのファイルの定数） |
| `scripts/pipeline/blocklist/` | データ（出典明記）: 共有サービス、広告ドメイン、パーキング語、成人向け属性語、種別語 |
| `tests/test_pipeline.py` | ネットワーク不要のテスト: `uv run python3 -m unittest tests.test_pipeline` |

## frontier 台帳

単位は**ホスト**（キーは先頭の `www.` を除いた小文字ホスト名）。主な列: `source`（`commoncrawl-webgraph` / `links`）、`source_detail`、`discovered_at`、`depth`、`state`、`next_fetch_at`、`priority`、`fetch_attempts`、`last_http_status`、`reject_reason`、`robots_status`、取得後の `title` / `description` / `lang` / `kana_count` / `text_len` / `ad_hits` / `flags` / `site_kind` / `adult` / `personal_score`（手作り感の語の出現数。並べ替えの目安のみで判定に使わない）。`source_cursor` 表に発見元ごとの「どこまで探したか」を残す。

`state`: `discovered` → `queued`（取得前を通過）/ `rejected` / `deferred`（共有サービス配下の個人領域）→ 取得後 `fetched`（特徴を抽出済み）/ `failed` / `blocked_by_robots` / `rejected` → `judge` で `candidate` または `rejected`。`judge` は台帳の特徴だけを見るので、閾値や語を変えたら再取得せず再判定できる（`triage --redo` は未取得の取得前除外と保留を引き直す）。

**再取得**: `failed`（DNS・接続・robots 到達不能・5xx・429）は `next_fetch_at` が来たら `fetch` の対象になる。待ちは 1 回目の失敗後 1 日、2 回目の失敗後 2 日（`BACKOFF_DAYS` × 2^(試行-1)）で、3 回目の失敗（通算 `MAX_ATTEMPTS`=3）で打ち止めにして `failed` のまま残す。再取得で成功/確定したら `last_error` は消える。

## 発見元

調査の詳細（出典・利用条件・規模・限界）は Issue #39 のコメント参照。

- **`discover-cc`（実装済み）**: Common Crawl のホストレベルのウェブグラフ（`cc-main-2026-jul-aug-sep`）の vertices から `.jp` ホストを取り、ホスト名の CRC32 で 1/N を決定論的に抽出する（既定 N=1000）。パート（約27〜40MB）は一時ファイルに落として完了後に rename し、`--max-filesize` 100MB で打ち切る。`--limit` で打ち切ると、パート内で先に出るホストへ偏る（再現はできる）。**`.jp` が入っているパート番号**（`CC_JP_PART`）はリリースごとに違う: `…-host-vertices.paths.gz` の各パートの先頭 3KB を Range 取得して `zcat | head -1` し、`jp.` が現れる境界のパートを選ぶ（`.jp` はそのパートと直前パートの末尾にまたがる）。
- **`discover-links`（実装済み）**: 既存掲載サイト（`data/collection-*.json`）のトップページから外部リンク先ホストを深さ1で発見する。`.jp` 以外も許し、日本語かは取得後に判定する。
- Wayback CDX / CT ログ（crt.sh）: 大量発見には不向き（TLD 全体の走査が遅い・拒否される／巨大）。個別ドメインの裏取りや補完用として、`discover.py` に関数を足す口だけがある（未実装）。

## フィルタ

**取得前（`hostnames.prefilter` と `run.triage`）**: `government`（`.go.jp`・`.lg.jp`・地域ドメインの市町村。層1の担当）、`spam_like_name`（量産ドメインの形）、`not_jp`、`invalid_host`。共有サービス配下は `deferred`（`shared_service_user`）: `blocklist/platforms.txt` の登録ドメイン配下のホスト、または、台帳に apex も `www.` も無い（本体がリンクされていない）登録ドメインの配下のホストが 3 件以上（`SHARED_SUBDOMAIN_THRESHOLD`）あるもの（`sakura.ne.jp` 型の共有サービスの利用者領域とみなす推定）。apex か `www.` が台帳にある登録ドメイン、および `links`（既掲載サイトからのリンク）由来のホストを含む登録ドメインは、独立サイトの機能サブドメイン群として推定の対象外。同一ドメインの機能サブドメイン（`blog.example.jp` 等）は拒否せず、優先度だけ下げる。

**取得後（`features.judge`）**: 取得できない/HTTP状態（`http_404` 等。5xx・429 は `failed`）、`noindex`（X-Robots-Tag と meta robots）、`parking_or_default_page`（売却・パーキング・サーバ初期ページ。`Under construction` / `coming soon` などの弱い語は **title に出た時だけ**有効。本文だけに出る場合は無視し、本文が薄ければ `thin_content` が拾う）、`thin_content`（可視テキスト 200 字未満）、`not_japanese`（かな 20 字未満）、`no_title`、`ad_heavy`（広告・アフィリエイト参照が 3 以上かつ「本文 300 字あたり 1 つ以上」＝広告が本文より多い、または 15 以上の量産テンプレ級。広告を載せた個人ブログは通す）。通過したものが `candidate`。語による拒否（会社・法人・成人向け・大手など）は使わない。

## 取得作法

- UA は `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`。1ホストあたり robots.txt 1〜2 回 + トップページ 1〜4 回（https→http、`www.` 有無の切替、同一サイト内リダイレクト最大3）。
- robots.txt 遵守（`MiseResearchBot` トークン。Crawl-delay は最大10秒まで尊重）。404/410 は規則なし、401/403 は拒否扱い、5xx 等は `failed`（再試行）。リダイレクトはホップごとに robots を再判定し、**同一サイト（`www.` 有無のみ許容）以外へは追従せず** `redirect_offsite` で除外する。
- ホスト別に 1.5 秒待機。ホストごとに1ジョブなので同一ホストへの同時アクセスはなく、全体の並列は 1〜4（`--workers`、既定3）。
- 本文は先頭 512KB まで、応答待ちは 20 秒まで、リダイレクトは3ホップまで。ポートは 80/443 のみ。
- ホスト名を解決して全アドレスが公開アドレス（`ipaddress.is_global`）であることを確認し、拒否すれば `ssrf_blocked`。プロキシを使わない環境では検査したIP（IPv4 優先）に `--resolve` で固定して接続する。**プロキシ経由の環境ではプロキシ側が名前解決するため固定はできず、検査は事前のみ**（運用機でプロキシなしにするか、egress を制限する）。
- 取得物は台帳に保存しない。保存するのは特徴量と本文冒頭 300 字のみ。成人向け(`adult=yes`)と判定したものは本文冒頭と description も保存しない（残るのは title と属性。title は一覧の識別に必要）。画像・サムネは取得しない（HTML の先頭だけ）。

## 実ネットワークでの未検証事項

次は単体テスト（ネットワークを差し替え）では確認したが、**実ネットワークでは未検証**: `failed` の再取得（バックオフ明けの実取得）、Common Crawl パートの一時ファイル + rename と `--max-filesize`（試験は手元に取得済みのパートを使用）、`triage --redo`（試験台帳の複製への適用のみ）、IPv6 の `--resolve`、プロキシなし環境での IP 固定。

## 実行方法

```sh
uv run python3 scripts/pipeline/run.py discover-cc --sample-every 1000 --limit 1000   # 発見（約40MBを取得してキャッシュ）
uv run python3 scripts/pipeline/run.py discover-links                                 # 既掲載サイトの外部リンク
uv run python3 scripts/pipeline/run.py triage                                         # 取得前フィルタ
uv run python3 scripts/pipeline/run.py fetch --limit 400 --workers 3                  # 取得（実サイトにアクセスする）
uv run python3 scripts/pipeline/run.py judge                                          # 取得後フィルタ・属性付与
uv run python3 scripts/pipeline/run.py report --sample 20                             # 内訳と候補サンプル
```

`--db` で台帳の場所を変えられる。この環境では CA/プロキシの設定を curl が拾う。


## 未実装（次段）

1. 規模拡大: `--sample-every` を下げる。取得は 1 日数百ホスト程度の低速を維持する。
2. 広告中心・量産の特徴量を実データで検証して足す（affiliate/購入導線リンクの量、同一テンプレートの量産）。
3. 保留（`deferred`）の基準（#50）。
4. 台帳の `candidate` を JSON/SQL に出す書き出し（D1 投入前の形）と、申請・拒否リスト（[ideas/requests.md](../ideas/requests.md)）の除外。
5. 紹介文・スパム一次判定・種別・成人向けの確定: 手元からの LLM バッチ（[ideas/llm-judging.md](../ideas/llm-judging.md)）。入力は `title` / `description` / `text_head` のみ。
6. 発見元の追加: Wayback/CT による個別補完、公開リンク集。リンク数順の優先度付けにはグラフの edges/ranks（巨大）が必要で、範囲取得の方法を別途調べる。
7. cron 登録（[operations/jobs.md](../operations/jobs.md)）。
