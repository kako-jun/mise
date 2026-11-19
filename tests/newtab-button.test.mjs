import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
// DOM を持たない環境のため、#81 の契約をソースの不変条件として固定する（実挙動は実ブラウザで確認済み。docs/design/ui.md）。
const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../public/style.css', import.meta.url), 'utf8');
test('別タブで開くボタン: 常に別タブ・noopener noreferrer・日英ラベル', () => {
  assert.match(app, /a\.target='_blank'; a\.rel='noopener noreferrer'/);
  assert.match(app, /newTab:'新しいタブで開く'/);
  assert.match(app, /newTab:'Open in new tab'/);
  assert.match(app, /a\.setAttribute\('aria-label',\s*label\);\s*a\.title\s*=\s*label/);
  assert.match(app, /\$\{name\} を\$\{t\('newTab'\)\}/);
  assert.match(app, /Open \$\{name\} in new tab/);
});
test('別タブで開くボタン: 表示はトグルと同期（描画時の初期化と切替時の更新）', () => {
  assert.match(app, /a\.hidden\s*=\s*openExternalInNewTab;/);
  assert.match(app, /querySelectorAll\('a\.site-newtab'\)\.forEach\(a=>\{ a\.hidden=openExternalInNewTab; \}\)/);
  assert.match(app, /if\(href\) content\.append\(newTabButton\(href,\s*site\.name\)\)/);
  assert.match(app, /classList\.contains\('site-newtab'\)\) return;/);
});
test('別タブで開くボタン: display を持つので [hidden] を併記・枠なし・32px', () => {
  assert.match(css, /\.site-newtab\[hidden\] \{ display: none; \}/);
  const rule = css.match(/\.site-newtab \{[^}]*\}/)[0];
  assert.match(rule, /width: 32px; height: 32px/);
  assert.doesNotMatch(rule, /border:|background:/);
  assert.match(rule, /position: relative; z-index: 1/); // 後続要素(説明文)に押下範囲を奪われない
  assert.doesNotMatch(rule, /margin:[^;]*-\d+px -\d+px -\d+px -\d+px/); // 右の負マージンで旗と重ねない
});
