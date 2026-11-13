# LLM判定工程（Claude API、手元実行）

候補サイトの取得文を Claude に渡し、紹介文・掲載可否の一次判定・カテゴリ候補・属性を**候補として**出力させる工程（Issue #48）。公開サーバーからは呼ばない。手元PC（将来は Raspberry Pi）だけで動かし、出力は人の承認を経るまで公開しない。実装は `scripts/llm/`、テストは `tests/test_llm_judge.py`。

## 構成

| ファイル | 役割 |
|---|---|
| `scripts/llm/judge.py` | 本体。JSONLを読み、判定・検証・キャッシュ・予算管理・単発/バッチ実行・ドライラン |
| `scripts/llm/export_sites.py` | 既掲載サイト（`migrations/` を in-memory SQLite に適用）と `data/` の取得結果から入力JSONLと、比較用の人手の値を書き出す。新規アクセスなし |
| `scripts/llm/export_candidates.py` | 発見パイプラインの frontier（`state='candidate'`）を読み取り専用で入力JSONLにする |
| `scripts/llm/fetch_about.py` | トップと About 系ページ（最大2、同一ホスト、robots遵守、2秒間隔）を取得して `body_excerpt` に入れる決定論の補助 |
| `scripts/llm/compare.py`, `review_long.py` | 人手の値との並置、長い紹介文の根拠突き合わせ表 |

## 入力

JSONLの1行が1候補: `id`, `url`, `title`, `meta_description`, `lang`, `body_excerpt`, `features`。LLMに渡るのは**この文章だけ**（Webには出ない）。本文は `--body-limit`（既定9000字）で切る。複数ページを入れるときは `body_excerpt` 内に `--- page: URL ---` の区切りを置く。

## 出力（JSONスキーマ固定）

構造化出力（`output_config.format = json_schema`）で次を返させ、さらに `judge.py` が検証する。不正（スキーマ違反、`max_tokens` 打ち切り、拒否、検証エラー）なら**1回だけ**再試行する（打ち切りの場合は `max_tokens` を2倍）。

- `description_ja` / `description_en`: 同じ事実だけの一文。根拠が無ければ両方 `null`
- `long_description_ja` / `long_description_en` / `long_description_evidence`: 200〜400字目安の長い紹介文（サイトの自己紹介に基づく）と、原文からの**逐語引用**2〜6件。引用が原文の部分文字列でなければ不正
- `spam`: `verdict`（`pass` / `suspect` / `reject` / `deferred`）、`kind`、`reason`。`reject` にするのは広告中心・量産（アフィリエイト目的等）・パーキング/売却・スパムだけ。事業者・団体・企業・大手・成人向けは理由にしない。`deferred` は共有サービス上の個別ユーザーのページ（`site_scope = user_page`）のみ
- `adult`: `yes` / `no` / `unknown`（`yes` は逐語引用つき）。成人向けでも紹介文は中立・婉曲
- `site_scope`: `standalone` / `service_top` / `user_page`、`site_kind`: `individual` / `business` / `organization` / `company` / `unknown`（属性。拒否の理由にしない）
- `categories`: `migrations/0001` のカテゴリidだけ（スキーマの enum は同ファイルから生成）、`tags`, `confidence`（low/medium/high）
- `location`: `country_code`, `region_code`, `evidence_quote`。本文に運営者の所在が明記され、その逐語引用がある場合だけ。ドメイン・言語・ホスティングは根拠にしない。不確かなら `null`

システム指示（英語・固定）はスキーマと合わせて約4,500トークンで、`cache_control` を付けてプロンプトキャッシュに載せる（`--cache-ttl 1h` も可。バッチは1h推奨）。

## 使い方

```sh
set -a; . ~/.config/mise/anthropic.env; set +a      # キーは環境変数でのみ渡す
uv run python3 scripts/llm/export_sites.py          # var/sites-candidates.jsonl, var/sites-reference.json
uv run python3 scripts/llm/judge.py var/sites-candidates.jsonl --model haiku --dry-run   # 見積りのみ（APIを呼ばない）
uv run python3 scripts/llm/judge.py var/sites-candidates.jsonl --model haiku --model sonnet --budget-run 1.0
uv run python3 scripts/llm/judge.py in.jsonl --mode batch --model haiku --cache-ttl 1h   # Message Batches API（結果をポーリング、--batch-resume ID で再開）
uv run python3 -m unittest discover tests          # キー不要の単体テスト
```

`--model` は `haiku`（`claude-haiku-5-5`）、`sonnet`（`claude-sonnet-5-5`）または完全なモデルID（料金表に登録済みのもの）。Opusは使わない。`--escalate-model sonnet` を付けると、単発モードで信頼度 low または `pass` 以外の結果を上位モデルで再判定する。社内プロキシ等でCAが違う環境は `SSL_CERT_FILE` にCAバンドルを指定する（検証は有効のまま）。

## キャッシュと予算ガード

- **結果キャッシュ**: `var/llm-cache/`。キーは モデル + システム指示 + スキーマ + 候補の描画テキスト + `max_tokens` + effort のハッシュ。同じ入力の再実行は課金されない。
- **台帳**: `var/llm-ledger.jsonl`（追記のみ）に呼び出しごとの input / output / cache作成 / cache読み取りトークン、推定費用、秒数を記録。
- **予算**: 呼び出し前に「入力の保守的な見積り + `max_tokens` 分の出力」を最悪費用として、`--budget-run`（既定2 USD）と `--budget-total`（既定90 USD、台帳の累積 + バッチの予約）を超えるなら停止（終了コード3）。バッチは投入時に最悪費用を台帳へ予約し、結果取得で解放して実費を記録する。
- **料金**: 公式の料金表（<https://platform.claude.com/docs/en/about-claude/pricing>、2026-11-13確認）を `judge.py` の `PRICING` に設定値として持つ（Haiku 5.5 は 100K トークン以下の階層、バッチは50%）。新モデルはここに足すまで使えない。
- **残高の突き合わせ**: 台帳の累積と Console の残高の減りを定期的に比べる。台帳に載らない利用（他のスクリプト等）があれば差になる。
- `var/` は `.gitignore`。結果JSON・キャッシュ・台帳はコミットしない。

## キーとクレジット

- キーは `~/.config/mise/anthropic.env`（リポ外、権限600）に `ANTHROPIC_API_KEY=...` として置く。**値をログ・出力・コミット・Issue/PRに出さない**。スクリプトは環境変数からのみ読み、リポ内に `.env` を作らない。APIのエラー応答は本文だけを表示する。
- 月額プロモーションクレジットの期限は **2026-12-08 00:00 UTC**（繰り越しなし）。全体上限を90 USDとし、残り10 USDを予備とする。クレジットは失効するので、費用より品質を優先してよい（上位モデルを積極的に使う）。

## 例（公開サイトの一文のみ）

原文（ELM by ChaN のプロフィールページの一文）: `This site is to share my projects with electronics enthusiasts in the world.`

→ `description_ja`: 電子工作のプロジェクトを世界の電子工作愛好家と共有するためのサイト。 / `description_en`: A site for sharing electronics projects with electronics enthusiasts around the world。 `long_description_evidence` にはこの一文が逐語で入り、原文に無い引用は検証で弾かれる。
