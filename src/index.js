import { Hono } from 'hono';
import { loadCategories, renderIndex, sitemapXml, SITE_ORDER } from './seo.js';
import { securityHeaders } from './security.js';
const app = new Hono();
app.use('*', async (c, next) => {
  await next();
  // HTML はインラインスクリプトのハッシュを含む CSP を seo.js が設定する。それ以外の応答には固定の CSP を付ける。
  for (const [k, v] of Object.entries(securityHeaders())) if (!c.res.headers.has(k)) c.res.headers.set(k, v);
});
// /api/* はブラウザ用。更新が速やかに反映されるよう短いキャッシュだけ許す（docs/design/architecture.md）。
// 成功応答だけ。4xx/5xx（D1 の一時的な失敗など）は no-store にして、エラーをキャッシュさせない。
app.use('/api/*', async (c, next) => { await next(); c.res.headers.set('Cache-Control', c.res.status < 400 ? 'public, max-age=30' : 'no-store'); });
// D1 失敗などの例外は 500 にして握りつぶさない（上のミドルウェアが no-store を付ける）。
app.onError((e, c) => c.text('Internal Server Error', 500));
app.get('/api/categories', async c => c.json(await loadCategories(c.env.DB)));
// LEFT JOIN: カテゴリ未所属のサイトも category_id = NULL で返す（画面では「未分類」として検索に出る）。
app.get('/api/sites', async c => {
  const { results } = await c.env.DB.prepare(`SELECT s.id, s.name, s.url, s.description_ja, s.description_en, s.favicon_path, s.registered_at, s.source_updated_at, s.last_crawled_at, s.country_code, s.region_code, (SELECT json_group_array(json_object('ja',t.label_ja,'en',t.label_en)) FROM site_tags t WHERE t.site_id=s.id) AS tags_json, sc.category_id FROM sites s LEFT JOIN site_categories sc ON s.id = sc.site_id ${SITE_ORDER}`).all();
  return c.json(results);
});
app.get('/api/activity', async c => {
  const totals = await c.env.DB.prepare(`SELECT COUNT(*) AS total FROM sites`).first();
  const { results } = await c.env.DB.prepare(`SELECT date(registered_at) AS day, COUNT(*) AS count FROM sites WHERE datetime(registered_at) IS NOT NULL AND datetime(registered_at) <= datetime('now') AND date(registered_at) >= date('now','-6 days') GROUP BY date(registered_at) ORDER BY day`).all();
  return c.json({total:totals.total, known_daily:results});
});
app.get('/', async c => renderIndex(c, await c.env.ASSETS.fetch(new Request(new URL('/index.html', c.req.url)))));
app.get('/sitemap.xml', async c => c.body(await sitemapXml(c.env.DB), 200, { 'Content-Type': 'application/xml; charset=utf-8', 'Cache-Control': 'public, max-age=3600' }));
export default app;
