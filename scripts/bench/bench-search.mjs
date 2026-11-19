// 計測用: /api/sites の各パターンと /api/picks・/api/activity の rows_read を、ローカル D1（miniflare）に流して測る。#126
// usage: node scripts/bench/bench-search.mjs [persistDir=.wrangler/state]   ローカルの D1 だけ（getPlatformProxy。--remote は使えない）。
//   データは scripts/bench/gen-synthetic-sites.mjs で 1 万 / 10 万件に増やしてから測る。結果は JSON（rows_read は D1 の meta.rows_read の合計）。所要 ms はローカルの値で本番の速さではない。
//   切替前後の比較: 旧コードを `git worktree` で別に取り出し、環境変数 SRC=<旧コードの src> を付けて同じ DB に流す（索引の migration 前後は DB を分けて測る）。
import { getPlatformProxy } from 'wrangler';
const root = new URL('../../', import.meta.url).pathname, dir = process.argv[2] ?? root + '.wrangler/state';
const base = process.env.SRC ?? root + 'src'; // SRC=旧コードの src ディレクトリ（切替前の計測用）
const { querySites, randomSites, cleanQuery, cleanTags, encodeCursor, decodeCursor, SORT_KEYS } = await import(base + '/sites.js');
const { env, dispose } = await getPlatformProxy({ configPath: root + 'wrangler.jsonc', persist: { path: dir + '/v3' } });
let acc;
const wrap = s => ({ _raw: s, bind: (...a) => wrap(s.bind(...a)), all: async () => { const r = await s.all(); acc.read += r.meta.rows_read; acc.stmts++; return r; }, first: async () => { const r = await s.all(); acc.read += r.meta.rows_read; acc.stmts++; return r.results[0] ?? null; } });
const db = { prepare: sql => wrap(env.DB.prepare(sql)) };
const raw = async sql => (await env.DB.prepare(sql).all()).results;
const out = [];
async function m(label, fn) { acc = { read: 0, stmts: 0 }; const t0 = performance.now(); let info = ''; try { info = await fn(); } catch (e) { info = 'ERR ' + e.message; } out.push({ label, rows_read: acc.read, stmts: acc.stmts, ms: +(performance.now() - t0).toFixed(0), info: String(info ?? '') }); }
const topCat = (await raw(`SELECT category_id c, COUNT(*) n FROM site_categories GROUP BY category_id ORDER BY n DESC LIMIT 1`))[0];
const smallCat = (await raw(`SELECT category_id c, COUNT(*) n FROM site_categories GROUP BY category_id HAVING n BETWEEN 40 AND 60 ORDER BY n DESC LIMIT 1`))[0];
const midCat = (await raw(`SELECT category_id c, COUNT(*) n FROM site_categories GROUP BY category_id HAVING n BETWEEN 800 AND 3000 ORDER BY n DESC LIMIT 1`))[0];
const tags = await raw(`SELECT label_ja t, COUNT(*) n FROM site_tags GROUP BY label_ja ORDER BY n DESC`);
const topTag = tags[0], rareTag = tags.find(x => x.n >= 15 && x.n <= 25) ?? tags.find(x => x.n >= 2), midTag = tags.find(x => x.n >= 450 && x.n <= 650) ?? tags[Math.floor(tags.length / 20)];
const api = p => async () => { const r = await querySites(db, p); return `items=${r.items.length} total=${r.total}${r.total_capped ? '+' : ''}`; };
const Q = [['q 多ヒット「写真」', '写真'], ['q 多ヒット 3字+「プログラミング」', 'プログラミング'], ['q 少ヒット「tag1999」', 'tag1999'], ['q 0 ヒット', 'zzqqxx存在しない']];
for (const [lab, q] of Q) await m(`/api/sites?q= ${lab}`, api({ q: cleanQuery(q) }));
await m('/api/sites 既定', api({})); await m('/api/sites scope=jp', api({ cc: 'JP' }));
{ const r = await querySites(db, { limit: 40 }); await m('/api/sites 既定 cursor 2 ページ目', api({ cursor: decodeCursor(r.next_cursor) })); }
await m(`tag よく使う(${topTag.n}件)`, api({ tags: [topTag.t] })); await m(`tag 中程度(${midTag.n}件)`, api({ tags: [midTag.t] })); await m(`tag 稀(${rareTag.n}件)`, api({ tags: [rareTag.t] }));
await m('tag よく使う + scope=jp', api({ tags: [topTag.t], cc: 'JP' }));
await m(`tag 稀 + 他の語 2 つ AND`, api({ tags: [rareTag.t, topTag.t] }));
const keyRow = async (cat, off) => { const keys = SORT_KEYS.map((k, i) => `${k.expr} AS _k${i}`).join(', '); const row = (await raw(`SELECT ${keys} FROM sites s WHERE s.id IN (SELECT site_id FROM site_categories WHERE category_id='${cat}') ORDER BY ${SORT_KEYS.map(k => `${k.expr} ${k.dir}`).join(', ')} LIMIT 1 OFFSET ${off}`))[0]; return row && decodeCursor(encodeCursor(SORT_KEYS.map((_, i) => row[`_k${i}`] ?? null))); };
for (const off of [0, 1000, 9000]) await m(`category=最大(${topCat.n}件) offset ${off}`, api({ category: topCat.c, offset: off }));
{ const c = await keyRow(topCat.c, 8999); if (c) await m(`category=最大 cursor（9000 件目の次）`, api({ category: topCat.c, cursor: c })); }
await m(`category=中(${midCat?.n}件) offset 0`, api({ category: midCat.c })); await m(`category=小(${smallCat.n}件) offset 0`, api({ category: smallCat.c }));
await m(`category=最大 + scope=jp`, api({ category: topCat.c, cc: 'JP' })); await m(`category=最大 + q`, api({ category: topCat.c, q: cleanQuery('写真') }));
await m('/api/picks', async () => (await randomSites(db, null, 3)).length); await m('/api/picks scope=jp', async () => (await randomSites(db, 'JP', 3)).length);
await m('/api/activity（日別）', async () => (await db.prepare(`SELECT date(registered_at) AS day, COUNT(*) AS count FROM sites WHERE bulk_import = 0 AND datetime(registered_at) IS NOT NULL AND datetime(registered_at) <= datetime('now') AND date(registered_at) >= date('now','-6 days') AND (?1 IS NULL OR country_code = ?1) GROUP BY date(registered_at) ORDER BY day`).bind(null).all()).results.length);
await dispose();
console.log(JSON.stringify(out, null, 1));
