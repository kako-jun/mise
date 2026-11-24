# 夜間ジョブ（運用の正本）

発見から掲載候補の SQL 作りまでを毎夜自動で回す仕組み（Issue #124）。実装は `scripts/nightly.py`、状態と成果物は `var/nightly/<date>/`。`mise-ops` スキルはこの文書の手順をそのまま使う。

方針（決定の全文は Issue #124）: 夜間ジョブは**決定論のスクリプト + cron**。LLM は判定の 1 工程（`scripts/llm/pipeline.py`、Anthropic API のバッチ）だけで、Claude Code の対話トークンは使わない。月の LLM 予算は $100。段階化は 1,000 → 1 万 → 10 万。**最初は本番に触れない運用で数夜回し、人が確認してから `--apply-prod` に切り替える。**

## 構成（段）

```
1 discover    discover-links（常に）+ discover-cc（1 夜の上限つき。失敗しても続行）+ discover-wikidata（1 夜 N 葉。失敗しても続行）
2 triage      取得前フィルタ
3 fetch       取得（上限つき）
4 judge       取得後フィルタ -> candidate
5 candidates  今夜の判定対象 = 未判定の candidate だけを candidates.jsonl に書く
6 llm         pipeline.py（第1段 haiku・第2段 sonnet、既定 batch）。予算ガードはここ
7 import      import_results.py で結果を台帳へ
8 approve     approve_policy.py --apply（自動承認 / 保留 / 古い個人サイトの却下）
9 sql         承認済みの未出荷をすべて（`--max-add N` を付けたときだけ key 順に N 件まで。既定は上限なし） -> approved-sites.sql（0 件なら作らない）
10 gate       生成 SQL をメモリ DB に流す整合チェック（integrity.gate）。違反なら停止
11 prod       --apply-prod のときだけ: bookmark 取得 -> 本番投入 -> 整合チェック(--remote)
12 favicon    --favicon-limit N のときだけ(既定は無効): ファビコン取得(#125。`uv run --with pillow` で起動。Pillow が無ければ失敗として guards に出る) -> 取得だけ（ローカルの `var/favicons/<k>.webp`〔サイト固定のキー〕と台帳 `var/favicons.sqlite` に置く。**SQL は作らない**: ローカルにしか無い画像を D1 に指す SQL の誤適用を避けるため）。D1 への反映は、R2 へ置いたあと手で `sql`（既定 `--store r2-remote`、`--since` なし。[手順](../collection/favicons.md#r2-への保存と配信)）。本番には触れず、失敗しても夜間全体は失敗にしない
13 media      #176: 本番に反映したサイトを画像取得の待ち行列へ追加し、取得 → R2 → D1 の順に進める（既定は無効。[設定と再開](#新規掲載の画像取得)）
14 usage      使用量ガード(#157。既定で有効、`--no-usage-guard` で無効): Cloudflare の Workers リクエスト・D1 行数を込みの枠(アカウント全体の合計［Workers と Pages Functions］。mise は内数の表示だけ)と比べ、50%/80%・月末予測・急増を guards に載せる(読むだけ。トークンが無い・API が失敗しても警告のみで続行し、前の段が失敗した夜も動く。手順は [usage-guard.md](usage-guard.md))
15 progress   `--apply-prod` のとき、発見・取得・判定・公開・画像処理の区切りで D1 `url_progress` へ URL の状態を同期（差分だけ更新）。申請 URL も同じ表へ入る。失敗しても収集・公開を止めず guards に記録
```

