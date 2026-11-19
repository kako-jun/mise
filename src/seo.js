import { securityHeaders, pageCsp, inlineScriptHashes } from './security.js';
import { querySites, parsePage, cleanTags, PAGE_SIZE, ALL_SCOPE } from './sites.js';
// 国名の表（ブラウザの app.js・globe.js と共有する単一の表）。読み込むと globalThis.MISE_COUNTRIES / MISE_FLAGS が定義される。
import '../public/countries.js';
// 区画（都道府県・州）の名前の表（ブラウザの app.js・globe.js と共有）。読み込むと globalThis.MISE_REGIONS が定義される。
import '../public/regions.js';
// リンク先のホスト名の表示（ブラウザの app.js・feed.js と共有。読み込むと globalThis.MiseSiteHost が定義される）。
import '../public/site-host.js';
// SEO 用ヘルパー: カテゴリパスの解決、canonical、sitemap、"/" へのメタ情報・本文の注入。
export const ORIGIN = 'https://mise.llll-ll.com';
// 既定言語は英語（app.js の初期言語と一致）。サイトの紹介文は日本語のみのため「(Japanese)」を添える。
const DEFAULT_TITLE = 'MISE — A directory search engine for today.';
const DEFAULT_DESC = 'For those tired of search results dominated by ads and major sites. Discover independent websites you have yet to find. MISE is a directory search engine.';

const esc = s => String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

// 範囲（scope）: 国コード（小文字 2 文字。jp, us…）、区画（国コード-区画。jp-13, us-ca…。URL は /jp/13/ /us/ca/）、または all。ルート `/` は既定範囲として扱う（docs/design/seo.md）。
export const DEFAULT_SCOPE = 'jp';
// API/URL の範囲指定を解釈する。省略・空・all は全世界（cc = null, region = null）、小文字 2 文字は国、小文字 2 文字 + ハイフン + 英数 1〜3 文字は区画
// （region は sites.region_code と同じ大文字の形 JP-13。cc はその国）、それ以外は null（不正）。区画が実在するか（サイトがあるか）は呼び出し側が scopes で確かめる。
export function parseScope(raw) {
  if (raw == null || raw === '' || raw === 'all') return { scope: 'all', cc: null, region: null };
  if (/^[a-z]{2}$/.test(raw)) return { scope: raw, cc: raw.toUpperCase(), region: null };
  if (/^[a-z]{2}-[a-z0-9]{1,3}$/.test(raw)) return { scope: raw, cc: raw.slice(0, 2).toUpperCase(), region: raw.toUpperCase() };
  return null;
}
// 範囲名 -> URL のパス。jp -> /jp/、jp-13 -> /jp/13/、all -> /all/。
export const scopePath = scope => `/${scope.replace('-', '/')}/`;
// page >= 2 のときだけ `page=N` を付ける（1 ページ目の URL は付けない形に正規化）。
export const categoryHref = (path, scope = DEFAULT_SCOPE, page = 1) => {
  const qs = [...path.map(id => 'category=' + encodeURIComponent(id)), ...(page > 1 ? ['page=' + page] : [])];
  return scopePath(scope) + (qs.length ? '?' + qs.join('&') : '');
};
// ページ番号リンク（JS なし・クローラ用）。先頭・末尾・現在地の前後 2 ページ + 前へ/次へ。
function pagerHtml(path, scope, page, pages) {
  const a = (n, label, rel, cur) => `<a href="${esc(categoryHref(path, scope, n))}"${rel ? ` rel="${rel}"` : ''}${cur ? ' aria-current="page"' : ''}>${label}</a>`;
  const nums = [...new Set([1, page - 2, page - 1, page, page + 1, page + 2, pages].filter(n => n >= 1 && n <= pages))].sort((x, y) => x - y);
  let out = page > 1 ? `<span class="pager-prev">${a(page - 1, '‹ Prev', 'prev')}</span> ` : '';
  let last = 0;
  for (const n of nums) { if (n - last > 1) out += '… '; out += (n === page ? `<span class="pager-num">${a(n, n, '', true)}</span>` : `<span class="pager-num">${a(n, n)}</span>`) + ' '; last = n; }
  if (page < pages) out += `<span class="pager-next">${a(page + 1, 'Next ›', 'next')}</span>`;
  return out;
}

