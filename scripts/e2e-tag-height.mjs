#!/usr/bin/env node
// タグの高さの実測検証 (#165)。仕様は docs/design/ui.md の「チップ・タグの大きさ」。
// 検査: (1) .site-tag と .tag-chip の高さが、ラテンのみ／日本語のみ／混在／絵文字入りで小数点以下まで同一（フォントのフォールバックに依存しない）、
// (2) 押せる範囲は見た目と別に確保（チップの × は 28px 以上・タッチ端末 44px 以上、結果行のタグはタッチ端末で 32px 以上。elementFromPoint で確認）。
// 条件: PC1280（fine）/ スマホ390（coarse）× ライト・ダーク × ja・en。
// 前提: `npx wrangler dev --local --port 8799` が起動していて、ローカル D1 の 1 サイトに
//   Web・寺院・Linux入門・ブログ(絵文字)・Rust のタグが付き、検索語「寺」にヒットすること（--remote は使わない）。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-tag-height.mjs [baseUrl=http://localhost:8799]
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
const kindOf = (s) => (/\p{Extended_Pictographic}/u.test(s) ? 'emoji' : /[぀-ヿ㐀-鿿]/.test(s) ? (/[A-Za-z]/.test(s) ? 'mixed' : 'jp') : 'latin');
const url = `${BASE}/jp/?q=${encodeURIComponent('寺')}&` + ['Web', '寺院', 'Linux入門', 'ブログ😀', 'Rust'].map((t) => 'tag=' + encodeURIComponent(t)).join('&');

let checks = 0;
for (const [label, width, coarse] of [['pc1280', 1280, false], ['sp390', 390, true]]) for (const theme of ['light', 'dark']) for (const lang of ['ja', 'en']) {
  const ctx = await browser.newContext({ viewport: { width, height: 800 }, hasTouch: coarse, isMobile: coarse });
  await ctx.addInitScript(([t, l]) => { try { localStorage.setItem('mise-theme', t); localStorage.setItem('mise-language', l); } catch (_) {} }, [theme, lang]);
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForSelector('a.site-tag');
  await page.waitForSelector('.tag-chip');
  const m = await page.evaluate(() => {
    const hit = (el, dy) => { const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2 + dy; const e = document.elementFromPoint(x, y); return !!e && (e === el || el.contains(e)); };
    // ::before による押せる範囲: 中心から上下 half px 離れた点でも自分に当たるか（上端・下端の外側 1px は当たらないことは見ない）
    const reach = (el) => { let up = 0; while (up < 60 && hit(el, -up - 1)) up++; let down = 0; while (down < 60 && hit(el, down + 1)) down++; return up + down + 1; };
    return {
      site: [...document.querySelectorAll('a.site-tag')].map((a) => ({ t: a.textContent, h: a.getBoundingClientRect().height, reach: reach(a) })),
      chip: [...document.querySelectorAll('.tag-chip')].map((c) => ({ t: c.firstChild.textContent, h: c.getBoundingClientRect().height, reach: reach(c.querySelector('button')) })),
    };
  });
  const tag = `${label}/${theme}/${lang}`;
  for (const group of ['site', 'chip']) {
    const kinds = new Set(m[group].map((x) => kindOf(x.t)));
    if (lang === 'ja') for (const k of ['latin', 'jp', 'mixed', 'emoji']) assert.ok(kinds.has(k), `${tag} ${group}: ${k} のタグが無い（前提の seed を確認）`);
    const hs = new Set(m[group].map((x) => x.h.toFixed(4)));
    assert.equal(hs.size, 1, `${tag} ${group} の高さが一定でない: ${m[group].map((x) => `${x.t}=${x.h}`).join(' ')}`);
    checks++;
  }
  const minChip = coarse ? 44 : 28;
  for (const x of m.chip) assert.ok(x.reach >= minChip, `${tag} チップの × の押せる範囲 ${x.reach}px < ${minChip}px (${x.t})`);
  if (coarse) for (const x of m.site) assert.ok(x.reach >= 32, `${tag} 結果行のタグの押せる範囲 ${x.reach}px < 32px (${x.t})`);
  console.log(`ok ${tag}: site-tag ${m.site[0].h}px / tag-chip ${m.chip[0].h}px`);
  await ctx.close();
}
await browser.close();
console.log(`all passed (${checks} height groups)`);
