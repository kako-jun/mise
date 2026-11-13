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
| `scripts/llm/fetch_about.py` | トップと About 系ページ（最大2、同一ホスト、robots遵守、2秒間隔）を取得して `body_excerpt` に入れる決定論の補助 |
| `scripts/llm/compare.py` `review_long.py` `exp_summary.py` | 人手の値との並置、長い紹介文の根拠突き合わせ表、実験の費用・一致率の表 |

## 入力

JSONL の1行が1候補: `id`, `url`, `title`, `meta_description`, `lang`, `body_excerpt`, `features`。LLM に渡るのは**この文章だけ**で、HTML は渡さない。決定論で決まるもの（ホスト、HTTP状態、広告タグ数、かな数 など）は `features` に入れるだけで、LLM に出させない。本文は `--body-limit`（既定9000字）で切る。複数ページを入れるときは `body_excerpt` 内に `--- page: URL ---` の区切りを置く。frontier 候補は本文冒頭300字が上限。

## 出力と段階

`pipeline.py` は**2段階**で動く。

1. **第1段（triage、小型モデル）**: 全候補に対し、決まったコードの短い判定を返す。`v` 判定（p pass / s suspect / r reject / d deferred）、`kd` 理由の種別、`sk` サイト種別、`ad` 成人向け（y/n/u）、`sc` 範囲（s standalone / t service_top / u user_page）、`cf` 信頼度、`cat` カテゴリid（最大3）、`r` 理由（pass で信頼度が中以上なら空、そうでなければ短い一文）。
2. **第2段（describe）**: 第1段で `pass` になった候補だけ。`dj`/`de` 短い紹介文、`lj`/`le` 長い紹介文（200〜400字目安）と `ev`（原文からの逐語引用2〜6件）、`tg` タグ、所在（`cc`/`rc`/`q`）。第2段だけ上位モデルにもできる（`--describe-model sonnet`）。

出力は構造化出力（`output_config.format = json_schema`）で固定し、**件ごとに**検証する（キー、列挙、カテゴリidが木にあること、引用が原文の部分文字列であること、`deferred` と `user_page` の整合 など）。不正な件だけを1件ずつ再試行する（打ち切りなら `max_tokens` を増やす）。第1段・第2段の結果は `judge.py` の全項目形に正規化して保存する。`--stages full` は全項目を1回で返させる基準形。

判定基準（指示文に入っている。正本は [listing-criteria.md](../concept/listing-criteria.md)）: `reject` は広告中心・量産（アフィリエイト目的等）・パーキング/売却・スパムだけ。個人/事業者/企業/大手/団体/学校/SNS本体/成人向けは理由にしない。`site_kind`（individual / business / organization / company / unknown）、`adult`、`site_scope` は属性として記録するだけ。共有サービス配下の個別ユーザーのページ（`user_page`）は `deferred`（保留）。成人向けの紹介文は中立・婉曲で、本文を再現しない。所在（国・区画）は本文に明記され、逐語引用がある場合だけ。不確かなら `null`。

## トークンを減らす設計

- 固定の英語指示文を `system` に置き、`cache_control` でプロンプトキャッシュに載せる（`--cache-ttl 1h` も可）。最小の長さは Haiku 5.5・Sonnet 5.5 とも 512 トークン（公式 prompt-caching、2026-11-13 確認）。
- 結果は**件ごとの入力ハッシュ**でキャッシュ（`var/llm-cache/`）。まとめ数を変えても同じ件は再課金されない。
- 出力キーは短いコードにし、`max_tokens` は件数に比例して絞る。思考トークンも出力として課金されるので、`--thinking-off`（Haiku は `disabled`、Sonnet は `between_tools`）と `--effort` で抑えられる（実測: 思考オフで出力トークンが約 1/3、[trials.md](trials.md#llm-判定工程の比較実験2026-11-14issue-48)）。
- 事前見積り: `--dry-run`（API を呼ばない、保守的な概算）と `--count-tokens`（`/v1/messages/count_tokens` で入力トークンを正確に数える。生成は課金されない）。
- バッチ（`--mode batch`、Message Batches API、50%）。結果はポーリングで取得し、不正な件は同期で再試行する。

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

- **台帳** `var/llm-ledger.jsonl`（追記のみ）: 呼び出しごとに input / output / cache作成 / cache読み取りのトークン、推定費用、秒数、件数。
- **予算**: 呼び出し前に「入力の保守的な見積り + `max_tokens` 分の出力」を最悪費用として、`--budget-run`（既定2 USD）と `--budget-total`（既定90 USD、台帳の累積 + バッチの予約）を超えるなら停止（終了コード3）。バッチは投入時に最悪費用を予約し、結果取得で解放して実費を記録する。
- **料金** は公式の料金表（<https://platform.claude.com/docs/en/about-claude/pricing>、2026-11-13確認）を `judge.py` の `PRICING` に設定値として持つ。Haiku 5.5 は 100K トークン以下の階層、バッチは50%。新モデルはここに足すまで使えない。
- **残高の突き合わせ**: 台帳の累積と Console の残高の減りを比べる。台帳に載らない利用があれば差になる。
- `var/` は `.gitignore`。結果JSON・キャッシュ・台帳はコミットしない。

鍵の置き場所は `~/.config/mise/anthropic.env`（リポ外、権限600、値は書かない）。クレジットの期限は **2026-12-08 00:00 UTC**（繰り越しなし）。

## 例（公開サイトの一文のみ）

原文（ELM by ChaN のプロフィールの一文）: `This site is to share my projects with electronics enthusiasts in the world.`

→ `description_ja`: 電子工作のプロジェクトを世界の電子工作愛好家と共有するためのサイト。 / `description_en`: A site for sharing electronics projects with electronics enthusiasts around the world. `ev` にはこの一文が逐語で入り、原文に無い引用は検証で弾かれる。
