#!/usr/bin/env node
// 自己検査（外部アクセスなし）: (1) /api/*（sites・picks・activity）の Cache-Control が成功時だけ max-age、エラー時（400/404/500）は no-store であること、
// (2) 入力検証（不正な cursor・limit・page・category）のステータス、(3) 最新の並びインデックスの migration が現在の SORT_KEYS と一致していること。
// 実行: node scripts/check-api-headers.mjs
import { readFileSync } from 'node:fs';
import app from '../src/index.js';
import { sortIndexSql } from '../src/sites.js';
const stmt = { all: async () => ({ results: [] }), first: async () => ({ total: 0 }), run: async () => ({ meta: { changes: 1 } }) };
stmt.bind = () => stmt;
const okDb = { prepare: () => stmt };
const badStmt = { all: async () => { throw new Error('D1 down'); }, first: async () => { throw new Error('D1 down'); } };
badStmt.bind = () => badStmt;
const badDb = { prepare: () => badStmt };
const noCatStmt = { all: async () => ({ results: [] }), first: async () => null };
noCatStmt.bind = () => noCatStmt;
const noCatDb = { prepare: () => noCatStmt };
const cases = [
  ['成功 /api/sites', '/api/sites', okDb, 200, 'public, max-age=30'],
  ['成功 /api/picks', '/api/picks', okDb, 200, 'public, max-age=30'],
  ['不正カーソル /api/sites', '/api/sites?cursor=zzz', okDb, 400, 'no-store'],
  ['limit=0 /api/sites', '/api/sites?limit=0', okDb, 400, 'no-store'],
  ['page 7 桁 /api/sites', '/api/sites?page=1234567', okDb, 400, 'no-store'],
  ['存在しない category /api/sites', '/api/sites?category=nope', noCatDb, 404, 'no-store'],
  ['成功 /api/activity', '/api/activity', okDb, 200, 'public, max-age=30'],
  ['D1 故障 /api/sites', '/api/sites', badDb, 500, 'no-store'],
  ['存在しない /api/nope', '/api/nope', okDb, 404, 'no-store'],
  // POST /api/requests は成功・失敗とも no-store(GET の max-age を引きずらない)
  ['POST 受理 /api/requests', '/api/requests', okDb, 202, 'no-store', { method: 'POST', headers: { Origin: 'http://localhost', 'Content-Type': 'application/json' }, body: JSON.stringify({ type: 'add', url: 'https://example.com/', contact: 'a@example.com' }) }, { INTAKE_SALT: 's' }],
  ['POST 不正 /api/requests', '/api/requests', okDb, 400, 'no-store', { method: 'POST', headers: { Origin: 'http://localhost', 'Content-Type': 'application/json' }, body: '{}' }, { INTAKE_SALT: 's' }],
  ['POST 塩なし /api/requests', '/api/requests', okDb, 503, 'no-store', { method: 'POST', headers: { Origin: 'http://localhost', 'Content-Type': 'application/json' }, body: '{}' }],
];
let failed = 0;
for (const [label, path, DB, status, cache, init = {}, env = {}] of cases) {
  const res = await app.request(path, init, { DB, ...env });
  const ok = res.status === status && res.headers.get('Cache-Control') === cache;
  console.log(`${ok ? 'OK  ' : 'NG  '}${label}: ${res.status} ${res.headers.get('Cache-Control')}`);
  if (!ok) failed++;
}
// 並びの定義（src/sites.js の SORT_KEYS）と、それを元にした式インデックスが migration に入っていること（定義だけ変えて migration を忘れるのを防ぐ）。
import { readdirSync } from 'node:fs';
const files = readdirSync(new URL('../migrations/', import.meta.url)).filter(f => f.endsWith('.sql')).sort();
const idxFiles = files.filter(f => readFileSync(new URL('../migrations/' + f, import.meta.url), 'utf8').includes('idx_sites_sort'));
const latest = idxFiles.at(-1), idx = sortIndexSql();
const idxOk = !!latest && readFileSync(new URL('../migrations/' + latest, import.meta.url), 'utf8').includes(idx);
console.log(`${idxOk ? 'OK  ' : 'NG  '}最新の並びインデックスの migration (${latest}) に現在の SORT_KEYS の CREATE INDEX がある: ${idx}`);
if (!idxOk) failed++;
process.exit(failed ? 1 : 0);
