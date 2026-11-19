import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { loadCategories, loadScopes, sitemapXml } from '../src/seo.js';
import { querySites } from '../src/sites.js';

// カテゴリ件数の集計表（migration 0022。#126）= 旧 loadCategories の再帰 CTE と常に完全一致すること。
// 全 migration を適用したメモリ SQLite（FK 有効。D1 と同じ）で、投入・削除・カテゴリ変更・国変更・カテゴリ木の変更の各操作の後に突き合わせる。
const MIG = new URL('../migrations/', import.meta.url);
const migs = () => readdirSync(MIG).filter(f => f.endsWith('.sql')).sort();
const fresh = (only = () => true) => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)'); // 一部の migration が参照する
  for (const f of migs().filter(only)) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  return db;
};
const apply = (db, f) => db.exec(readFileSync(new URL(f, MIG), 'utf8'));
const d = db => ({
  prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); },
  batch: async stmts => stmts.map(s => s.all ? { results: [] } : s), // 下の sitemap テストは batch を専用に差し替える
});
const TREE_CTE = `WITH RECURSIVE tree(root_id, id) AS (SELECT id, id FROM categories UNION SELECT tree.root_id, c.id FROM tree JOIN categories c ON c.parent_id = tree.id)`;
// 旧実装（再帰 CTE）の逐語。これが真値。
const truth = (db, cc) => db.prepare(`${TREE_CTE} SELECT c.id, COUNT(DISTINCT s.id) AS site_count FROM categories c LEFT JOIN tree ON tree.root_id = c.id LEFT JOIN site_categories sc ON sc.category_id = tree.id LEFT JOIN sites s ON s.id = sc.site_id AND (?1 IS NULL OR s.country_code = ?1 OR s.region_code = ?1) GROUP BY c.id`).all(cc);
const truthScopes = db => ({ total: db.prepare('SELECT COUNT(*) n FROM sites').get().n, countries: db.prepare(`SELECT country_code AS code, COUNT(*) AS count FROM sites WHERE country_code IS NOT NULL GROUP BY country_code ORDER BY count DESC, country_code`).all().map(r => ({ ...r })), regions: db.prepare(`SELECT region_code AS code, COUNT(*) AS count FROM sites WHERE region_code IS NOT NULL GROUP BY region_code ORDER BY region_code`).all().map(r => ({ ...r })) });

async function same(db, label) {
  // 範囲 = null（全世界）・国・区画（JP-13 など）。サイトの無い範囲（ZZ・JP-99）も 0 件で一致すること。
  const ccs = [null, ...new Set([...db.prepare('SELECT DISTINCT country_code c FROM sites WHERE country_code IS NOT NULL UNION SELECT DISTINCT region_code FROM sites WHERE region_code IS NOT NULL').all().map(r => r.c), 'JP', 'US', 'ZZ', 'JP-13', 'JP-99'])];
  for (const cc of ccs) {
    const got = new Map((await loadCategories(d(db), cc)).map(r => [r.id, r.site_count]));
    const want = truth(db, cc);
    assert.equal(got.size, want.length, `${label} [${cc}] カテゴリ数`);
    for (const w of want) assert.equal(got.get(w.id), w.site_count, `${label} [${cc}] ${w.id}`);
  }
  const t = truthScopes(db), g = await loadScopes(d(db));
  assert.equal(g.total, t.total, `${label} total`);
  assert.deepEqual(g.countries.filter(r => /^[A-Z]{2}$/.test(r.code)), t.countries.filter(r => /^[A-Z]{2}$/.test(r.code)), `${label} countries`);
  assert.deepEqual(g.regions, t.regions, `${label} regions`);
  // 閉包表 = 再帰 CTE
  const cl = db.prepare('SELECT ancestor_id a, descendant_id d FROM category_closure ORDER BY 1,2').all();
  const wantCl = db.prepare(`${TREE_CTE} SELECT root_id a, id d FROM tree ORDER BY 1,2`).all();
  assert.deepEqual(cl, wantCl, `${label} 閉包表`);
}

