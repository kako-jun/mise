import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import app from '../src/index.js';
import { parseScope, scopePath, categoryHref, loadScopes, scopeNavHtml, sitemapXml } from '../src/seo.js';
import { scopeKeyOf } from '../src/sites.js';

// 範囲を国から都道府県・州まで広げる（#127）。URL・API の解釈と、404/301 の規則。件数と一覧の一致は tests/category-counts.test.mjs。
const MIG = new URL('../migrations/', import.meta.url);
const fresh = () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  return db;
};
const d = db => ({
  prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); },
  batch: async stmts => Promise.all(stmts.map(s => s.all())),
});
const get = (db, path) => app.request(path, { redirect: 'manual' }, { DB: d(db) });
const json = async (db, path) => { const r = await get(db, path); return { status: r.status, body: r.status === 200 ? await r.json() : null }; };

test('parseScope: all・国・区画。大文字や形の崩れは不正', () => {
  assert.deepEqual(parseScope(undefined), { scope: 'all', cc: null, region: null });
  assert.deepEqual(parseScope('all'), { scope: 'all', cc: null, region: null });
  assert.deepEqual(parseScope('jp'), { scope: 'jp', cc: 'JP', region: null });
  assert.deepEqual(parseScope('jp-13'), { scope: 'jp-13', cc: 'JP', region: 'JP-13' });
  assert.deepEqual(parseScope('us-ca'), { scope: 'us-ca', cc: 'US', region: 'US-CA' });
  for (const bad of ['JP', 'JP-13', 'jp-', 'jp-1234', 'j-13', 'jp-13-1', 'jpn', '-13', 'jp/13']) assert.equal(parseScope(bad), null, bad);
  assert.equal(scopeKeyOf(parseScope('jp-13')), 'JP-13'); assert.equal(scopeKeyOf(parseScope('jp')), 'JP'); assert.equal(scopeKeyOf(parseScope('all')), 'all');
});

test('URL: 範囲名 jp-13 <-> /jp/13/（カテゴリ・ページ番号つきも）', () => {
  assert.equal(scopePath('all'), '/all/'); assert.equal(scopePath('jp'), '/jp/'); assert.equal(scopePath('jp-13'), '/jp/13/'); assert.equal(scopePath('us-ca'), '/us/ca/');
  assert.equal(categoryHref([], 'jp-13'), '/jp/13/');
  assert.equal(categoryHref(['government', 'cities'], 'jp-13', 2), '/jp/13/?category=government&category=cities&page=2');
  assert.equal(categoryHref(['government'], 'jp'), '/jp/?category=government'); // 国は従来どおり
});

test('API: scope=jp-13（JP-13 も可）で区画に絞れる。不正は 400。未知の区画は空', async () => {
  const db = fresh();
  const sites = await json(db, '/api/sites?scope=jp-13&limit=100');
  assert.equal(sites.status, 200);
  assert.ok(sites.body.items.length > 0 && sites.body.items.every(s => s.region_code === 'JP-13'));
  const upper = await json(db, '/api/sites?scope=JP-13&limit=100');
  assert.deepEqual(upper.body.items.map(s => s.id), sites.body.items.map(s => s.id), '大文字の区画も同じ');
  assert.equal(sites.body.total, db.prepare("SELECT COUNT(*) n FROM sites WHERE region_code = 'JP-13'").get().n);
  assert.equal((await json(db, '/api/sites?scope=jp&limit=1')).body.total, db.prepare("SELECT COUNT(*) n FROM sites WHERE country_code = 'JP'").get().n, '国は区画を含む全体');
  for (const bad of ['jp-', 'jp-1234', 'JP', 'j-1', 'jp-13-1']) assert.equal((await get(db, '/api/sites?scope=' + bad)).status, 400, bad);
  const unknown = await json(db, '/api/sites?scope=jp-99');
  assert.deepEqual([unknown.status, unknown.body.items, unknown.body.total], [200, [], 0]);
  const cats = await json(db, '/api/categories?scope=jp-13');
  assert.equal(cats.status, 200);
  assert.equal(cats.body.find(c => c.id === 'government').site_count, db.prepare("SELECT COUNT(DISTINCT s.id) n FROM sites s JOIN site_categories x ON x.site_id = s.id WHERE s.region_code = 'JP-13' AND x.category_id IN (SELECT id FROM categories WHERE id = 'government' OR parent_id = 'government')").get().n);
  assert.equal((await json(db, '/api/categories?scope=jp-99')).body.every(c => c.site_count === 0), true);
  const act = await json(db, '/api/activity?scope=jp-13');
  assert.equal(act.body.total, sites.body.total, 'HUD の総数 = 区画の件数');
  assert.equal((await get(db, '/api/picks?scope=jp-13')).status, 200);
});