- **discover-wikidata（#128）**: 本番の `/api/categories`（読み取りのみ）で `site_count` が 100 未満の葉（対応表 `data/wikidata-leaf-map.csv` にある葉）のうち、対応表の行がまだ全部は `done` でない葉を少ない順に `--wikidata-leaves N`（既定 3、0 で無効）葉まで取る。不足の葉が無い・全葉が完了ならこの段はスキップし（guards に記録）、対応表の順（固定）になるのは本番 API が取れないときだけ。WDQS が 429/503 で中断すると `discover-wikidata` は終了コード 3 で終わり、guards に「Wikidata が中断した（Retry-After）」を残して夜間は続行する。行ごとの続きの OFFSET は台帳の `source_cursor` に残り、夜をまたいで進む。WDQS のルール（5 秒間隔・429/503 で中断・キャッシュ）は [pipeline.md](../collection/pipeline.md#発見元)。
- 各段は冪等。状態の正本は frontier 台帳（`var/frontier.sqlite`）と `var/nightly/<date>/`。`<date>` は夜の名前（UTC の日付 `YYYY-MM-DD`。同日の 2 回目以降のサイクルは `YYYY-MM-DD-N`、N は 2 以上）。
- **同じ `--date` で再実行すると、成功済みの段は飛ばして失敗した段から続ける**。LLM バッチの未精算も再開される（`var/llm-batches/` の未精算の `pipeline-<段>-*` 状態ファイルは**日付に関係なく**拾われるので、翌夜の通常実行でも続きから進む。未精算バッチの候補は今夜の `candidates.jsonl` に上限の外でも必ず含める）。
- 成功済み `candidates` の入力ファイルは partial・失敗からの再開時も保持する。これにより判定対象の元の件数と結果照合が揃う。ファイルの欠落・不正・保存済み件数や SHA-256 の不一致は停止し、通常再実行を重ねても停止を維持する。旧状態で digest が無い場合は件数を確認した最初の再開時に記録する（記録前の同数改変は検出できない）。明示的な `--from-stage candidates` は入力を作り直すため、未精算バッチと旧結果を確認してから使う。
- **LLM 段が未解決を残した夜（pipeline.py が exit 2）は段の状態が `partial`**（失敗ではない。exit 0）。同じ `--date` で再実行すると llm 段から続きを再開し、import 以降もやり直す。`partial` の夜は本番反映（prod 段）を止める。
- **LLM 未解決の再試行と保留（#124）**: llm 段が exit 2 のとき、最大 `--llm-retries N` 回（既定 2）再試行する。再試行後も残る全件がサイト固有の根拠引用不一致と確認できれば、件数に関係なく `deferred` / `llm_validation` に保留し、guards に記録して続行する。API の共通障害、未精算バッチ、未分類の失敗、対象を照合できない場合は停止する。`--llm-retries 0` は保留せず停止する。
  - **戻し方**: 内容を確認後、台帳で `UPDATE frontier SET state='candidate', reject_reason=NULL WHERE reject_reason='llm_validation' AND key='<key>'` とする。`triage --redo` ではこの保留は解除されない。
  - 引用不一致が共通原因の兆候である可能性もあるため、件数・対象数・失敗率・連続サイクル数を `status.json` の `llm_validation_warning` とログに警告として残す。警告だけでは周回を止めない。
- 途中の段からやり直したいときは `--from-stage <段>`（例: `--from-stage sql`）。`status.json` を消さずに、その段以降を成功済みでも実行し直す。
- 失敗した段があればそれ以降は進めず、`status.json` に記録して exit 1。多重起動は `var/nightly/.lock` の flock で防ぎ、取れなければ exit 75。
- 本番 D1 に触れるのは prod 段と、明示的に有効にした media 段、および `--apply-prod` 時の URL 進捗同期。`--apply-prod` が無ければ `wrangler` は呼ばない。`--dry-run` と `--apply-prod` は併用できない。
- 「出荷済み」は `var/nightly/shipped.txt`（本番反映に成功した key のうち、**`export_approved_sql.py` が実際に SQL へ出した key だけ**。カテゴリ無し・重複で外れた key は記録しない。`keys.txt` も同じ）。承認済みで未出荷のものを 1 夜 `--max-add` 件まで出す。本物の台帳は変えず、`export.sqlite`（台帳のコピー）で上限をかけて書き出す。

## コマンド

```sh
uv run python3 scripts/nightly.py --dry-run                 # 計画と見積りだけ(何も書かない)
uv run python3 scripts/nightly.py                           # SQL と報告を作る(本番には触れない)
uv run python3 scripts/nightly.py --apply-prod              # 本番反映まで
uv run python3 scripts/nightly.py --date 2026-11-18         # 失敗した夜の再開
uv run python3 scripts/nightly.py --date 2026-11-20-2       # 同じ日の 2 回目のサイクル（新しい夜として別フォルダ）
```

## 発見済み URL の継続処理

`scripts/continuous.py --apply-prod` は `nightly.py` を連続して起動する。最初と6時間ごとに通常の発見元を巡り、間のサイクルは `--skip-discovery` で発見を省き、`discovered` の振り分け・`queued` と再試行期限が来た `failed` の取得・未判定 `candidate` の Claude API バッチ判定・自動承認・本番反映を行う。**既に台帳に溜まった待ちも同じ処理に入る**。発見回数や 1 サイクルの上限は起動オプションで変更できる。既定の API 予算は月 $100 で、nightly の費用見積りと台帳ガードをそのまま使う。

通常の夜間ジョブと台帳を共有し、既存の `.lock` が解放されるまで待つ。継続処理同士は別の `.continuous.lock` で重複起動を防ぐ。UTC 日付の枝番を原子的に確保するので、枝なしの cron と衝突しない。取得・判定対象が無くなったら30分待って再確認し、発見周期と取得失敗の再試行期限も守る。

