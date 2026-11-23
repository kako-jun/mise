// 縦ビューのフォルダ階層、新着、SSR の入口。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import app from '../src/index.js';
import '../public/vertical-link.js';
const { feedEntryHref } = globalThis.MiseFeedEntry;
const read = f => readFileSync(new URL('../' + f, import.meta.url), 'utf8');

test('縦ビューの URL に範囲・フォルダの全階層・一覧のページが残る', () => {
  const { directoryHref } = globalThis.MiseFeedEntry;
  const path = ['computing', 'programming'];
  assert.equal(feedEntryHref('jp-13', path, 2), '/jp/13/?category=computing&category=programming&page=2&view=feed');
  assert.equal(directoryHref('jp-13', path, 2), '/jp/13/?category=computing&category=programming&page=2');
  assert.equal(feedEntryHref('all', ['recent']), '/all/?category=recent&view=feed');
  assert.equal(feedEntryHref('../x', ['a b']), '/jp/?view=feed');
  assert.equal(feedEntryHref('us-ca', ['books', 'bad id', 'other']), '/us/ca/?category=books&view=feed');
});

// Workers の HTMLRewriter の #id に対する処理だけを Node で再現する。
globalThis.HTMLRewriter = class {
  handlers = [];
  on(sel, handler) { if (/^#[\w-]+$/.test(sel) && handler.element) this.handlers.push([sel.slice(1), handler.element]); return this; }
  transform(res) {
    const handlers = this.handlers;
    return new Response(new ReadableStream({ async start(ctl) {
      let html = await res.text();
      for (const [id, fn] of handlers) {
        html = html.replace(new RegExp(`<([a-z0-9]+)([^>]*\\sid="${id}"[^>]*)>([\\s\\S]*?)<\\/\\1>`), (all, tag, attrs, body) => {
          const a = new Map([...attrs.matchAll(/\s([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1], m[2] ?? '']));
          fn({
            setAttribute: (k, v) => a.set(k, String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;')),
            removeAttribute: k => a.delete(k),
            setInnerContent: (v, opts) => { if (opts?.html) body = v; },
            append() {},
          });
          return `<${tag}${[...a].map(([k, v]) => v === '' && k === 'hidden' ? ` ${k}` : ` ${k}="${v}"`).join('')}>${body}</${tag}>`;
        });
      }
      ctl.enqueue(new TextEncoder().encode(html)); ctl.close();
    } }), { status: res.status, headers: res.headers });
  }
};
const MIG = new URL('../migrations/', import.meta.url);
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
for (const f of readdirSync(MIG).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, MIG), 'utf8'));
const d = { prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); }, batch: async stmts => Promise.all(stmts.map(s => s.all())) };
const ASSETS = { fetch: async () => new Response(read('public/index.html'), { headers: { 'content-type': 'text/html' } }) };

const request = p => app.request(p, { redirect: 'manual' }, { DB: d, ASSETS });
const ssr = async p => { const r = await request(p); assert.equal(r.status, 200, p); return r.text(); };
const hrefOf = e => e.match(/href="([^"]*)"/)[1].replace(/&amp;/g, '&');
const top = db.prepare("SELECT p.id pid, c.id cid FROM categories p JOIN categories c ON c.parent_id = p.id WHERE p.parent_id IS NULL LIMIT 1").get();
const folder = '/jp/?category=' + top.pid + '&category=' + top.cid;

test('SSR: 新着が最初で縦ビューへ入り、隣に別リンクはない', async () => {
  const html = await ssr('/jp/');
  const first = html.match(/<div class="category category-recent">.*?<\/h2>/)?.[0];
  assert.match(first, /<div class="category category-recent">/);
  assert.equal(hrefOf(first.match(/<a class="recent-feed"[^>]*>/)[0]), '/jp/?category=recent&view=feed');
  assert.doesNotMatch(html, /class="recent-list"/);
});

test('SSR: 縦ビューの切り替えはフォルダを開いたときだけ表示する', async () => {
  for (const [url, link] of [
    ['/jp/', null],
    ['/all/?q=blog', '/all/?q=blog&view=feed'],
    [folder + '&tag=blog', folder + '&tag=blog&view=feed'],
    [folder, folder + '&view=feed'],
    ['/jp/?category=recent', '/jp/?category=recent&view=feed'],
  ]) {
    const entry = (await ssr(url)).match(/<a id="feed-entry"[^>]*>/)[0];
    assert.equal(/\shidden(?:\s|>|=)/.test(entry), !link, url);
    if (link) assert.equal(hrefOf(entry), link);
  }
});

test('新着 API は登録日時で絞り、同日時でもページをまたいで順序を維持する', async () => {
  const now = db.prepare("SELECT datetime('now') AS stamp").get().stamp;
  const insert = db.prepare("INSERT INTO sites (id, url, name, description_ja, country_code, registered_at, bulk_import, source_updated_at) VALUES (?, ?, 'feed-fixture-marker', 'fixture', ?, datetime(?, ?), ?, datetime(?, ?))");
  for (const [id, country, registered, bulk, updated] of [
    ['recent-a', 'JP', '-1 hour', 0, '-2 months'],
    ['recent-b', 'JP', '-1 hour', 0, '-2 months'],
    ['recent-c', 'JP', '-2 hours', 0, '-5 minutes'],
    ['recent-bulk', 'JP', '-5 minutes', 1, '-5 minutes'],
    ['recent-old', 'JP', '-31 days', 0, '-5 minutes'],
    ['recent-future', 'JP', '+2 hours', 0, '-5 minutes'],
    ['recent-foreign', 'US', '-5 minutes', 0, '-5 minutes'],
  ]) insert.run(id, 'https://' + id + '.example/', country, now, registered, bulk, now, updated);

  const endpoint = '/api/sites?scope=jp&category=recent&q=feed-fixture-marker&limit=1';
  const ids = [];
  let cursor;
  do {
    const response = await request(endpoint + (cursor ? '&cursor=' + cursor : ''));
    assert.equal(response.status, 200);
    const data = await response.json();
    if (!cursor) assert.equal(data.total, 3);
    ids.push(...data.items.map(s => s.id));
    cursor = data.next_cursor;
  } while (cursor);
  assert.deepEqual(ids, ['recent-a', 'recent-b', 'recent-c']);
  const listing = await ssr('/jp/?category=recent');
  assert.match(listing, /https:\/\/recent-a\.example\//);
});
