#!/usr/bin/env node
// 上下センタリングの画素測定 (#183)。仕様は docs/design/ui.md の「上下センタリングはインク基準で測る」。
// getBoundingClientRect（ボックス）では判定しない。対象だけを白地に黒で描いて（他は visibility:hidden）高解像度で撮り、
// 実際に塗られたピクセル（文字のインク）の上端・下端を取って、
//   in   : 枠（border の内側）の上端・下端までの距離（上 / 下）と、インク中心 − 枠中心（off）
//   pair : 旗・中点・アイコンの上下中心 と 文字のインク中心 の差（off）
// を CSS px で出す。許容は |off| ≤ 0.5px（strict な行のみ。ディセンダーのある字・絵文字は参考値）。
// 前提: `wrangler dev --local --persist-to <dir> --port <P>` が起動していて、ローカル D1 に migrate 済み＋ scripts/ink/seed.sql が入っていること（--remote は使わない）。
// 実行: PLAYWRIGHT_DIR=<playwright-core のパス> [CHROMIUM_PATH=...] [INK_FONT="Noto Sans CJK JP"] [INK_ONLY=tag,flag] node scripts/ink/measure-ink.mjs [baseUrl=http://localhost:8799] [--json out.json]
//   描画が固まる環境では `bwrap --dev-bind / / --tmpfs /dev/dri node ...` で包む。
//   終了コード: strict な行に許容外が 1 つでもあれば 1。
import { createRequire } from 'node:module';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { existsSync, readdirSync, writeFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright');
const args = process.argv.slice(2);
const jsonOut = args.includes('--json') ? args[args.indexOf('--json') + 1] : null;
const BASE = args.find((a) => /^https?:/.test(a)) || 'http://localhost:8799';
const TOL = 0.5, S = 4; // 許容(CSS px)・deviceScaleFactor
const ONLY = (process.env.INK_ONLY || '').split(',').filter(Boolean);
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  for (const d of (existsSync(base) ? readdirSync(base) : []).filter((x) => x.startsWith('chromium_headless_shell-')).sort().reverse()) {
    for (const sub of readdirSync(path.join(base, d))) { const p = path.join(base, d, sub, 'chrome-headless-shell'); if (existsSync(p)) return p; }
  }
  return undefined;
}

