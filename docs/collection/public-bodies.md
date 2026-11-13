# 官公庁・自治体の名簿（公式サイト台帳）

国の機関・独立行政法人・都道府県・市区町村の公式トップページを、LLM を使わず決定論で台帳化する（Issue #37、[発見ポリシー](discovery-policy.md)の層1）。**台帳 CSV が正本**で、D1 へ入れる SQL は台帳から生成する派生物にする（多重管理しない）。D1 への投入 SQL は `scripts/public_bodies_sql.py` が台帳から生成する（`migrations/0015_public_bodies.sql`）。本番 D1 への適用は [deploy.md](../operations/deploy.md) の手順で別途行う。件数などの進捗は書かず、`scripts/public_bodies_build.py` の出力と台帳を真実とする。

## ファイル

| パス | 内容 |
|---|---|
| `data/public-bodies/ledger.csv` | 台帳（正本）。2 つ以上の独立した出典でホスト名が一致した現行団体と、廃止・合併の履歴 |
| `data/public-bodies/review.csv` | 要確認。出典が 1 つ・不一致・出典なし・総務省名簿に無い。**掲載しない** |
| `data/public-bodies/sources-fetch.json` | 出典ごとの取得 URL・取得日時・Last-Modified・SHA-256（来歴） |
| `data/public-bodies/evidence/wikidata-gojp.csv` | 国の機関・独立行政法人の第2出典（Wikidata の逆引き結果）。**コミットする固定入力**。再取得で内容が変わりうるため、台帳の再現はこのファイルで行う |
| `data/public-bodies/raw/` | 生の取得物。コミットしない（`.gitignore`）。fetch で再取得できる |
| `scripts/public_bodies_fetch.py` | 出典を取得して `raw/` に保存 |
| `scripts/public_bodies_build.py` | 正規化・突き合わせ・台帳と要確認の生成 |
| `scripts/public_bodies_resolve.py` | 要確認の mismatch を、HTTP リダイレクトで解消できるものだけ台帳へ昇格（`fetch` でアクセス・`apply` で反映） |
| `data/public-bodies/evidence/redirect-check.csv` | 上のアクセス結果（出典 URL・robots 判定・HTTP 状態・最終 URL・確認時刻）。**コミットする固定入力** |
| `scripts/public_bodies_sql.py` | 台帳から D1 投入用 migration を生成（初回は全件、以降は `--since` で差分） |
| `scripts/public_bodies_diff.py` | 2 つの台帳を比べる差分追従スクリプト |
| `scripts/public_bodies_common.py` | ホスト名の正規化など共通関数 |
| `scripts/test_public_bodies.py` | テスト（`uv run --with openpyxl python3 -m unittest scripts/test_public_bodies.py`）。差分検出と、コミット済み台帳の整合（後継の存在・名称の一致など）を検査 |

台帳の列: `code`（総務省の団体コード6桁。国の機関・独立行政法人は名称から作る暫定ID `nat:xxxxxxxx`）、`name_ja` / `name_en`、`reading_kana`、`type`（国の機関 / 独立行政法人 / 都道府県 / 市 / 町 / 村 / 特別区 / 行政区）、`prefecture`、`url`、`host`（正規化したホスト名）、`domain_class`（`lg.jp` / `go.jp` / `geo.jp`＝地域型・都道府県型 / `other.jp` / `other`）、`sources`（一致した出典と取得日、`出典@日付;…`）、`status`（`active` / `merged` / `abolished`。`renamed` / `url-moved` は差分で検出して人が付与する）、`status_date`、`successor_code`、`note`。

## 出典と「一致」の定義

