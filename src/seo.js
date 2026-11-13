// SEO 用ヘルパー: カテゴリパスの解決、canonical、sitemap、"/" へのメタ情報・本文の注入。
export const ORIGIN = 'https://mise.llll-ll.com';
// 既定言語は英語（app.js の初期言語と一致）。サイトの紹介文は日本語のみのため「(Japanese)」を添える。
const DEFAULT_TITLE = 'MISE — A directory search engine for today.';
const DEFAULT_DESC = 'For those tired of search results dominated by ads and major sites. Discover independent websites you have yet to find. MISE is a directory search engine.';

const esc = s => String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

export const categoryHref = path => '/' + (path.length ? '?' + path.map(id => 'category=' + encodeURIComponent(id)).join('&') : '');

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

export async function loadCategories(db) {
  const { results } = await db.prepare(`WITH RECURSIVE tree(root_id, id) AS (SELECT id, id FROM categories UNION ALL SELECT tree.root_id, c.id FROM tree JOIN categories c ON c.parent_id = tree.id) SELECT c.id, c.parent_id, c.name_ja, c.name_en, c.sort_order, COUNT(DISTINCT sc.site_id) AS site_count FROM categories c LEFT JOIN tree ON tree.root_id = c.id LEFT JOIN site_categories sc ON sc.category_id = tree.id GROUP BY c.id ORDER BY c.sort_order`).all();
  return results;
}

function pathOf(categories, id) {
  const path = [];
  for (let c = categories.find(x => x.id === id); c; c = categories.find(x => x.id === c.parent_id)) path.unshift(c.id);
  return path;
}

export async function sitemapXml(db) {
  const categories = await loadCategories(db);
  const urls = [ORIGIN + '/'];
  for (const c of categories) if (c.site_count > 0) urls.push(ORIGIN + categoryHref(pathOf(categories, c.id)));
  const body = urls.map(u => `<url><loc>${esc(u)}</loc></url>`).join('');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${body}</urlset>\n`;
}

// "/" の HTML を、URL（?category= / ?q=）に応じたメタ情報と JS なしで読める本文に書き換える
export async function renderIndex(c, assetResponse) {
  const url = new URL(c.req.url);
  const categories = await loadCategories(c.env.DB);
  const raw = url.searchParams.getAll('category');
  const path = resolvePath(categories, raw) ?? [];
  const current = categories.find(x => x.id === path.at(-1));
  const isSearch = !!url.searchParams.get('q')?.trim();

  let title = DEFAULT_TITLE;
  let desc = DEFAULT_DESC;
  if (current) {
    const names = path.map(id => categories.find(x => x.id === id).name_en);
    title = `${names.join(' › ')} — MISE`;
    desc = `${names.join(' › ')} on MISE: ${current.site_count} ${current.site_count === 1 ? 'site' : 'sites'}. Discover independent websites you have yet to find.`;
  }
  const canonical = ORIGIN + categoryHref(isSearch ? [] : path);

  // 本文（JS が起動すると置き換わる）
  const children = categories.filter(x => x.parent_id === (path.at(-1) ?? null));
  const crumbs = path.length
    ? `<a href="/">Home</a>` + path.map((id, i) => {
        const name = esc(categories.find(x => x.id === id).name_en);
        return ' › ' + (i === path.length - 1 ? name : `<a href="${esc(categoryHref(path.slice(0, i + 1)))}">${name}</a>`);
      }).join('')
    : '';
  const cats = children.map(ch => {
    const sub = path.length ? '' : `<p class="sub">${esc(categories.filter(x => x.parent_id === ch.id).map(x => x.name_en).join(' '))}</p>`;
    return `<div class="category"><h2><a href="${esc(categoryHref([...path, ch.id]))}">${esc(ch.name_en)} (${ch.site_count})</a></h2>${sub}</div>`;
  }).join('');
  let siteItems = '';
  if (path.length) {
    const { results } = await c.env.DB.prepare('SELECT s.name, s.url, s.description_ja, s.description_en FROM sites s JOIN site_categories sc ON s.id = sc.site_id WHERE sc.category_id = ? ORDER BY s.id').bind(path.at(-1)).all();
    siteItems = results.map(s => `<li><a href="${esc(/^https?:\/\//i.test(s.url) ? s.url : '#')}">${esc(s.name)}</a> — ${s.description_en ? esc(s.description_en) : esc(s.description_ja) + ' (Japanese)'}</li>`).join('');
  }

  const setContent = v => ({ element: e => e.setAttribute('content', v) });
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
    .on('#breadcrumbs', { element: e => e.setInnerContent(crumbs, { html: true }) })
    .on('#categories', { element: e => e.setInnerContent(cats, { html: true }) })
    .on('#sample-label', { element: e => { if (siteItems) e.removeAttribute('hidden'); } })
    .on('#directory-sites', { element: e => e.setInnerContent(siteItems, { html: true }) });
  if (isSearch) rw.on('head', { element: e => e.append('<meta name="robots" content="noindex,follow">', { html: true }) });

  const res = rw.transform(new Response(assetResponse.body, assetResponse));
  // 元assetのETag/Last-Modifiedは書き換え後の本文と対応しないので残さない（304で別ページが返るのを防ぐ）
  res.headers.delete('ETag');
  res.headers.delete('Last-Modified');
  res.headers.set('Cache-Control', 'public, max-age=0, must-revalidate, s-maxage=60');
  return res;
}
