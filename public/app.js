const $ = selector => document.querySelector(selector);
let pickPaused = false, categories = [], sites = [], currentPath = [], currentSearch = null, pickedIds = [], pickIndex = 0, picksVisible = false, picksHovered = false, activityStats = null;
// 範囲（scope）: 国コード（小文字 2 文字）または all。ルート `/` は既定範囲の日本として扱う。scopes は選べる国（サイトが 1 件以上ある国）と件数。
const DEFAULT_SCOPE = 'jp', INLINE_SCOPES = 6;
let currentScope = DEFAULT_SCOPE, scopes = { total: 0, countries: [] }, scopeToken = 0, scopesExpanded = false;
const scopeCache = new Map();
const storage = { get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Preferences remain active for this page. */ } } };
let language = storage.get('mise-language') === 'ja' ? 'ja' : 'en';
let openExternalInNewTab = storage.get('mise-external-new-tab') === 'true';
const words = {
  ja: { tagline:'今だからこそ、ディレクトリ型検索エンジン。', recommendIntro:'サイトが見つからない場合は？', recommend:'推薦', searchLabel:'サイトを検索', top:'トップ', sample:'掲載サイト', noSites:'このカテゴリにサイトはありません。', noResults:'該当するサイトはありません。', back:'カテゴリ一覧に戻る', all:'すべてのサイト', results:q=>`「${q}」の検索結果`, concept:'広告や大手サイト多めの検索に疲れた人へ。\nまだ知らない独立サイトを見つける場所を提供しています。', author:'作者のサイト', github:'作者のGitHub', authorLabel:'作者', categoriesLabel:'カテゴリ一覧', locationLabel:'現在の階層', heading:'MISE — 今だからこそ、ディレクトリ型検索エンジン。', pickPause:'一時停止', pickResume:'再開', idle:'No activity yet', theme:'ダーク/ライト', lang:'English', error:'データを取得できませんでした。再読み込みしてください。', fallback:'', belongs:'カテゴリ', uncategorized:'未分類', picks:'ピックアップ', crawled:'最終巡回', uncrawled:'巡回日不明', tags:'属性', externalOn:'外部リンクを別タブで開く: オン', externalOff:'外部リンクを別タブで開く: オフ', summaryTitle:'SITE INDEX', historyTitle:'RECENT REGISTRATIONS', totalLabel:'SITES', unavailable:'UNAVAILABLE', scopeLabel:'範囲', allWorld:'全世界', moreScopes:'他の国', lessScopes:'閉じる' },
  en: { tagline:'A directory search engine for today.', recommendIntro:'Can’t find a site?', recommend:'Recommend it', searchLabel:'Search sites', top:'Home', sample:'Listed sites', noSites:'No sites in this category.', noResults:'No sites found.', back:'Back to categories', all:'All sites', results:q=>`Results for “${q}”`, concept:'For those tired of search results dominated by ads and major sites.\nDiscover independent websites you have yet to find.', author:'Creator’s website', github:'Creator on GitHub', authorLabel:'Creator', categoriesLabel:'Categories', locationLabel:'Current location', heading:'MISE — A directory search engine for today.', pickPause:'Pause', pickResume:'Resume', idle:'No activity yet', theme:'Dark / light', lang:'日本語', error:'Could not load data. Please reload.', fallback:' (Japanese)', belongs:'Categories', uncategorized:'Uncategorized', picks:'Featured sites', crawled:'Last crawled', uncrawled:'Crawl date unknown', tags:'Tags', externalOn:'Open external links in new tabs: on', externalOff:'Open external links in new tabs: off', summaryTitle:'SITE INDEX', historyTitle:'RECENT REGISTRATIONS', totalLabel:'SITES', unavailable:'UNAVAILABLE', scopeLabel:'Scope', allWorld:'All world', moreScopes:'More', lessScopes:'Less' }
};
Object.assign(words.ja, { reqTitle:'申請', reqNote:'申請は依頼です。叶うとは限りません。管理者が確認するまで、掲載の状態は何も変わりません。', reqType:'種類', reqAdd:'サイトを載せてほしい(推薦)', reqRemove:'自分のサイトを外してほしい(取り下げ)', reqCorrect:'掲載内容を直してほしい(訂正)', reqUrl:'サイトの URL', reqReason:'理由・詳細(訂正は必須)', reqContact:'連絡先(メールまたは https の URL。必須)', reqPrivacy:'連絡先は、サイトの外で申請を確認するためだけに使い、決定の 90 日後に削除します。', reqSubmit:'送信', reqSending:'送信中…', reqOk:'受け付けました。確認には時間がかかります。連絡先に返信が届くとは限りません。', reqErr_invalid_url:'URL を確認してください(https://example.com/ の形。IP アドレスや内部ホストは不可)。', reqErr_invalid_contact:'連絡先はメールアドレスか https の URL にしてください。', reqErr_invalid_type:'種類を選んでください。', reqErr_reason_required:'訂正は、直したい内容を書いてください。', reqErr_reason_too_long:'理由が長すぎます(1000 字まで)。', reqErr_invalid_reason:'HTML は使えません。', reqErr_rate_limited:'送信が多すぎます。しばらくしてからもう一度お試しください。', reqErr_other:'送信できませんでした。時間をおいてもう一度お試しください。' });
Object.assign(words.en, { reqTitle:'Requests', reqNote:'A request is only a request. It may not be granted. Nothing changes until an administrator has reviewed it.', reqType:'Kind', reqAdd:'Add a site (recommend)', reqRemove:'Remove my site (opt out)', reqCorrect:'Correct a listing', reqUrl:'Site URL', reqReason:'Reason / details (required for corrections)', reqContact:'Contact (email or https URL; required)', reqPrivacy:'The contact is used only to confirm the request outside this site, and is deleted 90 days after the decision.', reqSubmit:'Send', reqSending:'Sending…', reqOk:'Received. Review takes time, and a reply is not guaranteed.', reqErr_invalid_url:'Check the URL (like https://example.com/; IP addresses and internal hosts are not accepted).', reqErr_invalid_contact:'Contact must be an email address or an https URL.', reqErr_invalid_type:'Choose a kind.', reqErr_reason_required:'Please describe what should be corrected.', reqErr_reason_too_long:'The reason is too long (max 1000 characters).', reqErr_invalid_reason:'HTML is not allowed.', reqErr_rate_limited:'Too many requests. Please try again later.', reqErr_other:'Could not send. Please try again later.' });
const t = key => words[language][key];
const siteDescription = site => language === 'en' && site.description_en ? site.description_en : site.description_ja + t('fallback');
const name = category => category[language === 'ja' ? 'name_ja' : 'name_en'];
const activity = [];
const ACTIVITY_MAX_LINES = 5;
function activityLine(entry) { const line=document.createElement('div'); line.textContent=`> ${entry}`; return line; }
function logActivity(message) { const first=!activity.length; activity.push(message); while (activity.length > ACTIVITY_MAX_LINES) activity.shift(); if (first) { renderLog(); return; } const box=$('#activity-entries'); box.append(activityLine(message)); while (box.children.length > activity.length) box.firstElementChild.remove(); }
function renderLog() { $('#activity-entries').replaceChildren(...(activity.length ? activity.map(activityLine) : [Object.assign(document.createElement('div'), { textContent: t('idle') })])); }
// 範囲の表示名。all は「全世界」、国は国名（日英）。
const scopeName = code => code === 'all' ? t('allWorld') : (window.MISE_COUNTRIES?.[code.toUpperCase()]?.[language] ?? code.toUpperCase());
function updateTitle() {
  const cur = currentPath.map(id => categories.find(x => x.id === id)).filter(Boolean);
  const place = currentScope === 'all' ? null : scopeName(currentScope);
  document.title = cur.length ? `${cur.map(name).join(' › ')}${place ? ` (${place})` : ''} — MISE` : place ? `MISE — ${place}: ${t('tagline')}` : `MISE — ${t('tagline')}`;
}
function applyLanguage() {
  document.documentElement.lang = language;
  updateTitle();
  document.querySelectorAll('[data-i18n]').forEach(el => { const value=t(el.dataset.i18n); if (value) el.textContent=value; });
  document.querySelectorAll('[data-i18n-label]').forEach(el => { const value=t(el.dataset.i18nLabel); el.setAttribute('aria-label',value); if(el.hasAttribute('title')) el.title=value; });
  $('#query').setAttribute('aria-label',t('searchLabel'));
  updatePickPause();
  renderReqStatus();
  updateExternalToggle();
  $('#language-toggle').title=t('lang'); $('#language-toggle').setAttribute('aria-label',t('lang'));
  renderLog();
  renderStats();
  renderScopeNav();
  if (currentSearch !== null) showResults(currentSearch); else showDirectory(currentPath, false);
}
// サイトURLは http/https だけをリンクにする（javascript: 等は表に出さない）。
const safeHttpUrl = value => { try { const u=new URL(value); return u.protocol==='http:'||u.protocol==='https:' ? u.href : null; } catch (_) { return null; } };
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


