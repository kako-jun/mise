import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { querySites, planQuery, randomSites, decodeCursor, siteOrder, TOTAL_CAP, TAG_DRIVE_MAX, PAGE_SIZE, cleanQuery } from '../src/sites.js';

// 検索計画と総件数の上限（#126 第2段）。全 migration を適用したメモリ SQLite（索引・集計表・トリガ込み）で、
//  (1) どの計画（カテゴリの IN/EXISTS、タグの索引/EXISTS）でも、全ページ連結 = 独立に書いた SQL の並びと同一
//  (2) total の上限（TOTAL_CAP）と、末尾まで届いたときの正確な件数
//  (3) ピックアップ（rowid の乱数位置）の性質
// を検査する。
const MIG = new URL('../migrations/', import.meta.url);
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
const d = {
  prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); },
};
const leafIds = db.prepare(`SELECT id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) ORDER BY id`).all().map(r => r.id);
const BIG = leafIds[0], SMALL = leafIds[1], NONLEAF = db.prepare('SELECT parent_id p FROM categories WHERE id = ?').get(BIG).p;
db.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');
const N = TOTAL_CAP + 300;
db.exec('BEGIN');
const insS = db.prepare('INSERT INTO sites (id,url,name,description_ja,registered_at,source_updated_at,country_code,bulk_import) VALUES (?,?,?,?,?,?,?,?)');
const insC = db.prepare('INSERT INTO site_categories (site_id,category_id) VALUES (?,?)'), insT = db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)');
for (let i = 0; i < N; i++) {
  const id = 't' + String(i).padStart(4, '0');
  // 日時は重複・NULL・不正値を混ぜる（キーセットの NULL・同値の扱い）。bulk_import は一部だけ 1。
  const day = i % 40, reg = i % 29 === 0 ? 'bad' : `2026-0${1 + (day % 9)}-1${day % 9}T00:00:00Z`, upd = i % 3 === 0 ? null : `2026-0${1 + (i % 7)}-2${i % 8}T00:00:00Z`;
  insS.run(id, `https://${id}.example/`, 'n' + id, `d${i} ${i % 2 ? 'apple' : 'pear'} site`, reg, upd, i % 17 === 0 ? null : (i % 3 === 0 ? 'US' : 'JP'), i % 13 === 0 ? 1 : 0);
  if (i % 50 !== 0) insC.run(id, BIG);
  if (i % 260 === 5) insC.run(id, SMALL);
  if (i % 11 !== 0) insT.run(id, 'common', 'Common');
  if (i % 200 === 7) insT.run(id, 'rare', 'Rare');
  if (i % 200 === 7 || i % 11 === 3) insT.run(id, 'extra', 'Extra');
}
insS.run('fr-only', 'https://fr.example/', 'fr', 'd', '2026-01-01T00:00:00Z', null, 'FR', 0);
db.exec('COMMIT');

// 独立した参照実装: 絞り込みを素朴な SQL で書き、並びは siteOrder() で全件を取る。
const ref = ({ cc, category, q, tags = [] }) => {
  const w = [], a = [];
  if (cc) { w.push('s.country_code = ?'); a.push(cc); }
  if (category) { w.push('(SELECT COUNT(*) FROM site_categories x WHERE x.site_id = s.id AND x.category_id = ?) > 0'); a.push(category); }
  if (q) { w.push('instr(s.search_text, ?) > 0'); a.push(q); }
  for (const t of tags) { w.push('(SELECT COUNT(*) FROM site_tags x WHERE x.site_id = s.id AND (x.label_ja = ? OR x.label_en = ?)) > 0'); a.push(t, t); }
  return db.prepare(`SELECT s.id FROM sites s ${w.length ? 'WHERE ' + w.join(' AND ') : ''} ${siteOrder()}`).all(...a).map(r => r.id);
};
const pagesOf = async (o, limit = PAGE_SIZE) => {
  const ids = []; let cur = null, first;
  for (let g = 0; g < 200; g++) { const r = await querySites(d, { ...o, limit, cursor: cur ? decodeCursor(cur) : null }); if (g === 0) first = r; ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (!cur) break; }
  return { ids, first };
};