const leaves = db => db.prepare(`SELECT id, parent_id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) ORDER BY id`).all();
const addSite = (db, id, cc, cats, region = null) => {
  db.prepare('INSERT INTO sites (id,url,name,description_ja,country_code,region_code) VALUES (?,?,?,?,?,?)').run(id, 'https://' + id + '.example/', id, 'd', cc, region);
  for (const c of cats) db.prepare('INSERT OR IGNORE INTO site_categories (site_id,category_id) VALUES (?,?)').run(id, c);
};
const delSite = (db, id) => { db.prepare('DELETE FROM site_categories WHERE site_id = ?').run(id); db.prepare('DELETE FROM site_tags WHERE site_id = ?').run(id); db.prepare('DELETE FROM sites WHERE id = ?').run(id); };

test('初期集計: 全 migration 適用直後に再帰 CTE と一致', async () => { await same(fresh(), '初期'); });

// 0022 の初期集計（COUNT(DISTINCT) による重複排除）: 0021 までを当てた DB に「同じサイトが同じ祖先の下の複数の子孫に属する」行を先に入れてから 0022 を当てる。
// 初期集計を COUNT(sc.site_id) にするとこのテストが落ちる（トリガ経由の検査 = 他のテストでは初期集計は空の DB に対して走るので検出できない）。
function seedBeforeCounts(db) {
  const L = leaves(db), a = L[0], b = L.find(x => x.id !== a.id && x.parent_id === a.parent_id), other = L.find(x => x.parent_id !== a.parent_id);
  const top = db.prepare('SELECT parent_id FROM categories WHERE id = ?').get(a.parent_id).parent_id; // a の祖父（深さ 3 のとき）
  const deep = db.prepare('SELECT id FROM categories WHERE parent_id = ? AND id <> ? LIMIT 1').get(top ?? a.parent_id, a.parent_id);
  addSite(db, 'p1', 'JP', [a.id, b.id], 'JP-13');        // 兄弟 2 つ（親・祖先で重複）+ 区画
  addSite(db, 'p2', 'US', [a.id, b.id, other.id]);       // 兄弟 + 別の枝
  addSite(db, 'p3', 'GB', [a.parent_id, a.id]);          // 親そのものと子
  addSite(db, 'p4', null, [a.id, b.id]);                 // 国なし
  addSite(db, 'p5', 'JP', deep ? [a.id, deep.id] : [a.id], 'JP-13'); // 祖父の下の別の枝 + 区画（p1 と同じ区画で、祖先の重複が区画でも 1 件）
  addSite(db, 'p7', 'JP', [a.id], 'JP-01');              // 別の区画
  addSite(db, 'p8', 'US', [b.id], 'US-CA');              // 他国の区画
  addSite(db, 'p6', 'JP', []);                           // 所属なし
  return { a, b, other };
}
test('初期集計: 0021 までに重複所属があるDBへ 0022 → 0026 を当てた直後に再帰 CTE と一致（all・各国・各区画）', async () => {
  const db = fresh(f => f < '0022');
  seedBeforeCounts(db);
  apply(db, '0022_category_counts.sql');
  const after22 = db.prepare("SELECT COUNT(*) n FROM scope_counts WHERE scope LIKE '%-%'").get().n;
  assert.equal(after22, 0, '0022 は区画の行を作らない');
  apply(db, '0026_region_counts.sql');
  await same(db, '0026 直後');
  assert.ok(db.prepare("SELECT COUNT(*) n FROM scope_counts WHERE scope = 'JP-13'").get().n === 1 && db.prepare("SELECT n FROM scope_counts WHERE scope = 'JP-13'").get().n >= 2, '区画の範囲件数ができる');
  const { a } = { a: leaves(db)[0] };
  assert.equal(db.prepare("SELECT n FROM category_counts WHERE category_id = ? AND scope = 'all'").get(a.parent_id).n, truth(db, null).find(r => r.id === a.parent_id).site_count);
  assert.ok(db.prepare("SELECT COUNT(*) n FROM category_counts WHERE scope IN ('JP','US','GB')").get().n > 0);
});

