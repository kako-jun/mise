import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const dir = new URL('../migrations/', import.meta.url);
test('0036 は url_candidates を作り、主キー以外は NULL 可', () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(dir).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, dir), 'utf8'));
  const cols = db.prepare('PRAGMA table_info(url_candidates)').all();
  const names = cols.map(c => c.name);
  for (const n of ['identity_key', 'frontier_key', 'text_head', 'fetch_adult', 'fetch_site_kind', 'adult', 'site_kind', 'review_reason', 'updated_at']) assert.ok(names.includes(n), n);
  const type = Object.fromEntries(cols.map(c => [c.name, c.type]));
  for (const n of ['fetch_adult', 'adult', 'confidence', 'exempt', 'frontier_key']) assert.equal(type[n], 'TEXT', n);
  for (const n of ['priority', 'personal_score', 'depth']) assert.equal(type[n], 'INTEGER', n);
  assert.deepEqual(cols.filter(c => c.notnull && !c.pk).map(c => c.name), ['updated_at']);
  db.prepare('INSERT INTO url_candidates(identity_key) VALUES(?)').run('example.com');
  assert.equal(db.prepare('SELECT COUNT(*) n FROM url_candidates').get().n, 1);
});

test('公開コード（src/）は url_candidates を参照しない', () => {
  const walk = d => readdirSync(d, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(new URL(`${e.name}/`, d)) : [new URL(e.name, d)]);
  for (const f of walk(new URL('../src/', import.meta.url))) assert.ok(!readFileSync(f, 'utf8').includes('url_candidates'), f.pathname);
});
