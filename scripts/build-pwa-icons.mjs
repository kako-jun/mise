#!/usr/bin/env node
// サイト自身のアイコン一式（タブのファビコン・apple-touch-icon・PWA アイコン）を書き出す。生成物は commit する（#151）。
// 意匠: Anton の M を三毛猫のパッチ（橙・クリーム・茶）で塗り、外側にクリーム色の太い縁、その内側に濃色の輪郭（ステッカー風）。
// M は文字でなく path（ロゴと同じ自己ホスト Anton サブセットのグリフ輪郭。フォント非依存）。
// 出力: public/favicon.svg（小サイズ向けに縁・輪郭を細くした版）/ public/favicon.ico（16・32 は小サイズ版、48 は通常版）/
//       public/pwa/icon-180.png（濃色地。iOS は透過を黒で塗るため）/ icon-192.png / icon-512.png（透過）/ icon-maskable-512.png（濃色地）
// 実行: node scripts/build-pwa-icons.mjs（playwright と Chromium が必要。package.json の依存には入れていない。
//   別の場所にある場合は PLAYWRIGHT_MODULE=<playwright の index.js> CHROMIUM_PATH=<chrome 実行ファイル> で指す）
import fs from 'node:fs';

const root = new URL('../', import.meta.url).pathname;
const M = 'M78 0V1760H614L762 687L909 1760H1450V0H1128V1268L926 0H610L396 1268V0Z'; // グリフ座標（高さ 1760、中心 x=764）
const GH = 1760;
const DARK = '#202831', CREAM = '#fff8ea';
const GRAD = '<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ee8a3c"/><stop offset=".30" stop-color="#ee8a3c"/><stop offset=".31" stop-color="#fff1d6"/><stop offset=".57" stop-color="#fff1d6"/><stop offset=".58" stop-color="#8a5a37"/><stop offset=".72" stop-color="#8a5a37"/><stop offset=".73" stop-color="#ee8a3c"/><stop offset="1" stop-color="#ee8a3c"/></linearGradient>';

// h: 512 キャンバス上での M の高さ。rim / line: 縁と輪郭の太さ（512 キャンバス上の px。パスを中心に両側へ半分ずつ広がる）
function icon({ h, rim, line, bg = null, rx = 0 }) {
  const s = h / GH, tx = 256 - s * 764, ty = 256 + h / 2;
  const t = `transform="translate(${tx.toFixed(2)} ${ty.toFixed(2)}) scale(${s.toFixed(5)} ${(-s).toFixed(5)})"`;
  const w = px => (px / s).toFixed(1);
  const p = extra => `<path d="${M}" ${t} ${extra}/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs>${GRAD}</defs>`
    + (bg ? `<rect width="512" height="512" ${rx ? `rx="${rx}" ` : ''}fill="${bg}"/>` : '')
    + p(`fill="${CREAM}" stroke="${CREAM}" stroke-width="${w(rim)}" stroke-linejoin="round"`)
    + p(`fill="${DARK}" stroke="${DARK}" stroke-width="${w(line)}" stroke-linejoin="round"`)
    + p('fill="url(#g)"') + '</svg>\n';
}
// 通常版（180/192/512）と、16/32px で M の内側の切れ込みを残すため縁・輪郭を細くして M を大きくした小サイズ版
const NORMAL = { h: 420, rim: 44, line: 22 };
const SMALL = { h: 468, rim: 20, line: 8 };
const designs = {
  small: icon(SMALL),
  large: icon(NORMAL),
  apple: icon({ h: 360, rim: 38, line: 19, bg: DARK }),
  maskable: icon({ h: 280, rim: 29, line: 15, bg: DARK }),
  maskableContent: icon({ h: 280, rim: 29, line: 15 }), // 安全領域の実測用（背景なし）
};

fs.writeFileSync(root + 'public/favicon.svg', designs.small);

const PW = process.env.PLAYWRIGHT_MODULE;
const { chromium } = PW ? (await import(PW)).default : await import('playwright');
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox', '--disable-gpu'] } : {});
const page = await browser.newPage({ viewport: { width: 512, height: 512 }, deviceScaleFactor: 1 });
async function render(svg, size) {
  const uri = 'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64');
  await page.setContent(`<body style="margin:0;background:transparent"><img src="${uri}" width="${size}" height="${size}" style="display:block"></body>`);
  await page.waitForFunction(() => document.images[0].complete);
  return page.screenshot({ clip: { x: 0, y: 0, width: size, height: size }, omitBackground: true });
}
const out = (name, buf) => fs.writeFileSync(root + 'public/pwa/' + name, buf);
out('icon-180.png', await render(designs.apple, 180));
out('icon-192.png', await render(designs.large, 192));
out('icon-512.png', await render(designs.large, 512));
out('icon-maskable-512.png', await render(designs.maskable, 512));

// 安全領域: maskable は中央の半径 40%（512 なら 204.8px）の円に M と縁が収まること（背景なしで描いて実測）
await page.setContent(`<body style="margin:0"><img id="i" src="data:image/svg+xml;base64,${Buffer.from(designs.maskableContent).toString('base64')}" width="512" height="512"></body>`);
await page.waitForFunction(() => document.images[0].complete);
const maxR = await page.evaluate(() => {
  const c = document.createElement('canvas'); c.width = c.height = 512;
  const x = c.getContext('2d'); x.drawImage(document.getElementById('i'), 0, 0);
  const d = x.getImageData(0, 0, 512, 512).data; let m = 0;
  for (let y = 0; y < 512; y++) for (let xx = 0; xx < 512; xx++) if (d[(y * 512 + xx) * 4 + 3] > 8) m = Math.max(m, Math.hypot(xx + .5 - 256, y + .5 - 256));
  return m;
});
console.log(`maskable 安全領域: 最大半径 ${maxR.toFixed(1)} / 上限 204.8`);
if (maxR > 204.8) throw new Error('maskable の内容が安全領域を超えている');

// ICO: PNG 埋め込み形式（Vista 以降・全モダンブラウザで可）。16/32 は小サイズ版、48 は通常版
const frames = [[16, await render(designs.small, 16)], [32, await render(designs.small, 32)], [48, await render(designs.large, 48)]];
const head = Buffer.alloc(6); head.writeUInt16LE(1, 2); head.writeUInt16LE(frames.length, 4);
let offset = 6 + 16 * frames.length;
const dir = frames.map(([size, png]) => {
  const e = Buffer.alloc(16); e[0] = size; e[1] = size; e.writeUInt16LE(1, 4); e.writeUInt16LE(32, 6); e.writeUInt32LE(png.length, 8); e.writeUInt32LE(offset, 12);
  offset += png.length; return e;
});
fs.writeFileSync(root + 'public/favicon.ico', Buffer.concat([head, ...dir, ...frames.map(f => f[1])]));
await browser.close();
console.log('wrote favicon.svg favicon.ico pwa/icon-*.png');
