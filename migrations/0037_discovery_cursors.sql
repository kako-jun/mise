-- URL 発見ランナー(scripts/discover_online.py)の続きの位置と、同時実行を防ぐリース。非公開表。
-- 公開 API・SSR は参照しない。ローカルに状態を持たないため、続きの位置はここだけに置く。
--   source='wikidata'  cursor_key='<leaf>:<rel>:<qid>'  cursor_value='<次の OFFSET>' または 'done'
--   source='lease'     cursor_key='discover'            cursor_value='<owner>|<期限の epoch 秒>'
CREATE TABLE IF NOT EXISTS discovery_cursors (
  source TEXT NOT NULL,
  cursor_key TEXT NOT NULL,
  cursor_value TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, cursor_key)
);
