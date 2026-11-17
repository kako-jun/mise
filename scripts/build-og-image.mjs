#!/usr/bin/env node
// public/og/mise-og.svg（文字でなく path。フォント非依存。赤地に白い MISE、アイコンと同じ Anton の輪郭）から og.png（1200x630）を書き出す。生成物は commit する。
// 実行: node scripts/build-og-image.mjs（playwright と Chromium が必要。package.json の依存には入れていない）
import fs from 'node:fs';
const { chromium } = await import('playwright');
const dir = new URL('../public/og/', import.meta.url).pathname;
const svg = fs.readFileSync(dir + 'mise-og.svg', 'utf8');
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
await page.setContent(`<body style="margin:0">${svg.replace('<svg ', '<svg width="1200" height="630" ')}</body>`);
await page.screenshot({ path: dir + 'og.png' });
await browser.close();
