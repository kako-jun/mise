import test from 'node:test';
import assert from 'node:assert/strict';
import { Hono } from 'hono';
import app from '../src/index.js';
import { meterDb, usageMiddleware, isCacheable, ROWS_HEADER, ROWS_WARN, CACHE_TTL } from '../src/usage.js';

// #157: D1 読み取り行数の観測（X-D1-Rows-Read・閾値超過だけ警告）と、重い GET の短い共有キャッシュ。ネットワーク・Cloudflare は使わない。
const mkDb = (rowsPerStmt, calls = { n: 0 }) => {
  const mk = () => { const st = { bind: () => st, all: async () => { calls.n++; return { results: [{ total: 7 }], meta: { rows_read: rowsPerStmt } }; }, run: async () => { calls.n++; return { meta: { rows_read: rowsPerStmt, changes: 1 } }; }, raw: async () => [] }; return st; };
  return { prepare: () => mk(), batch: async sts => { calls.n++; return sts.map(() => ({ results: [], meta: { rows_read: rowsPerStmt } })); } };
};
const fakeCache = () => { const m = new Map(); return { m, match: async r => { const v = m.get(r.url); return v ? v.clone() : undefined; }, put: async (r, res) => { m.set(r.url, res); } }; };

test('meterDb: all / first / run / batch の rows_read を合計し、戻り値は変えない', async () => {
  const acc = { rows: 0, stmts: 0 }, db = meterDb(mkDb(10), acc);
  const a = await db.prepare('x').bind(1).all();
  assert.deepEqual(a.results, [{ total: 7 }]);
  assert.deepEqual(await db.prepare('x').first(), { total: 7 });
  assert.equal(await db.prepare('x').first('total'), 7);
  await db.prepare('x').run();
  await db.batch([db.prepare('a'), db.prepare('b')]);
  assert.equal(acc.rows, 10 + 10 + 10 + 10 + 20);
});

test('meterDb: meta の無い応答（モック）でも落ちない・DB が無ければそのまま返す', async () => {
  const acc = { rows: 0, stmts: 0 }, st = { bind: () => st, all: async () => ({ results: [] }) };
  assert.deepEqual((await meterDb({ prepare: () => st }, acc).prepare('x').all()).results, []);
  assert.equal(acc.rows, 0);
  assert.equal(meterDb(undefined, acc), undefined);
});

test('実アプリ: D1 を読んだ応答に X-D1-Rows-Read が付く（公開しても無害な数値のみ）', async () => {
  const res = await app.request('/api/scopes', {}, { DB: mkDb(123) });
  assert.equal(res.status, 200);
  assert.match(res.headers.get(ROWS_HEADER), /^\d+$/);
  assert.equal(Number(res.headers.get(ROWS_HEADER)), 123);
});

test('警告ログ: 閾値を超えたときだけ、検索語を含まない 1 行の JSON', async () => {
  const logs = [], mk = rows => { const a = new Hono(); a.use('*', usageMiddleware({ getCache: () => null, warnAt: 1000, log: l => logs.push(l) })); a.get('/api/sites', async c => { await c.env.DB.prepare('x').all(); return c.json({}); }); return (url) => a.request(url, {}, { DB: mkDb(rows) }); };
  await mk(1000)('/api/sites?q=secret');
  assert.equal(logs.length, 0);
  await mk(1001)('/api/sites?q=secret');
  assert.equal(logs.length, 1);
  const j = JSON.parse(logs[0]);
  assert.equal(j.event, 'd1-rows-read-high'); assert.equal(j.rows_read, 1001); assert.equal(j.path, '/api/sites');
  assert.ok(!logs[0].includes('secret'));
  assert.ok(ROWS_WARN >= 10000); // 通常の一覧で鳴らない高さ
});

const cachedApp = (cache, calls, extraRoutes = () => {}) => {
  const a = new Hono();
  a.use('*', usageMiddleware({ getCache: () => cache }));
  a.get('/api/sites', async c => { await c.env.DB.prepare('x').all(); return c.json({ items: [] }, 200, { 'Cache-Control': 'public, max-age=30' }); });
  a.get('/api/picks', async c => { await c.env.DB.prepare('x').all(); return c.json([Math.random()]); });
  a.get('/api/fail', async c => { await c.env.DB.prepare('x').all(); return c.json({ error: 'x' }, 500, { 'Cache-Control': 'no-store' }); });
  a.get('/jp/', async c => { await c.env.DB.prepare('x').all(); return c.text('nf', 404, { 'Cache-Control': 'no-store' }); });
  a.post('/api/requests', async c => { await c.env.DB.prepare('x').run(); return c.json({ ok: true }, 202); });
  extraRoutes(a);
  return (url, init) => a.request(url, init, { DB: mkDb(500, calls) });
};

