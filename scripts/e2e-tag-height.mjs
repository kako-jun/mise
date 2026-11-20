#!/usr/bin/env node
// タグの高さの実測検証 (#165)。仕様は docs/design/ui.md の「チップ・タグの大きさ」。
// 検査: (1) .site-tag と .tag-chip の高さが、ラテンのみ／日本語のみ／混在／絵文字入りで小数点以下まで同一（フォントのフォールバックに依存しない）、
// (2) 押せる範囲は見た目と別に確保（チップの × は 28px 以上・タッチ端末 44px 以上、結果行のタグはタッチ端末で 32px 以上。elementFromPoint で確認）。
// 条件: PC1280（fine）/ スマホ390・360（coarse）/ 最小フォント 24px 相当（390）/ ズーム 200%（幅 640 + zoom 2）× ライト・ダーク × ja・en。
// 追加の検査: 見た目の範囲の格子点が本来の要素に当たる（隣・検索欄・おすすめリンクに奪われない）、文字が枠（border-box）に収まる、高さが em 基準（.site-tag 1.8em / .tag-chip 30em/13。#183 で上げた）。
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

let checks = 0, ng = 0;
const conds = [['pc1280', 1280, false, 1, null], ['sp390', 390, true, 1, null], ['sp360', 360, true, 1, null], ['font24', 390, true, 1, 24], ['zoom200', 640, false, 2, null]];
for (const [label, width, coarse, zoom, font] of conds) for (const theme of ['light', 'dark']) for (const lang of ['ja', 'en']) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, hasTouch: coarse, isMobile: coarse, bypassCSP: true });
  await ctx.addInitScript(([t, l]) => { try { localStorage.setItem('mise-theme', t); localStorage.setItem('mise-language', l); } catch (_) {} }, [theme, lang]);
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForSelector('a.site-tag');
  await page.waitForSelector('.tag-chip');
  // 最小フォント相当: ブラウザの最小文字サイズ設定・Android の文字サイズ拡大では px 指定の文字も大きくなるので、タグ・チップの font-size を直接 24px にする
  if (font) await page.addStyleTag({ content: `html:root body, html:root .site-meta, html:root .tag-chip { font-size: ${font}px !important; }` });
  if (zoom !== 1) await page.addStyleTag({ content: `html:root body { zoom: ${zoom}; }` });
  await page.waitForTimeout(400);
  const m = await page.evaluate(() => {
    const hitSelf = (el, x, y) => { const e = document.elementFromPoint(x, y); return !!e && (e === el || el.contains(e)); };
    const input = document.querySelector('#query'), link = document.querySelector('#recommend-link');
    const ir = input.getBoundingClientRect(), lr = link.getBoundingClientRect();
    const inputEdge = hitSelf(input, ir.left + ir.width / 2, ir.bottom - 2), linkEdge = hitSelf(link, lr.left + lr.width / 2, lr.top + 2);
    // 押せる範囲: 中心から上下に何 px まで自分に当たるか
    const reach = (el) => { el.scrollIntoView({ block: 'center', behavior: 'instant' }); const r = el.getBoundingClientRect(), x = r.left + r.width / 2, cy = r.top + r.height / 2; let up = 0, down = 0; while (up < 80 && hitSelf(el, x, cy - up - 1)) up++; while (down < 80 && hitSelf(el, x, cy + down + 1)) down++; return up + down + 1; };
    // 見た目（border-box）の中を格子状に探る: どの点も本来の要素に当たる（隣の要素の押せる範囲に奪われない）
    const grid = (el) => { el.scrollIntoView({ block: 'center', behavior: 'instant' }); const r = el.getBoundingClientRect(), bad = []; for (const fx of [0.2, 0.5, 0.8]) for (const fy of [0.2, 0.5, 0.8]) { const x = r.left + r.width * fx, y = r.top + r.height * fy; if (!hitSelf(el, x, y)) bad.push(fx + ',' + fy); } return bad; };
    // 文字の Range が枠（border-box）の中に収まる
    const textIn = (el, node) => { const rg = document.createRange(); rg.selectNodeContents(node); const t = rg.getBoundingClientRect(), b = el.getBoundingClientRect(); return t.top >= b.top - 0.01 && t.bottom <= b.bottom + 0.01 && t.left >= b.left - 0.01 && t.right <= b.right + 0.01; };
    return {
      site: [...document.querySelectorAll('a.site-tag')].map((a) => ({ t: a.textContent, h: a.getBoundingClientRect().height, fs: parseFloat(getComputedStyle(a).fontSize), reach: reach(a), bad: grid(a), inside: textIn(a, a) })),
      chip: [...document.querySelectorAll('.tag-chip')].map((c) => ({ t: c.firstChild.textContent, h: c.getBoundingClientRect().height, fs: parseFloat(getComputedStyle(c).fontSize), reach: reach(c.querySelector('button')), bad: grid(c), inside: textIn(c, c.firstChild) })),
      // 検索欄の下端 2px 内側・おすすめリンクの上端 2px 内側（見た目の範囲）をタップして本来の要素に当たるか
      inputEdge,
      linkEdge,
    };
  });
  const tag = `${label}/${theme}/${lang}`;
  try {
    for (const group of ['site', 'chip']) {
      if (lang === 'ja') for (const k of ['latin', 'jp', 'mixed', 'emoji']) assert.ok(m[group].some((x) => kindOf(x.t) === k), `${group}: ${k} のタグが無い（前提の seed を確認）`);
      assert.equal(new Set(m[group].map((x) => x.h.toFixed(4))).size, 1, `${group} の高さが一定でない: ${m[group].map((x) => `${x.t}=${x.h}`).join(' ')}`);
      for (const x of m[group]) { assert.deepEqual(x.bad, [], `${group} ${x.t}: 見た目の範囲が別の要素に奪われる ${x.bad}`); assert.ok(x.inside, `${group} ${x.t}: 文字が枠からはみ出す`); }
      checks++;
    }
        if (!font && zoom === 1) { assert.equal(m.site[0].h, m.site[0].fs * 1.8, '.site-tag は 1.8em'); assert.ok(Math.abs(m.chip[0].h - 30) < 0.01, '.tag-chip は 13px 基準で 30px'); }
    if (font) { assert.ok(Math.abs(m.site[0].h - m.site[0].fs * 1.8) < 0.02, '.site-tag が文字に追従しない'); /* 1.8em は 2 進で割り切れず、レイアウトは 1/64px 単位に丸める（24px で 43.1875） */ assert.ok(Math.abs(m.chip[0].h - m.chip[0].fs * 30 / 13) < 0.01, '.tag-chip が文字に追従しない'); }
    const minChip = coarse ? 44 : 28;
    if (!font) for (const x of m.chip) assert.ok(x.reach >= minChip * zoom - 0.5, `チップの × の押せる範囲 ${x.reach}px < ${minChip * zoom}px (${x.t})`);
    if (coarse && !font) for (const x of m.site) assert.ok(x.reach >= 32, `結果行のタグの押せる範囲 ${x.reach}px < 32px (${x.t})`);
    assert.ok(m.inputEdge, '検索欄の下端が × の押せる範囲に奪われる');
    assert.ok(m.linkEdge, 'おすすめリンクの上端が × の押せる範囲に奪われる');
    console.log(`ok ${tag}: site-tag ${m.site[0].h}px / tag-chip ${m.chip[0].h}px (fs ${m.site[0].fs}/${m.chip[0].fs})`);
  } catch (e) { ng++; console.error(`NG ${tag}: ${e.message}`); }
  await ctx.close();
}
await browser.close();
console.log(`${conds.length * 4} 条件, ${checks} height groups, NG ${ng}`);
if (ng) process.exit(1);
