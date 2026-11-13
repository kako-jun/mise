const $ = selector => document.querySelector(selector);
let categories = [], sites = [], currentPath = [], currentSearch = null, pickedIds = [], pickIndex = 0, picksVisible = false, picksHovered = false, activityStats = null;
const storage = { get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Preferences remain active for this page. */ } } };
let language = storage.get('mise-language') === 'ja' ? 'ja' : 'en';
let openExternalInNewTab = storage.get('mise-external-new-tab') === 'true';
const words = {
  ja: { tagline:'今だからこそ、ディレクトリ型検索エンジン。', fly:'地球から日本へ', earth:'地球に戻る', browse:'カテゴリから探す', recommendIntro:'サイトが見つからない場合は？', recommend:'推薦', preparing:'推薦機能は準備中です。', search:'検索', searchLabel:'サイトを検索', top:'トップ', sample:'掲載サイト', noSites:'このカテゴリにサイトはありません。', noResults:'該当するサイトはありません。', back:'カテゴリ一覧に戻る', all:'すべてのサイト', results:q=>`「${q}」の検索結果`, concept:'広告や大手サイト多めの検索に疲れた人へ。\nまだ知らない独立サイトを見つける場所を提供しています。', author:'作者のサイト', github:'作者のGitHub', log:'ACTIVITY', idle:'No activity yet', theme:'ダーク/ライト', lang:'English', error:'データを取得できませんでした。再読み込みしてください。', fallback:'', belongs:'カテゴリ', picks:'ピックアップ', crawled:'最終巡回', uncrawled:'巡回日不明', tags:'属性', externalOn:'外部リンクを別タブで開く: オン', externalOff:'外部リンクを別タブで開く: オフ', summaryTitle:'SITE INDEX', historyTitle:'KNOWN REGISTRATIONS / UTC', crawlUnknown:'CRAWL HISTORY: NOT RECORDED', historyCaveat:'EXCLUDES UNKNOWN DATES', totalLabel:'SITES', unknownLabel:'DATE UNKNOWN', unavailable:'UNAVAILABLE' },
  en: { tagline:'A directory search engine for today.', fly:'Fly to Japan', earth:'Back to Earth', browse:'Browse categories', recommendIntro:'Can’t find a site?', recommend:'Recommend it', preparing:'Site recommendations are coming soon.', search:'Search', searchLabel:'Search sites', top:'Home', sample:'Listed sites', noSites:'No sites in this category.', noResults:'No sites found.', back:'Back to categories', all:'All sites', results:q=>`Results for “${q}”`, concept:'For those tired of search results dominated by ads and major sites.\nDiscover independent websites you have yet to find.', author:'Creator’s website', github:'Creator on GitHub', log:'ACTIVITY', idle:'No activity yet', theme:'Dark / light', lang:'日本語', error:'Could not load data. Please reload.', fallback:' (Japanese)', belongs:'Categories', picks:'Featured sites', crawled:'Last crawled', uncrawled:'Crawl date unknown', tags:'Tags', externalOn:'Open external links in new tabs: on', externalOff:'Open external links in new tabs: off', summaryTitle:'SITE INDEX', historyTitle:'KNOWN REGISTRATIONS / UTC', crawlUnknown:'CRAWL HISTORY: NOT RECORDED', historyCaveat:'EXCLUDES UNKNOWN DATES', totalLabel:'SITES', unknownLabel:'DATE UNKNOWN', unavailable:'UNAVAILABLE' }
};
const t = key => words[language][key];
const siteDescription = site => language === 'en' && site.description_en ? site.description_en : site.description_ja + t('fallback');
const name = category => category[language === 'ja' ? 'name_ja' : 'name_en'];
const activity = [];
function logActivity(message) { activity.unshift(message); activity.splice(4); renderLog(); }
function renderLog() { $('#activity-entries').replaceChildren(...(activity.length ? activity : [t('idle')]).map(entry => { const line=document.createElement('div'); line.textContent=activity.length ? `> ${entry}` : entry; return line; })); }
function updateTitle() {
  const cur = currentPath.map(id => categories.find(x => x.id === id)).filter(Boolean);
  document.title = cur.length ? `${cur.map(name).join(' › ')} — MISE` : `MISE — ${t('tagline')}`;
}
function applyLanguage() {
  document.documentElement.lang = language;
  updateTitle();
  document.querySelectorAll('[data-i18n]').forEach(el => { const value=t(el.dataset.i18n); if (value) el.textContent=value; });
  $('#query').setAttribute('aria-label',t('searchLabel'));
  updateExternalToggle();
  $('#language-toggle').title=t('lang'); $('#language-toggle').setAttribute('aria-label',t('lang'));
  renderLog();
  renderStats();
  if (currentSearch !== null) showResults(currentSearch); else showDirectory(currentPath, false);
}
function configureExternalLink(a) {
  const url=new URL(a.href,location.href);
  if(!['http:','https:'].includes(url.protocol) || url.origin===location.origin) return;
  if(openExternalInNewTab) { a.target='_blank'; a.rel='noopener noreferrer'; }
  else { a.removeAttribute('target'); a.removeAttribute('rel'); }
}
function updateExternalToggle() {
  const button=$('#external-toggle');
  button.setAttribute('aria-pressed',String(openExternalInNewTab));
  button.setAttribute('aria-label',t(openExternalInNewTab?'externalOn':'externalOff'));
  button.title=button.getAttribute('aria-label');
  document.querySelectorAll('a[href]').forEach(configureExternalLink);
}
$('#external-toggle').addEventListener('click',()=>{
  openExternalInNewTab=!openExternalInNewTab;
  storage.set('mise-external-new-tab',String(openExternalInNewTab));
  updateExternalToggle();
});
$('#language-toggle').addEventListener('click', () => { language=language==='ja'?'en':'ja'; storage.set('mise-language',language); applyLanguage(); });
$('#theme-toggle').addEventListener('click', () => { const theme=document.documentElement.dataset.theme==='dark'?'light':'dark'; document.documentElement.dataset.theme=theme; storage.set('mise-theme',theme); });
window.addEventListener('mise-fly-japan',()=>logActivity('fly japan'));