| 出典 | 使い方 | 提供形式 | 利用条件 |
|---|---|---|---|
| 総務省 [全国地方公共団体コード](https://www.soumu.go.jp/denshijiti/code.html) | 団体コード・名称・読み。改正一覧表で廃止・合併の履歴 | Excel（コード一覧、改正一覧表、一部事務組合等コード） | 公共データ利用規約（第1.0版）。出典記載が必要 |
| Wikidata（SPARQL） | 団体コード(P429)・公式サイト(P856)・英語名・終了日(P576)・後継(P1366) | CSV | CC0 |
| e-Gov [政府機関関連情報](https://www.e-gov.go.jp/government-directory/) | 都道府県・政令市・特別区、国の行政機関、国会・裁判所、独立行政法人等の公式サイト | HTML | 公共データ利用規約（第1.0版）。出典記載が必要 |
| J-LIS [全国自治体マップ検索](https://www.j-lis.go.jp/spd/map-search/cms_1069.html) | 市区町村・行政区の公式サイト。ページごとに更新日が違い古いものがある | HTML | 出所明示で引用可・改変不可・営利目的は事前相談（掲載前に確認が必要） |

**一致** = 正規化したホスト名（`www.` の有無・http/https・末尾スラッシュ・パスを無視）が、上の URL 出典のうち別々の公開リンク集 2 つで一致したもの。ただし Wikidata の公式サイト(P856)は編集者が各サイトやリンク集を参照して入れた値で、J-LIS・e-Gov と**完全に独立とは言い切れない**。「2 出典の一致」は偽サイト混入の確率を下げる確認であって、公式であることの証明ではない（次段のトップページ確認で補う）。台帳に載せる URL は、同じホストの候補のうち https を優先し、同じ scheme なら手で整えられたリンク集（e-Gov > J-LIS > Wikidata）を優先して、`index.html` を落とす。`domain_class`（.lg.jp / .go.jp など）は属性として記録するだけで、一致の出典には数えない（ドメイン名だけで公式と決めない方針）。国の機関・独立行政法人は、e-Gov の URL から作った候補 URL（http/https・www 有無・末尾スラッシュの違い）を Wikidata の公式サイト(P856)に完全一致で逆引きし、見つかったものを第 2 出典とする（Wikidata 側の項目名を `note` に残すので、人が名称を見比べられる）。以前は `.go.jp` を含む P856 の全件走査をしていたが、Wikidata Query Service のタイムアウトや部分結果で結果が実行ごとに揺れた（再取得で件数が減った）。完全一致検索に切り替えて結果が安定することを確認し、結果はコミットする `evidence/` に固定した。Wikidata は実行時点の内容で変わるので、見つからなかったものは正直に `review.csv`（`single:egov`）に置く。Wikidata の取得は最大3回試し、行数が下限未満なら取り込まず中止する。

`www2.` `www4.` など `www.` 以外の違いは別ホストとして不一致になる。偽サイト排除を優先する。

## 要確認（review.csv）の理由

- `mismatch`: 2 つ以上の出典に URL があるがホストが一致しない。旧ドメインから `.lg.jp` への移行、出典側の誤りなどが混ざる。人が両方を見て決める
- `single:<出典>`: URL の出典が 1 つだけ
- `none`: URL の出典なし
- `wikidata-only`: Wikidata にあるが総務省の現行名簿に無い（新設・コード改定の可能性）

## 廃止・合併・新設・名称変更・URL 変更への追従

1. 月 1 回程度、前回の台帳を退避してから `public_bodies_fetch.py` → `public_bodies_build.py` を実行する。
2. `public_bodies_diff.py 旧台帳 新台帳` が 新設 / 廃止・合併 / 名称変更 / URL変更(host) / URL変更(url) / 消失 を CSV で出す（差分があると終了コード 1）。host が変わったものと、host は同じで scheme（http→https）やパスだけ変わったものを区別する。
3. **台帳は削除せず状態を変える**。廃止・合併した団体は `status` を `merged` / `abolished`、`status_date` と後継 `successor_code` を入れ、掲載から外す。後継は改正一覧表の「合併後は○○」（合併先名が次の行に分かれているものは連結する）または同じ改正で新設された団体から決定論で引く。同名の新設があれば（支庁制度改革などのコード改定）それだけを後継にする。引けないものは `note` に「要確認」と残す。区分・事由が「〃」や空欄の行は上の行の継続として扱う。`test_public_bodies.py` が、後継が現行名簿に存在すること・事由の合併先名と一致することを検査する。
4. 差分が出ても**自動では公開を変えない**。人が確認してから台帳を確定し、下の「D1 への投入と追従」で SQL を生成する。名称変更・URL 移転は差分を見て人が `renamed` / `url-moved` として反映する。
5. 総務省の名簿は更新が遅れる（コード一覧は年に数回）。Wikidata の方が新しい変更を持つ場合は `wikidata-only` として要確認に出る。

## リダイレクトによる要確認の解消

mismatch の多くは「旧ドメイン（J-LIS）と新ドメイン `.lg.jp`（Wikidata）」の型。`public_bodies_resolve.py fetch` が mismatch 各団体の出典 URL へ robots.txt を確認したうえで 1 URL 1 リクエスト（1.5 秒待機・直列・リダイレクト最大 5 回）でアクセスし、HTTP 200 かつ**最終 URL のホストが別の出典のホストと一致**したときだけ、`apply` が台帳へ昇格する（`sources` に `j-lis+redirect@日付` のように残す）。行き先が一致しない（出典の誤り・別サイト）、取得できない、503 などは要確認のまま。JavaScript や meta refresh による転送は見ない。build を再実行すると台帳・要確認は出典から作り直されるので、**build の後に必ず `apply` をかける**（evidence があれば取得せず再現できる）。昇格した行は `name_en` と `reading_kana` が空。

**未確認**: raw が無い環境で build → `apply` の全工程を再現して台帳が同一になることは確認していない（出典の再取得で内容が変わりうるため）。行政区由来で昇格した浦和区は、独立性の確認が市サイトのホスト一致にとどまる。

## D1 への投入と追従

- 投入範囲: `status=active` の都道府県・市・町・村・特別区・国の機関・独立行政法人。カテゴリは既存の `government-government-agencies`（政府 > 政府機関）で、新カテゴリは作らない。`country_code=JP`。`region_code` は団体コードの上 2 桁から決定論で付与し（`JP-13` など）、国の機関・独立行政法人には付けない。タグは種別と都道府県。紹介文は「○○県○○市の公式サイト。」の定型（英語は台帳の `name_en` があるものだけ。無いものは NULL で、画面は日本語にフォールバックする）。`last_crawled_at`・`source_updated_at` は未設定（トップページ未取得）、`registered_at` は台帳の出典確認日で、`bulk_import=1`（0015 で追加する列）を付けて直近登録の集計・ACTIVITY・NEW タグ・既定の並びの先頭から外す（一括投入が HUD を埋めないため）。
- 政令市の行政区は既定で投入しない。理由: (1) 区のサイトは市サイトの配下のパスで、ホストが市と同じため独立 2 出典の一致が弱い証拠になる、(2) 区を載せると同じ市サイトが 1 市あたり複数件になり一覧を水増しする、(3) 掲載単位（市のみか区も載せるか）は未決。維持する。含めるときは `--include-wards`。台帳には行政区も残る（さいたま市浦和区はリダイレクトで昇格したが、この方針で投入されない）。
- id は `pb-<団体コード>`。URL が既に別の掲載サイトにある団体は INSERT しない（重複掲載しない）。
- 初回: `uv run python3 scripts/public_bodies_sql.py > migrations/0015_public_bodies.sql`。`test_public_bodies.py` が、コミット済み migration が台帳から再現できることを検査する（台帳だけ変えると落ちる）。
- 台帳が変わったとき（月次の差分で新設・合併・廃止・名称変更・URL 移転を確認して台帳を確定した後）: 適用済みの migration は書き換えず、適用時点の台帳を `git show <commit>:data/public-bodies/ledger.csv` で取り出して `public_bodies_sql.py --since 旧台帳.csv > migrations/00NN_public_bodies_update.sql` で差分 migration を作る。新設は INSERT、active でなくなったもの（合併・廃止・要確認への降格）は掲載から DELETE、名称・URL の変更は UPDATE。
- 適用の確認: ローカルで `npm run db:migrate` → `npm run dev` で `/api/sites`・`/jp/?category=government&category=government-government-agencies` を見る。本番は [deploy.md](../operations/deploy.md)。

## 既知の限界

- 廃止・合併の履歴は総務省の改正一覧表（2005年4月以降）の 1 出典のみ。Wikidata の終了日はごく一部にしか無い。
- 一部事務組合等コード（取得済み）は台帳の対象外。必要になったら別の type として追加する。
- 各団体のトップページへは、mismatch の解消に必要な一部を除いてまだアクセスしていない（到達性、title/meta description、リダイレクト先は次段）。取得するときは robots.txt 遵守・UA `MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)`・1 団体 1 リクエスト・待機あり・並列なし。
- J-LIS のサーバーは `robots.txt` を返さず 400 を返す。
- 取得先は名簿の出典サイト（総務省・e-Gov・Wikidata・J-LIS）であり、団体のサイトではない。UA を明示し、直列・1 秒待機・合計数十リクエストに限る。出典サイトの robots.txt は取得前に確認する運用とし（e-Gov は名簿ページを禁止していない）、禁止があれば取得しない。Wikidata は公式の SPARQL エンドポイントを使う。
- 公共データ利用規約（総務省・e-Gov）の出典記載は、台帳を元に公開ページを作るときにそのページ側にも必要。台帳の `sources` 列が出典名と取得日を持つ。