// ---- ページ内で動く部分 ------------------------------------------------------------------------------------
// 場面ごとの対象の集め方。1 対象 = {group, label, box, ink?, txt?, gfx?, gfxInk?, hide?, line1?}。要素は window.__ink[i] に保持する。
//   box: 枠の要素（in の基準。pair では使わない）。ink: 描画する要素（既定 box）。txt: 文字のある要素（x 範囲・最初の行の取得元）。
//   gfx: pair の相手（旗・中点・アイコン）。gfxInk: true ならそのインクの上下中心、false なら要素の箱の上下中心（画像）。
const collectAll = (scene) => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const all = (s, root = document) => [...root.querySelectorAll(s)].filter(vis);
  const first = (s, root = document) => all(s, root)[0] || null;
  const T = [];
  const seen = new Set(); // 同じ group・同じ label は最初の 1 つだけ（縦フィードは同種のカードが千件ある）
  const add = (o) => { const k = o.group + '|' + o.label; if ((o.box || o.gfx) && !seen.has(k)) { seen.add(k); T.push(o); } };
  const strictOf = (s) => !/[gjpqyJQ]|\p{Extended_Pictographic}/u.test(s);
  const refOf = (s) => (/[A-HK-PR-Z0-9\u3040-\u30ff\u3400-\u9fff]/u.exec(s) || [s.trim().slice(0, 1)])[0];
  const txtOf = (e) => e.textContent.trim();
  const inBox = (group, els, extra = {}) => els.forEach((e) => add({ group, label: txtOf(e).slice(0, 14) || e.id || e.className, strict: strictOf(txtOf(e)), box: e, ...extra }));
  const pair = (group, box, gfx, txt, extra = {}) => { if (gfx && txt) add({ group, label: txtOf(txt).slice(0, 14), strict: !/[gjpqyJQ]/.test(refOf(txtOf(txt))), box, gfx, txt, ...extra }); };
  if (scene === 'results') {
    const q = first('#query');
    if (q) { add({ group: 'input/search-text', label: 'query', strict: true, box: q }); const ic = first('.search-icon'); if (ic) add({ group: 'input/search-icon', label: 'icon', strict: true, box: q, ink: ic, txt: ic }); }
    inBox('tag/site-tag', all('a.site-tag'), { txt: null });
    all('.tag-chip').forEach((c) => add({ group: 'tag/chip', label: c.firstChild.textContent.trim(), strict: strictOf(c.firstChild.textContent), box: c, txt: c.firstChild, hide: [c.querySelector('button')] }));
    all('.tag-chip button').forEach((b) => add({ group: 'tag/chip-x', label: '×', strict: true, box: b }));
    all('#result-list > li').forEach((li) => {
      const head = li.querySelector('.site-head'), title = head && head.querySelector('.site-title');
      if (!title) return;
      const line = title.querySelector('a') || title;
      pair('row/bullet', null, first('.site-bullet', head), line, { gfxInk: true });
      pair('row/favicon', null, first('.site-icon img', head), line);
      pair('row/newtab-icon', null, first('.site-newtab svg', head) || first('.site-newtab', head), line);
      const place = head.querySelector('.site-place');
      if (place) pair('row/flag-region', null, place.querySelector('.site-flag img'), place.querySelector('.site-region'));
          });
  }
  if (scene === 'top') {
    all('.preferences button').forEach((b) => add({ group: 'btn/preferences', label: b.id, strict: true, box: b }));
    all('.scope-item').forEach((a) => { const g = a.querySelector('img.scope-flag, svg'), c = a.querySelector('.scope-count'); pair('scope/flag-count', null, g, c); });
    all('.region-select').forEach((s) => add({ group: 'select/region', label: 'region', strict: true, box: s, xr: 'left' }));
    all('.history-day').forEach((d) => pair('hud/history', null, d.querySelector('.history-track'), d.querySelector('.history-date')));
    all('.author-links a').forEach((a) => add({ group: 'btn/author-link', label: a.getAttribute('aria-label') || a.textContent.trim(), strict: true, box: a }));
    all('#feed-entry').forEach((a) => add({ group: 'link/feed-entry', label: txtOf(a), strict: strictOf(txtOf(a)), box: a, minH: matchMedia('(pointer: coarse)').matches ? 44 : 34 })); // 縦フィードの入口 (#178)
  }
  if (scene === 'request') {
    all('#req-submit, #req-type, #req-url').forEach((e) => add({ group: 'form/' + e.tagName.toLowerCase(), label: e.id, strict: true, box: e, xr: e.tagName === 'SELECT' ? 'left' : undefined }));
  }
  if (scene === 'banner') {
    all('.install-add, .install-close, #install-banner button').forEach((b) => add({ group: 'btn/install', label: txtOf(b).slice(0, 12) || b.id || b.className, strict: true, box: b }));
  }
  if (scene === 'feed') {
    all('#feed .f-icon').forEach((b) => add({ group: 'feed/icon-btn', label: b.getAttribute('aria-label') || b.textContent.trim(), strict: true, box: b }));
    all('#feed .f-directory').forEach((b) => add({ group: 'feed/directory', label: txtOf(b), strict: strictOf(txtOf(b)), box: b }));
    all('#feed .f-seg button').forEach((b) => add({ group: 'feed/seg', label: txtOf(b), strict: strictOf(txtOf(b)), box: b }));
    all('#feed .f-select').forEach((s) => add({ group: 'feed/select', label: 'select', strict: true, box: s, xr: 'left' }));
    all('#feed .f-nav button').forEach((b) => add({ group: 'feed/nav', label: b.id, strict: true, box: b }));
    all('#feed .f-open').forEach((b) => add({ group: 'feed/open', label: txtOf(b), strict: strictOf(txtOf(b)), box: b }));
    all('#feed .f-proto').forEach((b) => add({ group: 'feed/proto', label: txtOf(b), strict: strictOf(txtOf(b)), box: b }));
    all('#feed .f-seen').forEach((b) => add({ group: 'feed/seen-label', label: txtOf(b).slice(0, 10), strict: false, box: b }));
    all('#feed .f-place').slice(0, 2).forEach((p) => pair('feed/place', null, p.querySelector('img'), p.querySelector('span') || [...p.childNodes].find((n) => n.nodeType === 3 && n.textContent.trim()), { label: 'place' }));
  }
  if (scene === 'feed-panel') {
    all('#feed .f-panel-x').forEach((b) => add({ group: 'feed/panel-x', label: '×', strict: true, box: b, minH: 32 }));
    all('#feed .f-panel-acts button').forEach((b) => add({ group: 'feed/panel-btn', label: txtOf(b), strict: strictOf(txtOf(b)), box: b, minH: 32 }));
  }
  if (scene === 'feed') {
    all('#feed .f-end-actions button').forEach((b) => add({ group: 'feed/end-btn', label: txtOf(b).slice(0, 12), strict: strictOf(txtOf(b)), box: b }));
  }
  if (scene === 'about') {
    all('.preferences button').forEach((b) => add({ group: 'btn/preferences', label: b.id, strict: true, box: b }));
    all('.about-logo').forEach((a) => add({ group: 'about/logo', label: 'MISE', strict: true, box: a }));
  }
  window.__ink = T;
  return T.map((t, i) => ({ i, group: t.group, label: String(t.label), strict: !!t.strict, mode: t.gfx ? 'pair' : 'in' }));
};

