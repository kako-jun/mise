import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { inlineScriptHashes, pageCsp } from '../src/security.js';

// 縦フィードの試作ビュー(?view=feed。#52)。DOM を使う IIFE なので、通常表示を変えないこと・CSP と第三者アクセスの規約を静的に守らせる。
const read = f => readFileSync(new URL('../public/' + f, import.meta.url), 'utf8');
const html = read('index.html'), js = read('feed.js'), css = read('feed.css');

test('index.html: 試作ビューの読み込みフックは ?view=feed のときだけ feed.css / feed.js を足す', () => {
  const hook = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).find(s => s.includes('feed.js'));
  assert.ok(hook, 'フックがある');
  assert.match(hook, /get\("view"\) === "feed"/);
  assert.equal((html.match(/feed\.(js|css)/g) || []).length, 2, 'feed.css と feed.js を静的に読み込まない(通常表示は不変)');
  assert.ok(!/<(link|script)[^>]+feed\./.test(html), 'タグとして直接は読まない');
});

test('CSP: フックのインラインスクリプトはハッシュで許可され、feed.js は同一オリジン(script-src self)で読める', async () => {
  const hashes = await inlineScriptHashes(html);
  const csp = pageCsp(hashes);
  assert.match(csp, /script-src 'self' 'sha256-/);
  assert.ok(hashes.length >= 3);
  assert.match(csp, /style-src 'self'(;|$)/, 'インライン style は許さない');
});

test('feed.js: 第三者へアクセスしない(外部 URL・https サムネ・eval・インライン style 属性を持たない)', () => {
  const code = js.replace(/^\s*\/\/.*$/gm, '');
  const urls = [...code.matchAll(/https?:\/\/[^\s'"`)]+/g)].map(m => m[0]);
  assert.deepEqual(urls.filter(u => u !== 'http://www.w3.org/2000/svg'), [], 'SVG 名前空間以外の絶対 URL なし');
  assert.ok(!/\beval\(|new Function\(|setAttribute\('style'|\.cssText/.test(code));
  assert.ok(!/@import|url\(\s*['"]?https?:/.test(css), 'css も外部を読まない');
  // thumbOf は同一オリジンの絶対パスだけ(//host は不可)
  const m = code.match(/const thumbOf = s => \(typeof s\.thumb_url === 'string' && (\/.+\/)\.test\(s\.thumb_url\)\)/);
  assert.ok(m, 'thumbOf の形');
  const re = new RegExp(m[1].slice(1, -1));
  for (const ok of ['/thumb/a.webp', '/_mock-thumbs/x.webp']) assert.ok(re.test(ok), ok);
  for (const ng of ['https://evil.example/a.png', '//evil.example/a.png', 'data:image/png;base64,AA', 'javascript:alert(1)']) assert.ok(!re.test(ng), ng);
});

test('feed.js: 端末内保存は try/catch で包む(使えなくても動く)', () => {
  const direct = js.split('\n').filter(l => /\b(localStorage|sessionStorage|indexedDB)\s*[.[]/.test(l) && !/try\s*\{/.test(l) && !/^\s*\/\//.test(l));
  assert.deepEqual(direct, [], 'storage への直接アクセスは try の行だけ');
});

test('feed.js: public/ に画像を置かない規約(.webp は R2 経由)のため、モックのサムネ読み込みは持たない', () => {
  assert.ok(!/_mock-thumbs/.test(js));
});
