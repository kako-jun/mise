#!/usr/bin/env node
// 範囲ナビ（#scope-nav）と縦フィードの入口と引き継ぎの案内（#feed-entry・#feed-note。#178）の SSR と JS 描き直し後の DOM が同じ構造かを、実ブラウザで突き合わせる (#177)。
// JS 無効（SSR のまま）と JS 有効（app.js が描き直した後）で、要素の並び・class・hidden・href・title・aria-*・アイコン・文字を比べる。
// 区画リンクの枠（.region-row）は JS 有効では描き直しで消える仕様なので比べない。
// 前提: `npx wrangler dev --local --port 8799` が起動し、ローカル D1 にサイトがある（--remote は使わない）。CI には入れない。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/check-scope-nav-parity.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799';
const URLS = ['/', '/jp/', '/jp/13/', '/it/', '/all/', '/jp/13/?category=computing', '/jp/?q=blog', '/jp/?q=blog&tag=' + encodeURIComponent('ブログ'), '/jp/?category=computing&tag=' + encodeURIComponent('ブログ')];
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
// #scope-nav の要素を、比べたい属性だけの文字列の並びにする（.region-row は除く）。
const signature = () => {
  const out = [];
  for (const el of document.querySelectorAll('#scope-nav *, #feed-entry, #feed-note')) {
    if (el.closest('.region-row')) continue;
    const attrs = [...el.attributes].filter((a) => !['style'].includes(a.name)).map((a) => `${a.name}=${a.value}`).sort().join(' ');
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join('');
    out.push(`${el.tagName.toLowerCase()} [${attrs}] ${own}`);
  }
  return out;
};
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
let bad = 0;
for (const u of URLS) {
  const sig = {};
  for (const js of [false, true]) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'en-US', javaScriptEnabled: js });
    const page = await ctx.newPage();
    await page.goto(BASE + u); await page.waitForTimeout(js ? 2000 : 300);
    sig[js] = await page.evaluate(signature); await ctx.close();
  }
  const a = sig[false], b = sig[true], n = Math.max(a.length, b.length), diffs = [];
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) diffs.push(`  #${i}\n    SSR: ${a[i]}\n    JS : ${b[i]}`);
  console.log(`${diffs.length ? 'NG' : 'OK'}  ${u}  (${a.length} 要素)`);
  if (diffs.length) { bad++; console.log(diffs.join('\n')); }
}
await browser.close();
if (bad) { console.error(`${bad} 件で SSR と JS が一致しない`); process.exit(1); }
