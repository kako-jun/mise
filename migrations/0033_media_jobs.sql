-- Durable media acquisition state. `sites` remains the published projection; this table
-- owns retry/exclusion/result state so runners do not depend on machine-local ledgers.
CREATE TABLE IF NOT EXISTS site_media_jobs (
  identity_key TEXT NOT NULL,
  site_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('favicon','thumbnail')),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','ok','none','failed','excluded')),
  attempts INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  last_checked_at TEXT,
  next_retry_at TEXT,
  excluded INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0,1)),
  content_hash TEXT,
  asset_key TEXT,
  metadata_json TEXT,
  lease_owner TEXT,
  lease_until TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (identity_key, kind),
  FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_site_media_jobs_due
  ON site_media_jobs(kind, excluded, next_retry_at, state);
CREATE TABLE IF NOT EXISTS site_media_daily_counts (
  kind TEXT NOT NULL CHECK (kind IN ('favicon','thumbnail')),
  day TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  reserved INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(kind,day)
);

-- Seed current sites as jobs. Existing published pointers count as completed; the import
-- command later fills state/hash/asset details from the legacy ledgers after remote proof.
INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,updated_at)
SELECT progress_key,id,'favicon',CASE WHEN favicon_path IS NULL THEN 'pending' ELSE 'ok' END,
       favicon_path,COALESCE(registered_at,datetime('now')) FROM sites WHERE progress_key IS NOT NULL;
INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,updated_at)
SELECT progress_key,id,'thumbnail',CASE WHEN thumb_path IS NULL THEN 'pending' ELSE 'ok' END,
       thumb_path,COALESCE(registered_at,datetime('now')) FROM sites WHERE progress_key IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS sites_media_jobs_au AFTER UPDATE OF progress_key ON sites WHEN NEW.progress_key IS NOT NULL BEGIN
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key)
  VALUES(NEW.progress_key,NEW.id,'favicon',CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.favicon_path);
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key)
  VALUES(NEW.progress_key,NEW.id,'thumbnail',CASE WHEN NEW.thumb_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.thumb_path);
END;
CREATE TRIGGER IF NOT EXISTS sites_media_jobs_ai AFTER INSERT ON sites WHEN NEW.progress_key IS NOT NULL BEGIN
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key)
  VALUES(NEW.progress_key,NEW.id,'favicon',CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.favicon_path);
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key)
  VALUES(NEW.progress_key,NEW.id,'thumbnail',CASE WHEN NEW.thumb_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.thumb_path);
END;
CREATE TRIGGER IF NOT EXISTS sites_media_jobs_media_au AFTER UPDATE OF favicon_path,thumb_path ON sites BEGIN
  UPDATE site_media_jobs SET state=CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,
    asset_key=NEW.favicon_path,updated_at=datetime('now')
    WHERE site_id=NEW.id AND kind='favicon' AND (asset_key IS NOT NEW.favicon_path OR state='pending');
  UPDATE site_media_jobs SET state=CASE WHEN NEW.thumb_path IS NULL THEN 'pending' ELSE 'ok' END,
    asset_key=NEW.thumb_path,updated_at=datetime('now')
    WHERE site_id=NEW.id AND kind='thumbnail' AND (asset_key IS NOT NEW.thumb_path OR state='pending');
END;
