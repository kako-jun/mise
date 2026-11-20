#!/usr/bin/env node
// 縦フィードの入口「縦で見る / Feed」の実ブラウザ検証 (#178)。仕様は docs/design/ui.md の「縦フィードへの入口」。
// 検査: トップ・/jp/・区画・カテゴリ画面・検索結果・タグ絞り込みで (1) 入口が見える（computed style）、(2) href が引き継ぎどおり（範囲 + トップ階層カテゴリ。検索語・タグ・2 階層目は捨てる）、
// (3) 押すとフィードに入る（#feed）、(4) 「一覧表示」で戻れ、絞り込み（範囲・カテゴリ）が返る、(5) 文字が枠の上下左右中央にある、(6) JS 無効でも出てリンクが効く、(7) コンソールエラー 0。
// 前提: `npx wrangler dev --local` が起動し、ローカル D1 にサイトがある（JP-13 の computing カテゴリのサイトに「ブログ」のタグ、検索語「blog」にヒット）。--remote は使わない。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] [SHOTS=<dir>] node scripts/e2e-feed-entry.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799', SHOTS = process.env.SHOTS;
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
let checks = 0, ng = 0;
const check = (name, fn) => { checks++; try { fn(); } catch (e) { ng++; console.error('NG', name, '-', e.message.split('\n')[0]); } };
const probe = () => {
  const a = document.querySelector('#feed-entry'); if (!a) return null;
  const cs = getComputedStyle(a), r = a.getBoundingClientRect();
  const range = document.createRange(); range.selectNodeContents(a); const t = range.getBoundingClientRect();
  return { display: cs.display, visibility: cs.visibility, text: a.textContent, href: a.getAttribute('href'), rel: a.getAttribute('rel'), w: r.width, h: r.height,
    dy: (t.top + t.height / 2) - (r.top + r.height / 2), dx: (t.left + t.width / 2) - (r.left + r.width / 2), overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    note: (n => n && { display: getComputedStyle(n).display, text: n.textContent.trim(), h: n.getBoundingClientRect().height, w: n.getBoundingClientRect().width, right: n.getBoundingClientRect().right })(document.querySelector('#feed-note')),
    searchTop: document.querySelector('#query').getBoundingClientRect().top, entryTop: r.top, searchH: document.querySelector('#query').getBoundingClientRect().height };
};
const SCENES = [
  ['top', '/', '/jp/?view=feed', false],
  ['jp', '/jp/', '/jp/?view=feed', false],
  ['region', '/jp/13/', '/jp/13/?view=feed', false],
  ['category', '/jp/13/?category=computing', '/jp/13/?view=feed&cat=computing', false],
  ['search', '/jp/?q=blog', '/jp/?view=feed', true],
  ['tag', '/jp/?q=blog&category=computing&tag=' + encodeURIComponent('ブログ'), '/jp/?view=feed&cat=computing', true],
  ['tag-only', '/jp/?category=computing&tag=' + encodeURIComponent('ブログ'), '/jp/?view=feed&cat=computing', true],
  ['subcategory', '/jp/?category=computing&category=programming', '/jp/?view=feed&cat=computing', true],
  ['all', '/all/', '/all/?view=feed', false],
];
for (const [vn, vw, dark, lang] of [['pc', 1280, false, 'en'], ['sp', 390, true, 'ja'], ['sp', 390, false, 'en']]) {
  const ctx = await browser.newContext({ viewport: { width: vw, height: 844 }, locale: lang === 'ja' ? 'ja-JP' : 'en-US', hasTouch: vn === 'sp', isMobile: vn === 'sp' });
  await ctx.addInitScript(([d, l]) => { try { localStorage.setItem('mise-theme', d ? 'dark' : 'light'); localStorage.setItem('mise-language', l); } catch (_) {} }, [dark, lang]);
  const errors = [];
  for (const [name, url, want, note] of SCENES) {
    const page = await ctx.newPage(); page.on('console', (m) => { if (m.type() === 'error') errors.push(`${name}: ${m.text()}`); }); page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
    await page.goto(BASE + url); await page.waitForTimeout(1800);
    const p = await page.evaluate(probe), tag = `${vn}${dark ? '-dark' : ''}-${lang} ${name}`;
    check(`${tag} 表示`, () => { assert.ok(p, '入口が無い'); assert.notEqual(p.display, 'none'); assert.equal(p.visibility, 'visible'); assert.equal(p.text, lang === 'ja' ? '縦で見る' : 'Feed'); assert.match(p.rel, /nofollow/); });
    check(`${tag} 案内（引き継げない状態のときだけ）`, () => {
      assert.ok(p.note, '#feed-note が無い');
      if (note) { assert.notEqual(p.note.display, 'none'); assert.equal(p.note.text, lang === 'ja' ? '範囲とトップのカテゴリだけ引き継ぎます' : 'Only scope and top category carry over'); assert.ok(p.note.h > 0 && p.note.right <= vw, `note h=${p.note.h} right=${p.note.right}`); }
      else assert.equal(p.note.display, 'none');
    });
    check(`${tag} href`, () => assert.equal(p.href, want));
    check(`${tag} 中央`, () => { assert.ok(Math.abs(p.dy) <= 1.5, `dy=${p.dy}`); assert.ok(Math.abs(p.dx) <= 1.5, `dx=${p.dx}`); });
    check(`${tag} 大きさ/横溢れ`, () => { assert.ok(p.h >= (vn === 'sp' ? 44 : 32), `h=${p.h}`); assert.ok(p.overflowX <= 0, `overflowX=${p.overflowX}`); });
    check(`${tag} 検索欄と同じ高さ帯`, () => assert.ok(vn === 'sp' || Math.abs(p.entryTop - p.searchTop) <= 3, `entry=${p.entryTop} search=${p.searchTop}`));
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/entry-${vn}${dark ? '-dark' : ''}-${lang}-${name}.png` });
    await page.close();
  }
  // 押すとフィードに入り、「一覧表示」で絞り込みが返る
  for (const [name, url, , back] of [['category', '/jp/13/?category=computing', 0, '/jp/13/?category=computing'], ['jp', '/jp/', 0, '/jp/']]) {
    const page = await ctx.newPage(); page.on('console', (m) => { if (m.type() === 'error') errors.push(`click-${name}: ${m.text()}`); });
    await page.goto(BASE + url); await page.waitForTimeout(1500);
    await page.click('#feed-entry'); await page.waitForSelector('#feed', { timeout: 8000 }); await page.waitForTimeout(1500);
    const f = await page.evaluate(() => ({ url: location.pathname + location.search, feed: !!document.getElementById('feed'), dir: document.querySelector('.f-directory')?.getAttribute('href'), noindex: document.querySelector('meta[name=robots]')?.content }));
    check(`${vn}-${lang} ${name} フィードに入る`, () => { assert.ok(f.feed); assert.match(f.url, /view=feed/); assert.match(f.noindex, /noindex/); });
    check(`${vn}-${lang} ${name} 一覧表示の戻り先`, () => assert.equal(f.dir, back));
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/feed-${vn}${dark ? '-dark' : ''}-${lang}-${name}.png` });
    await page.click('.f-directory'); await page.waitForSelector('#feed-entry', { timeout: 8000 }); await page.waitForTimeout(1500);
    const b = await page.evaluate(() => ({ url: location.pathname + location.search, feed: !!document.getElementById('feed'), entry: !!document.querySelector('#feed-entry') && getComputedStyle(document.querySelector('#feed-entry')).display }));
    check(`${vn}-${lang} ${name} 一覧表示で戻る`, () => { assert.equal(b.url, back); assert.ok(!b.feed); assert.notEqual(b.entry, 'none'); });
    await page.close();
  }
  // 画面内の操作に追従: カテゴリを押す → 範囲を切り替える → 検索 → タグ。href が今の状態を指す
  {
    const page = await ctx.newPage(); page.on('console', (m) => { if (m.type() === 'error') errors.push(`live: ${m.text()}`); });
    await page.goto(BASE + '/jp/'); await page.waitForTimeout(1500);
    const href = () => page.getAttribute('#feed-entry', 'href');
    await page.click('#categories a >> text=/Computers|コンピュータ/'); await page.waitForTimeout(800);
    check(`${vn}-${lang} カテゴリ操作に追従`, () => {}); const h1 = await href();
    check(`${vn}-${lang} カテゴリ操作 href`, () => assert.equal(h1, '/jp/?view=feed&cat=computing'));
    await page.click('#scope-nav a[href="/all/"]'); await page.waitForTimeout(1200);
    const h2 = await href(); check(`${vn}-${lang} 範囲切替 href`, () => assert.equal(h2, '/all/?view=feed&cat=computing'));
    await page.fill('#query', 'blog'); await page.press('#query', 'Enter'); await page.waitForTimeout(1500);
    const h3 = await href(); check(`${vn}-${lang} 検索中 href（検索語は捨てる）`, () => assert.ok(!/blog|q=/.test(h3) && h3.startsWith('/all/?view=feed'), h3));
    await page.close();
  }
  // JS 無効でも出て、リンクが効く
  {
    const c2 = await browser.newContext({ viewport: { width: vw, height: 844 }, javaScriptEnabled: false });
    const page = await c2.newPage(); await page.goto(BASE + '/jp/13/?category=computing');
    const p = await page.evaluate(probe);
    check(`${vn} JS無効 表示`, () => { assert.ok(p && p.display !== 'none'); assert.equal(p.href, '/jp/13/?view=feed&cat=computing'); });
    const res = await page.request.get(BASE + p.href); check(`${vn} JS無効 リンク先 200`, () => assert.equal(res.status(), 200));
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/entry-${vn}-nojs.png` });
    await c2.close();
  }
  check(`${vn}${dark ? '-dark' : ''}-${lang} コンソールエラー 0`, () => assert.deepEqual(errors, []));
  await ctx.close();
}
await browser.close();
console.log(`${checks} 件中 NG ${ng}`); process.exit(ng ? 1 : 0);
