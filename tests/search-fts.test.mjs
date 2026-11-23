import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { querySites, planQuery, decodeCursor, siteOrder, filters, TOTAL_CAP, PAGE_SIZE, cleanQuery, searchText, FTS_EXTRA_PROBE } from '../src/sites.js';
import { SQL, chunkRanges, parseArgs, runBuild } from '../scripts/build-search-fts.mjs';
import { changedRows } from '../scripts/refresh-search-text.mjs';

// 全文索引（#126 第3段。migration 0027 の sites_fts = search_text の FTS5 trigram）。
//  (1) 索引の経路（ready = 1）と instr の経路（ready = 0）の結果が完全に一致（項目 ID の並び・total・total_capped）。独立に書いた SQL の参照実装とも一致
//  (2) 1〜2 文字は instr に分岐する・構築前/表なしは instr に落ちる・稀なタグ/小さいカテゴリでは索引を使わない
//  (3) トリガ同期（登録・紹介文・タグ・削除）後の索引 = search_text。値が変わらない UPDATE では索引に触らない
//  (4) 構築スクリプトの冪等性（範囲を分けて何度流しても同じ・トリガで入った行と二重にならない）
//  (5) EXPLAIN で索引（MATCH）が使われる
const MIG = new URL('../migrations/', import.meta.url);
const mkDb = () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  return db;
};
const wrap = db => ({ prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); } });
const build = (db, size = 500) => {
  db.exec(SQL.begin);
  const { lo, hi } = db.prepare(SQL.range).get();
  for (const [a, b] of chunkRanges(lo, hi, size)) db.exec(SQL.chunk(a, b));
  db.exec(SQL.integrity); db.exec(SQL.ready);
};
const setReady = (db, v) => db.exec(`UPDATE fts_state SET ready = ${v} WHERE name = 'sites_fts'`);

const db = mkDb(), d = wrap(db);
const leafIds = db.prepare(`SELECT id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) ORDER BY id`).all().map(r => r.id);
const BIG = leafIds[0], SMALL = leafIds[1];
db.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');
const N = 1400; // TOTAL_CAP を超える語と、FTS_EXTRA_PROBE 以下の併用が作れる件数
db.exec('BEGIN');
const insS = db.prepare('INSERT INTO sites (id,url,name,description_ja,description_en,registered_at,source_updated_at,country_code,region_code,bulk_import,search_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)');
const insC = db.prepare('INSERT INTO site_categories (site_id,category_id) VALUES (?,?)'), insT = db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)');
for (let i = 0; i < N; i++) {
  const id = 'f' + String(i).padStart(4, '0');
  const day = i % 40, reg = `2026-0${1 + (day % 9)}-1${day % 9}T00:00:00Z`, upd = i % 3 === 0 ? null : `2026-0${1 + (i % 7)}-2${i % 8}T00:00:00Z`;
  const ja = `紹介 ${i} ${i % 2 ? 'りんご' : 'みかん'}のサイト${i % 350 === 4 ? ' ラーメン屋' : ''}${i % 5 === 0 ? ' 写真' : ''}${i % 90 === 6 ? ' Tokyo Cafe' : ''}${i % 700 === 3 ? ' zzrare' : ''}${i % 61 === 0 ? ' quote"mark' : ''}`;
  const en = `Site ${i} ${i % 2 ? 'APPLE' : 'pear'} garden${i % 77 === 0 ? ' ＰＲＯＧＲＡＭＭＩＮＧ' : ''}`;
  const cc = i % 17 === 0 ? null : (i % 3 === 0 ? 'US' : 'JP');
  const region = cc === 'JP' && i % 4 === 0 ? 'JP-13' : null;
  const row = { id, name: 'n' + id, description_ja: ja, description_en: en, url: `https://${id}.example/`, tags: [] };
  insS.run(id, row.url, row.name, ja, en, reg, upd, cc, region, i % 13 === 0 ? 1 : 0, searchText(row));
  if (i % 50 !== 0) insC.run(id, BIG);
  if (i % 260 === 5) insC.run(id, SMALL);
  if (i % 11 !== 0) insT.run(id, 'common', 'Common');
  if (i % 200 === 7) insT.run(id, 'rare', 'Rare');
}
db.exec('COMMIT');
// 構築前: 索引はトリガで入っているが ready = 0。構築スクリプトの SQL で作り直して ready = 1 にする。
assert.equal(db.prepare("SELECT ready FROM fts_state WHERE name = 'sites_fts'").get().ready, 0);
build(db);

