import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import { querySites, searchText } from '../src/sites.js';
import { changedRows } from '../scripts/refresh-search-text.mjs';

// migration 0030（#167）: sites.name_ja / name_en。検索（search_text・FTS）が両列を含み、持たないサイトは従来と同じであること。
const MIG = new URL('../migrations/', import.meta.url);
const FILE = '0030_name_per_language.sql';
const mkDb = ({ without0030 = false } = {}) => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) {
    if (without0030 && f >= FILE) continue;
    db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  }
  db.exec('DELETE FROM site_categories; DELETE FROM site_tags; DELETE FROM sites;');
  return db;
};
const wrap = db => ({ prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); } });
const ins = db => db.prepare('INSERT INTO sites (id,url,name,description_ja,description_en,registered_at,country_code) VALUES (?,?,?,?,?,?,?)');
const seed = db => {
  const i = ins(db);
  for (let n = 0; n < 12; n++) i.run('s' + n, `https://s${n}.example/`, 'Plain ' + n, `紹介${n} りんご`, n % 2 ? `Apple ${n}` : null, `2026-01-${String(10 + n)}T00:00:00Z`, 'JP');
  db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)').run('s3', 'ゲーム', 'Games');
  db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)').run('s3', 'アプリ', null);
};
const st = db => Object.fromEntries(db.prepare('SELECT id, search_text FROM sites ORDER BY id').all().map(r => [r.id, r.search_text]));
const Q = { cc: null, region: null, category: null, q: '', tags: [], limit: 50, cursor: null, offset: 0 };
const ids = async (db, q) => (await querySites(wrap(db), { ...Q, q })).items.map(x => x.id);
const ready = db => db.exec("UPDATE fts_state SET ready = 1 WHERE name = 'sites_fts'");

test('0030: 列が増え、既存行は NULL・API は name_ja / name_en を返し name は従来のまま', async () => {
  const db = mkDb();
  const cols = db.prepare("SELECT name, type FROM pragma_table_info('sites')").all();
  for (const c of ['name_ja', 'name_en']) assert.equal(cols.find(x => x.name === c)?.type, 'TEXT', c);
  seed(db);
  const { items } = await querySites(wrap(db), Q);
  assert.equal(items.length, 12);
  for (const it of items) { assert.equal(it.name_ja, null); assert.equal(it.name_en, null); assert.match(it.name, /^Plain /); }
  db.prepare('UPDATE sites SET name_ja=?, name_en=? WHERE id=?').run('せおはやみ', 'Theo Hayami', 's0');
  const s0 = (await querySites(wrap(db), Q)).items.find(x => x.id === 's0');
  assert.deepEqual([s0.name, s0.name_ja, s0.name_en], ['Plain 0', 'せおはやみ', 'Theo Hayami']);
});

test('0030: 両列が NULL のサイトの search_text は migration 前と文字列まで同じ（タグ込み・追加・更新後も）', () => {
  const a = mkDb({ without0030: true }), b = mkDb();
  for (const db of [a, b]) {
    seed(db);
    db.prepare("UPDATE sites SET description_ja='直した紹介' WHERE id='s5'").run();
    db.prepare("DELETE FROM site_tags WHERE label_ja='アプリ'").run();
    db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)').run('s7', 'ブログ', 'Blog');
    db.prepare("UPDATE site_tags SET label_en='Game' WHERE label_ja='ゲーム'").run();
  }
  assert.deepEqual(st(b), st(a));
  // JS の searchText（refresh 用）とも、ASCII だけの行では暫定値と同じ
  for (const r of b.prepare('SELECT * FROM sites').all()) {
    r.tags = b.prepare('SELECT label_ja, label_en FROM site_tags WHERE site_id=? ORDER BY label_ja').all(r.id);
    assert.equal(searchText(r).toLowerCase(), r.search_text.toLowerCase());
  }
});

