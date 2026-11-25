#!/usr/bin/env node
// 縦フィードの自動送りの停止(#292 レビュー対応): 動きを減らす設定で止まる・バー(.f-progress)のタップと Space で一時停止/再開・カード内フォーカスと指が触れている間は進めない・
// 別のカードへ移ると一時停止が解ける・検索を閉じる/タブ復帰/向きの変更/最後のカードの挙動を、スマホ幅の実ブラウザで確認する。間隔は route で 3 秒に縮める(置換できなければ落とす)。
// 前提: wrangler dev --local とローカル D1(サイト数件以上)。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-feed-autopause.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799';
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter(x => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
const INTERVAL = 3000;
async function open(opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, ...opts });
  const page = await ctx.newPage(); let replaced = false;
  await page.route('**/feed.js*', async r => { const res = await r.fetch(); const src = await res.text(); replaced = src.includes('const AUTO_ADVANCE_MS = 12000;'); await r.fulfill({ response: res, body: src.replace('const AUTO_ADVANCE_MS = 12000;', `const AUTO_ADVANCE_MS = ${INTERVAL};`) }); });
  await page.goto(BASE + '/all/?category=recent&view=feed'); await page.waitForSelector('.f-card.is-active'); await page.waitForTimeout(800);
  assert.ok(replaced, 'AUTO_ADVANCE_MS の置換が効いている(効かなければ検証にならない)');
  assert.equal(await page.evaluate(() => document.querySelector('.f-auto')), null);
  return { ctx, page, cdp: await ctx.newCDPSession(page) };
}
const idx = page => page.evaluate(() => Number(document.querySelector('.f-card.is-active')?.dataset.index));
const bar = page => page.evaluate(() => { const b = document.querySelector('.f-progress'); return { state: b.dataset.state, pressed: b.getAttribute('aria-pressed'), p: parseFloat(b.firstChild.style.transform.replace(/[^0-9.]/g, '')) || 0, inert: b.inert, op: getComputedStyle(b).opacity, pe: getComputedStyle(b).pointerEvents, h: b.getBoundingClientRect().height }; });
const log = (k, v) => console.log(k.padEnd(34), typeof v === 'string' ? v : JSON.stringify(v));
try {
  // 1. 動きを減らす設定: 自動送りしない・バーは出ない/押せない
  { const { ctx, page } = await open({ reducedMotion: 'reduce' }); const i0 = await idx(page); await page.waitForTimeout(INTERVAL * 2 + 500);
    const b = await bar(page); log('reduce', { i0, i1: await idx(page), ...b }); assert.equal(await idx(page), i0); assert.equal(b.state, 'off'); assert.equal(b.op, '0'); assert.ok(b.inert);
    await page.emulateMedia({ reducedMotion: 'no-preference' }); await page.waitForTimeout(INTERVAL + 1500); log('reduce -> no-preference(即時反映)', await idx(page)); assert.ok(await idx(page) > i0, '設定を戻すと自動送りが動く');
    await page.emulateMedia({ reducedMotion: 'reduce' }); await page.waitForTimeout(300); const i2 = await idx(page); await page.waitForTimeout(INTERVAL + 800); assert.equal(await idx(page), i2, '設定を変えると即止まる'); assert.equal((await bar(page)).state, 'off'); await ctx.close(); }
  // 2. バーのタップ・Space で一時停止/再開。当たり判定・ラベル
  { const { ctx, page } = await open(); const h = await page.evaluate(() => { const b = document.querySelector('.f-progress'); const r = b.getBoundingClientRect(); return { top: r.top, hit: document.elementFromPoint(195, 844 - 20) === b, hit2: document.elementFromPoint(195, 844 - 2) === b, role: b.getAttribute('role'), label: b.getAttribute('aria-label'), tab: b.tabIndex }; });
    log('bar', h); assert.ok(h.hit && h.hit2 && h.role === 'button' && h.label && h.tab === 0);
    assert.ok((await bar(page)).h <= 4.01, '見た目は 4px のまま');
    await page.waitForTimeout(1000); const i0 = await idx(page);
    await page.touchscreen.tap(195, 844 - 20); await page.waitForTimeout(100); const b1 = await bar(page); await page.waitForTimeout(INTERVAL + 1500); const b2 = await bar(page);
    log('tap pause', { b1, b2, same: await idx(page) === i0 }); assert.equal(b1.pressed, 'true'); assert.equal(b2.state, 'paused'); assert.ok(Math.abs(b2.p - b1.p) < 0.001, '現在位置で止まる'); assert.equal(await idx(page), i0);
    await page.touchscreen.tap(195, 844 - 20); await page.waitForTimeout(100); assert.equal((await bar(page)).pressed, 'false'); const t0 = Date.now();
    await page.waitForFunction(i => Number(document.querySelector('.f-card.is-active')?.dataset.index) !== i, i0, { timeout: INTERVAL + 2000 }); log('tap resume -> advance ms', String(Date.now() - t0)); assert.ok(Date.now() - t0 < INTERVAL, '位置から続くので 1 周より短い');
    // Space / Enter
    await page.waitForTimeout(500); await page.focus('.f-progress'); await page.keyboard.press('Space'); await page.waitForTimeout(100); assert.equal((await bar(page)).pressed, 'true'); const i1 = await idx(page); await page.waitForTimeout(INTERVAL + 1000); assert.equal(await idx(page), i1);
    // 一時停止中に別のカードへ移ると解除され、0 から伸びる
    await page.keyboard.press('j'); await page.waitForTimeout(700); const b3 = await bar(page); log('pause -> j', b3); assert.equal(b3.pressed, 'false'); assert.equal(b3.state, 'running'); assert.ok(b3.p < 0.3);
    await page.focus('.f-progress'); await page.keyboard.press('Enter'); assert.equal((await bar(page)).pressed, 'true'); await page.keyboard.press('Enter'); assert.equal((await bar(page)).pressed, 'false');
    await ctx.close(); }
  // 3. カード内のフォーカス(:focus-visible でなくても)で進めない。指がカード本文に触れている間も進めない
  { const { ctx, page, cdp } = await open(); const i0 = await idx(page);
    await page.evaluate(() => { const a = document.querySelector('.f-card.is-active a, .f-card.is-active button'); a.focus(); }); await page.waitForTimeout(INTERVAL + 1500);
    const b = await bar(page); log('focus in card', { ...b, same: await idx(page) === i0 }); assert.equal(b.state, 'held'); assert.equal(await idx(page), i0);
    await page.evaluate(() => document.activeElement.blur()); await page.waitForTimeout(100);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 195, y: 300, id: 1 }] }); await page.waitForTimeout(INTERVAL + 1500);
    const b2 = await bar(page); log('finger on card body', { ...b2, same: await idx(page) === i0 }); assert.equal(b2.state, 'moving'); assert.equal(await idx(page), i0); await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await ctx.close(); }
  // 4. 検索を閉じたあと(フォーカスが外れた後)・タブ復帰・向きの変更・最後のカード
  { const { ctx, page } = await open(); await page.focus('.f-search input'); await page.waitForTimeout(INTERVAL + 1000); const bs = await bar(page); log('search focused', bs); assert.equal(bs.state, 'held');
    await page.evaluate(() => document.activeElement.blur()); const t0 = Date.now(); const i0 = await idx(page);
    await page.waitForTimeout(500); log('search closed +0.5s', (await bar(page)).state); assert.equal((await bar(page)).state, 'quiet', '閉じた直後は 4 秒待つ(勝手に動かさない)');
    await page.waitForFunction(() => document.querySelector('.f-progress').dataset.state === 'running', null, { timeout: 6000 }); log('search closed -> bar running ms', String(Date.now() - t0)); assert.ok(Date.now() - t0 >= 3800);
    // タブ復帰
    // ヘッドレスでは別ページを前面に出しても document.hidden が変わらないため、hidden を差し替えて visibilitychange を発火する(本物のタブ非表示は未確認)
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); }); await page.waitForTimeout(2500);
    const bh = await bar(page); log('tab hidden', bh); assert.equal(bh.state, 'held');
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => false }); document.dispatchEvent(new Event('visibilitychange', { bubbles: true })); }); await page.waitForTimeout(300); const bv = await bar(page); log('tab visible +0.3s', bv); assert.ok(bv.state === 'running' && bv.p < 0.2, '復帰したら待たず 0 から');
    // 向き
    await page.setViewportSize({ width: 844, height: 390 }); await page.waitForTimeout(800); const bo = await bar(page); log('landscape', bo); assert.equal(bo.state, 'running'); await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(800);
    // 最後のカード: 自動送りで終了カードへ進んだら止まり、巻き戻さない
    const total = await page.evaluate(() => document.querySelectorAll('.f-card:not(.f-end)').length);
    for (let i = 0; i < total + 2; i++) { await page.keyboard.press('j'); await page.waitForTimeout(40); } await page.waitForTimeout(1200);
    const be = await bar(page), ie = await page.evaluate(() => [...document.querySelectorAll('.f-card')].findIndex(c => c.classList.contains('is-active'))); log('end card', { ...be, ie, total }); assert.equal(be.state, 'off');
    await page.waitForTimeout(INTERVAL * 2); assert.equal(await page.evaluate(() => [...document.querySelectorAll('.f-card')].findIndex(c => c.classList.contains('is-active'))), ie, '巻き戻さない'); await ctx.close(); }
  console.log('e2e-feed-autopause: OK');
} finally { await browser.close(); }
