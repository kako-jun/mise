#!/usr/bin/env node
// 縦フィードの戻る矢印・スコアに枠が無いこと(#276)と、カードが切り替わった瞬間にスコアが増えること(#277)を、スマホ幅の実ブラウザで確認する。
// 前提: wrangler dev --local とローカル D1(サイト数件以上)。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-feed-score.mjs [baseUrl=http://localhost:8799]
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
  const page = await ctx.newPage();
  await page.goto(BASE + '/all/?category=recent&view=feed');
  await page.waitForSelector('.f-card.is-active'); await page.waitForTimeout(500);
  const styles = await page.evaluate(() => Object.fromEntries([['back', '.f-directory'], ['score', '.f-surf']].map(([k, s]) => {
    const el = document.querySelector('#feed ' + s), cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return [k, { bw: ['Top', 'Right', 'Bottom', 'Left'].map(x => cs['border' + x + 'Width']), shadow: cs.boxShadow, backdrop: cs.backdropFilter, w: r.width, h: r.height }];
  })));
  for (const [k, v] of Object.entries(styles)) {
    assert.ok(v.bw.every(w => w === '0px'), k + ' border'); assert.equal(v.shadow, 'none', k + ' box-shadow'); assert.equal(v.backdrop, 'none', k + ' backdrop');
    assert.ok(v.w >= 36 && v.h >= 36, k + ' tap target');
  }
  // 切り替え→スコア更新(#277): キー送りから 100ms 以内。連続送りは通過点を数えず送り先の 1 件だけ
  const num = () => page.locator('.f-surf-num').textContent();
  const n0 = Number(await num());
  await page.evaluate(() => { window.__t = null; new MutationObserver(() => { window.__t ??= performance.now(); }).observe(document.querySelector('.f-surf-num'), { childList: true, characterData: true, subtree: true }); window.__p = performance.now(); });
  await page.keyboard.press('j'); await page.waitForTimeout(2500);
  const dt = await page.evaluate(() => window.__t - window.__p);
  assert.equal(Number(await num()), n0 + 1); console.log('press -> score ms', Math.round(dt));
  assert.ok(dt < 800, 'スナップ(~300ms)から遅れず数える: ' + dt);
  const n1 = Number(await num());
  for (let i = 0; i < 5; i++) { await page.keyboard.press('j'); await page.waitForTimeout(100); }
  await page.waitForTimeout(2500);
  assert.equal(Number(await num()), n1 + 1, '連続送りは送り先の 1 件だけ');
  console.log('ok', JSON.stringify(styles));
} finally { await browser.close(); }