const ref = ({ cc, region, category, q, tags = [] }) => {
  const w = [], a = [];
  if (region) { w.push('s.region_code = ?'); a.push(region); } else if (cc) { w.push('s.country_code = ?'); a.push(cc); }
  if (category) { w.push('(SELECT COUNT(*) FROM site_categories x WHERE x.site_id = s.id AND x.category_id = ?) > 0'); a.push(category); }
  if (q) for (const term of q.split(/\s+/u).filter(Boolean)) { w.push('instr(s.search_text, ?) > 0'); a.push(term); }
  for (const t of tags) { w.push('(SELECT COUNT(*) FROM site_tags x WHERE x.site_id = s.id AND (x.label_ja = ? OR x.label_en = ?)) > 0'); a.push(t, t); }
  return db.prepare(`SELECT s.id FROM sites s ${w.length ? 'WHERE ' + w.join(' AND ') : ''} ${siteOrder()}`).all(...a).map(r => r.id);
};
const pagesOf = async (o, limit = PAGE_SIZE) => {
  const ids = []; let cur = null, first;
  for (let g = 0; g < 200; g++) { const r = await querySites(d, { ...o, limit, cursor: cur ? decodeCursor(cur) : null }); if (g === 0) first = r; ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (!cur) break; }
  return { ids, first };
};

const terms = [
  ['日本語 3 文字以上・ヒット多数（上限超え）', 'サイト'], ['日本語 3 文字以上・中ヒット', 'りんご'], ['日本語 3 文字以上・少ヒット', 'ラーメン屋'],
  ['英語・ヒット多数', 'garden'], ['英語・大小無視', 'apple'], ['英語の部分一致', 'ardе'], ['複数語（全角・連続空白・逆順）', 'cafe　 tokyo'], ['複数語（日英の間）', 'りんご site'],
  ['全角英字（NFKC）', 'ＰＲＯＧＲＡＭ'], ['引用符入り', 'quote"mark'], ['0 件', 'zzqqxx'], ['1 件', 'zzrare'],
  ['2 文字（instr に分岐）', '写真'], ['1 文字（instr に分岐）', 'み'],
].map(([l, t]) => [l, cleanQuery(t)]);
const combos = [
  ['q のみ', {}], ['q + 範囲 国', { cc: 'JP' }], ['q + 範囲 国 US', { cc: 'US' }], ['q + 県', { cc: 'JP', region: 'JP-13' }],
  ['q + カテゴリ 大', { category: BIG }], ['q + カテゴリ 小', { category: SMALL }], ['q + タグ 多', { tags: ['common'] }], ['q + タグ 稀', { tags: ['rare'] }],
  ['q + 国 + カテゴリ 大', { cc: 'JP', category: BIG }],
];

for (const [tl, q] of terms) for (const [cl, extra] of combos) {
  test(`索引の経路 = instr の経路 = 参照実装（並び・total・total_capped・全ページ）: ${tl} / ${cl}`, async () => {
    const o = { ...extra, q }, want = ref(o);
    setReady(db, 1);
    const a = await pagesOf(o);
    setReady(db, 0);
    const b = await pagesOf(o);
    setReady(db, 1);
    assert.deepEqual(a.ids, want, '索引の経路の全ページ連結');
    assert.deepEqual(b.ids, want, 'instr の経路の全ページ連結');
    assert.equal(a.first.total, b.first.total, 'total'); assert.equal(a.first.total_capped, b.first.total_capped, 'total_capped');
    if (!a.first.total_capped) assert.equal(a.first.total, want.length); else { assert.equal(a.first.total, TOTAL_CAP); assert.ok(want.length > TOTAL_CAP); }
    // 続きのページ（先頭ページの判断を引き継ぐ）を limit を変えても同じ並びで
    assert.deepEqual((await pagesOf(o, 7)).ids, want);
  });
}

