#!/usr/bin/env node
// 自己検査（外部アクセスなし）: /api/* の Cache-Control が成功時だけ max-age、エラー時は no-store であること。
// 実行: node scripts/check-api-headers.mjs
import { readFileSync } from 'node:fs';
import app from '../src/index.js';
import { sortIndexSql } from '../src/sites.js';
const stmt = { all: async () => ({ results: [] }), first: async () => ({ total: 0 }) };
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
];
let failed = 0;
for (const [label, path, DB, status, cache] of cases) {
  const res = await app.request(path, {}, { DB });
  const ok = res.status === status && res.headers.get('Cache-Control') === cache;
  console.log(`${ok ? 'OK  ' : 'NG  '}${label}: ${res.status} ${res.headers.get('Cache-Control')}`);
  if (!ok) failed++;
}
// 並びの定義（src/sites.js の SORT_KEYS）と、それを元にした式インデックスが migration に入っていること（定義だけ変えて migration を忘れるのを防ぐ）。
import { readdirSync } from 'node:fs';
const migrations = readdirSync(new URL('../migrations/', import.meta.url)).filter(f => f.endsWith('.sql')).map(f => readFileSync(new URL('../migrations/' + f, import.meta.url), 'utf8')).join('\n');
const idx = sortIndexSql();
const idxOk = migrations.includes(idx);
console.log(`${idxOk ? 'OK  ' : 'NG  '}並びのインデックスが migration にある: ${idx}`);
if (!idxOk) failed++;
process.exit(failed ? 1 : 0);
