// PLAYWRIGHT_DIR=/path/to/playwright CHROMIUM_PATH=/path/to/chrome node tests/feed-navigation.browser.mjs
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_DIR || 'playwright-core');
const root=path.resolve('public'); const requests=[];
const server=createServer(async(req,res)=>{
 const u=new URL(req.url,'http://localhost');
 if(u.pathname==='/api/categories'){res.setHeader('content-type','application/json');res.end('[]');return;}
 if(u.pathname==='/api/sites'){
  requests.push(u.searchParams.toString());const cursor=Number(u.searchParams.get('cursor')||0);const limit=Number(u.searchParams.get('limit')||40);const first=cursor || (Number(u.searchParams.get('page')||1)-1)*40;
  const items=Array.from({length:limit},(_,i)=>({id:'site'+(first+i+1),name:'Site '+(first+i+1),url:'https://example.com/'+(first+i+1),country_code:'JP',category_ids:[]}));
  res.setHeader('content-type','application/json');res.end(JSON.stringify({items,total:1000,total_capped:false,next_cursor:String(first+limit)}));return;
 }
 if(u.pathname.startsWith('/api/')){res.setHeader('content-type','application/json');res.end('{}');return;}
 const file=u.pathname==='/'||u.pathname==='/jp/'?'index.html':u.pathname.slice(1), filename=path.resolve(root,file);
 if(!filename.startsWith(root+path.sep)){res.writeHead(403);res.end();return;}
 try{const data=await readFile(filename);res.setHeader('content-type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)]||'application/octet-stream'));res.end(data);}catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;
try{
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:844}}),base=`http://127.0.0.1:${server.address().port}`;
 await page.goto(base+'/jp/?q=cat&tag=art&page=2&view=feed');
 await page.waitForSelector('#feed .f-card[data-id]');
 const first=new URLSearchParams(requests[0]);assert.equal(first.get('q'),'cat');assert.deepEqual(first.getAll('tag'),['art']);assert.equal(first.get('page'),'2');assert.equal(first.get('limit'),'40');
 await page.waitForFunction(()=>document.querySelectorAll('#feed .f-card[data-id]').length===500);
 const back=new URL(await page.locator('.f-directory').getAttribute('href'),base);assert.equal(back.searchParams.get('q'),'cat');assert.equal(back.searchParams.get('tag'),'art');assert.equal(back.searchParams.get('page'),'2');
 const more=new URL(await page.locator('.f-continue').getAttribute('href'),base);assert.equal(more.searchParams.get('q'),'cat');assert.equal(more.searchParams.get('tag'),'art');assert.equal(more.searchParams.get('page'),'14');assert.equal(more.hash,'#site-row-541');
 await page.goto(base+'/jp/?q=cat&tag=art');
 await page.waitForSelector('#feed-entry:not([hidden])');
 const entry=page.locator('#feed-entry');
 const entryUrl=new URL(await entry.getAttribute('href'),base);assert.equal(entryUrl.searchParams.get('q'),'cat');assert.equal(entryUrl.searchParams.get('tag'),'art');assert.equal(entryUrl.searchParams.get('view'),'feed');
 await page.evaluate(()=>{const spacer=document.createElement('div');spacer.style.height='3000px';document.body.append(spacer);window.scrollTo(0,1500);});
 await page.waitForFunction(()=>!document.querySelector('#back-to-top').hidden);
 const rects=await page.evaluate(()=>{const a=document.querySelector('#feed-entry').getBoundingClientRect(),b=document.querySelector('#back-to-top').getBoundingClientRect();return {entryTop:a.top,backBottom:b.bottom,entryRight:a.right,backRight:b.right};});
 assert.ok(rects.backBottom<=rects.entryTop,'右下の2ボタンが重ならない');assert.equal(rects.entryRight,rects.backRight);
 await page.locator('#back-to-top').click();await page.waitForFunction(()=>scrollY===0);assert.equal(await page.evaluate(()=>document.activeElement.id),'query');
 console.log('feed navigation browser regression: passed');
}finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