const cases = [
  ['q 多ヒット（上限超え）', { q: cleanQuery('site') }],
  ['q 中ヒット（上限未満）', { q: cleanQuery('apple') }],
  ['q 1 件', { q: cleanQuery('d1299') }],
  ['q 0 件', { q: cleanQuery('zzz') }],
  ['カテゴリ 大（EXISTS 形）', { category: BIG }],
  ['カテゴリ 小（IN 形）', { category: SMALL }],
  ['カテゴリ 大 + 範囲', { category: BIG, cc: 'US' }],
  ['カテゴリ 小 + 範囲', { category: SMALL, cc: 'JP' }],
  ['カテゴリ 大 + q', { category: BIG, q: cleanQuery('apple') }],
  ['葉でないカテゴリ（直下に所属なし）', { category: NONLEAF }],
  ['タグ 稀（索引から）', { tags: ['rare'] }],
  ['タグ 稀（英語名）', { tags: ['Rare'] }],
  ['タグ 多（EXISTS 形・上限超え）', { tags: ['common'] }],
  ['タグ 多 + 範囲', { tags: ['common'], cc: 'JP' }],
  ['タグ 稀 AND 多', { tags: ['common', 'rare'] }],
  ['タグ 中（extra）+ q', { tags: ['extra'], q: cleanQuery('apple') }],
  ['タグ 稀 + カテゴリ大', { tags: ['rare'], category: BIG }],
  ['範囲だけ', { cc: 'JP' }],
  ['絞り込みなし', {}],
];

for (const [label, o] of cases) {
  test(`全ページ連結が参照実装の並びと同一・total の意味: ${label}`, async () => {
    const want = ref(o), { ids, first } = await pagesOf(o);
    assert.deepEqual(ids, want);
    if (want.length <= PAGE_SIZE) { assert.equal(first.total, want.length); assert.equal(first.total_capped, false); assert.equal(first.next_cursor, null); return; }
    assert.ok(first.total_capped === (first.total === TOTAL_CAP && want.length > TOTAL_CAP) || first.total_capped === false);
    if (first.total_capped) { assert.equal(first.total, TOTAL_CAP); assert.ok(want.length > TOTAL_CAP, '上限を超える絞り込みだけが capped'); }
    else assert.equal(first.total, want.length, '上限以下、または集計表から正確に出せる経路は正確な件数');
    // limit を変えても、100 件ずつでも同じ並び（カーソルの項の切れ目をまたぐ）
    assert.deepEqual((await pagesOf(o, 7)).ids, want);
  });
}

test('total: 上限を超える q・単独のタグは total_capped = true で TOTAL_CAP、末尾まで読むと件数は正確', async () => {
  for (const o of [{ q: cleanQuery('site') }, { tags: ['common'] }]) {
    const r = await querySites(d, o);
    assert.equal(r.total, TOTAL_CAP); assert.equal(r.total_capped, true);
    assert.equal(ref(o).length > TOTAL_CAP, true);
  }
  // 葉カテゴリ・範囲だけは集計表から正確（上限を超えても capped にしない）
  const big = await querySites(d, { category: BIG });
  assert.equal(big.total, ref({ category: BIG }).length); assert.ok(big.total > TOTAL_CAP); assert.equal(big.total_capped, false);
  const all = await querySites(d, {});
  assert.equal(all.total, N + 1); assert.equal(all.total_capped, false);
  // q の中ヒット（上限未満）は正確に数える
  const apple = await querySites(d, { q: cleanQuery('apple') });
  assert.equal(apple.total, ref({ q: cleanQuery('apple') }).length); assert.equal(apple.total_capped, false);
});

test('total: 末尾まで届いた先頭ページは追加の読み取りなしで正確（0 件・1 ページ以内・?page=N の末尾）', async () => {
  let queries = 0;
  const counting = { prepare: sql => { queries++; return d.prepare(sql); } };
  const n0 = (queries = 0, await querySites(counting, { q: cleanQuery('zzz') }), queries);
  assert.equal((await querySites(d, { q: cleanQuery('zzz') })).total, 0);
  // q の 0 件: 3 文字以上は索引の該当数を見る 1 本 + 一覧 1 本（#126 第3段）、1〜2 文字は一覧 1 本だけ。どちらも件数の数え直しはしない
  // どちらにも関連度の段の鍵を数える 1 本（#301。鍵の索引で該当数 + 1 行まで）が加わる
  assert.equal(n0, 3);
  const n1 = (queries = 0, await querySites(counting, { q: cleanQuery('zz') }), queries);
  assert.equal(n1, 2);
  const want = ref({ q: cleanQuery('apple') }), last = Math.floor((want.length - 1) / PAGE_SIZE) * PAGE_SIZE;
  const r = await querySites(d, { q: cleanQuery('apple'), offset: last });
  assert.equal(r.total, want.length); assert.equal(r.next_cursor, null);
  const past = await querySites(d, { q: cleanQuery('apple'), offset: 100000 }); // 範囲外の ?page=N（行なし）でも件数は出る（0 を返さない）
  assert.equal(past.items.length, 0); assert.equal(past.total, want.length);
});