// サイトが 1 件以上ある国（件数の多い順）・区画（コード順）と全体の件数。範囲として選べる国・区画 = ここに出るものだけ。
export async function loadScopes(db) {
  // 集計表 scope_counts（migration 0022。トリガが sites に追従させる派生物）を読む。数百行以下。
  const { results } = await db.prepare(`SELECT scope, n FROM scope_counts WHERE n > 0 ORDER BY n DESC, scope`).all();
  const total = results.find(r => r.scope === ALL_SCOPE)?.n ?? 0;
  // regions: 区画（JP-13 など）。サイトが 1 件以上ある区画だけ。コードの昇順（JP-01〜JP-47 の順）。
  const regions = results.filter(r => /^[A-Z]{2}-[A-Z0-9]{1,3}$/.test(r.scope)).map(r => ({ code: r.scope, count: r.n })).sort((a, b) => a.code < b.code ? -1 : a.code > b.code ? 1 : 0);
  return { total, countries: results.filter(r => /^[A-Z]{2}$/.test(r.scope)).map(r => ({ code: r.scope, count: r.n })), regions };
}
const countryName = code => {
  const e = globalThis.MISE_COUNTRIES?.[code]?.en;
  if (e) return e;
  try { return new Intl.DisplayNames(['en'], { type: 'region' }).of(code) || code; } catch { return code; }
};
// 区画の英語名。表（regions.js）に無ければコード。
const regionName = code => globalThis.MISE_REGIONS?.[code]?.en ?? code;
// 範囲の表示名（英語）。区画は「区画名, 国名」。
const placeName = ({ cc, region }) => region ? `${regionName(region)}, ${countryName(cc)}` : cc ? countryName(cc) : null;
const flagUrl = code => (globalThis.MISE_FLAGS ?? []).includes(code) ? `/flags/${code.toLowerCase()}.svg` : null;

// `q` か `tag` が付いた URL は検索結果扱い（noindex）。tag は不正（6 個以上・101 文字以上で cleanTags が null）でも、付いた時点で対象にする。
export const isSearchQuery = sp => !!sp.get('q')?.trim() || sp.has('tag');
// `?view=feed`(縦フィードの試作ビュー。#52)。検索結果と同じく noindex にする（canonical は view を含まないので通常ページを指す）
export const isFeedView = sp => sp.get('view') === 'feed';
export const wantsNoindex = sp => isSearchQuery(sp) || isFeedView(sp);

// app.js の restoreLocation と同じ解決規則（id または name_ja。解決できない時点で null）
export function resolvePath(categories, raw) {
  let parent = null;
  const path = [];
  for (const part of raw) {
    const c = categories.find(x => x.parent_id === parent && (x.id === part || x.name_ja === part));
    if (!c) return null;
    path.push(c.id);
    parent = c.id;
  }
  return path;
}

// cc を渡すと、その範囲のサイトだけを数える（空のカテゴリも 0 件で返す）。国コード（JP）か区画コード（JP-13）。null は全世界。
// 件数は集計表 category_counts（migration 0022。そのカテゴリ自身と全子孫の所属サイト数、重複なし。旧: 再帰 CTE）から読む。
export async function loadCategories(db, cc = null) {
  const { results } = await db.prepare(`SELECT c.id, c.parent_id, c.name_ja, c.name_en, c.sort_order, COALESCE(k.n, 0) AS site_count FROM categories c LEFT JOIN category_counts k ON k.category_id = c.id AND k.scope = ?1 ORDER BY c.sort_order, c.id`).bind(cc ?? ALL_SCOPE).all();
  return results;
}

function pathOf(categories, id) {
  const path = [];
  for (let c = categories.find(x => x.id === id); c; c = categories.find(x => x.id === c.parent_id)) path.unshift(c.id);
  return path;
}

