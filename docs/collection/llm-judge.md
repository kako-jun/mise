# LLM 判定工程（Claude API、手元実行）

候補サイトの取得文を Claude に渡し、紹介文・長い紹介文・一次判定・カテゴリ候補・属性を**候補として**出力させる工程（Issue #48）。公開サーバーからは呼ばない。手元PCだけで動かし、出力は人の承認を経るまで公開しない。鍵・クレジットの運用ルールは [operations/jobs.md](../operations/jobs.md#鍵クレジットllm-予算)、載せる基準は [concept/listing-criteria.md](../concept/listing-criteria.md)、未実装の残りは [ideas/llm-judging.md](../ideas/llm-judging.md)。比較実験の結果と推奨の最小構成は [trials.md](trials.md#llm-判定工程の比較実験2026-11-14issue-48)。

状態: **単独プロトタイプ**（入力は JSONL、出力は `var/` の JSON）。frontier 台帳（[pipeline.md](pipeline.md)）への書き戻しとD1への投入は未実装。

## 構成

| ファイル | 役割 |
|---|---|
| `scripts/llm/pipeline.py` | **推奨の実行口**。2段階・N件まとめ・短い出力キー・バッチ・`--count-tokens`・ドライラン |
| `scripts/llm/judge.py` | 共通部（指示文・スキーマ・検証・キャッシュ・台帳・予算ガード・API）と、1件ずつ全項目を返す基準実装 |
| `scripts/llm/export_sites.py` | 既掲載サイト（`migrations/` を in-memory SQLite に適用）と `data/` の取得結果から入力JSONLと、比較用の人手の値を書き出す。新規アクセスなし |
| `scripts/llm/export_candidates.py` | 発見パイプラインの frontier（`state='candidate'`）を読み取り専用で入力JSONLにする |
| `scripts/llm/fetch_about.py` | トップと About 系ページ（最大2、同一ホスト、robots遵守、2秒間隔）を取得して `body_excerpt` に入れる決定論の補助。名前を解決して**全アドレスが公開アドレス**でなければ取得せず（解決できない名前も拒否）、curl は `--resolve` でチェック済みアドレスに固定する（チェック後の再解決＝DNS リバインディングを防ぐ）。本文テキストはインライン要素の間に空白を足さない（`support</a>.` が `support .` にならない） |
| `scripts/llm/merge_ledgers.py` | 旧 `<var-dir>/llm-ledger.jsonl` を共有台帳へ統合（繰り返し可） |
| `scripts/llm/ledger_release.py` | 開いたままの予約の一覧と手動解放 |
| `scripts/llm/compare.py` `review_long.py` `exp_summary.py` | 人手の値との並置、長い紹介文の根拠突き合わせ表、実験の費用・一致率の表 |

## 取得の限界（fetch_about.py）

プロキシ経由で外に出る環境（curl が `https_proxy` などを使う PC）では、接続するのはプロキシで、名前を解決するのもプロキシ側になる。手元で名前を解決して全アドレスが公開であることは確認するが、`--resolve` による固定は効かず、チェックとプロキシの解決が食い違う余地は残る。プロキシ設定がある場合、`fetch_about.py` は起動時に警告を出す。**取得対象は手元で明示した少数のサイトのリストに限る**運用を守り、不特定のURLを渡さない。プロキシ無しの環境ではアドレス固定が効く。

## 入力

JSONL の1行が1候補: `id`, `url`, `title`, `meta_description`, `lang`, `body_excerpt`, `features`。LLM に渡るのは**この文章だけ**で、HTML は渡さない。決定論で決まるもの（ホスト、HTTP状態、広告タグ数、かな数 など）は `features` に入れるだけで、LLM に出させない。本文は `--body-limit`（既定9000字）で切る。複数ページを入れるときは `body_excerpt` 内に `--- page: URL ---` の区切りを置く。frontier 候補は本文冒頭300字が上限。

## 出力と段階

`pipeline.py` は**2段階**で動く。