function renderStats() {
  $('#site-total').textContent = `${t('totalLabel')}${currentScope === 'all' ? '' : ` · ${currentScope.toUpperCase()}`}: ${activityStats ? activityStats.total : t('unavailable')}`;
  const bars=$('#history-bars'); bars.replaceChildren();
  if(!activityStats) { bars.textContent='UNAVAILABLE'; return; }
  const days=new Map(activityStats.known_daily.map(d=>[d.day,d.count]));
  const maxCount=Math.max(0,...days.values());
  bars.style.setProperty('--count-digits', String(Math.max(1,...Array.from(days.values(),n=>String(n).length))));
  for(let i=6;i>=0;i--) {
    const date=new Date(); date.setUTCHours(0,0,0,0); date.setUTCDate(date.getUTCDate()-i);
    const day=date.toISOString().slice(0,10), count=days.get(day)||0;
    const item=document.createElement('div'); item.className='history-day'; const sr=document.createElement('span'); sr.className='visually-hidden'; sr.textContent=`${day}: ${count}`;
    const label=document.createElement('span'); label.className='history-date'; label.textContent=date.toLocaleDateString('en-US',{weekday:'short',timeZone:'UTC'});
    const bar=document.createElement('span'); bar.className='history-bar'; bar.setAttribute('aria-hidden','true'); bar.style.width=count>0&&maxCount>0?`max(3px, ${count/maxCount*100}%)`:'0';
    const track=document.createElement('span'); track.className='history-track'; track.setAttribute('aria-hidden','true'); track.append(bar);
    const number=document.createElement('span'); number.className='history-count'; number.textContent=String(count);
    label.setAttribute('aria-hidden','true'); number.setAttribute('aria-hidden','true'); item.append(sr,label,number,track); bars.append(item);
  }
}
// 範囲つきの URL（/jp/?category=…）。カテゴリの階層は従来どおりクエリ。
function categoryUrl(path, scope=currentScope) { const u=new URL(`/${scope}/`,location.origin); path.forEach(id=>u.searchParams.append('category',id)); return u.pathname+u.search; }
const scopeFromLocation = () => { const m=location.pathname.match(/^\/([a-z]{2}|all)\/$/); return m ? m[1] : DEFAULT_SCOPE; };
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
// 国旗（SVG。絵文字は Windows で国コード文字になるため使わない）。国コードが無い/表に無いサイトは出さない。
// 旗の画像は必要になった国のものだけ /flags/ から読み、無ければ旗ごと隠す。
function regionInfo(site) {
  const code=site.region_code;
  return window.MISE_REGIONS && /^[A-Z]{2}-[A-Z0-9]{1,3}$/.test(code||'') && code.slice(0,2)===site.country_code ? window.MISE_REGIONS[code]||null : null;
}
// 区画（県・州）が分かるサイトだけ、旗の右に区画名を小さく出す。区画表に無ければ従来どおり旗のみ。
function countryFlag(site) {
  const code=site.country_code, info=window.MISE_COUNTRIES && /^[A-Z]{2}$/.test(code||'') ? window.MISE_COUNTRIES[code] : null;
  if(!info || !(window.MISE_FLAGS||[]).includes(code)) return null;
  const region=regionInfo(site);
  const label=region ? `${info.ja} ${region.ja} / ${info.en} ${region.en}` : `${info.ja} / ${info.en}`;
  const wrap=document.createElement('span'); wrap.className='site-place'; wrap.setAttribute('role','img'); wrap.setAttribute('aria-label',label); wrap.title=label; wrap.dataset.country=code;
  if(region) wrap.dataset.region=site.region_code;
  const flag=document.createElement('span'); flag.className='site-flag'; flag.setAttribute('aria-hidden','true');
  const img=document.createElement('img'); img.alt=''; img.src=`/flags/${code.toLowerCase()}.svg`; img.loading='lazy'; img.decoding='async';
  img.addEventListener('error',()=>{wrap.hidden=true;});
  flag.append(img); wrap.append(flag);
  if(region) { const name=document.createElement('span'); name.className='site-region'; name.textContent=language==='ja'?region.ja:region.en; wrap.append(name); }
  return wrap;
}
// 地球を国へ向ける。PC: 行/旗のホバー・フォーカスで約300ms静止してから。スマホ: 旗のタップ。行本体のクリックは従来どおり遷移のみ。
const HOVER_DELAY_MS=300;
// 区画（県・州）が分かるサイトは区画の代表点（国より近い距離）、無ければ国の代表点。
function flyToPlace(site,hold) {
  const country=window.MISE_COUNTRIES && window.MISE_COUNTRIES[site.country_code]; if(!country) return;
  const region=regionInfo(site), target=region||country;
  window.dispatchEvent(new CustomEvent('mise-fly-country',{detail:{lat:target.lat,lon:target.lon,hold,level:region?'region':'country'}}));
  logActivity(`look ${region?site.region_code:site.country_code}`);
}
function bindGlobeHover(row,flag,site) {
  let timer=0, flown=false;
  const start=()=>{ clearTimeout(timer); timer=setTimeout(()=>{ flown=true; flyToPlace(site,true); },HOVER_DELAY_MS); };
  // 地球を動かしていない（300ms未満で離れた）ときは release を出さない。
  const stop=()=>{ clearTimeout(timer); timer=0; if(flown) { flown=false; window.dispatchEvent(new CustomEvent('mise-fly-release')); } };
  row.addEventListener('pointerenter',e=>{ if(e.pointerType!=='touch') start(); });
  row.addEventListener('pointerleave',e=>{ if(e.pointerType!=='touch') stop(); });
  row.addEventListener('focusin',start);
  row.addEventListener('focusout',e=>{ if(!row.contains(e.relatedTarget)) stop(); });
  // タップ（マウスのクリックも可）。ホールドなし＝離れて数秒で自動回転へ戻る。
  flag.addEventListener('click',()=>{ clearTimeout(timer); flown=false; flyToPlace(site,false); });
}
function renderSite(target, site, showCategories=false) {
  const li=document.createElement('li'), a=document.createElement('a'), p=document.createElement('p');
  const href=safeHttpUrl(site.url); if(href) { a.href=href; configureExternalLink(a); }
  a.textContent=site.name; p.textContent=siteDescription(site);
  if(language==='en' && !site.description_en) p.lang='ja';
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
  const flag=countryFlag(site); if(flag) { content.insertBefore(flag,p); bindGlobeHover(li,flag,site); }
  const meta=document.createElement('p'); meta.className='site-meta';
  const crawlDate=site.last_crawled_at && !Number.isNaN(Date.parse(site.last_crawled_at)) ? site.last_crawled_at.slice(0,10) : null;
  meta.textContent=crawlDate ? `${t('crawled')}: ${crawlDate}` : '';
  let tags=[]; try { tags=JSON.parse(site.tags_json || '[]'); } catch (_) { tags=[]; }
  for(const tag of tags) { const span=document.createElement('span'); span.className='site-tag'; span.textContent=(language==='en' && tag.en) ? tag.en : tag.ja; meta.append(' ',span); }
  if(meta.textContent.trim()) content.append(meta); li.append(icon,content);
  if(showCategories) {
    const row=document.createElement('p'); row.className='site-categories'; row.append(`${t('belongs')}: `);
    const ids=[...new Set(sites.filter(s=>s.id===site.id).map(s=>s.category_id))].filter(id=>id!=null);
    if(!ids.length) row.append(t('uncategorized'));
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
  for(const c of children) { const next=[...path,c.id], div=document.createElement('div'),h=document.createElement('h2'); div.className='category'+(c.site_count?'':' is-empty'); h.append(link(`${name(c)} (${c.site_count})`,next,()=>{logActivity(`cd /${next.join('/')}`); showDirectory(next);})); div.append(h); if(!path.length){const p=document.createElement('p');p.className='sub';p.textContent=categories.filter(x=>x.parent_id===c.id).map(name).join(' ');div.append(p);} box.append(div); }
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
function updatePickPause() { const b=$('#pick-pause'); b.textContent=t(pickPaused?'pickResume':'pickPause'); b.setAttribute('aria-pressed',String(pickPaused)); b.hidden=pickedIds.length<2; }
$('#pick-pause').addEventListener('click',()=>{pickPaused=!pickPaused;updatePickPause();});
setInterval(()=>{
  if(pickedIds.length<2 || pickPaused || picks.hidden || !picksVisible || document.hidden || picksHovered || picks.contains(document.activeElement) || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
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
let reqStatus = null;
let composing = false;
$('#query').addEventListener('compositionstart',()=>{composing=true;});
$('#query').addEventListener('compositionend',()=>{composing=false;updateSearch();});
$('#query').addEventListener('input',event=>{if(!composing && !event.isComposing) updateSearch();});
$('#search-form').addEventListener('submit',e=>{e.preventDefault();if(composing) return;const q=$('#query').value.trim();if(q) {logActivity(`search ${q}`);showResults(q);} else showDirectory(currentPath,false);});
$('#clear').addEventListener('click',e=>{e.preventDefault();showDirectory(currentPath);});
$('#home-link').addEventListener('click',e=>{if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)return;e.preventDefault();logActivity('cd /');$('#query').value='';showDirectory([]);});
function renderReqStatus(){const st=$('#req-status');if(!reqStatus){st.textContent='';return;}st.textContent=t(reqStatus.key);st.dataset.state=reqStatus.state;}
$('#request-form').addEventListener('submit',async e=>{
  e.preventDefault(); const f=e.currentTarget, st=$('#req-status'), btn=$('#req-submit');
  const show=(key,state)=>{reqStatus={key,state};renderReqStatus();};
  btn.disabled=true; show('reqSending','info');
  try {
    const res=await fetch('/api/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:f.type.value,url:f.url.value,reason:f.reason.value,contact:f.contact.value,website:f.website.value})});
    if(res.ok){ show('reqOk','ok'); f.url.value=''; f.reason.value=''; }
    else { const j=await res.json().catch(()=>({})); show(t('reqErr_'+j.error)?'reqErr_'+j.error:(res.status===429?'reqErr_rate_limited':'reqErr_other'),'error'); }
  } catch(_) { show('reqErr_other','error'); }
  finally { btn.disabled=false; }
});
$('#recommend-link').addEventListener('click',e=>{e.preventDefault();const m=$('#recommend');m.hidden=!m.hidden;e.currentTarget.setAttribute('aria-expanded',String(!m.hidden));});
// 範囲切替。国旗/国名のリンクは通常の <a href="/us/"> で、修飾キー付き・中クリックはブラウザ任せ。
const SCOPE_ICON = '<svg class="scope-globe" aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2"/><ellipse cx="8" cy="8" rx="2.8" ry="6.2"/><path d="M1.8 8h12.4"/></svg>';
function scopeItem(code, label, count, icon) {
  const li=document.createElement('li'), a=document.createElement('a');
  a.className='scope-item'; a.href=`/${code}/`; if(code===currentScope) a.setAttribute('aria-current','page');
  if(icon instanceof Node) a.append(icon); else a.insertAdjacentHTML('beforeend',icon);
  const n=document.createElement('span'); n.className='scope-name'; n.textContent=label;
  const c=document.createElement('span'); c.className='scope-count'; c.textContent=String(count);
  a.append(n,c);
  a.addEventListener('click',e=>{ if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); chooseScope(code); });
  li.append(a); return li;
}
function renderScopeNav() {
  const nav=$('#scope-nav'); if(!nav) return;
  const key=document.createElement('span'); key.className='scope-key'; key.setAttribute('aria-hidden','true'); key.textContent='SCOPE';
  const list=document.createElement('ul'); list.className='scope-list';
  list.append(scopeItem('all',t('allWorld'),scopes.total,SCOPE_ICON));
  const extras=[];
  scopes.countries.forEach((r,i)=>{
    const code=r.code.toLowerCase(), info=window.MISE_COUNTRIES?.[r.code];
    let icon='';
    if(window.MISE_FLAGS?.includes(r.code)) { const img=document.createElement('img'); img.className='scope-flag'; img.alt=''; img.width=16; img.height=12; img.src=`/flags/${code}.svg`; img.addEventListener('error',()=>{img.hidden=true;}); icon=img; }
    const li=scopeItem(code,info?.[language]??r.code,r.count,icon);
    if(i>=INLINE_SCOPES) { li.hidden=!scopesExpanded && code!==currentScope; extras.push(li); }
    list.append(li);
  });
  const parts=[key,list];
  if(extras.length) {
    if(extras.some(li=>li.querySelector('[aria-current]'))) { scopesExpanded=true; extras.forEach(li=>{li.hidden=false;}); }
    const more=document.createElement('button'); more.type='button'; more.className='scope-more'; more.setAttribute('aria-expanded',String(scopesExpanded));
    more.textContent=scopesExpanded?t('lessScopes'):`${t('moreScopes')} (${extras.length})`;
    more.addEventListener('click',()=>{ scopesExpanded=!scopesExpanded; renderScopeNav(); });
    parts.push(more);
  }
  nav.replaceChildren(...parts);
  $('#home-link').href=categoryUrl([]);
  // 地球儀へ「選べる国」と現在の範囲を伝える（目印の点と、背景クリックで国を選ぶ判定に使う）。
  window.MISE_SCOPE={ current:currentScope, countries:scopes.countries.map(r=>({code:r.code,lat:window.MISE_COUNTRIES?.[r.code]?.lat,lon:window.MISE_COUNTRIES?.[r.code]?.lon})) };
  window.dispatchEvent(new CustomEvent('mise-scope-changed'));
}
// 範囲ごとのデータ（カテゴリ・サイト・活動）を取得する。同じ範囲は一度だけ（ページの寿命の間）。失敗は例外。
function loadScopeData(scope) {
  if(!scopeCache.has(scope)) {
    const q=`?scope=${encodeURIComponent(scope)}`;
    const p=Promise.all([fetch(`/api/categories${q}`),fetch(`/api/sites${q}`),fetch(`/api/activity${q}`).catch(()=>null)]).then(async ([a,b,stats])=>{
      if(!a.ok||!b.ok) throw Error('API');
      return { categories:await a.json(), sites:await b.json(), stats:stats && stats.ok ? await stats.json() : null };
    });
    scopeCache.set(scope,p); p.catch(()=>scopeCache.delete(scope));
  }
  return scopeCache.get(scope);
}
// 範囲のデータを画面の状態に反映する（URL・描画は呼び出し側）。後から来た別の切替が先に完了していたら false。
async function switchData(scope) {
  const token=++scopeToken, data=await loadScopeData(scope);
  if(token!==scopeToken) return false;
  currentScope=scope; categories=data.categories; sites=data.sites; activityStats=data.stats;
  const ids=[...new Set(sites.map(s=>s.id))];
  for(let i=ids.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[ids[i],ids[j]]=[ids[j],ids[i]];}
  pickedIds=ids.slice(0,3); pickIndex=0;
  return true;
}
// 切替後も、カテゴリ階層と検索語は維持する（docs/design/ui.md）。
async function chooseScope(scope) {
  if(scope===currentScope) return;
  logActivity(`scope ${scope}`);
  let ok=false; try { ok=await switchData(scope); } catch (_) { logActivity('scope error'); return; }
  if(!ok) return;
  const u=categoryUrl(currentPath); history.pushState(null,'',currentSearch!==null ? `/${scope}/?q=${encodeURIComponent(currentSearch)}` : u);
  renderScopeNav(); renderStats(); applyLanguage();
  const info=scope==='all' ? null : window.MISE_COUNTRIES?.[scope.toUpperCase()];
  if(info) window.dispatchEvent(new CustomEvent('mise-fly-country',{detail:{lat:info.lat,lon:info.lon,hold:false,level:'country'}}));
}
// 地球儀の背景クリック/タップ。選べる国（サイトがある国）だけ範囲にする。
window.addEventListener('mise-globe-pick',e=>{ const code=String(e.detail?.code||''); if(scopes.countries.some(r=>r.code===code)) chooseScope(code.toLowerCase()); });
function restoreLocation() {
  const q=new URLSearchParams(location.search).get('q');
  if(q && q.trim()){ $('#query').value=q; showResults(q.trim()); return; }
  $('#query').value='';
  const raw=new URLSearchParams(location.search).getAll('category'); let parent=null; const path=[];
  for(const part of raw) { const c=categories.find(x=>x.parent_id===parent && (x.id===part || x.name_ja===part)); if(!c){ history.replaceState(null,'',categoryUrl([]));showDirectory([],false);return; } path.push(c.id); parent=c.id; }
  if(raw.some((part,i)=>part!==path[i])) history.replaceState(null,'',categoryUrl(path));
  showDirectory(path,false);
}
// 戻る/進むは範囲も復元する（範囲が変わるときはそのデータを読んでから階層・検索を復元）。
window.addEventListener('popstate',async()=>{
  const scope=scopeFromLocation();
  if(scope!==currentScope) { try { if(!(await switchData(scope))) return; } catch (_) { return; } renderScopeNav(); renderStats(); }
  restoreLocation(); applyLanguage();
});
(async()=>{
  try {
    currentScope=scopeFromLocation();
    const [, sc]=await Promise.all([switchData(currentScope), fetch('/api/scopes').then(r=>r.ok?r.json():null).catch(()=>null)]);
    if(sc) scopes=sc;
    restoreLocation(); applyLanguage();
  } catch (_) { if(!$('#categories').children.length && !$('#directory-sites').children.length) $('#directory').textContent=t('error'); }
})();

// ロゴ: S を M と同じ表示幅にし、4文字の見た目の間隔を均等にそろえる（transform はレイアウトに効かないので余白で補正）
(document.fonts?document.fonts.ready:Promise.resolve()).then(()=>window.__miseLogo&&window.__miseLogo());
