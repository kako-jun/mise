import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const dir = new URL('../migrations/', import.meta.url);
test('0037 は discovery_cursors を (source, cursor_key) 主キーで作る', () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(dir).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, dir), 'utf8'));
  const cols = db.prepare('PRAGMA table_info(discovery_cursors)').all();
  assert.deepEqual(cols.filter(c => c.pk).map(c => c.name), ['source', 'cursor_key']);
  db.prepare('INSERT INTO discovery_cursors(source,cursor_key,cursor_value) VALUES(?,?,?)').run('lease', 'discover', 'a|1');
  assert.throws(() => db.prepare('INSERT INTO discovery_cursors(source,cursor_key,cursor_value) VALUES(?,?,?)').run('lease', 'discover', 'b|2'));
  assert.ok(db.prepare('SELECT updated_at FROM discovery_cursors').get().updated_at);
});

test('公開コード（src/）は discovery_cursors を参照しない', () => {
  const walk = d => readdirSync(d, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(new URL(`${e.name}/`, d)) : [new URL(e.name, d)]);
  for (const f of walk(new URL('../src/', import.meta.url))) assert.ok(!readFileSync(f, 'utf8').includes('discovery_cursors'), f.pathname);
});
