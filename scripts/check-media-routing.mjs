#!/usr/bin/env node
// 自己検査（外部アクセスなし）: /icon/*.webp・/thumb/*.webp が Worker に届く構成か。
// Worker に ASSETS へのフォールバックは無いので、run_worker_first に `!/*.webp` があると /icon/・/thumb/<hash>.webp が静的ファイル扱いになり
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
// 否定パターン（!で始まる）のどれかが /icon/<hash>.webp に当たると、静的ファイル扱いになって Worker に届かない（* は / も含めて任意の文字列）。
const patterns = [...rwf.matchAll(/"([^"]*)"/g)].map(m => m[1]);
const globRe = g => new RegExp('^' + g.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$');
for (const sample of ['/icon/a1b2c3d4e5f60718293a4b5c6d7e8f90.webp', '/thumb/a1b2c3d4e5f60718293a4b5c6d7e8f90.webp']) {
  const hit = patterns.filter(p => p.startsWith('!') && globRe(p.slice(1)).test(sample));
  check(`run_worker_first の除外（! パターン）が ${sample.replace(/[0-9a-f]{32}/, '<hash>')} に当たらない（Worker に届ける）` + (hit.length ? `: ${hit.join(', ')}` : ''), hit.length === 0);
}
check('public/ 以下に .webp が無い', walk(root + 'public').filter(f => /\.webp$/i.test(f)).length === 0);
if (failed) process.exit(1);
