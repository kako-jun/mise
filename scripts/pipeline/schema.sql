-- frontier 台帳（正本）。実体は var/frontier.sqlite（.gitignore。再生成可能）。
-- 単位はホスト（キー = 先頭の www. を除いた小文字ホスト名）。
CREATE TABLE IF NOT EXISTS frontier (
  key            TEXT PRIMARY KEY,
  host           TEXT NOT NULL,            -- 観測した形（www. 付きの場合あり）
  url            TEXT NOT NULL,            -- 取得するトップURL
  source         TEXT NOT NULL,            -- commoncrawl-webgraph | links | ...
  source_detail  TEXT,                     -- 出典の詳細（グラフのリリース名、リンク元URL 等）
  discovered_at  TEXT NOT NULL,            -- UTC ISO8601
  depth          INTEGER NOT NULL DEFAULT 0,
  state          TEXT NOT NULL DEFAULT 'discovered'
                 CHECK (state IN ('discovered','queued','fetched','failed','blocked_by_robots','rejected','candidate','deferred')),
  next_fetch_at  TEXT,
  priority       INTEGER NOT NULL DEFAULT 0,
  fetch_attempts INTEGER NOT NULL DEFAULT 0,
  last_fetched_at TEXT,
  last_http_status INTEGER,
  last_error     TEXT,
  reject_reason  TEXT,
  robots_status  TEXT,                     -- allowed | allowed_no_robots | disallowed | unavailable
  final_url      TEXT,
  title          TEXT,
  description    TEXT,
  lang           TEXT,
  kana_count     INTEGER,
  text_len       INTEGER,
  ad_hits        INTEGER,
  ext_refs       INTEGER,
  flags          TEXT,                     -- JSON: noindex, parking, adult 等のマーカー
  text_head      TEXT,                     -- 本文冒頭（最大300字）
  personal_score INTEGER,                  -- 手作り感の語の出現数（並べ替えの目安のみ。判定に使わない）
  adult          TEXT,                     -- yes|no|unknown。属性のみ（拒否に使わない）。決定論は yes/unknown まで、no は後段
  site_kind      TEXT,                     -- 個人|事業|団体|企業|不明（属性。拒否には使わない。決定論で分かる範囲のみ）
  freshness_signal_at TEXT,                -- 更新の兆候の最新日 YYYY-MM-DD（#129。header/footer/sitemap/feed のうち最新。無ければ NULL）
  freshness_source    TEXT                 -- header | footer | sitemap | feed
);
CREATE INDEX IF NOT EXISTS frontier_state_next ON frontier (state, next_fetch_at, priority DESC);
-- どこまで探したか（発見元ごとのカーソル）
CREATE TABLE IF NOT EXISTS source_cursor (
  source TEXT NOT NULL, cursor_key TEXT NOT NULL, cursor_value TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (source, cursor_key)
);
-- LLM 判定結果（Issue #48）。scripts/llm/import_results.py が judge/pipeline の出力を取り込む。1ホスト1行（最新の判定だけ）。
-- frontier の列とは別表: frontier.adult / site_kind は決定論の属性、ここは LLM の判定（出自が違うので混ぜない）。
-- frontier.state は変えない（判定は候補であり、公開は review_state が approved になってから）。FK は付けない（frontier の表作り直しで参照が壊れないように）。
CREATE TABLE IF NOT EXISTS llm_judgement (
  key            TEXT PRIMARY KEY,         -- frontier.key
  input_hash     TEXT NOT NULL,            -- 取り込み時に作るハッシュ（origin + モデル + LLM に渡した候補テキスト）。同じなら二重に取り込まない
  origin         TEXT NOT NULL CHECK (origin IN ('pipeline','judge')),
  model          TEXT NOT NULL,            -- 使ったモデル（pipeline の二段で第2段が違うときは 'haiku+sonnet' の形）
  judged_at      TEXT NOT NULL,            -- 判定結果ファイルの更新日時（UTC。API の応答時刻ではない）
  imported_at    TEXT NOT NULL,
  spam_verdict   TEXT NOT NULL CHECK (spam_verdict IN ('pass','suspect','reject','deferred')),
  spam_kind      TEXT NOT NULL,
  spam_reason    TEXT NOT NULL,
  description_ja TEXT,
  description_en TEXT,
  long_description_ja TEXT,                -- adult=yes では保存しない（本文の再現を避ける）
  long_description_en TEXT,
  long_description_evidence TEXT,          -- JSON 配列（原文からの逐語引用）。adult=yes では保存しない
  categories     TEXT NOT NULL,            -- JSON 配列（data/categories.csv の木の id、最大3。#114 より前の判定は旧 59 カテゴリの id が残り、judge.modernize_categories が書き出し時に写す）
  tags           TEXT NOT NULL,            -- JSON 配列（最大5）
  adult          TEXT NOT NULL CHECK (adult IN ('yes','no','unknown')),   -- 根拠引用は保存しない
  site_kind      TEXT NOT NULL CHECK (site_kind IN ('individual','business','organization','company','unknown')),
  site_kind_reason TEXT NOT NULL,
  site_scope     TEXT NOT NULL CHECK (site_scope IN ('standalone','service_top','user_page')),
  confidence     TEXT NOT NULL CHECK (confidence IN ('low','medium','high')),
  country_code   TEXT,                     -- 本文に根拠のあるものだけ。根拠引用は保存しない
  region_code    TEXT,
  notes          TEXT NOT NULL DEFAULT '',
  review_state   TEXT NOT NULL DEFAULT 'pending' CHECK (review_state IN ('pending','approved','rejected')),
  reviewed_at    TEXT,
  freshness      TEXT CHECK (freshness IN ('active','stale','unknown')),      -- #129。既存行は NULL（未判定）
  exempt         TEXT CHECK (exempt IN ('rare_genre','notable','none')),
  review_reason  TEXT                      -- approve_policy.py が自動で rejected にした理由（stale_individual）。人の操作では NULL
);
CREATE INDEX IF NOT EXISTS llm_judgement_review ON llm_judgement (review_state, spam_verdict);