test('作り直し SQL（scripts/rebuild-category-counts.sql）: 0022・0026 の初期集計と逐語一致し、壊れた集計表を再帰 CTE と同じに戻す', async () => {
  const m = readFileSync(new URL('0022_category_counts.sql', MIG), 'utf8');
  const initial = m.slice(m.indexOf('INSERT INTO category_closure'), m.indexOf('-- (4)')).trim();
  const m26 = readFileSync(new URL('0026_region_counts.sql', MIG), 'utf8');
  const initial26 = m26.slice(m26.indexOf('INSERT INTO category_counts'), m26.indexOf('-- (2)')).trim();
  const rebuild = readFileSync(new URL('../scripts/rebuild-category-counts.sql', import.meta.url), 'utf8');
  assert.ok(rebuild.includes(initial), '作り直し SQL は 0022 の初期集計をそのまま含む');
  assert.ok(rebuild.includes(initial26), '作り直し SQL は 0026 の初期集計（区画）をそのまま含む');
  const db = fresh(f => f < '0022');
  seedBeforeCounts(db);
  apply(db, '0022_category_counts.sql'); apply(db, '0026_region_counts.sql');
  const want = ['category_closure', 'category_counts', 'scope_counts'].map(t => db.prepare(`SELECT * FROM ${t} ORDER BY 1,2`).all().map(r => ({ ...r })));
  // 壊す: 件数のずれ・余計な行・消えた行
  db.exec("UPDATE category_counts SET n = n + 5 WHERE scope = 'all'; INSERT INTO scope_counts VALUES ('ZZ', 9); DELETE FROM category_closure WHERE ancestor_id <> descendant_id; DELETE FROM category_counts WHERE scope = 'JP'; DELETE FROM category_counts WHERE scope = 'JP-13'; UPDATE scope_counts SET n = n + 1 WHERE scope = 'JP-01'");
  await assert.rejects(() => same(db, '壊した後'), '壊れた状態は検出される');
  db.exec(rebuild);
  const got = ['category_closure', 'category_counts', 'scope_counts'].map(t => db.prepare(`SELECT * FROM ${t} ORDER BY 1,2`).all().map(r => ({ ...r })));
  assert.deepEqual(got, want, '作り直し後 = 0026 直後');
  await same(db, '作り直し後');
  // 作り直し後もトリガが働く
  addSite(db, 'after', 'JP', [leaves(db)[0].id]); await same(db, '作り直し後の投入');
});

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

test('区画の変更（#127）: 国だけ→区画あり・区画の付け替え・区画だけ外す・国ごと変える。国だけ分かる（区画 NULL）サイトは国の範囲にだけ数える', async () => {
  const db = fresh(), L = leaves(db);
  addSite(db, 'g1', 'JP', [L[0].id, L[3].id]); await same(db, '国だけ（区画 NULL）');
  const n = scope => db.prepare('SELECT n FROM scope_counts WHERE scope = ?').get(scope)?.n ?? 0;
  const base13 = n('JP-13'), base01 = n('JP-01'), baseJp = n('JP');
  db.prepare("UPDATE sites SET region_code = 'JP-13' WHERE id = 'g1'").run(); await same(db, '区画 NULL→JP-13');
  assert.deepEqual([n('JP-13') - base13, n('JP') - baseJp], [1, 0], '区画が増え、国は変わらない');
  db.prepare("UPDATE sites SET region_code = 'JP-01' WHERE id = 'g1'").run(); await same(db, '区画 JP-13→JP-01');
  assert.equal(n('JP-13'), base13); assert.equal(n('JP-01'), base01 + 1);
  db.prepare("UPDATE sites SET region_code = NULL WHERE id = 'g1'").run(); await same(db, '区画 →NULL（国には残る）');
  assert.equal(n('JP-01'), base01); assert.equal(n('JP'), baseJp);
  db.prepare("UPDATE sites SET region_code = 'JP-13' WHERE id = 'g1'").run();
  db.prepare("UPDATE sites SET country_code = 'US', region_code = 'US-CA' WHERE id = 'g1'").run(); await same(db, '国ごと JP/JP-13→US/US-CA');
  db.prepare("UPDATE sites SET country_code = NULL, region_code = NULL WHERE id = 'g1'").run(); await same(db, '国も区画も外す（all のみ）');
  db.prepare("UPDATE sites SET country_code = 'JP', region_code = 'JP-13' WHERE id = 'g1'").run(); await same(db, '同時に付ける');
  delSite(db, 'g1'); await same(db, '区画つきサイトの削除');
  assert.equal(n('JP-13'), base13);
});

