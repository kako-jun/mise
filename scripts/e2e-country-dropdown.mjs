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
// SSR の時点でボタンはあるが、操作を受けるのは起動処理が範囲ナビを描き直した後（__miseBooted）。i18n-pending は日本語表示でしか付かないので、それだけでは英語表示で描き直し前のボタンを押してしまう。
const loadTop = async (p, u = '/jp/') => { await p.goto(BASE + u); await p.waitForFunction(() => window.__miseBooted === true && document.querySelector('.scope-dd-btn') && !document.documentElement.classList.contains('i18n-pending')); };
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
// #296: ドロップダウンに装飾を付けない。ボタンは下線・背景・枠なし（通常・hover・開いた状態・タップ後。背景は隣の地球の項目と同じ）、一覧の行は左線なし。判定は computed style。
// 文字色も共通のダーク指定を受けない: ボタンと▾は地球の項目と同じ、行はパネルの文字色（訪問済みも同色）。
// dark は保存済みの設定で開く場合、dark-toggle は画面のテーマ切替ボタンで切り替える場合。
// el とその子孫に、見える枠線・下線・影・背景画像・疑似要素が無いかを調べて、見つかったものを返す（フォーカス枠の outline は対象外）。
const decorations = (p, sel) => p.evaluate(async (s) => {
  const out = [];
  for (const root of document.querySelectorAll(s)) {
    await Promise.all(root.getAnimations().map((a) => a.finished)); // border-color の transition の途中値を読まない
    for (const e of [root, ...root.querySelectorAll('*')]) {
      const c = getComputedStyle(e), who = `${root.id || root.className}${e === root ? '' : ' ' + e.tagName}`;
      for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
        const color = c[`border${side}Color`];
        if (parseFloat(c[`border${side}Width`]) > 0 && c[`border${side}Style`] !== 'none' && color !== 'transparent' && !/^rgba\(.*,\s*0\)$/.test(color)) out.push(`${who} border-${side.toLowerCase()} ${color}`);
      }
      if (c.textDecorationLine !== 'none') out.push(`${who} text-decoration ${c.textDecorationLine}`);
      if (c.boxShadow !== 'none') out.push(`${who} box-shadow ${c.boxShadow}`);
      if (c.backgroundImage !== 'none') out.push(`${who} background-image ${c.backgroundImage}`);
      for (const pe of ['::before', '::after']) if (getComputedStyle(e, pe).content !== 'none') out.push(`${who}${pe}`);
    }
  }
  return out;
}, sel);
const css = (p, sel, prop) => p.$eval(sel, (e, k) => getComputedStyle(e)[k], prop);
const isHover = (p, sel) => p.$eval(sel, (e) => e.matches(':hover'));
const CLEAR = 'rgba(0, 0, 0, 0)';
// 訪問済み（:visited）の色は getComputedStyle に出ないので、CDP で :visited を強制して描画した画素を見る。返すのは背景（最多）の次に多い色 = 文字の地の色。
const textPixel = async (p, sel) => {
  const png = await p.locator(sel).screenshot();
  return p.evaluate(async (d) => {
    const i = new Image(); i.src = 'data:image/png;base64,' + d; await i.decode();
    const c = document.createElement('canvas'); c.width = i.width; c.height = i.height; const x = c.getContext('2d'); x.drawImage(i, 0, 0);
    const px = x.getImageData(0, 0, c.width, c.height).data, n = {};
    for (let k = 0; k < px.length; k += 4) { const key = `rgb(${px[k]}, ${px[k + 1]}, ${px[k + 2]})`; n[key] = (n[key] || 0) + 1; }
    return Object.entries(n).sort((a, b) => b[1] - a[1])[1]?.[0];
  }, png.toString('base64'));
};
const forcePseudo = async (p, sel, classes) => {
  const cdp = await p.context().newCDPSession(p);
  await cdp.send('DOM.enable'); await cdp.send('CSS.enable');
  const { root } = await cdp.send('DOM.getDocument');
  const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: sel });
  await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: classes });
  return () => cdp.detach();
};
for (const mode of ['light', 'dark', 'dark-toggle']) for (const view of ['phone', 'pc']) {
  await scenario(`#296 ${mode}: ボタンに下線・背景・枠が出ず、一覧の行に左線が出ず、文字色が漏れない`, view, async (p) => {
    const theme = mode === 'light' ? 'light' : 'dark';
    if (mode === 'dark') await p.addInitScript(() => localStorage.setItem('mise-theme', 'dark'));
    await loadTop(p);
    if (mode === 'dark-toggle') {
      assert.equal(await p.evaluate(() => document.documentElement.dataset.theme), 'light');
      await (view === 'phone' ? p.tap('#theme-toggle') : p.click('#theme-toggle'));
    }
    assert.equal(await p.evaluate(() => document.documentElement.dataset.theme), theme);
    const bad = [];
    const globe = '.scope-list .scope-item';
    const check = async (state, sel = '.scope-dd-btn') => {
      for (const d of await decorations(p, sel)) bad.push(`[${state}] ${d}`);
      if (sel !== '.scope-dd-btn') return;
      const [b, g] = [await css(p, sel, 'backgroundColor'), await css(p, globe, 'backgroundColor')];
      if (b !== g || b !== CLEAR) bad.push(`[${state}] button background ${b}（地球の項目 ${g}）`);
      const [bo, go] = [await css(p, sel, 'opacity'), await css(p, globe, 'opacity')]; // 薄さ
      if (bo !== go) bad.push(`[${state}] button opacity ${bo}（地球の項目 ${go}）`);
      const want = await css(p, globe, 'color');
      for (const [part, prop] of [['', 'color'], [' .scope-dd-name', 'color'], [' .scope-dd-caret', 'stroke']]) {
        const got = await css(p, sel + part, prop);
        if (got !== want) bad.push(`[${state}] button${part} ${prop} ${got}（地球の項目 ${want}）`);
      }
    };
    const press = (sel) => (view === 'phone' ? p.tap(sel) : p.click(sel));
    const away = () => p.mouse.move(1, 1);
    await away(); await shot(p, `${view}-${mode}-296-closed`);
    await check('通常');
    await p.hover('.scope-dd-btn'); assert.equal(await isHover(p, '.scope-dd-btn'), true); await check('hover');
    await press('.scope-dd-btn'); await untilShown(p, '.scope-dd-panel', true); await check('開いた状態');
    await away(); await check('開いた状態（ポインタは外）');
    await p.evaluate(() => Promise.all(document.querySelector('.scope-dd-panel').getAnimations().map((a) => a.finished)));
    await shot(p, `${view}-${mode}-296-open`);
    await check('一覧の行', '.scope-dd-opt');
    const ink = await css(p, '.scope-dd-panel', 'color');
    assert.equal(ink, theme === 'dark' ? 'rgb(239, 250, 255)' : 'rgb(18, 39, 44)');
    for (const [id, got] of await p.$$eval('.scope-dd-opt', (a) => a.map((e) => [e.id, getComputedStyle(e).color]))) if (got !== ink) bad.push(`[一覧の行] ${id} color ${got}（パネル ${ink}）`);
    await away();
    for (const row of ['#scope-opt-us', '#scope-opt-jp']) { // 通常の行と選択中の行。未訪問・訪問済みの描画色
      for (const [label, classes] of [['未訪問', []], ['訪問済み', ['visited']]]) {
        const release = await forcePseudo(p, row, classes);
        const got = await textPixel(p, `${row} .scope-dd-name`);
        if (got !== ink) bad.push(`[${label}] ${row} の描画色 ${got}（パネル ${ink}）`);
        await release();
      }
    }
    const filled = await p.$$eval('.scope-dd-opt:not(.is-active):not(:hover)', (a, clear) => a.filter((e) => getComputedStyle(e).backgroundColor !== clear).map((e) => e.id), CLEAR);
    assert.deepEqual(filled, [], 'hover・キーボード位置以外の行に背景は無い');
    // 検索欄は入力欄なので自前の背景と枠を持つ（共通の button / input[type=search] の配色が漏れていないこと）
    assert.deepEqual([await css(p, '.scope-dd-search', 'backgroundColor'), await css(p, '.scope-dd-search', 'borderTopColor')], theme === 'dark' ? ['rgb(7, 20, 28)', 'rgb(61, 98, 112)'] : ['rgb(255, 255, 255)', 'rgb(148, 181, 190)']);
    const sel = '.scope-dd-opt[aria-selected="true"]';
    assert.equal(await p.getAttribute(sel, 'id'), 'scope-opt-jp');
    assert.equal(await css(p, sel, 'fontWeight'), '700', '選択行は太字');
    await p.hover('#scope-opt-us'); assert.notEqual(await css(p, '#scope-opt-us', 'backgroundColor'), CLEAR, '行の hover の背景は残る');
    await check('行の hover', '.scope-dd-opt');
    await p.hover(sel); assert.notEqual(await css(p, sel, 'backgroundColor'), CLEAR); await check('選択行の hover', '.scope-dd-opt');
    await away();
    await p.focus('.scope-dd-search'); await p.keyboard.press('ArrowDown');
    assert.notEqual(await css(p, '.scope-dd-opt.is-active', 'backgroundColor'), CLEAR, 'キーボード位置の行の背景は残る');
    await check('キーボード位置の行', '.scope-dd-opt');
    await p.keyboard.press('Escape'); await untilShown(p, '.scope-dd-panel', false); await check('Esc で閉じた後');
    // タップ/クリックで開いて閉じた後と、行を選んだ後（描き直した新しいボタン）。ポインタ（スマホは残る hover）は動かさずに調べる。
    await press('.scope-dd-btn'); await untilShown(p, '.scope-dd-panel', true);
    if (view === 'phone') await p.touchscreen.tap(5, 5); else await p.click('.scope-dd-btn');
    await untilShown(p, '.scope-dd-panel', false); await check('タップで閉じた後');
    await p.hover('.scope-dd-btn'); await check('閉じた後の hover');
    await press('.scope-dd-btn'); await untilShown(p, '.scope-dd-panel', true);
    await press('#scope-opt-us'); await p.waitForFunction(() => location.pathname === '/us/'); await untilShown(p, '.scope-dd-panel', false);
    await p.waitForFunction(() => document.querySelector('.scope-dd-btn')?.innerText.includes('アメリカ'));
    await check('行を選んだ後');
    await p.hover('.scope-dd-btn'); await check('行を選んだ後の hover');
    assert.equal(bad.length, 0, `装飾・色の漏れが残っている ${bad.length} 件: ${[...new Set(bad)].join(' | ')}`);
    // 下線が無いので border-color の transition は付けない。地球の項目の transition は残す
    assert.deepEqual([await css(p, '.scope-dd-btn', 'transitionDuration'), await css(p, globe, 'transitionDuration'), await css(p, globe, 'transitionProperty')], ['0s', '0.15s', 'border-color']);
    // 残すもの: キーボードのフォーカス枠、地球の項目の hover の下線
    await p.focus('.scope-dd-btn'); await p.keyboard.press('Shift+Tab'); await p.keyboard.press('Tab');
    assert.equal(await activeIs(p, '.scope-dd-btn'), true);
    assert.deepEqual([await css(p, '.scope-dd-btn', 'outlineStyle'), await css(p, '.scope-dd-btn', 'outlineWidth')], ['solid', '2px'], 'フォーカス枠は残る');
    await p.hover(globe); assert.ok((await decorations(p, globe)).some((d) => d.includes('border-bottom')), '地球の項目の hover の下線は変えない');
    // ドロップダウン以外のボタン・リンクの配色は変えない（共通の button / a の指定を、素の要素で見る）
    await p.evaluate(() => { const b = document.createElement('button'); b.id = 'probe-296'; b.textContent = 'probe'; b.style.cssText = 'position:fixed;left:0;top:50%;z-index:99'; document.body.append(b); });
    await away(); assert.equal(await css(p, '#probe-296', 'backgroundColor'), theme === 'dark' ? 'rgb(23, 58, 71)' : 'rgb(239, 249, 250)');
    assert.equal(await css(p, '#probe-296', 'color'), theme === 'dark' ? 'rgb(209, 249, 255)' : 'rgb(7, 83, 100)');
    await p.evaluate(() => { const a = document.createElement('a'); a.id = 'probe-296-a'; a.href = '/probe-296'; a.textContent = 'MMMMMM'; a.style.cssText = 'position:fixed;left:0;top:60%;z-index:99;font:700 24px sans-serif;background:#000;padding:4px'; document.body.append(a); });
    assert.equal(await css(p, '#probe-296-a', 'color'), theme === 'dark' ? 'rgb(138, 233, 255)' : 'rgb(0, 0, 204)');
    if (theme === 'dark') { const release = await forcePseudo(p, '#probe-296-a', ['visited']); assert.equal(await textPixel(p, '#probe-296-a'), 'rgb(223, 170, 255)', '他のリンクの訪問済みの色は変えない'); await release(); }
    await p.hover('#probe-296'); assert.equal(await css(p, '#probe-296', 'backgroundColor'), theme === 'dark' ? 'rgb(32, 85, 100)' : 'rgb(215, 240, 243)');
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
  for (const s of ['.scope-list .scope-item', '.scope-dd-btn']) assert.equal(await p.$eval(s, (e) => getComputedStyle(e).transitionDuration), '0s', s);
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
    const net = []; // 旗の読み込みの失敗（落ちたときに原因が分かるように出す）
    p.on('requestfailed', (r) => r.url().includes('/flags/') && net.push(`${r.url().split('/').pop()} ${r.failure()?.errorText}`));
    p.on('response', (r) => r.url().includes('/flags/') && r.status() >= 400 && net.push(`${r.url().split('/').pop()} HTTP ${r.status()}`));
    await loadTop(p);
    const res = await p.evaluate(async () => {
      const box = document.createElement('div'); box.id = 'flag-grid'; box.style.cssText = 'position:fixed;inset:0;z-index:99;background:#fff;overflow:auto;padding:6px;display:flex;flex-wrap:wrap;gap:4px;align-content:flex-start;font:9px monospace';
      for (const c of [...MISE_ISO, 'XX', 'ZZ']) { const cell = document.createElement('div'); cell.style.cssText = 'width:48px;text-align:center'; cell.append(MiseCountry.el(c, { width: 40, height: 30 })); const l = document.createElement('div'); l.textContent = c; cell.append(l); box.append(cell); }
      document.body.append(box);
      await Promise.all([...box.querySelectorAll('img')].map((i) => i.complete ? 0 : new Promise((r) => { i.onload = i.onerror = r; })));
      await new Promise((r) => setTimeout(r, 300)); // error → バッジ差し替えを待つ
      const imgs = [...box.querySelectorAll('img')];
      return { total: MISE_ISO.length, imgs: imgs.length, ok: imgs.filter((i) => i.naturalWidth > 0).length, zero: imgs.filter((i) => !i.naturalWidth).map((i) => `${i.src.split('/').pop()} complete=${i.complete}`), badges: [...box.querySelectorAll('.scope-code')].map((b) => b.textContent) };
    });
    assert.deepEqual({ imgs: res.imgs, ok: res.ok, badges: res.badges }, { imgs: 249, ok: 249, badges: ['XX', 'ZZ'] }, `実際の値 ${JSON.stringify({ imgs: res.imgs, ok: res.ok, badges: res.badges, zero: res.zero, net })}`); // 失敗の表示は先頭 3 行だけなので 1 行にまとめる
    console.log('     旗', res.ok, '/', res.total, '、バッジ', res.badges.join(','));
    if (SHOT) await p.screenshot({ path: path.join(SHOT, `${view}-all-flags.png`) });
  });
}
await browser.close();
if (failed) { console.error(`${failed} 件失敗`); process.exit(1); }
