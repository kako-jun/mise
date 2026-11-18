import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { loadCategories, loadScopes, sitemapXml } from '../src/seo.js';
import { querySites } from '../src/sites.js';

// カテゴリ件数の集計表（migration 0022。#126）= 旧 loadCategories の再帰 CTE と常に完全一致すること。
// 全 migration を適用したメモリ SQLite（FK 有効。D1 と同じ）で、投入・削除・カテゴリ変更・国変更・カテゴリ木の変更の各操作の後に突き合わせる。
const MIG = new URL('../migrations/', import.meta.url);
const fresh = () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)'); // 一部の migration が参照する
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  return db;
};
const d = db => ({
  prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); },
  batch: async stmts => stmts.map(s => s.all ? { results: [] } : s), // 下の sitemap テストは batch を専用に差し替える
});
const TREE_CTE = `WITH RECURSIVE tree(root_id, id) AS (SELECT id, id FROM categories UNION SELECT tree.root_id, c.id FROM tree JOIN categories c ON c.parent_id = tree.id)`;
// 旧実装（再帰 CTE）の逐語。これが真値。
const truth = (db, cc) => db.prepare(`${TREE_CTE} SELECT c.id, COUNT(DISTINCT s.id) AS site_count FROM categories c LEFT JOIN tree ON tree.root_id = c.id LEFT JOIN site_categories sc ON sc.category_id = tree.id LEFT JOIN sites s ON s.id = sc.site_id AND (?1 IS NULL OR s.country_code = ?1) GROUP BY c.id`).all(cc);
const truthScopes = db => ({ total: db.prepare('SELECT COUNT(*) n FROM sites').get().n, countries: db.prepare(`SELECT country_code AS code, COUNT(*) AS count FROM sites WHERE country_code IS NOT NULL GROUP BY country_code ORDER BY count DESC, country_code`).all().map(r => ({ ...r })) });

async function same(db, label) {
  const ccs = [null, ...new Set([...db.prepare('SELECT DISTINCT country_code c FROM sites WHERE country_code IS NOT NULL').all().map(r => r.c), 'JP', 'US', 'ZZ'])];
  for (const cc of ccs) {
    const got = new Map((await loadCategories(d(db), cc)).map(r => [r.id, r.site_count]));
    const want = truth(db, cc);
    assert.equal(got.size, want.length, `${label} [${cc}] カテゴリ数`);
    for (const w of want) assert.equal(got.get(w.id), w.site_count, `${label} [${cc}] ${w.id}`);
  }
  const t = truthScopes(db), g = await loadScopes(d(db));
  assert.equal(g.total, t.total, `${label} total`);
  assert.deepEqual(g.countries.filter(r => /^[A-Z]{2}$/.test(r.code)), t.countries.filter(r => /^[A-Z]{2}$/.test(r.code)), `${label} countries`);
  // 閉包表 = 再帰 CTE
  const cl = db.prepare('SELECT ancestor_id a, descendant_id d FROM category_closure ORDER BY 1,2').all();
  const wantCl = db.prepare(`${TREE_CTE} SELECT root_id a, id d FROM tree ORDER BY 1,2`).all();
  assert.deepEqual(cl, wantCl, `${label} 閉包表`);
}

const leaves = db => db.prepare(`SELECT id, parent_id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) ORDER BY id`).all();
const addSite = (db, id, cc, cats) => {
  db.prepare('INSERT INTO sites (id,url,name,description_ja,country_code) VALUES (?,?,?,?,?)').run(id, 'https://' + id + '.example/', id, 'd', cc);
  for (const c of cats) db.prepare('INSERT OR IGNORE INTO site_categories (site_id,category_id) VALUES (?,?)').run(id, c);
};
const delSite = (db, id) => { db.prepare('DELETE FROM site_categories WHERE site_id = ?').run(id); db.prepare('DELETE FROM site_tags WHERE site_id = ?').run(id); db.prepare('DELETE FROM sites WHERE id = ?').run(id); };

test('初期集計: 全 migration 適用直後に再帰 CTE と一致', async () => { await same(fresh(), '初期'); });

