// 1 リクエストあたりの D1 読み取り行数の観測と、重い GET の短い共有キャッシュ（#157）。docs/design/architecture.md「読み取りの上限と観測」。
// 目的は、暴走（攻撃・バグ）で料金が膨らむのを早く気づくことと、同じ URL の繰り返しで D1 を読み直さないこと。結果（検索結果・並び・total）は変えない。

// この行数を超えたリクエストだけ console.warn を出す（通常の一覧は数百〜数千行。1〜2 文字の検索が最悪で約 10 万行）。大量リクエストでログが洪水にならない高さ。
export const ROWS_WARN = 20000;
// 共有キャッシュ（caches.default）の TTL（秒）。/api/* はブラウザ側も元から max-age=30 なので、最悪で 60 + 30 = 約 90 秒古い応答がありうる。HTML は 60 秒。sitemap は元の max-age=3600 がブラウザ側を支配する。
export const CACHE_TTL = 60;
export const ROWS_HEADER = 'X-D1-Rows-Read';
// 開発用の診断ヘッダ（X-D1-Rows-Read・X-Cache）は、環境変数 DEBUG_ROWS が有るときだけ付ける。本番では付けない（コストの指標を攻撃者に渡さない）。
// wrangler.jsonc の vars には書かず、ローカル専用の .dev.vars（git 管理外）に `DEBUG_ROWS=1` と書く。ログ（閾値超えの console.warn）は常に出す。
const debugOn = env => !!env?.DEBUG_ROWS && env.DEBUG_ROWS !== '0';
// キャッシュに入れた応答が元々持っていた Cache-Control（取り出すときに戻す）。元が無かったときは ABSENT を立てて、取り出すときに Cache-Control を消す。どちらも外には出さない。
const ORIG_CC = 'X-Mise-Orig-Cache-Control', ABSENT = 'X-Mise-Orig-Cc-Absent';

// D1 のバインディングを包み、meta.rows_read の合計を acc.rows に足す。呼び出し側の見える挙動（戻り値）は変えない。
// first() は meta を返さないので all() で実行して先頭行を返す（D1 の first() も 1 回の実行で先頭行を取るだけで、同じ結果）。
export function meterDb(db, acc) {
  if (!db || typeof db.prepare !== 'function') return db;
  const note = r => { acc.stmts++; const n = r?.meta?.rows_read; if (Number.isFinite(n)) acc.rows += n; return r; };
  const wrap = st => ({
    _raw: st,
    bind: (...a) => wrap(st.bind(...a)),
    all: async () => note(await st.all()),
    // D1 の first(col) は列が無いと投げる。同じく投げる（現在の呼び出しは引数なしだけ）。
    first: async col => { const r = note(await st.all()), row = r?.results?.[0] ?? null; if (col === undefined || !row) return row; if (!(col in row)) throw new Error(`D1_ERROR: No such column: ${col}`); return row[col]; },
    run: async () => note(await st.run()),
    raw: (...a) => st.raw(...a), // raw() は meta を返さないので行数を数えられない（現在の呼び出しは無い）
  });
  const out = { prepare: sql => wrap(db.prepare(sql)) };
  if (typeof db.batch === 'function') out.batch = async sts => (await db.batch(sts.map(s => s._raw ?? s))).map(note);
  return out;
}

// 共有キャッシュに入れてよい GET か。DB を読む重い経路（検索・カテゴリ・範囲・活動・sitemap・範囲ページの SSR・アバウト）。
// 入れない: /api/picks（毎回ランダム。キャッシュすると結果が変わる）・/api/requests（POST）・/icon/・/thumb/（R2。immutable で別に長期キャッシュ）・その他の /api/*（404 等）。
const CACHE_PATHS = new Set(['/api/sites', '/api/categories', '/api/scopes', '/api/activity', '/api/progress', '/sitemap.xml']);
export function isCacheable(req) {
  if (req.method !== 'GET' || req.headers.has('Authorization')) return false;
  const { pathname } = new URL(req.url);
  if (pathname.startsWith('/api/')) return CACHE_PATHS.has(pathname);
  return !pathname.startsWith('/icon/') && !pathname.startsWith('/thumb/');
}

// キーは「デプロイのバージョン + URL（クエリ込み）」。caches.default はデプロイで消えないので、バージョンをキーに入れてデプロイ直後に古い応答を返さない
// （wrangler.jsonc の version_metadata バインディング CF_VERSION_METADATA の id。無い環境［テスト・ローカル］では空文字）。
// この Worker は Accept-Language・Cookie で応答を変えない（SSR は英語固定・クライアントが言語を切り替える）ので含めない。Authorization 付きは入れない（isCacheable）。
const cacheKey = (req, version = '') => { const u = new URL(req.url); return new Request(`${u.origin}/__v/${encodeURIComponent(version)}${u.pathname}${u.search}`, { method: 'GET' }); };
const defaultCache = () => globalThis.caches?.default ?? null;

// 先に実行するミドルウェア。ヒットなら D1 を読まず保存した応答を返す。ミスなら次へ進み、200 の応答を保存する。
export function usageMiddleware({ getCache = defaultCache, ttl = CACHE_TTL, warnAt = ROWS_WARN, log = console.warn } = {}) {
  return async (c, next) => {
    const acc = { rows: 0, stmts: 0 };
    const cache = isCacheable(c.req.raw) ? getCache() : null, debug = debugOn(c.env), version = String(c.env?.CF_VERSION_METADATA?.id ?? '');
    if (cache) {
      let hit = null;
      try { hit = await cache.match(cacheKey(c.req.raw, version)); } catch (_) { /* キャッシュの失敗は無視して通常処理 */ }
      if (hit) {
        const h = new Headers(hit.headers), cc = h.get(ORIG_CC);
        const absent = h.has(ABSENT);
        h.delete(ORIG_CC); h.delete(ABSENT);
        if (absent) h.delete('Cache-Control'); else if (cc !== null) h.set('Cache-Control', cc); // 元が空文字でも復元する(保存用の max-age=60 を残さない)
        if (debug) { h.set(ROWS_HEADER, '0'); h.set('X-Cache', 'HIT'); }
        c.res = new Response(hit.body, { status: hit.status, headers: h });
        return;
      }
    }
    if (c.env?.DB) c.env = { ...c.env, DB: meterDb(c.env.DB, acc) };
    await next();
    if (debug && acc.stmts) c.res.headers.set(ROWS_HEADER, String(acc.rows));
    if (acc.rows > warnAt) log(JSON.stringify({ event: 'd1-rows-read-high', rows_read: acc.rows, stmts: acc.stmts, method: c.req.method, path: new URL(c.req.url).pathname, query_length: new URL(c.req.url).search.length }));
    if (cache && (c.res.status === 200 || (c.res.status === 404 && !new URL(c.req.url).pathname.startsWith('/api/')))) { // 画面側の 404（範囲外のページ番号など。判定に D1 を数十万行読むことがある）も同じ URL なら同じ結果
      const copy = c.res.clone(), h = new Headers(copy.headers);
      if (h.has('Cache-Control')) h.set(ORIG_CC, h.get('Cache-Control')); else h.set(ABSENT, '1');
      h.set('Cache-Control', `public, max-age=${ttl}`); h.delete('Set-Cookie');
      const put = cache.put(cacheKey(c.req.raw, version), new Response(copy.body, { status: c.res.status, headers: h })).catch(() => {});
      try { c.executionCtx.waitUntil(put); } catch (_) { /* テスト等 executionCtx が無い環境では待たない */ }
      if (debug) c.res.headers.set('X-Cache', 'MISS');
    }
  };
}
