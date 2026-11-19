import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync } from 'node:fs';
import app from '../src/index.js';
import { parseScope, scopePath, categoryHref } from '../src/seo.js';
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
