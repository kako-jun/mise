import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { querySites, planQuery, decodeCursor, encodeCursor, siteOrder, SORT_KEYS, cleanQuery, searchText, relevanceKeys, prefixWord, RELEVANCE_MAX, RANK_REST, PREFIX_MAX, PREFIX_BASE, KEY_LEN_MAX } from '../src/sites.js';
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
// 前方一致の段（#301）: githu → github.com、yaho → Yahoo! JAPAN（長い名前の先頭）。日付は古め（今の並びでは下に沈む）
add('github', 'https://github.com/?locale=ja', 'GitHub', { reg: '2018-01-01T00:00:00Z', cc: 'US' });
add('chiebukuro', 'https://chiebukuro.yahoo.co.jp/', 'Yahoo!知恵袋 - みんなの知恵共有サービス', { reg: '2026-11-04T00:00:00Z' });
add('yahoo-auction', 'https://auctions.yahoo.co.jp/', 'Yahoo!オークション - 日本最大級のネットオークション・フリマアプリ', { reg: '2026-11-05T00:00:00Z' });
// 前方一致の鍵が PREFIX_MAX 行を超える語（dens）: 本命の短い名前 dense（古い・一括投入）と、長い名前の新しいサイト 150 件
add('dense', 'https://dense.example/', 'dense', { reg: '2015-01-01T00:00:00Z', bulk: 1 });
for (let i = 0; i < 150; i++) add('dn' + String(i).padStart(3, '0'), `https://dn${i}.example/`, 'densely packed long site name number ' + i, { reg: `2026-0${1 + (i % 9)}-2${i % 9}T00:00:00Z`, cc: i % 2 ? 'JP' : 'US', common: i % 3 !== 0 });
// 完全一致が 30 件（段をまたぐページ送り）。範囲・タグ・カテゴリも散らす。
// URL の形の検索語（候補の鍵）: www あり / なしの Yahoo! JAPAN 風の 2 件。www なしの方は search_text に www.yahoo.co.jp を含まない（部分一致の集合の外）
add('yahoo-www', 'https://www.yahoo.co.jp/', 'Yahoo! JAPAN', { reg: '2020-02-01T00:00:00Z' });
add('yahoo-bare', 'https://yahoo.co.jp/', 'ヤフー', { reg: '2020-03-01T00:00:00Z' });
// 完全一致がちょうど RELEVANCE_MAX 件（段あり）と RELEVANCE_MAX + 1 件（段なし）の境界
for (let i = 0; i < RELEVANCE_MAX; i++) add('e100-' + String(i).padStart(3, '0'), `https://e100-${i}.example/`, 'edge hundred', { reg: `2024-0${1 + (i % 9)}-1${i % 9}T00:00:00Z`, desc: 'edge' });
for (let i = 0; i < RELEVANCE_MAX + 1; i++) add('e101-' + String(i).padStart(3, '0'), `https://e101-${i}.example/`, 'edge overflow', { reg: `2024-0${1 + (i % 9)}-1${i % 9}T00:00:00Z`, desc: 'edge' });
// 境界の語を紹介文にだけ含む（完全一致でない）新しいサイト。段があれば完全一致の 100 件の後ろ、段がなければ日付順で先頭に来る
add('edge-desc-100', 'https://edge-desc-100.example/', 'edge note', { reg: '2026-11-20T00:00:00Z', desc: 'edge hundred の紹介' });
add('edge-desc-101', 'https://edge-desc-101.example/', 'edge note 2', { reg: '2026-11-20T00:00:00Z', desc: 'edge overflow の紹介' });
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
// 鍵を引く候補の参照実装: 検索語そのもの + scheme:// と末尾の / と（残りに . があれば）先頭の www. を除いた語
const refCands = q => { let t = q; const m = t.match(/^[a-z][a-z0-9+.-]*:\/\//); if (m) t = t.slice(m[0].length); while (t.endsWith('/')) t = t.slice(0, -1); if (t.startsWith('www.') && t.slice(4).includes('.')) t = t.slice(4); return t && t !== q ? [q, t] : [q]; };
// 前方一致の段の集合の参照実装: 最後の候補 c で始まり c より長い鍵（長さ KEY_LEN_MAX まで）を（長さ, 鍵, サイト id）の順に並べた先頭 PREFIX_MAX 行（索引なしで超えるなら作らない）。
//   サイトごとの score = min(鍵の長さ × 8 + rank)。
const refPrefix = (c, { index = true } = {}) => {
  const len = k => [...k].length, rows = [];
  for (const s of SITES) for (const [k, r] of refKeys(s)) if (k.startsWith(c) && len(k) > len(c) && len(k) <= KEY_LEN_MAX) rows.push({ k, id: s.id, sc: len(k) * 8 + r });
  if (rows.length > PREFIX_MAX && !index) return null;
  rows.sort((a, b) => len(a.k) - len(b.k) || (a.k < b.k ? -1 : a.k > b.k ? 1 : 0) || (a.id < b.id ? -1 : 1));
  const m = new Map(); for (const r of rows.slice(0, PREFIX_MAX)) if (!m.has(r.id) || m.get(r.id) > r.sc) m.set(r.id, r.sc);
  return m;
};
// 並び: 完全一致の段（候補のどれかの鍵を持つサイト。範囲・カテゴリ・タグで絞る。候補が検索語だけなら部分一致でも絞る）を rank → 今の並び、
// 次に前方一致の段（集合のうち完全一致でないサイト。同じ絞り込み）を score → 今の並び、最後に「部分一致 − 2 つの段の集合」を今の並び。
const ref = (o, opt) => {
  const plain = hitsInOrder(o);
  if (!o.q) return plain;
  const cands = refCands(o.q), byId = new Map(SITES.map(s => [s.id, refKeys(s)]));
  const rank = id => Math.min(...cands.map(c => byId.get(id).get(c) ?? RANK_REST));
  const exactAll = SITES.filter(s => rank(s.id) < RANK_REST);
  if (exactAll.length > RELEVANCE_MAX) return plain;
  const pw = cands.at(-1).startsWith('www.') && cands.at(-1).length > 4 ? cands.at(-1).slice(4) : cands.at(-1), loose = cands.length > 1 || pw !== o.q;
  const pm = refPrefix(pw, opt) ?? new Map();
  if (exactAll.length === 0 && pm.size === 0) return plain;
  const pool = (loose ? hitsInOrder({ ...o, q: null }) : plain).map((id, i) => [id, i]); // 今の並び（候補が 2 つなら部分一致で絞らない母集団）
  const exact = pool.filter(([id]) => rank(id) < RANK_REST).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  const prefix = pool.filter(([id]) => rank(id) === RANK_REST && pm.has(id)).sort((a, b) => pm.get(a[0]) - pm.get(b[0]) || a[1] - b[1]).map(x => x[0]);
  return [...exact, ...prefix, ...plain.filter(id => rank(id) === RANK_REST && !pm.has(id))];
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
  ['URL の形 www.x.com（段の x.com は部分一致の外）', { q: 'www.x.com' }],
  ['URL の形 https://x.com/', { q: 'https://x.com/' }],
  ['URL の形 https://www.x.com/ + 範囲 US', { q: 'https://www.x.com/', cc: 'US' }],
  ['URL の形 www.yahoo.co.jp', { q: 'www.yahoo.co.jp' }],
  ['URL の形 www.yahoo.co.jp + タグ', { q: 'www.yahoo.co.jp', tags: ['common'] }],
  ['完全一致ちょうど RELEVANCE_MAX 件（段あり）', { q: 'edge hundred' }],
  ['前方一致 githu', { q: 'githu' }],
  ['前方一致 yaho（長い名前の先頭・ドメイン）', { q: 'yaho' }],
  ['前方一致 yahoo!知（長い名前の先頭）', { q: cleanQuery('yahoo!知') }],
  ['前方一致の鍵が上限超え dens（短い鍵から PREFIX_MAX 行）', { q: 'dens' }],
  ['前方一致の鍵が上限超え dens + 範囲 + タグ', { q: 'dens', cc: 'JP', tags: ['common'] }],
  ['前方一致 1 文字 m（上限超え）', { q: 'm' }],
  ['完全一致 + 前方一致 mid', { q: 'mid' }],
  ['URL の形 www.githu（前方一致・部分一致の外）', { q: 'www.githu' }],
  ['完全一致 RELEVANCE_MAX + 1 件（段なし）', { q: 'edge overflow' }],
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

test('?page=N（offset）の各ページが参照実装の区切りと一致し、総件数は段の有無で変わらない（部分一致の外の段のサイトは足す）', async () => {
  for (const o of [{ q: 'mid name' }, { q: 'mid name', cc: 'JP' }, { q: 'x.com' }, { q: 'x' }, { q: 'www.x.com' }, { q: 'www.yahoo.co.jp' }, { q: 'https://www.x.com/', cc: 'US' }, { q: 'yaho' }, { q: 'dens' }, { q: 'dens', cc: 'JP' }, { q: 'mid' }, { q: 'www.githu' }]) {
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
  for (const bad of [-1, PREFIX_BASE + KEY_LEN_MAX * 8 + 5, 1.5, '0', null]) assert.equal(decodeCursor(encodeCursor([bad, ...tail])), null, String(bad));
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

test('URL の形の検索語: https://x.com/・www.x.com・https://www.x.com/ で x.com が先頭、www.yahoo.co.jp で www なしの方も段に入る。総件数は部分一致 + 段の外の分', async () => {
  for (const q of ['https://x.com/', 'www.x.com', 'https://www.x.com/', 'HTTPS://WWW.X.COM/']) assert.equal((await querySites(d, { q: cleanQuery(q), limit: 40 })).items[0].id, 'x-top', q);
  const y = await querySites(d, { q: 'www.yahoo.co.jp', limit: 40 });
  assert.deepEqual(y.items.map(i => i.id), ['yahoo-bare', 'yahoo-www']); // 同じ rank 0（ホスト一致・トップ）の中は今の並び（yahoo-bare の方が新しい）
  assert.equal(y.total, 2); // 部分一致は yahoo-www の 1 件だけ。段の外の yahoo-bare を足す
  const w = await querySites(d, { q: 'www.x.com', limit: 3 });
  assert.equal(w.total, ref({ q: 'www.x.com' }).length); // 末尾に届かない先頭ページ（数える経路）でも段の外の分を足す
  assert.ok(!hitsInOrder({ q: 'www.x.com' }).includes('x-top'));
});

test('候補の鍵（relevanceKeys）: scheme・末尾の /・先頭の www. を除いた語を足す。除いても同じなら検索語だけ', () => {
  const cases = [['x.com', ['x.com']], ['x', ['x']], ['https://x.com/', ['https://x.com/', 'x.com']], ['www.x.com', ['www.x.com', 'x.com']], ['https://www.yahoo.co.jp/', ['https://www.yahoo.co.jp/', 'yahoo.co.jp']],
    ['x.com/', ['x.com/', 'x.com']], ['www.com', ['www.com']], ['https://', ['https://']], ['///', ['///']], ['http://example.com/path/', ['http://example.com/path/', 'example.com/path']], ['金沢市', ['金沢市']]];
  for (const [q, want] of cases) { assert.deepEqual(relevanceKeys(q), want, q); assert.deepEqual(refCands(q), want, 'ref ' + q); }
});

test('境界: 完全一致がちょうど RELEVANCE_MAX 件なら段を使い、RELEVANCE_MAX + 1 件なら使わない', async () => {
  assert.equal((await planQuery(d, { q: 'edge hundred' })).relevance, true);
  assert.equal((await planQuery(d, { q: 'edge overflow' })).relevance, false);
  const h = (await querySites(d, { q: 'edge hundred', limit: 100 })).items.map(i => i.id);
  assert.ok(h.every(id => id.startsWith('e100-')) && h.length === RELEVANCE_MAX); // 段の 100 件が先、紹介文だけの新しいサイトは次のページ
  const o = (await querySites(d, { q: 'edge overflow', limit: 1 })).items.map(i => i.id);
  assert.deepEqual(o, ['edge-desc-101']); // 段なし: 日付の新しい紹介文だけのサイトが先頭
});

test('前方一致: githu → github.com、yaho → Yahoo! JAPAN が先頭。段の中は日付でなく語に近い順（短い鍵・ドメインのトップ）', async () => {
  assert.equal((await querySites(d, { q: 'githu', limit: 10 })).items[0].id, 'github');
  const y = (await querySites(d, { q: 'yaho', limit: 10 })).items.map(i => i.id);
  // yahoo（主要部・トップ。長さ 5）> yahoo.co.jp（ホスト・www なし・トップ。長さ 11）> 長い名前の 2 件（chiebukuro は名前より短いホスト chiebukuro.yahoo… が無いので名前の長さ）
  assert.deepEqual(y.slice(0, 2), ['yahoo-bare', 'yahoo-www'].sort((a, b) => ref({ q: 'yaho' }).indexOf(a) - ref({ q: 'yaho' }).indexOf(b)));
  assert.ok(y.indexOf('chiebukuro') > 1 && y.indexOf('yahoo-auction') > 1);
  assert.equal((await querySites(d, { q: cleanQuery('yahoo!知'), limit: 10 })).items[0].id, 'chiebukuro'); // 長い名前の先頭
  // 鍵が PREFIX_MAX 行を超える語でも、短い鍵（本命の dense。古い一括投入）が先頭
  assert.ok(refPrefix('dens').size > 0 && SITES.filter(s => s.name.startsWith('dens')).length > PREFIX_MAX);
  assert.equal((await querySites(d, { q: 'dens', limit: 10 })).items[0].id, 'dense');
  // x: 完全一致（x.com）の後に前方一致（xbox など短い鍵）、その後に今の並び
  const x = (await querySites(d, { q: 'x', limit: 40 })).items.map(i => i.id);
  assert.ok(x.includes('xbox')); // 今の並びでは 1,000 件の中の後ろの方
  assert.equal((await querySites(d, { q: 'www.githu', limit: 10 })).items[0].id, 'github'); // 打っている途中の URL の形
  for (const [q, w] of [['githu', 'githu'], ['www.githu', 'githu'], ['https://www.goo', 'goo'], ['www.', 'www.'], ['https://x.com/', 'x.com']]) assert.equal(prefixWord(q), w, q);
});

test('前方一致: 0042（長さの索引）が無いとき、鍵が PREFIX_MAX 行以下の語は段を作り、超える語は作らない（完全一致の段はそのまま）', async () => {
  db.exec('DROP INDEX idx_site_search_keys_len');
  try {
    for (const q of ['densel', 'githu', 'yahoo!', 'mi']) { // 計画のキャッシュに当たらない語
      const want = ref({ q: cleanQuery(q) }, { index: false });
      assert.deepEqual((await pagesOf({ q: cleanQuery(q) }, 7)).ids, want, q);
    }
    assert.equal((await planQuery(d, { q: 'densel' })).prefix, null);
    assert.ok(Object.keys((await planQuery(d, { q: 'githu' })).prefix).length > 0);
  } finally { db.exec(readFileSync(new URL('0042_site_search_keys_len.sql', MIG), 'utf8')); }
});

test('前方一致: 鍵が PREFIX_MAX 行を超える語は式インデックスを使い、読む鍵は PREFIX_MAX 行で止まる', () => {
  const plan = db.prepare(`EXPLAIN QUERY PLAN SELECT site_id FROM site_search_keys INDEXED BY idx_site_search_keys_len WHERE length(key) IN (5,6,7) AND key > 'dens' AND key < 'dens\u{10FFFF}' ORDER BY length(key), key LIMIT 100`).all().map(r => r.detail).join(' / ');
  assert.match(plan, /idx_site_search_keys_len/);
  assert.doesNotMatch(plan, /TEMP B-TREE/);
});