// i 番目を孤立させ、描画する矩形を返す。
const prepare = (i) => {
  const t = window.__ink[i];
  document.querySelectorAll('[data-ink-vis]').forEach((e) => e.removeAttribute('data-ink-vis'));
  let st = document.getElementById('__ink_style');
  if (!st) { st = document.createElement('style'); st.id = '__ink_style'; document.head.appendChild(st); }
  st.textContent = `html{background:#fff!important;background-image:none!important}body{background:none!important;background-image:none!important}
body *{visibility:hidden!important;animation:none!important;transition:none!important}
[data-ink-vis],[data-ink-vis] *{visibility:visible!important;color:#000!important;background:none!important;border-color:transparent!important;text-shadow:none!important;opacity:1!important;box-shadow:none!important;-webkit-text-fill-color:#000!important;text-decoration:none!important;-webkit-backdrop-filter:none!important;backdrop-filter:none!important}
[data-ink-vis] svg *{fill:currentColor!important} /* アイコンのインクの上下端を測るため、輪郭だけの図形（◐ の輪など）も塗りつぶして黒くする。形の上下端は変わらない */
*{-webkit-backdrop-filter:none!important;backdrop-filter:none!important}
#feed .f-art,#feed .f-scrim,.f-bg,.f-hero,#globe-background{display:none!important}
[data-ink-hide],[data-ink-hide] *{visibility:hidden!important}`;
  document.querySelectorAll('[data-ink-hide]').forEach((e) => e.removeAttribute('data-ink-hide'));
  const anchor = t.box || t.gfx;
  if (!anchor.isConnected) return { stale: true }; // 描き直された（縦フィードのパネルなど）。呼び出し側が集め直す
  { const r = anchor.getBoundingClientRect(); if (r.top < 4 || r.bottom > innerHeight - 4 || r.left < 4 || r.right > innerWidth - 4) anchor.scrollIntoView({ block: 'center', inline: 'center' }); } // 画面内なら動かさない（縦フィードのパネルはスクロールで閉じる）
  const vis = t.gfx ? [t.txt && (t.txt.nodeType === 3 ? t.txt.parentElement : t.txt), t.gfxInk ? t.gfx : null] : [t.ink || t.box];
  vis.filter(Boolean).forEach((e) => e.setAttribute('data-ink-vis', ''));
  (t.hide || []).filter(Boolean).forEach((e) => e.setAttribute('data-ink-hide', ''));
  const rect = (r) => ({ l: r.left, t: r.top, r: r.right, b: r.bottom });
  const rangeRect = (n, first) => { const rg = document.createRange(); rg.selectNodeContents(n); const rs = [...rg.getClientRects()].filter((r) => r.width > 0 && r.height > 0); return rect(first ? (rs[0] || rg.getBoundingClientRect()) : rg.getBoundingClientRect()); };
  const o = { clipPad: 4 };
  if (t.box) {
    const r = t.box.getBoundingClientRect(), cs = getComputedStyle(t.box);
    o.inner = { l: r.left + parseFloat(cs.borderLeftWidth), t: r.top + parseFloat(cs.borderTopWidth), r: r.right - parseFloat(cs.borderRightWidth), b: r.bottom - parseFloat(cs.borderBottomWidth) };
    o.border = rect(r);
  }
  // pair は「先頭 1 文字」のインクで測る（ディセンダー・複数文字の字形差で中心が動かないよう、大文字・かな・漢字の先頭字を使う）。in は文字全体。
  const REF = /[A-HK-PR-Z0-9\u3040-\u30ff\u3400-\u9fff]/; // 大文字（J・Q を除く）・数字・かな・漢字。ディセンダーのある字・小文字・記号は避ける
  const firstChar = (n) => { const w = document.createTreeWalker(n, NodeFilter.SHOW_TEXT); let fb = null; for (let x = w.nextNode(); x; x = w.nextNode()) { const re = new RegExp(REF.source, 'u'), m = re.exec(x.textContent) || (fb ? null : /\S/u.exec(x.textContent)); if (!m) continue; const ok = re.test(m[0]); const rg = document.createRange(); rg.setStart(x, m.index); rg.setEnd(x, m.index + m[0].length); const r = rect(rg.getBoundingClientRect()); if (ok) return r; fb = fb || r; } return fb; };
  if (t.txt) o.txt = t.gfx ? firstChar(t.txt.nodeType === 3 ? t.txt.parentNode : t.txt) || rangeRect(t.txt, true) : t.txt.nodeType === 3 ? rangeRect(t.txt, false) : rangeRect(t.txt, true);
  if (t.gfx) o.gfx = rect(t.gfx.getBoundingClientRect());
  if (t.xr === 'left' && o.inner) o.txt = { l: o.inner.l + 6, r: o.inner.l + (o.inner.r - o.inner.l) * 0.55, t: o.inner.t, b: o.inner.b };
  const rs = [o.border, o.txt, o.gfx].filter(Boolean);
  o.clip = { x: Math.min(...rs.map((r) => r.l)) - 4, y: Math.min(...rs.map((r) => r.t)) - 4, w: 0, h: 0 };
  o.clip.w = Math.max(...rs.map((r) => r.r)) + 4 - o.clip.x; o.clip.h = Math.max(...rs.map((r) => r.b)) + 4 - o.clip.y;
  o.imgGfx = !!(t.gfx && !t.gfxInk);
  o.minH = t.minH || 0; o.outerH = t.box ? t.box.getBoundingClientRect().height : 0; // 押せる範囲の最小の高さ（text-box 化で縮む退行を検出する）
  return o;
};

