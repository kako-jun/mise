import { Hono } from 'hono';
import { loadCategories, loadScopes, parseScope, renderIndex, sitemapXml, DEFAULT_SCOPE } from './seo.js';
import { securityHeaders } from './security.js';
import { receiveRequest } from './intake.js';
import { querySites, randomSites, parseLimit, parsePage, decodeCursor, cleanQuery } from './sites.js';
const app = new Hono();
app.use('*', async (c, next) => {
  await next();
  // HTML はインラインスクリプトのハッシュを含む CSP を seo.js が設定する。それ以外の応答には固定の CSP を付ける。
  for (const [k, v] of Object.entries(securityHeaders())) if (!c.res.headers.has(k)) c.res.headers.set(k, v);
});
// /api/* はブラウザ用。更新が速やかに反映されるよう短いキャッシュだけ許す（docs/design/architecture.md）。
// 成功応答だけ。4xx/5xx（D1 の一時的な失敗など）は no-store にして、エラーをキャッシュさせない。
app.use('/api/*', async (c, next) => { await next(); c.res.headers.set('Cache-Control', c.req.method === 'GET' && c.res.status < 400 ? 'public, max-age=30' : 'no-store'); });
// D1 失敗などの例外は 500 にして握りつぶさない（上のミドルウェアが no-store を付ける）。
app.onError((e, c) => c.text('Internal Server Error', 500));
// 範囲（?scope=jp|us|…|all。省略は all）。不正な値は 400（no-store）。カテゴリの件数・サイト・活動はすべて範囲内だけを数える。
const scopeOf = c => parseScope(c.req.query('scope'));
app.get('/api/scopes', async c => c.json(await loadScopes(c.env.DB)));
app.get('/api/categories', async c => { const sc = scopeOf(c); return sc ? c.json(await loadCategories(c.env.DB, sc.cc)) : c.json({ error: 'invalid scope' }, 400); });
// カーソル方式。`limit`（既定 40、1〜100。100 を超える値は 100 に丸める。0・負数・数字以外は 400）+ `cursor`（前の応答の next_cursor。不正は 400）。
// 絞り込みは `category`（そのカテゴリ直下。存在しない id は 404）・`q`（名前・紹介文・URL の部分一致。NFKC + 小文字化で比較。100 文字まで）。
// `page`（1〜999999。範囲外・数字以外は 400）は開始位置の指定（クロール用ページ番号URLと同じ区切り。カーソルが無いときだけ有効）。total は先頭の応答（cursor なし）だけに入る。
// カテゴリ未所属のサイトも category 指定なしなら出る（category_ids が空配列。画面では「未分類」）。
app.get('/api/sites', async c => {
  const sc = scopeOf(c); if (!sc) return c.json({ error: 'invalid scope' }, 400);
  const cursor = decodeCursor(c.req.query('cursor')); if (!cursor) return c.json({ error: 'invalid cursor' }, 400);
  const limit = parseLimit(c.req.query('limit')); if (limit === null) return c.json({ error: 'invalid limit' }, 400);
  const page = parsePage(c.req.query('page')); if (page === null) return c.json({ error: 'invalid page' }, 400);
  const category = c.req.query('category') || null;
  if (category && !(await c.env.DB.prepare('SELECT 1 AS ok FROM categories WHERE id = ?').bind(category).first())) return c.json({ error: 'unknown category' }, 404);
  return c.json(await querySites(c.env.DB, { cc: sc.cc, category, q: cleanQuery(c.req.query('q')), limit, cursor, offset: (page - 1) * limit }));
});
// ピックアップ用のランダム 3 件（範囲内）。
app.get('/api/picks', async c => { const sc = scopeOf(c); return sc ? c.json(await randomSites(c.env.DB, sc.cc, 3)) : c.json({ error: 'invalid scope' }, 400); });
app.get('/api/activity', async c => {
  const sc = scopeOf(c); if (!sc) return c.json({ error: 'invalid scope' }, 400);
  const totals = await c.env.DB.prepare(`SELECT COUNT(*) AS total FROM sites WHERE (?1 IS NULL OR country_code = ?1)`).bind(sc.cc).first();
  const { results } = await c.env.DB.prepare(`SELECT date(registered_at) AS day, COUNT(*) AS count FROM sites WHERE bulk_import = 0 AND datetime(registered_at) IS NOT NULL AND datetime(registered_at) <= datetime('now') AND date(registered_at) >= date('now','-6 days') AND (?1 IS NULL OR country_code = ?1) GROUP BY date(registered_at) ORDER BY day`).bind(sc.cc).all();
  return c.json({total:totals.total, known_daily:results});
});
// 申請の受付(推薦 add・取り下げ remove・訂正 correct)。受付のみで読み出しAPIは無い。docs/operations/requests.md
const NOJS = `<!doctype html><meta charset="utf-8"><title>MISE</title><p>JavaScript を有効にして送信してください。</p><p>Please enable JavaScript to send this form.</p>`;
app.post('/api/requests', async c => {
  // JS なしのフォーム送信(ブラウザの遷移)には JSON でなく簡単な HTML を返す。
  if ((c.req.header('Accept') || '').includes('text/html') && !(c.req.header('Content-Type') || '').toLowerCase().startsWith('application/json')) return c.html(NOJS, 415);
  const r = await receiveRequest(c); return c.json(r.body, r.status); });
app.get('/sitemap.xml', async c => c.body(await sitemapXml(c.env.DB), 200, { 'Content-Type': 'application/xml; charset=utf-8', 'Cache-Control': 'public, max-age=3600' }));
// 範囲ページ。`/` は既定範囲（日本）と同じ内容を 200 で返し、canonical だけ `/jp/` に向ける（301 にしない理由は docs/design/seo.md）。
const page = async (c, scope, scopes) => renderIndex(c, await c.env.ASSETS.fetch(new Request(new URL('/index.html', c.req.url))), scope, scopes);
app.get('/', async c => page(c, DEFAULT_SCOPE, await loadScopes(c.env.DB)));
// /jp/ /us/ /all/。大文字は小文字へ 301。サイトが 1 件も無い国・不正な値は 404（sitemap にも載せない）。
// 末尾スラッシュなし（/jp）は存在する範囲だけ /jp/ へ 301（クエリは維持）。run_worker_first の拡張子なしパターンで Worker に届く。
app.get('/:scope', async c => {
  const raw = c.req.param('scope'), sc = parseScope(raw.toLowerCase());
  if (!sc) return c.text('Not Found', 404);
  if (sc.cc && !(await loadScopes(c.env.DB)).countries.some(r => r.code === sc.cc)) return c.text('Not Found', 404);
  const u = new URL(c.req.url); u.pathname = `/${sc.scope}/`; return c.redirect(u.toString(), 301);
});
app.get('/:scope/', async c => {
  const raw = c.req.param('scope'), sc = parseScope(raw.toLowerCase());
  if (!sc) return c.text('Not Found', 404);
  if (raw !== sc.scope) { const u = new URL(c.req.url); u.pathname = `/${sc.scope}/`; return c.redirect(u.toString(), 301); }
  const scopes = await loadScopes(c.env.DB);
  if (sc.cc && !scopes.countries.some(r => r.code === sc.cc)) return c.text('Not Found', 404);
  return page(c, sc.scope, scopes);
});
export default app;
