import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import '../public/countries.js';

// 国旗と国名の表は ISO 3166-1 の全コード（249）を最初から持つ（#291）。国が増えても何も足さない。再発防止の検査。
const ISO = globalThis.MISE_ISO;
const flagDir = new URL('../public/flags/', import.meta.url);
const flagFiles = readdirSync(flagDir).filter(f => f.endsWith('.svg')).map(f => f.slice(0, -4).toUpperCase());

test('全 ISO コードは 249 個・重複なし・大文字 2 文字', () => {
  assert.equal(ISO.length, 249);
  assert.equal(new Set(ISO).size, 249);
  for (const c of ISO) assert.match(c, /^[A-Z]{2}$/);
});

test('(a) 全コードに旗アセットがあり、空ではない SVG である', () => {
  for (const c of ISO) {
    const f = new URL(`${c.toLowerCase()}.svg`, flagDir);
    assert.ok(statSync(f).size > 100, `${c}: 旗が無い`);
    assert.match(readFileSync(f, 'utf8'), /<svg[^>]*viewBox="0 0 640 480"/, `${c}: 4x3 の SVG ではない`);
  }
});

test('(b) 全旗アセットに国名（日英）がある。表にない旗ファイルを置かない', () => {
  assert.deepEqual([...flagFiles].sort(), [...ISO].sort());
  for (const c of flagFiles) {
    const n = globalThis.MISE_COUNTRIES[c];
    assert.ok(n && n.ja && n.en, c);
    assert.notEqual(n.en, c, `${c}: 英語名が取れていない`);
    assert.notEqual(n.ja, c, `${c}: 日本語名が取れていない`);
  }
  assert.equal(globalThis.MISE_COUNTRIES.JP.ja, '日本');
  assert.equal(globalThis.MISE_COUNTRIES.HK.ja, '香港'); // 長い正式名にしない
  assert.equal(globalThis.MISE_COUNTRIES.US.en, 'United States');
  assert.equal(globalThis.MISE_COUNTRIES.JP.lat, 36); // 地球の向き移動用の座標は従来どおり
});

test('(c) 未知のコードは壊れた画像でなく国コードのバッジになる', () => {
  for (const bad of ['XX', 'ZZ', 'AN', 'YU', '<b>']) {
    const h = globalThis.MiseCountry.html(bad);
    assert.doesNotMatch(h, /<img/, bad);
    assert.match(h, /^<span class="scope-code" aria-hidden="true">/, bad);
    assert.doesNotMatch(h, /<b>/, 'エスケープする');
    assert.equal(globalThis.MiseCountry.src(bad), null);
    assert.equal(globalThis.MiseCountry.name(bad, 'ja'), bad);
  }
  assert.match(globalThis.MiseCountry.html('JP'), /<img class="scope-flag" src="\/flags\/jp\.svg"/);
  assert.equal(globalThis.MiseCountry.src('PS'), '/flags/ps.svg');
});

test('(d) シード（migrations とデータ）の country_code・区画の国は、全て旗セットに含まれる', () => {
  const MIG = new URL('../migrations/', import.meta.url), db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
  for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
  const seen = new Set(db.prepare('SELECT DISTINCT country_code c FROM sites WHERE country_code IS NOT NULL').all().map(r => r.c));
  for (const r of db.prepare('SELECT DISTINCT region_code c FROM sites WHERE region_code IS NOT NULL').all()) seen.add(r.c.slice(0, 2));
  for (const f of readdirSync(new URL('../data/', import.meta.url)).filter(f => f.endsWith('.json'))) {
    for (const m of readFileSync(new URL(`../data/${f}`, import.meta.url), 'utf8').matchAll(/"country_code"\s*:\s*"([^"]+)"/g)) seen.add(m[1]);
  }
  assert.ok(seen.size > 0);
  for (const c of seen) assert.ok(ISO.includes(c), `${c}: 旗セットに無い`);
});

test('Python 側の ISO 一覧（整合チェック）は MISE_ISO と同じ', () => {
  const py = readFileSync(new URL('../scripts/llm/integrity.py', import.meta.url), 'utf8');
  const body = py.slice(py.indexOf('ISO_3166_1 = ('), py.indexOf('ISO_LIST'));
  assert.deepEqual([...body.matchAll(/"([A-Z]{2})"/g)].map(m => m[1]).sort(), [...ISO].sort());
});