`var/nightly/continuous.json` は現在のサイクルと最後の発見時刻を記録する。終了済みサイクルの確認前に再起動しても結果を再利用し、途中終了は同じ名前で再開する。**投入段の実行中に落ちて成否不明のときは再投入せず停止**する。月予算ガード中は Claude API 判定と公開を待ち、発見と取得前フィルタだけの `--discover-only` を6時間ごとに続ける。UTC の月が変わったら通常処理に戻り予算を確認し直す。異常ガード、未解決、画像処理の警告、コマンド失敗では停止する。`continuous.json` の `stopped`、対象の `status.json`・`report.md`・本番データと bookmark を確認してから原因を直し、停止状態を退避した上で再開する。投入成否不明を未確認のまま状態を書き換えない。保留・robots 拒否・期限待ちを承認や強制再取得しない。

Windows では `scripts/cron/mise-continuous.cmd` を専用クローン `C:\\Users\\kako-jun\\repos\\mise-continuous` から起動する。依存は専用クローンで `npm ci` して固定バージョンの Wrangler を用意し、データだけ既存の `C:\\Users\\kako-jun\\repos\\mise\\var` を共有する。従来の `mise-nightly.cmd` は起動時に元クローンを `git reset --hard` するため、**同じクローンで長時間動かさない**。専用クローンも実行中に更新せず、停止後に更新する。Windows のタスクスケジューラで該当ユーザーのログオン時または起動時にこの `.cmd` を起動し、既存インスタンスを多重起動しない設定にする。ジョブログは共有 `var/nightly/continuous.log`。認証用 env と Wrangler の本番認証は既存夜間ジョブと同じユーザーに用意する。停止したタスクは原因の確認前に無条件で再試行しない。

ファビコンは `--media-favicon-limit` を指定したときだけ実行する。サムネイルには別途撮影経路の設定が必要で、この継続処理だけでは画像取得は完了しない。`--once` は 1 サイクルだけ動かす運用確認用で、本番反映を含む（`--apply-prod` が必要）。

### `--date` の形式と同日の複数サイクル

- `--date` は `YYYY-MM-DD`（既定は UTC の今日）か `YYYY-MM-DD-N`（N は 2 以上の整数、先頭ゼロ不可）。`2026-11-20-1` は不可。名前はそのまま `var/nightly/<date>/` になる。
- **枝なしの名前（`YYYY-MM-DD`）は cron 専用。手動の追加サイクルは必ず `-2` 以上を使う（`-2`, `-3`, …）。** 手動で枝なしを使うと、同じ名前の cron の夜が「成功済み」と見なされて黙って何もしなくなる（`prod_applied` 済みなら再投入も拒否される）。
- 例: `uv run python3 scripts/nightly.py --date 2026-11-20-2`。別の夜として別フォルダに記録される。同じ名前での再実行の意味（成功済み段のスキップ、`prod_applied` 済みの再投入拒否）は従来どおり名前ごとに効く。
- **前日比の異常ガードは「1 サイクルの追加数」を「直近 7 日の日平均」と比べる**。日の追加数は、その日の `prod_applied` が true のサイクルの `added` の合計（反映済みが無い日は全サイクルの最大値＝未反映の再生成を二重に数えない）。今夜と同じ日の他サイクルは基準に含めない。
- **日の累計は抑えない**。サイクルを重ねれば 1 日の合計は平均の何倍にもなりうる。累計を見るのは人（`status.json` / `report.md`）。意図的な増量運転では `--anomaly-factor N`（既定 5、正の数。`--dry-run` にも出る）で倍率を上げる。

