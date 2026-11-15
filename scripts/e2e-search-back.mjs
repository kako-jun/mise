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
const WORD = '合成', T = 15000; // 条件待ちの上限（通常はすぐ満たされる。負荷対策で長め）
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
  const ctx = await browser.newContext(); const page = await ctx.newPage(); page.setDefaultTimeout(60000);
  try { await fn(page); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message.split('\n')[0]}`); } finally { await ctx.close(); }
}
let topSearch = '';
const search = (p) => new URL(p.url()).search;
const shown = (p, sel) => p.evaluate((s) => getComputedStyle(document.querySelector(s)).display !== 'none', sel);
// 条件待ち（固定ミリ秒待ちは負荷で落ちるため使わない）
const untilSearch = (p, eq) => p.waitForFunction((e) => location.search === e, eq, { timeout: T });
const untilQ = (p) => p.waitForFunction(() => location.search.includes('q='), null, { timeout: T });
const untilShown = (p, sel, want) => p.waitForFunction(([s, w]) => (getComputedStyle(document.querySelector(s)).display !== 'none') === w, [sel, want], { timeout: T });
const len = (p) => p.evaluate(() => history.length);
async function toCategoryAndSearch(page) {
  await page.goto(BASE + '/'); await page.waitForSelector('#categories a'); topSearch = search(page);
  await page.click('#categories a'); await page.waitForFunction(() => location.search.includes('category='));
  const catSearch = search(page);
  await page.fill('#query', WORD); await page.waitForFunction(() => location.search.includes('q='));
  return catSearch;
}
// hold=true: back() の実行（= popstate）を window.__release() まで保留する（負荷で時間が前後しても競合状態を保てる）
const wrapBack = (page, hold = false) => page.evaluate((h) => { window.__backs = 0; const o = history.back.bind(history); history.back = () => { window.__backs++; if (h) window.__release = o; else o(); }; }, hold);

await scenario('語を全部消す: 履歴は増えず、カテゴリ一覧に戻る。戻る1回で検索前のページ、進むで ?q= が復元', async (page) => {
  const catSearch = await toCategoryAndSearch(page);
  const lenSearch = await len(page);
  await page.fill('#query', ''); await untilSearch(page, catSearch);
  await untilShown(page, '#directory', true);
  assert.equal(search(page), catSearch); assert.equal(await len(page), lenSearch);
  assert.ok(await shown(page, '#directory')); assert.ok(!(await shown(page, '#results')));
  await page.goBack(); await untilSearch(page, topSearch);
  assert.equal(search(page), topSearch, '戻る1回で検索に入る前のページ（トップ）');
  await page.goForward(); await untilSearch(page, catSearch); assert.equal(search(page), catSearch);
  await page.goForward(); await untilQ(page);
  await untilShown(page, '#results', true);
  assert.match(search(page), /q=/); assert.equal(await page.inputValue('#query'), WORD); assert.ok(await shown(page, '#results'));
});
await scenario('検索中に再読込してから語を消す: 一覧が表示され、URL はカテゴリ', async (page) => {
  const catSearch = await toCategoryAndSearch(page);
  await page.reload(); await page.waitForSelector('#result-list li');
  await page.fill('#query', ''); await untilSearch(page, catSearch);
  await untilShown(page, '#directory', true);
  assert.equal(search(page), catSearch); assert.ok(await shown(page, '#directory')); assert.ok(!(await shown(page, '#results')));
  // 再読込後の back() は popstate でなく full page load になる。遷移完了（URL 反映）を待つ
  await page.goBack({ waitUntil: 'load', timeout: T }); await untilSearch(page, topSearch);
  assert.equal(search(page), topSearch);
});
await scenario('popstate が来ない場合: 1 秒後に URL をカテゴリへ戻して一覧を描く', async (page) => {
  const catSearch = await toCategoryAndSearch(page);
  await page.evaluate(() => { history.back = () => {}; });
  const lenSearch = await len(page);
  await page.fill('#query', '');
  // タイマー約 1250ms 後 + View Transition（最大 600ms 待ち + 200ms フェード）で、画面切替は最大約 2050ms 後（上限は負荷対策で余裕を持たせる）
  await untilShown(page, '#directory', true);
  assert.equal(await len(page), lenSearch, '安全弁は replaceState で履歴を積まない');
  assert.equal(search(page), catSearch); assert.ok(await shown(page, '#directory')); assert.ok(!(await shown(page, '#results')));
});
await scenario('back() 待ちの間に空欄 submit しても back() は 1 回だけ（leavingSearch ガード）', async (page) => {
  const catSearch = await toCategoryAndSearch(page); await wrapBack(page, true); // popstate を保留する
  await page.fill('#query', ''); await page.waitForFunction(() => window.__backs === 1, null, { timeout: T }); // debounce が発火し back() 済み・popstate 前（popstate は保留中）
  assert.equal(await page.evaluate(() => window.__backs), 1);
  await page.press('#query', 'Enter'); await page.waitForTimeout(200); // 「何も起きないこと」の確認なので固定待ちで足りる
  assert.equal(await page.evaluate(() => window.__backs), 1, 'submit で back() が 2 回目に積まれない');
  await page.evaluate(() => window.__release()); await untilSearch(page, catSearch);
  await untilShown(page, '#directory', true);
  assert.equal(search(page), catSearch);
  await page.goBack(); await untilSearch(page, topSearch); assert.equal(search(page), topSearch, '2 つ前へ飛んでいない');
});
await scenario('back() 待ちの間に入力した語は復元で消えず、検索し直される', async (page) => {
  await toCategoryAndSearch(page);
  // back() が呼ばれた同じタスク内で語を入力してから popstate を届ける。安全弁（1 秒）が負荷で先に切れて語が消える揺れを避けつつ、
  // 「back() 済み・popstate 前に入力済み」の typed 復元経路を確実に通す
  await page.evaluate((w) => {
    window.__backs = 0; window.__pops = 0; addEventListener('popstate', () => window.__pops++); const o = history.back.bind(history), q = document.querySelector('#query');
    history.back = () => { window.__backs++; q.value = w; q.dispatchEvent(new Event('input', { bubbles: true })); o(); };
  }, WORD);
  await page.fill('#query', '');
  await page.waitForFunction(() => window.__pops === 1, null, { timeout: T }); // popstate 処理後に（URL が一度カテゴリへ戻ってから）検索し直される
  await untilQ(page); await untilShown(page, '#results', true);
  assert.equal(await page.evaluate(() => window.__backs), 1, 'back() が通った（復元経路）');
  assert.equal(await page.inputValue('#query'), WORD); assert.match(search(page), /q=/); assert.ok(await shown(page, '#results'));
});
await browser.close();
if (failed) { console.log(`${failed} 件失敗`); process.exit(1); }
console.log('全シナリオ成功');
