// 計測用: 全ページをカーソルでたどった合計 rows_read と 1 ページの最大、ピックアップの偏りと読み取りを測る。#126
// usage: node scripts/bench/bench-traverse.mjs <persistDir> [picks=600]    ローカルの D1 だけ（getPlatformProxy。--remote は使えない）。
//   本番相当のデータ（一括投入が rowid の連続した塊で、日時が同値）は、本番の export をローカル D1 に読み込むか、gen-synthetic-sites.mjs の PROD_SHAPE=1 で作る。
//   旧コードとの比較: 環境変数 SRC=<旧コードの src ディレクトリ> を付けて同じ DB に流す。索引あり/なしは DB（persistDir）を分けて測る。
import { getPlatformProxy } from 'wrangler';
const root = new URL('../../', import.meta.url).pathname, dir = process.argv[2], picksN = Number(process.argv[3] ?? 600);
const base = process.env.SRC ?? root + 'src';
const { querySites, randomSites, decodeCursor } = await import(base + '/sites.js');
const { env, dispose } = await getPlatformProxy({ configPath: root + 'wrangler.jsonc', persist: { path: dir + '/v3' } });
let acc;
const wrap = s => ({ bind: (...a) => wrap(s.bind(...a)), all: async () => { const r = await s.all(); acc.read += r.meta.rows_read; acc.stmts++; return r; }, first: async () => { const r = await s.all(); acc.read += r.meta.rows_read; acc.stmts++; return r.results[0] ?? null; } });
const db = { prepare: sql => wrap(env.DB.prepare(sql)) };
const raw = async sql => (await env.DB.prepare(sql).all()).results;
const topCats = await raw(`SELECT category_id c, COUNT(*) n FROM site_categories GROUP BY category_id ORDER BY n DESC`);
const tags = await raw(`SELECT label_ja t, COUNT(*) n FROM site_tags GROUP BY label_ja ORDER BY n DESC`);
const cases = [['既定', {}], ['jp', { cc: 'JP' }]];
cases.push([`カテゴリ最大(${topCats[0].n})`, { category: topCats[0].c }], [`カテゴリ最大+jp`, { category: topCats[0].c, cc: 'JP' }]);
const midCat = topCats.find(x => x.n >= 20 && x.n <= 300); if (midCat) cases.push([`カテゴリ中(${midCat.n})`, { category: midCat.c }]);
cases.push([`タグ最多(${tags[0].n})`, { tags: [tags[0].t] }]);
cases.push([`よく使うタグ5個AND`, { tags: tags.slice(0, 5).map(x => x.t) }]);
const rare = tags.find(x => x.n >= 5 && x.n <= 40); if (rare) cases.push([`タグ稀(${rare.n})`, { tags: [rare.t] }]);
const mid = tags.find(x => x.n >= 150 && x.n <= 700); if (mid) cases.push([`タグ中(${mid.n})`, { tags: [mid.t] }]);
cases.push(['q「写真」', { q: '写真' }], ['q「x」（関連度 #301）', { q: 'x' }], ['q「x.com」（関連度 #301）', { q: 'x.com' }], ['q「金沢市」（関連度 #301）', { q: '金沢市' }], ['q「blog official lab」（関連度 #301）', { q: 'blog official lab' }], ['q「yaho」（前方一致 #301）', { q: 'yaho' }], ['q「s」（前方一致・上限超え #301）', { q: 's' }], ['q「サイ」（前方一致・上限超え #301）', { q: 'サイ' }]);
const out = [];
for (const [label, o] of cases) {
  const pages = []; let cur = null, ids = [], total;
  for (let g = 0; g < 300; g++) {
    acc = { read: 0, stmts: 0 };
    const r = await querySites(db, { ...o, limit: 40, cursor: cur ? decodeCursor(cur) : null });
    pages.push(acc.read); ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (g === 0) total = r.total + (r.total_capped ? '+' : '');
    if (!cur) break;
  }
  console.error(label, pages.length, Date.now()); out.push({ label, pages: pages.length, sum: pages.reduce((a, b) => a + b, 0), max: Math.max(...pages), first: pages[0], total, ids: ids.length + ':' + ids.slice(0, 2).join(',') + ':' + ids.slice(-2).join(',') });
}
console.error(JSON.stringify(out));
const picks = [];
for (const [label, cc] of picksN ? [['全範囲', null], ['jp', 'JP'], ['us', 'US']] : []) {
  const cnt = new Map(); let reads = 0, short = 0;
  for (let i = 0; i < picksN; i++) { acc = { read: 0, stmts: 0 }; const r = await randomSites(db, cc, 3); reads += acc.read; if (r.length < 3) short++; for (const x of r) cnt.set(x.id, (cnt.get(x.id) ?? 0) + 1); }
  const top = [...cnt.entries()].sort((a, b) => b[1] - a[1]);
  console.error(label, "picks", Date.now()); picks.push({ label, calls: picksN, distinct: cnt.size, avgRowsRead: +(reads / picksN).toFixed(1), maxShare: +(top[0][1] / picksN).toFixed(3), top3: top.slice(0, 3).map(([k, v]) => k.slice(0, 28) + ' ' + (v / picksN).toFixed(3)), short });
}
await dispose();
console.log(JSON.stringify({ cases: out, picks }, null, 1));
