# 官公庁・自治体の名簿（公式サイト台帳）

国の機関・独立行政法人・都道府県・市区町村の公式トップページを、LLM を使わず決定論で台帳化する（Issue #37、[発見ポリシー](discovery-policy.md)の層1）。**台帳 CSV が正本**で、D1 へ入れる SQL は台帳から生成する派生物にする（多重管理しない）。D1 への投入と公開は未実装で、内容を確認してから別途行う。件数などの進捗は書かず、`scripts/public_bodies_build.py` の出力と台帳を真実とする。

## ファイル

| パス | 内容 |
|---|---|
| `data/public-bodies/ledger.csv` | 台帳（正本）。2 つ以上の独立した出典でホスト名が一致した現行団体と、廃止・合併の履歴 |
| `data/public-bodies/review.csv` | 要確認。出典が 1 つ・不一致・出典なし・総務省名簿に無い。**掲載しない** |
| `data/public-bodies/sources-fetch.json` | 出典ごとの取得 URL・取得日時・Last-Modified・SHA-256（来歴） |
| `data/public-bodies/raw/` | 生の取得物。コミットしない（`.gitignore`）。fetch で再取得できる |
| `scripts/public_bodies_fetch.py` | 出典を取得して `raw/` に保存 |
| `scripts/public_bodies_build.py` | 正規化・突き合わせ・台帳と要確認の生成 |
| `scripts/public_bodies_diff.py` | 2 つの台帳を比べる差分追従スクリプト |

台帳の列: `code`（総務省の団体コード6桁。国の機関・独立行政法人は名称から作る暫定ID `nat:xxxxxxxx`）、`name_ja` / `name_en`、`reading_kana`、`type`（国の機関 / 独立行政法人 / 都道府県 / 市 / 町 / 村 / 特別区 / 行政区）、`prefecture`、`url`、`host`（正規化したホスト名）、`domain_class`（`lg.jp` / `go.jp` / `geo.jp`＝地域型・都道府県型 / `other.jp` / `other`）、`sources`（一致した出典と取得日、`出典@日付;…`）、`status`（`active` / `merged` / `abolished`。`renamed` / `url-moved` は差分で検出して人が付与する）、`status_date`、`successor_code`、`note`。

## 出典と「一致」の定義

| 出典 | 使い方 | 提供形式 | 利用条件 |
|---|---|---|---|
| 総務省 [全国地方公共団体コード](https://www.soumu.go.jp/denshijiti/code.html) | 団体コード・名称・読み。改正一覧表で廃止・合併の履歴 | Excel（コード一覧、改正一覧表、一部事務組合等コード） | 公共データ利用規約（第1.0版）。出典記載が必要 |
| Wikidata（SPARQL） | 団体コード(P429)・公式サイト(P856)・英語名・終了日(P576)・後継(P1366) | CSV | CC0 |
| e-Gov [政府機関関連情報](https://www.e-gov.go.jp/government-directory/) | 都道府県・政令市・特別区、国の行政機関、国会・裁判所、独立行政法人等の公式サイト | HTML | 公共データ利用規約（第1.0版）。出典記載が必要 |
| J-LIS [全国自治体マップ検索](https://www.j-lis.go.jp/spd/map-search/cms_1069.html) | 市区町村・行政区の公式サイト。ページごとに更新日が違い古いものがある | HTML | 出所明示で引用可・改変不可・営利目的は事前相談（掲載前に確認が必要） |

**一致** = 正規化したホスト名（`www.` の有無・http/https・末尾スラッシュ・パスを無視）が、上の URL 出典のうち独立した 2 つで一致したもの。台帳に載せる URL は手で整えられたリンク集（e-Gov、J-LIS）を優先し、`index.html` を落とす。`domain_class`（.lg.jp / .go.jp など）は属性として記録するだけで、一致の出典には数えない（ドメイン名だけで公式と決めない方針）。国の機関・独立行政法人は、e-Gov のホストを Wikidata の公式サイト一覧（.go.jp）でも逆引きし、見つかったものを第 2 出典とする（Wikidata 側の項目名を `note` に残すので、人が名称を見比べられる）。

`www2.` `www4.` など `www.` 以外の違いは別ホストとして不一致になる。偽サイト排除を優先する。

## 要確認（review.csv）の理由

- `mismatch`: 2 つ以上の出典に URL があるがホストが一致しない。旧ドメインから `.lg.jp` への移行、出典側の誤りなどが混ざる。人が両方を見て決める
- `single:<出典>`: URL の出典が 1 つだけ
- `none`: URL の出典なし
- `wikidata-only`: Wikidata にあるが総務省の現行名簿に無い（新設・コード改定の可能性）

## 廃止・合併・新設・名称変更・URL 変更への追従

1. 月 1 回程度、前回の台帳を退避してから `public_bodies_fetch.py` → `public_bodies_build.py` を実行する。
2. `public_bodies_diff.py 旧台帳 新台帳` が 新設 / 廃止・合併 / 名称変更 / URL変更 / 消失 を CSV で出す（差分があると終了コード 1）。
3. **台帳は削除せず状態を変える**。廃止・合併した団体は `status` を `merged` / `abolished`、`status_date` と後継 `successor_code` を入れ、掲載から外す。後継は改正一覧表の「合併後は○○」または同じ改正で新設された団体から決定論で引く。引けないものは `note` に「要確認」と残す。
4. 差分が出ても**自動では公開を変えない**。人が確認してから台帳を確定し、D1 用 SQL を生成する（未実装）。名称変更・URL 移転は差分を見て人が `renamed` / `url-moved` として反映する。
5. 総務省の名簿は更新が遅れる（コード一覧は年に数回）。Wikidata の方が新しい変更を持つ場合は `wikidata-only` として要確認に出る。

## 既知の限界

- 廃止・合併の履歴は総務省の改正一覧表（2005年4月以降）の 1 出典のみ。Wikidata の終了日はごく一部にしか無い。
- 一部事務組合等コード（取得済み）は台帳の対象外。必要になったら別の type として追加する。
- 各団体のトップページへはまだアクセスしていない（到達性、title/meta description、リダイレクト先は次段）。取得するときは robots.txt 遵守・UA `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`・1 団体 1 リクエスト・待機あり・並列なし。
- J-LIS のサーバーは `robots.txt` を返さず 400 を返す。名簿の出典としてのページ取得は少数で低頻度（1 秒待機）に限る。