test('計画: 3 文字以上で該当が TOTAL_CAP 以下なら索引、超えたら instr（総件数は上限付き/併用なら索引で数える）。1〜2 文字・構築前・稀なタグ・小さいカテゴリでは索引を使わない', async () => {
  setReady(db, 1);
  const P = o => planQuery(d, o);
  assert.equal((await P({ q: 'りんご' })).qMode, 'fts');
  assert.equal((await P({ q: 'zzqqxx' })).qMode, 'fts');
  const over = await P({ q: 'サイト' });
  assert.deepEqual([over.qMode, over.qOver, over.qCountViaFts], ['instr', true, false]);
  const overJp = await P({ q: 'サイト', cc: 'JP' });
  assert.deepEqual([overJp.qMode, overJp.qOver, overJp.qCountViaFts], ['instr', true, true], '併用あり・該当が FTS_EXTRA_PROBE 以下: 総件数だけ索引');
  assert.ok(ref({ q: 'サイト' }).length > TOTAL_CAP && ref({ q: 'サイト' }).length <= FTS_EXTRA_PROBE);
  assert.equal((await P({ q: '写真' })).qMode, 'instr', '2 文字');
  assert.equal((await P({ q: 'み' })).qMode, 'instr', '1 文字');
  assert.equal((await P({ q: '写真' })).qOver, false);
  assert.equal((await P({ q: 'りんご', tags: ['rare'] })).qMode, 'instr', '稀なタグで引ける');
  assert.equal((await P({ q: 'りんご', category: SMALL })).qMode, 'instr', '小さいカテゴリ（IN 形・上限以下）');
  assert.equal((await P({ q: 'りんご', category: BIG })).qMode, 'fts');
  assert.equal((await P({ q: 'りんご', cc: 'JP' })).qMode, 'fts');
  assert.equal((await P({ q: 'りんご', tags: ['common'] })).qMode, 'fts');
  setReady(db, 0);
  const notReady = await P({ q: 'りんご' });
  assert.deepEqual([notReady.qMode, notReady.qOver], ['instr', false], '構築前（ready = 0）');
  setReady(db, 1);
  // 2 文字の MATCH は 0 件を返す（黙って取りこぼす）ので、instr に分岐していなければ結果が違ってしまう
  assert.equal(db.prepare('SELECT COUNT(*) c FROM sites_fts WHERE sites_fts MATCH ?').get('"写真"').c, 0);
  assert.equal((await querySites(d, { q: '写真' })).total, ref({ q: '写真' }).length);
});

test('表が無い（ロールバック後）・未構築でも検索は instr で動く', async () => {
  const db2 = mkDb(); db2.exec("INSERT INTO sites (id,url,name,description_ja) VALUES ('a','https://a.example/','n','りんごです')");
  const d2 = wrap(db2);
  const before = await querySites(d2, { q: cleanQuery('りんご') });
  assert.deepEqual(before.items.map(i => i.id), ['a'], '未構築（ready = 0）');
  db2.exec('DROP TRIGGER sites_fts_ai; DROP TRIGGER sites_fts_ad; DROP TRIGGER sites_fts_au; DROP TABLE sites_fts; DROP TABLE fts_state;');
  const after = await querySites(d2, { q: cleanQuery('りんご') });
  assert.deepEqual(after.items.map(i => i.id), ['a'], 'DROP 後（ロールバック）');
  db2.exec("INSERT INTO sites (id,url,name,description_ja) VALUES ('b','https://b.example/','n','りんご2')"); // トリガも無いので索引なしで登録できる
});

test('MATCH の引数: 引用符・空白・FTS5 の演算子に見える語を文字列として扱う', async () => {
  for (const t of ['quote"mark', 'tokyo cafe', 'りんご OR', 'not りんご', 'a*bc', '(りんご)', 'near(りんご']) {
    const q = cleanQuery(t), want = ref({ q });
    assert.deepEqual((await pagesOf({ q })).ids, want, t);
  }
});

test('EXPLAIN: 索引の経路は sites_fts を MATCH で引き、sites を全件走査しない。1〜2 文字と instr 経路は MATCH を使わない', () => {
  const sql = (o, plan) => { const f = filters(o, plan); return { sql: `SELECT s.id FROM sites s WHERE ${f.where.join(' AND ')} ${siteOrder()} LIMIT 41`, args: f.args }; };
  const detail = x => db.prepare('EXPLAIN QUERY PLAN ' + x.sql).all(...x.args).map(r => r.detail).join('\n');
  const fts = detail(sql({ q: 'りんご' }, { qMode: 'fts' }));
  assert.match(fts, /VIRTUAL TABLE INDEX \d+:M/, 'MATCH で索引を引く');
  assert.match(fts, /SEARCH s USING INTEGER PRIMARY KEY \(rowid=\?\)/, '行は rowid の点引き');
  assert.doesNotMatch(fts, /SCAN s\b/, 'sites の全件走査にならない');
  assert.doesNotMatch(detail(sql({ q: 'りんご' }, { qMode: 'instr' })), /sites_fts/);
  assert.doesNotMatch(detail(sql({ q: '写真' }, { qMode: 'instr' })), /sites_fts/);
  const withCat = detail(sql({ q: 'りんご', category: BIG }, { qMode: 'fts', categoryMode: 'exists' }));
  assert.match(withCat, /VIRTUAL TABLE INDEX \d+:M/);
});