test('querySites / randomSites: 区画で絞れる。国だけ分かるサイトは国にだけ出る。未知の区画は 0 件。総件数は一覧と一致', async () => {
  const db = fresh(), L = leaves(db), leaf = L[0].id;
  for (let i = 0; i < 5; i++) addSite(db, 'a' + i, 'JP', [leaf], 'JP-13');
  for (let i = 0; i < 3; i++) addSite(db, 'b' + i, 'JP', [leaf], 'JP-01');
  addSite(db, 'c0', 'JP', [leaf]); // 国だけ（区画 NULL）
  addSite(db, 'd0', 'US', [leaf], 'US-CA');
  const dd = d(db), ids = r => r.items.map(x => x.id);
  const r13 = await querySites(dd, { cc: 'JP', region: 'JP-13', limit: 100 });
  assert.ok(r13.items.length >= 5 && r13.items.every(x => x.region_code === 'JP-13'), '区画で絞れる');
  assert.equal(r13.total, db.prepare("SELECT COUNT(*) n FROM sites WHERE region_code = 'JP-13'").get().n, '総件数 = 実数（件数集計と一致）');
  assert.ok(!ids(r13).includes('c0') && !ids(r13).includes('b0') && !ids(r13).includes('d0'), '国だけ・別区画・他国は出ない');
  const jp = await querySites(dd, { cc: 'JP', limit: 100 });
  assert.ok(ids(jp).includes('c0') && ids(jp).includes('a0') && ids(jp).includes('b0') && !ids(jp).includes('d0'), '国の範囲には区画の有無によらず出る');
  assert.equal((await querySites(dd, { cc: 'JP', region: 'JP-99' })).total, 0, '未知の区画は 0 件');
  assert.deepEqual((await querySites(dd, { cc: 'JP', region: 'JP-99' })).items, []);
  // カテゴリ × 区画: 件数集計（category_counts）と一覧の件数が一致
  const rc = await querySites(dd, { cc: 'JP', region: 'JP-13', category: leaf, limit: 2 });
  assert.equal(rc.total, db.prepare("SELECT COUNT(*) n FROM site_categories x JOIN sites s ON s.id = x.site_id WHERE x.category_id = ? AND s.region_code = 'JP-13'").get(leaf).n);
  assert.equal((await loadCategories(dd, 'JP-13')).find(c => c.id === leaf).site_count, rc.total, '/api/categories の件数 = 一覧の total');
  // 続きのページ（カーソル）でも区画の外に出ない・重複しない
  const seen = []; let cursor = null;
  for (let guard = 0; guard < 50; guard++) { const pg = await querySites(dd, { cc: 'JP', region: 'JP-01', limit: 20, cursor }); seen.push(...ids(pg)); if (!pg.next_cursor) break; cursor = { vals: JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(pg.next_cursor.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0)))) }; }
  assert.equal(new Set(seen).size, seen.length); assert.deepEqual(seen.slice().sort(), db.prepare("SELECT id FROM sites WHERE region_code = 'JP-01'").all().map(r => r.id).sort());
  // ピックアップ
  const { randomSites } = await import('../src/sites.js');
  assert.deepEqual(await randomSites(dd, 'JP', 3, 'JP-99'), []);
  const picks = await randomSites(dd, 'JP', 3, 'JP-01');
  assert.ok(picks.length === 3 && picks.every(x => x.region_code === 'JP-01'));
});

