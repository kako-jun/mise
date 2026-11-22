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
```

- **discover-wikidata（#128）**: 本番の `/api/categories`（読み取りのみ）で `site_count` が 100 未満の葉（対応表 `data/wikidata-leaf-map.csv` にある葉）のうち、対応表の行がまだ全部は `done` でない葉を少ない順に `--wikidata-leaves N`（既定 3、0 で無効）葉まで取る。不足の葉が無い・全葉が完了ならこの段はスキップし（guards に記録）、対応表の順（固定）になるのは本番 API が取れないときだけ。WDQS が 429/503 で中断すると `discover-wikidata` は終了コード 3 で終わり、guards に「Wikidata が中断した（Retry-After）」を残して夜間は続行する。行ごとの続きの OFFSET は台帳の `source_cursor` に残り、夜をまたいで進む。WDQS のルール（5 秒間隔・429/503 で中断・キャッシュ）は [pipeline.md](../collection/pipeline.md#発見元)。
- 各段は冪等。状態の正本は frontier 台帳（`var/frontier.sqlite`）と `var/nightly/<date>/`。`<date>` は夜の名前（UTC の日付 `YYYY-MM-DD`。同日の 2 回目以降のサイクルは `YYYY-MM-DD-N`、N は 2 以上）。
- **同じ `--date` で再実行すると、成功済みの段は飛ばして失敗した段から続ける**。LLM バッチの未精算も再開される（`var/llm-batches/` の未精算の `pipeline-<段>-*` 状態ファイルは**日付に関係なく**拾われるので、翌夜の通常実行でも続きから進む。未精算バッチの候補は今夜の `candidates.jsonl` に上限の外でも必ず含める）。
- 成功済み `candidates` の入力ファイルは partial・失敗からの再開時も保持する。これにより判定対象の元の件数と結果照合が揃う。ファイルの欠落・不正・保存済み件数や SHA-256 の不一致は停止し、通常再実行を重ねても停止を維持する。旧状態で digest が無い場合は件数を確認した最初の再開時に記録する（記録前の同数改変は検出できない）。明示的な `--from-stage candidates` は入力を作り直すため、未精算バッチと旧結果を確認してから使う。
- **LLM 段が未解決を残した夜（pipeline.py が exit 2）は段の状態が `partial`**（失敗ではない。exit 0）。同じ `--date` で再実行すると llm 段から続きを再開し、import 以降もやり直す。`partial` の夜は本番反映（prod 段）を止める。
- **LLM 未解決の自動再試行と打ち切り（#124）**: llm 段が exit 2 のとき、同じ実行内で最大 `--llm-retries N` 回（既定 2）再試行する。キャッシュ済みは課金されず、費用も毎回予算ガードを通る。それでも未解決が残れば、該当 host を `rejected` / `llm_unresolved` とし、guards に記録して残りを処理する。**安全弁**: 未解決が20件超、または対象の5%超なら停止。ただし判定対象が10件以下で、未解決が2件以下かつ半数未満なら打ち切って続行する。未解決を特定できない場合も停止する。
  - **戻し方**: 打ち切った host は台帳で `UPDATE frontier SET state='candidate', reject_reason=NULL WHERE reject_reason='llm_unresolved' AND key='<key>'`（全件なら key 条件を外す）に戻せば、次の夜に再判定される。
- 途中の段からやり直したいときは `--from-stage <段>`（例: `--from-stage sql`）。`status.json` を消さずに、その段以降を成功済みでも実行し直す。
- 失敗した段があればそれ以降は進めず、`status.json` に記録して exit 1。多重起動は `var/nightly/.lock` の flock で防ぎ、取れなければ exit 75。
- 本番 D1 に触れるのは prod 段と、明示的に有効にした media 段。`--apply-prod` が無ければ `wrangler` は呼ばない。`--dry-run` と `--apply-prod` は併用できない。
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

pi-3b の crontab 例（登録は kako-jun が行う。`%` は crontab で特殊文字、Windows パスは絶対パス）:

```cron
37 0 * * * ssh -o ConnectTimeout=10 -o ServerAliveInterval=60 -o ServerAliveCountMax=10 -o StrictHostKeyChecking=accept-new kako-rog 'cmd /c "C:\Users\kako-jun\mise-nightly.cmd"' >> /tmp/mise-nightly.log 2>&1
```

手動の確認は `ssh kako-rog 'cmd /c "C:\Users\kako-jun\mise-nightly.cmd --dry-run"'`（`--dry-run` はラッパーの先頭の `git reset` は走るが、何も書かず API も呼ばない）。

## 新規掲載の画像取得

- `--apply-prod --media-favicon-limit 25 --thumbnail-limit 25` で、新規掲載分のファビコンとサムネイルを取得・配信する。各上限は 1 サイクル 0〜100 件、0 は無効。上限は独立して設定できる。本番の D1 `mise` と R2 `mise-media` 専用。従来のローカル取得だけの `--favicon-limit` は別のオプションなので、自動配信時は省く。
- Windows の rog では `--media-favicon-limit 25 --thumbnail-limit 10` でファビコンとサムネイルを取得・配信する。Playwright Core と Chromium は Windows 上で実行する。必要な環境は[サムネイルの手順書](../collection/thumbnails.md#使い方)を参照。
- 対象は出荷 SQL の ID に一致し、D1 に実際に掲載され、該当の画像列が未設定のサイト。サムネイルは出荷時の台帳で `adult=yes` のサイトをキューに入れない。手動の除外も取得ツールが尊重する。取得と R2 の固定キーへの保存は既存のツールを使い、R2 に現在の画像を置いた印があるときだけ D1 を更新する。
- 未処理分は実行機の `var/nightly/media.sqlite` に残る。次の本番反映モードのサイクルは、追加が 0 件でも、処理可能になった時刻が古い順に処理する。取得に失敗した分は台帳の期限まで待ち、保存済みサムネイルの R2 への転送は翌日から再試行できる。手動の除外は待ち行列から外す。掲載と画像の成功分はそのまま反映され、保存先や段自体の失敗は `guards` と `var/nightly/<date>/media.log` に残る。
- 元の実行機で同じサイクルをやり直すには、元の `--date` と `--apply-prod --from-stage media` に画像上限オプションを付ける。成功済みの prod 段は飛ばし、画像だけを処理する。用意が間に合わなかったサイクルにも、保存済みの `keys.txt` と `export.sqlite` があれば同じ手順を使える。

### rog 一台で画像を継続取得する

`scripts/media_worker.py` は掲載処理とは独立して動く。成功して本番反映済みの過去・今後のサイクルの `status.json`・`keys.txt`・`export.sqlite` を読み、同じ Windows 側 `var/nightly/media.sqlite` に一度だけ取り込む。掲載失敗・処理中のサイクルは対象にしない。過去に出荷記録を残さず手動投入したサイトは対象外。ファビコンとサムネイルは期限を尊重し、R2 に格納できた画像だけ D1 に付ける。既定は両方とも Windows で処理する。並行実行を避けるため worker の専用ロックと、既存 media 段と共有する `.media.lock` を使う。

1. 実行中にコードが書き換わらない専用 clone `C:\Users\kako-jun\repos\mise-media` にこの変更を配置し、Windows 側で `npm ci` を実行する。`var` は作らず、`scripts/cron/mise-media.cmd` は `C:\Users\kako-jun\repos\mise\var` の台帳を使う。`mise-continuous` や既存 nightly の clone を reset しない。
2. Windows のタスクスケジューラで `scripts/cron/mise-media.cmd` を同一ユーザーのログオン時に起動し、実行時間制限なし・重複インスタンス無視に設定する。初回は手動起動し `var/nightly/media-worker.log`、`media.sqlite`、少数サイトの画像 URL と D1 の状態を確認する。既定はファビコン100件、サムネイル10件ずつ、300秒の待機。画像段の失敗は掲載ジョブを停止しない。

Windows では `npm ci` で Playwright Core を用意し、`%LOCALAPPDATA%\ms-playwright` の Chromium headless shell を自動検出する。別の場所を使うときは `PLAYWRIGHT_DIR` と `CHROMIUM_PATH` を設定する。Windows では bwrap/systemd は使わず、既存の robots 検査・公開 IP の検査・フィルタプロキシ・撮影時間と受信量の上限を使う。サムネイル試行は既存の1日上限3000件を守る。

旧 WSL オプションは明示的に拒否する。Windows 撮影時は WSL を起動しない。

### 別の Linux 撮影機へ渡す従来の手順

1. rog 上のサイクルの `status.json` を読み、`ok` と `prod_applied` が true、`prod` 段が `ok` であることを確認する。同じサイクルの `keys.txt` と `export.sqlite` を、撮影機の非公開作業ディレクトリへコピーする。出荷時の成人向け判定を使うため、別サイクルの台帳とは混ぜない。
2. Linux の撮影機でサムネイル用の自己検査を通し、同じ本番 D1 と R2 にアクセスできる Wrangler を用意する。撮影機に永続の `var/nightly` を作る。以下では `var/nightly/2026-11-20-7` に上記2ファイルを置いた例。撮影機ではこのコマンドや他の撮影処理を同時に実行しない。

   ```sh
   uv run --with pillow python3 scripts/nightly_media.py --var var --out var/nightly/2026-11-20-7 --d1-name mise --enqueue --favicon-limit 0 --thumbnail-limit 25
   ```

3. 次回の出荷分も同じ撮影機のキューに投入する。新しい出荷分がなければ `--enqueue` を外して未処理分を進められる。掲載の `--from-stage media` による再開は、元のサイクルの状態が残っている実行機でだけ使う。

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
