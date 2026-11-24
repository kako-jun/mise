import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, mkdtempSync, rmSync, readdirSync as listDir, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import app from '../src/index.js';

const migrationFiles = readdirSync(new URL('../migrations/', import.meta.url)).filter(f => f.endsWith('.sql')).sort();
function fixture({ through = null } = {}) {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of migrationFiles) {
    if (through && f > through) break;
    db.exec(readFileSync(new URL(`../migrations/${f}`, import.meta.url), 'utf8'));
  }
  if (!through) db.exec('DELETE FROM requests; DELETE FROM site_tags; DELETE FROM site_categories; DELETE FROM sites; DELETE FROM url_progress;');
  return db;
}
const d1 = db => ({ prepare: sql => {
  let args = [];
  const q = { bind(...a) { args = a; return q; }, all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) };
  return q;
} });
const assets = { fetch: async req => new Response(readFileSync(new URL(`../public${new URL(req.url).pathname}`, import.meta.url)), { headers: { 'content-type': 'text/html' } }) };
const api = (db, path, deps = {}) => app.request(path, {}, { DB: d1(db), ASSETS: assets, ...deps });
function site(db, id, url, descJa = '説明', descEn = 'Description') {
  db.prepare('INSERT INTO sites(id,url,name,description_ja,description_en,registered_at) VALUES(?,?,?,?,?,?)')
    .run(id, url, id, descJa, descEn, '2026-11-24T00:00:00Z');
}

test('GET /api/progress merges published sites and lifecycle rows, sorted newest first', async () => {
  const db = fixture();
  site(db, 'pub', 'https://pub.example/', '日本語', 'English');
  db.prepare("INSERT INTO site_tags(site_id,label_ja) VALUES('pub','タグ')").run();
  db.prepare("INSERT INTO site_categories(site_id,category_id) VALUES('pub','arts')").run();
  db.prepare("UPDATE url_progress SET state='approved',updated_at='2026-11-24 02:00:00' WHERE identity_key='pub.example'").run();
  db.prepare("INSERT INTO url_progress(identity_key,url,source,state,updated_at) VALUES('new.example','https://new.example/','discovered','fetched','2026-11-24 03:00:00')").run();
  const res = await api(db, '/api/progress');
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.total, 2);
  assert.equal(body.results[0].url, 'https://new.example/');
  assert.equal(body.results[0].state, 'fetched');
  const published = body.results[1];
  assert.equal(published.state, 'published');
  assert.equal(published.source, 'discovered');
  assert.equal(published.description_ja, '日本語');
  assert.equal(published.description_en, 'English');
  assert.equal(published.tags_count, 1);
  assert.equal(published.categories_count, 1);
});

test('site insert creates the canonical discovered row and marks it published in the API', async () => {
  const db = fixture(); site(db, 'pub', 'https://pub.example/');
  const body = await (await api(db, '/api/progress')).json();
  assert.equal(body.results.length, 1);
  assert.equal(body.results[0].source, 'discovered');
  assert.equal(body.results[0].state, 'published');
  assert.equal(db.prepare("SELECT identity_key FROM url_progress").get().identity_key, 'pub.example');
});

test('GET /api/progress keeps request source/status on an already published site', async () => {
  const db = fixture(); site(db, 'pub', 'https://pub.example/');
  db.prepare("UPDATE url_progress SET source='request',request_type='remove',request_status='pending' WHERE identity_key='pub.example'").run();
  const body = await (await api(db, '/api/progress')).json();
  assert.equal(body.results.length, 1);
  assert.equal(body.results[0].state, 'published');
  assert.equal(body.results[0].source, 'request');
  assert.equal(body.results[0].request_type, 'remove');
  assert.equal(body.results[0].request_status, 'pending');
});

test('GET /api/progress pagination defaults and limit/offset boundaries', async () => {
  const db = fixture();
  for (let i = 0; i < 3; i++) db.prepare('INSERT INTO url_progress(identity_key,url,state,updated_at) VALUES(?,?,?,?)').run(`${i}.example`, `https://${i}.example/`, 'fetched', `2026-11-24 0${i}:00:00`);
  const defaults = await (await api(db, '/api/progress')).json();
  assert.equal(defaults.limit, 50); assert.equal(defaults.results.length, 3);
  assert.equal((await api(db, '/api/progress?limit=1&offset=1')).status, 200);
  assert.equal((await api(db, '/api/progress?limit=100&offset=1000000')).status, 200);
  for (const qs of ['?limit=0', '?limit=101', '?limit=x', '?offset=-1', '?offset=1000001', '?offset=', '?limit=']) {
    assert.equal((await api(db, `/api/progress${qs}`)).status, 400, qs);
  }
});

