#!/usr/bin/env node
// フォルダ・新着・検索への切り替えを PC とスマホの実ブラウザで確認する。
// 前提: wrangler dev --local とローカル D1（JP-13 と computing/programming を含む）。
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

const category = '/jp/13/?category=computing&category=programming';
const recent = '/jp/?category=recent';
const feedUrl = path => path + '&view=feed';
const goto = async (page, url) => {
  const response = await page.goto(BASE + url);
  assert.equal(response.status(), 200, url);
  await page.waitForFunction(() => window.__miseBooted);
};
const shown = (page, selector) => page.locator(selector).evaluate(el => {
  const cs = getComputedStyle(el);
  return cs.display !== 'none' && cs.visibility === 'visible' && el.getClientRects().length > 0;
});

try {
  for (const [name, width, dark, lang] of [['pc', 1280, false, 'en'], ['mobile', 390, true, 'ja'], ['mobile', 390, false, 'en']]) {
    const label = [name, dark ? 'dark' : 'light', lang].join('-');
    const context = await browser.newContext({ viewport: { width, height: 844 }, isMobile: name === 'mobile', hasTouch: name === 'mobile' });
    await context.addInitScript(([d, l]) => {
      localStorage.setItem('mise-theme', d ? 'dark' : 'light');
      localStorage.setItem('mise-language', l);
    }, [dark, lang]);
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await goto(page, '/jp/');
    assert.equal(await shown(page, '#feed-entry'), false);
    assert.equal(await page.locator('#categories > :first-child .recent-feed').textContent(), lang === 'ja' ? '新着' : 'New arrivals');
    assert.equal(await page.locator('.recent-feed').getAttribute('href'), feedUrl(recent));
    assert.equal(await page.locator('.recent-list').count(), 0);

    await page.locator('.recent-feed').click();
    await page.waitForURL(BASE + feedUrl(recent));
    await page.waitForSelector('#feed .f-card');
    const header = await page.locator('#feed .f-bar').evaluate(el => {
      // #198: 帯はなく、矢印が左端・猫カウンター(.f-surf)が右端・検索欄は左右中央(幅 700px 未満は矢印とカウンターのあいだの 1 行)
      const [back, search, surf] = el.children;
      const box = el.getBoundingClientRect(), a = back.getBoundingClientRect(), b = search.getBoundingClientRect(), c = surf.getBoundingClientRect();
      const cs = getComputedStyle(el), narrow = innerWidth < 700;
      return {
        children: [...el.children].map(child => child.tagName + (child.classList.contains('f-surf') ? '.f-surf' : '')),
        bandless: cs.backgroundColor === 'rgba(0, 0, 0, 0)' && cs.backdropFilter === 'none' && cs.borderBottomWidth === '0px',
        edges: a.left - box.left <= 16 && box.right - c.right <= 16 && a.right <= c.left,
        centered: narrow || Math.abs((b.left + b.right) / 2 - (box.left + box.right) / 2) <= 2,
        row: a.right <= b.left && b.right <= c.left && (!narrow || Math.abs(a.top - b.top) < 12), // 幅を問わず 1 行(矢印 | 検索欄 | カウンター)
        visible: cs.position === 'fixed' && [back, search, surf].every(x => getComputedStyle(x).display !== 'none'),
      };
    });
    assert.deepEqual(header, { children: ['A', 'FORM', 'BUTTON.f-surf'], bandless: true, edges: true, centered: true, row: true, visible: true });
    assert.match(await page.locator('meta[name="robots"]').getAttribute('content'), /noindex/);
    if (SHOTS) await page.screenshot({ path: SHOTS + '/feed-' + label + '.png' });
    await page.locator('.f-directory').click();
    await page.waitForURL(BASE + recent);
    assert.equal(await shown(page, '#feed-entry'), true);

    await goto(page, category);
    assert.equal(await page.locator('#feed-entry').getAttribute('href'), feedUrl(category));
    const entrySize = await page.locator('#feed-entry').boundingBox();
    assert.ok(entrySize.height >= (name === 'mobile' ? 43.7 : 33.7));
    await page.locator('#feed-entry').click();
    await page.waitForURL(BASE + feedUrl(category));
    await page.waitForSelector('#feed .f-card');
    assert.equal(await page.locator('.f-directory').getAttribute('href'), category);
    const rows = await (await page.request.get(BASE + '/api/sites?scope=jp-13&category=programming&limit=5')).json();
    const cards = await page.locator('#feed .f-card[data-id]').evaluateAll(items => items.slice(0, 5).map(el => el.dataset.id));
    assert.deepEqual(cards, rows.items.map(site => site.id));
    await page.locator('.f-directory').click();
    await page.waitForURL(BASE + category);
    assert.equal(await shown(page, '#feed-entry'), true);
    await page.locator('#feed-entry').click();
    const input = page.locator('#feed .f-search input');
    await input.fill('blog');
    await input.press('Enter');
    await page.waitForURL(BASE + '/jp/13/?q=blog');
    await page.waitForSelector('#results');
    assert.equal(await shown(page, '#feed-entry'), true);
    assert.equal(await page.locator('#query').inputValue(), 'blog');
    await page.locator('#feed-entry').click();
    await page.waitForURL(BASE + '/jp/13/?q=blog&view=feed');
    await page.waitForSelector('#feed .f-card');
    assert.equal(await page.locator('.f-directory').getAttribute('href'), '/jp/13/?q=blog');
    await page.locator('.f-directory').click();
    await page.waitForURL(BASE + '/jp/13/?q=blog');
    assert.deepEqual(errors, []);
    await context.close();
    console.log(label + ' OK');
  }

  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto(BASE + category);
  assert.equal(await shown(page, '#feed-entry'), true);
  assert.equal(await page.locator('#feed-entry').getAttribute('href'), feedUrl(category));
  await page.goto(BASE + '/jp/');
  assert.equal(await page.locator('#categories > :first-child .recent-feed').getAttribute('href'), feedUrl(recent));
  await context.close();
} finally {
  await browser.close();
}