test('API /api/scopes: 区画の一覧（サイトのある区画だけ・コード順・件数つき）', async () => {
  const db = fresh();
  const { body } = await json(db, '/api/scopes');
  const want = db.prepare("SELECT region_code code, COUNT(*) count FROM sites WHERE region_code IS NOT NULL GROUP BY region_code ORDER BY region_code").all().map(r => ({ ...r }));
  assert.deepEqual(body.regions, want);
  assert.ok(body.regions.length >= 40 && body.regions.every(r => /^[A-Z]{2}-[A-Z0-9]{1,3}$/.test(r.code) && r.count > 0));
  assert.ok(body.countries.every(c => /^[A-Z]{2}$/.test(c.code)), '国の一覧に区画は混ざらない');
});

test('ページの URL: 未知の区画・不正な形は 404、大文字は小文字へ 301、末尾スラッシュなしは 301', async () => {
  const db = fresh();
  const have = db.prepare("SELECT region_code c FROM sites WHERE region_code IS NOT NULL LIMIT 1").get().c; // 例 JP-01
  const [cc, sub] = have.toLowerCase().split('-');
  for (const p of ['/jp/99/', '/jp/99', '/zz/01/', '/jp/ab-cd/', '/jp/01/02/', '/jp-01/', '/jp-01', '/all/01/']) assert.equal((await get(db, p)).status, 404, p);
  let r = await get(db, `/${cc.toUpperCase()}/${sub.toUpperCase()}/?category=government`);
  assert.equal(r.status, 301); assert.equal(new URL(r.headers.get('location')).pathname + new URL(r.headers.get('location')).search, `/${cc}/${sub}/?category=government`);
  r = await get(db, `/${cc}/${sub}?category=government`);
  assert.equal(r.status, 301); assert.equal(new URL(r.headers.get('location')).pathname + new URL(r.headers.get('location')).search, `/${cc}/${sub}/?category=government`);
  assert.equal((await get(db, '/JP/99/')).status, 301, '大文字は先に小文字へ（国の /ZZ/ と同じ。移動先で 404）');
  // 国のページは従来どおり
  assert.equal((await get(db, '/jp')).status, 301); assert.equal((await get(db, '/zz/')).status, 404);
});

// 0026 の戻し（scripts/rollback-region-counts.sql）: 適用 → 戻し で、0022 だけの状態と同じ集計表になり、旧 integrity.py（区画を知らない版）の検査が 0 行。
test('0026 の戻し SQL: 0022 のトリガと逐語一致し、戻した後は旧 integrity の国別検査が 0 行・以後の投入も旧トリガで数える', () => {
  const rb = readFileSync(new URL('../scripts/rollback-region-counts.sql', import.meta.url), 'utf8');
  const m22 = readFileSync(new URL('0022_category_counts.sql', MIG), 'utf8');
  for (const name of ['category_closure_au', 'category_counts_sc_ai', 'category_counts_sc_ad', 'category_counts_sc_au', 'category_counts_sites_ai', 'category_counts_sites_bd', 'category_counts_sites_au']) {
    const i = m22.indexOf('CREATE TRIGGER ' + name + ' '), t = m22.slice(i, m22.indexOf('\nEND;', i) + 5);
    assert.ok(rb.includes(t), name + ' は 0022 の逐語');
  }
  const only = f => { const db = new DatabaseSync(':memory:'); db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)'); for (const x of readdirSync(MIG).filter(x => x.endsWith('.sql')).sort().filter(f)) db.exec(readFileSync(new URL(x, MIG), 'utf8')); return db; };
  const snap = db => ['category_closure', 'category_counts', 'scope_counts'].map(t => db.prepare(`SELECT * FROM ${t} ORDER BY 1,2`).all().map(r => ({ ...r })));
  const db = only(() => true), base = only(f => f < '0026');
  db.exec(rb);
  assert.deepEqual(snap(db), snap(base), '戻した後 = 0022 だけの状態');
  // 旧 integrity.py（main 時点）の国別 2 本の SQL（逐語）
  const oldCountry = db.prepare("SELECT sc.scope FROM scope_counts sc WHERE sc.scope<>'all' AND sc.n<>(SELECT COUNT(*) FROM sites s WHERE s.country_code=sc.scope)").all();
  const oldCatExtra = db.prepare("SELECT cc.category_id FROM category_counts cc WHERE cc.scope<>'all' AND cc.n<>0 AND NOT EXISTS (SELECT 1 FROM sites s JOIN site_categories x ON x.site_id=s.id WHERE s.country_code=cc.scope)").all();
  assert.deepEqual([oldCountry, oldCatExtra], [[], []]);
  // 戻した後の投入（区画つき）も国・all だけ数える
  const n = () => db.prepare("SELECT n FROM scope_counts WHERE scope = 'JP'").get().n;
  const before = n();
  db.prepare("INSERT INTO sites (id,url,name,description_ja,country_code,region_code) VALUES ('rb1','https://rb1.example/','n','d','JP','JP-13')").run();
  assert.equal(n(), before + 1); assert.equal(db.prepare("SELECT COUNT(*) c FROM scope_counts WHERE scope LIKE '%-%'").get().c, 0);
});

