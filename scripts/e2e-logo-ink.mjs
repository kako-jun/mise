#!/usr/bin/env node
// トップのロゴの実測検証 (#152)。仕様は docs/design/ui.md の「ロゴ」。
// 各文字を 1 つずつ描き出して（他は不可視）、実際に塗られたピクセルから (1) 隣り合う文字の行ごとのインク間隔の平均、(2) 縦線の太さを測る。
// 期待: 平均インク間隔 17px(±1)、S の太さは M の ±1px、インク全体が行の中央(左右の余白差 ±1px)。ライト/ダーク × PC1280/スマホ390。
// 前提: `npx wrangler dev --local --port 8799` が起動していること（--remote は使わない）。
// LOGO_FALLBACK=1: ロゴの JS を無効にして style.css のフォールバックだけを測る（文字間 ±3px。太さ・中央寄せは検査せず表示のみ）。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-logo-ink.mjs [baseUrl=http://localhost:8799]
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
const ISOLATE = (k) => `
body *{visibility:hidden!important}
html,body{background:#fff!important}
.logo,.logo-row{visibility:visible!important}
html:root:root:root .logo span{visibility:hidden!important;background:none!important;background-image:none!important;opacity:1!important;-webkit-background-clip:border-box!important;background-clip:border-box!important;color:#000!important;text-shadow:none!important;transition:none!important}
html:root:root:root .logo span:nth-child(${k + 1}){visibility:visible!important}`;
const FB = !!process.env.LOGO_FALLBACK, TOL = FB ? 3 : 1;
const S = 4; // deviceScaleFactor
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
let failed = 0;
for (const width of [1280, 390]) for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({ viewport: { width, height: 800 }, deviceScaleFactor: S, bypassCSP: true }); // 測定用の <style> を差し込むため（本番の CSP は style-src 'self'）
  await ctx.addInitScript((t) => { try { localStorage.setItem('mise-theme', t); } catch (_) {} }, theme);
  const page = await ctx.newPage();
  try {
    if (FB) await page.route('**/jp/', async (r) => { const res = await r.fetch(); await r.fulfill({ response: res, body: (await res.text()).replace('window.__miseLogo = () =>', 'window.__noLogo = () =>') }); });
    await page.goto(BASE + '/jp/', { waitUntil: 'load' });
    if (FB) await page.waitForTimeout(1300); else await page.waitForFunction(() => document.documentElement.classList.contains('logo-ready'), null, { timeout: 8000 });
    await page.waitForTimeout(400);
    const lg = await page.evaluate(() => { const r = document.querySelector('.logo').getBoundingClientRect(), o = document.querySelector('.logo-row').getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, rowC: o.x + o.width / 2 }; });
    const clip = { x: Math.max(0, lg.x - 30), y: Math.max(0, lg.y - 10), width: Math.min(width, lg.w + 60), height: lg.h + 20 };
    const inks = [];
    for (let k = 0; k < 4; k++) {
      await page.evaluate((c) => { document.getElementById('iso')?.remove(); const st = document.createElement('style'); st.id = 'iso'; st.textContent = c; document.head.append(st); }, ISOLATE(k));
      const buf = await page.screenshot({ clip, type: 'png' });
      inks.push(await page.evaluate(async ({ b64, cx, S }) => {
        const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
        const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
        const g = c.getContext('2d'); g.drawImage(img, 0, 0);
        const d = g.getImageData(0, 0, c.width, c.height).data, W = c.width, H = c.height;
        const ink = (x, y) => d[(y * W + x) * 4] < 128;
        let y0 = 1e9, y1 = -1; const L = [], R = [];
        for (let y = 0; y < H; y++) { let a = -1, b = -1; for (let x = 0; x < W; x++) if (ink(x, y)) { if (a < 0) a = x; b = x + 1; } L.push(a < 0 ? null : cx + a / S); R.push(a < 0 ? null : cx + b / S); if (a >= 0) { y0 = Math.min(y0, y); y1 = y; } }
        const runs = (y) => { const rs = []; let s = -1; for (let x = 0; x <= W; x++) { const on = x < W && ink(x, y); if (on && s < 0) s = x; if (!on && s >= 0) { rs.push((x - s) / S); s = -1; } } return rs; };
        const at = (f) => runs(y0 + Math.round((y1 - y0) * f));
        return { L, R, at: [0.25, 0.35, 0.5, 0.65, 0.75, 0.85].map(at) };
      }, { b64: buf.toString('base64'), cx: clip.x, S }));
    }
    const gap = (k) => { let s = 0, n = 0; for (let i = 0; i < inks[k].R.length; i++) { const r = inks[k].R[i], l = inks[k + 1].L[i]; if (r === null || l === null) continue; s += l - r; n++; } return s / n; };
    const avg = (a) => a.reduce((x, y) => x + y, 0) / a.length;
    // 縦線の太さ: M は左右の脚(下寄り)、I は中央の柱、S は上下の弧の横断幅(= 縦に近い部分)、E は縦棒
    const stem = [avg([3, 4, 5].flatMap((i) => { const r = inks[0].at[i]; return [r[0], r[r.length - 1]]; })), avg([0, 2, 4].map((i) => inks[1].at[i][0])), avg([0, 4].flatMap((i) => inks[2].at[i])), avg([0, 1, 3, 4].map((i) => inks[3].at[i][0]))];
    const gaps = [0, 1, 2].map(gap);
    const first = inks[0].L.filter((v) => v !== null), last = inks[3].R.filter((v) => v !== null);
    const inkL = Math.min(...first), inkR = Math.max(...last), off = (inkL + inkR) / 2 - lg.rowC; // 0 なら中央
    const tag = `${width}px${FB ? ' fallback' : ''} ${theme}`;
    console.log(`${tag}: 平均インク間隔 ${gaps.map((v) => v.toFixed(1)).join(' / ')}  太さ M/I/S/E ${stem.map((v) => v.toFixed(1)).join(' / ')}  インク全幅 ${(inkR - inkL).toFixed(0)}  中央からのずれ ${off.toFixed(2)}`);
    gaps.forEach((v, k) => assert.ok(Math.abs(v - 17) <= TOL, `${tag}: 文字間 ${k} が ${v.toFixed(2)}px`));
    if (!FB) assert.ok(Math.abs(off) <= 1, `${tag}: インクが中央から ${off.toFixed(2)}px ずれている`);
    if (!FB) assert.ok(Math.abs(stem[2] - stem[0]) <= 1, `${tag}: S の太さ ${stem[2].toFixed(2)} が M ${stem[0].toFixed(2)} の ±1px を超える`);
  } catch (e) { failed++; console.log(`FAIL ${width} ${theme}: ${e.message.split('\n')[0]}`); } finally { await ctx.close(); }
}
await browser.close();
process.exit(failed ? 1 : 0);
