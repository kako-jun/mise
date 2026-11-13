import { securityHeaders, pageCsp, inlineScriptHashes } from './security.js';
import { querySites, parsePage, PAGE_SIZE } from './sites.js';
// 国名の表（ブラウザの app.js・globe.js と共有する単一の表）。読み込むと globalThis.MISE_COUNTRIES / MISE_FLAGS が定義される。
import '../public/countries.js';
// SEO 用ヘルパー: カテゴリパスの解決、canonical、sitemap、"/" へのメタ情報・本文の注入。
export const ORIGIN = 'https://mise.llll-ll.com';
// 既定言語は英語（app.js の初期言語と一致）。サイトの紹介文は日本語のみのため「(Japanese)」を添える。
const DEFAULT_TITLE = 'MISE — A directory search engine for today.';
const DEFAULT_DESC = 'For those tired of search results dominated by ads and major sites. Discover independent websites you have yet to find. MISE is a directory search engine.';

const esc = s => String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

// 範囲（scope）: 国コード（小文字 2 文字。jp, us…）または all。ルート `/` は既定範囲として扱う（docs/design/seo.md）。
export const DEFAULT_SCOPE = 'jp';
// API/URL の範囲指定を解釈する。省略・空・all は全世界（cc = null）、小文字 2 文字は国、それ以外は null（不正）。
export function parseScope(raw) {
  if (raw == null || raw === '' || raw === 'all') return { scope: 'all', cc: null };
  return /^[a-z]{2}$/.test(raw) ? { scope: raw, cc: raw.toUpperCase() } : null;
}
// page >= 2 のときだけ `page=N` を付ける（1 ページ目の URL は付けない形に正規化）。
export const categoryHref = (path, scope = DEFAULT_SCOPE, page = 1) => {
  const qs = [...path.map(id => 'category=' + encodeURIComponent(id)), ...(page > 1 ? ['page=' + page] : [])];
  return `/${scope}/` + (qs.length ? '?' + qs.join('&') : '');
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

// サイトが 1 件以上ある国（件数の多い順）と全体の件数。範囲として選べる国 = ここに出る国だけ。
export async function loadScopes(db) {
  const [total, countries] = await db.batch([
    db.prepare(`SELECT COUNT(*) AS n FROM sites`),
    db.prepare(`SELECT country_code AS code, COUNT(*) AS count FROM sites WHERE country_code IS NOT NULL GROUP BY country_code ORDER BY count DESC, country_code`),
  ]);
  return { total: total.results[0].n, countries: countries.results.filter(r => /^[A-Z]{2}$/.test(r.code)) };
}
const countryName = code => globalThis.MISE_COUNTRIES?.[code]?.en ?? code;
const flagUrl = code => (globalThis.MISE_FLAGS ?? []).includes(code) ? `/flags/${code.toLowerCase()}.svg` : null;

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

// cc を渡すと、その国のサイトだけを数える（空のカテゴリも 0 件で返す）。null は全世界。
export async function loadCategories(db, cc = null) {
  const { results } = await db.prepare(`WITH RECURSIVE tree(root_id, id) AS (SELECT id, id FROM categories UNION SELECT tree.root_id, c.id FROM tree JOIN categories c ON c.parent_id = tree.id) SELECT c.id, c.parent_id, c.name_ja, c.name_en, c.sort_order, COUNT(DISTINCT s.id) AS site_count FROM categories c LEFT JOIN tree ON tree.root_id = c.id LEFT JOIN site_categories sc ON sc.category_id = tree.id LEFT JOIN sites s ON s.id = sc.site_id AND (?1 IS NULL OR s.country_code = ?1) GROUP BY c.id ORDER BY c.sort_order`).bind(cc).all();
  return results;
}

function pathOf(categories, id) {
  const path = [];
  for (let c = categories.find(x => x.id === id); c; c = categories.find(x => x.id === c.parent_id)) path.unshift(c.id);
  return path;
}

// 範囲ごと（all と、サイトが 1 件以上ある国）に、範囲トップと、その範囲内に 1 サイト以上あるカテゴリの URL を載せる。`/` は `/jp/` の別名なので載せない。
export async function sitemapXml(db) {
  const scopes = await loadScopes(db);
  const urls = [];
  for (const [scope, cc] of [['all', null], ...scopes.countries.map(r => [r.code.toLowerCase(), r.code])]) {
    const categories = await loadCategories(db, cc);
    urls.push(ORIGIN + categoryHref([], scope));
    for (const c of categories) if (c.site_count > 0) urls.push(ORIGIN + categoryHref(pathOf(categories, c.id), scope));
  }
  const body = urls.map(u => `<url><loc>${esc(u)}</loc></url>`).join('');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${body}</urlset>\n`;
}

// 範囲切替の HTML（SSR。JS が起動すると app.js が同じ内容に描き直す）。現在の範囲は aria-current="page"。
function scopeNavHtml(scopes, scope) {
  const item = (code, href, label, count, icon) => `<li><a class="scope-item" href="${esc(href)}"${code === scope ? ' aria-current="page"' : ''}>${icon}<span class="scope-name">${esc(label)}</span><span class="scope-count">${count}</span></a></li>`;
  const globe = '<svg class="scope-globe" aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2"/><ellipse cx="8" cy="8" rx="2.8" ry="6.2"/><path d="M1.8 8h12.4"/></svg>';
  const items = [item('all', '/all/', 'All world', scopes.total, globe)];
  for (const r of scopes.countries) {
    const flag = flagUrl(r.code);
    items.push(item(r.code.toLowerCase(), `/${r.code.toLowerCase()}/`, countryName(r.code), r.count, flag ? `<img class="scope-flag" src="${esc(flag)}" alt="" width="16" height="12">` : ''));
  }
  return `<ul class="scope-list">${items.join('')}</ul>`;
}

// 範囲ページ（/ , /jp/ , /us/ , /all/）の HTML を、URL（?category= / ?q=）に応じたメタ情報と JS なしで読める本文に書き換える。
// scope は parseScope で検証済みの範囲名、scopes は loadScopes の結果。`/` は scope = jp で呼ばれ、canonical は `/jp/…`。
export async function renderIndex(c, assetResponse, scope, scopes) {
  const url = new URL(c.req.url);
  const cc = scope === 'all' ? null : scope.toUpperCase();
  const categories = await loadCategories(c.env.DB, cc);
  const raw = url.searchParams.getAll('category');
  const path = resolvePath(categories, raw) ?? [];
  const current = categories.find(x => x.id === path.at(-1));
  const isSearch = !!url.searchParams.get('q')?.trim();
  const page = path.length && !isSearch ? parsePage(url.searchParams.get('page')) : 1;
  if (page === null) return new Response('Not Found', { status: 404, headers: { 'Cache-Control': 'no-store' } });

  // 範囲ごとに title / description を変える（all は従来どおり）。国は英語名。
  const place = cc ? countryName(cc) : null, inPlace = place && (/^United /.test(place) ? 'the ' : '') + place;
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
    const sub = path.length ? '' : `<p class="sub">${esc(categories.filter(x => x.parent_id === ch.id).map(x => x.name_en).join(' '))}</p>`;
    return `<div class="category${ch.site_count ? '' : ' is-empty'}"><h2><a href="${esc(categoryHref([...path, ch.id], scope))}">${esc(ch.name_en)} (${ch.site_count})</a></h2>${sub}</div>`;
  }).join('');
  let siteItems = '', pagerBody = '', pages = 1;
  if (path.length) {
    const { items, total } = await querySites(c.env.DB, { cc, category: path.at(-1), limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE });
    pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (page > 1 && !items.length) return new Response('Not Found', { status: 404, headers: { 'Cache-Control': 'no-store' } });
    siteItems = items.map(s => `<li><a href="${esc(/^https?:\/\//i.test(s.url) ? s.url : '#')}">${esc(s.name)}</a>${/^[A-Z]{2}$/.test(s.country_code ?? '') ? ` [${/^[A-Z]{2}-[A-Z0-9]{1,3}$/.test(s.region_code ?? '') && s.region_code.startsWith(s.country_code) ? s.region_code : s.country_code}]` : ''} — ${s.description_en ? esc(s.description_en) : esc(s.description_ja) + ' (Japanese)'}</li>`).join('');
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
  if (isSearch) rw.on('head', { element: e => e.append('<meta name="robots" content="noindex,follow">', { html: true }) });

  const res = rw.transform(new Response(assetResponse.body, assetResponse));
  // 元assetのETag/Last-Modifiedは書き換え後の本文と対応しないので残さない（304で別ページが返るのを防ぐ）
  res.headers.delete('ETag');
  res.headers.delete('Last-Modified');
  res.headers.set('Cache-Control', 'public, max-age=0, must-revalidate, s-maxage=60');
  for (const [k, v] of Object.entries(securityHeaders(csp))) res.headers.set(k, v);
  return res;
}