test('ピックアップの区画経路: region_code の等値で idx_sites_region_sort を使う（activity の索引＋一時 B-tree にならない）', async () => {
  const db = fresh();
  const { randomPickSql } = await import('../src/sites.js');
  const plan = sql => db.prepare('EXPLAIN QUERY PLAN ' + sql).all('JP-13', 'x').map(r => r.detail).join('\n');
  const pr = plan(randomPickSql({ region: 'JP-13', nExclude: 1 }));
  assert.match(pr, /idx_sites_region_sort/); assert.doesNotMatch(pr, /idx_sites_activity/);
  assert.doesNotMatch(randomPickSql({ region: null }), /region_code = /); // 国・全世界の SQL は従来のまま
});

test('クライアント: app.js にプルダウン（region-select / regionSelect）が無い（#177）', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /region-select|regionSelect/);
});

test('style.css: JS 有効では区画リンクの枠を隠し、JS なしでは hidden の国を開いてボタンを隠す（#177）', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../public/style.css', import.meta.url), 'utf8');
  assert.match(css, /@media \(scripting: enabled\) \{ \.region-row \{ display: none; \} \}/);
  assert.match(css, /@media \(scripting: none\) \{ \.scope-list li\[hidden\] \{ display: list-item; \} \.scope-more \{ display: none; \} \}/);
});

test('SSR: 範囲ナビは app.js と同じ構造（先頭 MISE_INLINE_SCOPES 件＋残りは hidden＋もっと見るボタン。現在の国が残りなら全部開く）（#177）', async () => {
  const mk = n => ({ total: 9, regions: [], countries: Array.from({ length: n }, (_, i) => ({ code: ['JP','US','FR','GB','CN','DE','IT','KR','AU'][i], count: 1 })) });
  const n = globalThis.MISE_INLINE_SCOPES;
  assert.equal(n, 6);
  const html = scopeNavHtml(mk(9), 'jp');
  assert.equal((html.match(/<li hidden>/g) ?? []).length, 3);
  assert.match(html, /class="scope-more" aria-expanded="false"/);
  const open = scopeNavHtml(mk(9), 'au'); // 7 番目以降が現在 → 全部開く
  assert.doesNotMatch(open, /<li hidden>/);
  assert.match(open, /class="scope-more" aria-expanded="true"/);
  // 区画の範囲でも、その国の項目に aria-current="true"（範囲そのものは page ではない）。app.js と同じ。
  const reg = scopeNavHtml(mk(9), 'jp-13');
  assert.match(reg, /<a class="scope-item" href="\/jp\/"[^>]*aria-current="true"/);
  assert.doesNotMatch(reg, /aria-current="page"/);
  assert.doesNotMatch(scopeNavHtml(mk(9), 'jp'), /aria-current="true"/);
  assert.doesNotMatch(scopeNavHtml(mk(n), 'jp'), /scope-more|<li hidden>/); // 残りが無ければボタンも無い
});

test('台湾は国コードの代わりに旗画像を表示する', () => {
  const html = scopeNavHtml({ total: 1, regions: [], countries: [{ code: 'TW', count: 1 }] }, 'tw');
  assert.match(html, /src="\/flags\/tw\.svg"/);
  assert.doesNotMatch(html, /class="scope-code"[^>]*>TW/);
});

test('SSR: 国・区画のページに、サイトのある区画へのリンク（/jp/13/ 形式）が JS なしで出る。サイトの無い区画・区画の無い範囲では出ない', async () => {
  const db = fresh();
  const scopes = await loadScopes(d(db));
  for (const scope of ['jp', 'jp-13']) {
    const html = scopeNavHtml(scopes, scope);
    assert.match(html, /<details class="region-links">/);
    const hrefs = [...html.matchAll(/<a href="(\/jp\/\d\d\/)">/g)].map(m => m[1]);
    assert.deepEqual(hrefs, scopes.regions.filter(r => r.code.startsWith('JP-')).map(r => `/jp/${r.code.slice(3)}/`), scope + ': サイトのある区画だけ・コード順');
    assert.ok(hrefs.includes('/jp/13/') && !hrefs.includes('/jp/99/'));
    assert.doesNotMatch(html, /<select|region-select/); // プルダウンは撤去（#177）
  }
  assert.doesNotMatch(scopeNavHtml(scopes, 'all'), /region-links|region-select/);
  assert.doesNotMatch(scopeNavHtml({ ...scopes, regions: [] }, 'jp'), /region-links/);
});

test('sitemap: URL 数が上限を超えたら例外（黙って切り捨てない）', async () => {
  const db = fresh(), dd = d(db);
  const xml = await sitemapXml(dd);
  const n = (xml.match(/<loc>/g) || []).length;
  assert.ok(n > 100 && n <= 50000);
  assert.ok((await sitemapXml(dd, n)).includes('<urlset'), '上限ちょうどは通る');
  await assert.rejects(() => sitemapXml(dd, n - 1), /sitemap index/);
});