test('0030: name_ja / name_en を入れる・直す・消すと search_text が追従し、FTS（ready=1）も追従する', () => {
  const db = mkDb(); seed(db); ready(db);
  db.exec("INSERT INTO sites_fts(sites_fts) VALUES ('rebuild')");
  const fts = q => db.prepare('SELECT s.id FROM sites_fts f JOIN sites s ON s.rowid=f.rowid WHERE sites_fts MATCH ? ORDER BY s.id').all(`"${q}"`).map(r => r.id);
  assert.deepEqual(fts('せおはや'), []);
  db.prepare('UPDATE sites SET name_ja=?, name_en=? WHERE id=?').run('せおはやみ', 'Theo Hayami', 's0');
  assert.match(db.prepare("SELECT search_text FROM sites WHERE id='s0'").get().search_text, /^plain 0 せおはやみ theo hayami 紹介0/);
  assert.deepEqual(fts('せおはや'), ['s0']);
  assert.deepEqual(fts('theo hay'), ['s0']);
  db.prepare("UPDATE sites SET name_ja='別名前' WHERE id='s0'").run();
  assert.deepEqual(fts('せおはや'), []); assert.deepEqual(fts('別名前'), ['s0']);
  db.prepare("UPDATE sites SET name_ja=NULL, name_en=NULL WHERE id='s0'").run();
  assert.deepEqual(fts('別名前'), []); assert.deepEqual(fts('theo hay'), []);
  // INSERT（search_text 未指定）でも索引が壊れない（発火順は FTS の ai → search_text の ai。後者が埋めた search_text を sites_fts_au が索引に反映する。0030 が FTS のトリガを作り直す理由）
  db.prepare('INSERT INTO sites (id,url,name,name_ja,description_ja) VALUES (?,?,?,?,?)').run('ins', 'https://ins.example/', 'Ins', '挿入した名前', 'd');
  assert.deepEqual(fts('挿入した'), ['ins']);
  db.exec("INSERT INTO sites_fts(sites_fts, rank) VALUES ('integrity-check', 1)");
  // タグの追加・削除でも name_ja を落とさない
  db.prepare("UPDATE sites SET name_ja='日本名' WHERE id='s3'").run();
  db.prepare('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)').run('s3', 'ブログ', 'Blog');
  assert.match(db.prepare("SELECT search_text FROM sites WHERE id='s3'").get().search_text, /^plain 3 日本名 /);
  db.prepare("DELETE FROM site_tags WHERE label_ja='ブログ'").run();
  assert.match(db.prepare("SELECT search_text FROM sites WHERE id='s3'").get().search_text, /^plain 3 日本名 /);
  // 索引は search_text と一致（外部コンテンツ表の整合）
  db.exec("INSERT INTO sites_fts(sites_fts, rank) VALUES ('integrity-check', 1)");   // 索引が search_text とずれていれば例外を投げる（これが検査）
});

test('0030: 検索の回帰 — 日本語名・英語名で見つかる。名前だけのサイトの結果と並びは変わらない（索引あり/なし両経路）', async () => {
  const a = mkDb(), b = mkDb();   // a = 名前を持たないまま（基準）、b = s9 にだけ name_ja / name_en を入れる
  for (const db of [a, b]) seed(db);
  b.prepare('UPDATE sites SET name_ja=?, name_en=? WHERE id=?').run('小エビの間違いサラダ', 'Machigai Salad', 's9');
  for (const mode of [0, 1]) {
    if (mode) { ready(a); ready(b); a.exec("INSERT INTO sites_fts(sites_fts) VALUES ('rebuild')"); b.exec("INSERT INTO sites_fts(sites_fts) VALUES ('rebuild')"); }
    for (const q of ['plain', 'りんご', 'apple', 'games', 'ゲーム', 'p', '紹介1', 'example', 'no-such-word']) {
      assert.deepEqual(await ids(b, q), await ids(a, q), `mode ${mode} q=${q}`);   // 新しい名前は含まれないクエリでは完全に同じ
    }
    assert.deepEqual(await ids(b, '小エビの間違い'), ['s9']);
    assert.deepEqual(await ids(b, 'machigai salad'), ['s9']);
    assert.deepEqual(await ids(b, 'エビ'), ['s9']);
    assert.deepEqual(await ids(a, '小エビの間違い'), []);
  }
  assert.deepEqual(await ids(b, ''), await ids(a, ''));
});

test('0030: refresh-search-text は name_ja / name_en を NFKC + 小文字化で含める（暫定値との差分で更新対象になる）', () => {
  const db = mkDb(); seed(db);
  db.prepare('UPDATE sites SET name_ja=?, name_en=? WHERE id=?').run('ＡＢＣ名', 'Ｔheo', 's1');
  const rows = db.prepare('SELECT id, name, name_ja, name_en, description_ja, description_en, url, search_text FROM sites').all();
  for (const r of rows) r.tags = db.prepare('SELECT label_ja, label_en FROM site_tags WHERE site_id=? ORDER BY label_ja').all(r.id);
  const todo = changedRows(rows);
  assert.deepEqual(todo.map(r => r.id), ['s1']);   // 暫定値（ASCII の小文字化）は全角を直さないので s1 だけが埋め直し対象
  assert.match(searchText(todo[0]), /^plain 1 abc名 theo /);
});

