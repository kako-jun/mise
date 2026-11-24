-- Canonical stored key: lowercase authority without leading www/default port, plus path/query
-- exactly as written and without fragment. Path/query case and query order are preserved.
CREATE VIEW IF NOT EXISTS progress_identity_urls AS
SELECT entity,id,url,
  CASE WHEN authority='' OR (substr(authority,1,4)='www.' AND substr(authority,5)='')
       THEN '#malformed#'||entity||':'||id ELSE
  (CASE
    WHEN substr(authority,1,4)='www.' THEN substr(authority,5)
    ELSE authority END) ||
  (CASE WHEN suffix='' THEN '/' WHEN substr(suffix,1,1)='?' THEN '/'||suffix ELSE suffix END)
  END AS identity_key
FROM (
    SELECT entity,id,url,CASE WHEN scheme NOT IN ('http','https') THEN '' ELSE lower(
      CASE
        WHEN scheme='https' AND substr(authority, length(authority)-3)=':443' THEN substr(authority,1,length(authority)-4)
        WHEN scheme='http' AND substr(authority, length(authority)-2)=':80' THEN substr(authority,1,length(authority)-3)
        ELSE authority
      END) END AS authority,
    suffix
  FROM (
    SELECT entity,id,url,scheme,authority,
      CASE WHEN delimiter=0 THEN '' ELSE substr(rest,delimiter) END AS suffix
    FROM (
      SELECT entity,id,url,scheme,rest,
        substr(rest,1,CASE WHEN delimiter=0 THEN length(rest) ELSE delimiter-1 END) AS authority,
        delimiter
      FROM (
        SELECT entity,id,url,
          CASE WHEN lower(substr(clean_url,1,8))='https://' THEN 'https'
               WHEN lower(substr(clean_url,1,7))='http://' THEN 'http' ELSE '' END AS scheme,
          CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END AS rest,
          CASE
            WHEN instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'/')=0
             AND instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'?')=0 THEN 0
            WHEN instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'/')=0 THEN instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'?')
            WHEN instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'?')=0 THEN instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'/')
            ELSE min(instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'/'),instr(CASE WHEN lower(substr(clean_url,1,8))='https://' THEN substr(clean_url,9) ELSE substr(clean_url,8) END,'?'))
          END AS delimiter
        FROM (
          SELECT 'site' AS entity,id,url,substr(url,1,instr(url||'#','#')-1) AS clean_url FROM sites
          UNION ALL
          SELECT 'request' AS entity,id,url,substr(url,1,instr(url||'#','#')-1) AS clean_url FROM requests
          UNION ALL
          SELECT 'progress' AS entity,identity_key AS id,url,substr(url,1,instr(url||'#','#')-1) AS clean_url FROM url_progress
        )
      )
    )
  )
);

-- Materialize the expensive URL parser once, then use indexed lookups below. Correlated
-- lookups against the view repeat its sites/requests UNION for every row and exceed D1
-- CPU limits at production scale.
CREATE TABLE _migration_0034_identity_urls AS
SELECT entity,id,url,identity_key FROM progress_identity_urls;
CREATE INDEX _migration_0034_identity_entity_id ON _migration_0034_identity_urls(entity,id);
CREATE INDEX _migration_0034_identity_url ON _migration_0034_identity_urls(url);

ALTER TABLE requests ADD COLUMN progress_identity_key TEXT;
UPDATE requests SET progress_identity_key=(SELECT identity_key FROM _migration_0034_identity_urls WHERE entity='request' AND _migration_0034_identity_urls.id=requests.id);
CREATE INDEX IF NOT EXISTS idx_requests_progress_identity ON requests(progress_identity_key,id);
-- Re-keying sites must not seed duplicate canonical jobs through the 0033 AFTER UPDATE
-- trigger; the existing job rows are re-keyed in place below.
DROP TRIGGER IF EXISTS sites_media_jobs_au;
UPDATE sites SET progress_key=(SELECT identity_key FROM _migration_0034_identity_urls WHERE entity='site' AND _migration_0034_identity_urls.id=sites.id);
CREATE INDEX IF NOT EXISTS idx_sites_progress_key ON sites(progress_key);

