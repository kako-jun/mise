// PLAYWRIGHT_DIR=/path/to/playwright CHROMIUM_PATH=/path/to/chrome node tests/feed-open.browser.mjs
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_DIR || 'playwright-core');
const items = [
  { id:'first', name_ja:'一番', name_en:'First', url:'https://example.com/first' },
  { id:'second', name_ja:'二番', name_en:'Second', url:'https://example.org/second' },
  ...['javascript:alert(1)','data:text/html,hello','/relative','not a url'].map((url,i)=>({id:`unsafe${i}`,name_ja:`不正${i}`,name_en:`Unsafe${i}`,url}))
].map(s=>({...s,description_ja:'説明',description_en:'Description',country_code:'JP',category_ids:[],registered_at:'2026-11-01'}));
const root=path.resolve('public');
const server=createServer(async(req,res)=>{
  const u=new URL(req.url,'http://localhost');
  if(u.pathname==='/api/categories') {res.setHeader('Content-Type','application/json');res.end('[]');return;}
  if(u.pathname==='/api/sites') {res.setHeader('Content-Type','application/json');res.end(JSON.stringify({items,next_cursor:null}));return;}
  const file=u.pathname==='/'||u.pathname==='/jp/'?'index.html':u.pathname.slice(1), full=path.resolve(root,file);
  if(!full.startsWith(root+path.sep)){res.writeHead(403).end();return;}
  try {const data=await readFile(full);res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)]||'application/octet-stream'));res.end(data);} catch {res.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
try {
  browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']});
  for(const lang of ['ja','en']) for(const pref of ['false','true']) {
    const context=await browser.newContext({viewport:{width:390,height:844}});
    await context.addInitScript(({lang,pref})=>{localStorage.setItem('mise-language',lang);localStorage.setItem('mise-external-new-tab',pref);},{lang,pref});
    const page=await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/jp/?view=feed&sort=new`);
    await page.waitForSelector('#feed .f-card[data-id="second"]');
    for(const id of ['first','second']) {
      const card=page.locator(`#feed .f-card[data-id="${id}"]`), main=card.locator('.f-open'), other=card.locator('.f-open-new');
      const expected=id==='first'?'https://example.com/first':'https://example.org/second';
      assert.equal(await main.getAttribute('href'),expected); assert.equal(await other.getAttribute('href'),expected);
      assert.equal(await main.getAttribute('target'),pref==='true'?'_blank':null);
      assert.equal(await other.getAttribute('target'),'_blank'); assert.match(await other.getAttribute('rel'),/noopener/); assert.match(await other.getAttribute('rel'),/noreferrer/);
      assert.match(await main.getAttribute('aria-label'),new RegExp(lang==='ja'?(id==='first'?'一番':'二番'):(id==='first'?'First':'Second')));
      assert.ok(await other.getAttribute('aria-label'));
    }
    for(let i=0;i<4;i++) assert.equal(await page.locator(`#feed .f-card[data-id="unsafe${i}"] .f-open, #feed .f-card[data-id="unsafe${i}"] .f-open-new`).count(),0);
    await page.locator('#f-next').click(); await page.waitForFunction(()=>document.querySelector('#feed .f-card[data-id="second"]')?.classList.contains('is-active'));
    const link=page.locator('#feed .f-card[data-id="second"] .f-open-new'); await link.focus(); assert.equal(await link.evaluate(el=>document.activeElement===el),true);
    const boxes=await page.locator('#feed .f-card[data-id="second"] .f-open, #feed .f-card[data-id="second"] .f-open-new').evaluateAll(els=>els.map(e=>{const r=e.getBoundingClientRect();return {x:r.x,right:r.right,width:r.width,height:r.height};}));
    assert.ok(boxes[0].right<=boxes[1].x && boxes.every(b=>b.width>0&&b.height>0));
    await context.close();
  }
  console.log('feed open browser regression: passed');
} finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
