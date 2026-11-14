import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
// DOM を持たない環境のため、#101 の契約をソースの不変条件として固定する（実挙動は実ブラウザで確認済み。docs/design/ui.md）。
const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
test('pagehide の再保存は未保存の変化（listDirty）があるときだけ', () => {
  assert.match(app, /addEventListener\('pagehide',\(\)=>\{ if\(listDirty\) saveListState\(\); \}\)/);
  assert.doesNotMatch(app, /addEventListener\('pagehide',saveListState\)/);
});
test('listDirty: スクロールで立て、保存成功で下ろす（件数追加では立てない）', () => {
  assert.match(app, /addEventListener\('scroll',\(\)=>\{ listDirty=true;/);
  assert.doesNotMatch(app, /L\.items\.push\(\.\.\.d\.items\); listDirty/);
  assert.match(app, /y:scrollY\}\},''\); listDirty=false;/);
});
test('画面を移る前の保存（pushUrl）は常に同期で保存する', () => {
  assert.match(app, /function pushUrl\(url\) \{ saveListState\(\); history\.pushState/);
});
