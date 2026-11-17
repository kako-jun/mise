import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
const png = readFileSync(new URL('../public/og/og.png', import.meta.url));
test('OGP 画像: 1200x630 の PNG', () => {
  assert.equal(png.subarray(1, 4).toString(), 'PNG');
  assert.equal(png.readUInt32BE(16), 1200);
  assert.equal(png.readUInt32BE(20), 630);
});
test('OGP 画像: og:image / twitter:image / summary_large_image が揃う', () => {
  assert.match(html, /<meta property="og:image" content="https:\/\/mise\.llll-ll\.com\/og\/og\.png">/);
  assert.match(html, /<meta property="og:image:width" content="1200">/);
  assert.match(html, /<meta property="og:image:height" content="630">/);
  assert.match(html, /<meta property="og:image:type" content="image\/png">/);
  assert.match(html, /<meta property="og:image:alt" content="[^"]+">/);
  assert.match(html, /<meta name="twitter:image:alt" content="[^"]+">/);
  assert.match(html, /<meta name="twitter:card" content="summary_large_image">/);
  assert.match(html, /<meta name="twitter:image" content="https:\/\/mise\.llll-ll\.com\/og\/og\.png">/);
});
test('OGP 画像: SVG 正本は文字でなく path（フォント非依存）', () => {
  const svg = readFileSync(new URL('../public/og/mise-og.svg', import.meta.url), 'utf8');
  assert.match(svg, /<path /);
  assert.doesNotMatch(svg, /<text/);
});
