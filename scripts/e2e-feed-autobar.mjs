#!/usr/bin/env node
// 縦フィードの自動送りバー(#292): 「自動」ボタンが無いこと・旧保存値('off')が無害なこと・どの経路(指・ホイール・キー・ボタン・自動)でも
// カードが止まった(scrollend)瞬間にバーが 0 から伸び始めることを、スマホ幅の実ブラウザ(CDP タッチ)で確認する。
// 前提: wrangler dev --local とローカル D1(サイト数件以上)。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-feed-autobar.mjs [baseUrl=http://localhost:8799]
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
try {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await ctx.addInitScript(() => { try { localStorage.setItem('mise-feed-auto-v1', 'off'); } catch {} }); // 旧版が保存した「オフ」が残っていても自動送りは動く
  const page = await ctx.newPage(); const cdp = await ctx.newCDPSession(page);
await page.goto(BASE + '/all/?category=recent&view=feed');
await page.waitForSelector('.f-card.is-active'); await page.waitForTimeout(800);
await page.evaluate(() => {
  const sc = document.querySelector('.f-scroll'), bar = document.querySelector('.f-progress');
  window.__ev = []; const T = n => window.__ev.push([n, performance.now(), document.querySelector('.f-card.is-active')?.dataset.index]);
  const P = () => (bar.dataset.state||'?')+':'+(parseFloat(bar.firstChild.style.transform.replace(/[^0-9.]/g,''))||0); sc.addEventListener('scrollend', () => { T('scrollend'); const k = window.__ev.length-1; window.__ev[k].push(P()); setTimeout(()=>window.__ev.push(['p+200', performance.now(), '', P()]), 200); });
  let st = bar.dataset.state, started = false;
  const loop = () => { const s = bar.dataset.state, p = parseFloat(bar.firstChild.style.transform.replace(/[^0-9.]/g, '')) || 0;
    const run = s === 'running' && p > 0; if (run && !started) T('barstart'); if (!run && started) T('barstop'); started = run; requestAnimationFrame(loop); };
  loop();
});
const ev = async () => page.evaluate(() => { const e = window.__ev; window.__ev = []; return e; });
// settle = 最後の scrollend。カードが揃った瞬間 = 最後の scrollend (なければ操作終了)
function summarize(name, evs, t0) {
  const se = evs.filter(e => e[0] === 'scrollend').pop(), bs = evs.filter(e => e[0] === 'barstart').pop();
  const settle = se ? se[1] : null;
  console.log(name.padEnd(8), 'scrollend@', se ? Math.round(se[1] - t0) : 'n/a', 'barstart@', bs ? Math.round(bs[1] - t0) : 'none', '=> settled->bar ms', settle && bs ? Math.round(bs[1] - settle) : 'n/a');
  assert.ok(settle && bs && bs[1] - settle < 300, name + ': 止まってから 300ms 以内にバーが伸び始める');
}
const now = () => page.evaluate(() => performance.now());
const tp = y => [{ x: 195, y, id: 1 }];
async function swipe() { await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(700) }); for (let y = 680; y >= 300; y -= 38) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(y) }); await page.waitForTimeout(8); } await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); }
const cases = {
  finger: swipe,
  wheel: async () => { await cdp.send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: 195, y: 400, deltaX: 0, deltaY: 500 }); },
  key: async () => { await page.keyboard.press('j'); },
  button: async () => { await page.tap('#f-next'); },
};
for (const [name, fn] of Object.entries(cases)) {
  await page.waitForTimeout(600); await ev(); const t0 = await now(); await fn(); await page.waitForTimeout(6500);
  summarize(name, await ev(), t0);
}
// auto advance: 待つ
await ev(); const t0 = await now(); await page.waitForTimeout(18000); const e = await ev(); console.log(JSON.stringify(e.map(x => [x[0], Math.round(x[1] - t0), x[2]])));
const adv = e.filter(x => x[0] === 'barstop')[0], after = e.filter(x => x[0] === 'barstart' && adv && x[1] > adv[1])[0], se = e.filter(x => x[0] === 'scrollend' && adv && x[1] >= adv[1] - 50)[0];
assert.equal(await page.evaluate(() => document.querySelector('.f-auto')), null, '「自動」ボタンは存在しない');
assert.equal(await page.evaluate(() => [...document.querySelectorAll('#feed *')].some(e => /自動$|^Auto$/.test(e.textContent.trim()) && getComputedStyle(e).display !== 'none' && e.children.length === 0)), false, '「自動」の文字が表示されていない');
const sEnd = e.filter(x => x[0] === 'scrollend').pop(); assert.ok(sEnd, '旧保存値(off)でも自動送りが進んだ'); assert.ok(parseFloat(sEnd[3].split(':')[1]) < 0.02, '自動送りでも止まった瞬間は 0 から');
console.log('auto: bar state at scrollend / +200ms:', JSON.stringify(e.filter(x=>x[0]==='scrollend'||x[0]==='p+200').map(x=>[x[0],Math.round(x[1]-t0),x[3]])));
} finally { await browser.close(); }