主なオプション: `--from-stage <段>`（途中再開）、`--current-db <sqlite>`（gate 段の土台を本番再現 DB にする。無ければ空 DB）、`--max-links 300`（discover-links の 1 シードあたりの上限）、`--max-add 300`（1 夜の追加上限）、`--anomaly-factor 5`（前日比の異常ガードの倍率）、`--discover-limit 200`（0 で discover-cc 無効）、`--cc-sample-every 1000`（discover-cc の抽出率。`crc32(逆ホスト) % N == 0` の .jp ホストだけ台帳に入る決定論サンプリングなので、N が台帳に入る母集団の天井になり、入れ切ると毎夜 added=0 で供給が止まる。100 で約 10 倍、10 で約 100 倍。N の小さい側は大きい側の上位集合なので既存は重複として弾かれる。`--discover-limit` が 0 のとき無効）、`--wikidata-leaves 3`（0 で Wikidata 無効）、`--fetch-limit 600`、`--favicon-limit 0`（ファビコン段。0 で無効が既定。[collection/favicons.md](../collection/favicons.md)）、`--judge-limit 600`、`--llm-retries 2`（llm 段の未解決の自動再試行回数。0 で従来動作）、`--judge-mode batch|sync`、`--budget-month 100`、`--db`、`--var-dir`、`--ledger`。API キーは環境変数 `ANTHROPIC_API_KEY` のみ（[jobs.md](jobs.md#鍵クレジットllm-予算)。値は出力にも status.json にも出ない）。

## 実行機 kako-rog（Windows）と pi-3b の cron

実行機は **kako-rog**（Windows 11）。pi-3b の cron が ssh で起動する（tail-match と同じ型。[tail-match の cron 構成](https://github.com/kako-jun/tail-match/blob/main/scripts/cron/README.md)）。**ライブ版のラッパーは rog のリポ外** `C:\Users\kako-jun\mise-nightly.cmd`（`git reset --hard` が自分自身を書き換える事故を避ける）。`scripts/cron/mise-nightly.cmd` はその参照コピーで、**ASCII のみ**（SSH のコードページで .cmd の非 ASCII コメントが壊れる。tail-match #405）。変更したら両方を揃える。

- ラッパーは `git fetch` + `git reset --hard origin/main`（毎回最新）→ `PYTHONUTF8=1` → `C:\Users\kako-jun\.config\mise\anthropic.env` を環境変数へ → `uv run python scripts\nightly.py --fetch-limit 300 --judge-limit 300 %*`。ログは `C:\Users\kako-jun\mise-nightly.log` に追記。`--apply-prod` は付けない（人が確認してから）。
- リポは `C:\Users\kako-jun\repos\mise`。状態（`var/`）と LLM 台帳（`C:\Users\kako-jun\.config\mise\llm-ledger.jsonl`）は **rog が正本**。他の機で実行しない（予算と出荷済みが二重になる）。
- 本番反映（`--apply-prod`）に進む前に、rog の `npx wrangler whoami` が通ること（Cloudflare 認証）を確認する。
- ロックは `scripts/llm/filelock.py`（POSIX は `fcntl.flock`、Windows は `msvcrt.locking`）。`npx` は `shutil.which` で解決する。

pi-3b の 00:37 cron は、単発 `mise-nightly.cmd` ではなく、Windows の URL 継続タスク `MiseContinuous` と画像タスク `MiseMedia` を確認する。両タスクは事前に同じログオンユーザーで登録し、専用 clone・認証・重複実行無視・時間制限なしを設定する。`scripts/cron/mise-start-tasks.ps1` を rog の `C:\Users\kako-jun\mise-start-tasks.ps1` に配置し、まず `-CheckOnly` で両タスクが存在し実行中または待機中と確認する。

スクリプトは実行中のタスクには触れず、`Ready` のものだけ開始し、起動後に `Running` であることを確認する。異常・無効・不在・起動直後の終了は非ゼロとし、pi のログに出す。既存の `.continuous.lock`・`.media-worker.lock`、停止フラグと各ジョブのガードはそのまま維持する。タスクは現状 `Interactive` ログオン型のため、rog に対象ユーザーがログオンしていないと実行できない。ログオン後は Windows タスク自体のログオントリガーでも起動する。

pi-3b の crontab（`%` は crontab で特殊文字、Windows パスは絶対パス）:

```cron
37 0 * * * ssh -o ConnectTimeout=10 -o ServerAliveInterval=60 -o ServerAliveCountMax=10 -o StrictHostKeyChecking=accept-new kako-rog 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\kako-jun\mise-start-tasks.ps1' >> /tmp/mise-nightly.log 2>&1
```

手動確認は `ssh kako-rog 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\kako-jun\mise-start-tasks.ps1 -CheckOnly'`。この確認はタスクを起動しない。`-ExecutionPolicy Bypass` はこの起動プロセスに限る（システムの設定は変更しない）。単発ジョブの dry-run が必要な場合は既存の `mise-nightly.cmd --dry-run` を使うが、そのラッパーは元の clone を reset する。

## 画像キュー（D1/R2）

画像キューは migration `0033_media_jobs.sql` 適用後、`site_media_jobs` と `site_media_daily_counts` を正本にする。`scripts/nightly_media.py` と `scripts/media_worker.py` は D1 から期限到来分を原子的に claim し、各 claim は 20 分の lease が付き、異常終了時は期限後に再試行できる。サムネイルの日次上限も D1 の原子的予約で複数 worker 間に共有する。成人判定の初回導入は必ずこの順で行う: **全media workerを停止 → migration 0035を適用 → `export_approved_sql.py` の承認済み分類SQLを全てD1へ適用（最後のNULL分類eligibility backfillまで実行）→ 件数を確認 → workerを再開**。migration直後、既存thumbnail jobは `eligible=0` のままなので、バックフィル完了前にworkerを起動してはならない。承認済みLLM判定 `adult=yes` は `sites.adult_content` として同期され、サムネイル収集から除外する。`adult=no` と `unknown` は旧挙動を保ち収集対象で、favicon は成人判定で除外しない。変換結果の bytes は Cloudflare R2 API へ直接 PUT する。失敗・再試行・除外・eligibility・内容ハッシュ・asset key・日次上限は D1 に記録する。変換時の SQLite は `:memory:` だけで、JSON/SQL/永続画像をローカルに保存しない。ブラウザ撮影の PNG は既存 sandbox がサイトごとに短命な一時ディレクトリへ置き、処理終了時に自動削除する（残る再開状態ではない）。`sites` の画像ポインタ更新も D1 trigger で job state に同期する。

各撮影機には Cloudflare REST API の Bearer `CLOUDFLARE_API_TOKEN` が必要（R2 S3 API の Access Key ID / Secret Access Key ではない）。アカウント ID は `CLOUDFLARE_ACCOUNT_ID`、`wrangler.jsonc`、または token で許可される Cloudflare accounts API から解決する。トークンは専用の最小権限（D1 の対象 DB 読み書き、R2 の対象 bucket の object 読み書き、および自動解決時は account list read）にし、OS の環境設定または外部 secret manager から各 PC のプロセスへ渡す。リポジトリや `var/`、D1 には保存しない。既存の Wrangler OAuth は `wrangler` コマンド用であり、この直接 API クライアントの Bearer token には自動変換されない。`MISE_D1_DATABASE_ID` と `MISE_R2_BUCKET` は任意で、既定は `wrangler.jsonc` の D1 ID と `mise-media`。HTTPS はシステムの CA と proxy 設定を使い、証明書チェーンと hostname は検証する。検査 proxy の CA に欠ける X509 拡張要件だけを緩和する（`VERIFY_X509_STRICT` を無効化）。

Windows worker は `scripts/cron/mise-media.cmd` から起動し、既定で favicon 100 件・thumbnail 10 件を処理する。Playwright Core/Chromium は Windows 上で使い、robots・公開 IP・時間・受信量の既存制限を維持する。worker はローカルのキュー、カーソル、画像を保存しない。

### 旧メディア台帳の移行

`scripts/import_media_state.py` は既定で read-only dry-run。`--source DB=ASSET_DIR` を台帳ごとに繰り返し、全候補を同時に渡す。複数台帳の同一サイトは最新 timestamp の行だけを選び、同時刻で内容が異なるもの、D1 の方が新しいもの、既存 R2 と hash が違うものは overwrite せず ineligible として報告する。Importer は同時リクエストを1件に制限し、API要求間を0.5秒空ける（最大2 req/s）。429 は `Retry-After` と指数バックオフで最大7回の試行後に停止する。dry-run の結果は `verified-existing-r2-object` / `would-upload-local-object` / `state-only` で区別する。実行前に結果を確認し、レビュー後に明示的な `--apply` を付けたときだけ不足 R2 object を upload し、D1 を更新する。R2 の読み戻し hash と D1 の状態/metadata readback の両方が一致した行だけ `eligible_for_cleanup: true` とする。コマンドは元ファイルを削除しない。`var/` 全体の削除許可を意味しない。

この slice はメディア状態だけを移行する。発見 frontier、LLM 審査/承認台帳、夜間再開 checkpoint、source cursor と public body cache は依然ローカルであり、オンラインのみへの移行は未完了。これらの collection を再開する前に別 slice で D1 化する。

## URL 発見ランナー（本番 D1 へ直接）

`scripts/discover_online.py`（#235 / #39 / #128）。見つけた URL を本番 D1 の `url_progress`（`source='discovered'`, `state='discovered'`）へ **直接** 入れる。ローカルに SQLite frontier・CSV・JSONL・キャッシュ・チェックポイントを作らない（メモリのみ。出力は標準出力のレポートだけ）。続きの位置（Wikidata の OFFSET）と実行リースは D1 の `discovery_cursors`（migration 0037、非公開）に置く。**事前に 0037 を本番へ適用する**（未適用でも dry-run は動くが、カーソルは先頭扱い）。

```
uv run python scripts/discover_online.py --source wikidata|wikidata-classes|official|links|cc|all [--limit N] [--max-rows N] [--max-sites N] [--cc-sample-every N]   # 既定 = dry-run(何も書かない)
uv run python scripts/discover_online.py --source official --limit 200 --apply                                  # 書く
```

- 認証は `CLOUDFLARE_API_TOKEN`（上の「画像キュー」と同じ。D1 の読み書き）。D1 へは 0.3 秒間隔、429 は Retry-After を守る（`media_online.Cloudflare`）。
- **先に dry-run**。供給元ごと・カテゴリごとの「入れるはずの件数」と、既知（`url_progress` / `sites.progress_key` に既にある）・無効（IP・内部名・ポート・SNS/ポータル内ページ）・ラン内重複の件数を見る。
- `--apply` は、リース（`discovery_cursors` の `lease/discover`、30 分、バッチごとに更新）を取れたときだけ動く。別の機が動いていれば終了コード 2。終了時にリースを解放する。**同時に 2 台で回さない**。
- 書き込みは `INSERT OR IGNORE`（30 行 / 文、1 文 100 パラメータ未満）→ 全キーの読み戻し検証。最終表は供給元ごとに fetched / invalid / already_known / added / verified。**検証不一致は終了コード 1**。Wikidata が 429/503 で中断したら 3（中断したページのカーソルは進めない。それまでに取得・検証済みのページ分は進む）。実行中にリースを失ったら 4。照合不一致が出たら以降の供給元への書き込みは止める。
- Wikidata: `data/wikidata-leaf-map.csv` の行ごとに P856 を取る（クエリ・正規化は `pipeline/discover_wikidata.py` と同じ。WDQS は 5 秒間隔）。台帳の単位はホストのトップ URL。取り終えた行は `done`、続きは次の OFFSET。
- wikidata-classes（`WIKIDATA_CLASSES` 表、`all` に含む）: 業種・メディア・タイトル・IP・海外サービスなどの根クラスを表で持ち、**サブクラス（P279*）の P31 を持つ公式サイト P856 つきの項目を全部**取る。**知名度（言語版リンク数）では絞らない**（個人・零細も対象）。終了・統合済みも含む。`country`=P17/P495 の Japan 絞りは表の列。並びは項目 ID の文字列順（`ORDER BY STR(?item)`）の**キーセット**で、1 ページにつき WDQS を 2 回（ID の取得 → その ID の URL の取得。1 クエリを 60 秒の制限に収めるため）使う。カーソルは `discovery_cursors`（`source='wikidata-classes'`, `cursor_key='<name>:<クラス・国のハッシュ>'`、値は最後に取った項目 ID（例 `Q12345`）か `done`）。表の条件を変えるとカーソル名が変わり先頭から取り直す。件数が多いクラス（日本の企業は約 6 万件）は `--limit N` / `--max-rows N` で区切り、何回かに分けて回す（1 項目 1 URL。P856 が複数なら文字列順で最小のもの）。WDQS は 5 秒間隔、429/503 は終了コード 3。除外は `candidate_from_url` の機械的な拒否（SNS・ポータル・内部 IP・非 80/443 ポートなど）だけで、個人名ドメイン・ブログ・小規模サイトは通す。表に行を足すときは実 WDQS で件数と先頭ページの応答時間（60 秒以内）を確かめる。根クラスを広げすぎる（例: business の P279*）か複数束ねると WDQS が 504 になるので、**1 行 1 根・狭い根**にする（世界全体の行は特に）。504/タイムアウトのページはスキップしてカーソルを進めず、次回に同じ位置からやり直す。
- official: `OFFICIAL` 表（団体・公式ディレクトリ URL・カテゴリ）の各ページを robots.txt を守って 2 秒間隔で取り、`<main>` 内の外部リンクを会員サイトとして拾う（団体自身のドメイン・SNS・検索・PDF・mailto は除く）。取得できない団体は `UNVERIFIED` に理由つきで残し、毎回レポートに出す（URL を推測で作らない）。団体を足すときは、実ページを取得して会員リンクが並ぶことを確認してから `OFFICIAL` に 1 行足す。
- links（`scripts/discover_links_cc.py`）: 公開済みサイト（`sites.url`）を **`sites.id` 昇順**（機械的。人気・規模では並べない）に、続きから `--max-sites`（既定 100）件ずつ処理する。各サイトのトップを robots.txt を守り 2 秒間隔・本文 512 KB 上限・SSRF 検査つき（`PoliteFetcher`）で取り、外部リンクを **サイト単位（`scheme://host/`。パスは捨てる。`/~user/` の個人ページだけ残す）** にして候補にする。同じサイト内は除く（`xxx.hatenablog.com` 同士のような共有サービス配下は別サイト）。除外は入口（SNS・ポータル・検索・URL 短縮・アプリストア・CDN/インフラ。`ENTRANCE_TREE` と `platforms.txt` の本体）と安全上の拒否（IP・内部名・認証情報つき URL・80/443 以外のポート）だけ。個人名ドメイン・個人ブログ・共有サービス配下の小さなサイトは通る。カーソルは `discovery_cursors` の `links/sites_id`（最後に処理し終えた id）。上限（`--limit`）で候補を捨てたサイトはカーソルを進めず次回やり直す。取れなかったサイト（robots 不許可・HTTP エラー等）は理由別の件数をレポートに出してカーソルは進める。
- cc（`scripts/discover_links_cc.py`）: Common Crawl ホストグラフの vertices（`pipeline/discover.py` と同じ `.jp` ホストのパート）を **curl でストリームのまま** 読む（ファイル・キャッシュを作らない）。`crc32(ホスト名) % --cc-sample-every(既定 1000) == 0` のホストだけを抽出する（日本に絞るのは従来どおり `.jp` のみ。人気順位は使わない）。カーソルは `cc/<graph>/part37/every<N>`（読み終えた `.jp` 行数。続きは先頭から読み飛ばす＝毎回約 40 MB のダウンロード。`done` なら何もダウンロードしない）。`--limit` は追加件数の上限で、上限の行より前を入れて検証してからカーソルを進める。
- `--source all` は 4 供給元すべて。links / cc は 1 回あたりの量を `--max-sites` / `--limit` で小さく切って回す。
- 入った `discovered` 行は、従来どおり後段（取得・判定・審査）が進める。このランナーは `sites` へは何も入れない。

## cron の登録（kako-jun が行う）（Linux の例）

手元 PC か Raspberry Pi の、リポジトリを置いたユーザーの crontab に登録する。**登録は kako-jun が行う。エージェントは登録しない。** 鍵は cron から読めるようにラッパーで読み込む。

```cron
# 毎日 03:10 UTC。最初は --apply-prod を付けない
10 3 * * *  cd /path/to/mise && set -a && . ~/.config/mise/anthropic.env && set +a && uv run python3 scripts/nightly.py >> var/nightly/cron.log 2>&1
```

- 初回は `mkdir -p var/nightly` してから登録する。二重起動の防止は nightly.py 内の flock（`var/nightly/.lock`、LOCK_NB）の**一本だけ**。**外側で `flock` を付けない**（外側が持つロックを nightly.py が別に取りにいき、毎夜 exit 75 になる）。二重起動は後から来た方が即終了（exit 75）する。
- **最初の数夜（目安 3〜7 夜）は `--apply-prod` なしで回し、朝の確認手順で人が確認する。** 問題が無ければ crontab に `--apply-prod` を足す。
- 段階化: 1 夜の量は `--fetch-limit` / `--judge-limit` と予算ガード・前日比の異常ガードで抑える（`--max-add` は既定で上限なし。最初は `--fetch-limit 100 --max-add 50`（1,000 件規模）にしてもよい）。問題が無ければ 1 万、10 万と `--fetch-limit` / `--judge-limit` を上げる。

## 朝の確認手順

1. `cat var/nightly/$(date -u +%F)*/report.md`（今日の全サイクル。枝番の夜 `-2` 以降も含む）。先頭の「結果」が成功か、「費用」が想定内かを見る。
2. `status.json` を見る。`ok` が false なら `stages` の `status: failed` の段と `note` を読む。`guards` に何か出ていれば理由を読む。`next_action` に次の一手が出る。
3. `added`（追加数）が直近の夜と桁違いでないか（`guards` の「前日比の異常」が出ていれば本番反映は止まっている）。
4. 保留のサンプル確認: `uv run python3 scripts/llm/review_state.py list --state pending --limit 20`。保留の理由が妥当か、本来通すべきものが落ちていないかを見る。
5. 却下のサンプル確認: `report.md` の「却下理由別」で多い理由を選び、台帳から数件を見る（`sqlite3 var/frontier.sqlite "SELECT key,title FROM frontier WHERE state='rejected' AND reject_reason='<理由>' ORDER BY random() LIMIT 10"`）。
6. 出荷 SQL の確認: `var/nightly/<date>/approved-sites.sql` の件数（`keys.txt` の行数）と先頭数件のサイトを実際に開いて見る。 自動承認は confidence high / medium なので（2026-11-20 に緩和）、承認済みのサンプルは多めに目視する（`review_state.py list --state approved`）。
7. 問題があれば、該当する key を `review_state.py` で却下してから再実行する（`--date` を変えず再実行すれば SQL が作り直される。成功済みの段は飛ばされるので、`sql` 以降を作り直したいときは `--from-stage sql` を付ける）。

## 本番反映の前後確認（--apply-prod）

prod 段は次の順に動く。どれかが失敗した時点で止まる。

1. `wrangler d1 time-travel info mise --json` で bookmark を取り、`bookmark.txt` に保存して `status.json` の `bookmark` に記録（取れなければ反映しない）。
2. `wrangler d1 execute mise --remote --yes --file approved-sites.sql`（ログは `prod-execute.log`）。SQL は冪等なので、途中で失敗しても再実行で増えない。
3. `scripts/check-data-integrity.py --remote`（ログは `prod-integrity.log`）。error があれば exit 1。

投入の前に、読み取りだけの `check-data-integrity.py --remote` を走らせる（ログは `prod-integrity-before.log`）。**error なら投入しない**（前の夜の error を放置したまま重ねない）。

再実行の扱い: 投入が失敗した夜（`prod_applied` が false で `bookmark.txt` あり）の再実行は、最初に取った bookmark を**上書きせず**そのまま使って再投入する。**`prod_applied` が true の夜は同じ `--date` での再投入を拒否**する（投入後の整合 error など。bookmark も保持）。手順は下の「ロールバック」。

人が見るもの: 反映前後で `SELECT count(*) FROM sites` の差が `added` と一致するか、サイトの一覧に新規が出るか（`--remote` は SELECT のみで）。

## ロールバック

1. `status.json` の `bookmark` を控える（`bookmark.txt` に原文）。
2. 本番の整合チェックが error のとき、**そのまま再実行しない**。原因を見る（`prod-integrity.log`）。
3. 戻す場合のみ: `npx wrangler d1 time-travel restore mise --bookmark <bookmark>`。復元は反映後の全書き込み（申請など）も巻き戻す。実行は kako-jun が判断する。
4. 復元したら、その夜の出荷済み key を `var/nightly/shipped.txt` から削除する（`keys.txt` の key）。削除しないと翌夜以降も出荷されない。

## トラブルシュート

| 症状 | 見る場所 / 対処 |
|---|---|
| exit 75 | 別の夜間ジョブが動いている。`ps` で確認。残骸なら待つ（flock はプロセス終了で自動解除）。cron に外側の `flock` を付けていると毎夜これになる |
| llm 段が `ANTHROPIC_API_KEY が無い` | cron のラッパーで `anthropic.env` を読み込めていない |
| llm 段が skipped「予算ガード」 | 当月の台帳合計 + 最悪費用が `--budget-month` を超える。台帳は `~/.config/mise/llm-ledger.jsonl`。月が変われば自動で再開 |
| llm 段が `partial`「未解決あり」 | 自動再試行後も未解決が上記の安全弁を超えたか、`--llm-retries 0`。同じ `--date` で再実行すると続きから。prod 段はそれまで止まる。打ち切りの戻し方は上記参照 |
| 開いたままの予約（台帳） | `scripts/llm/ledger_release.py --list` で確認してから解放 |
| sql 段 / gate 段が整合チェック違反 | `report.md` の違反行を見る。ほとんどは台帳側の判定データ。該当 key を却下して再実行 |
| discover-cc が失敗 | 続行される（`guards` に記録）。ネットワークか Common Crawl 側 |
| fetch が遅い | `--fetch-limit` を下げる。cron の間隔を空ける |
| 追加が 0 件 | 承認ポリシーで保留が多い（朝の手順 4）。判定対象が尽きているなら `--discover-limit` を上げる |
| favicon 段が guards に出る | Pillow が無い等。`uv run --with pillow` 起動か確認（[favicons.md](../collection/favicons.md)） |
| media 段が guards に出る | `media.log` を読む。撮影環境の隔離要件と、実行機の Wrangler に R2/D1 の権限があるかを確認する |
| ローカル取得だけで保存したファビコンを D1 へ反映したい | R2 へ置いたうえで `sql --out var/favicons-all.sql`（`--since` なし、既定 r2-remote）。`IS NOT` 条件があるので何度適用しても安全（[手順](../collection/favicons.md#r2-への保存と配信)） |

## 未対応（v1）

- **Issue #124 の段 6（ローカル D1 で検証してから本番）は未対応**。現状の gate 段は生成 SQL を**空 DB（マイグレーションのみ）**に流す整合チェックで、本番の既存データでの検証ではない。`--current-db`（本番再現の sqlite）を渡せば、その上で SQL を流して判定する。cron の既定では未使用。
- 葉（カテゴリ）の目標未達を優先した選抜（各葉 100 件の最低ライン）。今は key 順。
- 既存全サイトの一括画像取得は未対応。新規掲載分は [media 段](#新規掲載の画像取得)で取得できる。
- 再巡回（[recrawl](../collection/recrawl.md)）の夜間組み込み。
- 構想の全体は [ideas/llm-judging.md](../ideas/llm-judging.md) と Issue #124。
