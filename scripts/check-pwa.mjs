#!/usr/bin/env node
// 自己検査（外部アクセスなし）: PWA 関連の配信設定が揃っていること。静的ファイルは Worker を通らないので、
// public/_headers と wrangler.jsonc の設定そのものを読んで確かめる。
// 実行: node scripts/check-pwa.mjs
import fs from 'node:fs';
import zlib from 'node:zlib';
import { pageCsp } from '../src/security.js';
const root = new URL('../', import.meta.url).pathname;
const read = p => fs.readFileSync(root + p, 'utf8');
let failed = 0;
const check = (label, ok) => { console.log(`${ok ? 'OK  ' : 'NG  '}${label}`); if (!ok) failed++; };

// _headers: パスごとのブロックを解釈する（インデントした行がヘッダ）
const blocks = {}; let cur = null;
for (const line of read('public/_headers').split('\n')) {
  if (!line.trim() || line.startsWith('#')) continue;
  if (/^\S/.test(line)) { cur = line.trim(); blocks[cur] = {}; continue; }
  const i = line.indexOf(':'); if (cur && i > 0) blocks[cur][line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
}
check('_headers /sw.js が Cache-Control: no-cache', blocks['/sw.js']?.['cache-control'] === 'no-cache');
check('_headers /manifest.webmanifest が no-cache', blocks['/manifest.webmanifest']?.['cache-control'] === 'no-cache');
check('_headers /manifest.webmanifest が application/manifest+json', blocks['/manifest.webmanifest']?.['content-type'] === 'application/manifest+json');
// sw.js の型は静的配信の既定（text/javascript）に任せる。_headers で上書きするなら text/javascript でなければならない
check('sw.js の Content-Type が text/javascript（既定または _headers の指定）', !blocks['/sw.js']?.['content-type'] || /^(text|application)\/javascript/.test(blocks['/sw.js']['content-type']));
const wr = read('wrangler.jsonc');
check('wrangler run_worker_first が .webmanifest と .js を除外', wr.includes('"!/*.webmanifest"') && wr.includes('"!/*.js"'));

const csp = pageCsp([]);
check('CSP に manifest-src \'self\'', csp.includes("manifest-src 'self'"));
check('CSP に worker-src \'self\'', csp.includes("worker-src 'self'"));

const manifest = JSON.parse(read('public/manifest.webmanifest'));
const html = read('public/index.html');
check('index.html が manifest を参照', html.includes('<link rel="manifest" href="/manifest.webmanifest">'));
check('start_url と scope が /', manifest.start_url === '/' && manifest.scope === '/');
const metas = [...html.matchAll(/<meta name="theme-color"[^>]*>/g)].map(m => m[0]);
check('theme-color は 1 つだけで media 指定なし（ページはライト既定で OS のダークに追従しない）', metas.length === 1 && !/media=/.test(metas[0]));
const metaColor = metas[0]?.match(/content="([^"]+)"/)?.[1];
check('manifest.theme_color が index.html の theme-color と一致', !!metaColor && metaColor.toLowerCase() === manifest.theme_color.toLowerCase());
check('manifest.background_color が theme_color と一致（既定のライト背景）', manifest.background_color?.toLowerCase() === manifest.theme_color.toLowerCase());
check('sw.js のオフライン画面がダーク配色を持たない（ページと一致）', !/prefers-color-scheme/.test(read('public/sw.js')));
for (const icon of manifest.icons) {
  const f = root + 'public' + icon.src;
  let ok = false;
  if (fs.existsSync(f)) { const b = fs.readFileSync(f); ok = b.readUInt32BE(16) + 'x' + b.readUInt32BE(20) === icon.sizes; }
  check(`アイコン ${icon.src} が存在し sizes(${icon.sizes}) と PNG の寸法が一致`, ok);
}
check('apple-touch-icon が 180px の PNG', (() => { const m = html.match(/rel="apple-touch-icon" href="([^"]+)"/); if (!m || !fs.existsSync(root + 'public' + m[1])) return false; const b = fs.readFileSync(root + 'public' + m[1]); return b.readUInt32BE(16) === 180 && b.readUInt32BE(20) === 180; })());

// --- アイコン一式（#151）---
for (const page of ['public/index.html', 'public/about.html']) {
  const h = read(page);
  check(`${page} に <link rel="icon"> が ico(32x32) と svg の 2 本`, h.includes('<link rel="icon" href="/favicon.ico" sizes="32x32">') && h.includes('<link rel="icon" href="/favicon.svg" type="image/svg+xml">'));
  check(`${page} の apple-touch-icon が /pwa/icon-180.png`, h.includes('<link rel="apple-touch-icon" href="/pwa/icon-180.png">'));
}
check('manifest: purpose any の PNG が 2 つ（192・512）、maskable が別エントリ、SVG は入れない',
  manifest.icons.filter(i => i.purpose === 'any').map(i => i.sizes).join() === '192x192,512x512' && manifest.icons.some(i => i.purpose === 'maskable') && manifest.icons.every(i => i.type === 'image/png'));
check('wrangler run_worker_first が .ico を除外（/favicon.ico を静的配信）', wr.includes('"!/*.ico"'));
check('public/favicon.svg が SVG', read('public/favicon.svg').startsWith('<svg '));
const ico = fs.readFileSync(root + 'public/favicon.ico');
const icoSizes = ico.readUInt16LE(0) === 0 && ico.readUInt16LE(2) === 1 ? Array.from({ length: ico.readUInt16LE(4) }, (_, i) => ico[6 + i * 16]) : [];
check('favicon.ico に 16・32・48 が入っている', icoSizes.join() === '16,32,48');
check('favicon.ico の各フレームが PNG で寸法が宣言と一致', icoSizes.length === 3 && icoSizes.every((s, i) => { const off = ico.readUInt32LE(6 + i * 16 + 12); return ico.readUInt32BE(off) === 0x89504e47 && ico.readUInt32BE(off + 16) === s && ico.readUInt32BE(off + 20) === s; }));

// maskable の安全領域: 背景色（四隅の色）と違う画素が、中央の半径 40% の円に収まること（PNG を自前で復号。8bit RGB/RGBA・非インターレース）
function decodePng(buf) {
  const w = buf.readUInt32BE(16), h = buf.readUInt32BE(20), type = buf[25], bpp = type === 6 ? 4 : 3;
  if (buf[24] !== 8 || (type !== 6 && type !== 2) || buf[28] !== 0) throw new Error('未対応の PNG 形式');
  const idat = []; for (let o = 8; o < buf.length;) { const n = buf.readUInt32BE(o); if (buf.toString('latin1', o + 4, o + 8) === 'IDAT') idat.push(buf.subarray(o + 8, o + 8 + n)); o += 12 + n; }
  const raw = zlib.inflateSync(Buffer.concat(idat)), stride = w * bpp, px = Buffer.alloc(h * stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], src = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? px[y * stride + x - bpp] : 0, b = y ? px[(y - 1) * stride + x] : 0, c = x >= bpp && y ? px[(y - 1) * stride + x - bpp] : 0;
      const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
      px[y * stride + x] = (src[x] + (f === 0 ? 0 : f === 1 ? a : f === 2 ? b : f === 3 ? (a + b) >> 1 : (pa <= pb && pa <= pc ? a : pb <= pc ? b : c))) & 255;
    }
  }
  return { w, h, bpp, px };
}
{
  const { w, h, bpp, px } = decodePng(fs.readFileSync(root + 'public/pwa/icon-maskable-512.png'));
  const at = (x, y) => [0, 1, 2].map(k => px[(y * w + x) * bpp + k]);
  const bg = at(0, 0); let maxR = 0, inside = 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const c = at(x, y);
    if (Math.abs(c[0] - bg[0]) + Math.abs(c[1] - bg[1]) + Math.abs(c[2] - bg[2]) > 24) { inside++; maxR = Math.max(maxR, Math.hypot(x + .5 - w / 2, y + .5 - h / 2)); }
  }
  check(`maskable-512: M が背景と違う画素で存在し、すべて中央 80%（半径 ${(w * .4).toFixed(1)}px）に収まる（最大 ${maxR.toFixed(1)}）`, inside > 5000 && maxR <= w * .4);
}
{
  // apple-touch-icon は不透明（iOS は透過を黒で塗るため）
  const { w, h, bpp, px } = decodePng(fs.readFileSync(root + 'public/pwa/icon-180.png'));
  let opaque = true;
  if (bpp === 4) for (let i = 0; i < w * h; i++) if (px[i * 4 + 3] !== 255) { opaque = false; break; }
  check('apple-touch-icon(icon-180.png) が不透明（アルファが全て 255）', opaque);
}
{
  // 16px のファビコン: M の上端の切れ込み（2 つの山の間）が上から 6 行とも中央に見えていること
  // （透明または濃色の輪郭の画素。縁・輪郭を太らせると切れ込みは透明でなく濃色の線として残る）
  const off = ico.readUInt32LE(6 + 12), len = ico.readUInt32LE(6 + 8);
  const { w, bpp, px } = decodePng(ico.subarray(off, off + len));
  const seen = y => bpp === 4 && [6, 7, 8, 9].some(x => { const o = (y * w + x) * 4; return px[o + 3] < 200 || px[o] + px[o + 1] + px[o + 2] < 250; });
  const rows = [0, 1, 2, 3, 4, 5].map(seen);
  check(`favicon.ico の 16px で M の上端の切れ込みが上から 6 行とも残る（${rows.map(v => (v ? 1 : 0)).join('')}）`, rows.every(Boolean));
}
process.exit(failed ? 1 : 0);
