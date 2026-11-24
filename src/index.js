import { Hono } from 'hono';
import { renderAbout, loadCategories, loadScopes, parseScope, scopePath, renderIndex, sitemapXml, DEFAULT_SCOPE } from './seo.js';
import { securityHeaders } from './security.js';
import { receiveRequest } from './intake.js';
import { serveMedia } from './media.js';
import { querySites, randomSites, parseLimit, parsePage, decodeCursor, cleanQuery, cleanTags, scopeKeyOf, RECENT_CATEGORY } from './sites.js';
import { usageMiddleware } from './usage.js';
const app = new Hono();
// 最初に登録する: 重い GET の共有キャッシュ（ヒットなら D1 を読まない）と、1 リクエストの D1 読み取り行数の観測（X-D1-Rows-Read・閾値超過だけ警告ログ）。#157 / src/usage.js
app.use('*', usageMiddleware());
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
// 範囲（?scope=jp|us|…|jp-13（区画。JP-13 も可）|all。省略は all）。不正な値は 400（no-store）。カテゴリの件数・サイト・活動はすべて範囲内だけを数える。
// 区画（ハイフンを含む値）だけは大文字小文字を区別しない（sites.region_code の形 JP-13 でも指定できる）。国は従来どおり小文字のみ。
const scopeOf = c => { const raw = c.req.query('scope'); return parseScope(raw?.includes('-') ? raw.toLowerCase() : raw); };
app.get('/api/scopes', async c => c.json(await loadScopes(c.env.DB)));
app.get('/api/categories', async c => { const sc = scopeOf(c); return sc ? c.json(await loadCategories(c.env.DB, sc.region ?? sc.cc)) : c.json({ error: 'invalid scope' }, 400); });
// カーソル方式。`limit`（既定 40、1〜100。100 を超える値は 100 に丸める。0・負数・数字以外は 400）+ `cursor`（前の応答の next_cursor。不正は 400）。
// 絞り込みは `category`（そのカテゴリ直下。存在しない id は 404）・`q`（名前・紹介文・URL・タグ名（日英）の部分一致。NFKC + 小文字化で比較。100 文字まで）。
// `page`（1〜999999。範囲外・数字以外は 400）は開始位置の指定（クロール用ページ番号URLと同じ区切り。カーソルが無いときだけ有効）。total は先頭の応答（cursor なし）だけに入る。q・tag・葉でないカテゴリの絞り込みでは TOTAL_CAP（1,000）までしか数えず、超えると total = 1000・total_capped = true（画面は「1,000+」。末尾まで届いたときは常に正確）。
// `tag`（タグの日本語名か英語名。複数指定は AND。5 個まで・各 100 文字まで、超えると 400）は q・category・範囲と AND で絞る。
// category=recent は登録日時に基づく仮想フォルダ。カテゴリ未所属のサイトも条件を満たせば出る。
app.get('/api/sites', async c => {
  const sc = scopeOf(c); if (!sc) return c.json({ error: 'invalid scope' }, 400);
  const category = c.req.query('category') || null;
  const recent = category === RECENT_CATEGORY;
  const cursor = decodeCursor(c.req.query('cursor'), recent); if (!cursor) return c.json({ error: 'invalid cursor' }, 400);
  const limit = parseLimit(c.req.query('limit')); if (limit === null) return c.json({ error: 'invalid limit' }, 400);
  const page = parsePage(c.req.query('page')); if (page === null) return c.json({ error: 'invalid page' }, 400);
  const tags = cleanTags(c.req.queries('tag')); if (!tags) return c.json({ error: 'invalid tag' }, 400);
  if (category && !recent && !(await c.env.DB.prepare('SELECT 1 AS ok FROM categories WHERE id = ?').bind(category).first())) return c.json({ error: 'unknown category' }, 404);
  return c.json(await querySites(c.env.DB, { cc: sc.cc, region: sc.region, category, q: cleanQuery(c.req.query('q')), tags, limit, cursor, offset: (page - 1) * limit }));
});
// ピックアップ用のランダム 3 件（範囲内）。
app.get('/api/picks', async c => { const sc = scopeOf(c); return sc ? c.json(await randomSites(c.env.DB, sc.cc, 3, sc.region)) : c.json({ error: 'invalid scope' }, 400); });
app.get('/api/activity', async c => {
  const sc = scopeOf(c); if (!sc) return c.json({ error: 'invalid scope' }, 400);
  const totals = await c.env.DB.prepare(`SELECT COALESCE((SELECT n FROM scope_counts WHERE scope = ?), 0) AS total`).bind(scopeKeyOf(sc)).first(); // 集計表から（#126）
  const { results } = await c.env.DB.prepare(`SELECT date(registered_at) AS day, COUNT(*) AS count FROM sites WHERE bulk_import = 0 AND datetime(registered_at) IS NOT NULL AND datetime(registered_at) <= datetime('now') AND date(registered_at) >= date('now','-6 days') AND (?1 IS NULL OR country_code = ?1) AND (?2 IS NULL OR region_code = ?2) GROUP BY date(registered_at) ORDER BY day`).bind(sc.region ? null : sc.cc, sc.region).all();
  return c.json({total:totals.total, known_daily:results});
});
// URL 単位の共通進捗表。公開済みの表示情報は sites と結合し、申請理由・連絡先・IP 情報は返さない。
app.get('/api/progress', async c => {
  const raw = c.req.query('offset') ?? '0', limitRaw = c.req.query('limit') ?? '50';
  if (!/^\d{1,7}$/.test(raw) || !/^\d{1,3}$/.test(limitRaw)) return c.json({ error: 'invalid pagination' }, 400);
  const offset = Number(raw), limit = Number(limitRaw);
  if (limit < 1 || limit > 100 || offset > 1000000) return c.json({ error: 'invalid pagination' }, 400);
  const { results } = await c.env.DB.prepare(`
    SELECT p.url AS url, s.name AS name, s.description_ja AS description_ja, s.description_en AS description_en,
      (SELECT COUNT(*) FROM site_tags t WHERE t.site_id=s.id) AS tags_count,
      (SELECT COUNT(*) FROM site_categories sc WHERE sc.site_id=s.id) AS categories_count,
      CASE WHEN s.favicon_path IS NOT NULL THEN 1 ELSE 0 END AS favicon,
      CASE WHEN s.thumb_path IS NOT NULL THEN 1 ELSE 0 END AS thumbnail,
      CASE WHEN s.id IS NOT NULL THEN 'published' ELSE p.state END AS state,
      p.source AS source, p.request_type AS request_type, p.request_status AS request_status, p.updated_at AS updated_at
    FROM url_progress p
    LEFT JOIN sites s ON s.id=(SELECT s2.id FROM sites s2 WHERE s2.progress_key=p.identity_key ORDER BY s2.registered_at DESC LIMIT 1)
    ORDER BY datetime(p.updated_at) DESC, p.identity_key LIMIT ?1 OFFSET ?2`).bind(limit, offset).all();
  const total = await c.env.DB.prepare('SELECT COUNT(*) AS total FROM url_progress').first();
  return c.json({ results, total: total.total, offset, limit });
});
// 申請の受付(推薦 add・取り下げ remove・訂正 correct)。受付のみで読み出しAPIは無い。docs/operations/requests.md
const NOJS = `<!doctype html><meta charset="utf-8"><title>MISE</title><p>JavaScript を有効にして送信してください。</p><p>Please enable JavaScript to send this form.</p>`;
app.post('/api/requests', async c => {
  // JS なしのフォーム送信(ブラウザの遷移)には JSON でなく簡単な HTML を返す。
  if ((c.req.header('Accept') || '').includes('text/html') && !(c.req.header('Content-Type') || '').toLowerCase().startsWith('application/json')) return c.html(NOJS, 415);
  const r = await receiveRequest(c); return c.json(r.body, r.status); });
