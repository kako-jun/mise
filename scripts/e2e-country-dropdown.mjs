#!/usr/bin/env node
// 範囲ナビの国ドロップダウン（#291）と、全 ISO コードの国旗（MiseCountry）の実ブラウザ検証。
// 前提: `npm run db:migrate` 済みのローカル D1 で `npx wrangler dev --local --port 8799` が起動し、国が 2 つ以上ある（scripts/dev-seed-sites.mjs の合成サイト・移行済みの実データでよい）。--remote は使わない。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [SHOT_DIR=/tmp/shots] [WIDTH_FILE=/tmp/w.json] node scripts/e2e-country-dropdown.mjs [baseUrl=http://localhost:8799]
//   SHOT_DIR: 指定すると画面を保存する。 WIDTH_FILE: トップの幅の記録先（国の数を変えて 2 回走らせ、同じ幅であることを `COMPARE_WIDTH=<別の記録>` で比べる）。
// 可視の判定は getComputedStyle（hidden 属性そのものは見ない）。固定ミリ秒待ちは使わず条件待ち。
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const BASE = process.argv[2] || 'http://localhost:8799', T = 15000, SHOT = process.env.SHOT_DIR;
if (SHOT) mkdirSync(SHOT, { recursive: true });
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
const VIEWS = { phone: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true }, pc: { viewport: { width: 1280, height: 800 } } };
async function scenario(name, view, fn, opts = {}) {
  const ctx = await browser.newContext({ locale: opts.locale ?? 'ja-JP', ...VIEWS[view] }); const page = await ctx.newPage(); page.setDefaultTimeout(T);
  try { await fn(page); console.log(`ok   ${view} ${name}`); } catch (e) { failed++; console.log(`FAIL ${view} ${name}\n     ${e.message.split('\n').slice(0, 3).join('\n     ')}`); } finally { await ctx.close(); }
}
const shown = (p, sel) => p.evaluate((s) => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden'; }, sel);
const untilShown = (p, sel, want) => p.waitForFunction(([s, w]) => { const e = document.querySelector(s); return (!!e && getComputedStyle(e).display !== 'none') === w; }, [sel, want]);
const activeIs = (p, sel) => p.evaluate((s) => document.activeElement?.matches(s), sel);
const rowIds = (p) => p.$$eval('.scope-dd-opt', (as) => as.map((a) => a.id.replace('scope-opt-', '')));
const visibleIds = (p) => p.$$eval('.scope-dd-opt', (as) => as.filter((a) => getComputedStyle(a.closest('li')).display !== 'none').map((a) => a.id.replace('scope-opt-', '')));
const open = async (p) => { await p.click('.scope-dd-btn'); await untilShown(p, '.scope-dd-panel', true); };
const loadTop = async (p, u = '/jp/') => { await p.goto(BASE + u); await p.waitForSelector('.scope-dd-btn'); await p.waitForFunction(() => document.querySelector('.scope-dd-btn') && !document.documentElement.classList.contains('i18n-pending')); };
const shot = (p, n) => SHOT && p.screenshot({ path: path.join(SHOT, `${n}.png`) });

for (const view of ['phone', 'pc']) {
  await scenario('閉じた状態: ボタンは選択中の 1 件、パネルは computed で非表示、地球+件数は左に残る', view, async (p) => {
    await loadTop(p);
    assert.equal(await shown(p, '.scope-dd-btn'), true);
    assert.equal(await shown(p, '.scope-dd-panel'), false);
    assert.equal(await shown(p, '.scope-dd-backdrop'), false);
    assert.equal(await p.getAttribute('.scope-dd-btn', 'aria-expanded'), 'false');
    const txt = await p.$eval('.scope-dd-btn', (b) => b.innerText.replace(/\s+/g, ' ').trim());
    assert.match(txt, /^日本 \d+$/, txt);
    assert.equal(await p.$eval('.scope-dd-btn img.scope-flag', (i) => i.naturalWidth > 0), true, '旗が表示される');
    const [g, b] = await p.evaluate(() => [document.querySelector('.scope-list .scope-item').getBoundingClientRect(), document.querySelector('.scope-dd-btn').getBoundingClientRect()]);
    assert.ok(g.right <= b.left + 1 && Math.abs(g.top - b.top) < 12, `地球の右隣（${JSON.stringify([g.right, b.left])}）`);
    assert.ok(b.right <= (await p.evaluate(() => innerWidth)), '画面端で切れない');
    await shot(p, `${view}-closed`);
  });
  await scenario('開閉・並び（国コード順・全世界が先頭）・ARIA・フォーカス', view, async (p) => {
    await loadTop(p); await open(p);
    assert.equal(await p.getAttribute('.scope-dd-btn', 'aria-expanded'), 'true');
    const ids = await rowIds(p);
    assert.equal(ids[0], 'all');
    assert.deepEqual(ids.slice(1), [...ids.slice(1)].sort(), '国コード順');
    assert.ok(ids.length >= 3);
    assert.equal(await p.getAttribute('.scope-dd-list', 'role'), 'listbox');
    assert.equal(await p.getAttribute('.scope-dd-search', 'role'), 'combobox');
    assert.equal(await p.$$eval('.scope-dd-opt[role=option]', (a) => a.length), ids.length);
    assert.equal(await p.$eval('.scope-dd-opt[aria-selected=true]', (a) => a.id), 'scope-opt-jp');
    const first = await p.$eval('#scope-opt-all', (a) => a.innerText.replace(/\s+/g, ' '));
    assert.match(first, /^全世界 所在国が未設定のサイトを含む \d+$/, first); // #229: 全世界の件数は国の合計と一致しない
    assert.match(await p.getAttribute('#scope-opt-all', 'title'), /所在国が未設定/);
    assert.match(await p.getAttribute('.scope-list .scope-item', 'title'), /所在国が未設定/);
    const row = await p.$eval('#scope-opt-us', (a) => a.innerText.replace(/\s+/g, ' ')); assert.match(row, /^アメリカ合衆国 \d+$/, row);
    await p.evaluate(() => Promise.all(document.querySelector('.scope-dd-panel').getAnimations().map((a) => a.finished))); // スライドの終了を待ってから位置を測る
    const r = await p.evaluate(() => { const pn = document.querySelector('.scope-dd-panel').getBoundingClientRect(), b = document.querySelector('.scope-dd-btn').getBoundingClientRect(); return { pn, b, h: innerHeight, w: innerWidth, back: getComputedStyle(document.querySelector('.scope-dd-backdrop')).display }; });
    if (view === 'phone') { assert.ok(Math.abs(r.pn.bottom - r.h) < 1 && r.pn.left < 1 && r.pn.right > r.w - 1, `ボトムシート ${JSON.stringify(r.pn)}`); assert.equal(r.back, 'block'); }
    else { assert.ok(r.pn.top >= r.b.bottom && r.pn.left >= 0 && r.pn.right <= r.w, `ボタンの下のポップオーバー ${JSON.stringify(r.pn)}`); assert.equal(r.back, 'none'); }
    if (view === 'pc') assert.equal(await activeIs(p, '.scope-dd-search'), true, '開くと検索欄にフォーカス');
    await shot(p, `${view}-open`);
    await p.keyboard.press('Escape'); await untilShown(p, '.scope-dd-panel', false);
    assert.equal(await activeIs(p, '.scope-dd-btn'), true, 'Esc でボタンへフォーカスが戻る');
    await open(p); await p.mouse.click(5, view === 'phone' ? 5 : 780); await untilShown(p, '.scope-dd-panel', false); // 外側（モバイルは背幕）のクリックで閉じる
  });
  await scenario('検索: 日本語名・英語名・国コード', view, async (p) => {
    await loadTop(p); await open(p);
    const q = async (v) => { await p.fill('.scope-dd-search', v); return visibleIds(p); };
    // 期待は「含むべきコードを含む・含まないべきコードを含まない」形（国の数・シードに依存しない）
    const has = async (v, yes, no = []) => { const ids = await q(v); for (const c of yes) assert.ok(ids.includes(c), `${v}: ${c} を含む（${ids.slice(0, 8)}）`); for (const c of no) assert.ok(!ids.includes(c), `${v}: ${c} を含まない`); return ids; };
    await has('ドイツ', ['de'], ['fr', 'jp', 'us']);
    await has('united', ['gb', 'us'], ['de', 'jp']);
    assert.deepEqual(await q('FR'), (await q('fr')));
    await has('fr', ['fr'], ['de', 'jp']);
    await has('jp', ['jp'], ['de', 'us']);
    await has('germany', ['de'], ['fr', 'jp']);
    await has('全世界', ['all'], ['de', 'jp']);
    assert.deepEqual(await q('zzzz'), []);
    assert.equal(await shown(p, '.scope-dd-empty'), true);
    assert.deepEqual((await q('')).length, (await rowIds(p)).length);
    assert.equal(await shown(p, '.scope-dd-empty'), false);
    await q('ドイツ'); await shot(p, `${view}-search`);
  });
  await scenario('選択 → 遷移・URL・戻る/進む・フォーカス（マウス/タップ）', view, async (p) => {
    await loadTop(p); await open(p);
    await p.click('#scope-opt-us');
    await p.waitForFunction(() => location.pathname === '/us/');
    await untilShown(p, '.scope-dd-panel', false);
    await p.waitForFunction(() => document.querySelector('.scope-dd-btn')?.innerText.includes('アメリカ'));
    assert.equal(await activeIs(p, '.scope-dd-btn'), true, '選択後はボタンへフォーカス');
    assert.equal(await p.$eval('.scope-dd-opt[aria-selected=true]', (a) => a.id), 'scope-opt-us');
    await p.goBack(); await p.waitForFunction(() => location.pathname === '/jp/');
    await p.waitForFunction(() => document.querySelector('.scope-dd-btn')?.innerText.includes('日本'));
    await p.goForward(); await p.waitForFunction(() => location.pathname === '/us/');
    await open(p); await p.click('#scope-opt-all'); await p.waitForFunction(() => location.pathname === '/all/');
    assert.match(await p.$eval('.scope-dd-btn', (b) => b.innerText), /国を選ぶ/);
    assert.equal(await p.$eval('.scope-dd-opt[aria-selected=true]', (a) => a.id), 'scope-opt-all');
    await p.click('.scope-list .scope-item'); // 地球（全世界）のリンクは従来どおり
  });
}
await scenario('キーボードだけで操作（矢印・Enter・Esc・タイプアヘッド）', 'pc', async (p) => {
  await loadTop(p);
  await p.focus('.scope-dd-btn'); await p.keyboard.press('ArrowDown'); await untilShown(p, '.scope-dd-panel', true);
  assert.equal(await activeIs(p, '.scope-dd-search'), true);
  const act = () => p.getAttribute('.scope-dd-search', 'aria-activedescendant');
  assert.equal(await act(), 'scope-opt-jp', '開くと選択中の行がアクティブ');
  await p.keyboard.press('Home'); // 検索欄では行頭へ移るだけ（行は動かさない）
  await p.keyboard.press('ArrowUp'); const up = await act(); assert.notEqual(up, 'scope-opt-jp');
  await p.keyboard.press('Escape'); await untilShown(p, '.scope-dd-panel', false); assert.equal(await activeIs(p, '.scope-dd-btn'), true);
  await p.keyboard.press('d'); await untilShown(p, '.scope-dd-panel', true); // タイプアヘッド: 文字で開いて絞る
  assert.equal(await p.inputValue('.scope-dd-search'), 'd'); assert.ok((await visibleIds(p)).includes('de'));
  await p.keyboard.type('e'); assert.deepEqual(await visibleIds(p).then((v) => v.includes('de')), true);
  await p.keyboard.press('ArrowDown'); await p.keyboard.press('Enter');
  await p.waitForFunction(() => /^\/[a-z]{2}\/$/.test(location.pathname) && location.pathname !== '/jp/');
  assert.equal(await activeIs(p, '.scope-dd-btn'), true, 'Enter 後もボタンにフォーカス');
  await p.keyboard.press('Enter'); await untilShown(p, '.scope-dd-panel', true); // ボタンから Enter でも開く
  await p.keyboard.press('Tab'); await untilShown(p, '.scope-dd-panel', false); // Tab で閉じて次へ
  assert.equal(await activeIs(p, '.scope-dd-search'), false, 'Tab でフォーカスは検索欄に残らない');
  await p.focus('.scope-dd-btn'); await p.keyboard.press('Enter'); await untilShown(p, '.scope-dd-panel', true);
  await p.mouse.click(10, 700); await untilShown(p, '.scope-dd-panel', false); // 外側クリック
});
await scenario('区画の範囲（/jp/13/）でも日本が選択中・都道府県の選び方は変わらない', 'pc', async (p) => {
  await loadTop(p, '/jp/13/');
  assert.match(await p.$eval('.scope-dd-btn', (b) => b.innerText), /^日本/);
  assert.equal(await p.$$eval('.region-select, select.region-select', (a) => a.length), 0);
});
await scenario('英語表示: 国名は英語、検索も効く', 'pc', async (p) => {
  await loadTop(p); await open(p);
  assert.match(await p.$eval('.scope-dd-btn', (b) => b.innerText), /^Japan/);
  assert.match(await p.$eval('#scope-opt-us', (a) => a.innerText), /^United States/);
  await p.fill('.scope-dd-search', '日本'); assert.deepEqual(await visibleIds(p), ['jp']);
}, { locale: 'en-US' });
await scenario('国が 1 つ以下ならドロップダウンを出さない（従来の旗の並び）', 'pc', async (p) => {
  await p.route('**/api/scopes', (r) => r.fulfill({ json: { total: 5, countries: [{ code: 'JP', count: 5 }], regions: [] } }));
  await p.goto(BASE + '/jp/'); await p.waitForSelector('.scope-item');
  await p.waitForFunction(() => document.querySelectorAll('.scope-list .scope-item').length === 2);
  assert.equal(await p.$$eval('.scope-dd', (a) => a.length), 0);
});
await scenario('reduced-motion: ボトムシートのアニメと▾の回転が止まる', 'phone', async (p) => {
  await p.emulateMedia({ reducedMotion: 'reduce' });
  await loadTop(p); await open(p);
  assert.equal(await p.$eval('.scope-dd-panel', (e) => getComputedStyle(e).animationName), 'none');
  assert.equal(await p.$eval('.scope-dd-caret', (e) => getComputedStyle(e).transitionDuration), '0s');
});
// JS 無効（SSR のまま）
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, javaScriptEnabled: false }); const p = await ctx.newPage();
  try {
    await p.goto(BASE + '/jp/');
    assert.equal(await shown(p, '.scope-dd-btn'), false, 'JS なしではボタンを隠す');
    assert.equal(await shown(p, '.scope-dd-search'), false);
    assert.equal(await shown(p, '.scope-dd-panel'), true, 'JS なしでは一覧が開いたまま');
    const n = await p.$$eval('.scope-dd-opt', (a) => a.filter((x) => x.getBoundingClientRect().width > 0).length); assert.ok(n >= 3, `${n} 件のリンクが見える`);
    console.log('ok   pc JS 無効の一覧'); if (SHOT) await p.screenshot({ path: path.join(SHOT, 'pc-nojs.png') });
  } catch (e) { failed++; console.log(`FAIL JS 無効\n     ${e.message.split('\n')[0]}`); } finally { await ctx.close(); }
}
// トップの幅: 国の数に依らない。記録して、別の記録と比べる。
for (const view of ['phone', 'pc']) {
  await scenario('トップの幅の記録（国の数に依らない）', view, async (p) => {
    await loadTop(p);
    const w = await p.evaluate(() => ({ docW: document.documentElement.scrollWidth, innerW: innerWidth, nav: Math.round(document.querySelector('#scope-nav').getBoundingClientRect().width * 10) / 10, btn: Math.round(document.querySelector('.scope-dd-btn').getBoundingClientRect().width * 10) / 10, header: Math.round(document.querySelector('header').getBoundingClientRect().width * 10) / 10, countries: document.querySelectorAll('.scope-dd-opt').length - 1 }));
    assert.ok(w.docW <= w.innerW, `横スクロールが出ない ${JSON.stringify(w)}`);
    console.log('     ', view, JSON.stringify(w));
    if (process.env.WIDTH_FILE) { const f = process.env.WIDTH_FILE; const all = existsSync(f) ? JSON.parse(readFileSync(f, 'utf8')) : {}; all[view] = w; writeFileSync(f, JSON.stringify(all)); }
    if (process.env.COMPARE_WIDTH) { const o = JSON.parse(readFileSync(process.env.COMPARE_WIDTH, 'utf8'))[view]; for (const k of ['nav', 'btn', 'header', 'docW']) assert.equal(w[k], o[k], `${k}: ${o[k]} (国 ${o.countries}) と ${w[k]} (国 ${w.countries}) が違う`); }
  });
}
// 全 ISO コードの旗（249）。旗を出す共通部品 MiseCountry を、実ブラウザで全コード分描く。
for (const view of ['phone', 'pc']) {
  await scenario('全 249 コードの旗が表示される（未知コードは国コードのバッジ）', view, async (p) => {
    await loadTop(p);
    const res = await p.evaluate(async () => {
      const box = document.createElement('div'); box.id = 'flag-grid'; box.style.cssText = 'position:fixed;inset:0;z-index:99;background:#fff;overflow:auto;padding:6px;display:flex;flex-wrap:wrap;gap:4px;align-content:flex-start;font:9px monospace';
      for (const c of [...MISE_ISO, 'XX', 'ZZ']) { const cell = document.createElement('div'); cell.style.cssText = 'width:48px;text-align:center'; cell.append(MiseCountry.el(c, { width: 40, height: 30 })); const l = document.createElement('div'); l.textContent = c; cell.append(l); box.append(cell); }
      document.body.append(box);
      await Promise.all([...box.querySelectorAll('img')].map((i) => i.complete ? 0 : new Promise((r) => { i.onload = i.onerror = r; })));
      await new Promise((r) => setTimeout(r, 300)); // error → バッジ差し替えを待つ
      const imgs = [...box.querySelectorAll('img')];
      return { total: MISE_ISO.length, imgs: imgs.length, ok: imgs.filter((i) => i.naturalWidth > 0).length, badges: [...box.querySelectorAll('.scope-code')].map((b) => b.textContent) };
    });
    assert.deepEqual({ imgs: res.imgs, ok: res.ok, badges: res.badges }, { imgs: 249, ok: 249, badges: ['XX', 'ZZ'] });
    console.log('     旗', res.ok, '/', res.total, '、バッジ', res.badges.join(','));
    if (SHOT) await p.screenshot({ path: path.join(SHOT, `${view}-all-flags.png`) });
  });
}
await browser.close();
if (failed) { console.error(`${failed} 件失敗`); process.exit(1); }
