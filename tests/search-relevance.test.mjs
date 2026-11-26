import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { querySites, planQuery, decodeCursor, encodeCursor, siteOrder, SORT_KEYS, cleanQuery, searchText, RELEVANCE_MAX, RANK_REST } from '../src/sites.js';
import { SQL, chunkRanges } from '../scripts/build-search-fts.mjs';

// 検索の関連度（#301。migration 0041 の site_search_keys）。全 migration を適用したメモリ SQLite で、
//  (1) 鍵（ホスト名・ドメインの主要部・サイト名）がトリガ（登録・変更・削除）と migration の埋め直しで、独立に書いた JS の参照実装と一致する
//  (2) x・x.com で https://x.com/ が先頭。完全一致の段は bulk_import より前。同じ段の中は今の並び
//  (3) 全ページ連結（カーソル・?page=N・limit 違い）が参照実装と一致（重複・欠落なし）。範囲・カテゴリ・タグとの AND、索引（FTS）の経路と instr の経路
//  (4) 旧形式のカーソル（段の値なし）は今の並びのまま続く。完全一致が RELEVANCE_MAX 件を超える語・表が無いときは今の並び
// を検査する。
const MIG = new URL('../migrations/', import.meta.url);
const mkDb = () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  db.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');
  return db;
};
const wrap = db => ({ prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); } });
const buildFts = db => { db.exec(SQL.begin); const { lo, hi } = db.prepare(SQL.range).get(); for (const [a, b] of chunkRanges(lo, hi, 500)) db.exec(SQL.chunk(a, b)); db.exec(SQL.integrity); db.exec(SQL.ready); };

