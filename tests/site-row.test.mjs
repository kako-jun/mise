// 一覧の行（#166）: ファビコンはサイト名の左だけ・枠なし／サイト名より下は常に左端／リンク先ホスト名の表示。
// DOM の実測は実ブラウザで行う（PR に記録）。ここでは表示ホスト名の変換と、構造・CSS の取り決めを静的に守らせる。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const read = f => readFileSync(new URL('../' + f, import.meta.url), 'utf8');
const hostJs = read('public/site-host.js'), css = read('public/style.css'), app = read('public/app.js');
const ctx = { URL, String, Error }; ctx.globalThis = ctx; vm.runInNewContext(hostJs, ctx);
const siteHost = ctx.MiseSiteHost;

test('siteHost: href と同じ URL のホスト名だけ（パス・クエリ・ポート・資格情報は出さない。www. はそのまま）', () => {
  assert.equal(siteHost('https://kinpusen.or.jp/'), 'kinpusen.or.jp');
  assert.equal(siteHost('https://www.example.com/a/b?q=1#x'), 'www.example.com');
  assert.equal(siteHost('http://example.com:8080/'), 'example.com');
  assert.equal(siteHost('https://user:pw@example.com/'), 'example.com');
  assert.equal(siteHost('https://EXAMPLE.com/'), 'example.com');
  assert.equal(siteHost('https://example.com/blog/'), 'example.com'); // サブディレクトリのサイトでもホスト名だけ
});

test('siteHost: 国際化ドメインは Unicode に戻す', () => {
  assert.equal(siteHost('https://日本語.jp/'), '日本語.jp');
  assert.equal(siteHost('https://xn--wgv71a119e.jp/'), '日本語.jp');
  assert.equal(siteHost('https://bücher.example/'), 'bücher.example');
  assert.equal(siteHost('https://xn--bcher-kva.example/'), 'bücher.example');
  assert.doesNotThrow(() => siteHost('https://xn--a-.example/')); // 壊れた punycode でも例外にしない
});

test('siteHost: 不正・空・http(s) 以外は null（何も出さない）', () => {
  for (const v of [null, undefined, '', 'not a url', '/relative', 'javascript:alert(1)', 'data:text/html,x', 'ftp://example.com/']) assert.equal(siteHost(v), null, String(v));
});

test('行の構造: ファビコンは 1 行目（.site-head）のサイト名の左。説明文・タグ・所属は .site-content 直下（インデントしない）', () => {
  assert.match(app, /head\.append\(bullet,icon,title\)/);
  assert.match(app, /content\.append\(head\)/);
  assert.doesNotMatch(app, /li\.append\(icon/);
  assert.match(app, /li\.append\(content\)/);
  assert.match(app, /img\.width=18; img\.height=18/);
  assert.match(app, /icon\.setAttribute\('aria-hidden','true'\)/);
  assert.match(app, /img\.alt=''/);
  assert.match(app, /img\.addEventListener\('error',\(\)=>\{icon\.hidden=true/);
});

const rule = sel => { const out = []; for (const m of css.matchAll(/^([^{}\n]+)\{([^}]*)\}/gm)) if (m[1].split(',').map(s => s.trim()).includes(sel)) out.push(m[2]); return out.join(' '); };

test('CSS: .site-icon とその画像に枠・角丸・縁・下地を付けない（ダーク用の下地も無い）', () => {
  for (const sel of ['.site-icon', '.site-icon img']) {
    const r = rule(sel);
    assert.match(r, /border:\s*0/); assert.match(r, /border-radius:\s*0/); assert.match(r, /box-shadow:\s*none/); assert.match(r, /background:\s*none/);
  }
  assert.doesNotMatch(css, /html\[data-theme="dark"\] \.site-icon/);
  assert.doesNotMatch(css, /\.site-icon[^{]*\{[^}]*(?:rgba\(|#[0-9a-f]{3,6})/i);
  assert.match(rule('.site-icon'), /width:\s*18px/);
});

test('CSS: 行頭の中点は li::before（フォント依存）でなく、1 行目の flex 内の要素を 1 行ぶんの高さの箱の中で align-items:center にする', () => {
  assert.doesNotMatch(css, /(?:#directory-sites|#result-list) li::before/);
  assert.match(rule('.site-head'), /display:\s*flex/); assert.match(rule('.site-bullet'), /align-items:\s*center/); assert.match(rule('.site-icon'), /align-items:\s*center/);
  assert.doesNotMatch(css, /(?:#directory-sites|#result-list|#pick-list) li \{[^}]*display:\s*flex/);
});

test('ホスト名: 案 A（同じ行）が既定、案 B は html[data-url-layout=below] で切替。どちらも aria-hidden にしない', () => {
  assert.match(rule('.site-host-below'), /display:\s*none/);
  assert.match(css, /html\[data-url-layout="below"\] \.site-host-inline \{ display: none/);
  assert.match(css, /html\[data-url-layout="below"\] \.site-host-below \{ display: block/);
  assert.match(rule('.site-host'), /overflow-wrap:\s*anywhere/);
  assert.match(app, /const host=siteHost\(site\.url\)/);
  assert.doesNotMatch(app, /site-host[^;\n]*aria-hidden/);
});
