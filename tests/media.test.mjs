import test from 'node:test';
import assert from 'node:assert/strict';
import app from '../src/index.js';
import { mediaKey, faviconUrl } from '../src/media.js';

// R2（MEDIA）から画像を配信する /icon/<hash>.webp（#125）。R2 は最小の模擬で置き換える。
const H = 'a1b2c3d4e5f60718293a4b5c6d7e8f90';
const fakeBucket = (objects = {}) => ({ calls: [], async get(key) { this.calls.push(key); const v = objects[key]; return v ? { body: new Response(v).body, httpEtag: '"etag-1"' } : null; } });
const get = (path, env, headers) => app.request(path, { headers }, env);

test('mediaKey/faviconUrl: 32 桁 16 進 + .webp だけ許可', () => {
  assert.equal(mediaKey('icon', `${H}.webp`), `icon/${H}.webp`);
  for (const bad of [`${H}.png`, `${H}`, `${H}.WEBP`, `${H.toUpperCase()}.webp`, `${H}0.webp`, `${H.slice(1)}.webp`, '../x.webp', `${H}.webp.x`, 'g'.repeat(32) + '.webp', '']) assert.equal(mediaKey('icon', bad), null, bad);
  assert.equal(mediaKey('thumb', `${H}.webp`), null);   // thumb は未実装
  assert.equal(faviconUrl(`${H}.webp`), `/icon/${H}.webp`);
  assert.equal(faviconUrl(null), null);
  assert.equal(faviconUrl('/icons/x.png'), null);
});

test('/icon/<hash>.webp: 200 + immutable 1 年 + ETag + image/webp', async () => {
  const env = { MEDIA: fakeBucket({ [`icon/${H}.webp`]: 'RIFFxxxx' }) };
  const r = await get(`/icon/${H}.webp`, env);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('Content-Type'), 'image/webp');
  assert.equal(r.headers.get('Cache-Control'), 'public, max-age=31536000, immutable');
  assert.equal(r.headers.get('ETag'), '"etag-1"');
  assert.equal(r.headers.get('X-Content-Type-Options'), 'nosniff');
  assert.equal(await r.text(), 'RIFFxxxx');
  assert.deepEqual(env.MEDIA.calls, [`icon/${H}.webp`]);
});

test('/icon/: If-None-Match が一致すれば 304', async () => {
  const env = { MEDIA: fakeBucket({ [`icon/${H}.webp`]: 'x' }) };
  const r = await get(`/icon/${H}.webp`, env, { 'If-None-Match': '"etag-1"' });
  assert.equal(r.status, 304);
  assert.equal(r.headers.get('ETag'), '"etag-1"');
});

test('/icon/: 不正な名前は R2 を見ずに 404', async () => {
  const env = { MEDIA: fakeBucket({}) };
  for (const f of [`${H}.png`, 'abc.webp', `${H.toUpperCase()}.webp`, `${H}0.webp`, '..%2Fx.webp']) {
    const r = await get(`/icon/${f}`, env);
    assert.equal(r.status, 404, f);
  }
  assert.deepEqual(env.MEDIA.calls, []);
});

test('/icon/: 存在しなければ 404（代替画像なし・キャッシュさせない）', async () => {
  const r = await get(`/icon/${H}.webp`, { MEDIA: fakeBucket({}) });
  assert.equal(r.status, 404);
  assert.equal(r.headers.get('Cache-Control'), 'no-store');
});

test('/icon/: MEDIA 未設定でも落ちず 404', async () => {
  assert.equal((await get(`/icon/${H}.webp`, {})).status, 404);
  assert.equal((await get(`/icon/${H}.webp`, { MEDIA: undefined })).status, 404);
});

// API の site 項目: favicon_path はそのまま、favicon_url を足す（取れていなければ null）。
import { DatabaseSync } from 'node:sqlite';
import { querySites } from '../src/sites.js';
test('querySites: favicon_url を足し、favicon_path の形は変えない', async () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE sites(id TEXT PRIMARY KEY,url TEXT,name TEXT,description_ja TEXT,description_en TEXT,favicon_path TEXT,registered_at TEXT,source_updated_at TEXT,last_crawled_at TEXT,country_code TEXT,region_code TEXT,bulk_import INTEGER NOT NULL DEFAULT 0,search_text TEXT);
CREATE TABLE site_tags(site_id TEXT, label_ja TEXT NOT NULL, label_en TEXT);
CREATE TABLE site_categories(site_id TEXT, category_id TEXT);
CREATE TABLE scope_counts(scope TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE categories(id TEXT PRIMARY KEY, parent_id TEXT);
CREATE TABLE category_counts(category_id TEXT NOT NULL, scope TEXT NOT NULL, n INTEGER NOT NULL);`);
  const ins = db.prepare('INSERT INTO sites(id,url,name,description_ja,favicon_path,country_code,registered_at) VALUES(?,?,?,?,?,?,?)');
  ins.run('a', 'https://a.example', 'a', 'd', `${H}.webp`, 'JP', '2026-01-01T00:00:00Z');
  ins.run('b', 'https://b.example', 'b', 'd', null, 'JP', '2026-01-02T00:00:00Z');
  ins.run('c', 'https://c.example', 'c', 'd', 'legacy.png', 'JP', '2026-01-03T00:00:00Z');
  db.exec("INSERT INTO scope_counts VALUES ('all', 3)");
  const d = { prepare: sql => ({ bind: (...a) => ({ all: async () => ({ results: db.prepare(sql).all(...a) }), first: async () => db.prepare(sql).get(...a) }) }) };
  const by = Object.fromEntries((await querySites(d, { cc: null, region: null, category: null, q: '', tags: [], limit: 10, cursor: null, offset: 0 })).items.map(i => [i.id, i]));
  assert.equal(by.a.favicon_path, `${H}.webp`); assert.equal(by.a.favicon_url, `/icon/${H}.webp`);
  assert.equal(by.b.favicon_path, null); assert.equal(by.b.favicon_url, null);
  assert.equal(by.c.favicon_path, 'legacy.png'); assert.equal(by.c.favicon_url, null);
});
