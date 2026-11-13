#!/usr/bin/env node
// デプロイ後の CSP 確認。実サイトへ数リクエスト（トップ・範囲・カテゴリ・検索の4ページ）だけ送り、CSP 違反の件数を出す。
// 実行: PLAYWRIGHT_DIR=<playwright の入った node_modules> [CHROMIUM=<chrome 実行ファイル>] node scripts/check-prod-csp.mjs [URL]
// URL 省略時は本番。ローカル確認は http://localhost:8787 など。手順は docs/operations/deploy.md。
// 終了コード: 違反 0 件なら 0、1 件以上または CSP ヘッダなしなら 1。
import { createRequire } from 'node:module';
const base = (process.argv[2] || 'https://mise.llll-ll.com').replace(/\/$/, '');
const dir = process.env.PLAYWRIGHT_DIR;
const { chromium } = createRequire(dir ? dir.replace(/\/?$/, '/') : import.meta.url)('playwright');
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined, args: ['--use-gl=swiftshader', '--enable-unsafe-swiftshader', '--no-sandbox'] });
let total = 0, missing = 0;
for (const path of ['/', '/us/', '/all/?category=science', '/jp/?q=a']) {
  const page = await (await browser.newContext({ viewport: { width: 1300, height: 900 } })).newPage();
  const found = [];
  await page.addInitScript(() => document.addEventListener('securitypolicyviolation', e => (window.__cspv = window.__cspv || []).push(`${e.violatedDirective} ${e.blockedURI} ${(e.sample || '').slice(0, 60)}`)));
  page.on('console', m => { if (/Content Security Policy|Refused to/i.test(m.text())) found.push('console: ' + m.text().slice(0, 200)); });
  const res = await page.goto(base + path, { waitUntil: 'load' });
  await page.waitForTimeout(3500);
  const csp = res.headers()['content-security-policy'] || res.headers()['content-security-policy-report-only'];
  if (!csp) missing++;
  found.push(...(await page.evaluate(() => window.__cspv || [])));
  const logo = await page.evaluate(() => document.documentElement.classList.contains('logo-ready'));
  const canvas = await page.evaluate(() => !!document.querySelector('#globe-background canvas'));
  const inline = await page.evaluate(() => [...document.scripts].filter(s => !s.src && s.type !== 'application/ld+json').length);
  console.log(`${base + path}\n  CSP header: ${csp ? 'あり' : 'なし'} / logo-ready: ${logo} / globe canvas: ${canvas} / inline scripts: ${inline} / 違反: ${found.length}`);
  found.forEach(f => console.log('    - ' + f));
  total += found.length;
  await page.context().close();
}
await browser.close();
console.log(`CSP 違反の合計: ${total}${missing ? `（CSP ヘッダなしのページ ${missing}）` : ''}`);
process.exit(total || missing ? 1 : 0);
