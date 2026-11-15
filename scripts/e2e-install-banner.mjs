#!/usr/bin/env node
// インストール提案バナー (#105) の実ブラウザ検証。仕様は docs/design/ui.md の「アプリとして追加（PWA）」。
// 前提: `npm run db:migrate` 済みのローカル D1 で `npx wrangler dev --local --port 8805` が起動していること。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> SHOT_DIR=<作業ツリー外の出力先> node scripts/e2e-install-banner.mjs [baseUrl=http://localhost:8805]
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync, mkdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8805';
const SHOT = process.env.SHOT_DIR; if (SHOT) mkdirSync(SHOT, { recursive: true });
const T = 15000, KEY = 'mise-install-dismissed-at', DAY = 24 * 60 * 60 * 1000; // 条件待ちの上限（通常はすぐ満たされる）
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
async function scenario(name, fn, viewport = { width: 1000, height: 700 }, locale = 'en-US') {
  const ctx = await browser.newContext({ viewport, locale }); const page = await ctx.newPage();
  try { await fn(page, ctx); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n     ${e.message.split('\n').slice(0, 6).join(' | ')}`); } finally { await ctx.close(); }
}
const shown = (p) => p.evaluate(() => getComputedStyle(document.querySelector('#install-banner')).display !== 'none');
const untilShown = (p, want) => p.waitForFunction((w) => (getComputedStyle(document.querySelector('#install-banner')).display !== 'none') === w, want, { timeout: T });
const ready = async (p) => { await p.goto(BASE + '/'); await p.waitForFunction(() => window.__miseBooted === true && !!window.MiseInstall, null, { timeout: T }); };
// 本物の beforeinstallprompt の代わりに、prompt()/userChoice を持つイベントを発火する。defaultPrevented でブラウザ標準提案の抑止も見る。
const fire = (p, outcome = 'accepted') => p.evaluate((o) => {
  const e = new Event('beforeinstallprompt', { cancelable: true });
  window.__prompts = 0; e.prompt = async () => { window.__prompts++; }; e.userChoice = Promise.resolve({ outcome: o, platform: 'web' });
  window.dispatchEvent(e); return e.defaultPrevented;
}, outcome);
const bootRegression = async (page, url) => { await page.goto(BASE + url); await page.waitForFunction(() => window.__miseBooted === true, null, { timeout: T }); };
const stored = (p) => p.evaluate((k) => localStorage.getItem(k), KEY);
const shot = (p, n) => SHOT && p.screenshot({ path: path.join(SHOT, `${n}.png`) });

await scenario('イベント前は出ない。発火で preventDefault され、下部に固定表示（検索欄・本文に重ならない）', async (page) => {
  await ready(page); assert.equal(await shown(page), false);
  assert.equal(await fire(page), true, 'preventDefault されている'); await untilShown(page, true);
  const g = await page.evaluate(() => { const r = (s) => document.querySelector(s).getBoundingClientRect(); const b = r('#install-banner'), q = r('#query'); return { bt: b.top, bb: b.bottom, bl: b.left, br: b.right, vh: innerHeight, vw: innerWidth, qb: q.bottom, pos: getComputedStyle(document.querySelector('#install-banner')).position }; });
  assert.equal(g.pos, 'fixed'); assert.ok(g.bb <= g.vh && g.bb > g.vh - 40, `下端付近 ${g.bb}/${g.vh}`); assert.ok(g.bt > g.vh / 2, '画面の下半分'); assert.ok(g.bt > g.qb, '検索欄より下');
  assert.ok(g.bl >= 0 && g.br <= g.vw);
  assert.equal(await page.textContent('#install-add'), 'Install'); await shot(page, 'banner-desktop');
});
await scenario('[あとで]で消えて記録され、リロードしても再発火で出ない。7日経過の模擬では出る', async (page) => {
  await ready(page); await fire(page); await untilShown(page, true);
  await page.click('#install-later'); await untilShown(page, false);
  const at = Number(await stored(page)); assert.ok(Math.abs(Date.now() - at) < 60_000, '現在時刻付近が記録される');
  await ready(page); await fire(page); await page.waitForFunction(() => window.MiseInstall.event !== null, null, { timeout: T }); // 捕捉済み(イベントは保持される)でも、抑制中なら出さない
  await page.waitForTimeout(200); // 「出ないこと」の確認なので固定待ちで足りる
  assert.equal(await shown(page), false, '7日以内は出ない');
  await page.evaluate(([k, v]) => localStorage.setItem(k, v), [KEY, String(Date.now() - 7 * DAY - 1000)]);
  await ready(page); await fire(page); await untilShown(page, true);
  await page.evaluate(([k, v]) => localStorage.setItem(k, v), [KEY, String(Date.now() - 6 * DAY)]);
  await ready(page); await fire(page); assert.equal(await shown(page), false, '6日経過ではまだ出ない');
});
await scenario('[インストール]で prompt() が呼ばれてバナーが消える（accepted は記録しない）', async (page) => {
  await ready(page); await fire(page, 'accepted'); await untilShown(page, true);
  await page.click('#install-add'); await untilShown(page, false);
  assert.equal(await page.evaluate(() => window.__prompts), 1); assert.equal(await stored(page), null);
});
await scenario('インストール結果が dismissed なら却下扱いで記録', async (page) => {
  await ready(page); await fire(page, 'dismissed'); await untilShown(page, true);
  await page.click('#install-add'); await untilShown(page, false);
  assert.equal(await page.evaluate(() => window.__prompts), 1); assert.ok(await stored(page));
});
await scenario('appinstalled でバナーが消える', async (page) => {
  await ready(page); await fire(page); await untilShown(page, true);
  await page.evaluate(() => window.dispatchEvent(new Event('appinstalled'))); await untilShown(page, false);
});
await scenario('standalone 起動では出ない', async (page, ctx) => {
  await page.addInitScript(() => { const o = window.matchMedia.bind(window); window.matchMedia = (q) => (q.includes('display-mode: standalone') ? { matches: true, media: q, addEventListener() {}, removeEventListener() {} } : o(q)); });
  await ready(page); assert.equal(await fire(page), true); assert.equal(await shown(page), false);
});
await scenario('localStorage が使えなくてもバナーは出て閉じられる', async (page) => {
  await page.addInitScript(() => { Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } }); });
  await ready(page); await fire(page); await untilShown(page, true);
  await page.click('#install-later'); await untilShown(page, false);
});
await scenario('日本語表示・ダーク・モバイル幅（375px）で崩れず、下部に収まる', async (page) => {
  await page.addInitScript(() => { try { localStorage.setItem('mise-language', 'ja'); localStorage.setItem('mise-theme', 'dark'); } catch (_) {} });
  await ready(page); await fire(page); await untilShown(page, true);
  assert.equal(await page.textContent('#install-later'), 'あとで'); assert.equal(await page.textContent('#install-add'), 'インストール');
  const g = await page.evaluate(() => { const b = document.querySelector('#install-banner').getBoundingClientRect(); return { l: b.left, r: b.right, bb: b.bottom, h: b.height, vw: innerWidth, vh: innerHeight, sw: document.documentElement.scrollWidth }; });
  assert.ok(g.l >= 0 && g.r <= g.vw && g.bb <= g.vh && g.sw <= g.vw, JSON.stringify(g)); assert.ok(g.h < 130, `高さ ${g.h}`); await shot(page, 'banner-mobile-ja-dark');
}, { width: 375, height: 667 });
await scenario('回帰: 起動処理が最後まで走る（日本語カテゴリ・?category= / ?q= の復元。バナー関連の宣言順で止まらない）', async (page) => {
  await bootRegression(page, '/');
  await page.waitForSelector('#categories a');
  const first = await page.evaluate(() => ({ id: new URL(document.querySelector('#categories a').href).searchParams.get('category'), name: document.querySelector('#categories a').textContent }));
  assert.ok(/[぀-ヿ一-鿿]/.test(first.name), `日本語のカテゴリ名 ${first.name}`);
  assert.ok(/[぀-ヿ一-鿿]/.test(await page.title()), '日本語のタイトル');
  assert.ok(first.id, 'カテゴリリンクに category がある');
  await bootRegression(page, `/?category=${first.id}`);
  assert.equal(new URL(page.url()).searchParams.get('category'), first.id);
  await page.waitForFunction(() => / › |^[^M]/.test(document.title) && !document.title.startsWith('MISE —'), null, { timeout: T }); // カテゴリ名から始まるタイトル（復元された）
  await bootRegression(page, '/?q=zzqq');
  await page.waitForFunction(() => document.querySelector('#query').value === 'zzqq', null, { timeout: T });
}, { width: 1000, height: 700 }, 'ja-JP');
await scenario('install.js が読めなくても(404)起動は最後まで走る: 日本語タイトル・?q= の復元・pageerror なし', async (page) => {
  const errors = []; page.on('pageerror', (e) => errors.push(e.message));
  await page.route('**/install.js*', (r) => r.fulfill({ status: 404, body: 'not found' }));
  await page.goto(BASE + '/?q=zzqq'); await page.waitForFunction(() => window.__miseBooted === true, null, { timeout: T });
  assert.equal(await page.evaluate(() => 'MiseInstall' in window), false, 'install.js は読まれていない');
  assert.equal(await page.inputValue('#query'), 'zzqq');
  assert.ok(/[぀-ヿ一-鿿]/.test(await page.title()), `日本語のタイトル ${await page.title()}`);
  assert.equal(await shown(page), false, 'バナーは出ない');
  assert.deepEqual(errors, [], 'pageerror 0');
}, { width: 1000, height: 700 }, 'ja-JP');
await browser.close();
if (failed) { console.log(`${failed} 件失敗`); process.exit(1); }
console.log('すべて成功');
