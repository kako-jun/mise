import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import app from '../src/index.js';

// A minimal Workers HTMLRewriter implementation for exercising SSR markup.
globalThis.HTMLRewriter = class {
  handlers = [];
  on(sel, handler) { if (/^#[\w-]+$/.test(sel) && handler.element) this.handlers.push([sel.slice(1), handler.element]); return this; }
  transform(res) {
    const handlers = this.handlers;
    return new Response(new ReadableStream({ async start(ctl) {
      let html = await res.text();
      for (const [id, fn] of handlers) html = html.replace(new RegExp(`<([a-z0-9]+)([^>]*\\sid="${id}"[^>]*)>([\\s\\S]*?)<\\/\\1>`), (all, tag, attrs, body) => {
        const a = new Map([...attrs.matchAll(/\s([\w-]+)(?:="([^"]*)")?/g)].map(m => [m[1], m[2] ?? '']));
        fn({ setAttribute: (k, v) => a.set(k, String(v)), removeAttribute: k => a.delete(k), setInnerContent: v => { body = String(v); }, append() {} });
        return `<${tag}${[...a].map(([k, v]) => v === '' && k === 'hidden' ? ` ${k}` : ` ${k}="${v}"`).join('')}>${body}</${tag}>`;
      });
      ctl.enqueue(new TextEncoder().encode(html)); ctl.close();
    } }), { status: res.status, headers: res.headers });
  }
};
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)');
const migrations = new URL('../migrations/', import.meta.url);
for (const f of readdirSync(migrations).filter(f => f.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, migrations), 'utf8'));
const d = { prepare: sql => { const mk = args => ({ bind: (...a) => mk(a), all: async () => ({ results: db.prepare(sql).all(...args) }), first: async () => db.prepare(sql).get(...args) }); return mk([]); }, batch: async stmts => Promise.all(stmts.map(s => s.all())) };
const ASSETS = { fetch: async () => new Response(readFileSync(new URL('../public/index.html', import.meta.url)), { headers: { 'content-type': 'text/html' } }) };
const request = p => app.request(p, { redirect: 'manual' }, { DB: d, ASSETS });
const html = async p => { const r = await request(p); assert.equal(r.status, 200, p); return r.text(); };
const count = s => s.match(/<span id="sample-count"[^>]*>([^<]*)<\/span>/)?.[1];

test('category heading counts direct membership only, respecting scope and page', async () => {
  db.exec("INSERT INTO categories VALUES ('test-parent',NULL,'親','Parent',999), ('test-child','test-parent','子','Child',999), ('test-empty',NULL,'空','Empty',999)");
  const add = db.prepare('INSERT INTO sites (id,url,name,description_ja,country_code,region_code) VALUES (?,?,?,?,?,?)');
  const join = db.prepare('INSERT INTO site_categories (site_id,category_id) VALUES (?,?)');
  for (const [id, country, region, cats] of [['a','JP','JP-13',['test-parent']], ['b','JP','JP-01',['test-parent']], ['both','JP','JP-13',['test-parent','test-child']], ['child','JP','JP-13',['test-child']], ['foreign','US','US-CA',['test-parent']]]) {
    add.run(id, `https://${id}.example/`, id, 'fixture', country, region);
    for (const cat of cats) join.run(id, cat);
  }
  assert.equal(count(await html('/all/?category=test-parent')), '(4)');
  assert.equal(count(await html('/jp/?category=test-parent')), '(3)');
  assert.equal(count(await html('/jp/13/?category=test-parent')), '(2)');
  assert.equal(count(await html('/jp/?category=test-empty')), '(0)');
  for (let i = 0; i < 45; i++) { const id = `fixture-${i}`; add.run(id, `https://${id}.example/`, id, 'fixture', 'JP', 'JP-13'); join.run(id, 'test-parent'); }
  assert.equal(count(await html('/jp/?category=test-parent')), '(48)');
  assert.equal(count(await html('/jp/?category=test-parent&page=2')), '(48)');
  for (let i = 45; i < 1000; i++) { const id = `fixture-${i}`; add.run(id, `https://${id}.example/`, id, 'fixture', 'JP', 'JP-13'); join.run(id, 'test-parent'); }
  assert.equal(count(await html('/jp/?category=test-parent')), '(1,000+)');
});
