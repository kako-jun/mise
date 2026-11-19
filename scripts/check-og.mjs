#!/usr/bin/env node
// 自己検査（外部アクセスなし）: OGP 画像が実在し 1200x630 の PNG で、index.html の meta が指す URL が public/ の実ファイルに対応すること。
// 実行: node scripts/check-og.mjs
import fs from 'node:fs';
const root = new URL('../', import.meta.url).pathname;
const html = fs.readFileSync(root + 'public/index.html', 'utf8');
let failed = 0;
const check = (label, ok) => { console.log(`${ok ? 'OK  ' : 'NG  '}${label}`); if (!ok) failed++; };
const meta = (attr, name) => html.match(new RegExp(`<meta ${attr}="${name}" content="([^"]*)"`))?.[1];
const ORIGIN = 'https://mise.llll-ll.com';

const ogImage = meta('property', 'og:image'), twImage = meta('name', 'twitter:image');
check('og:image が絶対 URL（https://mise.llll-ll.com/…）', !!ogImage && ogImage.startsWith(ORIGIN + '/'));
check('twitter:image が og:image と同じ', !!twImage && twImage === ogImage);
check('twitter:card が summary_large_image', meta('name', 'twitter:card') === 'summary_large_image');
check('og:image:type が image/png', meta('property', 'og:image:type') === 'image/png');
check('og:image:alt が空でない', !!meta('property', 'og:image:alt'));
check('twitter:image:alt が og:image:alt と同じ', !!meta('name', 'twitter:image:alt') && meta('name', 'twitter:image:alt') === meta('property', 'og:image:alt'));
const file = ogImage ? root + 'public' + ogImage.slice(ORIGIN.length) : null;
let w = 0, h = 0, isPng = false;
if (file && fs.existsSync(file)) { const b = fs.readFileSync(file); isPng = b.subarray(1, 4).toString() === 'PNG'; w = b.readUInt32BE(16); h = b.readUInt32BE(20); }
check('og:image の URL が public/ の実在する PNG を指す', isPng);
check('PNG の寸法が 1200x630', w === 1200 && h === 630);
check('og:image:width/height が PNG の寸法と一致', meta('property', 'og:image:width') === String(w) && meta('property', 'og:image:height') === String(h));
check('SVG 正本（public/og/mise-og.svg）が存在', fs.existsSync(root + 'public/og/mise-og.svg'));
const wr = fs.readFileSync(root + 'wrangler.jsonc', 'utf8');
check('wrangler run_worker_first が .png を除外（静的配信）', wr.includes('"!/*.png"'));
process.exit(failed ? 1 : 0);
