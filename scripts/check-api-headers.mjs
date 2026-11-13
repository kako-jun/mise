#!/usr/bin/env node
// 自己検査（外部アクセスなし）: /api/* の Cache-Control が成功時だけ max-age、エラー時は no-store であること。
// 実行: node scripts/check-api-headers.mjs
import app from '../src/index.js';
const stmt = { all: async () => ({ results: [] }), first: async () => ({ total: 0 }), run: async () => ({ meta: { changes: 1 } }) };
stmt.bind = () => stmt;
const okDb = { prepare: () => stmt };
const badStmt = { all: async () => { throw new Error('D1 down'); }, first: async () => { throw new Error('D1 down'); } };
badStmt.bind = () => badStmt;
const badDb = { prepare: () => badStmt };
const cases = [
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
process.exit(failed ? 1 : 0);
