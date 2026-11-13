#!/usr/bin/env node
// public/pwa/mise-icon.svg（文字でなく path。フォント非依存）から PNG を書き出す。生成物は commit する。
// 実行: node scripts/build-pwa-icons.mjs（playwright と Chromium が必要。package.json の依存には入れていない）
import fs from 'node:fs';
const { chromium } = await import('playwright');
const dir = new URL('../public/pwa/', import.meta.url).pathname;
const svg = fs.readFileSync(dir + 'mise-icon.svg', 'utf8');
// maskable は外周 1/5 が切り抜かれうるので M を中央 76% に縮める
const maskable = svg.replace(/<path /, '<path transform="translate(256 256) scale(.76) translate(-256 -256)" ');
const browser = await chromium.launch();
for (const [name, s, size] of [['icon-180.png', svg, 180], ['icon-192.png', svg, 192], ['icon-512.png', svg, 512], ['icon-maskable-512.png', maskable, 512]]) {
  const page = await browser.newPage({ viewport: { width: size, height: size } });
  await page.setContent(`<body style="margin:0">${s.replace('<svg ', `<svg width="${size}" height="${size}" `)}</body>`);
  await page.screenshot({ path: dir + name });
}
await browser.close();