// 範囲ごと（all と、サイトが 1 件以上ある国・区画）に、範囲トップと、その範囲内に 1 サイト以上あるカテゴリの URL を載せる。`/` は `/jp/` の別名なので載せない。
export const SITEMAP_MAX_URLS = 50000;
export async function sitemapXml(db, maxUrls = SITEMAP_MAX_URLS) {
  const scopes = await loadScopes(db);
  const urls = [ORIGIN + '/about/']; // アバウトページ（静的な説明ページ。索引に載せてよい。#118）
  // カテゴリ一覧と全範囲の件数を 1 回ずつ読む（範囲ごとに loadCategories を実行しない。#126）。
  const [cats, counts] = await db.batch([
    db.prepare(`SELECT id, parent_id FROM categories ORDER BY sort_order`),
    db.prepare(`SELECT category_id, scope FROM category_counts WHERE n > 0`),
  ]);
  const categories = cats.results, nonEmpty = new Map();
  for (const r of counts.results) { if (!nonEmpty.has(r.scope)) nonEmpty.set(r.scope, new Set()); nonEmpty.get(r.scope).add(r.category_id); }
  for (const [scope, key] of [['all', ALL_SCOPE], ...scopes.countries.map(r => [r.code.toLowerCase(), r.code]), ...scopes.regions.map(r => [r.code.toLowerCase(), r.code])]) {
    urls.push(ORIGIN + categoryHref([], scope));
    for (const c of categories) if (nonEmpty.get(key)?.has(c.id)) urls.push(ORIGIN + categoryHref(pathOf(categories, c.id), scope));
  }
  // 1 ファイルの上限（sitemaps.org: 50,000 URL）。超えたら黙って切り捨てず例外にする（分割は sitemap index へ。docs/design/seo.md）。
  if (urls.length > maxUrls) throw new Error(`sitemap has ${urls.length} URLs (limit ${maxUrls}): split into a sitemap index`);
  const body = urls.map(u => `<url><loc>${esc(u)}</loc></url>`).join('');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${body}</urlset>\n`;
}

// 範囲切替の HTML（SSR。JS が起動すると app.js が同じ内容に描き直す）。現在の範囲は aria-current="page"。
export function scopeNavHtml(scopes, scope) {
  // 国名は見える文字にせず aria-label と title にだけ置く（アイコンで識別する。#76）。
  const item = (code, href, label, count, icon) => `<li><a class="scope-item" href="${esc(href)}" aria-label="${esc(label + ", " + count + (count === 1 ? " site" : " sites"))}" title="${esc(label)}"${code === scope ? ' aria-current="page"' : ''}>${icon}<span class="scope-count">${count}</span></a></li>`;
  const globe = '<svg class="scope-globe" aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2"/><ellipse cx="8" cy="8" rx="2.8" ry="6.2"/><path d="M1.8 8h12.4"/></svg>';
  const items = [item('all', '/all/', 'All world', scopes.total, globe)];
  for (const r of scopes.countries) {
    const flag = flagUrl(r.code);
    items.push(item(r.code.toLowerCase(), `/${r.code.toLowerCase()}/`, countryName(r.code), r.count, flag ? `<img class="scope-flag" src="${esc(flag)}" alt="" width="16" height="12">` : `<span class="scope-code" aria-hidden="true">${esc(r.code)}</span>`));
  }
  return `<ul class="scope-list">${items.join('')}</ul>${regionSelectHtml(scopes, scope)}`;
}
// JS なしでも・クローラも区画のページへ辿れるよう、同じ区画を通常のリンク（/jp/13/ 形式）で並べた折りたたみ。サイトのある区画だけ（無い区画は 404 なので出さない）。
// JS が起動すると app.js が範囲ナビごと描き直し、select だけになる。
function regionLinksHtml(cc, regions) {
  return `<details class="region-links"><summary>Regions of ${esc(countryName(cc))}</summary><ul>${regions.map(r => `<li><a href="${esc(scopePath(r.code.toLowerCase()))}">${esc(regionName(r.code))} (${r.count})</a></li>`).join('')}</ul></details>`;
}
// 国（または国の中の区画）が現在の範囲で、その国に区画があるときの区画選択（SSR。app.js が同じ内容に描き直す）。value は範囲名（jp / jp-13）。
// 先頭は国全体（区画で絞らない）。区画が無い範囲（all・区画を持たない国）では出さない。
export function regionSelectHtml(scopes, scope) {
  const sc = parseScope(scope);
  if (!sc?.cc) return '';
  const regions = (scopes.regions ?? []).filter(r => r.code.startsWith(sc.cc + '-'));
  if (!regions.length) return '';
  const country = scopes.countries.find(r => r.code === sc.cc);
  const opt = (value, label, count) => `<option value="${esc(value)}"${value === scope ? ' selected' : ''}>${esc(label)} (${count})</option>`;
  return `<div class="region-row"><select class="region-select" aria-label="Region">${opt(sc.cc.toLowerCase(), 'All of ' + countryName(sc.cc), country?.count ?? 0)}${regions.map(r => opt(r.code.toLowerCase(), regionName(r.code), r.count)).join('')}</select>${regionLinksHtml(sc.cc, regions)}</div>`;
}

// 範囲ページ（/ , /jp/ , /us/ , /all/）の HTML を、URL（?category= / ?q=）に応じたメタ情報と JS なしで読める本文に書き換える。
// scope は parseScope で検証済みの範囲名、scopes は loadScopes の結果。`/` は scope = jp で呼ばれ、canonical は `/jp/…`。
export async function renderIndex(c, assetResponse, scope, scopes) {
  const url = new URL(c.req.url);
  const { cc, region } = parseScope(scope);
  const categories = await loadCategories(c.env.DB, region ?? cc);
  const raw = url.searchParams.getAll('category');
  const path = resolvePath(categories, raw) ?? [];
  const current = categories.find(x => x.id === path.at(-1));
  const tags = cleanTags(url.searchParams.getAll('tag')) ?? [];
  const isSearch = isSearchQuery(url.searchParams); // タグ絞り込みも検索結果と同じ扱い（noindex・ページ番号なし）
  const page = path.length && !isSearch ? parsePage(url.searchParams.get('page')) : 1;
  if (page === null) return new Response('Not Found', { status: 404, headers: { 'Cache-Control': 'no-store' } });

  // 範囲ごとに title / description を変える（all は従来どおり）。国は英語名。
  const place = placeName({ cc, region }), inPlace = place && (/^United /.test(place) ? 'the ' : '') + place;
  let title = place ? `MISE — ${place}: A directory search engine for today.` : DEFAULT_TITLE;
  let desc = place
    ? `Independent websites from ${inPlace} on MISE, a directory search engine. For those tired of search results dominated by ads and major sites.`
    : DEFAULT_DESC;
  if (current) {
    const names = path.map(id => categories.find(x => x.id === id).name_en);
    title = `${names.join(' › ')}${place ? ` (${place})` : ''} — MISE`;
    desc = `${names.join(' › ')} on MISE${place ? ` in ${inPlace}` : ''}: ${current.site_count} ${current.site_count === 1 ? 'site' : 'sites'}. Discover independent websites you have yet to find.`;
  }

  // 本文（JS が起動すると置き換わる）
  const children = categories.filter(x => x.parent_id === (path.at(-1) ?? null));
  const crumbs = path.length
    ? `<a href="${esc(categoryHref([], scope))}">Home</a>` + path.map((id, i) => {
        const name = esc(categories.find(x => x.id === id).name_en);
        return ' › ' + (i === path.length - 1 ? name : `<a href="${esc(categoryHref(path.slice(0, i + 1), scope))}">${name}</a>`);
      }).join('')
    : '';
  const cats = children.map(ch => {
    const sub = path.length ? '' : `<p class="sub">${categories.filter(x => x.parent_id === ch.id).map(x => `<span>${esc(x.name_en)}</span>`).join(' / ')}</p>`;
    return `<div class="category${ch.site_count ? '' : ' is-empty'}"><h2><a href="${esc(categoryHref([...path, ch.id], scope))}">${esc(ch.name_en)} (${ch.site_count})</a></h2>${sub}</div>`;
  }).join('');
  let siteItems = '', pagerBody = '', pages = 1;
  if (path.length || tags.length) {
    const { items, total } = await querySites(c.env.DB, { cc, region, category: path.at(-1) ?? null, tags, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE });
    pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (page > 1 && !items.length) return new Response('Not Found', { status: 404, headers: { 'Cache-Control': 'no-store' } });
    // 行の構造はブラウザ描画（app.js の renderSite）と同じ: 1 行目（.site-head = 中点・サイト名・国・ホスト名）、説明文は次の行の左端から。
    siteItems = items.map(s => { const host = globalThis.MiseSiteHost(s.url), place = /^[A-Z]{2}$/.test(s.country_code ?? '') ? ` [${/^[A-Z]{2}-[A-Z0-9]{1,3}$/.test(s.region_code ?? '') && s.region_code.startsWith(s.country_code) ? s.region_code : s.country_code}]` : ''; return `<li><div class="site-head"><span class="site-bullet" aria-hidden="true"></span><span class="site-title"><a href="${esc(/^https?:\/\//i.test(s.url) ? s.url : '#')}">${esc(s.name_en || s.name)}</a>${place}${host ? ` <span class="site-host">${esc(host)}</span>` : ''}</span></div><p>${s.description_en ? esc(s.description_en) : esc(s.description_ja) + ' (Japanese)'}</p></li>`; }).join('');
    if (isSearch) pages = 1; // 検索結果はページ番号なし（noindex。エンドレススクロールのみ）
    if (pages > 1) pagerBody = pagerHtml(path, scope, page, pages);
  }
  // ページ番号つきの URL は自己参照の canonical（2 ページ目以降を 1 ページ目に寄せない）。title に「Page N」を添えて区別する。
  const canonical = ORIGIN + categoryHref(isSearch ? [] : path, scope, page);
  if (page > 1) { title = `${title.replace(/ — MISE$/, '')} — Page ${page} — MISE`; desc = `${desc} Page ${page} of ${pages}.`; }

  const setContent = v => ({ element: e => e.setAttribute('content', v) });
  const html = await assetResponse.clone().text();
  const csp = pageCsp(await inlineScriptHashes(html));
  const rw = new HTMLRewriter()
    .on('title', { element: e => e.setInnerContent(title) })
    .on('meta[name="description"]', setContent(desc))
    .on('meta[property="og:title"]', setContent(title))
    .on('meta[property="og:description"]', setContent(desc))
    .on('meta[property="og:url"]', setContent(canonical))
    .on('meta[name="twitter:title"]', setContent(title))
    .on('meta[name="twitter:description"]', setContent(desc))
    .on('link[rel="canonical"]', { element: e => e.setAttribute('href', canonical) })
    .on('link[hreflang]', { element: e => e.setAttribute('href', canonical) })
    .on('#home-link', { element: e => e.setAttribute('href', categoryHref([], scope)) })
    .on('#scope-nav', { element: e => e.setInnerContent(scopeNavHtml(scopes, scope), { html: true }) })
    .on('#breadcrumbs', { element: e => e.setInnerContent(crumbs, { html: true }) })
    .on('#categories', { element: e => e.setInnerContent(cats, { html: true }) })
    .on('#sample-label', { element: e => { if (siteItems) e.removeAttribute('hidden'); } })
    .on('#directory-sites', { element: e => e.setInnerContent(siteItems, { html: true }) })
    .on('#site-pager', { element: e => { if (pagerBody) { e.setInnerContent(pagerBody, { html: true }); e.removeAttribute('hidden'); } } });
  // 前後ページの関係（クローラ向け。Google は rel=next/prev を使わないが、他の検索エンジンと意味づけのために出す）
  const rels = [page > 1 && `<link rel="prev" href="${esc(ORIGIN + categoryHref(path, scope, page - 1))}">`, page < pages && `<link rel="next" href="${esc(ORIGIN + categoryHref(path, scope, page + 1))}">`].filter(Boolean).join('');
  if (rels) rw.on('head', { element: e => e.append(rels, { html: true }) });
  if (wantsNoindex(url.searchParams)) rw.on('head', { element: e => e.append('<meta name="robots" content="noindex,follow">', { html: true }) });

  const res = rw.transform(new Response(assetResponse.body, assetResponse));
  // 元assetのETag/Last-Modifiedは書き換え後の本文と対応しないので残さない（304で別ページが返るのを防ぐ）
  res.headers.delete('ETag');
  res.headers.delete('Last-Modified');
  res.headers.set('Cache-Control', 'public, max-age=0, must-revalidate, s-maxage=60');
  for (const [k, v] of Object.entries(securityHeaders(csp))) res.headers.set(k, v);
  return res;
}

// アバウトページ（/about/）。静的な public/about.html に、このページ用の CSP（インラインスクリプトのハッシュ入り）を付けて返す。DB は使わない。
export async function renderAbout(assetResponse) {
  const html = await assetResponse.text();
  const csp = pageCsp(await inlineScriptHashes(html));
  const res = new Response(html, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=0, must-revalidate, s-maxage=60' } });
  for (const [k, v] of Object.entries(securityHeaders(csp))) res.headers.set(k, v);
  return res;
}
