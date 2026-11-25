#!/usr/bin/env node
// 縦フィードの先読み済みの帯と、指を離したあとの寄せ(#278)を、スマホ幅の実ブラウザ(CDP のタッチ)で確認する。
// 確認: (1) 20px ドラッグした時点で隣のカードの名前・説明が見えている (2) 25/50/75%・速い flick で離したときの行き先とカード境界での停止(停止後に補正で動かない・300ms 前後で止まる)
//       (3) 30 枚送ってもカードの中身(DOM ノード)が窓の分だけに収まる。
// 前提: wrangler dev --local とローカル D1(サイト 35 件以上。scripts/dev-seed-sites.mjs)。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-feed-strip.mjs [baseUrl=http://localhost:8799]
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
const tp = y => [{ x: 195, y, id: 1 }];
async function fresh(reducedMotion = 'no-preference') {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, reducedMotion });
  const page = await ctx.newPage();
  await page.goto(BASE + '/all/?category=government&category=cities&view=feed&seed=e2e');
  await page.waitForSelector('.f-card.is-active'); await page.waitForTimeout(800);
  const auto = page.locator('.f-auto'); if (await auto.getAttribute('aria-pressed') === 'true') await auto.click();
  return { ctx, page, cdp: await ctx.newCDPSession(page) };
}
try {
  const probe = await fresh(); const total = await probe.page.locator('.f-card:not(.f-end)').count(); await probe.ctx.close();
  assert.ok(total >= 35, `サイトが 35 件以上いる(いまは ${total})`);

  // (1) 20px ドラッグ
  { const { ctx, page, cdp } = await fresh();
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(600) });
    for (let y = 595; y >= 580; y -= 5) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(y) }); await page.waitForTimeout(16); }
    await page.waitForTimeout(50);
    const r = await page.evaluate(() => { const o = {}; for (const k of [1, 2, 3]) { const c = document.querySelector(`.f-card[data-index="${k}"]`), nm = c && c.querySelector('.f-name'), ds = c && c.querySelector('.f-desc'), im = c && c.querySelector('.f-hero img.f-img');
      const vis = el => !!el && el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true }) && el.textContent.trim().length > 0;
      o[k] = { name: vis(nm), desc: ds ? vis(ds) : null, img: im ? im.complete && im.naturalWidth > 0 && getComputedStyle(im).opacity === '1' : null }; } return o; });
    console.log('20px drag', JSON.stringify(r));
    for (const k of [1, 2]) { assert.ok(r[k].name, `隣(+${k})のカードの名前が 20px ドラッグ時点で見える`); assert.notEqual(r[k].img, false, `隣(+${k})のサムネは届いて描画済み(サムネの無いサイトは対象外)`); }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await ctx.close(); }

  // (2) 離したあと
  const cases = [['25% ゆっくり', .25, 25, 20, 300, 0], ['50% 手前(45%)', .45, 25, 20, 300, 0], ['75% ゆっくり', .75, 25, 20, 300, 1], ['速い flick(25%)', .25, 12, 8, 0, 1], ['速い flick(10%)', .12, 8, 6, 0, 1]];
  for (const [name, dist, steps, stepMs, hold, want] of cases) {
    const { ctx, page, cdp } = await fresh();
    await page.evaluate(() => { const sc = document.querySelector('.f-scroll'); window.__tr = []; window.__rel = null; window.__run = true;
      document.addEventListener('touchend', () => { window.__rel = performance.now(); }, { capture: true, passive: true });
      const loop = () => { window.__tr.push([performance.now(), sc.scrollTop]); if (window.__run) requestAnimationFrame(loop); }; loop(); });
    const y0 = 700, y1 = y0 - dist * 844;
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(y0) });
    for (let i = 1; i <= steps; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(Math.round(y0 + (y1 - y0) * i / steps)) }); await page.waitForTimeout(stepMs); }
    if (hold) await page.waitForTimeout(hold);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(1500);
    const r = await page.evaluate(() => { window.__run = false; const sc = document.querySelector('.f-scroll'); return { tr: window.__tr, rel: window.__rel, top: sc.scrollTop, h: sc.clientHeight }; });
    const tr = r.tr.filter(([t]) => t >= r.rel).map(([t, v]) => [t - r.rel, v]), last = tr[tr.length - 1][1];
    let stop = 0; for (let i = tr.length - 1; i >= 0; i--) if (Math.abs(tr[i][1] - last) > 0.5) { stop = tr[Math.min(i + 1, tr.length - 1)][0]; break; }
    let back = 0; { const dir = last - tr[0][1]; let ext = tr[0][1]; for (const [, v] of tr) { if (dir >= 0) { ext = Math.max(ext, v); back = Math.max(back, ext - v); } else { ext = Math.min(ext, v); back = Math.max(back, v - ext); } } }
    console.log(name, JSON.stringify({ idx: Math.round(r.top / r.h), stopMs: Math.round(stop), backtrackPx: Math.round(back) }));
    assert.equal(r.top % r.h, 0, `${name}: カードの境界で止まる`); assert.equal(Math.round(r.top / r.h), want, `${name}: 行き先は ${want} 枚目`);
    assert.ok(stop <= 450, `${name}: 離してから 450ms 以内に止まる(${Math.round(stop)}ms)`); assert.ok(back <= 1, `${name}: 行き過ぎ・戻りがない(${Math.round(back)}px)`);
    await ctx.close();
  }

  // (2b) 動きを減らす設定: 47% で離しても 1 枚だけ進み、慣性で 2 枚目へ飛ばない
  { const { ctx, page, cdp } = await fresh('reduce');
    const y0 = 700, y1 = y0 - 0.47 * 844;
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(y0) });
    for (let i = 1; i <= 12; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(Math.round(y0 + (y1 - y0) * i / 12)) }); await page.waitForTimeout(8); }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await page.waitForTimeout(1500);
    const r = await page.evaluate(() => { const sc = document.querySelector('.f-scroll'); return { top: sc.scrollTop, h: sc.clientHeight }; });
    console.log('reduced 47%', JSON.stringify(r)); assert.equal(r.top % r.h, 0); assert.ok(Math.round(r.top / r.h) <= 1, `reduced-motion でも 1 枚だけ(${r.top})`); await ctx.close(); }

  // (3) 30 枚送ってもノード数が窓の分だけ
  { const { ctx, page } = await fresh();
    const n0 = await page.evaluate(() => document.querySelectorAll('.f-card[data-filled]').length);
    for (let i = 0; i < 30; i++) { await page.keyboard.press('j'); await page.waitForTimeout(120); }
    await page.waitForTimeout(800);
    const r = await page.evaluate(() => ({ filled: document.querySelectorAll('.f-card[data-filled]').length, nodes: document.querySelectorAll('#feed *').length, cur: document.querySelector('.f-card.is-active')?.dataset.index }));
    console.log('30 steps', JSON.stringify({ n0, ...r }));
    assert.ok(r.filled <= 12, `中身のあるカードは窓の分(12 枚以内)だけ(${r.filled})`); await ctx.close(); }
  console.log('e2e-feed-strip: OK');
} finally { await browser.close(); }
