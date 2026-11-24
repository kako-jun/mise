import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const files = readdirSync(new URL('../migrations/', import.meta.url)).filter(f => f.endsWith('.sql')).sort();
function migrated({ through = null } = {}) {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const file of files) {
    if (through && file > through) break;
    db.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), 'utf8'));
  }
  return db;
}

test('media jobs seed from published site pointers and follow later site media updates', () => {
  const db = migrated();
  db.prepare(`INSERT INTO sites(id,url,name,description_ja,registered_at,favicon_path,thumb_path,thumb_lqip,thumb_captured_at)
    VALUES(?,?,?,?,?,?,?,?,?)`).run('existing', 'https://example.com/a%20b', 'existing', '説明', null,
      'known-icon.webp', 'known-thumb.webp', 'data:image/webp;base64,x', '2026-11-24T00:00:00Z');
  const existing = db.prepare('SELECT kind,state,asset_key FROM site_media_jobs WHERE site_id=? ORDER BY kind').all('existing').map(r => ({ ...r }));
  assert.deepEqual(existing, [
    { kind: 'favicon', state: 'ok', asset_key: 'known-icon.webp' },
    { kind: 'thumbnail', state: 'ok', asset_key: 'known-thumb.webp' },
  ]);
  db.prepare("UPDATE sites SET favicon_path=NULL,thumb_path=NULL WHERE id='existing'").run();
  const pending = db.prepare('SELECT kind,state,asset_key FROM site_media_jobs WHERE site_id=? ORDER BY kind').all('existing').map(r => ({ ...r }));
  assert.deepEqual(pending, [
    { kind: 'favicon', state: 'pending', asset_key: null },
    { kind: 'thumbnail', state: 'pending', asset_key: null },
  ]);
  db.close();
});

test('media jobs are keyed by canonical progress identity and unique per media kind', () => {
  const db = migrated();
  db.prepare('INSERT INTO sites(id,url,name,description_ja) VALUES(?,?,?,?)').run('s1', 'https://www.example.com/path%20x', 's1', '説明');
  const rows = db.prepare('SELECT identity_key,kind FROM site_media_jobs WHERE site_id=? ORDER BY kind').all('s1').map(r => ({ ...r }));
  assert.deepEqual(rows, [
    { identity_key: 'example.com/path%20x', kind: 'favicon' },
    { identity_key: 'example.com/path%20x', kind: 'thumbnail' },
  ]);
  assert.throws(() => db.prepare("INSERT INTO site_media_jobs(identity_key,site_id,kind) VALUES('example.com/path%20x','s1','favicon')").run());
  const jobColumns = db.prepare('PRAGMA table_info(site_media_jobs)').all().map(r => r.name);
  assert.ok(jobColumns.includes('lease_owner'));
  assert.ok(jobColumns.includes('lease_until'));
  const quotaColumns = db.prepare('PRAGMA table_info(site_media_daily_counts)').all().map(r => r.name);
  assert.ok(quotaColumns.includes('reserved'));
  assert.ok(quotaColumns.includes('attempts'));
  db.close();
});

test('thumbnail eligibility excludes only approved adult=yes and follows classification updates', () => {
  const db = migrated();
  db.prepare('INSERT INTO sites(id,url,name,description_ja,adult_content) VALUES(?,?,?,?,?)')
    .run('adult-yes', 'https://adult.example/', 'adult', '説明', 1);
  db.prepare('INSERT INTO sites(id,url,name,description_ja,adult_content) VALUES(?,?,?, ?,?)')
    .run('adult-no', 'https://safe.example/', 'safe', '説明', 0);
  db.prepare('INSERT INTO sites(id,url,name,description_ja) VALUES(?,?,?,?)')
    .run('unknown-adult', 'https://unknown.example/', 'unknown', '説明');
  const states = db.prepare(`SELECT s.id,s.adult_content,j.kind,j.eligible,j.state
    FROM sites s JOIN site_media_jobs j ON j.site_id=s.id
    WHERE s.id IN ('adult-no','adult-yes','unknown-adult') ORDER BY s.id,j.kind`).all().map(r => ({ ...r }));
  assert.deepEqual(states, [
    { id: 'adult-no', adult_content: 0, kind: 'favicon', eligible: 1, state: 'pending' },
    { id: 'adult-no', adult_content: 0, kind: 'thumbnail', eligible: 1, state: 'pending' },
    { id: 'adult-yes', adult_content: 1, kind: 'favicon', eligible: 1, state: 'pending' },
    { id: 'adult-yes', adult_content: 1, kind: 'thumbnail', eligible: 0, state: 'excluded' },
    { id: 'unknown-adult', adult_content: null, kind: 'favicon', eligible: 1, state: 'pending' },
    { id: 'unknown-adult', adult_content: null, kind: 'thumbnail', eligible: 1, state: 'pending' },
  ]);
  db.prepare("UPDATE sites SET adult_content=1 WHERE id='adult-no'").run();
  assert.deepEqual({ ...db.prepare("SELECT eligible,state FROM site_media_jobs WHERE site_id='adult-no' AND kind='thumbnail'").get() },
    { eligible: 0, state: 'excluded' });
  db.prepare("UPDATE sites SET adult_content=0 WHERE id='adult-yes'").run();
  assert.deepEqual({ ...db.prepare("SELECT eligible,state FROM site_media_jobs WHERE site_id='adult-yes' AND kind='thumbnail'").get() },
    { eligible: 1, state: 'pending' });
  db.close();
});

