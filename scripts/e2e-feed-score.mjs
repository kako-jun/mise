#!/usr/bin/env node
// 縦フィードの戻る矢印・スコアに枠が無いこと(#276)と、カードが切り替わった瞬間にスコアが増えること(#277)を、スマホ幅の実ブラウザで確認する。
// 前提: wrangler dev --local とローカル D1(サイト数件以上)。
// 実行: PLAYWRIGHT_DIR=<node_modules/playwright> [CHROMIUM_PATH=...] node scripts/e2e-feed-score.mjs [baseUrl=http://localhost:8799]
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
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter(x => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
try {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const page = await ctx.newPage();
  let replaced = false; await page.route('**/feed.js*', async r => { const res = await r.fetch(), src = await res.text(); replaced = src.includes('const AUTO_ADVANCE_MS = 12000;'); await r.fulfill({ response: res, body: src.replace('const AUTO_ADVANCE_MS = 12000;', 'const AUTO_ADVANCE_MS = 1e9;') }); }); // 自動送り(12 秒)は常に有効でオフにできない: 計測に混ざらないよう、この検証だけ間隔を実質無限にする
  await page.goto(BASE + '/all/?category=recent&view=feed');
  await page.waitForSelector('.f-card.is-active'); assert.ok(replaced, 'AUTO_ADVANCE_MS の置換が効いている(効かなければ自動送りが計測に混ざる)'); await page.waitForTimeout(500);
  const styles = await page.evaluate(() => Object.fromEntries([['back', '.f-directory'], ['score', '.f-surf']].map(([k, s]) => {
    const el = document.querySelector('#feed ' + s), cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return [k, { bw: ['Top', 'Right', 'Bottom', 'Left'].map(x => cs['border' + x + 'Width']), shadow: cs.boxShadow, backdrop: cs.backdropFilter, w: r.width, h: r.height }];
  })));
  for (const [k, v] of Object.entries(styles)) {
    assert.ok(v.bw.every(w => w === '0px'), k + ' border'); assert.equal(v.shadow, 'none', k + ' box-shadow'); assert.equal(v.backdrop, 'none', k + ' backdrop');
    assert.ok(v.w >= 36 && v.h >= 36, k + ' tap target');
  }
  // 切り替え→スコア更新(#277): キー送りから 100ms 以内。連続送りは通過点を数えず送り先の 1 件だけ
  const num = () => page.locator('.f-surf-num').textContent();
  const n0 = Number(await num());
  await page.evaluate(() => { window.__t = null; new MutationObserver(() => { window.__t ??= performance.now(); }).observe(document.querySelector('.f-surf-num'), { childList: true, characterData: true, subtree: true }); window.__p = performance.now(); });
  await page.keyboard.press('j'); await page.waitForTimeout(2500);
  const dt = await page.evaluate(() => window.__t - window.__p);
  assert.equal(Number(await num()), n0 + 1); console.log('press -> score ms', Math.round(dt));
  assert.ok(dt < 800, 'スナップ(~300ms)から遅れず数える: ' + dt);
  const n1 = Number(await num());
  for (let i = 0; i < 5; i++) { await page.keyboard.press('j'); await page.waitForTimeout(100); }
  await page.waitForTimeout(2500);
  assert.equal(Number(await num()), n1 + 1, '連続送りは送り先の 1 件だけ');
  // 通過しただけのカードは数えない(#277): スナップを外してホイール相当に位置を連続で動かし、止まった 1 枚だけが数えられる
  await page.waitForTimeout(1500);
  const n2 = Number(await num());
  await page.evaluate(async () => {
    const sc = document.querySelector('.f-scroll'), h = sc.clientHeight; sc.style.scrollSnapType = 'none';
    const base = sc.scrollTop; let off = 7; // どの通過点も境界から 4px 以上離れるようにずらす(境界でたまたま止まるのは「止まって揃った」に当たる)
    const clear = o => { for (let y = base + o; y < base + o + h * 4; y += 40) { const m = (y - base) % h; if (Math.min(m, h - m) <= 4) return false; } return true; };
    while (!clear(off)) off++;
    for (let y = base + off; y < base + off + h * 4; y += 40) { sc.scrollTop = y; await new Promise(r => setTimeout(r, 16)); }
    sc.scrollTop = Math.round(sc.scrollTop / h) * h; sc.style.scrollSnapType = '';
  });
  await page.waitForTimeout(1500);
  assert.equal(Number(await num()), n2 + 1, 'ホイール相当の通過は止まった 1 枚だけ');
  // 指の fling(複数カード分)でも、見た枚数を超えて数えない。止まってから揃うまでの時間も測る
  const n3 = Number(await num()), cdp = await ctx.newCDPSession(page);
  await page.evaluate(() => { window.__t = null; window.__e = null; const sc = document.querySelector('.f-scroll'); sc.addEventListener('scrollend', () => { window.__e = performance.now(); }); new MutationObserver(() => { window.__t ??= performance.now(); }).observe(document.querySelector('.f-surf-num'), { childList: true, characterData: true, subtree: true }); });
  const tp = y => [{ x: 195, y, id: 1 }];
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(760) });
  for (let y = 740; y >= 100; y -= 40) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(y) }); await page.waitForTimeout(8); }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(2500);
  const fl = await page.evaluate(() => ({ idx: [...document.querySelectorAll('.f-card')].findIndex(c => c.classList.contains('is-active')), t: window.__t, e: window.__e }));
  console.log('fling', JSON.stringify(fl), await page.evaluate(() => document.querySelector('.f-scroll').scrollTop));
  const d3 = Number(await num()) - n3; assert.ok(d3 >= 1 && d3 <= 5, 'fling の数え方 ' + d3);
  console.log('fling counted', d3, 'scrollend->score ms', fl.t && fl.e ? Math.round(fl.t - fl.e) : 'n/a');
  // 明るい・暗いサムネ × スマホ・PC 幅で、戻る矢印・ラベル・スコアの文字色と背景のコントラスト比(影を除く)が 4.5 以上
  const lum = c => { const v = c.map(x => { x /= 255; return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; }); return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]; };
  for (const [w, bg, scheme] of [[390, '#fff', 'light'], [390, '#000', 'light'], [390, '#fff', 'dark'], [1280, null, 'light'], [1280, null, 'dark']]) {
    const p2 = await (await browser.newContext({ viewport: { width: w, height: 844 }, colorScheme: scheme, bypassCSP: true })).newPage();
    await p2.goto(BASE + '/all/?category=recent&view=feed'); await p2.waitForSelector('.f-card.is-active'); await p2.waitForTimeout(300);
    const cols = await p2.evaluate(() => Object.fromEntries([['.f-directory svg', '.f-directory'], ['.f-surf-label', '.f-surf-label'], ['.f-surf-num', '.f-surf-num']].map(([k, s]) => [k, getComputedStyle(document.querySelector('#feed ' + s)).color.match(/\d+/g).slice(0, 3).map(Number)])));
    await p2.addStyleTag({ content: (bg ? `#feed .f-hero, #feed .f-card{background:${bg} !important} #feed .f-img{display:none !important} ` : '') + '#feed .f-bar *{color:transparent !important;stroke:transparent !important;text-shadow:none !important;filter:none !important}' });
    await p2.waitForTimeout(300);
    for (const sel of ['.f-directory svg', '.f-surf-label', '.f-surf-num']) {
      const box = await p2.locator('#feed ' + sel).first().boundingBox(); if (!box || box.width === 0) continue;
      const png = await p2.screenshot({ clip: { x: box.x, y: box.y, width: Math.max(box.width, 4), height: Math.max(box.height, 4) } });
      const px = await p2.evaluate(async b64 => { const im = new Image(); im.src = 'data:image/png;base64,' + b64; await im.decode(); const c = document.createElement('canvas'); c.width = im.width; c.height = im.height; const x = c.getContext('2d'); x.drawImage(im, 0, 0); return Array.from(x.getImageData(1, 1, 1, 1).data).slice(0, 3); }, png.toString('base64'));
      const l1 = lum(cols[sel]), l2 = lum(px), ratio = (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05);
      console.log(w, bg || 'page', scheme, sel, 'bg', px.join(','), 'ratio', ratio.toFixed(2)); assert.ok(ratio >= 4.5, `${w} ${bg} ${scheme} ${sel} ${ratio}`);
    }
  }
  console.log('ok', JSON.stringify(styles));
} finally { await browser.close(); }
