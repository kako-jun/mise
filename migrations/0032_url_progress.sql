-- URL ごとの公開用進捗表。申請と自動発見を同じ URL 行にまとめ、出自は source に残す。
-- 審査理由・本文・連絡先などは保存せず、処理段階だけを公開する。
CREATE TABLE IF NOT EXISTS url_progress (
  url TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'discovered' CHECK (source IN ('discovered','request')),
  state TEXT NOT NULL DEFAULT 'discovered' CHECK (state IN ('discovered','queued','fetched','candidate','reviewing','approved','rejected','published','failed','blocked')),
  request_type TEXT CHECK (request_type IN ('add','remove','correct')),
  request_status TEXT CHECK (request_status IN ('pending','approved','rejected')),
  discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_url_progress_updated ON url_progress(updated_at DESC, url);
-- 既存申請も URL ごとに最新の 1 件だけ取り込む。理由・連絡先は進捗表へ移さない。
INSERT OR IGNORE INTO url_progress(url,source,state,request_type,request_status,discovered_at,updated_at)
SELECT r.url,'request',CASE WHEN r.type='add' AND r.status='rejected' THEN 'rejected'
  WHEN r.type='add' AND r.status='approved' THEN 'approved' ELSE 'discovered' END,
  r.type,r.status,r.created_at,COALESCE(r.decided_at,r.created_at)
FROM requests r WHERE r.id=(SELECT MAX(r2.id) FROM requests r2 WHERE r2.url=r.url);
CREATE TRIGGER IF NOT EXISTS requests_progress_ai AFTER INSERT ON requests BEGIN
  INSERT INTO url_progress(url,source,state,request_type,request_status,discovered_at,updated_at)
  VALUES(NEW.url,'request',CASE WHEN NEW.type='add' THEN CASE NEW.status WHEN 'rejected' THEN 'rejected' WHEN 'approved' THEN 'approved' ELSE 'discovered' END ELSE 'discovered' END,NEW.type,NEW.status,NEW.created_at,NEW.created_at)
  ON CONFLICT(url) DO UPDATE SET source=CASE WHEN url_progress.source='request' THEN 'request' ELSE url_progress.source END,
    request_type=excluded.request_type,request_status=excluded.request_status,
    state=CASE WHEN url_progress.state='published' THEN 'published' ELSE excluded.state END,updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS requests_progress_au AFTER UPDATE OF status ON requests BEGIN
  UPDATE url_progress SET request_type=NEW.type,request_status=NEW.status,
    state=CASE WHEN url_progress.state='published' THEN 'published' WHEN NEW.type!='add' THEN url_progress.state
      WHEN NEW.status='rejected' THEN 'rejected' WHEN NEW.status='approved' THEN 'approved' ELSE 'discovered' END,
    updated_at=COALESCE(NEW.decided_at,datetime('now')) WHERE url=NEW.url;
END;