// 撮った PNG(base64) から上下のインク端（CSS px、ビューポート座標）を返す。
const analyze = async ({ b64, clip, S, xr }) => {
  const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
  const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
  const g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, c.width, c.height).data;
  const inkRows = (x0, x1) => {
    let top = null, bot = null;
    const a = Math.max(0, Math.floor((x0 - clip.x) * S)), z = Math.min(c.width, Math.ceil((x1 - clip.x) * S));
    for (let y = 0; y < c.height; y++) for (let x = a; x < z; x++) { const k = (y * c.width + x) * 4, dark = 255 - (d[k] * 0.3 + d[k + 1] * 0.59 + d[k + 2] * 0.11); if (dark > 96 && d[k + 3] > 0) { if (top === null) top = y; bot = y + 1; break; } }
    return top === null ? null : { t: clip.y + top / S, b: clip.y + bot / S };
  };
  return Object.fromEntries(Object.entries(xr).map(([k, [x0, x1]]) => [k, inkRows(x0, x1)]));
};

// ---- ノード側 ----------------------------------------------------------------------------------------------
const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'] });
const rows = [];
const SCENES = [
  ['results', '/jp/?q=' + encodeURIComponent('寺') + '&' + ['Web', '寺院', 'Linux入門', 'ブログ😀', 'Rust'].map((x) => 'tag=' + encodeURIComponent(x)).join('&'), 'a.site-tag'],
  ['top', '/jp/', '#query'],
  ['request', '/jp/', '#recommend-link', async (p) => { await p.evaluate(() => { const r = document.getElementById('recommend'); r.hidden = false; }); }],
  ['banner', '/jp/', '#query', async (p) => { await p.evaluate(() => { const b = document.getElementById('install-banner'); if (b) b.hidden = false; }); }],
  ['feed', '/jp/?view=feed', '#feed .f-card'],
  // パネルは開いたときに中身が描かれ、スクロールで閉じる。別の場面にして、クリックで開いた直後に（スクロールせずに）測る
  ['feed-panel', '/jp/?view=feed', '#feed .f-seen', async (p) => { if (await p.$('#feed .f-panel[hidden]')) await p.click('#feed .f-seen'); await p.waitForSelector('#feed .f-panel:not([hidden]) .f-panel-acts button'); }],
  ['about', '/about/', '.about-logo'],
];
// テーマ: 既定はライトだけ。測定は対象を白地・黒に固定して描くので、ライトとダークで値は同じ（色は幾何に効かない）。INK_BOTH_THEMES=1 で両方。
// INK_QUICK=1: PC1280・ライト・ja だけ（開発中の素早い確認）
const CONDS = [['pc1280', 1280, 900, false], ['sp390', 390, 844, true]];
for (const scene of SCENES.filter(([s]) => !ONLY.length || ONLY.includes(s))) {
  const [sceneName, url, waitFor, setup] = scene;
  for (const [vp, width, height, coarse] of CONDS.filter((c) => !process.env.INK_QUICK || c[0] === 'pc1280')) for (const theme of (process.env.INK_QUICK || !process.env.INK_BOTH_THEMES ? ['light'] : ['light', 'dark'])) for (const lang of (process.env.INK_QUICK ? ['ja'] : ['ja', 'en'])) {
    const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: S, hasTouch: coarse, isMobile: coarse, bypassCSP: true, serviceWorkers: 'block' });
    await ctx.addInitScript(([t, l]) => { try { localStorage.setItem('mise-theme', t); localStorage.setItem('mise-language', l); localStorage.setItem('mise-install-dismissed', '0'); } catch (_) {} }, [theme, lang]);
    // 縦フィードの背景（ぼかし・backdrop-filter）は DSF 4 のソフトウェア描画では重くて固まる。測定に無関係で、絶対配置なので配置は変わらない。
    await ctx.addInitScript(() => { const s = document.createElement('style'); s.textContent = '#feed .f-art,#feed .f-scrim,.f-bg,.f-hero,#globe-background{display:none!important}*{-webkit-backdrop-filter:none!important;backdrop-filter:none!important}'; if (document.documentElement) document.documentElement.appendChild(s); });
    const page = await ctx.newPage();
    await page.route(/\/(globe\.js|land-110m\.geojson)/, (r) => r.abort()); // 地球儀（WebGL）は測定に無関係で、DSF 4 では描画が重く固まる
    const errs = [];
    page.on('pageerror', (e) => errs.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error' && !/favicon|thumb|Failed to load resource/.test(m.text())) errs.push(m.text()); });
    try {
      await page.goto(BASE + url, { waitUntil: 'networkidle' });
      await page.waitForSelector(waitFor, { timeout: 8000 }).catch(async (e) => { await page.screenshot({ path: process.env.INK_FAILSHOT || path.join(tmpdir(), 'mise-ink-fail.png') }); throw new Error(e.message + ' url=' + page.url()); });
      if (process.env.INK_FONT) await page.addStyleTag({ content: `html:root body, html:root button, html:root input, html:root select, html:root #feed { font-family: ${process.env.INK_FONT} !important; }` });
      if (process.env.INK_CSS) await page.addStyleTag({ content: process.env.INK_CSS }); // 試作用: 任意の CSS を足して測る
      if (setup) await setup(page);
      await page.waitForTimeout(500);
      await page.evaluate(() => { document.fonts && document.fonts.ready; });
      const list = await page.evaluate(collectAll, sceneName);
      if (process.env.INK_DEBUG) console.error(sceneName, vp, theme, lang, list.length, 'targets');
      for (const d of list) {
        if (ONLY.length && !ONLY.some((o) => d.group.includes(o) || sceneName === o)) continue;
        let p = await page.evaluate(prepare, d.i);
        for (let k = 0; p.stale && k < 3; k++) { // 対象が描き直されていたら、場面の準備をやり直して集め直す
          if (setup) await setup(page);
          const again = (await page.evaluate(collectAll, sceneName)).find((x) => x.group === d.group && x.label === d.label);
          if (again) { d.i = again.i; p = await page.evaluate(prepare, d.i); }
        }
        if (p.stale) { rows.push({ scene: sceneName, vp, theme, lang, group: d.group, label: d.label, strict: d.strict, ok: false, err: '対象が描き直されて測れない' }); continue; }
        await page.waitForTimeout(30);
        const clip = { x: p.clip.x, y: p.clip.y, width: Math.max(1, p.clip.w), height: Math.max(1, p.clip.h) };
        const png = await page.screenshot({ clip, animations: 'disabled' });
        const xr = {};
        const full = p.inner ? [p.inner.l, p.inner.r] : [p.gfx.l, p.gfx.r];
        xr.txt = p.txt ? [p.txt.l, p.txt.r] : full;
        if (p.gfx && !p.imgGfx) xr.gfx = [p.gfx.l, p.gfx.r];
        const ink = await page.evaluate(analyze, { b64: png.toString('base64'), clip: p.clip, S, xr });
        const row = { scene: sceneName, vp, theme, lang, group: d.group, label: d.label, strict: d.strict, mode: d.mode };
        if (!ink.txt) row.err = 'ink なし';
        else if (d.mode === 'in') {
          row.top = +(ink.txt.t - p.inner.t).toFixed(2); row.bottom = +(p.inner.b - ink.txt.b).toFixed(2);
          row.off = +(((ink.txt.t + ink.txt.b) / 2 - (p.inner.t + p.inner.b) / 2)).toFixed(2); row.h = +(p.inner.b - p.inner.t).toFixed(2);
        } else {
          const g = p.imgGfx ? p.gfx : { t: ink.gfx && ink.gfx.t, b: ink.gfx && ink.gfx.b };
          if (g.t == null) row.err = '相手(gfx)のインクなし';
          else { row.off = +(((ink.txt.t + ink.txt.b) / 2 - (g.t + g.b) / 2)).toFixed(2); row.gfxC = +((g.t + g.b) / 2).toFixed(2); row.txtC = +((ink.txt.t + ink.txt.b) / 2).toFixed(2); }
        }
        // strict: 中心のずれ ≤ 0.5px。参考値の行（ディセンダー・絵文字・見出し）: 枠にインクが 1px 未満まで迫らない（in のみ）。
        if (row.top !== undefined) row.margin = Math.min(row.top, row.bottom);
        if (p.minH) { row.h = +p.outerH.toFixed(2); if (p.outerH < p.minH - 0.3) row.err = `高さ ${p.outerH.toFixed(1)}px < ${p.minH}px（押せる範囲）`; }
        row.ok = row.err ? false : d.strict ? Math.abs(row.off) <= TOL + 1e-9 : row.margin === undefined ? Math.abs(row.off) <= TOL + 1.0 : row.margin >= 1;
        rows.push(row);
      }
    } catch (e) { rows.push({ scene: sceneName, vp, theme, lang, group: '(scene)', label: String(e.message).split('\n')[0], strict: true, ok: false, err: 'scene failed' }); }
    if (errs.length) rows.push({ scene: sceneName, vp, theme, lang, group: '(console)', label: errs.slice(0, 2).join(' | ').slice(0, 120), strict: true, ok: false, err: 'console error' });
    await ctx.close();
  }
}
await browser.close();

