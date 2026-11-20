// 縦フィードの入口 (#178) の href 生成（public/vertical-link.js）と、入口が SSR・JS 両方にあること。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import '../public/vertical-link.js';
const { feedEntryHref } = globalThis.MiseFeedEntry;
const read = f => readFileSync(new URL('../' + f, import.meta.url), 'utf8');
test('範囲とトップ階層カテゴリだけを引き継ぐ', () => {
  assert.equal(feedEntryHref('jp'), '/jp/?view=feed');
  assert.equal(feedEntryHref('jp-13'), '/jp/13/?view=feed');
  assert.equal(feedEntryHref('all', 'computing'), '/all/?view=feed&cat=computing');
  assert.equal(feedEntryHref('us-ca', 'a b'), '/us/ca/?view=feed'); // 不正な id は捨てる
});
test('不正な範囲は既定(jp)に倒す', () => {
  assert.equal(feedEntryHref('../x'), '/jp/?view=feed');
  assert.equal(feedEntryHref(undefined, undefined), '/jp/?view=feed');
});
test('入口は nofollow の通常リンクで、SSR と app.js が同じ関数を使う', () => {
  assert.match(read('public/index.html'), /<a id="feed-entry"[^>]*href="[^"]+"[^>]*rel="nofollow"/);
  assert.match(read('src/seo.js'), /#feed-entry[\s\S]{0,120}feedEntryHref/);
  assert.match(read('public/app.js'), /MiseFeedEntry\.feedEntryHref/);
});