// ---- トリガ同期 ----
const hits = (db, t) => db.prepare('SELECT rowid FROM sites_fts WHERE sites_fts MATCH ? ORDER BY rowid').all('"' + t + '"').map(r => r.rowid);
const truth = (db, t) => db.prepare('SELECT rowid FROM sites WHERE instr(search_text, ?) > 0 ORDER BY rowid').all(t).map(r => r.rowid);
const same = (db, ts, msg) => { for (const t of ts) assert.deepEqual(hits(db, t), truth(db, t), `${msg}: ${t}`); db.exec(SQL.integrity); };

test('トリガ同期: 登録・紹介文・タグの追加/変更/削除・サイト削除の後も、索引 = search_text', () => {
  const db2 = mkDb(); build(db2); // 空の索引を ready = 1 にしてから
  db2.exec("INSERT INTO sites (id,url,name,description_ja,description_en) VALUES ('a','https://a.example/','アルファ','最初の紹介文','first')");
  db2.exec("INSERT INTO sites (id,url,name,description_ja,search_text) VALUES ('b','https://b.example/','ブラボー','別の紹介文','明示した値 explicit')");
  const ts = ['最初の紹介', 'first', 'explicit', '新しい紹', 'タグあり', 'tagone', 'tagtwo', '別の紹介', 'alpha'];
  same(db2, ts, '登録');
  db2.exec("UPDATE sites SET description_ja = '新しい紹介文です' WHERE id = 'a'"); same(db2, ts, '紹介文');
  db2.exec("INSERT INTO site_tags VALUES ('a','タグあり','tagone')"); same(db2, ts, 'タグ追加');
  assert.deepEqual(hits(db2, 'tagone'), truth(db2, 'tagone')); assert.equal(hits(db2, 'tagone').length, 1);
  db2.exec("UPDATE site_tags SET label_en = 'tagtwo' WHERE site_id = 'a'"); same(db2, ts, 'タグ変更');
  assert.equal(hits(db2, 'tagone').length, 0, '古いタグ名は外れる'); assert.equal(hits(db2, 'tagtwo').length, 1);
  db2.exec("UPDATE sites SET search_text = 'ａｌｐｈａ 正規化 alpha' WHERE id = 'a'"); same(db2, ts, 'search_text の直接更新');
  db2.exec("DELETE FROM site_tags WHERE site_id = 'a'"); same(db2, ts, 'タグ削除');
  db2.exec("DELETE FROM sites WHERE id = 'a'"); same(db2, ts, 'サイト削除');
  assert.equal(db2.prepare('SELECT COUNT(*) c FROM sites_fts_docsize').get().c, db2.prepare('SELECT COUNT(*) c FROM sites').get().c);
});

test('トリガ同期: 値が変わらない UPDATE（refresh の同値書き込み）は索引に触らない', () => {
  const db2 = mkDb(); build(db2);
  db2.exec("INSERT INTO sites (id,url,name,description_ja,search_text) VALUES ('a','https://a.example/','n','d','abc def ghi')");
  const changes = f => { const b = db2.prepare('SELECT total_changes() c').get().c; f(); return db2.prepare('SELECT total_changes() c').get().c - b; };
  assert.equal(changes(() => db2.exec("UPDATE sites SET search_text = 'abc def ghi' WHERE id = 'a'")), 1, '同値は sites の 1 行だけ');
  assert.ok(changes(() => db2.exec("UPDATE sites SET search_text = 'abc def xyz' WHERE id = 'a'")) > 1, '変更は索引も更新する');
  same(db2, ['abc', 'xyz', 'ghi'], '同値の後');
});

