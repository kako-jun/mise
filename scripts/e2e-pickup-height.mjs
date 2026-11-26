#!/usr/bin/env node
// ピックアップの高さの確保 (#297) の実ブラウザ検証。仕様は docs/design/ui.md の「ピックアップ」。
// 検査: 長短の差が大きい 3 件を一巡しても、#picks の高さ・見出しの top・下の要素の top が変わらないこと。確保する高さは一番高い 1 件ぶん（それより大きくしない）。
// 検索結果の画面: ?q= の直開き・範囲切替・言語切替でピックアップが追従し、入力のたびには描き直さないこと。
// 条件: 幅 360/390（タッチ）・800/1280 × ja・en、同じページでの幅の変更（1200px の境界を含む）、非表示中の幅の変更、範囲切替、最小フォント 24px、ファビコンの読み込み前後。
// 前提: `npm run db:migrate` 済みのローカル D1 で `npx wrangler dev --local --port 8799` が起動していること（--remote は使わない）。/api/picks はページ内で固定の 3 件に差し替える。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> node scripts/e2e-pickup-height.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799', T = 15000;
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const site = (prefix, name, ja, en, tags = [], extra = {}) => ({
  id: `e2e-h-${prefix}-${name}`, name: `${prefix} ${name}`, url: `https://${prefix.toLowerCase()}-${name.length}.invalid/`, description_ja: ja, description_en: en, country_code: 'JP',
  tags_json: JSON.stringify(tags.map((x) => ({ ja: x, en: `${x}-en` }))), category_ids: [], ...extra,
});
const LONG_TAGS = ['天文学', '観測情報', '研究機関', '一般公開', '教育普及', '暦と時刻', '星空', 'アーカイブ', '施設見学', '刊行物'];
// 表示順の先頭は短いもの（最初の 1 件だけを測る実装を通さない）
const picksFor = (p) => [
  site(p, 'Short', '短い。', 'Short.'),
  site(p, 'Very long site name that has to wrap onto more than one line on narrow screens', '長い紹介文です。観測や研究の情報を、一般向けの解説とあわせて継続的に発信している公式サイト。'.repeat(3), 'A long description of the site that keeps publishing observation and research news with plain explanations for everyone. '.repeat(3), LONG_TAGS,
    { favicon_url: `/icon/${'a'.repeat(32)}.webp`, last_crawled_at: '2026-11-01T00:00:00Z', registered_at: new Date(Date.now() - 86400000).toISOString() }),
  site(p, 'Middle', '中くらいの長さの紹介文。二行くらいになる程度の文章です。', 'A middle sized description that takes about two lines.', ['中くらい', '紹介']),
];
// 範囲切替の確認用: 別の範囲では、より短い組を返す（前の範囲の高さが残らないことも見る）
const OTHER = [site('B', 'One', '一。', 'One.'), site('B', 'Two', '二つ目の紹介文です。'.repeat(4), 'Second description. '.repeat(4), ['二']), site('B', 'Three', '三。', 'Three.')];
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XPpwAAAABJRU5ErkJggg==', 'base64');
const launch = (extra = []) => chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu', ...extra] });
const frames = (p) => p.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
const measure = (p) => p.evaluate(() => {
  const q = (s) => document.querySelector(s), rect = (s) => q(s).getBoundingClientRect(), r1 = (v) => Math.round(v * 10) / 10, top = (s) => r1(rect(s).top + scrollY);
  const items = [...document.querySelectorAll('#pick-list li')], shown = items.filter((li) => getComputedStyle(li).visibility === 'visible');
  return {
    layout: { picksH: r1(rect('#picks').height), picksTop: top('#picks'), headingTop: top('#picks h2'), panelH: r1(rect('.corner-panel').height), belowTop: top('.author-links'), footerTop: top('.footer-meta') },
    name: shown[0]?.querySelector('.site-title a').textContent, shown: shown.length, firstIsShown: shown[0] === items[0],
    listH: r1(rect('#pick-list').height), naturalH: r1(shown[0]?.getBoundingClientRect().height ?? 0),
    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
});
const next = (p, key = 'ArrowRight') => p.evaluate((k) => document.querySelector('#picks').dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })), key);
// 3 件を一巡して戻り、戻る方向も 2 件ぶん確認する。返り値は一定だった配置と、各件の自然な高さ
async function cycle(page, label) {
  await page.evaluate(() => document.fonts.ready); await frames(page);
  const seen = [];
  for (const key of [null, 'ArrowRight', 'ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowLeft']) {
    if (key) { await next(page, key); await frames(page); }
    seen.push(await measure(page));
  }
  assert.equal(new Set(seen.map((m) => m.name)).size, 3, `${label}: 3 件を一巡していない`);
  for (const m of seen) {
    assert.deepEqual(m.layout, seen[0].layout, `${label}: ${m.name} で配置が動いた ${JSON.stringify(seen.map((x) => [x.name?.slice(0, 12), x.layout]))}`);
    assert.equal(m.shown, 1, `${label}: 見えている行は 1 件`);
    assert.ok(m.firstIsShown, `${label}: 表示中の行が先頭`);
    assert.ok(m.overflowX <= 0, `${label}: 横にはみ出す ${m.overflowX}`);
  }
  const natural = Object.fromEntries(seen.map((m) => [m.name, m.naturalH]));
  assert.ok(Math.abs(seen[0].listH - Math.max(...Object.values(natural))) <= 0.5, `${label}: 確保する高さが一番高い 1 件と違う list=${seen[0].listH} ${JSON.stringify(natural)}`);
  return { layout: seen[0].layout, natural: Object.values(natural) };
}
async function open(browser, { width, lang = 'ja', touch = width < 600, iconDelay = 0, start = '/', ready = '#pick-list li a' }) {
  const ctx = await browser.newContext({ viewport: { width, height: 800 }, hasTouch: touch, isMobile: touch });
  const page = await ctx.newPage(); page.setDefaultTimeout(60000);
  await page.route((u) => u.hostname !== new URL(BASE).hostname, (r) => r.abort()); // 外部は読まない
  await page.route('**/api/picks*', (r) => r.fulfill({ json: new URL(r.request().url()).searchParams.get('scope') === 'jp' ? picksFor('A') : OTHER }));
  let release; const gate = new Promise((r) => { release = r; });
  await page.route('**/icon/*', async (r) => { if (iconDelay) await gate; await r.fulfill({ contentType: 'image/png', body: PNG }); });
  await page.addInitScript((l) => {
    localStorage.setItem('mise-language', l);
    const si = window.setInterval.bind(window); window.setInterval = (fn, ms, ...a) => (ms === 8000 ? -1 : si(fn, ms, ...a)); // 自動入れ替えは発火させない
  }, lang);
  await page.goto(BASE + start, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction((s) => window.__miseBooted && document.querySelector(s), ready, { timeout: T });
  return { ctx, page, release };
}
let failed = 0;
async function scenario(name, browser, opts, fn) {
  let o;
  try { o = await open(browser, opts); const info = await fn(o.page, o); console.log(`ok   ${name}${info ? ` ${info}` : ''}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message.split('\n')[0].slice(0, 900)}`); } finally { await o?.ctx.close(); }
}
const summary = (r) => `#picks=${r.layout.picksH} 各件=${r.natural.join('/')}`;
const browser = await launch();
for (const width of [360, 390, 800, 1280]) for (const lang of ['ja', 'en']) {
  await scenario(`幅${width} ${lang}: 一巡しても動かない`, browser, { width, lang }, async (page) => {
    const r = await cycle(page, `${width} ${lang}`);
    assert.ok(Math.max(...r.natural) - Math.min(...r.natural) > 40, `長短の差が小さい ${r.natural}`);
    return summary(r);
  });
}
await scenario('同じページで幅を変更（1200px の境界を含む）しても、各幅の一番高い件に追従する', browser, { width: 1280, touch: false }, async (page) => {
  const out = [];
  for (const width of [1280, 360, 800, 1199, 1200, 390, 1280]) {
    await page.setViewportSize({ width, height: 800 }); out.push(`${width}:${(await cycle(page, `resize ${width}`)).layout.picksH}`);
  }
  return out.join(' ');
});
await scenario('非表示の間に幅を変えても、再表示した高さは一番高い件', browser, { width: 1280, touch: false }, async (page) => {
  await page.evaluate(() => { document.querySelector('#picks').hidden = true; });
  await page.setViewportSize({ width: 360, height: 800 }); await frames(page); await next(page); await frames(page);
  await page.evaluate(() => { document.querySelector('#picks').hidden = false; });
  return summary(await cycle(page, 'hidden'));
});
await scenario('言語切替の後も一定', browser, { width: 390 }, async (page) => {
  await cycle(page, 'ja'); await page.click('#language-toggle'); return summary(await cycle(page, 'ja→en'));
});
for (const width of [390, 1280]) {
  await scenario(`幅${width}: 範囲切替の後は、新しい 3 件の一番高い件に合わせる`, browser, { width }, async (page) => {
    const a = await cycle(page, 'jp');
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('mise-globe-pick', { detail: { code: 'US' } })));
    await page.waitForFunction(() => document.querySelector('#pick-list li a').textContent.startsWith('B '), null, { timeout: T });
    const b = await cycle(page, 'us');
    assert.ok(b.layout.picksH < a.layout.picksH, `前の範囲の高さが残っている ${a.layout.picksH} → ${b.layout.picksH}`);
    return `${a.layout.picksH} → ${b.layout.picksH}`;
  });
}
await scenario('ファビコンの読み込み前後で高さが変わらない', browser, { width: 360, iconDelay: 1 }, async (page, o) => {
  const before = (await cycle(page, 'icon 読み込み前')).layout;
  o.release();
  await page.waitForFunction(() => [...document.querySelectorAll('#pick-list .site-icon img')].some((i) => i.complete && i.naturalWidth), null, { timeout: T });
  assert.deepEqual((await cycle(page, 'icon 読み込み後')).layout, before);
});
await scenario('見えない行はフォーカスもクリックもできない。左右キーのフォーカスは表示中の行に残る', browser, { width: 1280, touch: false }, async (page) => {
  await next(page); await frames(page); // 長い件を非表示側に回す前後で確認する
  await next(page); await frames(page);
  const bad = await page.evaluate(() => [...document.querySelectorAll('#pick-list li')].slice(1).flatMap((li) => [...li.querySelectorAll('a, button, [tabindex]')]).filter((el) => { el.focus(); return document.activeElement === el; }).map((el) => el.textContent));
  assert.deepEqual(bad, []);
  const hit = await page.evaluate(() => {
    const list = document.querySelector('#pick-list'); list.scrollIntoView({ block: 'center' }); const r = list.getBoundingClientRect(); let n = 0;
    for (let y = r.top + 1; y < r.bottom; y += 4) for (let x = r.left + 1; x < r.right; x += 8) { const li = document.elementFromPoint(x, y)?.closest('#pick-list li'); if (li && li !== list.firstElementChild) n++; }
    return n;
  });
  assert.equal(hit, 0);
  await page.focus('#pick-list li a'); await page.keyboard.press('ArrowRight'); await frames(page);
  assert.equal(await page.evaluate(() => document.querySelector('#pick-list li').contains(document.activeElement)), true);
});
// 検索結果の画面（showResultsNow）でもピックアップは描かれ、選び直し・言語の変更に追従する。入力のたびには描き直さない
const settled = (p) => p.waitForFunction(() => document.querySelector('#pick-list li') && document.querySelector('#pick-list li').getAnimations().every((a) => a.playState === 'finished'), null, { timeout: T });
const type = async (p, q) => { await p.fill('#query', q); await p.waitForFunction((v) => !document.querySelector('#results').hidden && document.querySelector('#result-title').textContent.includes(v), q, { timeout: T }); await frames(p); };
const pickState = (p) => p.evaluate(() => { const li = document.querySelector('#pick-list li'); return li ? { name: li.querySelector('.site-title a').textContent, desc: li.querySelector('.site-content > p').textContent } : null; });
const until = async (p, fn, arg, label) => { try { await p.waitForFunction(fn, arg, { timeout: 5000 }); } catch (_) { assert.fail(`${label}: ${JSON.stringify(await pickState(p))}`); } };
const prefixIs = (x) => document.querySelector('#pick-list li a')?.textContent.startsWith(x);
const toScope = (p, code) => p.evaluate((c) => window.dispatchEvent(new CustomEvent('mise-globe-pick', { detail: { code: c } })), code);
for (const width of [390, 1280]) {
  await scenario(`幅${width}: ?q= を直接開いてもピックアップが出る`, browser, { width, start: '/?q=llll', ready: '#results:not([hidden])' }, async (page) => {
    await until(page, prefixIs, 'A ', '?q= 直開きでピックアップが空');
    return summary(await cycle(page, '?q='));
  });
  await scenario(`幅${width}: 検索結果の画面で範囲を切り替えると、すぐ新しい組になり高さが一定`, browser, { width }, async (page) => {
    await type(page, 'llll'); const a = await cycle(page, 'search jp');
    await toScope(page, 'US'); await page.waitForFunction(() => location.pathname === '/us/', null, { timeout: T });
    await until(page, prefixIs, 'B ', '範囲切替後も前の範囲のピックアップが残る');
    const b = await cycle(page, 'search us');
    assert.ok(b.layout.picksH < a.layout.picksH, `高さが新しい組になっていない ${a.layout.picksH} → ${b.layout.picksH}`);
    return `${a.layout.picksH} → ${b.layout.picksH}`;
  });
}
await scenario('検索の入力では描き直さない（同じ要素のまま・入場アニメなし・表示中の件とフォーカスは巻き戻らない）', browser, { width: 1280, touch: false }, async (page) => {
  await next(page); await frames(page); await type(page, 'l'); await settled(page);
  const before = await pickState(page); assert.ok(before.name.startsWith('A Very long'));
  await page.evaluate(() => { window.__li = document.querySelector('#pick-list li'); window.__added = 0; new MutationObserver((ms) => { for (const m of ms) window.__added += m.addedNodes.length; }).observe(document.querySelector('#pick-list'), { childList: true }); });
  for (const q of ['ll', 'lll', 'llll']) await type(page, q);
  // フォーカスをピックアップに置いたまま、検索の入力イベントだけを起こす
  await page.focus('#pick-list li a');
  await page.evaluate(() => { const q = document.querySelector('#query'); q.value = 'lll'; q.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.waitForFunction(() => new URLSearchParams(location.search).get('q') === 'lll', null, { timeout: T }); await frames(page);
  assert.deepEqual(await page.evaluate(() => ({ same: window.__li === document.querySelector('#pick-list li'), added: window.__added, running: window.__li.getAnimations().filter((a) => a.playState !== 'finished').length, focus: window.__li.contains(document.activeElement) })), { same: true, added: 0, running: 0, focus: true });
  assert.deepEqual(await pickState(page), before);
});
await scenario('検索結果の画面の言語切替は、表示中の件を保ってピックアップの言語も変える', browser, { width: 390 }, async (page) => {
  await type(page, 'llll'); await next(page); await frames(page);
  const ja = await pickState(page);
  await page.click('#language-toggle');
  await until(page, () => document.querySelector('#pick-list li .site-content > p')?.textContent.startsWith('A long description'), null, '言語切替後もピックアップが前の言語のまま');
  assert.equal((await pickState(page)).name, ja.name, '表示中の件が巻き戻った');
  return summary(await cycle(page, 'search en'));
});
await browser.close();
const big = await launch(['--blink-settings=minimumFontSize=24,minimumLogicalFontSize=24']);
for (const width of [360, 1280]) for (const lang of ['ja', 'en']) {
  await scenario(`最小フォント24px 幅${width} ${lang}`, big, { width, lang }, async (page) => {
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('#pick-list .site-title a')).fontSize), '24px', '最小フォントが効いていない');
    return summary(await cycle(page, `font24 ${width} ${lang}`));
  });
}
await big.close();
console.log(failed ? `${failed} failed` : 'all passed'); process.exit(failed ? 1 : 0);
