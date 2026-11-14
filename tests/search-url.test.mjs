import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
// DOM を持たない環境のため、#99 の契約をソースの不変条件として固定する（実挙動は実ブラウザで確認。docs/design/ui.md）。
const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
test('検索の URL 反映: 入力デバウンス・フォーム確定とも commitSearchUrl を通す', () => {
  assert.match(app, /function updateSearch\(\) \{[\s\S]*?commitSearchUrl\(q\); showResults\(q\);/);
  assert.match(app, /addEventListener\('submit',e=>\{[\s\S]*?commitSearchUrl\(q\);showResults\(q\);/);
});
test('検索の URL 反映: 検索に入る最初だけ pushState、検索中は replaceState', () => {
  const fn = app.match(/function commitSearchUrl\(q\) \{[\s\S]*?\n\}/)[0];
  assert.match(fn, /if\(was\) history\.replaceState\(history\.state,'',url\); else pushUrl\(url\);/);
  assert.match(app, /sp\.has\('q'\) \|\| sp\.has\('tag'\)/);
});
test('検索を空にしてカテゴリへ戻るとき URL もカテゴリへ戻す', () => {
  assert.match(app, /leaveSearchUrl\(\); showDirectory\(currentPath, false\)/);
  assert.match(app, /function leaveSearchUrl\(\)[^\n]*history\.replaceState\(null,'',url\)/);
});
test('位置復元の打ち切り: 入力欄でのキー操作は無視', () => {
  assert.match(app, /e\.type==='keydown' && \(!SCROLL_KEYS\.has\(e\.key\) \|\| isEditable\(e\.target\)\)/);
  assert.match(app, /INPUT\|TEXTAREA\|SELECT/);
  assert.match(app, /isContentEditable/);
});
