const $ = selector => document.querySelector(selector);
let categories = [], pickedSites = [], currentPath = [], currentSearch = null, currentTags = [], tagPath = [], pickIndex = 0, picksVisible = false, activityStats = null;
// 範囲（scope）: 国コード（小文字 2 文字）または all。ルート `/` は既定範囲の日本として扱う。scopes は選べる国（サイトが 1 件以上ある国）と件数。
const DEFAULT_SCOPE = 'jp', INLINE_SCOPES = 6;
let currentScope = DEFAULT_SCOPE, scopes = { total: 0, countries: [] }, scopeToken = 0, scopesExpanded = false;
const scopeCache = new Map();
const storage = { get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Preferences remain active for this page. */ } } };
// 初期言語: 保存値（ja / en）を最優先。無ければ index.html の先頭インラインスクリプトがブラウザ言語から決めた <html data-initial-lang> を使う（判定は二重に持たない）。
let keepRequestOpen = false;
const initialLanguage = () => {
  const saved = storage.get('mise-language');
  if (saved === 'ja' || saved === 'en') return saved;
  return document.documentElement.dataset.initialLang === 'ja' ? 'ja' : 'en';
  // index.html の先頭スクリプトが決めた値（保険タイマーが lang を en に戻しても、遅れて動いた app.js は ja にできる）
};
let language = initialLanguage();
const endI18nPending = () => document.documentElement.classList.remove('i18n-pending');
let openExternalInNewTab = storage.get('mise-external-new-tab') === 'true';
const words = {
  ja: { tagline:'今だからこそ、ディレクトリ型検索エンジン。', recommendIntro:'サイトが見つからない場合は？', recommend:'推薦', searchLabel:'サイトを検索', top:'トップ', sample:'掲載サイト', noSites:'このカテゴリにサイトはありません。', noResults:'該当するサイトはありません。', back:'カテゴリ一覧に戻る', all:'すべてのサイト', results:q=>`「${q}」の検索結果`, credit:'官公庁・自治体の掲載は、総務省「全国地方公共団体コード」とデジタル庁 e-Gov「政府機関関連情報」(いずれも公共データ利用規約 第1.0版)、Wikidata(CC0)をもとに、MISE が編集・加工したものです。', concept:'広告や大手サイト多めの検索に疲れた人へ。\nまだ知らない独立サイトを見つける場所を提供しています。', author:'作者のサイト', github:'作者のGitHub', authorLabel:'作者', categoriesLabel:'カテゴリ一覧', locationLabel:'現在の階層', heading:'MISE — 今だからこそ、ディレクトリ型検索エンジン。', idle:'No activity yet', theme:'ダーク/ライト', lang:'English', error:'データを取得できませんでした。再読み込みしてください。', fallback:'', belongs:'カテゴリ', uncategorized:'未分類', picks:'ピックアップ', crawled:'最終巡回', uncrawled:'巡回日不明', tags:'属性', externalOn:'外部リンクを別タブで開く: オン', externalOff:'外部リンクを別タブで開く: オフ', summaryTitle:'SITE INDEX', historyTitle:'RECENT REGISTRATIONS', totalLabel:'SITES', unavailable:'UNAVAILABLE', scopeLabel:'範囲', allWorld:'全世界', moreScopes:'他の国を表示', lessScopes:'他の国を閉じる', loadingMore:'読み込み中…', loadMore:'もっと見る', retry:'再試行', listError:'読み込めませんでした。', listEnd:n=>`ここまで（全 ${n} 件）`, remaining:n=>`あと ${n} 件`, prevPage:'‹ 前のページ', tagsLabel:'絞り込みのタグ', tagRemove:n=>`タグ「${n}」を外す`, tagFilter:n=>`タグ「${n}」で絞り込む`, taggedSites:n=>`${n} のサイト`, tagsMax:'タグは5個までです。' },
  en: { tagline:'A directory search engine for today.', recommendIntro:'Can’t find a site?', recommend:'Recommend it', searchLabel:'Search sites', top:'Home', sample:'Listed sites', noSites:'No sites in this category.', noResults:'No sites found.', back:'Back to categories', all:'All sites', results:q=>`Results for “${q}”`, credit:'Government and municipal sites: based on MIC “National Local Government Codes” and Digital Agency e-Gov “Government Directory” (both under the Public Data License 1.0) and Wikidata (CC0); compiled and processed by MISE.', concept:'For those tired of search results dominated by ads and major sites.\nDiscover independent websites you have yet to find.', author:'Creator’s website', github:'Creator on GitHub', authorLabel:'Creator', categoriesLabel:'Categories', locationLabel:'Current location', heading:'MISE — A directory search engine for today.', idle:'No activity yet', theme:'Dark / light', lang:'日本語', error:'Could not load data. Please reload.', fallback:' (Japanese)', belongs:'Categories', uncategorized:'Uncategorized', picks:'Featured sites', crawled:'Last crawled', uncrawled:'Crawl date unknown', tags:'Tags', externalOn:'Open external links in new tabs: on', externalOff:'Open external links in new tabs: off', summaryTitle:'SITE INDEX', historyTitle:'RECENT REGISTRATIONS', totalLabel:'SITES', unavailable:'UNAVAILABLE', scopeLabel:'Scope', allWorld:'All world', moreScopes:'Show more countries', lessScopes:'Hide more countries', loadingMore:'Loading…', loadMore:'Load more', retry:'Retry', listError:'Could not load more sites.', listEnd:n=>`End of list (${n} total)`, remaining:n=>`${n} more`, prevPage:'‹ Previous page', tagsLabel:'Tag filters', tagRemove:n=>`Remove tag “${n}”`, tagFilter:n=>`Filter by tag “${n}”`, taggedSites:n=>`Sites tagged ${n}`, tagsMax:'Up to 5 tags.' }
};
Object.assign(words.ja, { reqTitle:'申請', reqNote:'申請は依頼です。叶うとは限りません。管理者が確認するまで、掲載の状態は何も変わりません。', reqType:'種類', reqAdd:'サイトを載せてほしい(推薦)', reqRemove:'自分のサイトを外してほしい(取り下げ)', reqCorrect:'掲載内容を直してほしい(訂正)', reqUrl:'サイトの URL', reqReason:'理由・詳細(必須)', reqSubmit:'送信', reqSending:'送信中…', reqOk:'受け付けました。', reqErr_invalid_url:'URL を確認してください(https://example.com/ の形。IP アドレスや内部ホストは不可)。', reqErr_invalid_type:'種類を選んでください。', reqErr_reason_required:'内容を入力してください。', reqErr_reason_too_long:'理由が長すぎます(1000 字まで)。', reqErr_invalid_reason:'HTML は使えません。', reqErr_rate_limited:'送信が多すぎます。しばらくしてからもう一度お試しください。', reqErr_too_large:'入力が大きすぎます。短くしてください。', reqErr_other:'送信できませんでした。時間をおいてもう一度お試しください。' });
Object.assign(words.en, { reqTitle:'Requests', reqNote:'A request is only a request. It may not be granted. Nothing changes until an administrator has reviewed it.', reqType:'Kind', reqAdd:'Add a site (recommend)', reqRemove:'Remove my site (opt out)', reqCorrect:'Correct a listing', reqUrl:'Site URL', reqReason:'Reason / details (required)', reqSubmit:'Send', reqSending:'Sending…', reqOk:'Received.', reqErr_invalid_url:'Check the URL (like https://example.com/; IP addresses and internal hosts are not accepted).', reqErr_invalid_type:'Choose a kind.', reqErr_reason_required:'Please enter the details.', reqErr_reason_too_long:'The reason is too long (max 1000 characters).', reqErr_invalid_reason:'HTML is not allowed.', reqErr_rate_limited:'Too many requests. Please try again later.', reqErr_too_large:'The input is too large. Please shorten it.', reqErr_other:'Could not send. Please try again later.' });
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
// データ取得前でも描ける静的部分。初回の英語→日本語のちらつきを、API 応答を待たずに抑える。
function applyStaticLanguage() {
  document.documentElement.lang = language;
  window.__miseReady = true;
  document.querySelectorAll('[data-i18n]').forEach(el => { const value=t(el.dataset.i18n); if (value) el.textContent=value; });
  document.querySelectorAll('[data-i18n-label]').forEach(el => { const value=t(el.dataset.i18nLabel); el.setAttribute('aria-label',value); if(el.hasAttribute('title')) el.title=value; });
  $('#query').setAttribute('aria-label',t('searchLabel'));
  renderReqStatus();
  updateExternalToggle();
  $('#language-toggle').title=t('lang'); $('#language-toggle').setAttribute('aria-label',t('lang'));
  renderLog();
}
function applyLanguage(animate=false) {
  applyStaticLanguage();
  updateTitle();
  renderStats();
  renderScopeNav();
  keepRequestOpen = true; animateRedraw = animate; // 言語切替の再描画では申請フォームの開閉を変えない。範囲切替だけは一覧をフェードさせる
  try { if (currentSearch !== null) showResults(currentSearch); else showDirectory(currentPath, false); } finally { keepRequestOpen = false; animateRedraw = false; }
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
// 地球を国へ向ける。PC: 行/旗のホバー・フォーカスで約300ms静止してから。スマホ: 旗または行（リンク以外）のタップ。リンクのクリックは従来どおり遷移のみ。
// 世界全体の見え方へ戻す（ロゴ・トップ・範囲 all）。一覧を描き直すときはホールドも解く（消えた行から pointerleave/focusout が来ないため）。
const resetGlobe=()=>window.dispatchEvent(new CustomEvent('mise-fly-reset'));
const releaseGlobe=()=>window.dispatchEvent(new CustomEvent('mise-fly-release'));
// サイト行を作り直す共通の入口。消える行からは pointerleave/focusout が来ないので、置換の前にホールドを解く（地球儀側は保持中でなければ何もしない）。
// 範囲を変えたときの地球儀: 国別範囲はその国へ、all は世界全体へ。範囲ナビ・地球儀の選択・戻る/進むで共通。
function globeForScope(scope) {
  const info=scope==='all' ? null : window.MISE_COUNTRIES?.[scope.toUpperCase()];
  if(info) window.dispatchEvent(new CustomEvent('mise-fly-country',{detail:{lat:info.lat,lon:info.lon,hold:false,level:'country'}})); else resetGlobe();
}
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
  const start=()=>{ clearTimeout(timer); timer=setTimeout(()=>{ if(!row.isConnected) return; flown=true; flyToPlace(site,true); },HOVER_DELAY_MS); };
  // 地球を動かしていない（300ms未満で離れた）ときは release を出さない。
  const stop=()=>{ clearTimeout(timer); timer=0; if(flown) { flown=false; window.dispatchEvent(new CustomEvent('mise-fly-release')); } };
  row.addEventListener('pointerenter',e=>{ if(e.pointerType!=='touch') start(); });
  row.addEventListener('pointerleave',e=>{ if(e.pointerType!=='touch') stop(); });
  // キーボード操作のフォーカスだけ（リンクのタップ/クリックによるフォーカスでは動かさない）。
  row.addEventListener('focusin',e=>{ if(e.target.matches?.(':focus-visible')) start(); });
  row.addEventListener('focusout',e=>{ if(!row.contains(e.relatedTarget)) stop(); });
  // タップ（マウスのクリックも可）。ホールドなし＝離れて数秒で自動回転へ戻る。
  flag.addEventListener('click',()=>{ clearTimeout(timer); flown=false; flyToPlace(site,false); });
  // スマホ: 行のリンク・旗以外（説明文・メタ情報・余白）のタップでも所在地へ。click はスクロール/スワイプでは出ない。タイトルのリンクは遷移を妨げない。
  // 古い iOS Safari などは click が pointerType なしの MouseEvent になるため、直前の pointerdown/touchstart で入力種別を記録して判定する（pen も touch と同じ扱い）。
  let input={type:'',at:0};
  const note=type=>{ input={type,at:performance.now()}; };
  row.addEventListener('pointerdown',e=>note(e.pointerType));
  row.addEventListener('touchstart',()=>note('touch'),{passive:true});
  row.addEventListener('click',e=>{
    const type=performance.now()-input.at<500 ? input.type : (e.pointerType||'');
    if(type!=='touch' && type!=='pen') return;
    if(e.target.closest('a, button, .site-place')) return;
    clearTimeout(timer); flown=false; flyToPlace(site,false);
  });
}
// ---- タグ（#31）: 押すと検索バー横の取り消せるチップになり、検索語・範囲・カテゴリと AND で絞る。URL は ?tag=（複数は繰り返し）。 ----
const MAX_TAGS=5;
const tagLabel=tag=>(language==='en' && tag.en) ? tag.en : tag.ja;
const tagUrl=(key,scope=currentScope)=>{ const u=new URL(`/${scope}/`,location.origin); u.searchParams.append('tag',key); return u.pathname+u.search; };
// 結果ページ/タグ付きの URL。タグが無ければ従来どおり（検索は ?q=、ディレクトリは ?category=）。
function stateUrl(scope=currentScope) {
  if(currentSearch===null) return categoryUrl(currentPath,scope);
  if(!currentTags.length) return `/${scope}/?q=${encodeURIComponent(currentSearch)}`;
  const u=new URL(`/${scope}/`,location.origin);
  if(currentSearch) u.searchParams.set('q',currentSearch);
  tagPath.forEach(id=>u.searchParams.append('category',id));
  currentTags.forEach(tg=>u.searchParams.append('tag',tg.key));
  return u.pathname+u.search;
}
const tagActive=key=>currentTags.some(tg=>tg.key===key);
function tagLink(tag) {
  const a=document.createElement('a'); a.className='site-tag'; a.href=tagUrl(tag.ja); a.textContent=tagLabel(tag); a.dataset.tagKey=tag.ja;
  a.title=t('tagFilter')(tagLabel(tag));
  if(tagActive(tag.ja)) { a.classList.add('is-active'); a.setAttribute('aria-current','true'); }
  a.addEventListener('click', e=>{ if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); toggleTag(tag, !!a.closest('#directory-sites')); });
  return a;
}
function refreshTagLinks() {
  document.querySelectorAll('a.site-tag').forEach(a=>{ const on=tagActive(a.dataset.tagKey); a.classList.toggle('is-active',on); if(on) a.setAttribute('aria-current','true'); else a.removeAttribute('aria-current'); });
}
function renderChips() {
  const box=$('#tag-chips'); box.replaceChildren(); box.hidden=!currentTags.length;
  for(const tg of currentTags) {
    const chip=document.createElement('span'), label=document.createElement('span'), x=document.createElement('button'), n=tagLabel(tg);
    chip.className='tag-chip'; label.textContent=`#${n}`; x.type='button'; x.textContent='×'; x.setAttribute('aria-label',t('tagRemove')(n)); x.title=t('tagRemove')(n);
    x.addEventListener('click',()=>{ toggleTag(tg, false); });
    chip.append(label,x); box.append(chip);
  }
  box.setAttribute('aria-label',t('tagsLabel'));
  refreshTagLinks();
}
// URL から来たタグは日本語名/英語名のキーだけなので、最初の結果が届いたら表示名（日英）を埋める。
function resolveTagLabels(items) {
  let changed=false;
  for(const tg of currentTags) {
    if(tg.resolved) continue;
    for(const it of items) { let ts=[]; try { ts=JSON.parse(it.tags_json||'[]'); } catch (_) { ts=[]; } const hit=ts.find(x=>x.ja===tg.key || x.en===tg.key); if(hit) { tg.key=hit.ja; tg.ja=hit.ja; tg.en=hit.en; tg.resolved=true; changed=true; break; } }
  }
  if(!changed) return;
  // 英語名で来たタグは日本語名へ正規化する（行の太字・チップ・URL を日本語名に揃え、同じタグの二重チップも防ぐ）。
  currentTags=currentTags.filter((tg,i)=>currentTags.findIndex(x=>x.key===tg.key)===i);
  renderChips(); if(currentSearch!==null) setResultTitle(currentSearch);
  if(currentSearch!==null && activeList && activeList.locKey===location.pathname+location.search) { const url=stateUrl(); if(location.pathname+location.search!==url) { history.replaceState(history.state,'',url); activeList.locKey=url; } }
}
// タグを付ける/外す。検索語（入力欄の文字）はそのまま残り、AND で絞る。履歴は 1 手ずつ積む（戻る/進むで復元）。
function showTagNotice() {
  const n=$('#tag-notice'); n.textContent=t('tagsMax'); n.hidden=false; clearTimeout(showTagNotice.timer); showTagNotice.timer=setTimeout(()=>{ n.hidden=true; },4000);
}
function toggleTag(tag, fromDirectory=false) {
  const i=currentTags.findIndex(tg=>tg.key===tag.ja);
  if(i>=0) { currentTags.splice(i,1); logActivity(`untag ${tag.ja}`); }
  else { if(currentTags.length>=MAX_TAGS) { logActivity(t('tagsMax')); showTagNotice(); return; }
    // カテゴリとの AND は、カテゴリ一覧の行から最初のタグを押したときだけ（検索結果・ピックアップからは範囲全体）。付いたあとは維持する。
    if(!currentTags.length) tagPath=fromDirectory ? [...currentPath] : [];
    currentTags.push({key:tag.ja,ja:tag.ja,en:tag.en??null,resolved:true}); logActivity(`tag ${tag.ja}`); }
  const q=$('#query').value.trim();
  if(q || currentTags.length) { currentSearch=q; const url=stateUrl(); if(location.pathname+location.search!==url) history.pushState(null,'',url); showResults(q); }
  else showDirectory(currentPath);
  // 画面（スマホは先頭が見えていないことが多い）を、検索バー（チップ）と結果の先頭が見える位置へ。ホバーは使わずタップで完結する。
  requestAnimationFrame(()=>requestAnimationFrame(()=>{ const row=document.querySelector('.search-row'); if(row.getBoundingClientRect().top<0) row.scrollIntoView({block:'start'}); }));
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
  if(!site.bulk_import && Number.isFinite(registered) && age>=0 && age<30*24*60*60*1000) { const tag=document.createElement('span'); tag.className='new-tag'; tag.textContent=' NEW'; content.append(tag); }
  content.append(p);
  const flag=countryFlag(site); if(flag) { content.insertBefore(flag,p); bindGlobeHover(li,flag,site); }
  const meta=document.createElement('p'); meta.className='site-meta';
  const crawlDate=site.last_crawled_at && !Number.isNaN(Date.parse(site.last_crawled_at)) ? site.last_crawled_at.slice(0,10) : null;
  meta.textContent=crawlDate ? `${t('crawled')}: ${crawlDate}` : '';
  let tags=[]; try { tags=JSON.parse(site.tags_json || '[]'); } catch (_) { tags=[]; }
  for(const tag of tags) { meta.append(' ',tagLink(tag)); }
  if(meta.textContent.trim()) content.append(meta); li.append(icon,content);
  if(showCategories) {
    const row=document.createElement('p'); row.className='site-categories'; row.append(`${t('belongs')}: `);
    const ids=(site.category_ids||[]).filter(id=>categories.some(c=>c.id===id));
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

// ---- 一覧のページング（カーソル方式 API + スクロールで追加読み込み。docs/design/ui.md） ----
// 1 つの一覧（カテゴリ直下 or 検索結果）= 1 つの list。key が同じなら描き直すだけで再取得しない（言語切替など）。
const PAGE_SIZE=40; // src/sites.js の PAGE_SIZE と同じ（?page=N の開始位置と「あと何件」の計算に使う）
let activeList=null; const listIo=(typeof IntersectionObserver!=='undefined') ? new IntersectionObserver(entries=>{ if(activeList && entries.some(e=>e.isIntersecting)) activeList.loadMore(); },{rootMargin:'0px 0px 600px 0px'}) : null;
// 行を消す処理はここ 1 か所。消える行からは pointerleave/focusout が来ないので、置換の前に地球儀のホールドを解く（保持中でなければ地球側は何もしない）。追記読み込みは行を消さないので解除しない。
function clearListRows(ul) { releaseGlobe(); ul.replaceChildren(); }
const pageFromLocation=()=>{ const n=/^\d{1,6}$/.test(new URLSearchParams(location.search).get('page')||'') ? Number(new URLSearchParams(location.search).get('page')) : 1; return n>=1?n:1; };
function startList(opts) {
  if(activeList && activeList.key===opts.key) { activeList.opts={...opts,preserve:activeList.opts.preserve}; activeList.rerender(); return; }
  if(activeList) activeList.stop();
  // 一覧があるときだけ自前で位置を復元する（manual）。一覧の無い画面（トップ・子カテゴリだけの階層）はブラウザ標準の復元（auto）に任せる。履歴の項目ごとの設定なので遷移のたびに切り替える。
  history.scrollRestoration=opts.params ? 'manual' : 'auto';
  if(!opts.params) { activeList=null; clearListRows(opts.ul); opts.foot.replaceChildren(); return; }
  activeList=createList(opts); activeList.start();
}
const USER_MOVE=['wheel','touchstart','keydown','pointerdown'];
function createList(opts) {
  const L={ key:opts.key, opts, items:[], next:null, total:null, ended:false, loading:false, error:false, stopped:false, ctl:null, offset:(opts.startPage-1)*PAGE_SIZE, locKey:location.pathname+location.search, restore:null };
  L.firstLoad=new Promise(r=>{ L.resolveFirst=r; });
  const foot=opts.foot, ul=opts.ul;
  const saved=history.state && history.state.mise; const onMove=()=>{ L.userMoved=true; };
  // 復元中にユーザーが操作したら引き戻さない。監視は復元が終わる/失敗する/一覧を離れるときに外す。
  L.endRestore=()=>{ L.restore=null; for(const ev of USER_MOVE) removeEventListener(ev,onMove); };
  if(saved && saved.key===opts.key) { L.restore=saved; for(const ev of USER_MOVE) addEventListener(ev,onMove,{passive:true}); }
  const status=document.createElement('p'), live=document.createElement('p'), btn=document.createElement('button');
  status.className='list-status'; live.className='visually-hidden'; live.setAttribute('role','status'); live.setAttribute('aria-live','polite');
  btn.type='button'; btn.className='list-more'; btn.addEventListener('click',()=>L.loadMore(true));
  const prev=document.createElement('p'); prev.className='list-prev';
  L.render=()=>{
    const o=L.opts, ps=ul.parentNode.querySelector('#site-pager');
    // 最初の応答が来るまで（または最初の応答が失敗したら）、一覧と SSR のページ番号リンクには触らない（SSR の 1 ページ目を残す）。
    const hold=o.preserve && !L.items.length && (L.loading || L.error);
    if(!hold) { clearListRows(ul); L.items.forEach(s=>renderSite(ul,s,!!o.showCategories)); }
    const empty=!L.items.length && L.ended;
    status.textContent=L.loading ? t('loadingMore') : L.error ? t('listError') : L.ended ? (L.items.length ? t('listEnd')(L.total) : '') : L.total!=null ? t('remaining')(Math.max(0,L.total-L.offset-L.items.length)) : '';
    btn.textContent=t(L.error?'retry':'loadMore'); btn.hidden=!(L.error || (!listIo && !L.ended && !L.loading));
    if(empty) foot.replaceChildren(); else foot.replaceChildren(status,btn,live);
    if(hold) return;
    if(ps) { ps.hidden=true; ps.replaceChildren(); }
    prev.replaceChildren(); if(opts.startPage>1) { const a=document.createElement('a'); a.href=categoryUrl(currentPath,currentScope)+(opts.startPage>2?`&page=${opts.startPage-1}`:''); a.textContent=t('prevPage'); a.rel='prev'; prev.append(a); ul.before(prev); } else prev.remove();
  };
  L.rerender=()=>{ L.render(); };
  L.stop=()=>{ L.stopped=true; L.resolveFirst(); L.endRestore(); L.ctl&&L.ctl.abort(); listIo&&listIo.unobserve(foot); };
  // 復元は 100 件ずつ要求する（?page=N の最初の要求だけは既定の件数のまま。offset の単位が変わるため）
  L.url=()=>{ const limit=L.restore && L.next ? 100 : 0; const u=new URLSearchParams({scope:currentScope}); for(const [k,v] of Object.entries(opts.params)) for(const x of [].concat(v)) u.append(k,x); if(L.next) u.set('cursor',L.next); else if(opts.startPage>1 && !L.items.length) u.set('page',String(opts.startPage)); if(limit) u.set('limit',String(limit)); return '/api/sites?'+u; };
  L.loadMore=async (force)=>{
    if(L.loading || L.stopped || (L.ended && !force)) return;
    L.loading=true; L.error=false; status.textContent=t('loadingMore'); btn.hidden=true; L.ctl=new AbortController();
    try {
      const r=await fetch(L.url(),{signal:L.ctl.signal}); if(!r.ok) throw Error('API');
      const d=await r.json(); if(L.stopped) return;
      const first=!L.items.length && L.total==null;
      L.items.push(...d.items); L.next=d.next_cursor; L.ended=!d.next_cursor; if(d.total!=null) L.total=d.total;
      if(first) { L.render(); if(!opts.preserve) fadeIn(ul); opts.onTotal&&opts.onTotal(L.total); } else { d.items.forEach(s=>renderSite(ul,s,!!opts.showCategories)); }
    } catch (e) { if(L.stopped) return; L.error=true; L.endRestore(); }
    L.loading=false; L.resolveFirst(); if(L.stopped) return;
    status.textContent=L.error ? t('listError') : L.ended ? (L.items.length ? t('listEnd')(L.total) : '') : t('remaining')(Math.max(0,L.total-L.offset-L.items.length));
    btn.textContent=t(L.error?'retry':'loadMore'); btn.hidden=!(L.error || (!listIo && !L.ended));
    live.textContent=L.error ? t('listError') : L.ended && L.items.length ? t('listEnd')(L.total) : '';
    if(L.restore && !L.error) { const w=L.restore; if(L.userMoved) L.endRestore(); else if(!L.ended && L.items.length<w.loaded) return L.loadMore(); else { L.endRestore(); scrollTo(0,w.y); } }
    if(!L.ended && !L.error && listIo) { listIo.unobserve(foot); listIo.observe(foot); }
  };
  L.start=()=>{
    if(!opts.preserve) clearListRows(ul);
    foot.replaceChildren(status,btn,live); status.textContent=t('loadingMore'); btn.hidden=true;
    // preserve のとき SSR の 1 ページ目とページ番号リンクは最初の応答まで残す。
    const ps=document.getElementById('site-pager'); if(ps && !opts.preserve) { ps.hidden=true; ps.replaceChildren(); }
    L.loadMore();
  };
  return L;
}
let saveTimer=0;
addEventListener('scroll',()=>{ clearTimeout(saveTimer); saveTimer=setTimeout(saveListState,150); },{passive:true});
addEventListener('pagehide',saveListState);
function saveListState() { const L=activeList; if(!L || L.restore || L.locKey!==location.pathname+location.search) return; try { history.replaceState({...(history.state||{}),mise:{key:L.key,loaded:L.items.length,y:scrollY}},''); } catch (_) { /* state is best-effort */ } }
// 一覧の入れ替えにだけ短いフェードを付ける（200ms）。画面（範囲・階層・検索語）が変わるときだけ。言語切替の再描画・SSR 初期表示・追記読み込みは対象外。
let lastViewKey=null, activeVt=null, vtPending=false, vtLive=false, vtSeq=0, fromPop=false, animateRedraw=false;
const VT_WAIT_MS=600;
const reducedMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
function fadeIn(el) { if(reducedMotion() || vtLive) return; el.classList.remove('fade-in'); void el.offsetWidth; el.classList.add('fade-in'); }
// 画面全体（列）のフェード。1200px 以上は列の中に position:fixed のコーナーパネル・ピックアップがあり、列ごと opacity を動かすと 0 から点滅するので、一覧側（#directory/#results）だけにかける。
function fadeInView() {
  const col=$('#view-column'), parts=[$('#directory'),$('#results')], wide=matchMedia('(min-width: 1200px)').matches;
  [col,...parts].forEach(e=>e.classList.remove('fade-in'));
  if(wide) parts.forEach(fadeIn); else fadeIn(col);
}
function swapView(viewKey, animate, fn, noVt=false) {
  const changed=viewKey!==lastViewKey; lastViewKey=viewKey;
  // 入れ替え待ちの遷移がある間の同じ画面の再描画（戻る/進む・範囲切替後の applyLanguage）は、待機中の描画が行うので何もしない。
  if(!changed && vtPending) return;
  const seq=++vtSeq; // 後から別の画面が決まったら、古い遷移の描画コールバックは何もしない（URL と画面を食い違わせない）
  if(changed) vtPending=false;
  if(!animate || !changed || reducedMotion()) { fn(); return; }
  if(activeVt) { try { activeVt.skipTransition(); } catch (_) { /* already finished */ } }
  if(document.startViewTransition && !noVt) {
    vtPending=true;
    // 新しい画面の最初のページ（上限 600ms）を待ってから描画を確定する。旧→空→新の二段にしない。時間切れなら行は後からフェードインする。
    const vt=activeVt=document.startViewTransition(async()=>{
      if(seq!==vtSeq) return;
      vtPending=false; vtLive=true;
      try { fn(); const L=activeList; if(L && L.firstLoad) await Promise.race([L.firstLoad,new Promise(r=>setTimeout(r,VT_WAIT_MS))]); } finally { vtLive=false; }
    });
    vt.ready.catch(()=>{}); vt.updateCallbackDone.catch(()=>{}); // skipTransition() で捨てられた遷移の拒否を未捕捉にしない
    vt.finished.catch(()=>{}).finally(()=>{ if(activeVt===vt) activeVt=null; });
    return;
  }
  fn(); fadeInView();
}
let viewShown=false;
function showDirectory(path, updateHistory=true, preserveSsr=false) {
  const first=!viewShown; viewShown=true;
  currentPath=path; currentSearch=null; currentTags=[]; tagPath=[]; renderChips(); // 描画が遅れても、以降の再描画（applyLanguage 等）は新しい画面を指す
  // URL は遷移の描画を待たず同期で確定する（遷移中に戻るを押しても URL と画面が食い違わない）
  if(updateHistory && location.pathname+location.search!==categoryUrl(path)) history.pushState(null,'',categoryUrl(path));
  swapView(`${currentScope}|${path.join('/')}|`, (!keepRequestOpen || animateRedraw) && !preserveSsr && !first, ()=>showDirectoryNow(path, updateHistory, preserveSsr), fromPop && !path.length); // 戻る/進むで一覧の無い画面（ブラウザ標準のスクロール復元）へ戻るときは、描画を同期にする
}
function showDirectoryNow(path, updateHistory, preserveSsr) {
  if(!keepRequestOpen) setRequestOpen(false);
  currentPath=path; currentSearch=null; currentTags=[]; tagPath=[]; renderChips(); updateTitle(); $('#directory').hidden=false; $('#results').hidden=true;
  const crumbs=$('#breadcrumbs'), box=$('#categories'), siteBox=$('#directory-sites'); crumbs.replaceChildren(); box.replaceChildren();
  if(path.length) {
    crumbs.append(link(t('top'),[],()=>{logActivity('cd /');showDirectory([]);resetGlobe();}));
    path.forEach((id,i)=>{ crumbs.append(' › '); const c=categories.find(x=>x.id===id); if(i===path.length-1) crumbs.append(document.createTextNode(name(c))); else crumbs.append(link(name(c),path.slice(0,i+1),()=>{logActivity(`cd /${path.slice(0,i+1).join('/')}`);showDirectory(path.slice(0,i+1));})); });
  }
  const children=categories.filter(c=>c.parent_id===(path.at(-1)??null));
  for(const c of children) { const next=[...path,c.id], div=document.createElement('div'),h=document.createElement('h2'); div.className='category'+(c.site_count?'':' is-empty'); h.append(link(`${name(c)} (${c.site_count})`,next,()=>{logActivity(`cd /${next.join('/')}`); showDirectory(next);})); div.append(h); if(!path.length){const p=document.createElement('p');p.className='sub';p.textContent=categories.filter(x=>x.parent_id===c.id).map(name).join(' ');div.append(p);} box.append(div); }
  $('#picks').hidden=false; renderPick();
  const pageNo=path.length ? pageFromLocation() : 1;
  startList({ key:`${currentScope}|${path.join('/')}|${pageNo}`, ul:siteBox, foot:$('#directory-foot'), params:path.length?{category:path.at(-1)}:null, startPage:pageNo, preserve:!!preserveSsr,
    onTotal:total=>{ $('#sample-label').hidden=!total; $('#directory-empty').hidden=!!children.length || !!total || !path.length; } });
  if(!path.length) { $('#sample-label').hidden=true; $('#directory-empty').hidden=true; }
}
function renderPick() {
  const list=$('#pick-list');
  // 入れ替えで行が消えても、フォーカス位置と地球儀のホールドを残さない（pointerleave/focusout は来ない）。
  const links=[...list.querySelectorAll('a')], focusIdx=links.indexOf(document.activeElement);
  if(focusIdx>=0 || list.matches(':hover')) window.dispatchEvent(new CustomEvent('mise-fly-release'));
  list.replaceChildren();
  if(!pickedSites.length) return;
  renderSite(list,pickedSites[pickIndex % pickedSites.length],true);
  if(focusIdx>=0){ const nl=list.querySelectorAll('a'); nl[Math.min(focusIdx,nl.length-1)]?.focus({preventScroll:true}); }
}
const picks=$('#picks');
if(typeof IntersectionObserver!=='undefined') new IntersectionObserver(entries=>{picksVisible=entries[0].isIntersecting;}).observe(picks); else picksVisible=true;
setInterval(()=>{
  if(pickedSites.length<2 || picks.hidden || !picksVisible || document.hidden || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  pickIndex=(pickIndex+1)%pickedSites.length;
  renderPick();
},8000);
function showResults(query) {
  const first=!viewShown; viewShown=true;
  currentSearch=query; renderChips();
  swapView(`${currentScope}||${query}|${currentTags.map(tg=>tg.key).join('\n')}|${currentTags.length?tagPath.join('/'):''}`, (!keepRequestOpen || animateRedraw) && !first, ()=>showResultsNow(query));
}
function setResultTitle(query) {
  const tagText=currentTags.map(tg=>`#${tagLabel(tg)}`).join(' ');
  $('#result-title').textContent=currentTags.length ? (query ? `${t('results')(query)} ${tagText}` : t('taggedSites')(tagText)) : query?t('results')(query):t('all');
}
function showResultsNow(query) {
  if(!keepRequestOpen) setRequestOpen(false);
  currentSearch=query; $('#directory').hidden=true; $('#picks').hidden=false; $('#results').hidden=false;
  // タグがあるときは現在のカテゴリ階層とも AND（タグ押下でカテゴリは変えない）。タグなしの検索は従来どおり範囲全体。
  const tagKeys=currentTags.map(tg=>tg.key), cat=tagKeys.length ? tagPath.at(-1) : undefined;
  setResultTitle(query); $('#empty').hidden=true;
  startList({ key:`${currentScope}|?q|${query}|${tagKeys.join('\n')}|${cat??''}`, ul:$('#result-list'), foot:$('#result-foot'), params:{q:query,...(tagKeys.length?{tag:tagKeys}:{}),...(cat?{category:cat}:{})}, startPage:1, showCategories:true,
    onTotal:total=>{ $('#empty').hidden=!!total; if(activeList) resolveTagLabels(activeList.items); } });
}
function updateSearch() {
  const q=$('#query').value.trim();
  if(q || currentTags.length) { showResults(q); if(currentTags.length) history.replaceState(history.state,'',stateUrl()); } else showDirectory(currentPath, false);
}
let reqStatus = null;
let searchTimer=0; const updateSearchSoon=()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(updateSearch,250); };
let composing = false;
$('#query').addEventListener('compositionstart',()=>{composing=true;});
$('#query').addEventListener('compositionend',()=>{composing=false;updateSearchSoon();});
$('#query').addEventListener('input',event=>{if(!composing && !event.isComposing) updateSearchSoon();});
$('#search-form').addEventListener('submit',e=>{e.preventDefault();clearTimeout(searchTimer);if(composing) return;const q=$('#query').value.trim();if(q) {logActivity(`search ${q}`);showResults(q);if(currentTags.length) history.replaceState(history.state,'',stateUrl());} else if(currentTags.length) {showResults(q);history.replaceState(history.state,'',stateUrl());} else showDirectory(currentPath,false);});
$('#clear').addEventListener('click',e=>{e.preventDefault();showDirectory(currentPath);}); // カテゴリへ戻るとタグのチップも外れる
$('#home-link').addEventListener('click',e=>{if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)return;e.preventDefault();logActivity('cd /');$('#query').value='';showDirectory([]);resetGlobe();});
function renderReqStatus(){const st=$('#req-status');if(!reqStatus){st.textContent='';return;}st.textContent=t(reqStatus.key);st.dataset.state=reqStatus.state;}
$('#request-form').addEventListener('submit',async e=>{
  e.preventDefault(); const f=e.currentTarget, st=$('#req-status'), btn=$('#req-submit');
  const show=(key,state)=>{reqStatus={key,state};renderReqStatus();};
  if(btn.getAttribute('aria-disabled')==='true') return; // 二重送信ガード(disabled にするとフォーカスが外れて Esc が効かなくなる)
  btn.setAttribute('aria-disabled','true'); show('reqSending','info');
  try {
    const res=await fetch('/api/requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:f.type.value,url:f.url.value,reason:f.reason.value,website:f.website.value})});
    if(res.ok){ show('reqOk','ok'); f.url.value=''; f.reason.value=''; }
    else { const j=await res.json().catch(()=>({})); show(t('reqErr_'+j.error)?'reqErr_'+j.error:(res.status===429?'reqErr_rate_limited':'reqErr_other'),'error'); }
  } catch(_) { show('reqErr_other','error'); }
  finally { btn.removeAttribute('aria-disabled'); }
});
// 申請フォームの開閉。開いている間はカテゴリ一覧(パンくず・カテゴリ)を隠す(CSS の .req-open。再描画されても取り残されない)。
function setRequestOpen(open, focus) {
  const m=$('#recommend'), link=$('#recommend-link');
  m.hidden=!open; link.setAttribute('aria-expanded',String(open)); document.documentElement.classList.toggle('req-open',open);
  if(focus) (open?$('#req-type'):link).focus();
}
$('#recommend-link').addEventListener('click',e=>{e.preventDefault();setRequestOpen($('#recommend').hidden,true);});
document.addEventListener('keydown',e=>{ if(e.key==='Escape' && !e.defaultPrevented && !$('#recommend').hidden){ e.preventDefault(); setRequestOpen(false,true); } });
// 範囲切替。国旗/国名のリンクは通常の <a href="/us/"> で、修飾キー付き・中クリックはブラウザ任せ。
const SCOPE_ICON = '<svg class="scope-globe" aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2"/><ellipse cx="8" cy="8" rx="2.8" ry="6.2"/><path d="M1.8 8h12.4"/></svg>';
// 国名: 表（日英）→ Intl.DisplayNames → コードの順。表に無い国（MC・RO・TD…）も読み上げで国名になる。
const countryLabel = code => { const e=window.MISE_COUNTRIES?.[code]?.[language]; if(e) return e; try { return new Intl.DisplayNames([language],{type:'region'}).of(code)||code; } catch (_) { return code; } };
const scopeAria = (label,count) => language==='ja' ? `${label} ${count}件` : `${label}, ${count} ${Number(count)===1?'site':'sites'}`;
const codeIcon = code => { const el=document.createElement('span'); el.className='scope-code'; el.setAttribute('aria-hidden','true'); el.textContent=code; return el; };
function scopeItem(code, label, count, icon) {
  const li=document.createElement('li'), a=document.createElement('a');
  a.className='scope-item'; a.href=`/${code}/`; if(code===currentScope) a.setAttribute('aria-current','page');
  if(icon instanceof Node) a.append(icon); else a.insertAdjacentHTML('beforeend',icon);
  a.setAttribute('aria-label',scopeAria(label,count)); a.title=label;
  const c=document.createElement('span'); c.className='scope-count'; c.textContent=String(count);
  a.append(c);
  a.addEventListener('click',e=>{ if(e.button!==0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); chooseScope(code); });
  li.append(a); return li;
}
function renderScopeNav() {
  const nav=$('#scope-nav'); if(!nav) return;
  // 作り直すとフォーカスが BODY に落ちるので、範囲ナビ内にあったなら同じ項目へ戻す。
  const fa=document.activeElement, hadFocus=nav.contains(fa), focusMore=hadFocus && fa.classList?.contains('scope-more');
  const list=document.createElement('ul'); list.className='scope-list';
  list.append(scopeItem('all',t('allWorld'),scopes.total,SCOPE_ICON));
  const extras=[];
  scopes.countries.forEach((r,i)=>{
    const code=r.code.toLowerCase();
    let icon='';
    if(window.MISE_FLAGS?.includes(r.code)) { const img=document.createElement('img'); img.className='scope-flag'; img.alt=''; img.width=16; img.height=12; img.src=`/flags/${code}.svg`; img.addEventListener('error',()=>img.replaceWith(codeIcon(r.code))); icon=img; }
    else icon=codeIcon(r.code);
    const li=scopeItem(code,countryLabel(r.code),r.count,icon);
    if(i>=INLINE_SCOPES) { li.hidden=!scopesExpanded && code!==currentScope; extras.push(li); }
    list.append(li);
  });
  const parts=[list];
  if(extras.length) {
    if(extras.some(li=>li.querySelector('[aria-current]'))) { scopesExpanded=true; extras.forEach(li=>{li.hidden=false;}); }
    const more=document.createElement('button'); more.type='button'; more.className='scope-more'; more.setAttribute('aria-expanded',String(scopesExpanded));
    const label=scopesExpanded?t('lessScopes'):t('moreScopes'); // 文字は出さず、アイコン(⋯ / ▴)+ aria-label + title
    more.setAttribute('aria-label',label); more.title=label;
    more.innerHTML=scopesExpanded ? '<svg class="scope-more-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M3.5 10.5 8 6l4.5 4.5"/></svg>' : '<svg class="scope-more-icon" aria-hidden="true" viewBox="0 0 16 16"><circle cx="3.5" cy="8" r="1.1"/><circle cx="8" cy="8" r="1.1"/><circle cx="12.5" cy="8" r="1.1"/></svg>';
    more.addEventListener('click',()=>{ scopesExpanded=!scopesExpanded; renderScopeNav(); });
    parts.push(more);
  }
  nav.replaceChildren(...parts);
  if(hadFocus) (focusMore ? nav.querySelector('.scope-more') : nav.querySelector('[aria-current]'))?.focus();
  $('#home-link').href=categoryUrl([]);
  // 地球儀へ「選べる国」と現在の範囲を伝える（目印の点と、背景クリックで国を選ぶ判定に使う）。
  window.MISE_SCOPE={ current:currentScope, countries:scopes.countries.map(r=>({code:r.code,lat:window.MISE_COUNTRIES?.[r.code]?.lat,lon:window.MISE_COUNTRIES?.[r.code]?.lon})) };
  window.dispatchEvent(new CustomEvent('mise-scope-changed'));
}
// 範囲ごとのデータ（カテゴリ・サイト・活動）を取得する。同じ範囲は一度だけ（ページの寿命の間）。失敗は例外。
function loadScopeData(scope) {
  if(!scopeCache.has(scope)) {
    const q=`?scope=${encodeURIComponent(scope)}`;
    const p=Promise.all([fetch(`/api/categories${q}`),fetch(`/api/picks${q}`).catch(()=>null),fetch(`/api/activity${q}`).catch(()=>null)]).then(async ([a,b,stats])=>{
      if(!a.ok) throw Error('API');
      return { categories:await a.json(), picks:b && b.ok ? await b.json() : [], stats:stats && stats.ok ? await stats.json() : null };
    });
    scopeCache.set(scope,p); p.catch(()=>scopeCache.delete(scope));
  }
  return scopeCache.get(scope);
}
// 範囲のデータを画面の状態に反映する（URL・描画は呼び出し側）。後から来た別の切替が先に完了していたら false。
async function switchData(scope) {
  const token=++scopeToken, data=await loadScopeData(scope);
  if(token!==scopeToken) return false;
  currentScope=scope; categories=data.categories; pickedSites=data.picks; activityStats=data.stats; pickIndex=0;
  return true;
}
// 切替後も、カテゴリ階層と検索語は維持する（docs/design/ui.md）。
async function chooseScope(scope) {
  if(scope===currentScope) return;
  logActivity(`scope ${scope}`);
  let ok=false; try { ok=await switchData(scope); } catch (_) { logActivity('scope error'); return; }
  if(!ok) return;
  history.pushState(null,'',stateUrl(scope));
  renderScopeNav(); renderStats(); setRequestOpen(false); applyLanguage(true);
  globeForScope(scope);
}
// 地球儀の背景クリック/タップ。選べる国（サイトがある国）だけ範囲にする。
window.addEventListener('mise-globe-pick',e=>{ const code=String(e.detail?.code||''); if(scopes.countries.some(r=>r.code===code)) chooseScope(code.toLowerCase()); });
function restoreLocation() {
  const sp=new URLSearchParams(location.search), q=sp.get('q');
  // ?tag=（複数は AND）。空・重複は無視し、5 個を超える分は捨てる。タグがあるときはカテゴリ階層（?category=）とも AND。
  const keys=[...new Set(sp.getAll('tag').map(x=>x.trim()).filter(x=>x && [...x].length<=100))].slice(0,MAX_TAGS);
  if(keys.length) {
    const path=[]; let parent=null;
    for(const part of sp.getAll('category')) { const c=categories.find(x=>x.parent_id===parent && (x.id===part || x.name_ja===part)); if(!c) { path.length=0; break; } path.push(c.id); parent=c.id; }
    restoredOnce=true; currentPath=path; tagPath=[...path]; currentTags=keys.map(k=>({key:k,ja:k,en:null,resolved:false}));
    $('#query').value=(q||'').trim(); showResults((q||'').trim());
    // 6 個以上・空・重複・不正なカテゴリなどは切り詰めた形に URL を正規化する（履歴は増やさない）。
    const norm=stateUrl(); if(location.pathname+location.search!==norm && (sp.getAll('tag').length!==keys.length || sp.getAll('category').length!==path.length)) history.replaceState(history.state,'',norm);
    return;
  }
  if(q && q.trim()){ currentTags=[]; $('#query').value=q; showResults(q.trim()); return; }
  const first=!restoredOnce; restoredOnce=true;
  $('#query').value='';
  const raw=new URLSearchParams(location.search).getAll('category'); let parent=null; const path=[];
  for(const part of raw) { const c=categories.find(x=>x.parent_id===parent && (x.id===part || x.name_ja===part)); if(!c){ history.replaceState(null,'',categoryUrl([]));showDirectory([],false);return; } path.push(c.id); parent=c.id; }
  if(raw.some((part,i)=>part!==path[i])) history.replaceState(null,'',categoryUrl(path));
  showDirectory(path,false,first);
}
let restoredOnce=false;
// 戻る/進むは範囲も復元する（範囲が変わるときはそのデータを読んでから階層・検索を復元）。
window.addEventListener('popstate',async()=>{
  const scope=scopeFromLocation(), changed=scope!==currentScope;
  if(changed) { try { if(!(await switchData(scope))) return; } catch (_) { return; } renderScopeNav(); renderStats(); }
  fromPop=true; try { restoreLocation(); } finally { fromPop=false; }
  applyLanguage();
  if(changed) globeForScope(scope);
});
(async()=>{
  try {
    currentScope=scopeFromLocation();
    applyStaticLanguage();
    const [, sc]=await Promise.all([switchData(currentScope), fetch('/api/scopes').then(r=>r.ok?r.json():null).catch(()=>null)]);
    if(sc) scopes=sc;
    restoreLocation(); applyLanguage();
  } catch (_) { if(!$('#categories').children.length && !$('#directory-sites').children.length) $('#directory').textContent=t('error'); }
  finally { endI18nPending(); }
})();

// ロゴ: S を M と同じ表示幅にし、4文字の見た目の間隔を均等にそろえる（transform はレイアウトに効かないので余白で補正）
(document.fonts?document.fonts.ready:Promise.resolve()).then(()=>window.__miseLogo&&window.__miseLogo());
