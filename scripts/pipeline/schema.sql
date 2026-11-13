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
                 CHECK (state IN ('discovered','queued','fetched','failed','blocked_by_robots','rejected','candidate')),
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
  site_kind      TEXT                      -- 個人|事業|団体|企業|不明（属性。拒否には使わない。決定論で分かる範囲のみ）
);
CREATE INDEX IF NOT EXISTS frontier_state_next ON frontier (state, next_fetch_at, priority DESC);
-- どこまで探したか（発見元ごとのカーソル）
CREATE TABLE IF NOT EXISTS source_cursor (
  source TEXT NOT NULL, cursor_key TEXT NOT NULL, cursor_value TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (source, cursor_key)
);