// ---- 構築スクリプト ----
test('構築前（ready = 0）はトリガが索引に触らない: 既存行の削除・更新で壊れない。構築スクリプトは何度流しても同じ索引（冪等）・ready は最後に 1', () => {
  const pre = mkDb(); // migration の seed 行が入った構築前の DB で、削除・更新・タグ操作ができる
  pre.exec("UPDATE sites SET description_ja = description_ja || ' 追記'; DELETE FROM site_tags WHERE rowid IN (SELECT rowid FROM site_tags LIMIT 3); DELETE FROM site_categories WHERE site_id IN (SELECT id FROM sites LIMIT 5); DELETE FROM site_tags WHERE site_id IN (SELECT id FROM sites LIMIT 5); DELETE FROM sites WHERE id IN (SELECT id FROM sites LIMIT 5)");
  assert.equal(pre.prepare('SELECT COUNT(*) c FROM sites_fts_docsize').get().c, 0, 'ready = 0 の間は索引は空のまま');
  build(pre); same(pre, ['http', 'example'], '構築後'); assert.equal(pre.prepare(SQL.counts).get().sites, pre.prepare(SQL.counts).get().indexed);
  const db2 = mkDb(); db2.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');
  db2.exec('BEGIN');
  const ins = db2.prepare('INSERT INTO sites (id,url,name,description_ja,description_en) VALUES (?,?,?,?,?)');
  for (let i = 0; i < 53; i++) ins.run('s' + i, `https://s${i}.example/`, 'n' + i, `紹介${i} りんご${i % 3}`, `site ${i} apple`);
  db2.exec('COMMIT');
  db2.exec("DELETE FROM sites WHERE id IN ('s3','s10')"); // rowid の欠番
  const ready = () => db2.prepare("SELECT ready FROM fts_state WHERE name = 'sites_fts'").get().ready;
  assert.equal(ready(), 0);
  const dump = () => db2.prepare("SELECT rowid FROM sites_fts WHERE sites_fts MATCH '\"りんご\"' ORDER BY rowid").all().map(r => r.rowid);
  const run = size => { db2.exec(SQL.begin); assert.equal(ready(), 0); const { lo, hi } = db2.prepare(SQL.range).get(); for (const [a, b] of chunkRanges(lo, hi, size)) db2.exec(SQL.chunk(a, b)); db2.exec(SQL.integrity); db2.exec(SQL.ready); };
  run(10); const a = dump(); same(db2, ['りんご', 'apple', 'site 1'], '1 回目'); assert.equal(ready(), 1);
  db2.exec("INSERT INTO sites (id,url,name,description_ja) VALUES ('late','https://late.example/','n','りんご遅れて登録')"); // ready = 1 の間の登録はトリガが入れる
  assert.equal(dump().length, a.length + 1); run(7); assert.equal(dump().length, a.length + 1, 'トリガで入った行と二重にならない');
  db2.exec("DELETE FROM sites WHERE id = 'late'"); run(7); same(db2, ['りんご', 'apple', 'site 1'], '2 回目'); assert.deepEqual(dump(), a);
  run(100000); assert.deepEqual(dump(), a);
  const c = db2.prepare(SQL.counts).get(); assert.equal(c.sites, c.indexed); assert.equal(c.sites, 51);
  // 二重（構築中の登録を想定: 範囲の INSERT を二度流す）は整合性検査が検出する
  db2.exec(SQL.chunk(1, 5));
  assert.throws(() => db2.exec(SQL.integrity), /malformed|corrupt|SQL logic/i);
});

test('構築スクリプト: 空の表・chunkRanges・引数検証', () => {
  const db2 = mkDb(); db2.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;'); db2.exec(SQL.begin); const r = db2.prepare(SQL.range).get(); assert.deepEqual(chunkRanges(r.lo, r.hi, 10), []); db2.exec(SQL.integrity); db2.exec(SQL.ready);
  assert.deepEqual(chunkRanges(1, 25, 10), [[1, 10], [11, 20], [21, 25]]);
  assert.deepEqual(chunkRanges(5, 5, 10000), [[5, 5]]);
  assert.deepEqual(parseArgs(['--remote', '--db', 'mise-preview', '--chunk', '5000']), { mode: 'remote', db: 'mise-preview', persistTo: null, chunk: 5000, check: false });
  assert.equal(parseArgs(['--local', '--check']).check, true);
  assert.throws(() => parseArgs(['--local', '--db', 'mise-preview']), /--local/);
  assert.throws(() => parseArgs(['--remote', '--db', 'other']), /--db/);
  assert.throws(() => parseArgs(['--remote', '--chunk', '0']), /--chunk/);
  assert.throws(() => parseArgs([]), /--local か --remote/);
});

test('refresh-search-text: 今の search_text と同じ行は更新しない（差分の行だけ）', () => {
  const mk = (id, st, tags = []) => ({ id, name: 'N' + id, description_ja: 'ＡＢＣ', description_en: null, url: 'https://x/' + id, tags, search_text: st });
  const ok = mk('a', null); ok.search_text = searchText(ok);
  const withTag = mk('b', null, [{ label_ja: 'タグ', label_en: 'Tag' }]); withTag.search_text = searchText(withTag);
  const stale = mk('c', 'n c abc https://x/c'); // 暫定値（NFKC 前）
  const nul = mk('d', null);
  assert.deepEqual(changedRows([ok, withTag, stale, nul]).map(r => r.id), ['c', 'd']);
});

