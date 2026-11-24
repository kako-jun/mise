-- Canonical stored key: lowercase authority without leading www/default port, plus path/query
-- exactly as written and without fragment. Path/query case and query order are preserved.
CREATE VIEW IF NOT EXISTS progress_identity_urls AS
SELECT entity,id,url,
  (CASE
    WHEN substr(authority,1,4)='www.' THEN substr(authority,5)
    ELSE authority END) ||
  (CASE WHEN suffix='' THEN '/' WHEN substr(suffix,1,1)='?' THEN '/'||suffix ELSE suffix END) AS identity_key
FROM (
  SELECT entity,id,url,lower(
    CASE
      WHEN scheme='https' AND substr(authority, length(authority)-3)=':443' THEN substr(authority,1,length(authority)-4)
      WHEN scheme='http' AND substr(authority, length(authority)-2)=':80' THEN substr(authority,1,length(authority)-3)
      ELSE authority
    END) AS authority,
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
          CASE WHEN lower(substr(clean_url,1,8))='https://' THEN 'https' ELSE 'http' END AS scheme,
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
        )
      )
    )
  )
);

ALTER TABLE requests ADD COLUMN progress_identity_key TEXT;
UPDATE requests SET progress_identity_key=(SELECT identity_key FROM progress_identity_urls WHERE entity='request' AND progress_identity_urls.id=requests.id);
CREATE INDEX IF NOT EXISTS idx_requests_progress_identity ON requests(progress_identity_key,id);
UPDATE sites SET progress_key=(SELECT identity_key FROM progress_identity_urls WHERE entity='site' AND progress_identity_urls.id=sites.id);
CREATE INDEX IF NOT EXISTS idx_sites_progress_key ON sites(progress_key);

-- D1's SQL authorizer rejects TEMP schema objects (`SQLITE_AUTH`). These short-lived
-- migration staging tables live in main and are dropped below; failed migrations roll
-- back as a unit, leaving the preceding migration applied.
CREATE TABLE _migration_0034_media_jobs_rekey AS
WITH ranked AS (
  SELECT j.*,s.progress_key AS new_key,
    ROW_NUMBER() OVER (PARTITION BY s.progress_key,j.kind ORDER BY datetime(s.registered_at) DESC,j.state='ok' DESC,j.updated_at DESC) AS rn
FROM site_media_jobs j JOIN sites s ON s.id=j.site_id
)
SELECT identity_key,site_id,kind,state,attempts,consecutive_failures,last_error,last_checked_at,next_retry_at,
  excluded,content_hash,asset_key,metadata_json,lease_owner,lease_until,updated_at,new_key
FROM ranked WHERE rn=1;
DELETE FROM site_media_jobs;
INSERT INTO site_media_jobs(identity_key,site_id,kind,state,attempts,consecutive_failures,last_error,last_checked_at,next_retry_at,
  excluded,content_hash,asset_key,metadata_json,lease_owner,lease_until,updated_at)
SELECT new_key,site_id,kind,state,attempts,consecutive_failures,last_error,last_checked_at,next_retry_at,
  excluded,content_hash,asset_key,metadata_json,lease_owner,lease_until,updated_at FROM _migration_0034_media_jobs_rekey;
DROP TABLE _migration_0034_media_jobs_rekey;

-- Merge old identity collisions by retaining a published state when present, otherwise the
-- most recently updated lifecycle row, and overlaying the newest request for the new key.
CREATE TABLE _migration_0034_progress_identity_rekey AS
WITH ranked AS (
  SELECT p.*, v.identity_key AS new_key,
    ROW_NUMBER() OVER (PARTITION BY v.identity_key ORDER BY (p.state='published') DESC, datetime(p.updated_at) DESC, p.identity_key) AS rn,
    MIN(datetime(p.discovered_at)) OVER (PARTITION BY v.identity_key) AS first_seen,
    MAX(datetime(p.updated_at)) OVER (PARTITION BY v.identity_key) AS last_seen
  FROM url_progress p JOIN progress_identity_urls v ON v.url=p.url
), latest_request AS (
  SELECT progress_identity_key, type, status, created_at,
    ROW_NUMBER() OVER (PARTITION BY progress_identity_key ORDER BY id DESC) AS rn
  FROM requests WHERE progress_identity_key IS NOT NULL
)
SELECT r.new_key AS identity_key,r.url,
  CASE WHEN q.progress_identity_key IS NOT NULL THEN 'request' ELSE r.source END AS source,
  r.state,q.type AS request_type,q.status AS request_status,
  COALESCE(r.first_seen,datetime('now')) AS discovered_at,
  MAX(COALESCE(r.last_seen,''),COALESCE(datetime(q.created_at),'')) AS updated_at
FROM ranked r LEFT JOIN latest_request q ON q.progress_identity_key=r.new_key AND q.rn=1
WHERE r.rn=1;
DELETE FROM url_progress;
INSERT INTO url_progress(identity_key,url,source,state,request_type,request_status,discovered_at,updated_at)
SELECT identity_key,url,source,state,request_type,request_status,discovered_at,updated_at FROM _migration_0034_progress_identity_rekey;
DROP TABLE _migration_0034_progress_identity_rekey;

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
