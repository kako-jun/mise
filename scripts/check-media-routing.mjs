#!/usr/bin/env node
// 自己検査（外部アクセスなし）: /icon/*.webp が Worker に届く構成か。
// Worker に ASSETS へのフォールバックは無いので、run_worker_first に `!/*.webp` があると /icon/<hash>.webp が静的ファイル扱いになり
// R2 に届かず空の 404 になる。public/ に .webp を置くときも同じ理由で 404 になるので、置かない（src/media.js・docs/design/architecture.md）。
// 実行: node scripts/check-media-routing.mjs
import fs from 'node:fs';
import path from 'node:path';
const root = new URL('../', import.meta.url).pathname;
let failed = 0;
const check = (label, ok) => { console.log(`${ok ? 'OK  ' : 'NG  '}${label}`); if (!ok) failed++; };
const walk = d => fs.readdirSync(d, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]);
const cfg = fs.readFileSync(root + 'wrangler.jsonc', 'utf8').replace(/^\s*\/\/.*$/gm, '');
const rwf = cfg.match(/"run_worker_first"\s*:\s*\[([^\]]*)\]/)?.[1] ?? '';
check('wrangler.jsonc の run_worker_first が読める', rwf.includes('"/*"'));
check('run_worker_first に !/*.webp が無い（/icon/*.webp を Worker に届ける）', !/"!\/\*\.webp"/.test(rwf));
check('public/ 以下に .webp が無い', walk(root + 'public').filter(f => /\.webp$/i.test(f)).length === 0);
if (failed) process.exit(1);