test('キャッシュ: 2 回目は D1 を読まず、本文と元の Cache-Control が同じ・X-D1-Rows-Read は 0', async () => {
  const calls = { n: 0 }, cache = fakeCache(), req = cachedApp(cache, calls);
  const r1 = await req('/api/sites?q=abc'), b1 = await r1.text();
  assert.equal(calls.n, 1); assert.equal(r1.headers.get(ROWS_HEADER), '500'); assert.equal(r1.headers.get('X-Cache'), 'MISS');
  const r2 = await req('/api/sites?q=abc'), b2 = await r2.text();
  assert.equal(calls.n, 1, 'ヒットで D1 を読まない');
  assert.equal(b2, b1); assert.equal(r2.headers.get('X-Cache'), 'HIT'); assert.equal(r2.headers.get(ROWS_HEADER), '0');
  assert.equal(r2.headers.get('Cache-Control'), 'public, max-age=30', '保存用の TTL でなく元のヘッダに戻す');
  assert.equal(r2.headers.get('X-Mise-Orig-Cache-Control'), null);
  assert.equal(cache.m.get('http://localhost/api/sites?q=abc').headers.get('Cache-Control'), `public, max-age=${CACHE_TTL}`);
  await req('/api/sites?q=abd'); assert.equal(calls.n, 2, 'クエリが違えば別キー');
});

test('キャッシュ対象外: picks（ランダム）・POST・Authorization 付き・エラー応答', async () => {
  const calls = { n: 0 }, cache = fakeCache(), req = cachedApp(cache, calls);
  await req('/api/picks'); await req('/api/picks'); assert.equal(calls.n, 2); assert.equal(cache.m.size, 0);
  await req('/api/requests', { method: 'POST', body: '{}' }); await req('/api/requests', { method: 'POST', body: '{}' }); assert.equal(calls.n, 4);
  await req('/api/sites', { headers: { Authorization: 'Bearer x' } }); await req('/api/sites', { headers: { Authorization: 'Bearer x' } }); assert.equal(calls.n, 6); assert.equal(cache.m.size, 0);
  await req('/api/fail'); await req('/api/fail'); assert.equal(calls.n, 8); assert.equal(cache.m.size, 0);
  assert.equal(isCacheable(new Request('http://x/icon/abc.webp')), false);
  assert.equal(isCacheable(new Request('http://x/api/sites', { method: 'POST' })), false);
});

test('キャッシュ: 画面側の 404 は保存する（API の 404 は保存しない）', async () => {
  const calls = { n: 0 }, cache = fakeCache(), req = cachedApp(cache, calls);
  const a = await req('/jp/?page=9'), b = await req('/jp/?page=9');
  assert.equal(a.status, 404); assert.equal(b.status, 404); assert.equal(calls.n, 1); assert.equal(b.headers.get('Cache-Control'), 'no-store');
});

test('キャッシュ: Accept-Language・Cookie は結果を変えないので鍵に含めない（同じ応答を返す）', async () => {
  const calls = { n: 0 }, cache = fakeCache(), req = cachedApp(cache, calls);
  await req('/api/sites', { headers: { 'Accept-Language': 'ja' } });
  await req('/api/sites', { headers: { 'Accept-Language': 'fr', Cookie: 'a=b' } });
  assert.equal(calls.n, 1);
});

test('キャッシュの失敗（match / put が例外）でも通常どおり応答する', async () => {
  const calls = { n: 0 }, bad = { match: async () => { throw new Error('boom'); }, put: async () => { throw new Error('boom'); } };
  const r = await cachedApp(bad, calls)('/api/sites');
  assert.equal(r.status, 200); assert.equal(calls.n, 1);
});

test('実アプリ: caches が無い環境（テスト・未対応）では従来どおり動く', async () => {
  const r = await app.request('/api/scopes', {}, { DB: mkDb(1) });
  assert.equal(r.status, 200); assert.equal(r.headers.get('Cache-Control'), 'public, max-age=30');
  assert.equal(r.headers.get('X-Cache'), null);
});