// ---- 続きのページ・境界・畳み込み・構築の順序（レビュー指摘）----
const mkSmall = (n, desc = i => `りんご${i} 紹介サイト`) => {
  const db2 = mkDb(); db2.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;'); build(db2);
  db2.exec('BEGIN'); const ins = db2.prepare('INSERT INTO sites (id,url,name,description_ja,registered_at) VALUES (?,?,?,?,?)');
  for (let i = 0; i < n; i++) ins.run('c' + String(i).padStart(4, '0'), `https://c${i}.example/`, 'n' + i, desc(i), `2026-01-${String(1 + (i % 28)).padStart(2, '0')}T00:00:00Z`);
  db2.exec('COMMIT'); return db2;
};
const idsOf = async (d2, o) => { const ids = []; let cur = null, first; for (let g = 0; g < 50; g++) { const r = await querySites(d2, { ...o, cursor: cur ? decodeCursor(cur) : null }); if (g === 0) first = r; ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (!cur) break; } return { ids, first }; };
const instrIds = (db2, q) => db2.prepare(`SELECT s.id FROM sites s WHERE instr(s.search_text, ?) > 0 ${siteOrder()}`).all(q).map(r => r.id);

test('続きのページ: 先頭ページの後に索引の作り直し（ready = 0）が始まっても、instr で同じ結果・0 件にならない', async () => {
  const db2 = mkSmall(120), d2 = wrap(db2), q = cleanQuery('りんご'), want = instrIds(db2, q);
  const p1 = await querySites(d2, { q }); assert.equal(p1.items.length, 40);
  db2.exec(SQL.begin); // ready = 0・空の索引
  const rest = []; let cur = p1.next_cursor;
  while (cur) { const r = await querySites(d2, { q, cursor: decodeCursor(cur) }); rest.push(...r.items.map(i => i.id)); cur = r.next_cursor; }
  assert.deepEqual([...p1.items.map(i => i.id), ...rest], want);
  // 作り直しが終わったら（ready = 1）先頭ページから索引の経路に戻る
  for (const [a, b] of chunkRanges(1, 1000, 500)) db2.exec(SQL.chunk(a, b)); db2.exec(SQL.ready);
  assert.equal((await planQuery(d2, { q })).qMode, 'fts');
  assert.deepEqual((await idsOf(d2, { q })).ids, want);
});

test('続きのページ: 先頭ページの後に表を DROP（ロールバック）しても 500 にならず instr で同じ結果', async () => {
  const db2 = mkSmall(120), d2 = wrap(db2), q = cleanQuery('りんご'), want = instrIds(db2, q);
  const p1 = await querySites(d2, { q }); assert.equal((await planQuery(d2, { q })).qMode, 'fts');
  db2.exec('DROP TRIGGER sites_fts_ai; DROP TRIGGER sites_fts_ad; DROP TRIGGER sites_fts_au; DROP TABLE sites_fts; DROP TABLE fts_state;');
  const rest = []; let cur = p1.next_cursor;
  while (cur) { const r = await querySites(d2, { q, cursor: decodeCursor(cur) }); rest.push(...r.items.map(i => i.id)); cur = r.next_cursor; }
  assert.deepEqual([...p1.items.map(i => i.id), ...rest], want);
});

// 文の実行の前後に処理を差し込めるラッパー（窓の再現用）。after(sql) が true を返した文の実行の直後に hook() を 1 回だけ流す。before は文の実行前に fail(sql) が Error を返したら投げる。
const hooked = (db2, { after = () => false, hook = () => {}, fail = () => null } = {}) => {
  let fired = false;
  return { prepare: sql => { const mk = args => { const run = async fn => { const f = fail(sql); if (f) throw f; const r = fn(); if (!fired && after(sql)) { fired = true; hook(); } return r; }; return { bind: (...a) => mk(a), all: () => run(() => ({ results: db2.prepare(sql).all(...args) })), first: () => run(() => db2.prepare(sql).get(...args)) }; }; return mk([]); } };
};
const READY_SQL = s => s.includes('SELECT ready FROM fts_state') && !s.includes('COUNT(*)');
const PAGES = async (d2, o) => { const items = []; let cur = null, first, n = 0; for (; n < 50; n++) { const r = await querySites(d2, { ...o, limit: 40, cursor: cur ? decodeCursor(cur) : null }); if (n === 0) first = r; items.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (!cur) break; } return { ids: items, first, pages: n + 1 }; };

test('窓: 計画の判断（probe）の後・一覧の実行の前に作り直し（ready = 0）が始まっても、先頭ページは instr と同じ（total = 0・途中打ち切りにならない）', async () => {
  const db2 = mkSmall(800), q = cleanQuery('りんご'), want = instrIds(db2, q);
  const d2 = hooked(db2, { after: s => s.includes('AS ready'), hook: () => db2.exec(SQL.begin) });
  const r = await PAGES(d2, { q });
  assert.deepEqual(r.ids, want); assert.equal(r.first.total, want.length); assert.equal(want.length, 800);
});

