#!/usr/bin/env node
// 画面内の操作でカテゴリ画面を開いたらページ先頭を表示する (#295) の実ブラウザ検証。仕様は docs/design/ui.md の「カテゴリ」まわり。
// 前提: `npm run db:migrate` と `node scripts/dev-seed-sites.mjs 300` 済みのローカル D1 で `npx wrangler dev --local --port 8799` が起動していること。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> node scripts/e2e-category-scroll.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799';
const T = 15000;
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]))) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
let failed = 0;
const scrollY = (p) => p.evaluate(() => Math.round(scrollY));
// 画面の入れ替え（View Transition を含む）が終わり、一覧の読み込みも終わるまで待つ
const settled = (p) => p.waitForFunction(() => window.__miseBooted && !document.getAnimations().some((a) => a.effect?.pseudoElement?.startsWith('::view-transition'))
  && ![...document.querySelectorAll('.list-status')].some((e) => /読み込み中|Loading/.test(e.textContent)), null, { timeout: T }).then(() => p.waitForTimeout(350));
const untilY = async (p, y, msg) => {
  await p.waitForFunction((v) => Math.abs(scrollY - v) <= 2, y, { timeout: T }).catch(() => {});
  const got = await scrollY(p); assert.ok(Math.abs(got - y) <= 2, `${msg}: scrollY=${got} 期待=${y}`);
};
async function scrollDown(p, y) {
  await p.evaluate((v) => scrollTo(0, v), y);
  const got = await scrollY(p); assert.ok(got > 50, `前提: 下へスクロールできる（scrollY=${got}）`);
  await p.waitForTimeout(250); return got;
}
// Playwright の click() は要素を画面内へスクロールしてしまうので、DOM の click() で押す（button=0・修飾キーなし）
async function clickLink(p, selector, urlPart) {
  await p.locator(selector).first().evaluate((el) => el.click());
  await p.waitForFunction((s) => (location.pathname + location.search).endsWith(s), urlPart, { timeout: T });
  await settled(p);
}
const CITIES = '/jp/?category=government&category=cities';
const sizes = [['PC', { width: 1280, height: 500 }, {}], ['スマホ', { width: 390, height: 600 }, { hasTouch: true, isMobile: true }]];
for (const [label, viewport, extra] of sizes) {
  for (const reducedMotion of ['no-preference', 'reduce']) {
    const tag = `${label} reducedMotion=${reducedMotion}`;
    const scenario = async (name, fn) => {
      const ctx = await browser.newContext({ viewport, reducedMotion, locale: 'ja-JP', ...extra }); const page = await ctx.newPage(); page.setDefaultTimeout(T);
      // View Transition を始めた瞬間（旧画面を撮る前）の位置を記録する
      await page.addInitScript(() => { window.__vt = []; const o = document.startViewTransition; if (o) document.startViewTransition = function (cb) { window.__vt.push(Math.round(scrollY)); return o.call(document, cb); }; });
      try { await fn(page); console.log(`ok   [${tag}] ${name}`); } catch (e) { failed++; console.log(`FAIL [${tag}] ${name}\n     ${e.message.split('\n')[0]}`); } finally { await ctx.close(); }
    };
    await scenario('トップのカテゴリ行 → 先頭。戻るで前の位置（一覧の無い画面）、進むで先頭', async (page) => {
      await page.goto(BASE + '/'); await page.waitForSelector('#categories a'); await settled(page);
      const y = await scrollDown(page, 120);
      await clickLink(page, '#categories h2 a[href$="category=government"]', '?category=government');
      await untilY(page, 0, 'カテゴリ行');
      const vt = await page.evaluate(() => window.__vt);
      if (reducedMotion === 'reduce') assert.equal(vt.length, 0); else assert.equal(vt.at(-1), y, 'startViewTransition の呼び出し時点ではまだ先頭へ戻していない');
      await page.goBack(); await page.waitForFunction(() => !location.search, null, { timeout: T }); await settled(page);
      await untilY(page, y, '戻る');
      await page.goForward(); await page.waitForFunction(() => location.search === '?category=government', null, { timeout: T }); await settled(page);
      await untilY(page, 0, '進む');
    });
    await scenario('一覧のある画面でパンくず → 先頭。戻る・再読込で前の位置', async (page) => {
      await page.goto(BASE + CITIES); await page.waitForSelector('#directory-sites li'); await settled(page);
      const y = await scrollDown(page, 900);
      await clickLink(page, '#breadcrumbs a[href$="category=government"]', '?category=government');
      await untilY(page, 0, 'パンくず（中間）');
      await page.goBack(); await page.waitForFunction((u) => location.pathname + location.search === u, CITIES, { timeout: T }); await settled(page);
      await untilY(page, y, '戻る');
      await page.reload(); await page.waitForSelector('#directory-sites li'); await settled(page);
      await untilY(page, y, '再読込');
      await clickLink(page, '#breadcrumbs a:first-child', '/jp/');
      await untilY(page, 0, 'パンくず（トップ）');
      await page.goBack(); await page.waitForFunction((u) => location.pathname + location.search === u, CITIES, { timeout: T }); await settled(page);
      await untilY(page, y, 'トップから戻る');
    });
    await scenario('検索結果の行の所属欄 → 先頭。戻るで前の位置', async (page) => {
      await page.goto(BASE + '/jp/?q=' + encodeURIComponent('合成')); await page.waitForSelector('#result-list li'); await settled(page);
      const y = await scrollDown(page, 900);
      await clickLink(page, '#result-list .site-categories a[href$="category=cities"]', CITIES);
      await untilY(page, 0, '所属欄');
      await page.goBack(); await page.waitForFunction(() => location.search.startsWith('?q='), null, { timeout: T }); await page.waitForSelector('#result-list li'); await settled(page);
      await untilY(page, y, '戻る');
    });
    await scenario('ロゴ → 先頭。戻るで前の位置（一覧のある画面・範囲なしの "/"・同じ画面）', async (page) => {
      await page.goto(BASE + CITIES); await page.waitForSelector('#directory-sites li'); await settled(page);
      const y = await scrollDown(page, 900);
      await clickLink(page, '#home-link', '/jp/');
      await untilY(page, 0, 'ロゴ（カテゴリ画面から）');
      await page.goBack(); await page.waitForFunction((u) => location.pathname + location.search === u, CITIES, { timeout: T }); await settled(page);
      await untilY(page, y, '戻る');
      await page.goto(BASE + '/'); await page.waitForSelector('#categories a'); await settled(page);
      const top = await scrollDown(page, 120);
      await clickLink(page, '#home-link', '/jp/');
      await untilY(page, 0, 'ロゴ（"/" から）');
      await page.goBack(); await page.waitForFunction(() => location.pathname === '/', null, { timeout: T }); await settled(page);
      await untilY(page, top, '"/" へ戻る');
      await page.goto(BASE + '/jp/'); await page.waitForSelector('#categories a'); await settled(page);
      await scrollDown(page, 120);
      const len = await page.evaluate(() => history.length);
      await clickLink(page, '#home-link', '/jp/');
      await untilY(page, 0, 'ロゴ（同じ画面）');
      assert.equal(await page.evaluate(() => history.length), len, '同じ画面では履歴を積まない');
    });
    for (const [from, start, dest] of [['範囲のトップ', '/jp/', '/jp/'], ['カテゴリ画面', CITIES, CITIES]]) {
      await scenario(`検索結果の「カテゴリに戻る」（${from}から検索） → 先頭。戻るで前の位置`, async (page) => {
        await page.goto(BASE + start); await page.waitForSelector('#categories a, #directory-sites li', { state: 'attached' }); await settled(page);
        // 検索欄が非表示の画面幅でも検索できるよう、値を入れて input を発火する
        await page.locator('#query').evaluate((el) => { el.value = '合成'; el.dispatchEvent(new Event('input', { bubbles: true })); });
        await page.waitForSelector('#result-list li'); await settled(page);
        const y = await scrollDown(page, 400);
        await clickLink(page, '#clear', dest);
        await untilY(page, 0, 'カテゴリに戻る');
        await page.goBack(); await page.waitForFunction(() => location.search.includes('q='), null, { timeout: T }); await page.waitForSelector('#result-list li'); await settled(page);
        await untilY(page, y, '戻る');
      });
    }
    await scenario('言語切替の再描画と修飾キー付きクリックは位置を変えない', async (page) => {
      await page.goto(BASE + CITIES); await page.waitForSelector('#directory-sites li'); await settled(page);
      const y = await scrollDown(page, 300);
      await page.locator('#breadcrumbs a').first().evaluate((el) => { el.addEventListener('click', (e) => e.preventDefault()); el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true })); });
      await page.waitForTimeout(300); await untilY(page, y, '修飾キー'); assert.equal(await page.evaluate(() => location.pathname + location.search), CITIES);
      await page.locator('#home-link').evaluate((el) => { el.addEventListener('click', (e) => e.preventDefault()); el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true })); });
      await page.waitForTimeout(300); await untilY(page, y, '修飾キー（ロゴ）'); assert.equal(await page.evaluate(() => location.pathname + location.search), CITIES);
      await page.locator('#language-toggle').evaluate((el) => el.click()); await settled(page);
      assert.ok(await scrollY(page) > 50, `言語切替で先頭へ戻らない（scrollY=${await scrollY(page)}）`);
    });
  }
}
await browser.close();
if (failed) { console.log(`${failed} 件失敗`); process.exit(1); }
console.log('全シナリオ成功');
