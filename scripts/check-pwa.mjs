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
// .js は静的配信の既定型（text/javascript）になるので sw.js には型指定を足さない（足すなら検査も変える）
const wr = read('wrangler.jsonc');
check('wrangler run_worker_first が .webmanifest と .js を除外', wr.includes('"!/*.webmanifest"') && wr.includes('"!/*.js"'));

const csp = pageCsp([]);
check('CSP に manifest-src \'self\'', csp.includes("manifest-src 'self'"));
check('CSP に worker-src \'self\'', csp.includes("worker-src 'self'"));

const manifest = JSON.parse(read('public/manifest.webmanifest'));
const html = read('public/index.html');
check('index.html が manifest を参照', html.includes('<link rel="manifest" href="/manifest.webmanifest">'));
check('start_url と scope が /', manifest.start_url === '/' && manifest.scope === '/');
const lightMeta = html.match(/<meta name="theme-color" content="([^"]+)" media="\(prefers-color-scheme: light\)">/);
check('manifest.theme_color が index.html のライト theme-color と一致', !!lightMeta && lightMeta[1].toLowerCase() === manifest.theme_color.toLowerCase());
for (const icon of manifest.icons) {
  const f = root + 'public' + icon.src;
  let ok = false;
  if (fs.existsSync(f)) { const b = fs.readFileSync(f); ok = b.readUInt32BE(16) + 'x' + b.readUInt32BE(20) === icon.sizes; }
  check(`アイコン ${icon.src} が存在し sizes(${icon.sizes}) と PNG の寸法が一致`, ok);
}
check('apple-touch-icon が 180px の PNG', (() => { const m = html.match(/rel="apple-touch-icon" href="([^"]+)"/); if (!m || !fs.existsSync(root + 'public' + m[1])) return false; const b = fs.readFileSync(root + 'public' + m[1]); return b.readUInt32BE(16) === 180 && b.readUInt32BE(20) === 180; })());
process.exit(failed ? 1 : 0);
