-- 再巡回(Issue #2)の結果。scripts/recrawl/recrawl.py sql が生成する INSERT の受け皿。
-- 表示側はまだ読まない(フィード等からの除外は人が候補を確認してから別途)。FK は付けない(台帳側の id と独立に流せるように)。
CREATE TABLE site_checks (
  site_id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'listed',        -- listed | needs_review | hide_candidate
  flag TEXT,                                   -- 由来: offsite(別ドメイン転送) | ssrf(内部アドレス)。人が confirm するまで残る
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT, last_ok_at TEXT, last_kind TEXT, last_http_status INTEGER,
  title TEXT, content_hash TEXT, next_check_at TEXT
);