test('GET /api/progress returns an empty page after the last row', async () => {
  const db = fixture(); db.prepare("INSERT INTO url_progress(identity_key,url) VALUES('one.example','https://one.example/')").run();
  const body = await (await api(db, '/api/progress?offset=1')).json();
  assert.equal(body.total, 1); assert.deepEqual(body.results, []);
});

test('GET /api/progress never returns private request or review fields', async () => {
  const db = fixture();
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason,contact,ip_hash,net_hash,note)
    VALUES('add','pending','https://request.example/','request.example','request.example','private reason','me@example.test','ip-secret','net-secret','admin note')`).run();
  const res = await api(db, '/api/progress'); const body = await res.json();
  const serialized = JSON.stringify(body);
  for (const secret of ['private reason', 'me@example.test', 'ip-secret', 'net-secret', 'admin note']) assert.equal(serialized.includes(secret), false);
  assert.equal(body.results[0].source, 'request');
  assert.equal(body.results[0].request_status, 'pending');
});

test('request triggers preserve pending-to-approved/rejected and add/remove/correct overlay states', async () => {
  const db = fixture(); site(db, 'pub', 'https://pub.example/');
  const add = db.prepare("INSERT INTO requests(type,status,url,host,normalized_key,reason) VALUES(?,?,?,?,?,?)");
  add.run('add','pending','https://new.example/','new.example','new.example','x');
  assert.deepEqual({ ...db.prepare("SELECT source,state,request_type,request_status FROM url_progress WHERE url='https://new.example/'").get() },
    { source: 'request', state: 'discovered', request_type: 'add', request_status: 'pending' });
  db.prepare("UPDATE requests SET status='approved',decided_at='2026-11-24 04:00:00' WHERE url='https://new.example/'").run();
  const approved = db.prepare("SELECT state,request_status FROM url_progress WHERE identity_key='new.example'").get();
  assert.equal(approved.state, 'discovered'); assert.equal(approved.request_status, 'approved');
  for (const type of ['remove','correct']) {
    add.run(type,'pending','https://pub.example/','pub.example','pub.example',`private ${type}`);
    const row = db.prepare("SELECT state,request_type,request_status FROM url_progress WHERE identity_key='pub.example'").get();
    assert.equal(row.request_type, type); assert.equal(row.request_status, 'pending');
    const view = await (await api(db, '/api/progress')).json();
    assert.equal(view.results.find(x => x.url === 'https://pub.example/').state, 'published');
  }
});

test('normalized user request merges with published http/www variant into one URL row', async () => {
  const db = fixture(); site(db, 'pub', 'https://example.com/');
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason)
    VALUES('add','pending','http://www.example.com/','www.example.com','example.com','private')`).run();
  const body = await (await api(db, '/api/progress')).json();
  assert.equal(body.total, 1);
  assert.equal(body.results.length, 1);
  assert.equal(body.results[0].url, 'https://example.com/');
  assert.equal(body.results[0].state, 'published');
  assert.equal(body.results[0].request_type, 'add');
  assert.equal(body.results[0].request_status, 'pending');
});

test('normalized path URL with an encoded space merges despite a decoded request dedup key', async () => {
  const db = fixture(); site(db, 'pub-path', 'https://example.com/a%20b/');
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason)
    VALUES('add','pending','http://www.example.com/a%20b','www.example.com','example.com/a b','private')`).run();
  const body = await (await api(db, '/api/progress')).json();
  assert.equal(body.total, 1);
  assert.equal(body.results.length, 1);
  assert.equal(body.results[0].url, 'https://example.com/a%20b/');
  assert.equal(body.results[0].state, 'published');
  assert.equal(body.results[0].request_type, 'add');
  assert.equal(body.results[0].request_status, 'pending');
});

test('request overlay does not regress an in-progress candidate stage', () => {
  const db = fixture();
  db.prepare("INSERT INTO url_progress(identity_key,url,source,state) VALUES('candidate.example','https://candidate.example/','discovered','candidate')").run();
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason)
    VALUES('add','pending','http://www.candidate.example/','www.candidate.example','candidate.example','private')`).run();
  const row = db.prepare("SELECT state,request_type,request_status FROM url_progress WHERE identity_key='candidate.example'").get();
  assert.equal(row.state, 'candidate');
  assert.equal(row.request_type, 'add');
  assert.equal(row.request_status, 'pending');
});

