#!/usr/bin/env node
// 自己検査（外部アクセスなし）: /api/* の Cache-Control が成功時だけ max-age、エラー時は no-store であること。
// 実行: node scripts/check-api-headers.mjs
import app from '../src/index.js';
const okDb = { prepare: () => ({ all: async () => ({ results: [] }), first: async () => ({ total: 0 }) }) };
const badDb = { prepare: () => { throw new Error('D1 down'); } };
const cases = [
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
process.exit(failed ? 1 : 0);
