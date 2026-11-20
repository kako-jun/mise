# LLM 判定工程（Claude API、手元実行）

候補サイトの取得文を Claude に渡し、紹介文・長い紹介文・一次判定・カテゴリ候補・属性を**候補として**出力させる工程（Issue #48）。公開サーバーからは呼ばない。手元PCだけで動かし、出力は人の承認を経るまで公開しない。鍵・クレジットの運用ルールは [operations/jobs.md](../operations/jobs.md#鍵クレジットllm-予算)、載せる基準は [concept/listing-criteria.md](../concept/listing-criteria.md)、構想の残りは [ideas/llm-judging.md](../ideas/llm-judging.md)。比較実験の結果と推奨の最小構成は [trials.md](trials.md#llm-判定工程の比較実験2026-11-14issue-48)。

状態: 判定（入力は JSONL、出力は `var/` の JSON）は手元実行のプロトタイプ。結果の frontier 台帳（[pipeline.md](pipeline.md)）への取り込み・承認・承認済みの投入 SQL の書き出しは実装済み（下の「台帳への取り込みと承認」）。D1 への適用は人が行い、本番への投入は kako-jun の承認後に別途（この工程は本番に流さない）。

## 構成

| ファイル | 役割 |
|---|---|
| `scripts/llm/pipeline.py` | **推奨の実行口**。2段階・N件まとめ・短い出力キー・バッチ・`--count-tokens`・ドライラン |
| `scripts/llm/judge.py` | 共通部（指示文・スキーマ・検証・キャッシュ・台帳・予算ガード・API）と、1件ずつ全項目を返す基準実装 |
| `scripts/llm/export_sites.py` | 既掲載サイト（`migrations/` を in-memory SQLite に適用）と `data/` の取得結果から入力JSONLと、比較用の人手の値を書き出す。新規アクセスなし |
| `scripts/llm/export_candidates.py` | 発見パイプラインの frontier（`state='candidate'`）を読み取り専用で入力JSONLにする |
| `scripts/llm/fetch_about.py` | トップと About 系ページ（最大2、同一ホスト、robots遵守、2秒間隔）を取得して `body_excerpt` に入れる決定論の補助。名前を解決して**全アドレスが公開アドレス**でなければ取得せず（解決できない名前も拒否）、curl は `--resolve` でチェック済みアドレスに固定する（チェック後の再解決＝DNS リバインディングを防ぐ）。本文テキストはインライン要素の間に空白を足さない（`support</a>.` が `support .` にならない） |
| `scripts/llm/import_results.py` | 判定結果 JSON を frontier 台帳の `llm_judgement` に取り込む（冪等）。API なし |
| `scripts/llm/review_state.py` | `llm_judgement.review_state` の一覧と承認・却下・差し戻し。API なし |
| `scripts/llm/export_approved_sql.py` | 承認済みを `sites` / `site_categories` / `site_tags` への冪等な INSERT SQL として `var/` に書き出す（旧カテゴリ id は新 id に写す）。API なし |
| `scripts/llm/merge_ledgers.py` | 旧 `<var-dir>/llm-ledger.jsonl` を共有台帳へ統合（繰り返し可） |
| `scripts/llm/ledger_release.py` | 開いたままの予約の一覧と手動解放 |
| `scripts/llm/compare.py` `review_long.py` `exp_summary.py` | 人手の値との並置、長い紹介文の根拠突き合わせ表、実験の費用・一致率の表 |

## 台帳への取り込みと承認

判定結果は**候補**で、公開は人の承認の後。流れは「判定 → `import_results.py` → `review_state.py` → `export_approved_sql.py` → ローカル D1 で検証 → （kako-jun の承認後に）本番」。下の 3 つのスクリプトは API も外部アクセスも使わない（費用 $0）。

```sh
uv run python3 scripts/llm/import_results.py var/pipeline-two-g5-haiku.json --input var/frontier-candidates.jsonl   # 台帳 var/frontier.sqlite へ
uv run python3 scripts/llm/review_state.py list --state pending --verdict pass
uv run python3 scripts/llm/review_state.py approve example.com another.example   # reject で却下、pending で差し戻し
uv run python3 scripts/llm/export_approved_sql.py            # -> var/approved-sites.sql
npx wrangler d1 execute mise --local --file var/approved-sites.sql   # ローカル D1 で検証（再実行しても増えない）
```

**台帳の表** `llm_judgement`（正本は `scripts/pipeline/schema.sql`。1ホスト1行、最新の判定だけ。接続時に `CREATE TABLE IF NOT EXISTS` で旧台帳にも付く）: 判定（`spam_verdict` / `spam_kind` / `spam_reason`、`description_ja/en`、`long_description_ja/en` と根拠引用、`categories`、`tags`、`adult` / `site_kind` / `site_scope`、`confidence`、`country_code` / `region_code`、`notes`）、由来（`input_hash`、`origin` = pipeline|judge、`model`、`judged_at` = 結果ファイルの更新日時、`imported_at`）、承認（`review_state` = pending|approved|rejected、既定 pending、`reviewed_at`）。`frontier.adult` / `site_kind`（決定論の属性）とは別の列で、混ぜない。

**取り込みの規則**（`import_results.py RESULTS --input CANDIDATES`。`--input` は judge に渡した JSONL）:

- 結果の `id` は台帳のキーと `export_candidates.cand_id` で対応づける。台帳に無いホスト、複数キーに当たる id、`--input` に無い候補、候補の URL が台帳の行（`final_url` か `url`）と違う（古い入力）もの、`judge.validate_output` を通らない出力（逐語引用の突き合わせを含む）は**拒否**して件数と理由を出し、書かない（終了コード 2）。出力の無い件（失敗）は数えるだけ。
- **冪等**: 入力ハッシュ = sha256(origin、モデル、LLM に渡した候補テキスト)。同じハッシュは何もしない（承認の状態も保つ）。**新しいハッシュ（候補テキストかモデルが変わった）は行を上書きし、`review_state` を pending に戻す**（承認済みでも。内容が変わったので承認し直す）。
- **入力ハッシュに含めるのは origin・モデル・候補テキストだけ**で、指示文・スキーマのバージョンは含めない（含めると既存の台帳・キャッシュの全件が別物になり、承認のやり直しになるため。指示文を変えて再判定したい行は、新しいモデル名か候補テキストの変更で別ハッシュにする）。候補テキストは、判定を実行したときの本文上限（pipeline は結果の `args.body_limit`、`judge.py` は各件の `body_limit`。古い出力にはこれが無いので既定の 9000 字）で描き直し、LLM が実際に見た文章でハッシュと引用の検証を行う。
- **古い結果ファイルは取り込まない**: 結果ファイルの更新日時（`judged_at`）が、台帳にある同じホストの判定より古いときは拒否する（終了コード 2）。古いファイルで新しい判定を上書きし、承認済みを pending に戻さないため。意図して戻すときは `--allow-older`。（限界: `judged_at` はファイルの mtime なので、`cp`（`-p` なし）・`git checkout`・同期で古い結果ファイルが新しく見え、新しい判定を上書きできてしまう。結果ファイルは作った時刻を保ったまま扱う）
- `reject` は取り込みと同時に `review_state=rejected`（広告・量産・パーキング・スパムの一次判定。理由は残る）。`deferred`（共有サービス配下の個別ページ）・`pass`・`suspect` は pending。**`frontier.state` は変えない**（判定は候補で、台帳の状態遷移は決定論の段のもの）。
- `review_state.py approve` は `reject` / `deferred` と、日英の短い紹介文が無い行を拒む（`sites.description_ja` が必須で、載せられないため）。`suspect` は人の判断で承認できるが、**2 段階 pipeline は pass だけ紹介文を作る**ので、suspect に日英の紹介文があるのは full ステージ（`--stages full` と `judge.py`）の結果だけ。2 段階の suspect は紹介文が無く承認できない。
- 成人向け（`adult=yes`）は、台帳の方針（本文冒頭を保存しない）を踏襲し、長い紹介文とその根拠引用を保存しない。成人向けの根拠引用（`adult.evidence_quote`）と所在の根拠引用は、成人向けかどうかに関わらず保存しない（列を持たない。検証は取り込み時に済んでいる）。

**SQL の書き出し**（`export_approved_sql.py`。台帳は読み取り専用で開く）:

- 対象は approved かつ verdict が pass / suspect で、日英の紹介文がある行、かつ **frontier の `state` が今も `candidate`** のもの（`rejected` / `blocked_by_robots` / `deferred` に移ったホストは承認済みでも出さない）。`sites`（`id` = `fr-<ホスト>-<キーの sha1 先頭8桁>`、`url`、`name`、`description_ja/en`、`country_code`、`region_code`）、`site_categories`（木に存在する id だけ。最大 3）、`site_tags`（最大 5）を作る。`registered_at` と検索用の暫定 `search_text`（タグ込み。タグの INSERT のトリガが足す。タグは label_ja 昇順）は DB のトリガが入れ、`bulk_import` は 0（個別に承認したサイトは通常の登録として扱う）。非 ASCII を含む `search_text` の正規化は掲載後に `scripts/refresh-search-text.mjs`（[deploy.md](../operations/deploy.md)）で埋め直す。タグの追加・変更・削除でそのサイトの `search_text` は暫定値（ASCII のみ）に戻るので、まとめて入れた後は埋め直しを回す。回すスクリプトは 0020 を含む版（マージ後は main）を使う（古い版はタグを落とす。deploy.md と同じ注意）。
- `name` は**書き出し時点の** frontier の `title`（LLM が見た title と違うことがある。空白を畳み 80 字まで）。空か `adult=yes` のときはホスト名（成人向けのタイトルは公開文言にしない。紹介文は判定の指示で既に中立・婉曲）。長い紹介文は載せる列がまだ無いので出さない。タグは LLM が返す英語のキーワードで、`label_ja` と `label_en` の両方に同じ値を入れる。タグの英語は暫定で、`label_ja` にも英語が入るため日本語 UI にも英語が出る（和訳は別途）。大文字小文字だけ違う重複は 1 つにし、40 字を超えるタグは捨てる。
- **冪等・除外は SQL 自身が行う**ので、ローカル D1（`wrangler d1 execute --local --file`）に流しても安全（`changes()` の挙動はここでだけ確認した。`--remote --file`＝import API 経路は未検証なので、本番へ流す前に確認する）: 既掲載（`sites.url` を、台帳の `url`・`final_url`・`key` のどれか一つでも一致すれば除外する〔同一サイト内のリダイレクトで `/` が `/ja/` や `/index.html` に変わっても漏らさない。下の `requests`・パス指定の `blocked_hosts` も同じ〕。比較は、スキーム・先頭の `www.`・末尾スラッシュ・大文字小文字・`%7E` と `~` の違いを無視して比較。中間の `www.` は消さない。migration 0015 は任意の位置の `www.` を消すが、ここは `awww.site.com` を別のサイトと取り違えないよう先頭だけにした）、`blocked_hosts`（ホスト全体の拒否は親ドメイン配下を含み、判定は申請の取り込みと同じ `import_requests.block_candidates`。共有ホスティングは `user.github.io` までで、`github.io` の行は利用者全体には効かない。パス指定の行は、そのホスト自身で、パスを intake と同じ規則で畳んだ（`%7E`→`~`、`;params`、`//`、`..`、末尾の `.`・空白）あとのパス配下だけ。LIKE を使わないので `_` `%` は普通の文字）、`requests`（同じ `normalized_key` の推薦 `type='add'` が `rejected`〔手動・取り下げ・期限切れ・blocklisted〕、または `type='remove'` が `approved`。`pending` は除外しない）、同じ `id` は挿入しない。**子の行（タグ・カテゴリ）は、直前の `sites` の INSERT が実際に 1 行入ったときだけ入る**（SQLite の `changes()`。再承認して再書き出ししても、掲載済みのサイトにカテゴリ・タグは増えない）。登録時の `url` がパス指定の `blocked_hosts` 配下なら、`final_url` が配下の外へリダイレクトしていても書き出さない（和集合で判定するため。取り下げ・拒否側に倒れる保守的な挙動で、意図どおり）。承認集合内の同じ URL の重複は書き出し側で 1 件にする。既存の行の更新はしない（紹介文を直したいときは別の仕組みで）。
- 出力は `var/`（`.gitignore`）の派生物でコミットしない。適用先はローカル D1（`--local`）で検証し、**本番 D1 への適用はこの工程に含めない**。
- **成人向け（`adult=yes`）の印**: `sites` に adult 列は無く、approved の成人向けは印なしで公開される（名前だけホスト名に差し替える）。[listing-criteria.md](../concept/listing-criteria.md) の方針（成人向けは理由にせず属性として記録するだけ。中立・婉曲）に従い現状を維持する。公開側に印を出す列・UI は未実装で、別 Issue（sites に adult 属性を持たせるか。[#109](https://github.com/kako-jun/mise/issues/109)）で扱う。

## 取得の限界（fetch_about.py）

プロキシ経由で外に出る環境（curl が `https_proxy` などを使う PC）では、接続するのはプロキシで、名前を解決するのもプロキシ側になる。手元で名前を解決して全アドレスが公開であることは確認するが、`--resolve` による固定は効かず、チェックとプロキシの解決が食い違う余地は残る。プロキシ設定がある場合、`fetch_about.py` は起動時に警告を出す。**取得対象は手元で明示した少数のサイトのリストに限る**運用を守り、不特定のURLを渡さない。プロキシ無しの環境ではアドレス固定が効く。

## 更新の有無と例外（#129）

LLM は `freshness`（`active` / `stale` / `unknown`）と `exempt`（`rare_genre` / `notable` / `none`）を返す（2 段階 pipeline の第 1 段では短いコード `fr`（a/s/u）・`ex`（r/n/-）。理由は既存の `r` / `notes` に 1 行）。`export_candidates.py` が決定論で取れた `freshness_signal_at` / `freshness_source` を `features` に入れて渡す（取れなければキーごと無し）。今日の日付は指示文に入れるが、**キャッシュ鍵（`judge.input_hash`＝`judge.Cache` と `pipeline` の鍵）からは日付行を除く**ので、日をまたいで再開してもキャッシュは外れない（台帳の `import_results.input_hash` は指示文を含まないので元から無関係）。指示文の 4d: 兆候が 3 年以上前または無い個人サイトは `stale` / `unknown`（活動中と推測しない）。`exempt` の根拠は希少性（同じ主題の代替が少ない）か、サイト自体に独立した解説（百科事典等）があることだけで、人気・閲覧数・知名度・古さは根拠にしない。どちらも属性で、これだけでは reject しない。台帳 `llm_judgement.freshness` / `exempt` に保存（既存行は NULL = 未判定）。**入力ハッシュの規則（origin・モデル・候補テキストのみ。指示文を含めない）は変えない**。ただし新しい出力キーは必須なので、#129 より前の結果ファイルは `import_results.py` の検証で拒否される（再判定が要る）。一方 `export_approved_sql.py --update-categories-from` はカテゴリしか使わないので、`freshness` / `exempt` が無い旧結果も受け付ける（`validate_output(require_freshness=False)`。ある場合は値を検証する）。

### 自動承認ポリシー（`approve_policy.py`）

```sh
uv run python3 scripts/llm/approve_policy.py --db var/frontier.sqlite --dry-run   # approve / hold / reject_stale の件数と理由別の内訳
uv run python3 scripts/llm/approve_policy.py --db var/frontier.sqlite --apply     # pending の行だけ review_state を更新
```

- **approve**: `spam_verdict=pass` かつ `confidence` が high または medium（定数 `APPROVE_CONFIDENCES`。low は保留）かつ日英の紹介文あり かつ有効なカテゴリあり かつ公的機関らしくない（名前の語彙: 市立・県立・広域連合・消防・役場・市役所・自治体・教育委員会 等と、`.lg.jp` / `.go.jp` 等のホスト。定数は `PUBLIC_WORDS` / `PUBLIC_HOST_SUFFIXES`）かつ（`site_kind` が business / organization / company と確定している、または `freshness=active` で決定論の日付と矛盾しない）。`individual` と `unknown`（NULL 含む）は個人の可能性があるので freshness が active でなければ承認しない。`review_reason=auto_approved`。
- **reject_stale**: `site_kind=individual` かつ `freshness=stale` かつ `exempt=none` かつ pass + high かつ、決定論の日付（`freshness_signal_at`）が存在し 3 年以上前で、出所が © 年だけ（`footer_copyright`）ではないときだけ。`unknown` や日付なしは更新停止の証拠ではないので hold。`review_state=rejected`、`review_reason=stale_individual`。`review_state.py pending HOST` で戻せる。
- **hold**（人が見る）: suspect・medium・low、個人（または不明）で stale/unknown かつ exempt が rare_genre / notable、stale/unknown だが決定論の日付が無い・© 年だけのもの、#129 より前の判定（個人で freshness が NULL）、LLM の `freshness` と決定論の `freshness_signal_at` が食い違うもの、公的機関らしいもの。
- 「3 年」は `approve_policy.STALE_YEARS` の 1 箇所。ちょうど 3 年前の日付は stale（以上）。人が approved / rejected にした行と、`pending` 以外の行には触らない。`review_state.py` で人が状態を変えると `review_reason` は消える。

## 入力

JSONL の1行が1候補: `id`, `url`, `title`, `meta_description`, `lang`, `body_excerpt`, `features`。LLM に渡るのは**この文章だけ**で、HTML は渡さない。決定論で決まるもの（ホスト、HTTP状態、広告タグ数、かな数 など）は `features` に入れるだけで、LLM に出させない。本文は `--body-limit`（既定9000字）で切る。複数ページを入れるときは `body_excerpt` 内に `--- page: URL ---` の区切りを置く。frontier 候補は本文冒頭300字が上限。

## 出力と段階

`pipeline.py` は**2段階**で動く。

1. **第1段（triage、小型モデル）**: 全候補に対し、決まったコードの短い判定を返す。`v` 判定（p pass / s suspect / r reject / d deferred）、`kd` 理由の種別、`sk` サイト種別、`ad` 成人向け（y/n/u）、`sc` 範囲（s standalone / t service_top / u user_page）、`cf` 信頼度、`cat` カテゴリid（最大3）、`r` 理由（pass で信頼度が中以上なら空、そうでなければ短い一文）。
2. **第2段（describe）**: 第1段で `pass` になった候補だけ。`dj`/`de` 短い紹介文、`lj`/`le` 長い紹介文（200〜400字目安）と `ev`（原文からの逐語引用2〜6件）、`tg` タグ、所在（`cc`/`rc`/`q`）。第2段だけ上位モデルにもできる（`--describe-model sonnet`）。

出力は構造化出力（`output_config.format = json_schema`）で固定し、**件ごとに**検証する（キー、列挙、カテゴリidが木にあること、`deferred` と `user_page` の整合、`verdict` と `kind` の対応（pass/deferred は none、reject は広告・量産・パーキング・スパムの種別、suspect は none 以外）、英語の長文の長さ上限、第1段で `ad` が y でないのに `aq` がある場合の拒否 など）。引用は、入力のうち値の部分（`url:` `title:` などのラベル・`(none)`・`<candidate>` タグ・ページ区切りを除く）に空白を1つへ正規化して照合し、語をつなげて一致させることはしない。不正な件だけを1件ずつ再試行する（打ち切りなら `max_tokens` を増やす）。`judge.py --escalate-model` の上位モデル再判定が失敗しても、最初の有効な結果は残し、失敗は `escalation_failed` に記録する。第1段・第2段の結果は `judge.py` の全項目形に正規化して保存する。`--stages full` は全項目を1回で返させる基準形。

判定基準（指示文に入っている。正本は [listing-criteria.md](../concept/listing-criteria.md)）: `reject` は広告中心・量産（アフィリエイト目的等）・パーキング/売却・スパムだけ。個人/事業者/企業/大手/団体/学校/SNS本体/成人向けは理由にしない。`site_kind`（individual / business / organization / company / unknown）、`adult`、`site_scope` は属性として記録するだけ。共有サービス配下の個別ユーザーのページ（`user_page`）は `deferred`（保留）。既知のポータル/SNS の店舗・ユーザーのページ（`platform_page`）は LLM に渡す前に決定論で除外される（[listing-criteria.md](../concept/listing-criteria.md#優先して充実させる181)。#181 の調査では、`site_kind` と `site_scope` で足りるため、指示文・出力項目は変えていない。`user_page` の保留のうち自作サイトの扱いは未見直し）。成人向けの紹介文は中立・婉曲で、本文を再現しない。所在（国・区画）は本文に明記され、逐語引用がある場合だけ。不確かなら `null`。

## カテゴリの木と指示文

- **選択肢の取得元は `data/categories.csv`**（旧: `migrations/0001_initial.sql` を正規表現で読んでいた）。`judge.load_categories()` が木の順・深さ付きで返し、`assign=rule` の行（`government` 配下の種別 7 葉）は**LLM に渡さない**（官公庁は台帳の種別から決定論で振る。台帳に無い公的機関だけ `government`（トップ）か主題を選ぶ）。構造化出力の `enum` と、結果の検証（カテゴリ id が木にあること）も同じ一覧を使う。
- **指示文**（規則 5）: カテゴリは主題で選ぶ（運営主体で選ばない。店は扱う品の主題へ）、**最も深い（具体的な）id を優先**し、合う子が無いときだけ親、同じ枝からは原則 1 つ、最大 3 つ。一覧は 1 レベル 2 スペースの字下げで木の深さを示す（`category_lines`。`pipeline.py` の第1段も同じ）。
- **トークン**: 一覧は 59 個 約 980 → 153 個 約 3,030 トークン（`count_tokens` で実測、2026-11-18）。差は約 +2,050 で、キャッシュ読み（$0.01/M）ならほぼ 0、キャッシュ無しでも 77 リクエスト（381 件、`--group 5`）で約 $0.016。`system` はキャッシュの最小長 512 トークンを十分に超え、Haiku の 100K トークン階層にも遠い。見積り（`--dry-run`）は文字数ベースで一覧の増加分が自動で入る。木を変えると system が変わるので、結果の入力ハッシュキャッシュ（`var/llm-cache/`）は全件が外れる（一覧が 300 個になっても約 6,000 トークンで費用は誤差。上限を決めるのは費用でなく選び間違いの率なので、育てるときは `exp_summary.py` の category overlap で見る）。
- **旧カテゴリ id の互換**（#114）: 旧 59 → 新の対応表は `data/category-map.csv`。`judge.modernize_categories` が旧 id を新 id に写す（`map`/`keep` は常に、`fallback`（企業・団体・製品とサービス・地域）は他に残る id が無いときだけ、`rule`（官公庁の旧 id）は行政・公的機関 `government`（トップ）に写し（種別の葉は台帳で決まる。0021 の退避と同じ）、`drop` は消す。最大 3、重複なし。0021 の退避先付与（SQL）と同じ結果になることを `tests/test_taxonomy.py` が検査する）。使う場所は 2 つ: (1) `export_approved_sql.py` — 台帳に残る旧 id の承認済み判定を書き出すときに新 id へ写す（写さないと `category_id IN (SELECT id FROM categories)` で黙って落ち、カテゴリ無しで載る）。(2) `import_results.py` — 旧 id を含む過去の結果ファイルの取り込みで、検証の前に写す。**台帳 `llm_judgement.categories` に残る既存の判定そのものは書き換えない**。全件の LLM 再判定（非官公庁 307 件、Haiku 第1段だけで約 $0.05〜0.1）で主題に振り直すのは別作業（#114 Q5）。
- **注意（再判定と掲載済みサイト）**: (a) `exp_summary.py` の「カテゴリ重なり」は、比較元の判定が旧 id だと 0 になる（新 id との一致を数えるため。旧 id を写してから比べる）。(b) `export_approved_sql.py` は掲載済みのサイトを除外するので、**全件再判定（Q5）をしても、既に掲載済みのサイトの `site_categories` は変わらない**（0021 で機械的に移した状態のまま）。既存サイトのカテゴリを更新するには、別モードの書き出し（`--update-categories`）を使う（次節、#122）。

## 掲載済みサイトのカテゴリ更新（#122）

0021 は旧カテゴリを新カテゴリへ機械的に移しただけで、企業/団体系は退避先（`business` / `society` / `business-services` / `community`）に付いたまま。再判定の結果を掲載済みサイトへ反映する、`export_approved_sql.py` の**別モード**（通常の INSERT 出力とは混ぜない。同時指定は不可）。出力は `var/update-categories.sql`（git 管理外）。

流れ: 再判定（`pipeline.py`）→ `import_results.py` / `review_state.py approve`（台帳の掲載候補）→ **`--update-categories`** → ローカル D1 で検証 → （kako-jun の承認後に）本番。

```sh
# (a) 台帳の承認済み（frontier.state='candidate'、verdict pass / suspect）で、sites に fr-<host>-<キーの sha1 先頭8桁> で掲載済みのサイト
uv run python3 scripts/llm/export_approved_sql.py --update-categories --current LOCAL_D1.sqlite --dry-run   # 要約だけ
uv run python3 scripts/llm/export_approved_sql.py --update-categories --current LOCAL_D1.sqlite             # -> var/update-categories.sql
# (b) 台帳に無い掲載済み（sample-*、collected-* など。export_sites.py が出す入力の id = サイト id）。結果 JSON をそのまま使う
uv run python3 scripts/llm/export_approved_sql.py --update-categories-from var/sites-results.json --input var/sites-candidates.jsonl --current LOCAL_D1.sqlite
npx wrangler d1 execute mise --local --file var/update-categories.sql   # ローカル D1 で検証（本番は --remote、承認後のみ）
```

- **置換の SQL**: サイトごとに **INSERT が先、DELETE が後**: `INSERT OR IGNORE … SELECT id FROM categories WHERE id IN (新セット)` → `DELETE FROM site_categories WHERE site_id=… AND category_id NOT IN (新セット)`。どちらも「sites に実在する」「`government` 配下の葉を持たない」「`site_id NOT LIKE 'pb-%'`」で守られ、2 回流しても同じ（冪等）。DELETE にはさらに「新セットの id が対象 DB の `categories` に 1 つでも存在する」条件が付くので、途中で失敗しても、カテゴリ木が違う DB に流しても、サイトが未分類になることはない（余分な行が残るだけ）。
- **対象外**: id が `pb-` の官公庁（SQL 側でも両モードで除外）、(b) では id が `fr-` のもの（台帳の承認を経ていない pass 判定で上書きしないため、(a) の対象。要約 `skipped_ledger_mode`）、`government` 配下の葉（種別 7 葉。台帳の種別から決定論で振った分）を持つサイト、sites に無い id（挿入はしない）。(a) は掲載済みの id が `fr-` で導出できるものだけなので、手動掲載は構造的に当たらない。
- **判定の扱い**: カテゴリは `judge.modernize_categories` を通す（旧 id が残る行は写像）。空・有効な id が 1 つも無い判定は触れない（件数を要約に出す）。(a) は承認済みの pass / suspect、(b) は **pass のみ**（reject / suspect / deferred は触れない）で、出力は `judge.validate_output` を逐語引用込みで再検証し、通らない件は理由を出して書かない（終了コード 2）。
- **親子の同時付与**: 親カテゴリと子カテゴリを同時に付ける判定は、通常の書き出し（INSERT 出力）と同じく子だけに畳む（#137。下の「カテゴリ所属の正規形」）。
- **`--out`**: 未指定ならモードの既定（更新モードは `var/update-categories.sql`）。明示した `--out` は常に尊重する。
- **`--current`**: 反映先の sqlite（ローカル D1 の `.wrangler/state/v3/d1/**.sqlite` など）を読み取り専用で開き、実際の増減を要約に出す。付けると、sites に無い id と `government` 配下の葉を持つサイトは SQL にも書かない。(b) では候補の URL が掲載済みサイトの URL と違う件も除外する（要約 `skipped_url_mismatch`、理由は標準エラーに `SKIPPED`）。付けないと要約は計画件数だけで、除外は SQL 側の守りに任せる。
- **要約（標準出力）**: 計画件数、変わるサイト数・変わらない数・未掲載・官公庁の葉で除外、判定が空で触れなかった件数（`skipped_empty`）、「適用前は退避先カテゴリのみ、適用後に退避先以外がある」サイト数、カテゴリ別の増減（gained / lost の件数）。`--dry-run` は SQL を書かず要約だけ。
- **限界**: (1) 判定の質は再判定の結果次第（このモードは置換するだけ）。(2) 置換は丸ごと入れ替えで、人が付けたカテゴリ（手動掲載）の差分は残さない。(a) の対象は fr- 掲載分に限る。(3) 掲載済みの `sites` 行（名前・紹介文・タグ）は更新しない。(4) 本番の実行は承認後。実行前に `--dry-run --current` の要約でカテゴリ別の増減と退避先からの移動件数を確認する。

## カテゴリ所属の正規形とデータ整合チェック（#137）

**正規形**（正本は [data-model.md](../design/data-model.md#カテゴリ所属の正規形)）: 親と子の両方が付いたら子だけ残す（祖先の除去）。退避先（`business` / `society` / `business-services` / `community`。定義の正本は `data/category-map.csv` の `fallback` 行）は**外さない**: 新しい木ではこれも LLM が選べる正規のカテゴリで、LLM が選んだものを捨てると正しい分類を失う。旧 id の fallback が退避先を付けるのは他に何も残らないときだけ（単独なので外す対象にならない）。官公庁の種別葉（`assign=rule`）はそのまま。判定が空・無効なら今までどおり触れない。

- 正規化は `judge.normalize_categories`（木は `data/categories.csv` の `parent_id`）の 1 箇所。`judge.modernize_categories`（旧 id の翻訳 + 正規化、そのあと最大 3 に切る）と `judge.canonical_categories`（出力 1 件分）が通す。`import_results.py`（台帳に入る時点）と `export_approved_sql.py` の 3 モード（通常の INSERT・`--update-categories`・`--update-categories-from`）が同じ関数を使う。
- 要約（`--update-categories*` の `--dry-run` を含む標準出力、通常モードは標準エラー）に `dropped_ancestors`（外した親）の件数が出る。
- **取り込み**: `import_results.py` は検証の前に categories を黙って正規形にして台帳に入れる（旧 id は新 id に写し、親子は子だけ、同じ id の重複は 1 つに畳む）。「正規形でないから `REJECTED`」という分岐は無い（直前で必ず正規化するため）。未知の id と、親子でない 4 個以上の列は従来どおり `validate_output` が `REJECTED` にする。`validate_output` の重複 id・4 個以上の検査は、正規化を通らない経路（`judge.py` が LLM の生の出力を検証するとき）で効く。
- **書き出しの門**: `export_approved_sql.py` は SQL を書く前に、生成した SQL を全 migration 適用済みのメモリ DB（`--current` があればその sqlite の写し）に流して下の検査を走らせ、**その SQL が新たに作った違反**があれば SQL を書かずに標準エラーへ出して終了コード 3 で止まる（もとから DB にあった違反は止めない＝一度きりの整理を待つ古いデータで夜間ジョブが止まらない）。門を飛ばすフラグは設けない。夜間ジョブの件数ガード（#124。未実装）は、この終了コード 3 を異常として扱う**予定**。
- **`--update-categories` の門は `--current` を付けたときだけ本当に効く**: `--current` が無いと、門が使う基の DB（全 migration 適用済みの空の DB）に `fr-` のサイトが無いので UPDATE は何も変えず、違反を見つけられない（素通し）。`--current` なしで実行すると標準エラーに警告を出す。**夜間ジョブでは `--current` を必須にする**。通常の INSERT 出力は基が空でも新規 INSERT を検査できるので、この制約はない。
- 既存データの一度きりの整理は `scripts/fix-redundant-categories.sql`（手順は [deploy.md](../operations/deploy.md)）。

### データ整合チェック（#137）

`uv run python3 scripts/check-data-integrity.py --local [--persist-to DIR]` / `--remote`（SELECT のみ）/ `--sqlite FILE`。定義の正本は `scripts/llm/integrity.py`（検査ごとに単純な 1 SELECT。D1 本番の複合 SELECT 項数上限のため）。**0 行が正**。error が 1 つでもあれば終了コード 1、warn（`retreat_with_real` `no_country`）は表示のみで終了コードは 0。

| 検査 | 意味 | 重さ |
|---|---|---|
| `ancestor_duplicate` | 親と子の両方が付いている所属（`pb-` 除く） | error |
| `retreat_with_real` | 退避先と他のカテゴリが併存（`pb-` 除く）。LLM が選んだ可能性があり、違反ではなく情報 | warn |
| `uncategorized` | カテゴリが 1 つも無いサイト | error |
| `orphan_site_categories` | 存在しないカテゴリ・サイトを指す `site_categories` | error |
| `category_counts_all` / `category_counts_country` / `category_counts_region` | `category_counts` が再帰 CTE の数え直しと違う（国別・区画別（`JP-13` のような scope 値。0026）は、サイトの無い国・区画に残った n>0 の行も） | error |
| `scope_counts_country` / `scope_counts_all` | `scope_counts` が実サイト数と違う（国・区画とも。区画は `region_code` の一致数） | error |
| `category_closure` | 閉包表の欠け・余りを再帰 CTE と比較 | error |
| `duplicate_site_category` | 同じ (サイト, カテゴリ) の二重（主キーで防がれているはずの確認） | error |
| `duplicate_url` | 正規化後（scheme・`www.`・末尾スラッシュ・大文字小文字を無視）の URL が同じサイト | error |
| `public_body_not_leaf` | 官公庁 `pb-` が `government` 直下の種別葉以外に付いている | error |
| `empty_text` | name か `description_ja` が空 | error |
| `no_country` | `country_code` が NULL（正規の状態だが国別の画面に出ない） | warn |

## トークンを減らす設計

- 固定の英語指示文を `system` に置き、`cache_control` でプロンプトキャッシュに載せる（`--cache-ttl 1h` も可）。最小の長さは Haiku 5.5・Sonnet 5.5 とも 512 トークン（公式 prompt-caching、2026-11-13 確認）。
- 結果は**件ごとの入力ハッシュ**でキャッシュ（`var/llm-cache/`）。まとめ数を変えても同じ件は再課金されない。
- 出力キーは短いコードにし、`max_tokens` は件数に比例して絞る。思考トークンも出力として課金されるので、`--thinking-off`（Haiku は `disabled`、Sonnet は `between_tools`。`--effort xhigh/max` との併用は起動時にエラー）と `--effort` で抑えられる（実測: 思考オフで出力トークンが約 1/3、[trials.md](trials.md#llm-判定工程の比較実験2026-11-14issue-48)）。
- 事前見積り: `--dry-run`（API を呼ばない、保守的な概算）と `--count-tokens`（`/v1/messages/count_tokens` で入力トークンを正確に数える。生成は課金されない）。
- バッチ（`--mode batch`、Message Batches API、50%）。結果はポーリングで取得し、不正な件は同期で再試行する。投入前に状態ファイル（`var/llm-batches/`。pipeline.py は `pipeline-<stage>-<実行ID>.json`、judge.py は `<batch_id>.json`）を書く。pipeline.py は**同じ stage に未精算の状態が残っていれば、同じコマンドの再実行で新しいバッチを投入せず、その状態の続きから取得**する（状態に件の並びを保存しているので、途中で一部がキャッシュ済みになっていても同じバッチを復元する）。judge.py は `--batch-resume <id>` で続きから取得し、未精算の状態が残ったままの新規投入は拒否する。結果行は**全行を先に記録・キャッシュしてから**不正な件を再試行し、精算（結果の記録・再試行・予約の解放が済んだ印）はその後に付ける。精算済みバッチの resume は拒否され、台帳は変わらない。旧形式（精算印なし）の状態ファイルの resume は、費用の二重計上を避けるため拒否する。結果行の費用は `<batch_id>:<custom_id>` で冪等に記録し、予約の解放も台帳上の残りだけを解放するので、途中で落ちて再実行しても合計は増減しない。同じコマンドを2プロセスで同時に走らせても、投入の確認・予約・書き込みはロックで直列化する。**投入 POST に応答が無い・Ctrl-C で中断した場合は、バッチが存在するか不明として予約と状態（`batch_id: null`）を残し、再投入せず停止する**。Console で確認のうえ、存在しなければ `scripts/llm/ledger_release.py <tag>`（`--list` で開いた予約を表示）で手動解放し、状態ファイルを消す。

## 使い方

```sh
set -a; . ~/.config/mise/anthropic.env; set +a        # キーは環境変数でのみ渡す
uv run python3 scripts/llm/export_sites.py            # 既掲載 -> var/sites-candidates.jsonl, var/sites-reference.json
uv run python3 scripts/llm/export_candidates.py --db <frontier.sqlite>   # 発見パイプラインの候補 -> var/frontier-candidates.jsonl
uv run python3 scripts/llm/fetch_about.py in.jsonl out.jsonl            # 任意: About 系ページを追加取得（小規模のみ）
uv run python3 scripts/llm/pipeline.py in.jsonl --dry-run               # 見積りのみ（APIを呼ばない）
uv run python3 scripts/llm/pipeline.py in.jsonl --count-tokens          # 入力トークンを正確に数える（課金なし）
uv run python3 scripts/llm/pipeline.py in.jsonl --stages two --group 5  # 実行（結果は var/）
uv run python3 scripts/llm/pipeline.py in.jsonl --mode batch --cache-ttl 1h
uv run python3 -m unittest discover tests               # キー不要の単体テスト
```

`--model` は `haiku`（`claude-haiku-5-5`）、`sonnet`（`claude-sonnet-5-5`）、料金表に登録済みの完全なモデルID。Opus は使わない。CA が差し替えられる環境は `SSL_CERT_FILE` に CA バンドルを指定する（検証は有効のまま。Python 3.13 の X.509 厳格モードだけ緩める）。

## キャッシュ・台帳・予算ガード

- **台帳** `~/.config/mise/llm-ledger.jsonl`（追記のみ、リポ外・鍵と同じ場所）: 呼び出しごとに input / output / cache作成 / cache読み取りのトークン、推定費用、秒数、件数。**`--var-dir` やワークツリーに依存しない固定パス**なので、全体予算 `--budget-total` は分断されない（`--ledger PATH` で明示したときだけ別の台帳）。読み書きは `<台帳>.lock` への `flock` の下で行い、並列実行でも予算チェックと予約が一体で通る。
- **台帳が無いのに結果が残っている場合は停止**（`var-dir` の `llm-cache/` や `llm-batches/` が空でないのに台帳が無いと、使用済みが 0 円に見えるため）。古い台帳を統合するか、意図して空から始めるなら `--init-ledger`。台帳が壊れている（JSON として読めない、費用の欄が無い）場合も合計を推測せず停止する。
- **以前の `<var-dir>/llm-ledger.jsonl` の統合**: `uv run python3 scripts/llm/merge_ledgers.py $(find var -name llm-ledger.jsonl)`（`--dry-run` で確認可）。同じ記録は二重に足さないので繰り返してよい。
- **予算**: 呼び出し前に「入力の保守的な見積り + `max_tokens` 分の出力」を最悪費用として、`--budget-run`（既定2 USD）と `--budget-total`（既定90 USD、台帳の累積 + 予約）を超えるなら停止（終了コード3）。**単発もバッチも、呼ぶ前に最悪費用を台帳へ予約**し（チェックと書き込みは同じロック内）、実費を記録してから解放する。`max_tokens` を倍にする再試行は倍にした値で再チェックする。最悪費用には system のキャッシュ書き込み割増（5分 1.25 倍、1時間 2 倍）を含める。Haiku 5.5 は入力 100K トークン超が高単価帯（$0.50 / $2.50）なので、見積りが超えそうなら停止し（`--body-limit` / `--group` を下げる）、実費の計算は帯を切り替える。
- **POST は自動再送しない**: タイムアウト・接続エラーの後に生成 POST やバッチ投入を再送すると二重課金・二重投入になりうる。再送するのは冪等な GET と `count_tokens` だけ（429/5xx の応答は全メソッドで再試行）。応答なしで終わった POST の予約は解放せず台帳に残す（課金されたかもしれない）。バッチで `custom_id` が突き合わない結果行も、課金されていれば台帳に記録して予約と整合させる。
- **料金** は公式の料金表（<https://platform.claude.com/docs/en/about-claude/pricing>、2026-11-13確認）を `judge.py` の `PRICING`（Haiku 5.5 は **100K トークン以下**の階層。入力 $0.10 / 出力 $0.50 / キャッシュ書き込み 5分 $0.125・1時間 $0.20 / 読み $0.01、per 1M）と `PRICING_LONG`（**100K 超**の高単価帯）に設定値として持つ。バッチは50%。新モデルはここに足すまで使えない。
- **100K 超の高単価帯の単価**: 入力 $0.50 / 出力 $2.50 は料金表の値。キャッシュの単価は表で確認していない**導出値**で、標準の倍率（書き込み 5分 1.25 倍 = $0.625、1時間 2 倍 = $1.00、読み 0.1 倍 = $0.05、いずれも入力 $0.50 に対する倍率）を当てている（100K 以下の帯でも同じ倍率が成り立つことは上の値で確認できる）。実際に超える場合は Console の請求で突き合わせる。最悪費用の見積りが 100K を超えそうな時点で停止するので、通常はこの帯に入らない。
- **残高の突き合わせ**: 台帳の累積と Console の残高の減りを比べる。台帳に載らない利用があれば差になる。
- リポ内の `/var/`（`.gitignore`）の結果JSON・キャッシュはコミットしない。台帳はリポ外。

鍵の置き場所は `~/.config/mise/anthropic.env`（リポ外、権限600、値は書かない）。クレジットの期限は **2026-12-08 00:00 UTC**（繰り越しなし）。

## 例（公開サイトの一文のみ）

原文（ELM by ChaN のプロフィールの一文）: `This site is to share my projects with electronics enthusiasts in the world.`

→ `description_ja`: 電子工作のプロジェクトを世界の電子工作愛好家と共有するためのサイト。 / `description_en`: A site for sharing electronics projects with electronics enthusiasts around the world. `ev` にはこの一文が逐語で入り、原文に無い引用は検証で弾かれる。