// ---- 対象が 0 件なら NG（場面ごとに期待するグループ。黙って測定から抜けるのを防ぐ。pc=PC幅でだけ出るもの）
const EXPECT = {
  results: ['tag/site-tag', 'tag/chip', 'tag/chip-x', 'row/bullet', 'row/newtab-icon', 'row/flag-region', 'input/search-text', 'input/search-icon'],
  top: ['btn/preferences', 'scope/flag-count', 'btn/author-link', 'link/feed-entry', 'pc:hud/history'],
  request: ['form/select', 'form/input', 'form/button'],
  banner: ['btn/install'],
  feed: ['feed/icon-btn', 'feed/directory', 'feed/seg', 'feed/select', 'feed/nav', 'feed/open', 'feed/proto', 'feed/seen-label', 'feed/place', 'feed/end-btn'],
  'feed-panel': ['feed/panel-x', 'feed/panel-btn'],
  about: ['btn/preferences', 'about/logo'],
};
for (const [sc, groups] of Object.entries(EXPECT)) {
  if (ONLY.length && !ONLY.includes(sc)) continue;
  const conds = new Set(rows.filter((r) => r.scene === sc).map((r) => `${r.vp}|${r.theme}|${r.lang}`));
  for (const c of conds) {
    const [vp, theme, lang] = c.split('|');
    for (const g of groups) { const pc = g.startsWith('pc:'), name = pc ? g.slice(3) : g; if (pc && vp !== 'pc1280') continue; if (!rows.some((r) => r.scene === sc && r.vp === vp && r.theme === theme && r.lang === lang && r.group === name)) rows.push({ scene: sc, vp, theme, lang, group: '(missing) ' + name, label: '対象が 0 件', strict: true, ok: false, err: '期待するグループが測定に出ていない' }); }
  }
}

