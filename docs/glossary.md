# 用語集

mise の docs・Issue・コードで使う語の定義。新しい語を足すときはここに1行で定義する（定義の正本はここ）。

## サイトと基準

| 語 | 意味 |
|---|---|
| 独立サイト | 広告・大手プラットフォームの量産物でなく、運営者が自分の判断で運営するサイト。個人・小さな事業者・企業・団体・大手の公式サイトも含む。基準は [listing-criteria.md](concept/listing-criteria.md) |
| 層1 / 層2 | 収集の分担。層1=官公庁・自治体（名簿から作る。[public-bodies.md](collection/public-bodies.md)）、層2=それ以外の独立サイト（[pipeline.md](collection/pipeline.md)） |
| 候補 | 決定論の収集・フィルタを通過したが、まだ掲載していないホスト。掲載ではない |
| 掲載 | D1 に登録され、画面・API・sitemap に出ている状態 |
| 範囲（scope） | 国をディレクトリ全体にかける考え方。カテゴリの木は1つで、国は絞り込み軸。**構想**（[ideas/scope-and-search.md](ideas/scope-and-search.md)） |
| 運営者の所在国 / 区画 | `sites.country_code`（ISO 3166-1 alpha-2）/ `sites.region_code`（ISO 3166-2 第1階層）。公開情報で確認できたものだけ入れる |

## 収集

| 語 | 意味 |
|---|---|
| frontier 台帳 | 発見したホストの状態を持つ SQLite（`var/frontier.sqlite`、再生成可能）。単位はホスト |
| state | frontier 台帳でのホストの状態: discovered / queued / rejected / deferred / fetched / failed / blocked_by_robots / candidate |
| deferred（保留） | 共有サービス配下の個別ユーザー領域。拒否でなく保留（基準は #50 で未決） |
| 決定論 | LLM を使わず、スクリプトの規則だけで処理すること |
| 台帳（名簿） | `data/public-bodies/ledger.csv`。官公庁の正本 |
| MiseResearchBot | 収集時の User-Agent 名。robots.txt はこのトークンで判定する |

## 証券取引所の比喩

画面・文言・docs で統一する用語。掲載の状態を証券取引所にたとえる。**現状は画面に出ておらず、構想**（[ideas/exchange-and-deliberation.md](ideas/exchange-and-deliberation.md)）。

| 語 | 意味 |
|---|---|
| 新規上場 | 新規掲載 |
| 上場 | 掲載（`listed`） |
| 売買停止 | 一時的に到達できない（`down`）。削除せず、フィード・一覧・検索・sitemap から外す |
| 監理ポスト（監理銘柄） | 閉鎖・乗っ取りの疑いがあり見守り中（`long_down` / `suspect`）。「管理ポスト」ではない |
| 上場廃止（整理ポスト） | 却下・掲載終了 |
| 審議コーナー | 候補・保留・取り下げ申請を、3 体の LLM の多数決とともに公開する画面案 |
| 3匹の猫 | 審議コーナーの LLM 3 体。三毛猫の 3 色にちなむ（MAGI の名称・設定は使わない） |

## 画面

| 語 | 意味 |
|---|---|
| ピックアップ | トップで掲載サイトを 1 件ずつ入れ替えて見せる欄 |
| HUD | 画面四隅の情報表示。左上=掲載総数と直近の日別登録数、左下=ACTIVITY（この端末での操作ログ） |
| NEW | 登録から一定期間内のサイトに付くタグ |
| サーフモード / 縦フィード | ネットサーフィンを復興する表示の構想。**構想・試作中** |
| 申請 | 推薦（add）・取り下げ（remove）・訂正（correct）の総称。依頼であり、叶うとは限らない。**構想** |