test('検索計画: カテゴリは小さければ IN、大きければ EXISTS。タグは稀なものだけ索引から', async () => {
  assert.equal((await planQuery(d, { category: BIG })).categoryMode, 'exists');
  assert.equal((await planQuery(d, { category: SMALL })).categoryMode, 'in');
  assert.equal((await planQuery(d, { category: BIG })).categoryLeafCount, ref({ category: BIG }).length);
  assert.equal((await planQuery(d, { category: NONLEAF })).categoryLeafCount, null, '葉でないカテゴリの件数は集計表（子孫を含む）では出せない');
  assert.equal((await planQuery(d, { tags: ['rare'] })).driveTag, 'rare');
  assert.equal((await planQuery(d, { tags: ['common'] })).driveTag, null);
  assert.equal((await planQuery(d, { tags: ['common'] })).tagsOverCap, true);
  assert.equal((await planQuery(d, { tags: ['common', 'rare'] })).driveTag, 'rare');
  assert.ok(ref({ tags: ['common'] }).length > TAG_DRIVE_MAX && ref({ tags: ['rare'] }).length <= TAG_DRIVE_MAX);
});

test('カーソルは項ごとに索引を引いて連結: 深い位置・NULL・同値キーでも欠落と重複がない', async () => {
  const want = ref({});
  for (const limit of [13, 100]) assert.deepEqual((await pagesOf({}, limit)).ids, want);
  // 任意の位置のカーソル（全位置）から始めて、その次から最後まで同じ並び
  for (const at of [0, 1, 39, 40, 41, 600, want.length - 2]) {
    const row = db.prepare(`SELECT ${require_keys()} FROM sites s WHERE s.id = ?`).get(want[at]);
    const cur = Buffer.from(JSON.stringify([row.k0, row.k1, row.k2])).toString('base64url');
    const ids = []; let c = cur;
    for (let g = 0; g < 100 && c; g++) { const r = await querySites(d, { cursor: decodeCursor(c), limit: 100 }); ids.push(...r.items.map(i => i.id)); c = r.next_cursor; }
    assert.deepEqual(ids, want.slice(at + 1), `位置 ${at} の次から`);
  }
});
import { SORT_KEYS } from '../src/sites.js';
function require_keys() { return SORT_KEYS.map((k, i) => `${k.expr} AS k${i}`).join(', '); }

test('ピックアップ: 重複なく n 件・一括投入を除く・範囲を守る・該当が少なければ少なく返す', async () => {
  for (let i = 0; i < 30; i++) {
    const r = await randomSites(d, null, 3);
    assert.equal(r.length, 3); assert.equal(new Set(r.map(x => x.id)).size, 3);
    assert.ok(r.every(x => x.bulk_import === 0));
    const us = await randomSites(d, 'US', 3);
    assert.equal(us.length, 3); assert.ok(us.every(x => x.country_code === 'US' && x.bulk_import === 0));
  }
  assert.deepEqual((await randomSites(d, 'ZZ', 3)).map(x => x.id), []);
  assert.deepEqual((await randomSites(d, 'FR', 3)).map(x => x.id), ['fr-only']);
});

test('migration 0023〜0025: 索引が作られ、検索計画が使う形のクエリが索引で引かれる', () => {
  const idx = db.prepare("SELECT name FROM sqlite_master WHERE type='index'").all().map(r => r.name);
  for (const n of ['idx_site_tags_label_ja', 'idx_site_tags_label_en', 'idx_sites_activity']) assert.ok(idx.includes(n), n);
  const plan = sql => db.prepare('EXPLAIN QUERY PLAN ' + sql).all('x').map(r => r.detail).join(' | ');
  assert.match(plan('SELECT site_id FROM site_tags WHERE label_ja = ?'), /idx_site_tags_label_ja/);
  assert.match(plan('SELECT site_id FROM site_tags WHERE label_en = ?'), /idx_site_tags_label_en/);
  assert.match(db.prepare("EXPLAIN QUERY PLAN SELECT date(registered_at) FROM sites WHERE bulk_import = 0 AND datetime(registered_at) IS NOT NULL AND date(registered_at) >= date('now','-6 days') GROUP BY date(registered_at)").all().map(r => r.detail).join(' | '), /idx_sites_activity/);
});