test('0030: thumb / favicon 列の更新で sites のトリガは発火しない・UPDATE トリガは UPDATE OF で列を限る', () => {
  const db = mkDb(); seed(db); ready(db); db.exec("INSERT INTO sites_fts(sites_fts) VALUES ('rebuild')");
  db.prepare('UPDATE sites SET name_ja=? WHERE id=?').run('日本名', 's2');
  for (const t of db.prepare("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='sites'").all()) {
    if (!/AFTER UPDATE|BEFORE UPDATE/i.test(t.sql)) continue;
    assert.match(t.sql, /UPDATE OF/i, t.name);
    assert.doesNotMatch(t.sql.split(/ ON sites/i)[0], /thumb_|favicon/i, t.name);
  }
  const before = JSON.stringify([db.prepare('SELECT * FROM scope_counts ORDER BY scope').all(), db.prepare('SELECT * FROM category_counts ORDER BY category_id, scope').all(), st(db)]);
  const n0 = db.prepare('SELECT total_changes() AS n').get().n;
  db.prepare('UPDATE sites SET thumb_path=?, thumb_lqip=?, thumb_captured_at=?, favicon_path=? WHERE id=?').run('x.webp', 'l', '2026-11-20T00:00:00Z', '/icons/a', 's2');
  assert.equal(db.prepare('SELECT total_changes() AS n').get().n - n0, 1);
  assert.equal(JSON.stringify([db.prepare('SELECT * FROM scope_counts ORDER BY scope').all(), db.prepare('SELECT * FROM category_counts ORDER BY category_id, scope').all(), st(db)]), before);
  // 集計（0026）は name_ja / name_en の更新で動かない（search_text と FTS だけが追従する）
  const counts = () => JSON.stringify([db.prepare('SELECT * FROM scope_counts ORDER BY scope').all(), db.prepare('SELECT * FROM category_counts ORDER BY category_id, scope').all()]);
  const c0 = counts();
  db.prepare("UPDATE sites SET name_en='Two' WHERE id='s2'").run();
  assert.equal(counts(), c0);
  assert.match(st(db).s2, /^plain 2 日本名 two /);
});

test('0030: 本番の複合 SELECT の項数上限に触れない（UNION は使わない）', () => {
  const sql = readFileSync(new URL(FILE, MIG), 'utf8').replace(/^--.*$/gm, '');
  assert.equal((sql.match(/\bUNION\b/gi) ?? []).length, 0);
  assert.ok(sql.includes('ADD COLUMN name_ja') && sql.includes('ADD COLUMN name_en'));
});

test('0030: name_ja / name_en が空文字なら NULL と同じ search_text（トリガも JS の searchText も余分な空白を足さない）', () => {
  const db = mkDb(); seed(db);
  db.prepare("UPDATE sites SET name_ja='', name_en='' WHERE id='s3'").run();
  const empty = st(db).s3;
  db.prepare("UPDATE sites SET name_ja=NULL, name_en=NULL WHERE id='s3'").run();
  assert.equal(empty, st(db).s3);
  assert.doesNotMatch(empty, /^plain 3  /);
  const row = { ...db.prepare('SELECT * FROM sites WHERE id=?').get('s3'), tags: db.prepare('SELECT label_ja, label_en FROM site_tags WHERE site_id=? ORDER BY label_ja').all('s3') };
  const base = searchText(row);
  assert.equal(searchText({ ...row, name_ja: '', name_en: '' }), base);
  assert.equal(searchText({ ...row, name_ja: null, name_en: null }), base);
});

test('0030: name_ja を持つサイトのタグを UPDATE / INSERT / DELETE しても search_text から name_ja・name_en が消えない', () => {
  const db = mkDb(); seed(db);
  db.prepare("UPDATE sites SET name_ja='日本名', name_en='English Name' WHERE id='s3'").run();
  const has = () => assert.match(st(db).s3, /^plain 3 日本名 english name /);
  db.prepare("UPDATE site_tags SET label_en='Game2' WHERE site_id='s3' AND label_ja='ゲーム'").run(); has();
  assert.match(st(db).s3, /game2/);
  db.prepare("UPDATE site_tags SET site_id='s4' WHERE site_id='s3' AND label_ja='アプリ'").run(); has();   // 付け替え（OLD 側の更新）
  db.prepare("UPDATE sites SET name_ja='日本名' WHERE id='s4'").run();
  db.prepare("UPDATE site_tags SET site_id='s3' WHERE site_id='s4'").run(); has();
  db.prepare("DELETE FROM site_tags WHERE site_id='s3' AND label_ja='アプリ'").run(); has();
});
