// PLAYWRIGHT_DIR=/path/to/playwright CHROMIUM_PATH=/path/to/chromium node tests/direct-count.browser.mjs
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_DIR || 'playwright-core');
const root = path.resolve('public');
const categories = [
  { id:'parent', parent_id:null, name_ja:'親', name_en:'Parent', site_count:7 },
  { id:'child', parent_id:'parent', name_ja:'子', name_en:'Child', site_count:2 }
];
let pendingInitial, pendingChild, childAttempt = 0;
const respond = (res, total) => { if (!res.destroyed) { res.setHeader('Content-Type','application/json'); res.end(JSON.stringify({items:[],total,total_capped:false,next_cursor:null})); } };
const server = createServer(async (req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (url.pathname === '/api/categories') { res.setHeader('Content-Type','application/json'); res.end(JSON.stringify(categories)); return; }
  if (url.pathname === '/api/sites') {
    if (url.searchParams.get('category') === 'parent') { pendingInitial = res; return; }
    if (url.searchParams.get('category') === 'child') { if (++childAttempt === 1) pendingChild = res; else respond(res,2); return; }
    respond(res,0); return;
  }
  if (url.pathname.startsWith('/api/')) { res.setHeader('Content-Type','application/json'); res.end('{}'); return; }
  const file = url.pathname === '/jp/' ? 'index.html' : url.pathname.slice(1);
  const filename = path.resolve(root,file);
  if (!filename.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
  try {
    let data = await readFile(filename);
    if (file === 'index.html') data = Buffer.from(data.toString().replace('id="sample-label" class="directory-empty" hidden', 'id="sample-label" class="directory-empty"').replace('<span id="sample-count"></span>', '<span id="sample-count">(7)</span>'));
    res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)] || 'application/octet-stream')); res.end(data);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
let browser;
try {
  browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:390,height:844}});
  await page.goto(`http://127.0.0.1:${server.address().port}/jp/?category=parent`, {waitUntil:'domcontentloaded'});
  await page.waitForFunction(() => document.querySelector('#categories a[href*="category=child"]'));
  assert.equal(await page.locator('#sample-count').textContent(), '(7)');
  assert.equal(await page.locator('#sample-label').isVisible(), true, 'SSR count stays visible during initial request');
  respond(pendingInitial,7);
  await page.locator('#categories a[href*="category=child"]').click();
  await page.waitForFunction(() => location.search.includes('category=child'));
  await page.waitForFunction(() => document.querySelector('#sample-label').hidden);
  assert.equal(await page.locator('#sample-label').isVisible(), false, 'old parent count hidden while child loads');
  await new Promise(resolve => { const check = () => pendingChild ? resolve() : setTimeout(check,10); check(); });
  pendingChild.writeHead(500); pendingChild.end();
  await page.locator('#directory-foot button').waitFor({state:'visible'});
  assert.equal(await page.locator('#sample-label').isVisible(), false, 'failure does not reveal stale count');
  await page.locator('#directory-foot button').click();
  await page.waitForFunction(() => !document.querySelector('#sample-label').hidden && document.querySelector('#sample-count').textContent === '(2)');
  await page.locator('#language-toggle').click();
  assert.equal(await page.locator('#sample-label').isVisible(), true, 'language change preserves the loaded count');
  assert.equal(await page.locator('#sample-count').textContent(), '(2)');
  console.log('direct count browser regression: passed');
} finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