function renderStats() {
  $('#site-total').textContent = `${t('totalLabel')}: ${activityStats ? activityStats.total : t('unavailable')}`;
  $('#unknown-dates').textContent = `${t('unknownLabel')}: ${activityStats ? activityStats.unknown_dates : t('unavailable')}`;
  const bars=$('#history-bars'); bars.replaceChildren();
  if(!activityStats) { bars.textContent='UNAVAILABLE'; return; }
  const days=new Map(activityStats.known_daily.map(d=>[d.day,d.count]));
  bars.style.setProperty('--count-digits', String(Math.max(1,...Array.from(days.values(),n=>String(n).length))));
  for(let i=6;i>=0;i--) {
    const date=new Date(); date.setUTCHours(0,0,0,0); date.setUTCDate(date.getUTCDate()-i);
    const day=date.toISOString().slice(0,10), count=days.get(day)||0;
    const item=document.createElement('div'); item.className='history-day'; item.setAttribute('aria-label',`${day}: ${count}`);
    const label=document.createElement('span'); label.className='history-date'; label.textContent=date.toLocaleDateString('en-US',{weekday:'short',timeZone:'UTC'});
    const squares=document.createElement('span'); squares.className='history-squares'; squares.setAttribute('aria-hidden','true');
    for(let n=0;n<count;n++){ const square=document.createElement('span'); square.className='history-square'; squares.append(square); }
    const number=document.createElement('span'); number.className='history-count'; number.textContent=String(count);
    item.append(label,number,squares); bars.append(item);
  }
}
function categoryUrl(path) { const u=new URL(location.href); u.search=''; u.hash=''; path.forEach(id=>u.searchParams.append('category',id)); return u.pathname+u.search; }
function link(label, path, callback) {
  const a=document.createElement('a'); a.href=categoryUrl(path); a.textContent=label;
  a.addEventListener('click', e=>{ if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); callback(); });
  return a;
}
function categoryPath(id) {
  const path=[]; let node=categories.find(c=>c.id===id);
  while(node) { path.unshift(node.id); node=categories.find(c=>c.id===node.parent_id); }
  return path;
}
function renderSite(target, site, showCategories=false) {
  const li=document.createElement('li'), a=document.createElement('a'), p=document.createElement('p');
  a.href=site.url; configureExternalLink(a); a.textContent=site.name; p.textContent=siteDescription(site);
  const icon=document.createElement('span'); icon.className='site-icon'; icon.setAttribute('aria-hidden','true'); icon.hidden=true;
  // Only serve previously collected icons from this origin; never probe third-party sites on page load.
  if(site.favicon_path && /^\/icons\/[a-zA-Z0-9._-]+$/.test(site.favicon_path)) {
    const img=document.createElement('img'); img.alt=''; img.src=site.favicon_path;
    img.addEventListener('load',()=>{icon.hidden=false;}); img.addEventListener('error',()=>{icon.hidden=true;icon.replaceChildren();}); icon.replaceChildren(img);
  }
  const content=document.createElement('div'); content.className='site-content'; content.append(a);
  const registered=Date.parse(site.registered_at), age=Date.now()-registered;
  if(Number.isFinite(registered) && age>=0 && age<30*24*60*60*1000) { const tag=document.createElement('span'); tag.className='new-tag'; tag.textContent=' NEW'; content.append(tag); }
  content.append(p);
  const meta=document.createElement('p'); meta.className='site-meta';
  const crawlDate=site.last_crawled_at && !Number.isNaN(Date.parse(site.last_crawled_at)) ? site.last_crawled_at.slice(0,10) : null;
  meta.textContent=crawlDate ? `${t('crawled')}: ${crawlDate}` : '';
  let tags=[]; try { tags=JSON.parse(site.tags_json || '[]'); } catch (_) { tags=[]; }
  for(const tag of tags) { const span=document.createElement('span'); span.className='site-tag'; span.textContent=(language==='en' && tag.en) ? tag.en : tag.ja; meta.append(' ',span); }
  if(meta.textContent.trim()) content.append(meta); li.append(icon,content);
  if(showCategories) {
    const row=document.createElement('p'); row.className='site-categories'; row.append(`${t('belongs')}: `);
    const ids=[...new Set(sites.filter(s=>s.id===site.id).map(s=>s.category_id))];
    ids.forEach((id,index)=>{
      if(index) row.append(' / ');
      const path=categoryPath(id), text=path.map(part=>name(categories.find(c=>c.id===part))).join(' › ');
      row.append(link(text,path,()=>{logActivity(`cd /${path.join('/')}`);showDirectory(path);}));
    });
    content.append(row);
  }
  target.append(li);
}
function showDirectory(path, updateHistory=true) {
  currentPath=path; currentSearch=null; updateTitle(); $('#directory').hidden=false; $('#results').hidden=true;
  if(updateHistory && location.pathname+location.search!==categoryUrl(path)) history.pushState(null,'',categoryUrl(path));
  const crumbs=$('#breadcrumbs'), box=$('#categories'), siteBox=$('#directory-sites'); crumbs.replaceChildren(); box.replaceChildren(); siteBox.replaceChildren();
  if(path.length) {
    crumbs.append(link(t('top'),[],()=>{logActivity('cd /');showDirectory([]);}));
    path.forEach((id,i)=>{ crumbs.append(' › '); const c=categories.find(x=>x.id===id); if(i===path.length-1) crumbs.append(document.createTextNode(name(c))); else crumbs.append(link(name(c),path.slice(0,i+1),()=>{logActivity(`cd /${path.slice(0,i+1).join('/')}`);showDirectory(path.slice(0,i+1));})); });
  }
  const children=categories.filter(c=>c.parent_id===(path.at(-1)??null));
  for(const c of children) { const next=[...path,c.id], div=document.createElement('div'),h=document.createElement('h2'); div.className='category'; h.append(link(`${name(c)} (${c.site_count})`,next,()=>{logActivity(`cd /${next.join('/')}`); showDirectory(next);})); div.append(h); if(!path.length){const p=document.createElement('p');p.className='sub';p.textContent=categories.filter(x=>x.parent_id===c.id).map(name).join(' ');div.append(p);} box.append(div); }
  const direct=sites.filter(s=>s.category_id===path.at(-1)); direct.forEach(s=>renderSite(siteBox,s));
  $('#picks').hidden=false; renderPick();
  $('#sample-label').hidden=!direct.length; $('#directory-empty').hidden=!!children.length || !!direct.length || !path.length;
}
function renderPick() {
  const list=$('#pick-list'); list.replaceChildren();
  if(!pickedIds.length) return;
  const site=sites.find(s=>s.id===pickedIds[pickIndex % pickedIds.length]);
  if(site) renderSite(list,site,true);
}
const picks=$('#picks');
new IntersectionObserver(entries=>{picksVisible=entries[0].isIntersecting;}).observe(picks);
picks.addEventListener('pointerenter',()=>{picksHovered=true;});
picks.addEventListener('pointerleave',()=>{picksHovered=false;});
setInterval(()=>{
  if(pickedIds.length<2 || picks.hidden || !picksVisible || document.hidden || picksHovered || picks.contains(document.activeElement) || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  pickIndex=(pickIndex+1)%pickedIds.length;
  renderPick();
},8000);
function showResults(query) {
  currentSearch=query; $('#directory').hidden=true; $('#picks').hidden=false; $('#results').hidden=false;
  const q=query.toLocaleLowerCase(), uniqueSites=[...new Map(sites.map(site=>[site.id,site])).values()], matches=q?uniqueSites.filter(s=>`${s.name} ${s.description_ja} ${s.description_en??''} ${s.url}`.toLocaleLowerCase().includes(q)):uniqueSites;
  $('#result-title').textContent=q?t('results')(query):t('all'); $('#result-list').replaceChildren(); matches.forEach(s=>renderSite($('#result-list'),s,true)); $('#empty').hidden=!!matches.length;
}
function updateSearch() {
  const q=$('#query').value.trim();
  if(q) showResults(q); else showDirectory(currentPath, false);
}
let composing = false;
$('#query').addEventListener('compositionstart',()=>{composing=true;});
$('#query').addEventListener('compositionend',()=>{composing=false;updateSearch();});
$('#query').addEventListener('input',event=>{if(!composing && !event.isComposing) updateSearch();});
$('#search-form').addEventListener('submit',e=>{e.preventDefault();if(composing) return;const q=$('#query').value.trim();if(q) {logActivity(`search ${q}`);showResults(q);} else showDirectory(currentPath,false);});
$('#clear').addEventListener('click',e=>{e.preventDefault();showDirectory(currentPath);});
$('#home-link').addEventListener('click',e=>{if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)return;e.preventDefault();logActivity('cd /');$('#query').value='';showDirectory([]);});
$('#recommend-link').addEventListener('click',e=>{e.preventDefault();const m=$('#recommend');m.hidden=!m.hidden;e.currentTarget.setAttribute('aria-expanded',String(!m.hidden));});
function restoreLocation() {
  const q=new URLSearchParams(location.search).get('q');
  if(q && q.trim()){ $('#query').value=q; showResults(q.trim()); return; }
  const raw=new URLSearchParams(location.search).getAll('category'); let parent=null; const path=[];
  for(const part of raw) { const c=categories.find(x=>x.parent_id===parent && (x.id===part || x.name_ja===part)); if(!c){ history.replaceState(null,'',categoryUrl([]));showDirectory([],false);return; } path.push(c.id); parent=c.id; }
  if(raw.some((part,i)=>part!==path[i])) history.replaceState(null,'',categoryUrl(path));
  showDirectory(path,false);
}
window.addEventListener('popstate',restoreLocation);
Promise.all([fetch('/api/categories'),fetch('/api/sites'),fetch('/api/activity').catch(()=>null)]).then(async ([a,b,stats])=>{if(!a.ok||!b.ok)throw Error('API');categories=await a.json();sites=await b.json();if(stats && stats.ok) activityStats=await stats.json();const ids=[...new Set(sites.map(s=>s.id))];for(let i=ids.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[ids[i],ids[j]]=[ids[j],ids[i]];}pickedIds=ids.slice(0,3);restoreLocation();applyLanguage();}).catch(()=>{if(!$('#categories').children.length && !$('#directory-sites').children.length) $('#directory').textContent=t('error');});

// ロゴ: S を M と同じ表示幅にし、4文字の見た目の間隔を均等にそろえる（transform はレイアウトに効かないので余白で補正）
(document.fonts?document.fonts.ready:Promise.resolve()).then(()=>{const l=[...document.querySelectorAll('.logo span')];if(l.length<4)return;const m=l[0].offsetWidth,s=l[2].offsetWidth;if(!m||!s)return;const sc=[1.5,1,1.5*m/s,1],gap=8;l.forEach((e,k)=>{const x=(sc[k]-1)*e.offsetWidth/2+gap/2;e.style.transform=`scaleX(${sc[k]})`;e.style.margin=`0 ${x}px`;});});
