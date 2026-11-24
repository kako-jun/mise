-- URL ごとの公開用進捗表。申請と自動発見を同じ URL 行にまとめ、出自は source に残す。
-- 審査理由・本文・連絡先などは保存せず、処理段階だけを公開する。
CREATE TABLE IF NOT EXISTS url_progress (
  identity_key TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'discovered' CHECK (source IN ('discovered','request')),
  state TEXT NOT NULL DEFAULT 'discovered' CHECK (state IN ('discovered','queued','fetched','candidate','reviewing','approved','rejected','published','failed','blocked')),
  request_type TEXT CHECK (request_type IN ('add','remove','correct')),
  request_status TEXT CHECK (request_status IN ('pending','approved','rejected')),
  discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_url_progress_updated ON url_progress(updated_at DESC, identity_key);
-- D1 stores the canonical identity on each catalog site so crawler, requests, and the public table join on one URL key.
ALTER TABLE sites ADD COLUMN progress_key TEXT;
UPDATE sites SET progress_key=lower(rtrim(CASE
  WHEN lower(substr(url,1,12))='https://www.' THEN substr(url,13)
  WHEN lower(substr(url,1,11))='http://www.' THEN substr(url,12)
  WHEN lower(substr(url,1,8))='https://' THEN substr(url,9)
  WHEN lower(substr(url,1,7))='http://' THEN substr(url,8)
  ELSE url END,'/'));
CREATE INDEX IF NOT EXISTS idx_sites_progress_key ON sites(progress_key);
-- 既存の公開済みサイトを共通進捗行へ移す。
INSERT OR IGNORE INTO url_progress(identity_key,url,source,state,discovered_at,updated_at)
SELECT progress_key,url,'discovered','published',registered_at,registered_at FROM sites WHERE progress_key IS NOT NULL;
-- 既存申請も URL ごとに最新の 1 件だけ取り込む。理由・連絡先は進捗表へ移さない。
INSERT INTO url_progress(identity_key,url,source,state,request_type,request_status,discovered_at,updated_at)
SELECT r.normalized_key,r.url,'request','discovered',r.type,r.status,r.created_at,COALESCE(r.decided_at,r.created_at)
FROM requests r WHERE r.id=(SELECT MAX(r2.id) FROM requests r2 WHERE r2.normalized_key=r.normalized_key)
ON CONFLICT(identity_key) DO UPDATE SET request_type=excluded.request_type,request_status=excluded.request_status,
  updated_at=excluded.updated_at;
CREATE TRIGGER IF NOT EXISTS requests_progress_ai AFTER INSERT ON requests BEGIN
  INSERT INTO url_progress(identity_key,url,source,state,request_type,request_status,discovered_at,updated_at)
  VALUES(NEW.normalized_key,NEW.url,'request','discovered',NEW.type,NEW.status,NEW.created_at,NEW.created_at)
  ON CONFLICT(identity_key) DO UPDATE SET request_type=excluded.request_type,request_status=excluded.request_status,
    updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS requests_progress_au AFTER UPDATE OF status ON requests BEGIN
  UPDATE url_progress SET request_type=NEW.type,request_status=NEW.status,
    updated_at=COALESCE(NEW.decided_at,datetime('now')) WHERE identity_key=NEW.normalized_key;
END;
CREATE TRIGGER IF NOT EXISTS sites_progress_ai AFTER INSERT ON sites BEGIN
  UPDATE sites SET progress_key=lower(rtrim(CASE
    WHEN lower(substr(NEW.url,1,12))='https://www.' THEN substr(NEW.url,13)
    WHEN lower(substr(NEW.url,1,11))='http://www.' THEN substr(NEW.url,12)
    WHEN lower(substr(NEW.url,1,8))='https://' THEN substr(NEW.url,9)
    WHEN lower(substr(NEW.url,1,7))='http://' THEN substr(NEW.url,8)
    ELSE NEW.url END,'/')) WHERE id=NEW.id;
  INSERT INTO url_progress(identity_key,url,source,state,discovered_at,updated_at)
  SELECT progress_key,url,'discovered','published',registered_at,registered_at FROM sites WHERE id=NEW.id
  ON CONFLICT(identity_key) DO UPDATE SET url=excluded.url,state='published',updated_at=excluded.updated_at;
END;
