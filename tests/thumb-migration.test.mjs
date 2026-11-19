import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { querySites } from '../src/sites.js';

// migration 0028（#130）: sites にサムネイルの列を足す。FTS5 の外部コンテンツ表・集計トリガ・並びの索引に影響しないこと。
const MIG = new URL('../migrations/', import.meta.url);
import { createHash } from 'node:crypto';
const H = createHash('sha256').update('a').digest('hex').slice(0, 32);   // サイト a のキー（sha256(site_id) の先頭 32 桁）
const LQIP = 'data:image/webp;base64,UklGRhoAAABXRUJQVlA4IA4AAAAwAQCdASoQAAoAAUAmJZQAA3AA/v3AgAA=';
const mkDb = () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  db.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');   // migration の初期データを消す
  return db;
};
const wrap = db => ({ prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); } });
const cat = db => db.prepare('SELECT id FROM categories LIMIT 1').get().id;

test('0028: 列が増え、既存行は NULL のまま', () => {
  const db = mkDb();
  const cols = db.prepare("SELECT name, type FROM pragma_table_info('sites')").all();
  for (const c of ['thumb_path', 'thumb_lqip', 'thumb_captured_at']) assert.deepEqual(cols.find(x => x.name === c)?.type, 'TEXT', c);
  db.prepare('INSERT INTO sites (id,url,name,description_ja,registered_at,country_code) VALUES (?,?,?,?,?,?)').run('a', 'https://a.example', 'a', 'd', '2026-01-01T00:00:00Z', 'JP');
  assert.deepEqual({ ...db.prepare('SELECT thumb_path, thumb_lqip, thumb_captured_at FROM sites').get() }, { thumb_path: null, thumb_lqip: null, thumb_captured_at: null });
});

test('0028: sites のトリガはどれも thumb 列の更新で発火しない（UPDATE OF で列を限っている）', () => {
  const db = mkDb();
  for (const t of db.prepare("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='sites'").all()) {
    if (!/AFTER UPDATE|BEFORE UPDATE/i.test(t.sql)) continue;
    assert.match(t.sql, /UPDATE OF/i, `${t.name} は UPDATE OF で列を限る`);
    assert.doesNotMatch(t.sql.split(/ ON sites/i)[0], /thumb_/i, t.name);
  }
});

test('0028: thumb 列の UPDATE は他の表・索引を書かない（total_changes が 1 行分だけ）', () => {
  const db = mkDb();
  db.exec("INSERT INTO sites (id,url,name,description_ja,registered_at,country_code,region_code) VALUES ('a','https://a.example','a','d','2026-01-01T00:00:00Z','JP','JP-13')");
  db.exec(`INSERT INTO site_categories (site_id, category_id) VALUES ('a', '${cat(db)}')`);
  db.exec("UPDATE fts_state SET ready = 1 WHERE name = 'sites_fts'");   // 索引が使われる状態（sites_fts_au が有効なとき）でも触らない
  const snap = () => JSON.stringify([db.prepare('SELECT * FROM scope_counts ORDER BY scope').all(), db.prepare('SELECT * FROM category_counts ORDER BY category_id, scope').all(), db.prepare('SELECT search_text FROM sites').all()]);
  const before = snap(), n0 = db.prepare('SELECT total_changes() AS n').get().n;
  db.prepare('UPDATE sites SET thumb_path=?, thumb_lqip=?, thumb_captured_at=? WHERE id=?').run(`${H}.webp`, LQIP, '2026-11-20T00:00:00Z', 'a');
  assert.equal(db.prepare('SELECT total_changes() AS n').get().n - n0, 1);
  assert.equal(snap(), before);
  assert.equal(db.prepare('SELECT thumb_path FROM sites').get().thumb_path, `${H}.webp`);
});

test('API: thumb_url / thumb_lqip を返す。列が NULL・形が違う値・画像なしのプレースホルダは null', async () => {
  const db = mkDb(), d = wrap(db);
  const ins = db.prepare('INSERT INTO sites (id,url,name,description_ja,registered_at,country_code,thumb_path,thumb_lqip,thumb_captured_at) VALUES (?,?,?,?,?,?,?,?,?)');
  const T = '2026-11-20T03:00:00Z';
  ins.run('a', 'https://a.example', 'a', 'd', '2026-01-01T00:00:00Z', 'JP', `${H}.webp`, LQIP, T);
  ins.run('b', 'https://b.example', 'b', 'd', '2026-01-02T00:00:00Z', 'JP', null, null, null);
  ins.run('c', 'https://c.example', 'c', 'd', '2026-01-03T00:00:00Z', 'JP', 'legacy.png', LQIP, T);
  ins.run('d', 'https://d.example', 'd', 'd', '2026-01-04T00:00:00Z', 'JP', `${H}.webp`, 'javascript:alert(1)', null);
  const by = Object.fromEntries((await querySites(d, { cc: null, region: null, category: null, q: '', tags: [], limit: 10, cursor: null, offset: 0 })).items.map(i => [i.id, i]));
  assert.equal(by.a.thumb_url, `/thumb/${H}.webp?v=${Math.floor(Date.parse(T) / 1000).toString(36)}`); assert.equal(by.a.thumb_lqip, LQIP);
  assert.equal(by.b.thumb_url, null); assert.equal(by.b.thumb_lqip, null);
  assert.equal(by.c.thumb_url, null); assert.equal(by.c.thumb_lqip, null);
  assert.equal(by.d.thumb_url, `/thumb/${H}.webp`); assert.equal(by.d.thumb_lqip, null);   // 撮影時刻が無ければ v なし
  assert.ok(!('thumb_path' in by.a)); assert.ok(!('thumb_captured_at' in by.a));
  assert.ok('favicon_url' in by.a);
});