test('updating an older request does not replace the latest request overlay for its URL key', () => {
  const db = fixture();
  const add = db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason,created_at)
    VALUES(?,?,?,?,?,?,?)`);
  add.run('add','pending','https://example.com/','example.com','example.com','old add','2026-11-23 10:00:00');
  add.run('remove','pending','https://www.example.com/','www.example.com','example.com','new remove','2026-11-24 10:00:00');
  db.prepare("UPDATE requests SET status='approved',decided_at='2026-11-25 10:00:00' WHERE type='add' AND normalized_key='example.com'").run();
  const row = db.prepare("SELECT request_type,request_status FROM url_progress WHERE identity_key='example.com'").get();
  assert.equal(row.request_type, 'remove');
  assert.equal(row.request_status, 'pending');
});

test('mixed ISO-T and SQLite-space timestamps sort by actual time', async () => {
  const db = fixture();
  db.prepare("INSERT INTO url_progress(identity_key,url,state,updated_at) VALUES('older.example','https://older.example/','fetched','2026-11-24 02:30:00'),('newer.example','https://newer.example/','fetched','2026-11-24T03:00:00Z')").run();
  const body = await (await api(db, '/api/progress')).json();
  assert.equal(body.results[0].url, 'https://newer.example/');
  assert.equal(body.results[1].url, 'https://older.example/');
});

test('migration 0032 backfills latest request per normalized URL without copying private columns', () => {
  const db = fixture({ through: '0031_recent_registration_sort.sql' });
  db.prepare(`INSERT INTO requests(type,status,url,host,normalized_key,reason,contact,ip_hash,net_hash,note,created_at)
    VALUES('add','pending','https://old.example/','old.example','old.example','secret1','mail1','ip1','net1','note1','2026-11-23'),
          ('add','rejected','https://old.example/','old.example','old.example','secret2','mail2','ip2','net2','note2','2026-11-24')`).run();
  const sql = readFileSync(new URL('../migrations/0032_url_progress.sql', import.meta.url), 'utf8');
  db.exec(sql);
  assert.deepEqual({ ...db.prepare("SELECT source,state,request_type,request_status,discovered_at FROM url_progress WHERE identity_key='old.example'").get() },
    { source: 'request', state: 'discovered', request_type: 'add', request_status: 'rejected', discovered_at: '2026-11-24' });
  const cols = db.prepare('PRAGMA table_info(url_progress)').all().map(x => x.name);
  for (const privateField of ['reason','contact','ip_hash','net_hash','note']) assert.equal(cols.includes(privateField), false);
});

test('migration 0032 backfills legacy published rows with NULL registered_at', () => {
  const db = fixture({ through: '0031_recent_registration_sort.sql' });
  // Simulate an older pre-clock row: the normal insert trigger would fill this value on current writes.
  db.exec('DROP TRIGGER sites_registration_clock');
  db.prepare("INSERT INTO sites(id,url,name,description_ja,registered_at) VALUES('legacy-null','https://legacy-null.example/','Legacy','legacy',NULL)").run();
  db.exec(readFileSync(new URL('../migrations/0032_url_progress.sql', import.meta.url), 'utf8'));
  const row = db.prepare("SELECT identity_key,state,discovered_at,updated_at FROM url_progress WHERE identity_key='legacy-null.example'").get();
  assert.ok(row);
  assert.equal(row.state, 'published');
  assert.match(row.discovered_at, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  assert.equal(row.updated_at, row.discovered_at);
});

test('GET /progress redirects bare path and the screen is linked from the footer in both languages', async () => {
  const db = fixture();
  const redirect = await api(db, '/progress');
  assert.equal(redirect.status, 301); assert.equal(redirect.headers.get('location'), '/progress/');
  const screen = await api(db, '/progress/');
  assert.equal(screen.status, 200); assert.match(await screen.text(), /Collection and publishing progress/);
  const index = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  const appJs = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  assert.match(index, /href="\/progress\/" data-i18n="progress"/);
  assert.match(appJs, /progress:'収集・掲載の進捗'/); assert.match(appJs, /progress:'Collection and publishing status'/);
  assert.match(readFileSync(new URL('../public/progress.css', import.meta.url), 'utf8'), /overflow:auto/);
});

test('GET /api/progress sets the browser API cache policy and database failures are not cached', async () => {
  const db = fixture();
  const ok = await api(db, '/api/progress');
  assert.equal(ok.headers.get('cache-control'), 'public, max-age=30');
  const broken = await app.request('/api/progress', {}, { DB: { prepare: () => { throw new Error('db down'); } }, ASSETS: assets });
  assert.equal(broken.status, 500); assert.equal(broken.headers.get('cache-control'), 'no-store');
});

test('nightly exporter escapes URLs and emits only changed lifecycle fields', () => {
  const dir = mkdtempSync(join(tmpdir(), 'mise-progress-'));
  try {
    const dbPath = join(dir, 'frontier.sqlite'), sqlPath = join(dir, 'out.sql'), db = new DatabaseSync(dbPath);
    db.exec(readFileSync(new URL('../scripts/pipeline/schema.sql', import.meta.url), 'utf8'));
    db.exec("INSERT INTO frontier(key,host,url,source,discovered_at,state) VALUES('a','a.example','https://a.example/it''s','commoncrawl-webgraph','2026-11-24T00:00:00Z','candidate')");
    db.exec("INSERT INTO llm_judgement(key,input_hash,origin,model,judged_at,imported_at,spam_verdict,spam_kind,spam_reason,categories,tags,adult,site_kind,site_kind_reason,site_scope,confidence,description_ja,description_en,review_state) VALUES('a','h','judge','m','now','now','pass','none','','[]','[]','unknown','unknown','','standalone','high','説明','Description','approved')");
    db.close();
    const run = spawnSync('uv', ['run', 'python3', new URL('../scripts/progress/export_sql.py', import.meta.url).pathname, '--db', dbPath, '--out', sqlPath], { encoding: 'utf8' });
    assert.equal(run.status, 0, run.stderr);
    const sql = readFileSync(sqlPath, 'utf8');
    assert.match(sql, /it''s/); assert.match(sql, /'approved'/); assert.match(sql, /WHERE \(url_progress.state!='published'/);
    assert.match(sql, /identity_key/);
    const d = fixture(); d.prepare("INSERT INTO url_progress(identity_key,url,state) VALUES(?,?,?)").run("a.example/it's", "https://a.example/it's", 'fetched'); d.exec(sql);
    assert.equal(d.prepare("SELECT state FROM url_progress WHERE identity_key=?").get("a.example/it's").state, 'approved');
    d.prepare("UPDATE url_progress SET state='published' WHERE identity_key=?").run("a.example/it's"); d.exec(sql);
    assert.equal(d.prepare("SELECT state FROM url_progress WHERE identity_key=?").get("a.example/it's").state, 'published');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('nightly exporter splits a large frontier into bounded files without dropping or duplicating URLs', () => {
  const dir = mkdtempSync(join(tmpdir(), 'mise-progress-scale-'));
  try {
    const dbPath = join(dir, 'frontier.sqlite'), sqlPath = join(dir, 'url-progress.sql'), db = new DatabaseSync(dbPath);
    db.exec(readFileSync(new URL('../scripts/pipeline/schema.sql', import.meta.url), 'utf8'));
    const insert = db.prepare('INSERT INTO frontier(key,host,url,source,discovered_at,state) VALUES(?,?,?,?,?,?)');
    const expected = new Set();
    const count = 25000;
    const tx = db.prepare('BEGIN');
    tx.run();
    for (let i = 0; i < count; i++) {
      const n = String(i).padStart(5, '0'), url = `https://site${n}.example/${'p'.repeat(80)}`;
      expected.add(url);
      insert.run(`site${n}.example`, `site${n}.example`, url, 'commoncrawl-webgraph', '2026-11-24T00:00:00Z', 'discovered');
    }
    db.prepare('COMMIT').run(); db.close();
    const run = spawnSync('uv', ['run', 'python3', new URL('../scripts/progress/export_sql.py', import.meta.url).pathname, '--db', dbPath, '--out', sqlPath], { encoding: 'utf8', timeout: 60000 });
    assert.equal(run.status, 0, run.stderr || run.error?.message);
    const outputs = listDir(dir).filter(name => name.startsWith('url-progress.sql')).sort().map(name => join(dir, name));
    assert.ok(outputs.length > 1, `expected multiple outputs, got ${outputs.length}`);
    const seen = [];
    for (const output of outputs) {
      assert.ok(statSync(output).size <= 5 * 1024 * 1024, `${output} exceeds 5 MiB`);
      for (const line of readFileSync(output, 'utf8').split('\n')) {
        const m = line.match(/^INSERT INTO url_progress\(identity_key,url,[\s\S]*?VALUES\('([^']+)','([^']+)'/);
        if (m) seen.push(m[2]);
      }
    }
    assert.equal(seen.length, count);
    assert.equal(new Set(seen).size, count);
    assert.deepEqual(new Set(seen), expected);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
