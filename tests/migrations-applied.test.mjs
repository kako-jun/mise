import test from 'node:test';
import assert from 'node:assert/strict';
import { migrationFiles, pendingMigrations, appliedFromJson, check } from '../scripts/check-migrations-applied.mjs';

test('migrationFiles: migrations/ の連番ファイルを昇順で（0028 を含む）', () => {
  const f = migrationFiles(new URL('../migrations/', import.meta.url));
  assert.ok(f.includes('0028_thumbnails.sql')); assert.deepEqual(f, [...f].sort());
});
test('pendingMigrations: 未適用だけ返す', () => {
  assert.deepEqual(pendingMigrations(['0001_a.sql', '0002_b.sql', '0003_c.sql'], ['0001_a.sql', '0002_b.sql']), ['0003_c.sql']);
  assert.deepEqual(pendingMigrations(['0001_a.sql'], ['0001_a.sql', '0009_zzz.sql']), []);
});
test('appliedFromJson: wrangler の出力から名前を取り、形が違えば例外', () => {
  assert.deepEqual(appliedFromJson('[{"results":[{"name":"0001_a.sql"}],"success":true}]'), ['0001_a.sql']);
  for (const bad of ['{}', '[]', '[{"results":null}]', 'not json']) assert.throws(() => appliedFromJson(bad));
});
test('check: DB ごとに未適用・読めない DB を報告（読めないものを「適用済み」にしない）', () => {
  const files = ['0001_a.sql', '0002_b.sql'];
  const r = check(['mise', 'mise-preview', 'broken'], { files, query: db => { if (db === 'broken') throw new Error('boom\nstack'); return db === 'mise' ? files : ['0001_a.sql']; } });
  assert.deepEqual(r, { mise: { pending: [] }, 'mise-preview': { pending: ['0002_b.sql'] }, broken: { error: 'boom' } });
});