test('窓: 続きのページで、ready の事前確認の後・一覧の実行の前に作り直しが始まっても、instr と同じ（next_cursor が途中で null にならない）', async () => {
  const db2 = mkSmall(800), q = cleanQuery('りんご'), want = instrIds(db2, q);
  let armed = false;
  const d2 = hooked(db2, { after: s => armed && READY_SQL(s), hook: () => db2.exec(SQL.begin) });
  const p1 = await querySites(d2, { q, limit: 40 }); assert.ok(p1.next_cursor);
  armed = true;
  const ids = p1.items.map(i => i.id); let cur = p1.next_cursor;
  while (cur) { const r = await querySites(d2, { q, limit: 40, cursor: decodeCursor(cur) }); ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; }
  assert.deepEqual(ids, want);
  assert.equal(db2.prepare("SELECT ready FROM fts_state WHERE name = 'sites_fts'").get().ready, 0, '窓を再現できている（作り直しが始まった）');
});

test('実行時の失敗の安全策: 先頭ページで索引の計画を覚えた後、ready = 1 のまま sites_fts だけ消えても、続きのページは instr で同じ（実行時 catch）', async () => {
  const db2 = mkSmall(120), d2 = wrap(db2), q = cleanQuery('りんご'), want = instrIds(db2, q);
  const p1 = await querySites(d2, { q, limit: 40 }); assert.equal((await planQuery(d2, { q })).qMode, 'fts');
  db2.exec('DROP TRIGGER sites_fts_ai; DROP TRIGGER sites_fts_ad; DROP TRIGGER sites_fts_au; DROP TABLE sites_fts;'); // fts_state（ready = 1）は残す
  assert.equal(db2.prepare("SELECT ready FROM fts_state").get().ready, 1);
  const ids = p1.items.map(i => i.id); let cur = p1.next_cursor;
  while (cur) { const r = await querySites(d2, { q, limit: 40, cursor: decodeCursor(cur) }); ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; }
  assert.deepEqual(ids, want);
});

test('実行時の catch は索引の失敗だけ: 表なし系は握って instr、それ以外（too many terms・一時エラー）は投げる。両方失敗したら先のエラーも出す', async () => {
  const db2 = mkSmall(60), q = cleanQuery('りんご'), want = instrIds(db2, q);
  const isList = s => s.startsWith('SELECT s.id') && s.includes('sites_fts MATCH');
  const quiet = async f => { const e = console.error; console.error = () => {}; try { return await f(); } finally { console.error = e; } };
  // 握る: sites_fts 系
  for (const msg of ['no such table: main.sites_fts', 'vtable constructor failed: sites_fts', 'database disk image is malformed']) {
    const d2 = hooked(db2, { fail: s => isList(s) ? new Error(msg) : null });
    assert.deepEqual((await quiet(() => idsOf(d2, { q }))).ids, want, msg);
  }
  // 握らない
  for (const msg of ['too many terms in compound SELECT', 'D1_ERROR: Network connection lost', 'near "FROM": syntax error']) {
    const d2 = hooked(db2, { fail: s => isList(s) ? new Error(msg) : null });
    await assert.rejects(() => quiet(() => querySites(d2, { q })), new RegExp(msg.slice(0, 12)), msg);
  }
  // 索引も instr も失敗: 両方のメッセージ
  const both = hooked(db2, { fail: s => s.startsWith('SELECT s.id') ? new Error(s.includes('sites_fts MATCH') ? 'no such table: sites_fts' : 'boom instr') : null });
  await assert.rejects(() => quiet(() => querySites(both, { q })), /boom instr.*no such table: sites_fts/);
});

test('構築: ready = 0 に戻す操作も失敗したら、元の検証エラーも合わせて出す', () => {
  const wr = sql => { if (sql === SQL.range) return [{ lo: 1, hi: 5 }]; if (sql === SQL.counts) return [{ sites: 5, indexed: 4 }]; if (sql === SQL.unready) throw new Error('unready failed'); return []; };
  assert.throws(() => runBuild(wr, { chunk: 10 }), err => /件数が合わない.*unready failed.*1 のまま/.test(err.message) && /件数が合わない/.test(err.cause.message));
});

test('畳み込み: 索引が instr より多く拾う行（非 ASCII の大文字・語末シグマ）は instr と同じ結果（索引の行にも instr を重ねる）', async () => {
  const db2 = mkSmall(0), d2 = wrap(db2);
  // 0020 の暫定値（ASCII だけ小文字化）で非 ASCII の大文字が残った search_text を想定
  db2.exec("INSERT INTO sites (id,url,name,description_ja,search_text) VALUES ('g1','https://g1.example/','n','d','ΟΔΟΣ 例'), ('g2','https://g2.example/','n','d','ςας 例'), ('g3','https://g3.example/','n','d','οδος σας')");
  for (const t of ['οδο', 'σας', 'ςας', 'οδοσ']) {
    const q = cleanQuery(t), ftsHits = db2.prepare('SELECT COUNT(*) c FROM sites_fts WHERE sites_fts MATCH ?').get('"' + q + '"').c;
    assert.deepEqual((await idsOf(d2, { q })).ids, instrIds(db2, q), t);
    if (t === 'οδο') assert.ok(ftsHits > instrIds(db2, q).length, '索引は instr より多く拾う（このテストが意味を持つ前提）');
  }
});