test('0035 holds preexisting thumbnails until adult-classification backfill explicitly activates them', () => {
  const db = migrated({ through: '0034_progress_identity_rekey.sql' });
  db.prepare('INSERT INTO sites(id,url,name,description_ja) VALUES(?,?,?,?)')
    .run('preexisting', 'https://preexisting.example/', 'preexisting', '説明');
  db.exec(readFileSync(new URL('../migrations/0035_adult_thumbnail_eligibility.sql', import.meta.url), 'utf8'));
  assert.deepEqual({ ...db.prepare("SELECT eligible,state FROM site_media_jobs WHERE site_id='preexisting' AND kind='thumbnail'").get() },
    { eligible: 0, state: 'pending' });
  // The approved exporter emits an UPDATE even when adult is unknown/NULL. The trigger
  // records that classification pass and restores the historical unknown-is-eligible rule.
  db.prepare("UPDATE sites SET adult_content=NULL WHERE id='preexisting'").run();
  assert.deepEqual({ ...db.prepare("SELECT eligible,state FROM site_media_jobs WHERE site_id='preexisting' AND kind='thumbnail'").get() },
    { eligible: 1, state: 'pending' });
  db.close();
});

test('request-only URL gets the new identity key and a single progress row', () => {
  const db = migrated();
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason)
    VALUES('add','pending','http://www.request-only.example/A?b=2&a=1','request-only.example','request-only.example/a','reason')`).run();
  assert.equal(db.prepare("SELECT identity_key FROM progress_identity_urls WHERE entity='request' AND url='http://www.request-only.example/A?b=2&a=1'").get()?.identity_key, 'request-only.example/A?b=2&a=1');
  const request = db.prepare('SELECT progress_identity_key FROM requests').get();
  assert.equal(request.progress_identity_key, 'request-only.example/A?b=2&a=1');
  const progress = db.prepare('SELECT identity_key,source,request_status FROM url_progress WHERE url LIKE \'http://www.request-only.example/%\'').all();
  assert.equal(progress.length, 1);
  assert.equal(progress[0].identity_key, request.progress_identity_key);
  assert.equal(progress[0].source, 'request');
  assert.equal(progress[0].request_status, 'pending');
  db.close();
});

test('0034 rekeys old fragment collisions and preserves the newest request overlay', () => {
  const db = migrated({ through: '0033_media_jobs.sql' });
  db.prepare(`INSERT INTO sites(id,url,name,description_ja,registered_at)
    VALUES('a','https://www.rekey.example/Case?x=1#old','a','説明','2026-11-20T00:00:00Z'),
          ('b','http://rekey.example/Case?x=1#new','b','説明','2026-11-21T00:00:00Z')`).run();
  db.prepare(`INSERT INTO url_progress(identity_key,url,source,state,updated_at)
    VALUES('https://www.rekey.example/case?x=1#old','https://www.rekey.example/Case?x=1#old','discovered','fetched','2026-11-20 00:00:00'),
          ('http://rekey.example/case?x=1#new','http://rekey.example/Case?x=1#new','discovered','candidate','2026-11-21 00:00:00')`).run();
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason,created_at)
    VALUES('add','pending','https://www.rekey.example/Case?x=1','rekey.example','rekey.example/case','old','2026-11-20 00:00:00'),
          ('correct','approved','http://rekey.example/Case?x=1','rekey.example','rekey.example/case','new','2026-11-22 00:00:00')`).run();
  db.exec(readFileSync(new URL('../migrations/0034_progress_identity_rekey.sql', import.meta.url), 'utf8'));
  assert.equal(db.prepare("SELECT progress_key FROM sites WHERE id='a'").get().progress_key, 'rekey.example/Case?x=1');
  const rows = db.prepare("SELECT identity_key,state,request_type,request_status FROM url_progress WHERE identity_key='rekey.example/Case?x=1'").all();
  assert.equal(rows.length, 1);
  assert.deepEqual({ ...rows[0] }, { identity_key: 'rekey.example/Case?x=1', state: 'published', request_type: 'correct', request_status: 'approved' });
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM site_media_jobs WHERE identity_key='rekey.example/Case?x=1'").get().n, 2);
  db.close();
});

test('0034 stages rekey data without SQLite TEMP schema objects rejected by D1', () => {
  const sql = readFileSync(new URL('../migrations/0034_progress_identity_rekey.sql', import.meta.url), 'utf8');
  assert.doesNotMatch(sql, /\bCREATE\s+TEMP(?:ORARY)?\b/i);
  assert.match(sql, /CREATE INDEX _migration_0034_identity_entity_id/);
  assert.match(sql, /CREATE INDEX _migration_0034_identity_url/);
  assert.match(sql, /progress_identity_urls WHERE entity=/);
  assert.match(sql, /_migration_0034_identity_urls WHERE entity=/);
  assert.match(sql, /CREATE TABLE _migration_0034_media_jobs_rekey AS/);
  assert.match(sql, /CREATE TABLE _migration_0034_progress_identity_rekey AS/);
  assert.match(sql, /DROP TABLE _migration_0034_media_jobs_rekey/);
  assert.match(sql, /DROP TABLE _migration_0034_progress_identity_rekey/);
});