// ---- 集計 --------------------------------------------------------------------------------------------------
const fmt = (r) => r.err ? `${r.err}` : r.mode === 'in' ? `上 ${r.top} / 下 ${r.bottom} / off ${r.off >= 0 ? '+' : ''}${r.off} (枠高 ${r.h})` : `中心差 ${r.off >= 0 ? '+' : ''}${r.off} (相手 ${r.gfxC} / 文字 ${r.txtC})`;
const byKey = new Map();
for (const r of rows) { const k = `${r.scene} | ${r.group} | ${r.label}`; if (!byKey.has(k)) byKey.set(k, []); byKey.get(k).push(r); }
let strictNg = 0, strictN = 0, infoNg = 0;
for (const [k, rs] of byKey) {
  const bad = rs.filter((r) => !r.ok);
  const st = rs[0].strict;
  if (st) { strictN += rs.length; strictNg += bad.length; } else infoNg += bad.length;
  const worst = rs.reduce((a, b) => (Math.abs(b.off ?? 99) > Math.abs(a.off ?? 99) ? b : a));
  console.log(`${bad.length ? (st ? 'NG ' : 'ng?') : 'ok '} ${k}  [${rs.length - bad.length}/${rs.length}] 最悪: ${worst.vp}/${worst.theme}/${worst.lang} ${fmt(worst)}`);
}
console.log(`\nstrict 行: ${strictN - strictNg}/${strictN} が許容 ±${TOL}px 内。参考値（ディセンダー・絵文字・見出し等）の許容外: ${infoNg}`);
if (jsonOut) writeFileSync(jsonOut, JSON.stringify(rows, null, 1));
process.exit(strictNg ? 1 : 0);
