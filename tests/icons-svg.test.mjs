// ◐ とタグチップ・パネルの × は文字でなく SVG（字形がフォントで変わり、上下中央に置けないため。#183）。文字に戻す変異を検出する。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const read = (f) => readFileSync(new URL(`../public/${f}`, import.meta.url), 'utf8');
test('テーマ切替の ◐ は SVG（index.html・about.html）で、文字の ◐ は残っていない', () => {
  for (const f of ['index.html', 'about.html']) {
    const m = read(f).match(/<button id="theme-toggle"[^>]*>(.*?)<\/button>/s);
    assert.ok(m, `${f}: theme-toggle`);
    assert.match(m[1], /<svg aria-hidden="true"/, `${f}: SVG で aria-hidden`);
    assert.ok(!m[1].includes('◐'), `${f}: 文字の ◐ が残っている`);
  }
});
test('タグチップの × は SVG（aria-hidden。名前はボタンの aria-label）', () => {
  const app = read('app.js');
  assert.match(app, /const TAG_X_ICON='<svg aria-hidden="true"/);
  assert.match(app, /x\.innerHTML=TAG_X_ICON/);
  assert.ok(!/x\.textContent='×'/.test(app), 'チップの × が文字に戻っている');
  assert.match(app, /x\.setAttribute\('aria-label',t\('tagRemove'\)\(n\)\)/);
});
test('縦フィードのパネルの × は SVG（aria-hidden と aria-label を維持）', () => {
  const line = read('feed.js').split('\n').find((l) => l.includes("'f-panel-x'"));
  assert.ok(line, 'f-panel-x');
  assert.match(line, /x\.innerHTML = '<svg aria-hidden="true"/);
  assert.match(line, /'aria-label': t\('seenClose'\)/);
  assert.ok(!/x\.textContent = '×'/.test(line), 'パネルの × が文字に戻っている');
});