// ---- 同値の多いデータ（本番の形）での回帰（#126 レビュー）----
// 本番は台帳の一括投入（bulk_import=1）が rowid の連続した塊で、日時がほぼ全部同値。散らばったデータでは出なかった 2 つの問題を固定する:
//  (1) キーセットの項が一時 B-tree になり、同日時の塊を毎回全部読む / IN で引く計画に範囲条件を足すと並びの索引の走査になる
//  (2) ピックアップが塊の直後の 1 件に偏る
// メモリ SQLite は読んだ行数を数えられないので、読み取り量は実行計画で検査する（一時 B-tree が無い・IN で引く計画は並びの索引を走査しない）。
// 行数そのものは scripts/bench/bench-traverse.mjs（ローカル D1 の rows_read）で測る。
const sk = new DatabaseSync(':memory:');
sk.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) sk.exec(readFileSync(new URL(f, MIG), 'utf8'));
sk.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');
const SK_N = 2200, SK_BULK_FROM = 37, SK_BULK_TO = 1887; // 本番: 2,158 件中 1,851 件が rowid 38〜1888 の一括投入
const skCat = sk.prepare(`SELECT id FROM categories c WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id) ORDER BY id`).all().map(r => r.id);
sk.exec('BEGIN');
{
  const iS = sk.prepare('INSERT INTO sites (id,url,name,description_ja,registered_at,source_updated_at,country_code,bulk_import) VALUES (?,?,?,?,?,?,?,?)');
  const iC = sk.prepare('INSERT INTO site_categories (site_id,category_id) VALUES (?,?)'), iT = sk.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)');
  for (let i = 0; i < SK_N; i++) {
    const bulk = i >= SK_BULK_FROM && i < SK_BULK_TO, id = (bulk ? 'fr-' : 'pb-') + String(i).padStart(4, '0');
    const cc = !bulk && i % 40 === 3 ? 'US' : 'JP';
    iS.run(id, `https://${id}.example/`, 'n' + id, 'd' + i, bulk ? '2026-11-13 00:00:00' : `2026-0${1 + (i % 9)}-1${i % 9} 00:00:00`, bulk || i % 3 ? null : '2026-05-05', cc, bulk ? 1 : 0);
    if (i % 10 !== 0) iC.run(id, skCat[0]);
    if (i % 90 === 1) iC.run(id, skCat[1]);
    if (i % 5 !== 0) iT.run(id, 'common', 'Common');
    if (i % 150 === 7) iT.run(id, 'rare', 'Rare');
  }
}
sk.exec('COMMIT');
const skLog = [];
const skd = { prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => { skLog.push({ sql, args }); return { results: sk.prepare(sql).all(...args) }; }, first: async () => { skLog.push({ sql, args }); return sk.prepare(sql).get(...args); } }); return mk([]); } };
const skRef = ({ cc, category, tags = [] }) => {
  const w = [], a = [];
  if (cc) { w.push('s.country_code = ?'); a.push(cc); }
  if (category) { w.push('s.id IN (SELECT site_id FROM site_categories WHERE category_id = ?)'); a.push(category); }
  for (const t of tags) { w.push('s.id IN (SELECT site_id FROM site_tags WHERE label_ja = ? OR label_en = ?)'); a.push(t, t); }
  return sk.prepare(`SELECT s.id FROM sites s ${w.length ? 'WHERE ' + w.join(' AND ') : ''} ${siteOrder()}`).all(...a).map(r => r.id);
};
const skCases = [
  ['既定', {}, false], ['範囲 JP', { cc: 'JP' }, false], ['大カテゴリ（EXISTS 形）', { category: skCat[0] }, false], ['大カテゴリ + JP', { category: skCat[0], cc: 'JP' }, false],
  ['小カテゴリ（IN 形）', { category: skCat[1] }, true], ['よく使うタグ', { tags: ['common'] }, false], ['稀なタグ（索引から）', { tags: ['rare'] }, true], ['稀なタグ + JP', { tags: ['rare'], cc: 'JP' }, true],
];
for (const [label, o, inDriven] of skCases) {
  test(`同値の多いデータ: ${label} は全ページ連結が参照実装と同一で、続きのページは一時 B-tree を作らない / IN の計画は並びの索引を走査しない`, async () => {
    const want = skRef(o), ids = []; let cur = null, pages = 0;
    for (let g = 0; g < 200; g++) {
      skLog.length = 0;
      const r = await querySites(skd, { ...o, limit: inDriven ? 3 : PAGE_SIZE, cursor: cur ? decodeCursor(cur) : null });
      ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; pages++;
      if (g > 0) for (const { sql, args } of skLog.filter(x => /LIMIT \? OFFSET \?/.test(x.sql))) {
        const eqp = sk.prepare('EXPLAIN QUERY PLAN ' + sql).all(...args).map(x => x.detail).join(' | ');
        if (inDriven) assert.doesNotMatch(eqp, /idx_sites_sort/, `IN で引く計画が並びの索引の走査になっている: ${eqp}`);
        else assert.doesNotMatch(eqp, /TEMP B-TREE/, `項の ORDER BY が索引と合わず一時 B-tree になっている（同日時の塊を毎回読む）: ${eqp}`);
      }
      if (!cur) break;
    }
    assert.deepEqual(ids, want);
    assert.ok(pages > 3 || inDriven && pages > 2, '続きのページを複数たどっている');
  });
}