-- D1's SQL authorizer rejects TEMP schema objects (`SQLITE_AUTH`). These short-lived
-- migration staging tables live in main and are dropped below; failed migrations roll
-- back as a unit, leaving the preceding migration applied.
CREATE TABLE _migration_0034_media_jobs_rekey AS
WITH mapped AS (
  SELECT j.identity_key AS old_identity_key,j.site_id,j.kind,s.progress_key AS new_key,
    COUNT(*) OVER (PARTITION BY s.progress_key,j.kind) AS collision_count
  FROM site_media_jobs j JOIN sites s ON s.id=j.site_id
)
SELECT old_identity_key,site_id,kind,
  CASE WHEN collision_count=1 THEN new_key
       ELSE '#collision#'||hex(CAST(old_identity_key AS BLOB))||':'||hex(CAST(site_id AS BLOB)) END AS new_key
FROM mapped;
CREATE UNIQUE INDEX _migration_0034_media_jobs_old_key ON _migration_0034_media_jobs_rekey(old_identity_key,kind);
-- Preserve every media job and all retry/result state while changing its identity. Multiple
-- jobs that normalize to one URL get injective fallback keys instead of being ranked away.
UPDATE site_media_jobs SET identity_key='#0034#'||identity_key;
UPDATE site_media_jobs AS j SET identity_key=r.new_key
  FROM _migration_0034_media_jobs_rekey r
  WHERE j.identity_key='#0034#'||r.old_identity_key AND j.site_id=r.site_id AND j.kind=r.kind;
DROP TABLE _migration_0034_media_jobs_rekey;

-- Re-key existing lifecycle rows without discarding collision members. Unique mappings
-- retain their canonical key; colliding mappings get an injective fallback key.
CREATE TABLE _migration_0034_progress_identity_rekey AS
WITH mapped AS (
  SELECT p.*, v.identity_key AS new_key,
    COUNT(*) OVER (PARTITION BY v.identity_key) AS collision_count
  FROM url_progress p JOIN _migration_0034_identity_urls v
    ON v.entity='progress' AND v.id=p.identity_key
), latest_request AS (
  SELECT progress_identity_key, type, status, created_at,
    ROW_NUMBER() OVER (PARTITION BY progress_identity_key ORDER BY id DESC) AS rn
  FROM requests WHERE progress_identity_key IS NOT NULL
)
SELECT CASE WHEN r.collision_count=1 THEN r.new_key ELSE '#collision#'||r.identity_key END AS identity_key,
  r.new_key AS canonical_key,r.collision_count,
  ROW_NUMBER() OVER (PARTITION BY r.new_key ORDER BY (r.state='published') DESC,datetime(r.updated_at) DESC,r.identity_key) AS summary_rank,
  r.identity_key AS old_identity_key,r.url,
  CASE WHEN r.collision_count=1 AND q.progress_identity_key IS NOT NULL THEN 'request' ELSE r.source END AS source,
  r.state,
  CASE WHEN r.collision_count=1 THEN q.type ELSE r.request_type END AS request_type,
  CASE WHEN r.collision_count=1 THEN q.status ELSE r.request_status END AS request_status,
  q.type AS summary_request_type,q.status AS summary_request_status,q.created_at AS summary_request_created_at,
  COALESCE(r.discovered_at,datetime('now')) AS discovered_at,
  CASE WHEN r.collision_count=1
       THEN MAX(COALESCE(datetime(r.updated_at),''),COALESCE(datetime(q.created_at),''))
       ELSE COALESCE(r.updated_at,datetime('now')) END AS updated_at
FROM mapped r LEFT JOIN latest_request q ON q.progress_identity_key=r.new_key AND q.rn=1;
CREATE UNIQUE INDEX _migration_0034_progress_winner_old_key ON _migration_0034_progress_identity_rekey(old_identity_key);
CREATE TABLE _migration_0034_progress_collision_summary AS
WITH grouped AS (
  SELECT canonical_key,MIN(datetime(discovered_at)) AS first_seen,MAX(datetime(updated_at)) AS last_seen
  FROM _migration_0034_progress_identity_rekey WHERE collision_count>1 GROUP BY canonical_key
)
SELECT r.canonical_key AS identity_key,
  COALESCE((SELECT s.url FROM sites s WHERE s.progress_key=r.canonical_key ORDER BY datetime(s.registered_at) DESC LIMIT 1),r.url) AS url,
  CASE WHEN r.summary_request_type IS NOT NULL THEN 'request' ELSE r.source END AS source,
  CASE WHEN EXISTS(SELECT 1 FROM sites s WHERE s.progress_key=r.canonical_key) THEN 'published' ELSE r.state END AS state,
  r.summary_request_type AS request_type,r.summary_request_status AS request_status,
  COALESCE(g.first_seen,datetime('now')) AS discovered_at,
  MAX(COALESCE(g.last_seen,''),COALESCE(datetime(r.summary_request_created_at),'')) AS updated_at