1. **第1段（triage、小型モデル）**: 全候補に対し、決まったコードの短い判定を返す。`v` 判定（p pass / s suspect / r reject / d deferred）、`kd` 理由の種別、`sk` サイト種別、`ad` 成人向け（y/n/u）、`sc` 範囲（s standalone / t service_top / u user_page）、`cf` 信頼度、`cat` カテゴリid（最大3）、`r` 理由（pass で信頼度が中以上なら空、そうでなければ短い一文）。
2. **第2段（describe）**: 第1段で `pass` になった候補だけ。`dj`/`de` 短い紹介文、`lj`/`le` 長い紹介文（200〜400字目安）と `ev`（原文からの逐語引用2〜6件）、`tg` タグ、所在（`cc`/`rc`/`q`）。第2段だけ上位モデルにもできる（`--describe-model sonnet`）。

出力は構造化出力（`output_config.format = json_schema`）で固定し、**件ごとに**検証する（キー、列挙、カテゴリidが木にあること、`deferred` と `user_page` の整合、`verdict` と `kind` の対応（pass/deferred は none、reject は広告・量産・パーキング・スパムの種別、suspect は none 以外）、英語の長文の長さ上限、第1段で `ad` が y でないのに `aq` がある場合の拒否 など）。引用は、入力のうち値の部分（`url:` `title:` などのラベル・`(none)`・`<candidate>` タグ・ページ区切りを除く）に空白を1つへ正規化して照合し、語をつなげて一致させることはしない。不正な件だけを1件ずつ再試行する（打ち切りなら `max_tokens` を増やす）。`judge.py --escalate-model` の上位モデル再判定が失敗しても、最初の有効な結果は残し、失敗は `escalation_failed` に記録する。第1段・第2段の結果は `judge.py` の全項目形に正規化して保存する。`--stages full` は全項目を1回で返させる基準形。

判定基準（指示文に入っている。正本は [listing-criteria.md](../concept/listing-criteria.md)）: `reject` は広告中心・量産（アフィリエイト目的等）・パーキング/売却・スパムだけ。個人/事業者/企業/大手/団体/学校/SNS本体/成人向けは理由にしない。`site_kind`（individual / business / organization / company / unknown）、`adult`、`site_scope` は属性として記録するだけ。共有サービス配下の個別ユーザーのページ（`user_page`）は `deferred`（保留）。成人向けの紹介文は中立・婉曲で、本文を再現しない。所在（国・区画）は本文に明記され、逐語引用がある場合だけ。不確かなら `null`。

## トークンを減らす設計

- 固定の英語指示文を `system` に置き、`cache_control` でプロンプトキャッシュに載せる（`--cache-ttl 1h` も可）。最小の長さは Haiku 5.5・Sonnet 5.5 とも 512 トークン（公式 prompt-caching、2026-11-13 確認）。
- 結果は**件ごとの入力ハッシュ**でキャッシュ（`var/llm-cache/`）。まとめ数を変えても同じ件は再課金されない。
- 出力キーは短いコードにし、`max_tokens` は件数に比例して絞る。思考トークンも出力として課金されるので、`--thinking-off`（Haiku は `disabled`、Sonnet は `between_tools`。`--effort xhigh/max` との併用は起動時にエラー）と `--effort` で抑えられる（実測: 思考オフで出力トークンが約 1/3、[trials.md](trials.md#llm-判定工程の比較実験2026-11-14issue-48)）。
- 事前見積り: `--dry-run`（API を呼ばない、保守的な概算）と `--count-tokens`（`/v1/messages/count_tokens` で入力トークンを正確に数える。生成は課金されない）。
- バッチ（`--mode batch`、Message Batches API、50%）。結果はポーリングで取得し、不正な件は同期で再試行する。投入前に状態ファイル `var/llm-batches/` を書き、**同じコマンドの再実行（pipeline.py）または `judge.py --batch-resume <id>` で続きから取得**する（ポーリング失敗・Ctrl-C で予約と有料結果を失わない）。精算済み（結果の記録と予約の解放が済んだ）バッチの resume は拒否され、台帳は変わらない。結果行の費用は `<batch_id>:<custom_id>` で冪等に記録し、予約の解放も台帳上の残りだけを解放するので、途中で落ちて再実行しても合計は増減しない。応答なしで終わった投入（バッチが存在するか不明）は再投入せず停止する。Console で確認のうえ、存在しなければ `scripts/llm/ledger_release.py <tag>`（`--list` で開いた予約を表示）で手動解放する。

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