test('同値の多いデータ: 続きのページはタグの該当数のプローブをやり直さない（計画は先頭ページで決める）', async () => {
  const o = { tags: ['common', 'rare'], limit: 10 };
  skLog.length = 0;
  const first = await querySites(skd, o);
  const probes = skLog.filter(x => /UNION SELECT site_id FROM site_tags WHERE label_en = \?1 LIMIT/.test(x.sql)).length;
  assert.ok(probes >= 1 && first.next_cursor);
  skLog.length = 0;
  await querySites(skd, { ...o, cursor: decodeCursor(first.next_cursor) });
  assert.equal(skLog.filter(x => /SELECT COUNT\(\*\) AS n FROM \(SELECT site_id FROM site_tags/.test(x.sql)).length, 0);
});

test('同値の多いデータ: ピックアップは一括投入の塊の直後に偏らず、非一括の全サイトにほぼ一様に当たる（600 回）', async () => {
  const nonBulk = sk.prepare('SELECT COUNT(*) n FROM sites WHERE bulk_import = 0').get().n;
  for (const cc of [null, 'JP']) {
    const cnt = new Map(); let short = 0;
    for (let i = 0; i < 600; i++) { const r = await randomSites(skd, cc, 3); if (r.length < 3) short++; assert.ok(r.every(x => x.bulk_import === 0)); for (const x of r) cnt.set(x.id, (cnt.get(x.id) ?? 0) + 1); }
    const max = Math.max(...cnt.values()) / 600;
    assert.equal(short, 0);
    assert.ok(max <= 0.05, `最大の出現率 ${max}（一様なら約 ${(3 / nonBulk).toFixed(3)}。偏った旧方式は 0.85）`);
    assert.ok(cnt.size >= nonBulk * 0.85, `当たったサイト ${cnt.size} / ${nonBulk}`);
  }
  // 乱数 rowid の引きは rowid の点引き（並びの索引で非一括の全件を走査しない。走査すると 1 回で 300 行余り読む）
  skLog.length = 0; await randomSites(skd, 'JP', 3);
  const probe = skLog.find(x => /s\.rowid IN/.test(x.sql));
  assert.ok(probe, '乱数 rowid の問い合わせがある');
  const eqp = sk.prepare('EXPLAIN QUERY PLAN ' + probe.sql).all(...probe.args).map(x => x.detail).join(' | ');
  assert.match(eqp, /rowid=\?/); assert.doesNotMatch(eqp, /idx_sites_/);
  // 極端に少ない範囲（US 7 件程度）は ORDER BY RANDOM() で足りる分を返す
  const us = sk.prepare("SELECT COUNT(*) n FROM sites WHERE country_code = 'US'").get().n;
  const seen = new Set();
  for (let i = 0; i < 100; i++) { const r = await randomSites(skd, 'US', 3); assert.equal(r.length, Math.min(3, us)); r.forEach(x => seen.add(x.id)); }
  assert.equal(seen.size, us);
});
