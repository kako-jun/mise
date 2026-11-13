#!/usr/bin/env node
// 自己検査（外部アクセスなし）: PWA 関連の配信設定が揃っていること。静的ファイルは Worker を通らないので、
// public/_headers と wrangler.jsonc の設定そのものを読んで確かめる。
// 実行: node scripts/check-pwa.mjs
import fs from 'node:fs';
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
process.exit(failed ? 1 : 0);