FROM _migration_0034_progress_identity_rekey r JOIN grouped g ON g.canonical_key=r.canonical_key
WHERE r.collision_count>1 AND r.summary_rank=1;

-- Move every existing key out of the target namespace first, then update each original row
-- in place. When several old rows normalize to one URL, keep every row under an injective
-- reserved fallback key; do not discard even a canonical alias.
UPDATE url_progress SET identity_key='#0034#'||identity_key;
UPDATE url_progress AS p SET identity_key=r.identity_key,url=r.url,source=r.source,state=r.state,
    request_type=r.request_type,request_status=r.request_status,discovered_at=r.discovered_at,updated_at=r.updated_at
  FROM _migration_0034_progress_identity_rekey r
  WHERE p.identity_key='#0034#'||r.old_identity_key;
INSERT INTO url_progress(identity_key,url,source,state,request_type,request_status,discovered_at,updated_at)
SELECT identity_key,url,source,state,request_type,request_status,discovered_at,updated_at
FROM _migration_0034_progress_collision_summary;
DROP TABLE _migration_0034_progress_collision_summary;
DROP TABLE _migration_0034_progress_identity_rekey;
DROP TABLE _migration_0034_identity_urls;

DROP TRIGGER IF EXISTS requests_progress_ai;
DROP TRIGGER IF EXISTS requests_progress_au;
DROP TRIGGER IF EXISTS sites_progress_ai;
CREATE TRIGGER requests_progress_ai AFTER INSERT ON requests BEGIN
  UPDATE requests SET progress_identity_key=(SELECT identity_key FROM progress_identity_urls WHERE entity='request' AND id=NEW.id)
    WHERE id=NEW.id;
  INSERT INTO url_progress(identity_key,url,source,state,request_type,request_status,discovered_at,updated_at)
  SELECT progress_identity_key,NEW.url,'request','discovered',NEW.type,NEW.status,NEW.created_at,NEW.created_at
    FROM requests WHERE id=NEW.id
  ON CONFLICT(identity_key) DO UPDATE SET source='request',request_type=excluded.request_type,
    request_status=excluded.request_status,updated_at=excluded.updated_at;
END;
CREATE TRIGGER requests_progress_au AFTER UPDATE OF status ON requests BEGIN
  UPDATE url_progress SET request_type=NEW.type,request_status=NEW.status,
    updated_at=COALESCE(NEW.decided_at,datetime('now'))
  WHERE identity_key=NEW.progress_identity_key
    AND NEW.id=(SELECT MAX(id) FROM requests WHERE progress_identity_key=NEW.progress_identity_key);
END;
CREATE TRIGGER sites_progress_ai AFTER INSERT ON sites BEGIN
  UPDATE sites SET progress_key=(SELECT identity_key FROM progress_identity_urls WHERE entity='site' AND id=NEW.id) WHERE id=NEW.id;
  INSERT INTO url_progress(identity_key,url,source,state,discovered_at,updated_at)
  SELECT progress_key,url,'discovered','published',COALESCE(registered_at,datetime('now')),COALESCE(registered_at,datetime('now'))
  FROM sites WHERE id=NEW.id
  ON CONFLICT(identity_key) DO UPDATE SET state='published',updated_at=excluded.updated_at;
END;
CREATE TRIGGER sites_media_jobs_au AFTER UPDATE OF progress_key ON sites WHEN NEW.progress_key IS NOT NULL BEGIN
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key)
  VALUES(NEW.progress_key,NEW.id,'favicon',CASE WHEN NEW.favicon_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.favicon_path);
  INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key)
  VALUES(NEW.progress_key,NEW.id,'thumbnail',CASE WHEN NEW.thumb_path IS NULL THEN 'pending' ELSE 'ok' END,NEW.thumb_path);
END;