test('境界: 該当がちょうど TOTAL_CAP 件なら索引・総件数は正確、TOTAL_CAP + 1 件なら instr・total_capped', async () => {
  for (const [n, mode, capped] of [[TOTAL_CAP, 'fts', false], [TOTAL_CAP + 1, 'instr', true]]) {
    const db2 = mkSmall(n, i => `ぼうだい${i}`), d2 = wrap(db2), q = cleanQuery('ぼうだい');
    const plan = await planQuery(d2, { q });
    assert.equal(plan.qMode, mode, `${n} 件`);
    const r = await querySites(d2, { q });
    assert.equal(r.total, TOTAL_CAP); assert.equal(r.total_capped, capped);
    setReady(db2, 0); const b = await querySites(d2, { q }); // instr の経路
    assert.deepEqual([r.total, r.total_capped, r.items.map(i => i.id)], [b.total, b.total_capped, b.items.map(i => i.id)]);
  }
});

test('計画のキャッシュは q ごと: 続きのページでも、索引の語は索引・上限超えの語は instr の計画を使う', async () => {
  const db2 = mkSmall(1100, i => `共通語 ${i % 2 ? 'りんご' : 'みかん'}${i}`), q1 = cleanQuery('みかん'), q2 = cleanQuery('共通語');
  const seen = []; const d2 = { prepare: sql => { seen.push(sql); return wrap(db2).prepare(sql); } };
  const a = await querySites(d2, { q: q1, limit: 10 }), b = await querySites(d2, { q: q2, limit: 10 });
  seen.length = 0;
  await querySites(d2, { q: q1, limit: 10, cursor: decodeCursor(a.next_cursor) });
  assert.ok(seen.some(s => s.includes('sites_fts MATCH')), 'q1（550 件）は索引');
  seen.length = 0;
  await querySites(d2, { q: q2, limit: 10, cursor: decodeCursor(b.next_cursor) });
  assert.ok(!seen.some(s => s.includes('sites_fts MATCH')), 'q2（1,100 件）は instr');
});

test('構築の順序: ready = 1 にしてから検証し、失敗したら ready = 0 に戻して止まる（モック）', () => {
  const calls = [];
  const mk = (counts, integrityFails) => sql => { calls.push(sql); if (sql === SQL.range) return [{ lo: 1, hi: 25 }]; if (sql === SQL.counts) return [counts]; if (sql === SQL.integrity && integrityFails) throw new Error('malformed'); return []; };
  const ok = runBuild(mk({ sites: 25, indexed: 25 }, false), { chunk: 10 });
  assert.equal(ok.indexed, 25);
  const i = f => calls.indexOf(f);
  assert.ok(i(SQL.ready) > i(SQL.chunk(21, 25)) && i(SQL.ready) < i(SQL.counts) && i(SQL.counts) < i(SQL.integrity), 'ready は検証の前');
  assert.ok(!calls.includes(SQL.unready));
  for (const [counts, fails, msg] of [[{ sites: 25, indexed: 24 }, false, /件数/], [{ sites: 25, indexed: 25 }, true, /malformed/]]) {
    calls.length = 0;
    assert.throws(() => runBuild(mk(counts, fails), { chunk: 10 }), msg);
    assert.equal(calls.at(-1), SQL.unready, '失敗したら ready = 0 に戻す');
  }
});

test('probe（planQuery）の catch は索引の失敗だけ: 索引系は握って instr、それ以外は再送出', async () => {
  const db2 = mkSmall(60), q = cleanQuery('りんご');
  const isProbe = s => s.includes('AS ready') && s.includes('sites_fts MATCH');
  const ok = await planQuery(hooked(db2, { fail: s => isProbe(s) ? new Error('no such table: sites_fts') : null }), { q });
  assert.equal(ok.qMode, 'instr');
  for (const msg of ['too many terms in compound SELECT', 'D1_ERROR: Network connection lost']) {
    await assert.rejects(() => planQuery(hooked(db2, { fail: s => isProbe(s) ? new Error(msg) : null }), { q }), new RegExp(msg.slice(0, 12)));
  }
});