// ---- 鍵の参照実装（SQL のトリガとは独立に JS で書く。0041 の先頭のコメントの定義）----
const SLD = ['co', 'or', 'ne', 'ac', 'go', 'lg', 'ed', 'gr', 'ad', 'com', 'net', 'org', 'gov', 'edu'];
const refKeys = ({ url, name, name_ja, name_en }) => {
  const m = new Map(), add = (k, r) => { if (k && (!m.has(k) || m.get(k) > r)) m.set(k, r); };
  const i = url.indexOf('://'), rest = i >= 0 ? url.slice(i + 3) : '';
  const cut = rest.search(/[/?#]/), hp = (cut < 0 ? rest : rest.slice(0, cut)).toLowerCase(), tail = cut < 0 ? '' : rest.slice(cut);
  const deep = tail === '' || tail === '/' ? 0 : 1, h0 = hp.split(':')[0], h = /^www\..*\./.test(h0) ? h0.slice(4) : h0, labels = h.split('.');
  add(h, deep);
  add(labels.length === 2 || (labels.length === 3 && SLD.includes(labels[1])) ? labels[0] : null, 2 + deep);
  for (const n of [name, name_ja, name_en]) if (n != null) add(n.trim().toLowerCase(), 4);
  return m;
};
const keysOf = (db, id) => new Map(db.prepare('SELECT key, rank FROM site_search_keys WHERE site_id = ? ORDER BY key').all(id).map(r => [r.key, r.rank]));
const sortMap = m => new Map([...m].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));

// ---- テストデータ ----
const db = mkDb(), d = wrap(db);
const leafIds = db.prepare(`SELECT id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) ORDER BY id`).all().map(r => r.id);
const BIG = leafIds[0];
const insS = db.prepare('INSERT INTO sites (id,url,name,name_ja,name_en,description_ja,registered_at,source_updated_at,country_code,bulk_import,search_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)');
const insC = db.prepare('INSERT INTO site_categories (site_id,category_id) VALUES (?,?)'), insT = db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)');
const SITES = [];
const add = (id, url, name, { ja = null, en = null, desc = 'サイト', reg = '2026-03-01T00:00:00Z', upd = null, cc = 'JP', bulk = 0, cat = true, common = true } = {}) => {
  SITES.push({ id, url, name, name_ja: ja, name_en: en });
  insS.run(id, url, name, ja, en, desc, reg, upd, cc, bulk, searchText({ name, name_ja: ja, name_en: en, description_ja: desc, url }));
  if (cat) insC.run(id, BIG);
  if (common) insT.run(id, 'common', 'Common');
};
db.exec('BEGIN');
// 1,000 件を超える母数（q=x は .example の x で全件に当たる = instr の経路・総件数は上限付き）。日時は重複・NULL を混ぜる。
for (let i = 0; i < 1100; i++) {
  const day = i % 40, reg = i % 29 === 0 ? 'bad' : `2026-0${1 + (day % 9)}-1${day % 9}T00:00:00Z`, upd = i % 3 === 0 ? null : `2026-0${1 + (i % 7)}-2${i % 8}T00:00:00Z`;
  add('t' + String(i).padStart(4, '0'), `https://t${i}.example/`, i % 9 === 0 ? 'common name' : 'n' + i, { desc: `紹介 ${i}${i % 4 === 0 ? ' x.com の話' : ''}${i % 6 === 0 ? ' 金沢市' : ''}${i % 10 === 0 ? ' mid name の話' : ''}`, reg, upd, cc: i % 3 === 0 ? 'US' : 'JP', bulk: i % 13 === 0 ? 1 : 0, cat: i % 5 !== 0, common: i % 11 !== 0 });
}
// 完全一致の段に入るサイト（日付は古め = 今の並びでは下に沈む）
add('x-top', 'https://x.com/', 'X', { en: 'X', reg: '2020-01-01T00:00:00Z', cc: 'US' });
add('x-www-path', 'https://www.x.com/about', 'About X', { reg: '2021-01-01T00:00:00Z', cc: 'US' });
add('x-cojp', 'https://x.co.jp/', 'エックス', { reg: '2019-06-01T00:00:00Z' }); // 主要部 x が同じ別ドメイン（同じ段の中は日付順なので、新しければ x.com より上に来る）
add('x-named', 'https://named.example/', 'x', { reg: '2019-01-01T00:00:00Z' });
add('x-bulk-named', 'https://bulk.example/', 'X', { reg: '2019-01-01T00:00:00Z', bulk: 1 });
add('xbox', 'https://www.xbox.com/', 'Xbox', { reg: '2026-11-01T00:00:00Z', cc: 'US' });
add('dropbox', 'https://www.dropbox.com/', 'Dropbox', { reg: '2026-11-02T00:00:00Z', cc: 'US' });
add('sub-x', 'https://sub.x.com/', 'Sub', { reg: '2026-11-03T00:00:00Z', cc: 'US' }); // サブドメインは x.com とも x とも一致しない
add('kanazawa', 'https://www4.city.kanazawa.lg.jp/', '金沢市', { reg: '2026-11-13 00:00:00', bulk: 1 });
// 完全一致が 30 件（段をまたぐページ送り）。範囲・タグ・カテゴリも散らす。
for (let i = 0; i < 30; i++) add('mid' + String(i).padStart(2, '0'), `https://mid${i}.example/`, i % 2 ? 'Mid Name' : 'mid name', { reg: `2025-0${1 + (i % 9)}-0${1 + (i % 7)}T00:00:00Z`, cc: i % 3 ? 'JP' : 'US', bulk: i % 7 === 0 ? 1 : 0, cat: i % 4 !== 0, common: i % 5 !== 0 });
db.exec('COMMIT');
// 完全一致が RELEVANCE_MAX を超える語（common name は 1100 / 9 = 123 件）
assert.ok(SITES.filter(s => s.name === 'common name').length > RELEVANCE_MAX);
buildFts(db);