test('区画の索引: JP-13 の一覧は idx_sites_region_sort を使う（他の区画の行を読み飛ばさない）', async () => {
  const db = fresh();
  const { siteOrder } = await import('../src/sites.js');
  const plan = db.prepare(`EXPLAIN QUERY PLAN SELECT s.id FROM sites s WHERE s.region_code = ? ${siteOrder()} LIMIT 41`).all('JP-13').map(r => r.detail).join('\n');
  assert.match(plan, /idx_sites_region_sort/); assert.doesNotMatch(plan, /TEMP B-TREE/, '並び替えの一時 B-tree が要らない');
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
  const pick = a => a[Math.floor(rnd() * a.length)], PAIRS = [['JP', 'JP-13'], ['JP', 'JP-01'], ['JP', null], ['US', 'US-CA'], ['US', null], ['GB', null], [null, null]]; // [国, 区画]（区画は国が必須で先頭 2 文字が一致する）
  const ids = [];
  for (let i = 0; i < 400; i++) {
    const r = rnd();
    if (r < 0.35 || !ids.length) { const id = 'r' + i, [cc, rc] = pick(PAIRS); addSite(db, id, cc, Array.from({ length: Math.floor(rnd() * 4) }, () => pick(allCats)), rc); ids.push(id); }
    else if (r < 0.5) { const j = Math.floor(rnd() * ids.length); delSite(db, ids[j]); ids.splice(j, 1); }
    else if (r < 0.7) db.prepare('INSERT OR IGNORE INTO site_categories VALUES (?,?)').run(pick(ids), pick(allCats));
    else if (r < 0.85) { const row = db.prepare('SELECT site_id, category_id FROM site_categories WHERE site_id = ? LIMIT 1').get(pick(ids)); if (row) db.prepare('DELETE FROM site_categories WHERE site_id = ? AND category_id = ?').run(row.site_id, row.category_id); }
    else { const [cc, rc] = pick(PAIRS); db.prepare('UPDATE sites SET country_code = ?, region_code = ? WHERE id = ?').run(cc, rc, pick(ids)); }
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
  addSite(db, 's1', 'JP', [L[0].id, L[3].id], 'JP-13'); addSite(db, 's2', 'US', [L[3].id]); addSite(db, 's3', null, [L[7].id]);
  const dd = d(db); dd.batch = async stmts => Promise.all(stmts.map(s => s.all()));
  const xml = await sitemapXml(dd);
  const got = [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map(m => m[1].replaceAll('&amp;', '&'));
  const { categoryHref, ORIGIN } = await import('../src/seo.js');
  const cats = db.prepare('SELECT id, parent_id FROM categories ORDER BY sort_order').all();
  const pathOf = id => { const p = []; for (let c = cats.find(x => x.id === id); c; c = cats.find(x => x.id === c.parent_id)) p.unshift(c.id); return p; };
  const want = [ORIGIN + '/about/'];
  const scopes = [['all', null], ...truthScopes(db).countries.map(r => [r.code.toLowerCase(), r.code]), ...truthScopes(db).regions.map(r => [r.code.toLowerCase(), r.code])]; // 区画は /jp/13/ 形式（categoryHref が scopePath で組む）
  for (const [scope, cc] of scopes) { want.push(ORIGIN + categoryHref([], scope)); const cnt = new Map(truth(db, cc).map(r => [r.id, r.site_count])); for (const c of cats) if (cnt.get(c.id) > 0) want.push(ORIGIN + categoryHref(pathOf(c.id), scope)); }
  assert.deepEqual(got.slice().sort(), want.sort()); // sort_order が同値のカテゴリの並びは未定義（旧実装も同じ）なので集合で比べる
});

// 本番の一致確認クエリ（scripts/llm/integrity.py）と、docs/operations/deploy.md の戻し方の SQL（文書から取り出す）をそのまま実行する。
const deployMd = readFileSync(new URL('../docs/operations/deploy.md', import.meta.url), 'utf8');
const cmdOf = re => { const m = deployMd.match(re); assert.ok(m, 'deploy.md に見つからない: ' + re); return m[1]; };
// 一致確認の 6 本は scripts/llm/integrity.py が正本（deploy.md は scripts/check-data-integrity.py を参照するだけ。#137）。順に [1]..[6]。
const CHECK_NAMES = ['category_counts_all', 'category_counts_country', 'category_counts_region', 'scope_counts_country', 'scope_counts_all', 'category_closure'];
const checkSqls = () => { const all = JSON.parse(execFileSync('uv', ['run', 'python3', 'scripts/check-data-integrity.py', '--print-sql'], { cwd: new URL('..', import.meta.url), encoding: 'utf8' })); return CHECK_NAMES.map(n => all[n]); };
const compoundTerms = q => (q.match(/\b(UNION ALL|UNION|EXCEPT|INTERSECT)\b/g) || []).length + 1;

test('一致確認クエリ(integrity.py の 6 本): 正しい DB では全部 0 行、わざとずらすとそれぞれの本が検出', async () => {
  const db = fresh(f => f < '0022');
  seedBeforeCounts(db);
  apply(db, '0022_category_counts.sql'); apply(db, '0026_region_counts.sql');
  const qs = checkSqls();
  assert.equal(qs.length, 6, '一致確認のクエリは 6 本');
  // D1 本番の複合 SELECT 項数上限に当たらないよう、各本の UNION ALL は 1 つ以下（再帰 CTE の UNION を含めても 4 項以内）
  for (const q of qs) assert.ok((q.match(/UNION ALL/g) || []).length <= 2 && compoundTerms(q) <= 4, '複合 SELECT が多すぎる: ' + q.slice(0, 60));
  const run = i => db.prepare(qs[i]).all().map(r => ({ ...r }));
  const runAll = () => qs.map((_, i) => run(i));
  const only = (i, n = 1) => { const all = runAll(); all.forEach((r, k) => k === i ? assert.equal(r.length, n, `[${i + 1}] が ${n} 行`) : assert.deepEqual(r, [], `[${k + 1}] は 0 行のはず(注入は [${i + 1}])`)); return all[i]; };
  assert.deepEqual(runAll(), [[], [], [], [], [], []], '一致している DB では全部 0 行');
  const a = leaves(db)[0].id, pa = leaves(db)[0].parent_id;
  // [2] 国別（JP）の件数を +1
  db.prepare("UPDATE category_counts SET n = n + 1 WHERE category_id = ? AND scope = 'JP'").run(a);
  let r = only(1); assert.deepEqual([r[0].id, r[0].scope, r[0].cached - r[0].expected], [a, 'JP', 1]);
  db.prepare("UPDATE category_counts SET n = n - 1 WHERE category_id = ? AND scope = 'JP'").run(a);
  // [1] all の件数を +1（親が二重に数えられた状態）
  db.prepare("UPDATE category_counts SET n = n + 1 WHERE category_id = ? AND scope = 'all'").run(pa);
  r = only(0); assert.deepEqual([r[0].id, r[0].cached - r[0].expected], [pa, 1]);
  db.prepare("UPDATE category_counts SET n = n - 1 WHERE category_id = ? AND scope = 'all'").run(pa);
  assert.deepEqual(runAll(), [[], [], [], [], [], []]);
  // [2] サイトの無い国（ZZ）に残った category_counts 行
  db.exec("INSERT INTO category_counts VALUES ('" + a + "', 'ZZ', 2)");
  r = only(1); assert.deepEqual([r[0].id, r[0].scope, r[0].expected, r[0].cached], [a, 'ZZ', 0, 2]);
  db.exec("DELETE FROM category_counts WHERE scope = 'ZZ'");
  // [3] 区画（JP-13）の件数を +1 / サイトの無い区画（JP-99）に残った行（0026）
  db.prepare("UPDATE category_counts SET n = n + 1 WHERE category_id = ? AND scope = 'JP-13'").run(a);
  r = only(2); assert.deepEqual([r[0].id, r[0].scope, r[0].cached - r[0].expected], [a, 'JP-13', 1]);
  db.prepare("UPDATE category_counts SET n = n - 1 WHERE category_id = ? AND scope = 'JP-13'").run(a);
  db.exec("INSERT INTO category_counts VALUES ('" + a + "', 'JP-99', 2)");
  r = only(2); assert.deepEqual([r[0].id, r[0].scope, r[0].expected, r[0].cached], [a, 'JP-99', 0, 2]);
  db.exec("DELETE FROM category_counts WHERE scope = 'JP-99'");
  db.exec("INSERT INTO scope_counts VALUES ('JP-99', 4)");
  r = only(3); assert.deepEqual([r[0].scope, r[0].expected, r[0].cached], ['JP-99', 0, 4]);
  db.exec("DELETE FROM scope_counts WHERE scope = 'JP-99'; UPDATE scope_counts SET n = n + 1 WHERE scope = 'JP-13'");
  r = only(3); assert.deepEqual([r[0].scope, r[0].cached - r[0].expected], ['JP-13', 1]);
  db.exec("UPDATE scope_counts SET n = n - 1 WHERE scope = 'JP-13'");
  // [3] サイトの無い国（ZZ）に残った scope_counts 行
  db.exec("INSERT INTO scope_counts VALUES ('ZZ', 3)");
  r = only(3); assert.deepEqual([r[0].scope, r[0].expected, r[0].cached], ['ZZ', 0, 3]);
  db.exec("DELETE FROM scope_counts WHERE scope = 'ZZ'");
  // [3] 範囲件数のずれ（JP を +1）
  db.exec("UPDATE scope_counts SET n = n + 1 WHERE scope = 'JP'");
  r = only(3); assert.deepEqual([r[0].scope, r[0].cached - r[0].expected], ['JP', 1]);
  db.exec("UPDATE scope_counts SET n = n - 1 WHERE scope = 'JP'");
  // [4] scope_counts(all) を +1
  db.exec("UPDATE scope_counts SET n = n + 1 WHERE scope = 'all'");
  r = only(4); assert.equal(r[0].cached - r[0].expected, 1);
  db.exec("UPDATE scope_counts SET n = n - 1 WHERE scope = 'all'");
  assert.deepEqual(runAll(), [[], [], [], [], [], []]);
  // [5] 閉包表の欠け・余り
  db.exec("DELETE FROM category_closure WHERE ancestor_id <> descendant_id AND descendant_id = '" + a + "'");
  assert.ok(run(5).some(x => x.kind === 'missing') && run(5).every(x => x.kind === 'missing'));
  apply_rebuild(db);
  assert.deepEqual(runAll(), [[], [], [], [], [], []]);
  db.exec("INSERT INTO category_closure VALUES ('" + a + "', (SELECT id FROM categories WHERE id <> '" + a + "' AND parent_id IS NULL LIMIT 1))");
  r = run(5); assert.ok(r.length >= 1 && r.every(x => x.kind === 'extra'));
});
const apply_rebuild = db => db.exec(readFileSync(new URL('../scripts/rebuild-category-counts.sql', import.meta.url), 'utf8'));

test('deploy.md の戻し方 SQL: トリガ 9 本と表 3 つを全部消し、そのあと通常の投入・削除が通る', async () => {
  const db = fresh(); addSite(db, 'g1', 'JP', [leaves(db)[0].id]);
  const sql = cmdOf(/d1 execute mise --remote --command "(DROP TRIGGER [^"]+)"/);
  assert.equal((sql.match(/DROP TRIGGER/g) || []).length, 9);
  const triggersBefore = db.prepare("SELECT name FROM sqlite_master WHERE type = 'trigger' AND (name LIKE 'category_closure_%' OR name LIKE 'category_counts_%')").all().length;
  assert.equal(triggersBefore, 9, 'migration が作るトリガは 9 本');
  db.exec(sql);
  assert.deepEqual(db.prepare("SELECT name FROM sqlite_master WHERE name LIKE 'category_closure%' OR name LIKE 'category_counts%' OR name LIKE 'scope_counts%' OR name LIKE 'idx_category_closure%'").all(), []);
  addSite(db, 'g2', 'US', [leaves(db)[0].id]); delSite(db, 'g1'); // 旧コードの操作は通る
});
