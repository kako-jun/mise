-- 夜間処理の frontier / llm_judgement（収集機のローカル SQLite）を D1 へ移すための非公開表。
-- url_progress と同じ identity_key で 1 URL 1 行。外部キーは使わない（リポの流儀。孤児は夜間整合検査で見る）。
-- 非公開: 公開 API・SSR は絶対に SELECT しない（本文冒頭 text_head・判定理由・メモを含むため）。docs/design/data-model.md 参照。
-- 型は 収集機の scripts/pipeline/schema.sql に揃える（adult/fetch_adult=yes|no|unknown、confidence=low|medium|high、exempt=rare_genre|notable|none）。
-- 主キー以外はすべて NULL 可。frontier 由来の adult / site_kind は fetch_adult / fetch_site_kind（judgement 側と衝突するため）。
-- 戻し方: DROP TABLE url_candidates;（他から参照されない）
CREATE TABLE IF NOT EXISTS url_candidates (
  identity_key TEXT PRIMARY KEY,
  frontier_key TEXT, -- ローカル frontier.key（host を小文字にして先頭の www. を除いたもの。llm_judgement の結合キー）
  -- frontier
  host TEXT, url TEXT, source TEXT, source_detail TEXT, discovered_at TEXT, depth INTEGER, state TEXT,
  next_fetch_at TEXT, priority INTEGER, fetch_attempts INTEGER, last_fetched_at TEXT, last_http_status INTEGER,
  last_error TEXT, reject_reason TEXT, robots_status TEXT, final_url TEXT, title TEXT, description TEXT, lang TEXT,
  kana_count INTEGER, text_len INTEGER, ad_hits INTEGER, ext_refs INTEGER, flags TEXT,
  text_head TEXT, -- 本文冒頭。サイズが大きい（収集機側で切り詰めてから入れる）
  personal_score INTEGER, fetch_adult TEXT, fetch_site_kind TEXT, freshness_signal_at TEXT, freshness_source TEXT, hint_leaf TEXT,
  -- llm_judgement
  input_hash TEXT, origin TEXT, model TEXT, judged_at TEXT, imported_at TEXT, spam_verdict TEXT, spam_kind TEXT, spam_reason TEXT,
  description_ja TEXT, description_en TEXT, long_description_ja TEXT, long_description_en TEXT, long_description_evidence TEXT,
  categories TEXT, tags TEXT, adult TEXT, site_kind TEXT, site_kind_reason TEXT, site_scope TEXT, confidence TEXT,
  country_code TEXT, region_code TEXT, notes TEXT, review_state TEXT, reviewed_at TEXT, freshness TEXT, exempt TEXT, review_reason TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_url_candidates_state ON url_candidates(state, next_fetch_at);
CREATE INDEX IF NOT EXISTS idx_url_candidates_review ON url_candidates(review_state, judged_at);
