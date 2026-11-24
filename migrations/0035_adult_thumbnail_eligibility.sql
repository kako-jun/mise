-- Keep the existing thumbnail policy durable: only an approved adult=yes judgement
-- excludes thumbnail collection. Unknown classifications remain eligible as before.
ALTER TABLE sites ADD COLUMN adult_content INTEGER CHECK (adult_content IN (0,1));
ALTER TABLE site_media_jobs ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0 CHECK (eligible IN (0,1));

-- Existing thumbnail jobs stay unclaimable until the approved-classification export
-- synchronizes known adult flags and runs its final unknown-is-eligible backfill.
UPDATE site_media_jobs SET eligible=0 WHERE kind='thumbnail';
UPDATE site_media_jobs SET state='excluded'
  WHERE kind='thumbnail' AND excluded=0
    AND (SELECT adult_content FROM sites WHERE sites.id=site_media_jobs.site_id)=1
    AND state IN ('pending','failed','none');

DROP TRIGGER IF EXISTS sites_media_jobs_ai;
CREATE TRIGGER sites_media_jobs_ai AFTER INSERT ON sites WHEN NEW.progress_key IS NOT NULL BEGIN
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,eligible)
  VALUES(NEW.progress_key,NEW.id,'favicon',CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.favicon_path,1);
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,eligible)
  VALUES(NEW.progress_key,NEW.id,'thumbnail',
    CASE WHEN NEW.thumb_path IS NOT NULL THEN 'ok' WHEN NEW.adult_content=1 THEN 'excluded' ELSE 'pending' END,
    NEW.thumb_path,CASE WHEN NEW.adult_content=1 THEN 0 ELSE 1 END);
END;

DROP TRIGGER IF EXISTS sites_media_jobs_au;
CREATE TRIGGER sites_media_jobs_au AFTER UPDATE OF progress_key ON sites WHEN NEW.progress_key IS NOT NULL BEGIN
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,eligible)
  VALUES(NEW.progress_key,NEW.id,'favicon',CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.favicon_path,1);
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,eligible)
  VALUES(NEW.progress_key,NEW.id,'thumbnail',
    CASE WHEN NEW.thumb_path IS NOT NULL THEN 'ok' WHEN NEW.adult_content=1 THEN 'excluded' ELSE 'pending' END,
    NEW.thumb_path,CASE WHEN NEW.adult_content=1 THEN 0 ELSE 1 END);
END;

CREATE TRIGGER sites_media_adult_au AFTER UPDATE OF adult_content ON sites BEGIN
  UPDATE site_media_jobs SET eligible=CASE WHEN NEW.adult_content=1 THEN 0 ELSE 1 END,
    state=CASE
      WHEN NEW.thumb_path IS NOT NULL THEN 'ok'
      WHEN NEW.adult_content=1 THEN 'excluded'
      WHEN OLD.adult_content=1 THEN 'pending'
      ELSE state END,
    updated_at=datetime('now')
  WHERE site_id=NEW.id AND kind='thumbnail';
END;

DROP TRIGGER IF EXISTS sites_media_jobs_media_au;
CREATE TRIGGER sites_media_jobs_media_au AFTER UPDATE OF favicon_path,thumb_path ON sites BEGIN
  UPDATE site_media_jobs SET state=CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,
    asset_key=NEW.favicon_path,updated_at=datetime('now')
    WHERE site_id=NEW.id AND kind='favicon' AND (asset_key IS NOT NEW.favicon_path OR state='pending');
  UPDATE site_media_jobs SET state=CASE
      WHEN NEW.thumb_path IS NOT NULL THEN 'ok'
      WHEN NEW.adult_content=1 THEN 'excluded'
      ELSE 'pending' END,
    eligible=CASE WHEN NEW.adult_content=1 THEN 0 ELSE 1 END,
    asset_key=NEW.thumb_path,updated_at=datetime('now')
    WHERE site_id=NEW.id AND kind='thumbnail'
      AND (asset_key IS NOT NEW.thumb_path OR state='pending' OR eligible<>(CASE WHEN NEW.adult_content=1 THEN 0 ELSE 1 END));
END;
