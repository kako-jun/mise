#!/usr/bin/env node
// 一覧の行の実測検証 (#166)。仕様は docs/design/ui.md の「ファビコン」。
// 検査: (1) 中点・ファビコンの中心 y がサイト名の 1 行目の中心と ±1px、(2) 説明文・タグ・所属の左端が行の左端（ファビコンの有無で動かない）、
// (3) .site-icon と画像に border/radius/box-shadow/background が無い、(4) 折り返した名前の行が重ならない、(5) 短いホスト名が行内で割れない、
// (6) site-host.js の読み込みに失敗しても一覧が出る（ホスト名だけ出ない）、(7) ?urllayout=below でホスト名が 2 行目の左端に出る。
// 条件: PC1280 / スマホ390 / 幅360 / 最小フォント 24px 相当（390）/ ズーム 200%（幅 640 + zoom 2）× ライト・ダーク × ja・en。
// 前提: `npx wrangler dev --local --port 8799` が起動していて、ローカル D1 に favicon_path を持つサイト（検索語 llll の 4 件。R2 にも画像）と、
//   持たないサイト（検索語 国立）があること（--remote は使わない）。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-site-row.mjs [baseUrl=http://localhost:8799]
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
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });

const measure = () => [...document.querySelectorAll('#result-list li')].map((li) => {
  const a = li.querySelector('.site-title a'), ic = li.querySelector('.site-icon'), bl = li.querySelector('.site-bullet');
  const first = a.getClientRects()[0], cy = (r) => r.top + r.height / 2, rc = (e) => e.getBoundingClientRect();
  const shown = ic && getComputedStyle(ic).display !== 'none';
  const st = (e) => { const c = getComputedStyle(e); return [c.borderTopWidth, c.borderTopLeftRadius, c.boxShadow, c.backgroundColor, c.backgroundImage].join('|'); };
  const rects = [...a.getClientRects()], fs = parseFloat(getComputedStyle(a).fontSize);
  const hosts = [...li.querySelectorAll('.site-host')].filter((e) => getComputedStyle(e).display !== 'none');
  const lx = rc(li).left;
  const desc = li.querySelector('.site-content > p'), meta = li.querySelector('.site-meta'), cat = li.querySelector('.site-categories');
  return {
    name: a.textContent, shown, bulletDy: cy(rc(bl)) - cy(first), iconDy: shown ? cy(rc(ic)) - cy(first) : null,
    leftDiff: [desc, meta, cat].filter(Boolean).map((e) => Math.abs(rc(e).left - lx)),
    iconStyle: shown ? [st(ic), st(ic.querySelector('img'))] : null, iconW: shown ? rc(ic).width : null,
    overlap: rects.some((r, i) => i && r.top < rects[i - 1].top + fs * 0.9),
    hostLines: hosts.map((h) => ({ cls: h.className, lines: new Set([...h.getClientRects()].map((r) => Math.round(r.top))).size, text: h.textContent, left: rc(h).left - lx, top: rc(h).top, headBottom: rc(li.querySelector('.site-head')).bottom })),
  };
});

let rows = 0, maxDy = 0, failed = 0;
const conds = [['pc1280', 1280, 1, null], ['sp390', 390, 1, null], ['sp360', 360, 1, null], ['font24', 390, 1, 24], ['zoom200', 640, 2, null]];
for (const [label, width, zoom, font] of conds) for (const theme of ['light', 'dark']) for (const lang of ['ja', 'en']) for (const q of ['llll', '国立']) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, bypassCSP: true });
  await ctx.addInitScript(([t, l]) => { try { localStorage.setItem('mise-theme', t); localStorage.setItem('mise-language', l); } catch (_) {} }, [theme, lang]);
  const page = await ctx.newPage();
  await page.goto(`${BASE}/jp/?q=${encodeURIComponent(q)}`, { waitUntil: 'networkidle' });
  if (font) await page.addStyleTag({ content: `html:root body { font-size: ${font}px !important; }` });
  if (zoom !== 1) await page.addStyleTag({ content: `html:root body { zoom: ${zoom}; }` });
  await page.waitForTimeout(400);
  const m = await page.evaluate(measure);
  assert.ok(m.length > 0, `${label} ${theme} ${lang} ${q}: 行が出ない`);
  for (const r of m) {
    rows++;
    try {
      assert.ok(Math.abs(r.bulletDy) <= 1, `中点 dy=${r.bulletDy}`); maxDy = Math.max(maxDy, Math.abs(r.bulletDy));
      if (r.shown) { assert.ok(Math.abs(r.iconDy) <= 1, `アイコン dy=${r.iconDy}`); maxDy = Math.max(maxDy, Math.abs(r.iconDy)); assert.ok(Math.abs(r.iconW - 16 * zoom) < 0.5, `アイコン幅 ${r.iconW}`); for (const s of r.iconStyle) assert.equal(s, '0px|0px|none|rgba(0, 0, 0, 0)|none', `装飾あり: ${s}`); }
      for (const d of r.leftDiff) assert.ok(d <= 0.5, `説明文・タグの左端が行の左端とずれる ${d}`);
      assert.ok(!r.overlap, '折り返した名前の行が重なる');
      for (const h of r.hostLines) { assert.ok(h.text.length > 0); if (h.text.length <= 12 && label !== 'zoom200' && label !== 'font24') assert.equal(h.lines, 1, `短いホスト名が割れる: ${h.text}`); }
    } catch (e) { failed++; console.error(`NG ${label} ${theme} ${lang} ${q} ${r.name}: ${e.message}`); }
  }
  await ctx.close();
}

// (6) site-host.js の読み込み失敗でも一覧は出る（ホスト名だけ出ない）
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 900 } });
  const page = await ctx.newPage(); await page.route('**/site-host.js*', (r) => r.abort());
  await page.goto(`${BASE}/jp/?q=llll`, { waitUntil: 'networkidle' });
  const n = await page.evaluate(() => [document.querySelectorAll('#result-list li').length, document.querySelectorAll('.site-host').length]);
  try { assert.ok(n[0] > 0, '一覧が出ない'); assert.equal(n[1], 0, 'ホスト名が出ている'); } catch (e) { failed++; console.error('NG site-host.js abort:', e.message); }
  console.log(`site-host.js abort: rows=${n[0]} hosts=${n[1]}`);
  await ctx.close();
}
// (7) ?urllayout=below: ホスト名が 2 行目の左端
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 900 } });
  const page = await ctx.newPage(); await page.goto(`${BASE}/jp/?q=llll&urllayout=below`, { waitUntil: 'networkidle' });
  const m = await page.evaluate(measure);
  try { for (const r of m) { assert.equal(r.hostLines.length, 1); assert.ok(r.hostLines[0].cls.includes('site-host-below')); assert.ok(r.hostLines[0].left <= 0.5, '2 行目の左端でない'); assert.ok(r.hostLines[0].top >= r.hostLines[0].headBottom - 1); assert.ok(Math.abs(r.bulletDy) <= 1); } } catch (e) { failed++; console.error('NG urllayout=below:', e.message); }
  await ctx.close();
}
await browser.close();
console.log(`rows=${rows} maxCenterDiff=${maxDy.toFixed(2)}px failed=${failed}`);
process.exit(failed ? 1 : 0);