// ---- 並びの参照実装: 絞り込みは素朴な SQL、並びは siteOrder() の全件に、JS の鍵から求めた段を安定ソートで前に出す ----
const hitsInOrder = ({ cc, category, q, tags = [] }) => {
  const w = [], a = [];
  if (cc) { w.push('s.country_code = ?'); a.push(cc); }
  if (category) { w.push('(SELECT COUNT(*) FROM site_categories x WHERE x.site_id = s.id AND x.category_id = ?) > 0'); a.push(category); }
  for (const t of q ? q.trim().split(/\s+/u) : []) { w.push('instr(s.search_text, ?) > 0'); a.push(t); }
  for (const t of tags) { w.push('(SELECT COUNT(*) FROM site_tags x WHERE x.site_id = s.id AND (x.label_ja = ? OR x.label_en = ?)) > 0'); a.push(t, t); }
  return db.prepare(`SELECT s.id FROM sites s ${w.length ? 'WHERE ' + w.join(' AND ') : ''} ${siteOrder()}`).all(...a).map(r => r.id);
};
const ref = o => {
  const plain = hitsInOrder(o);
  if (!o.q) return plain;
  const byId = new Map(SITES.map(s => [s.id, refKeys(s)]));
  const exactAll = SITES.filter(s => byId.get(s.id).has(o.q));
  if (exactAll.length === 0 || exactAll.length > RELEVANCE_MAX) return plain;
  const rank = id => byId.get(id).get(o.q) ?? RANK_REST;
  return plain.map((id, i) => [id, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
};
const pagesOf = async (o, limit) => {
  const ids = []; let cur = null, first;
  for (let g = 0; g < 2000; g++) { const r = await querySites(d, { ...o, limit, cursor: cur ? decodeCursor(cur) : null }); if (g === 0) first = r; ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (!cur) break; }
  return { ids, first };
};

test('鍵: 登録のトリガが参照実装と一致する（www・ポート・パス・クエリ・主要部・名前）', () => {
  for (const s of SITES) assert.deepEqual(keysOf(db, s.id), sortMap(refKeys(s)), s.id);
  assert.deepEqual(keysOf(db, 'x-top'), new Map([['x', 2], ['x.com', 0]])); // 名前 X の鍵 x（rank 4）は主要部の x（rank 2）にまとまる
  assert.deepEqual(keysOf(db, 'x-www-path'), new Map([['about x', 4], ['x', 3], ['x.com', 1]]));
  assert.deepEqual(keysOf(db, 'x-cojp'), new Map([['x', 2], ['x.co.jp', 0], ['エックス', 4]]));
  assert.deepEqual(keysOf(db, 'sub-x'), new Map([['sub', 4], ['sub.x.com', 0]]));
  assert.deepEqual(keysOf(db, 'kanazawa'), new Map([['www4.city.kanazawa.lg.jp', 0], ['金沢市', 4]]));
});

test('鍵: 変更・削除のトリガと migration の埋め直しが参照実装と一致する。値が同じ UPDATE では作り直さない', () => {
  const t = mkDb();
  const ins = t.prepare('INSERT INTO sites (id,url,name,name_ja,name_en,description_ja) VALUES (?,?,?,?,?,?)');
  const rows = [
    ['a', 'https://WWW.Example.co.jp:8080/path?x=1', 'Example', null, 'EX'],
    ['b', 'http://example.com', '  Spaced Name ', '日本語名', null],
    ['c', 'https://x.com?lang=ja', 'X', null, null],
    ['d', 'https://www.com/', 'www', null, null],
    ['e', 'https://deep.sub.example.org/#top', 'Deep', null, null],
    ['f', 'not a url', 'Broken', null, null],
    ['g', 'https://shop.example.net/', '', null, null],
  ];
  for (const r of rows) ins.run(...r, 'd');
  const all = () => t.prepare('SELECT id, url, name, name_ja, name_en FROM sites ORDER BY id').all();
  for (const s of all()) assert.deepEqual(keysOf(t, s.id), sortMap(refKeys(s)), s.id);
  t.exec(`UPDATE sites SET url = 'https://www.example.com/', name = 'New' WHERE id = 'a'; UPDATE sites SET name_ja = NULL, name_en = 'English' WHERE id = 'b'`);
  for (const s of all()) assert.deepEqual(keysOf(t, s.id), sortMap(refKeys(s)), s.id);
  t.exec(`DELETE FROM sites WHERE id = 'd'`);
  assert.equal(t.prepare(`SELECT COUNT(*) n FROM site_search_keys WHERE site_id = 'd'`).get().n, 0);
  // 同じ値を SET し直す UPDATE はトリガの WHEN で何もしない（消した鍵が戻らない = 作り直していない）
  t.exec(`DELETE FROM site_search_keys WHERE site_id = 'e'; UPDATE sites SET url = url, name = name WHERE id = 'e'`);
  assert.equal(t.prepare(`SELECT COUNT(*) n FROM site_search_keys WHERE site_id = 'e'`).get().n, 0);
  // migration の埋め直し（ファイル末尾の DELETE + INSERT）で全件がトリガと同じになる
  const mig = readFileSync(new URL('0041_site_search_keys.sql', MIG), 'utf8');
  t.exec(mig.slice(mig.indexOf('DELETE FROM site_search_keys;')));
  for (const s of all()) assert.deepEqual(keysOf(t, s.id), sortMap(refKeys(s)), s.id);
});

test('x と x.com で https://x.com/ が先頭。完全一致の段は bulk_import と日付より前', async () => {
  for (const q of ['x', 'x.com', 'X.COM', ' x.com ', 'ｘ．ｃｏｍ']) {
    const r = await querySites(d, { q: cleanQuery(q), limit: 40 });
    assert.equal(r.items[0].url, 'https://x.com/', q);
  }
  const x = (await querySites(d, { q: 'x', limit: 40 })).items.map(i => i.id);
  // rank: x.com トップ(2: 主要部・トップ) = x.co.jp(2) > www.x.com/about(3) > 名前 x(4、通常 → 一括投入)。同じ rank の中は今の並び（日付の新しい順）
  assert.deepEqual(x.slice(0, 5), ['x-top', 'x-cojp', 'x-www-path', 'x-named', 'x-bulk-named']);
  const xc = (await querySites(d, { q: 'x.com', limit: 40 })).items.map(i => i.id);
  assert.deepEqual(xc.slice(0, 2), ['x-top', 'x-www-path']); // ホスト完全一致: トップ > パスあり（www の有無は問わない）
  assert.ok(xc.indexOf('xbox') > 1 && xc.indexOf('dropbox') > 1 && xc.indexOf('sub-x') > 1); // 〜x.com で終わるドメイン・サブドメインは段に入らない
  // 官公庁（一括投入）でも名前の完全一致なら、紹介文に名前を含む通常のサイトより上
  const k = (await querySites(d, { q: cleanQuery('金沢市'), limit: 40 })).items.map(i => i.id);
  assert.equal(k[0], 'kanazawa');
});

const cases = [
  ['x（1 文字・instr・上限超え）', { q: 'x' }],
  ['x.com（索引の経路）', { q: 'x.com' }],
  ['x + 範囲', { q: 'x', cc: 'US' }],
  ['x + カテゴリ', { q: 'x', category: BIG }],
  ['x.com + タグ', { q: 'x.com', tags: ['common'] }],
  ['mid name（完全一致 30 件・段をまたぐ）', { q: 'mid name' }],
  ['mid name + 範囲 + タグ', { q: 'mid name', cc: 'JP', tags: ['Common'] }],
  ['mid name + カテゴリ', { q: 'mid name', category: BIG }],
  ['金沢市', { q: cleanQuery('金沢市') }],
  ['common name（完全一致が上限超え = 段なし）', { q: 'common name' }],
  ['名前の完全一致 1 件（n10）', { q: 'n10' }],
  ['完全一致なし（example）', { q: 'example' }],
];
for (const [label, o] of cases) {
  test(`全ページ連結が参照実装と一致（重複・欠落なし）: ${label}`, async () => {
    const want = ref(o);
    assert.ok(want.length > 0);
    for (const limit of [1, 7, 40, 100]) {
      if (limit === 1 && want.length > 400) continue; // 1 件ずつは小さい集合だけ（時間）
      const { ids } = await pagesOf(o, limit);
      assert.deepEqual(ids, want, `limit=${limit}`);
      assert.equal(new Set(ids).size, ids.length);
    }
  });
}

test('?page=N（offset）の各ページが参照実装の区切りと一致し、総件数は段の有無で変わらない', async () => {
  for (const o of [{ q: 'mid name' }, { q: 'mid name', cc: 'JP' }, { q: 'x.com' }, { q: 'x' }]) {
    const want = ref(o), limit = 7;
    for (let p = 1; p <= Math.min(12, Math.ceil(want.length / limit) + 1); p++) {
      const r = await querySites(d, { ...o, limit, offset: (p - 1) * limit });
      assert.deepEqual(r.items.map(i => i.id), want.slice((p - 1) * limit, p * limit), `${JSON.stringify(o)} page=${p}`);
      assert.equal(r.total, Math.min(want.length, 1000));
      assert.equal(r.total_capped, want.length > 1000);
    }
  }
});

test('索引の経路と instr の経路で同じ並び（x.com）', async () => {
  const o = { q: 'x.com' }, want = ref(o);
  assert.equal((await planQuery(d, o)).qMode, 'fts');
  db.exec(`UPDATE fts_state SET ready = 0 WHERE name = 'sites_fts'`);
  try {
    assert.equal((await planQuery(d, o)).qMode, 'instr');
    assert.deepEqual((await pagesOf({ ...o, tags: [] }, 7)).ids, want);
  } finally { db.exec(`UPDATE fts_state SET ready = 1 WHERE name = 'sites_fts'`); }
});

test('カーソル: 段の値つき（要素が 1 つ多い）を受け付け、不正な段の値は拒否する', async () => {
  const r = await querySites(d, { q: 'mid name', limit: 7 });
  const c = decodeCursor(r.next_cursor);
  assert.equal(c.rel, true);
  assert.equal(c.vals.length, SORT_KEYS.length + 1);
  const tail = c.vals.slice(1);
  for (const bad of [-1, RANK_REST + 1, 1.5, '0', null]) assert.equal(decodeCursor(encodeCursor([bad, ...tail])), null, String(bad));
  assert.equal(decodeCursor(encodeCursor([0, ...tail, 'extra'])), null);
  assert.deepEqual(decodeCursor(encodeCursor(tail)), { vals: tail }); // 旧形式（段の値なし）
});

test('旧形式のカーソル（段の値なし）は今の並びのまま続く（重複・欠落なし）。検索語なしの段つきカーソルも今の並び', async () => {
  const o = { q: 'mid name' }, plain = hitsInOrder(o);
  const at = 3, row = db.prepare(`SELECT ${SORT_KEYS.map((k, i) => `${k.expr} AS _k${i}`).join(', ')} FROM sites s WHERE s.id = ?`).get(plain[at]);
  const vals = SORT_KEYS.map((_, i) => row[`_k${i}`] ?? null);
  const ids = []; let cur = decodeCursor(encodeCursor(vals));
  for (let g = 0; g < 100 && cur; g++) { const r = await querySites(d, { ...o, limit: 4, cursor: cur }); ids.push(...r.items.map(i => i.id)); cur = r.next_cursor && decodeCursor(r.next_cursor); assert.ok(!r.next_cursor || !cur.rel); }
  assert.deepEqual(ids, plain.slice(at + 1));
  const all = hitsInOrder({}), row2 = db.prepare(`SELECT ${SORT_KEYS.map((k, i) => `${k.expr} AS _k${i}`).join(', ')} FROM sites s WHERE s.id = ?`).get(all[10]);
  const r = await querySites(d, { limit: 5, cursor: decodeCursor(encodeCursor([0, ...SORT_KEYS.map((_, i) => row2[`_k${i}`] ?? null)])) });
  assert.deepEqual(r.items.map(i => i.id), all.slice(11, 16));
});

test('検索語なしの一覧・タグだけ・カテゴリだけは段を使わない（今の並び・カーソルは旧形式）', async () => {
  for (const o of [{}, { tags: ['common'] }, { category: BIG }, { cc: 'US' }]) {
    const r = await querySites(d, { ...o, limit: 40 });
    assert.deepEqual(r.items.map(i => i.id), hitsInOrder(o).slice(0, 40));
    assert.ok(!decodeCursor(r.next_cursor).rel);
    assert.equal((await planQuery(d, o)).relevance, false);
  }
});

test('site_search_keys が無い（migration 未適用・ロールバック後）ときは今の並び。段つきカーソルも落ちずに続く', async () => {
  const t = mkDb(), dt = wrap(t);
  const ins = t.prepare('INSERT INTO sites (id,url,name,description_ja,registered_at,bulk_import,search_text) VALUES (?,?,?,?,?,?,?)');
  const rows = [['a', 'https://x.com/', 'X', '2020-01-01T00:00:00Z'], ['b', 'https://xbox.example/', 'Xbox', '2026-01-01T00:00:00Z'], ['c', 'https://c.example/', 'C', '2025-01-01T00:00:00Z']];
  for (const [id, url, name, reg] of rows) ins.run(id, url, name, 'd', reg, 0, searchText({ name, description_ja: 'd', url }));
  const relCur = decodeCursor((await querySites(dt, { q: 'x', limit: 1 })).next_cursor); // 表があるうちに段つきカーソルを作る（x.com が先頭）
  assert.equal(relCur.rel, true);
  t.exec('DROP TRIGGER sites_search_keys_ai; DROP TRIGGER sites_search_keys_au; DROP TRIGGER sites_search_keys_ad; DROP TABLE site_search_keys;');
  const r = await querySites(dt, { q: 'x.', limit: 10 }); // 別の語（計画のキャッシュを使わない）
  assert.deepEqual(r.items.map(i => i.id), ['b', 'a']);
  assert.equal((await planQuery(dt, { q: 'x' })).relevance, false);
  const r2 = await querySites(dt, { q: 'x', limit: 10, cursor: relCur }); // 段つきカーソル + 表なし: 例外にせず今の並びの位置として続ける
  assert.ok(Array.isArray(r2.items));
});
