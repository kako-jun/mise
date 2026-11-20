// 縦フィードの入口 (#178) の href 生成（public/vertical-link.js）と、入口が SSR・JS 両方にあること。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { readdirSync } from 'node:fs';
import app from '../src/index.js';
import '../public/vertical-link.js';
const { feedEntryHref } = globalThis.MiseFeedEntry;
const read = f => readFileSync(new URL('../' + f, import.meta.url), 'utf8');
test('範囲とトップ階層カテゴリだけを引き継ぐ', () => {
  assert.equal(feedEntryHref('jp'), '/jp/?view=feed');
  assert.equal(feedEntryHref('jp-13'), '/jp/13/?view=feed');
  assert.equal(feedEntryHref('all', 'computing'), '/all/?view=feed&cat=computing');
  assert.equal(feedEntryHref('us-ca', 'a b'), '/us/ca/?view=feed'); // 不正な id は捨てる
});
test('不正な範囲は既定(jp)に倒す', () => {
  assert.equal(feedEntryHref('../x'), '/jp/?view=feed');
  assert.equal(feedEntryHref(undefined, undefined), '/jp/?view=feed');
});
test('入口は nofollow の通常リンクで、app.js は同じ関数を使う', () => {
  assert.match(read('public/index.html'), /<a id="feed-entry"[^>]*href="[^"]+"[^>]*rel="nofollow"/);
  assert.match(read('public/app.js'), /MiseFeedEntry\.feedEntryHref/);
  assert.match(read('public/app.js'), /MiseFeedEntry\.feedEntryLossy/);
  assert.match(read('public/app.js'), /feedNote:globalThis\.MiseFeedEntry\?\.NOTE\.ja/);
});
test('案内は引き継げない状態（検索・タグ・2 段目以降のカテゴリ）のときだけ出す', () => {
  const { feedEntryLossy, NOTE } = globalThis.MiseFeedEntry;
  assert.equal(feedEntryLossy(false, 0), false); // トップ・範囲のみ
  assert.equal(feedEntryLossy(false, 1), false); // トップ階層カテゴリのみ
  assert.equal(feedEntryLossy(true, 0), true);   // 検索中・タグ中
  assert.equal(feedEntryLossy(true, 1), true);
  assert.equal(feedEntryLossy(false, 2), true);  // 2 段目以降
  assert.ok(NOTE.ja && NOTE.en);
});

// Node には Workers の HTMLRewriter が無いので、ここで検査する #id の属性操作（setAttribute / removeAttribute）だけを実装した最小の代用品を使う。
// 他のセレクタ（title・meta・head 等）と setInnerContent / append は何もしない（この検査の対象外）。
globalThis.HTMLRewriter = class {
  constructor() { this.h = []; }
  on(sel, handler) { if (/^#[\w-]+$/.test(sel) && handler.element) this.h.push([sel.slice(1), handler.element]); return this; }
  transform(res) {
    const h = this.h;
    return new Response(new ReadableStream({ async start(ctl) {
      let html = await res.text();
      for (const [id, fn] of h) html = html.replace(new RegExp(`<([a-z0-9]+)([^>]*\\sid="${id}"[^>]*)>`), (all, tag, attrs) => {
        const a = new Map([...attrs.matchAll(/\s([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1], m[2] ?? '']));
        fn({ setAttribute: (k, v) => a.set(k, String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;')), removeAttribute: k => a.delete(k), setInnerContent() {}, append() {} });
        return `<${tag}${[...a].map(([k, v]) => v === '' && k === 'hidden' ? ` ${k}` : ` ${k}="${v}"`).join('')}>`;
      });
      ctl.enqueue(new TextEncoder().encode(html)); ctl.close();
    } }), { status: res.status, headers: res.headers });
  }
};
// SSR: 実アプリ（src/index.js）を実 DB（migrations を流した in-memory SQLite）で動かし、返った HTML の #feed-entry / #feed-note を見る。
const MIG = new URL('../migrations/', import.meta.url);
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
const d = { prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); }, batch: async stmts => Promise.all(stmts.map(s => s.all())) };
const ASSETS = { fetch: async () => new Response(read('public/index.html'), { headers: { 'content-type': 'text/html' } }) };
const ssr = async p => { const r = await app.request(p, { redirect: 'manual' }, { DB: d, ASSETS }); assert.equal(r.status, 200, p); const h = await r.text(); return { note: h.match(/<p id="feed-note"[^>]*>/)?.[0], entry: h.match(/<a id="feed-entry"[^>]*>/)?.[0] }; };
const hrefOf = e => e.match(/href="([^"]*)"/)[1].replace(/&amp;/g, '&');
// 実データから「国に登録のある区画」と「子を持つトップ階層カテゴリ」を選ぶ（固定 id に依存しない）
const region = db.prepare("SELECT region_code c FROM sites WHERE region_code IS NOT NULL AND country_code = 'JP' LIMIT 1").get().c.toLowerCase().replace('-', '/');
const top = db.prepare("SELECT p.id pid, c.id cid FROM categories p JOIN categories c ON c.parent_id = p.id JOIN category_counts k ON k.category_id = c.id AND k.scope = 'all' AND k.n > 0 WHERE p.parent_id IS NULL LIMIT 1").get();
const SSR_CASES = [
  // [URL, 案内が出るか, 入口の href]
  ['/jp/', false, '/jp/?view=feed'],
  ['/all/', false, '/all/?view=feed'],
  [`/jp/${region.split('/')[1]}/`, false, `/${region}/?view=feed`],
  [`/jp/?category=${top.pid}`, false, `/jp/?view=feed&cat=${top.pid}`],
  ['/jp/?q=x', true, '/jp/?view=feed'],
  [`/jp/?q=x&category=${top.pid}`, true, `/jp/?view=feed&cat=${top.pid}`],
  ['/jp/?tag=' + encodeURIComponent('ブログ'), true, '/jp/?view=feed'],
  [`/jp/?category=${top.pid}&tag=` + encodeURIComponent('ブログ'), true, `/jp/?view=feed&cat=${top.pid}`],
  [`/jp/?category=${top.pid}&category=${top.cid}`, true, `/jp/?view=feed&cat=${top.pid}`],
  ['/jp/?category=__no_such_category__', false, '/jp/?view=feed'], // 不正なカテゴリは捨てて既定
];
test('SSR: 引き継げない状態のときだけ #feed-note の hidden が外れ、#feed-entry の href が引き継ぎどおり', async () => {
  for (const [url, shown, href] of SSR_CASES) {
    const r = await ssr(url);
    assert.ok(r.note && r.entry, `${url}: 要素が無い`);
    assert.equal(!/\shidden(\s|>|=)/.test(r.note), shown, `${url}: 案内の表示 (${r.note})`);
    assert.equal(hrefOf(r.entry), href, `${url}: href`);
    assert.match(r.entry, /rel="nofollow"/);
  }
});
