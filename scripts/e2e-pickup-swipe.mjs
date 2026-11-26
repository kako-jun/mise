#!/usr/bin/env node
// ピックアップの横スワイプ/ドラッグ/左右キー (#113) の実ブラウザ検証。仕様は docs/design/ui.md の「ピックアップ」。
// 前提: `npm run db:migrate` と `node scripts/dev-seed-sites.mjs 30` 済みのローカル D1 で `npx wrangler dev --local --port 8799` が起動していること（--remote は使わない）。
// ピックアップはランダムなので /api/picks をページ内で固定の 3 件に差し替える。自動入れ替えの 8 秒間隔は、登録だけ記録して発火させない（手動操作のタイマー再始動の確認用）。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> node scripts/e2e-pickup-swipe.mjs [baseUrl=http://localhost:8799]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799', T = 15000; // 条件待ちの上限（通常はすぐ満たされる。負荷対策で長め）
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const PICKS = ['One', 'Two', 'Three'].map((n, i) => ({
  id: `e2e-pick-${i}`, name: `Pick ${n}`, url: `https://pick-${i}.invalid/`, description_ja: `ピック ${n}`, description_en: `Pick ${n}`, country_code: 'JP',
  tags_json: JSON.stringify([{ ja: `タグ${i}`, en: `tag${i}` }]),
}));
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
let failed = 0;
async function scenario(name, opts, fn) {
  const ctx = await browser.newContext(opts); const page = await ctx.newPage(); page.setDefaultTimeout(60000);
  await page.route('**/api/picks*', (r) => r.fulfill({ json: PICKS }));
  await page.addInitScript(() => {
    window.__iv = { set: 0, cleared: 0 }; const si = window.setInterval.bind(window), ci = window.clearInterval.bind(window);
    window.setInterval = (fn, ms, ...a) => { if (ms === 8000) { window.__iv.set++; return -1; } return si(fn, ms, ...a); };
    window.clearInterval = (id) => { if (id === -1) { window.__iv.cleared++; return; } return ci(id); };
    addEventListener('click', (e) => { if (e.target.closest?.('#pick-list a:not(.site-tag)')) e.preventDefault(); }, true); // リンクの遷移だけ潰す（タグの click は触らない）
  });
  const navs = []; page.on('popup', (p) => navs.push(p.url()));
  try { await page.goto(BASE + '/'); await page.waitForSelector('#pick-list li a'); await reveal(page); await fn(page, navs); console.log(`ok   ${name}`); }
  catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message.split('\n')[0]}`); } finally { await ctx.close(); }
}
// ピックアップを画面の中ほどへ（スマホではページ下部にあり、そのままでは画面外）
const reveal = (p) => p.evaluate(() => document.querySelector('#picks').scrollIntoView({ block: 'center' }));
const current = (p) => p.evaluate(() => document.querySelector('#pick-list li a')?.textContent);
const untilPick = (p, name) => p.waitForFunction((n) => document.querySelector('#pick-list li a')?.textContent === n, name, { timeout: T });
const chips = (p) => p.evaluate(() => document.querySelectorAll('#tag-chips .tag-chip').length);
// 入場アニメーションの終了待ち（pick-enter 500ms 内の押下はタグを付けない既存規則のため）
const settled = (p) => p.waitForFunction(() => document.querySelector('#pick-list li') && document.querySelector('#pick-list li').getAnimations().every((a) => a.playState === 'finished'), null, { timeout: T });
const box = (p, sel) => p.evaluate((s) => { const r = document.querySelector(s).getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; }, sel);
// CDP のタッチ（from→to を steps 分割。まず touchStart、最後に touchEnd）
async function touchDrag(page, cdp, [x0, y0], [x1, y1], steps = 8) {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y: y0 }] });
  for (let i = 1; i <= steps; i++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x0 + ((x1 - x0) * i) / steps, y: y0 + ((y1 - y0) * i) / steps }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}
const mobile = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 };
const desktop = { viewport: { width: 1280, height: 800 } };
// 行の中央付近（リンクとタグを避けた余白でなく、実際に指が乗る説明文の位置）を起点にする
const startPoint = async (page) => { const b = await box(page, '#pick-list li'); return [b.x + b.w / 2, b.y + b.h / 2]; };

await scenario('スマホ390: 左スワイプで次、右で前、履歴が尽きたら前は無反応。自動タイマーを再始動', mobile, async (page) => {
  const cdp = await page.context().newCDPSession(page);
  assert.equal(await current(page), 'Pick One');
  const [x, y] = await startPoint(page); const setBefore = await page.evaluate(() => window.__iv.set);
  await touchDrag(page, cdp, [x + 60, y], [x - 60, y]); await untilPick(page, 'Pick Two');
  assert.ok((await page.evaluate(() => window.__iv.set)) > setBefore, '手動で送るとタイマーを張り直す');
  assert.ok((await page.evaluate(() => window.__iv.cleared)) > 0);
  assert.match(await page.textContent('#picks [role=status]'), /2 \/ 3: Pick Two/);
  await touchDrag(page, cdp, [x + 60, y], [x - 60, y]); await untilPick(page, 'Pick Three');
  await touchDrag(page, cdp, [x - 60, y], [x + 60, y]); await untilPick(page, 'Pick Two');
  await touchDrag(page, cdp, [x - 60, y], [x + 60, y]); await untilPick(page, 'Pick One');
  await touchDrag(page, cdp, [x - 60, y], [x + 60, y]); // 履歴が尽きた
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One');
  await touchDrag(page, cdp, [x + 60, y], [x - 60, y]); await untilPick(page, 'Pick Two'); // 進む側は引き続き動く
});
await scenario('スマホ390: 閾値未満の横移動・縦優勢の斜め移動では切り替わらない（縦スクロールを奪わない）', mobile, async (page) => {
  const cdp = await page.context().newCDPSession(page); const [x, y] = await startPoint(page);
  await touchDrag(page, cdp, [x + 15, y], [x - 15, y]); // 30px < 40px
  await touchDrag(page, cdp, [x + 30, y - 80], [x - 30, y + 80]); // 水平 60 / 垂直 160
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One');
});
await scenario('スマホ390: ピックアップ上の縦ドラッグはページをスクロールし、切り替えない', mobile, async (page) => {
  const cdp = await page.context().newCDPSession(page);
  await page.evaluate(() => { document.body.style.paddingBottom = '2000px'; }); // スクロールできる高さを確保（CSP のため CSSOM で）
  await reveal(page); const [x, y] = await startPoint(page); const y0 = await page.evaluate(() => scrollY);
  await touchDrag(page, cdp, [x, y + 100], [x + 6, y - 60], 10);
  await page.waitForFunction((v) => scrollY > v + 20, y0, { timeout: T });
  assert.equal(await current(page), 'Pick One');
});
await scenario('スマホ390: タグのタップは従来どおり効く。スワイプ（タグ上から開始）の直後にタグは付かない', mobile, async (page) => {
  const cdp = await page.context().newCDPSession(page);
  await settled(page); const tb = await box(page, '#pick-list .site-tag');
  const tx = tb.x + tb.w / 2, ty = tb.y + tb.h / 2;
  await touchDrag(page, cdp, [tx + 50, ty], [tx - 50, ty]); // タグの上から左へスワイプ
  await untilPick(page, 'Pick Two'); await settled(page);
  assert.equal(await chips(page), 0, 'スワイプでタグは付かない');
  assert.equal(new URL(page.url()).search.includes('tag='), false);
  const t2 = await box(page, '#pick-list .site-tag');
  await page.touchscreen.tap(t2.x + t2.w / 2, t2.y + t2.h / 2); // 通常のタップ
  await page.waitForFunction(() => document.querySelectorAll('#tag-chips .tag-chip').length === 1, null, { timeout: T });
  assert.match(await page.textContent('#tag-chips'), /#(タグ1|tag1)/); // チップの言語はブラウザの言語に従う
});
await scenario('スマホ390: 切替直後 500ms 以内のタグのタップは従来どおり無視（既存ガード）', mobile, async (page) => {
  const cdp = await page.context().newCDPSession(page); const [x, y] = await startPoint(page);
  await settled(page);
  await touchDrag(page, cdp, [x + 60, y], [x - 60, y]); await untilPick(page, 'Pick Two');
  const tb = await box(page, '#pick-list .site-tag');
  const young = await page.evaluate(() => document.querySelector('#pick-list li').getAnimations().some((a) => a.playState !== 'finished'));
  if (young) { await page.touchscreen.tap(tb.x + tb.w / 2, tb.y + tb.h / 2); await page.evaluate(() => new Promise((r) => setTimeout(r, 100))); assert.equal(await chips(page), 0); }
});
await scenario('PC1280: マウスの横ドラッグで次/前。ドラッグ後にリンク・タグの click が出ない', desktop, async (page, navs) => {
  await settled(page); const b = await box(page, '#pick-list li a'); const x = b.x + 4, y = b.y + b.h / 2; // リンクの上から開始
  await page.mouse.move(x + 80, y); await page.mouse.down(); await page.mouse.move(x - 80, y, { steps: 10 }); await page.mouse.up();
  await untilPick(page, 'Pick Two'); await page.evaluate(() => new Promise((r) => setTimeout(r, 50)));
  assert.equal(navs.length, 0); assert.equal(await chips(page), 0);
  await settled(page); const t = await box(page, '#pick-list .site-tag'); // タグの上から右へ戻す
  await page.mouse.move(t.x + 2, t.y + t.h / 2); await page.mouse.down(); await page.mouse.move(t.x + 120, t.y + t.h / 2, { steps: 10 }); await page.mouse.up();
  await untilPick(page, 'Pick One'); assert.equal(await chips(page), 0, 'ドラッグ後にタグが付かない');
  await settled(page); const t1 = await box(page, '#pick-list .site-tag'); // 通常クリックは従来どおり
  await page.mouse.click(t1.x + t1.w / 2, t1.y + t1.h / 2);
  await page.waitForFunction(() => document.querySelectorAll('#tag-chips .tag-chip').length === 1, null, { timeout: T });
});
await scenario('PC1280: ピックアップ内にフォーカス時の左右キー。外では効かない。支援技術へ通知', desktop, async (page) => {
  await page.focus('#pick-list li a');
  await page.keyboard.press('ArrowRight'); await untilPick(page, 'Pick Two');
  assert.equal(await page.evaluate(() => document.activeElement?.closest('#pick-list') !== null), true, 'フォーカスは新しい行に残る');
  assert.match(await page.textContent('#picks [role=status]'), /2 \/ 3: Pick Two/);
  await page.keyboard.press('ArrowLeft'); await untilPick(page, 'Pick One');
  await page.keyboard.press('ArrowLeft'); await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One', '履歴が尽きたら無反応');
  await page.evaluate(() => document.activeElement.blur()); await page.focus('#query'); await page.keyboard.press('ArrowRight');
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One', '検索欄の矢印キーでは動かない');
});
await scenario('PC1280: 枠外で離したドラッグの状態が残らない（後のボタンなしホバーで切り替わらない）', desktop, async (page) => {
  await settled(page); const b = await box(page, '#pick-list li'); const x = b.x + b.w / 2, y = b.y + b.h / 2;
  await page.mouse.move(x, y); await page.mouse.down(); await page.mouse.move(x + 5, y - 10, { steps: 3 }); // 縦気味で判定は成立しない
  await page.mouse.move(5, 5, { steps: 5 }); await page.mouse.up(); // 枠外で離す
  await page.mouse.move(x + 60, y, { steps: 4 }); await page.mouse.move(x - 60, y, { steps: 10 }); // ボタンなしで 120px 横に動かす
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One', 'ホバーだけでは切り替わらない');
});
await scenario('PC1280: pointerup が欠落した状態でも、ボタンなしのホバーでは切り替わらない（buttons===0 の保険）', desktop, async (page) => {
  await settled(page); const b = await box(page, '#pick-list li'); const x = b.x + b.w / 2, y = b.y + b.h / 2;
  await page.mouse.move(x, y);
  await page.evaluate(() => { // pointerup を window に発生させない: pointerdown だけ合成する
    const a = document.querySelector('#pick-list li a');
    a.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0, buttons: 1, clientX: 0, clientY: 0 }));
  });
  await page.mouse.move(x + 60, y, { steps: 4 }); await page.mouse.move(x - 60, y, { steps: 10 }); // ボタンなし（buttons===0）で 120px 横に動かす
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One', '保険で状態を捨て、切り替わらない');
});
await scenario('PC1280: 実マウス押下から動かしてリンクをドラッグすると dragstart が止められる', desktop, async (page) => {
  await settled(page);
  await page.evaluate(() => { window.__ds = []; document.addEventListener('dragstart', (e) => window.__ds.push(e.defaultPrevented), false); });
  const b = await box(page, '#pick-list li a'); const x = b.x + b.w / 2, y = b.y + b.h / 2;
  await page.mouse.move(x, y); await page.mouse.down(); await page.mouse.move(x + 8, y + 2, { steps: 4 }); await page.mouse.move(x + 20, y + 4, { steps: 4 });
  const r = await page.evaluate(() => window.__ds); await page.mouse.up();
  assert.ok(r.length > 0, 'ネイティブの dragstart が発火している');
  assert.ok(r.every((v) => v === true), 'dragstart は defaultPrevented になる');
});
await scenario('PC1280: 押下なしの dragstart は止めない', desktop, async (page) => {
  await settled(page);
  assert.equal(await page.evaluate(() => { const e = new Event('dragstart', { bubbles: true, cancelable: true }); document.querySelector('#pick-list li a').dispatchEvent(e); return e.defaultPrevented; }), false, '押下中でなければ dragstart を止めない');
});
await scenario('スマホ390: 2 本指（2 本目の指で判定を破棄）では切り替わらない', mobile, async (page) => {
  const cdp = await page.context().newCDPSession(page); const [x, y] = await startPoint(page);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1 }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1 }, { x: x + 30, y, id: 2 }] });
  for (let i = 1; i <= 8; i++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x - i * 10, y, id: 1 }, { x: x + 30 - i * 10, y, id: 2 }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.equal(await current(page), 'Pick One');
  assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('#picks')).touchAction), 'pan-y pinch-zoom');
});
await scenario('言語切替で role=status の古い文言が残らない', desktop, async (page) => {
  await page.focus('#pick-list li a'); await page.keyboard.press('ArrowRight'); await untilPick(page, 'Pick Two');
  assert.match(await page.textContent('#picks [role=status]'), /2 \/ 3/);
  await page.click('#language-toggle');
  assert.equal((await page.textContent('#picks [role=status]')).trim(), '');
});
await scenario('reduced-motion: アニメなしで即切替（タッチ・キー）', { ...mobile, reducedMotion: 'reduce' }, async (page) => {
  const cdp = await page.context().newCDPSession(page); const [x, y] = await startPoint(page);
  assert.equal(await page.evaluate(() => document.querySelector('#pick-list li').getAnimations().length), 0);
  await touchDrag(page, cdp, [x + 60, y], [x - 60, y]); await untilPick(page, 'Pick Two');
  assert.equal(await page.evaluate(() => document.querySelector('#pick-list li').getAnimations().length), 0);
  await page.focus('#pick-list li a'); await page.keyboard.press('ArrowLeft'); await untilPick(page, 'Pick One');
  assert.equal(await page.evaluate(() => document.querySelector('#pick-list li').getAnimations().length), 0);
});
await browser.close();
console.log(failed ? `${failed} failed` : 'all passed'); process.exit(failed ? 1 : 0);