test('投入・削除・カテゴリの付け外し・国の変更', async () => {
  const db = fresh(), L = leaves(db);
  // 兄弟（同じ親）の葉 2 つ = 同じサイトが同じ祖先の下の複数の子孫に属する → 祖先は 1 件
  const a = L[0], b = L.find(x => x.id !== a.id && x.parent_id === a.parent_id), other = L.find(x => x.parent_id !== a.parent_id);
  addSite(db, 'x1', 'JP', [a.id, b.id]); await same(db, '兄弟 2 つに所属');
  const parent = db.prepare('SELECT n FROM category_counts WHERE category_id = ? AND scope = ?').get(a.parent_id, 'all');
  assert.equal(parent.n - (truth(db, null).find(r => r.id === a.parent_id).site_count - 1) , 1, '親は二重に数えない');
  addSite(db, 'x2', null, [a.id]); await same(db, '国なし（all のみ）');
  addSite(db, 'x3', 'US', [other.id, a.id]); await same(db, '別の枝にも所属');
  addSite(db, 'x4', 'JP', []); await same(db, 'カテゴリなし');
  db.prepare('DELETE FROM site_categories WHERE site_id = ? AND category_id = ?').run('x1', a.id); await same(db, '兄弟の片方を外す（親は残る）');
  db.prepare('DELETE FROM site_categories WHERE site_id = ? AND category_id = ?').run('x1', b.id); await same(db, '兄弟の両方を外す（親も減る）');
  db.prepare('INSERT OR IGNORE INTO site_categories VALUES (?,?)').run('x3', a.id); await same(db, '重複 INSERT は無視');
  db.prepare("UPDATE sites SET country_code = 'GB' WHERE id = 'x3'").run(); await same(db, '国 US→GB');
  db.prepare("UPDATE sites SET country_code = NULL WHERE id = 'x3'").run(); await same(db, '国 →NULL');
  db.prepare("UPDATE sites SET country_code = 'JP' WHERE id = 'x2'").run(); await same(db, '国 NULL→JP');
  db.prepare("UPDATE sites SET name = 'renamed' WHERE id = 'x2'").run(); await same(db, '国以外の更新');
  db.prepare('UPDATE site_categories SET category_id = ? WHERE site_id = ? AND category_id = ?').run(other.id, 'x2', a.id); await same(db, '所属行の UPDATE');
  delSite(db, 'x3'); await same(db, 'サイト削除');
  delSite(db, 'x1'); delSite(db, 'x2'); delSite(db, 'x4'); await same(db, '全削除');
});

test('カテゴリ木の変更: 追加・親の付け替え（部分木ごと）・削除', async () => {
  const db = fresh(), L = leaves(db);
  const tops = db.prepare('SELECT id FROM categories WHERE parent_id IS NULL ORDER BY sort_order').all().map(r => r.id);
  const mid = db.prepare('SELECT id, parent_id FROM categories WHERE parent_id IS NOT NULL AND EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = categories.id) LIMIT 1').get();
  addSite(db, 'y1', 'JP', L.slice(0, 3).map(x => x.id)); addSite(db, 'y2', 'US', [L[1].id]); addSite(db, 'y3', 'JP', [L[5].id, L[6].id]);
  db.prepare("INSERT INTO categories VALUES ('zz-new', ?, '新', 'New', 99)").run(tops[0]); await same(db, 'カテゴリ追加');
  db.prepare("INSERT INTO categories VALUES ('zz-new2', 'zz-new', '新2', 'New2', 0)").run(); await same(db, '深い位置に追加');
  db.prepare("INSERT INTO site_categories VALUES ('y1','zz-new2'), ('y3','zz-new2')").run(); await same(db, '新カテゴリに所属');
  if (mid) { const other = tops.find(t => t !== mid.parent_id && t !== mid.id); db.prepare('UPDATE categories SET parent_id = ? WHERE id = ?').run(other, mid.id); await same(db, '中間カテゴリを別のトップへ'); db.prepare('UPDATE categories SET parent_id = ? WHERE id = ?').run(mid.parent_id, mid.id); await same(db, '元へ戻す'); }
  db.prepare("UPDATE categories SET parent_id = NULL WHERE id = 'zz-new'").run(); await same(db, '部分木をトップへ');
  db.prepare("UPDATE categories SET parent_id = ? WHERE id = 'zz-new'").run(tops[1]); await same(db, '部分木を別のトップへ');
  db.prepare("DELETE FROM site_categories WHERE category_id = 'zz-new2'").run(); await same(db, '所属を外す');
  db.prepare("DELETE FROM categories WHERE id IN ('zz-new2')").run(); db.prepare("DELETE FROM categories WHERE id = 'zz-new'").run(); await same(db, 'カテゴリ削除');
});

