import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { cleanTags, MAX_TAGS, querySites, searchText, cleanQuery } from '../src/sites.js';

// タグ絞り込み（#31）: ?tag= の解釈と、複数タグ・検索語・範囲・カテゴリとの AND。
const db = new DatabaseSync(':memory:');
db.exec(`CREATE TABLE sites(id TEXT PRIMARY KEY,url TEXT,name TEXT,description_ja TEXT,description_en TEXT,favicon_path TEXT,registered_at TEXT,source_updated_at TEXT,last_crawled_at TEXT,country_code TEXT,region_code TEXT,bulk_import INTEGER NOT NULL DEFAULT 0,search_text TEXT);
CREATE TABLE site_tags(site_id TEXT, label_ja TEXT NOT NULL, label_en TEXT, PRIMARY KEY(site_id,label_ja));
CREATE TABLE site_categories(site_id TEXT, category_id TEXT);
CREATE TABLE scope_counts(scope TEXT PRIMARY KEY, n INTEGER NOT NULL);`); // scope_counts: タグなし・範囲だけの total が読む集計表（本物はトリガが維持する。#126）
const site = (id, cc, st, cats, tags) => {
  db.prepare('INSERT INTO sites(id,url,name,description_ja,country_code,registered_at,search_text) VALUES(?,?,?,?,?,?,?)').run(id, 'https://' + id, id, 'd', cc, '2026-01-01T00:00:00Z', st);
  for (const c of cats) db.prepare('INSERT INTO site_categories VALUES(?,?)').run(id, c);
  for (const [ja, en] of tags) db.prepare('INSERT INTO site_tags VALUES(?,?,?)').run(id, ja, en);
};
site('a', 'JP', 'apple pie', ['c1'], [['キノコ', 'Mushrooms'], ['図鑑', 'Field guide']]);
site('b', 'JP', 'banana', ['c2'], [['キノコ', 'Mushrooms']]);
site('c', 'US', 'cherry pie', ['c1'], [['キノコ', 'Mushrooms'], ['図鑑', 'Field guide']]);
site('d', 'JP', 'plain', [], []);
db.exec("INSERT INTO scope_counts SELECT 'all', COUNT(*) FROM sites UNION ALL SELECT country_code, COUNT(*) FROM sites GROUP BY country_code");
const d = { prepare: sql => ({ bind: (...a) => ({ all: async () => ({ results: db.prepare(sql).all(...a) }), first: async () => db.prepare(sql).get(...a) }) }) };
const ids = async o => (await querySites(d, o)).items.map(i => i.id).sort();

test('cleanTags: 空・重複の除去、個数・長さの上限', () => {
  assert.deepEqual(cleanTags(undefined), []);
  assert.deepEqual(cleanTags(['', ' a ', 'a', 'b']), ['a', 'b']);
  assert.equal(cleanTags(Array.from({ length: MAX_TAGS + 1 }, (_, i) => 't' + i)), null);
  assert.equal(cleanTags(['x'.repeat(101)]), null);
  assert.deepEqual(cleanTags(['x'.repeat(100)]).length, 1);
});
test('タグ 1 つ: 日本語名・英語名のどちらでも一致', async () => {
  assert.deepEqual(await ids({ tags: ['キノコ'] }), ['a', 'b', 'c']);
  assert.deepEqual(await ids({ tags: ['Mushrooms'] }), ['a', 'b', 'c']);
  assert.deepEqual(await ids({ tags: ['無い'] }), []);
});
test('複数タグは AND', async () => {
  assert.deepEqual(await ids({ tags: ['キノコ', '図鑑'] }), ['a', 'c']);
});
test('検索語・範囲・カテゴリとも AND', async () => {
  assert.deepEqual(await ids({ tags: ['キノコ'], q: 'pie' }), ['a', 'c']);
  assert.deepEqual(await ids({ tags: ['キノコ'], cc: 'JP' }), ['a', 'b']);
  assert.deepEqual(await ids({ tags: ['キノコ', '図鑑'], cc: 'JP', category: 'c1', q: 'apple' }), ['a']);
  assert.deepEqual(await ids({ tags: ['キノコ'], category: 'c2' }), ['b']);
});
test('タグなしは従来どおり全件、total はタグ絞り込み後の件数', async () => {
  assert.equal((await querySites(d, {})).total, 4);
  assert.equal((await querySites(d, { tags: ['キノコ', '図鑑'] })).total, 2);
});

test('SSR: tag パラメータが付けば不正（6 個以上）でも noindex 対象', async () => {
  const { isSearchQuery } = await import('../src/seo.js');
  const sp = s => new URLSearchParams(s);
  assert.equal(isSearchQuery(sp('tag=a')), true);
  assert.equal(isSearchQuery(sp(Array.from({ length: MAX_TAGS + 1 }, (_, i) => 'tag=t' + i).join('&'))), true);
  assert.equal(isSearchQuery(sp('q=x')), true);
  assert.equal(isSearchQuery(sp('category=a')), false);
});

test('searchText: タグ ja/en を正規化して含める（q がタグに当たる）', async () => {
  const st = searchText({ name: 'N', description_ja: 'd', description_en: null, url: 'https://x/', tags: [{ label_ja: 'ｷﾉｺ', label_en: 'Mushrooms' }, { label_ja: '図鑑', label_en: null }] });
  assert.equal(st, 'n d  https://x/ キノコ mushrooms 図鑑 ');
  assert.equal(searchText({ name: 'N', description_ja: 'd', description_en: 'e', url: 'u' }), 'n d e u');
  db.prepare('UPDATE sites SET search_text=? WHERE id=?').run(st, 'd');
  assert.deepEqual(await ids({ q: cleanQuery('MUSHROOM') }), ['d']);
  assert.deepEqual(await ids({ q: cleanQuery('図鑑') }), ['d']);
});
