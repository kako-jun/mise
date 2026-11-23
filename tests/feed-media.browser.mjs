// 実ブラウザ回帰: PLAYWRIGHT_DIR=/path/to/playwright CHROMIUM_PATH=/path/to/chrome node tests/feed-media.browser.mjs
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright-core');
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII=', 'base64');
const items = [
  { id:'missing', name_ja:'Missing', description_ja:'No image', url:'https://example.com/missing', country_code:'JP', category_ids:[], registered_at:'2026-11-03', thumb_url:null },
  { id:'broken', name_ja:'Broken', description_ja:'Broken image', url:'https://example.com/broken', country_code:'JP', category_ids:[], registered_at:'2026-11-02', thumb_url:'/thumb/broken.webp', thumb_lqip:'data:image/png;base64,' + png.toString('base64') },
  { id:'valid', name_ja:'Valid', description_ja:'Valid image', url:'https://example.com/valid', country_code:'JP', category_ids:[], registered_at:'2026-11-01', thumb_url:'/thumb/valid.webp' }
];
const root = path.resolve('public');
const server = createServer(async (req,res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/api/categories') { res.setHeader('Content-Type','application/json'); res.end('[]'); return; }
  if (url.pathname === '/api/sites') { res.setHeader('Content-Type','application/json'); res.end(JSON.stringify({items,next_cursor:null})); return; }
  if (url.pathname === '/thumb/broken.webp') { res.writeHead(404); res.end(); return; }
  if (url.pathname === '/thumb/valid.webp') { res.setHeader('Content-Type','image/png'); res.end(png); return; }
  const file = url.pathname === '/' || url.pathname === '/jp/' ? 'index.html' : url.pathname.slice(1);
  const filePath = path.resolve(root,file);
  if (!filePath.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
  try { const data = await readFile(filePath); const ext = path.extname(file); res.setHeader('Content-Type', ({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[ext] || 'application/octet-stream')); res.end(data); }
  catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
let browser;
try {
  browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH, args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:390,height:844}});
  await page.goto(`http://127.0.0.1:${server.address().port}/jp/?view=feed&sort=new`);
  await page.waitForSelector('#feed .f-card[data-id="valid"]');
  const cards = page.locator('#feed .f-card[data-id]');
  assert.equal(await cards.count(),3);
  for (const id of ['missing','broken']) {
    const card = page.locator(`#feed .f-card[data-id="${id}"]`);
    if (id === 'broken') await page.waitForFunction(() => [...document.querySelectorAll('#feed .f-card[data-id="broken"] .f-img')].length === 0);
    assert.equal(await card.locator('.f-art img, .f-art svg').count(),0, id + ': 画像と代替模様は表示しない');
    assert.equal(await card.locator('.f-open').getAttribute('href'),`https://example.com/${id}`);
    assert.ok(await card.locator('.f-desc').isVisible());
  }
  const valid = page.locator('#feed .f-card[data-id="valid"]');
  await valid.locator('.f-hero img.f-img').waitFor({state:'attached'});
  await page.waitForFunction(() => document.querySelector('#feed .f-card[data-id="valid"] .f-hero img')?.naturalWidth > 0);
  assert.equal(await valid.locator('.f-art img.f-img').count(),2);
  assert.equal(await page.locator('#feed .calico').count(),0);
  await page.locator('#f-next').click();
  await page.waitForFunction(() => document.querySelector('#feed .f-card[data-id="broken"]')?.classList.contains('is-active'));
  const pan = await page.locator('#feed .f-card[data-id="broken"] .f-hero').evaluate(el => [el.style.getPropertyValue('--pan-w'),el.style.getPropertyValue('--pan-px')]);
  assert.deepEqual(pan,['','']);
  await page.locator('#f-next').click();
  await page.waitForFunction(() => document.querySelector('#feed .f-card[data-id="valid"]')?.classList.contains('is-active'));
  await page.locator('#f-prev').click();
  await page.waitForFunction(() => document.querySelector('#feed .f-card[data-id="broken"]')?.classList.contains('is-active'));
  assert.equal(await page.locator('#feed .f-card[data-id="broken"] .f-art img, #feed .f-card[data-id="broken"] .f-art svg').count(),0);
  console.log('feed media browser regression: passed');
} finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
