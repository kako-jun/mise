#!/usr/bin/env node
// 検索を空にして戻るときの履歴挙動 (#102) の実ブラウザ検証。仕様は docs/design/ui.md の「検索」。
// 前提: `npm run db:migrate` と `node scripts/dev-seed-sites.mjs 30` 済みのローカル D1 で `npx wrangler dev --local --port 8799` が起動していること。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> node scripts/e2e-search-back.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799';
const WORD = '合成', WAIT = 600; // 入力デバウンスは 250ms
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
let failed = 0;
async function scenario(name, fn) {
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  try { await fn(page); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message.split('\n')[0]}`); } finally { await ctx.close(); }
}
let topSearch = '';
const search = (p) => new URL(p.url()).search;
const shown = (p, sel) => p.evaluate((s) => getComputedStyle(document.querySelector(s)).display !== 'none', sel);
const len = (p) => p.evaluate(() => history.length);
async function toCategoryAndSearch(page) {
  await page.goto(BASE + '/'); await page.waitForSelector('#categories a'); topSearch = search(page);
  await page.click('#categories a'); await page.waitForFunction(() => location.search.includes('category='));
  const catSearch = search(page);
  await page.fill('#query', WORD); await page.waitForFunction(() => location.search.includes('q='));
  return catSearch;
}
const wrapBack = (page) => page.evaluate(() => { window.__backs = 0; const o = history.back.bind(history); history.back = () => { window.__backs++; o(); }; });

await scenario('語を全部消す: 履歴は増えず、カテゴリ一覧に戻る。戻る1回で検索前のページ、進むで ?q= が復元', async (page) => {
  const catSearch = await toCategoryAndSearch(page);
  const lenSearch = await len(page);
  await page.fill('#query', ''); await page.waitForTimeout(WAIT);
  assert.equal(search(page), catSearch); assert.equal(await len(page), lenSearch);
  assert.ok(await shown(page, '#directory')); assert.ok(!(await shown(page, '#results')));
  await page.goBack(); await page.waitForTimeout(WAIT);
  assert.equal(search(page), topSearch, '戻る1回で検索に入る前のページ（トップ）');
  await page.goForward(); await page.waitForTimeout(WAIT); assert.equal(search(page), catSearch);
  await page.goForward(); await page.waitForTimeout(WAIT);
  assert.match(search(page), /q=/); assert.equal(await page.inputValue('#query'), WORD); assert.ok(await shown(page, '#results'));
});
await scenario('検索中に再読込してから語を消す: 一覧が表示され、URL はカテゴリ', async (page) => {
  const catSearch = await toCategoryAndSearch(page);
  await page.reload(); await page.waitForSelector('#result-list li');
  await page.fill('#query', ''); await page.waitForTimeout(WAIT);
  assert.equal(search(page), catSearch); assert.ok(await shown(page, '#directory')); assert.ok(!(await shown(page, '#results')));
  await page.goBack(); await page.waitForTimeout(WAIT); assert.equal(search(page), topSearch);
});
await scenario('popstate が来ない場合: 1 秒後に URL をカテゴリへ戻して一覧を描く', async (page) => {
  const catSearch = await toCategoryAndSearch(page);
  await page.evaluate(() => { history.back = () => {}; });
  await page.fill('#query', ''); await page.waitForTimeout(1500);
  assert.equal(search(page), catSearch); assert.ok(await shown(page, '#directory')); assert.ok(!(await shown(page, '#results')));
});
await scenario('空欄 submit と debounce が続けて通っても back() は 1 回だけ', async (page) => {
  const catSearch = await toCategoryAndSearch(page); await wrapBack(page);
  await page.fill('#query', ''); await page.press('#query', 'Enter'); await page.waitForTimeout(WAIT);
  assert.equal(await page.evaluate(() => window.__backs), 1);
  assert.equal(search(page), catSearch);
  await page.goBack(); await page.waitForTimeout(WAIT); assert.equal(search(page), topSearch, '2 つ前へ飛んでいない');
});
await scenario('back() 待ちの間に入力した語は復元で消えず、検索し直される', async (page) => {
  await toCategoryAndSearch(page);
  await page.evaluate(() => { const o = history.back.bind(history); history.back = () => setTimeout(o, 150); }); // popstate を遅らせる
  await page.fill('#query', ''); await page.waitForTimeout(280);
  await page.fill('#query', WORD); await page.waitForTimeout(900);
  assert.equal(await page.inputValue('#query'), WORD); assert.match(search(page), /q=/); assert.ok(await shown(page, '#results'));
});
await browser.close();
if (failed) { console.log(`${failed} 件失敗`); process.exit(1); }
console.log('全シナリオ成功');