test('FK を後で満たす順序: サイトより先に所属行 / 所属行を残したままサイト削除（defer_foreign_keys）', async () => {
  const db = fresh(), L = leaves(db);
  db.exec('PRAGMA defer_foreign_keys = ON; BEGIN');
  db.prepare('INSERT INTO site_categories VALUES (?,?)').run('early', L[0].id);
  db.prepare('INSERT INTO sites (id,url,name,description_ja,country_code) VALUES (?,?,?,?,?)').run('early', 'https://early.example/', 'e', 'd', 'JP');
  db.exec('COMMIT'); await same(db, '所属行が先');
  db.exec('PRAGMA defer_foreign_keys = ON; BEGIN');
  db.prepare('DELETE FROM sites WHERE id = ?').run('early');
  db.prepare('DELETE FROM site_categories WHERE site_id = ?').run('early');
  db.exec('COMMIT'); await same(db, 'サイトが先に消える');
});

test('ランダム操作 400 回の後も一致（決定的乱数）', async () => {
  const db = fresh(), L = leaves(db), allCats = db.prepare('SELECT id FROM categories').all().map(r => r.id);
  let seed = 42; const rnd = () => { seed = (Math.imul(seed, 1103515245) + 12345) >>> 0; return seed / 2 ** 32; };
  const pick = a => a[Math.floor(rnd() * a.length)], CC = ['JP', 'US', 'GB', null];
  const ids = [];
  for (let i = 0; i < 400; i++) {
    const r = rnd();
    if (r < 0.35 || !ids.length) { const id = 'r' + i; addSite(db, id, pick(CC), Array.from({ length: Math.floor(rnd() * 4) }, () => pick(allCats))); ids.push(id); }
    else if (r < 0.5) { const j = Math.floor(rnd() * ids.length); delSite(db, ids[j]); ids.splice(j, 1); }
    else if (r < 0.7) db.prepare('INSERT OR IGNORE INTO site_categories VALUES (?,?)').run(pick(ids), pick(allCats));
    else if (r < 0.85) { const row = db.prepare('SELECT site_id, category_id FROM site_categories WHERE site_id = ? LIMIT 1').get(pick(ids)); if (row) db.prepare('DELETE FROM site_categories WHERE site_id = ? AND category_id = ?').run(row.site_id, row.category_id); }
    else db.prepare('UPDATE sites SET country_code = ? WHERE id = ?').run(pick(CC), pick(ids));
    if (i % 50 === 49) await same(db, `ランダム ${i + 1} 回`);
  }
  await same(db, 'ランダム 最終');
});

test('querySites の total（範囲だけ）は集計表、カテゴリ・q は従来の数え方', async () => {
  const db = fresh(), L = leaves(db);
  addSite(db, 'q1', 'JP', [L[0].id]); addSite(db, 'q2', 'US', [L[0].id]); addSite(db, 'q3', 'JP', []);
  const base = db.prepare('SELECT COUNT(*) n FROM sites').get().n - 3, baseJp = db.prepare("SELECT COUNT(*) n FROM sites WHERE country_code = 'JP'").get().n - 2; // 既存サイト分
  assert.equal((await querySites(d(db), {})).total, base + 3);
  assert.equal((await querySites(d(db), { cc: 'JP' })).total, baseJp + 2);
  assert.equal((await querySites(d(db), { cc: 'ZZ' })).total, 0);
  assert.equal((await querySites(d(db), { category: L[0].id })).total, db.prepare('SELECT COUNT(*) n FROM site_categories WHERE category_id = ?').get(L[0].id).n);
});

test('sitemap: 旧実装（範囲ごとに再帰 CTE）と同じ URL 列', async () => {
  const db = fresh(), L = leaves(db);
  addSite(db, 's1', 'JP', [L[0].id, L[3].id]); addSite(db, 's2', 'US', [L[3].id]); addSite(db, 's3', null, [L[7].id]);
  const dd = d(db); dd.batch = async stmts => Promise.all(stmts.map(s => s.all()));
  const xml = await sitemapXml(dd);
  const got = [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map(m => m[1].replaceAll('&amp;', '&'));
  const { categoryHref, ORIGIN } = await import('../src/seo.js');
  const cats = db.prepare('SELECT id, parent_id FROM categories ORDER BY sort_order').all();
  const pathOf = id => { const p = []; for (let c = cats.find(x => x.id === id); c; c = cats.find(x => x.id === c.parent_id)) p.unshift(c.id); return p; };
  const want = [ORIGIN + '/about/'];
  const scopes = [['all', null], ...truthScopes(db).countries.map(r => [r.code.toLowerCase(), r.code])];
  for (const [scope, cc] of scopes) { want.push(ORIGIN + categoryHref([], scope)); const cnt = new Map(truth(db, cc).map(r => [r.id, r.site_count])); for (const c of cats) if (cnt.get(c.id) > 0) want.push(ORIGIN + categoryHref(pathOf(c.id), scope)); }
  assert.deepEqual(got.slice().sort(), want.sort()); // sort_order が同値のカテゴリの並びは未定義（旧実装も同じ）なので集合で比べる
});