// R2 の画像（ファビコン /icon/・サムネイル /thumb/）。:scope/:sub より先に登録する。MEDIA 未設定でも 404 を返すだけ。docs/design/architecture.md
app.get('/icon/:file', c => serveMedia(c.env, c.req.raw, 'icon', c.req.param('file')));
app.get('/thumb/:file', c => serveMedia(c.env, c.req.raw, 'thumb', c.req.param('file')));
app.get('/sitemap.xml', async c => c.body(await sitemapXml(c.env.DB), 200, { 'Content-Type': 'application/xml; charset=utf-8', 'Cache-Control': 'public, max-age=3600' }));
// 範囲ページ。`/` は既定範囲（日本）と同じ内容を 200 で返し、canonical だけ `/jp/` に向ける（301 にしない理由は docs/design/seo.md）。
const page = async (c, scope, scopes) => renderIndex(c, await c.env.ASSETS.fetch(new Request(new URL('/index.html', c.req.url))), scope, scopes);
app.get('/', async c => page(c, DEFAULT_SCOPE, await loadScopes(c.env.DB)));
// アバウトページ（#118）。範囲（/:scope/）より先に登録する。/about は /about/ へ 301。理由は docs/design/seo.md。
app.get('/about', c => { const u = new URL(c.req.url); u.pathname = '/about/'; return c.redirect(u.toString(), 301); });
app.get('/about/', async c => renderAbout(await c.env.ASSETS.fetch(new Request(new URL('/about.html', c.req.url)))));
app.get('/progress', c => c.redirect('/progress/', 301));
app.get('/progress/', async c => c.html(await (await c.env.ASSETS.fetch(new Request(new URL('/progress.html', c.req.url)))).text()));
// /jp/ /us/ /all/。大文字は小文字へ 301。サイトが 1 件も無い国・不正な値は 404（sitemap にも載せない）。
// 末尾スラッシュなし（/jp）は存在する範囲だけ /jp/ へ 301（クエリは維持）。run_worker_first の拡張子なしパターンで Worker に届く。
// 区画（都道府県・州）は /jp/13/ /us/ca/（範囲名 jp-13 の「-」を「/」にした形）。区画にサイトが 1 件も無いとき・不正な値は 404、大文字は小文字へ 301。
// 範囲名（'jp' / 'jp-13'）を URL のパラメータから組み立てる。sub が無ければ国・all。
const scopeFromParams = (a, b) => parseScope((b ? `${a}-${b}` : a).toLowerCase());
const exists = (sc, scopes) => !sc.cc || (sc.region ? scopes.regions.some(r => r.code === sc.region) : scopes.countries.some(r => r.code === sc.cc));
app.get('/:scope', async c => {
  const sc = scopeFromParams(c.req.param('scope'));
  if (!sc || sc.region) return c.text('Not Found', 404);
  if (!exists(sc, await loadScopes(c.env.DB))) return c.text('Not Found', 404);
  const u = new URL(c.req.url); u.pathname = scopePath(sc.scope); return c.redirect(u.toString(), 301);
});
app.get('/:scope/', async c => {
  const raw = c.req.param('scope'), sc = scopeFromParams(raw);
  if (!sc || sc.region) return c.text('Not Found', 404);
  if (raw !== sc.scope) { const u = new URL(c.req.url); u.pathname = scopePath(sc.scope); return c.redirect(u.toString(), 301); }
  const scopes = await loadScopes(c.env.DB);
  if (!exists(sc, scopes)) return c.text('Not Found', 404);
  return page(c, sc.scope, scopes);
});
app.get('/:scope/:sub', async c => {
  const sc = scopeFromParams(c.req.param('scope'), c.req.param('sub'));
  if (!sc?.region || !exists(sc, await loadScopes(c.env.DB))) return c.text('Not Found', 404);
  const u = new URL(c.req.url); u.pathname = scopePath(sc.scope); return c.redirect(u.toString(), 301);
});
app.get('/:scope/:sub/', async c => {
  const a = c.req.param('scope'), b = c.req.param('sub'), sc = scopeFromParams(a, b);
  if (!sc?.region) return c.text('Not Found', 404);
  if (`${a}/${b}` !== sc.scope.replace('-', '/')) { const u = new URL(c.req.url); u.pathname = scopePath(sc.scope); return c.redirect(u.toString(), 301); }
  const scopes = await loadScopes(c.env.DB);
  if (!exists(sc, scopes)) return c.text('Not Found', 404);
  return page(c, sc.scope, scopes);
});
export default app;
