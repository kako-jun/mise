#!/usr/bin/env node
// 自己検査: カーソル方式のページングが、キーに NULL を含む行・キーを足した並びでも「全ページ連結 = 全件の並び」になること（node:sqlite のメモリ DB）。
// src/sites.js の SORT_KEYS を足す/変えたあとの回帰検査（現行の並び = bulk_import 先頭。さらに仮のキー region_code を先頭に足した並びも試す）。
import { DatabaseSync } from 'node:sqlite';
import { SORT_KEYS, querySites, randomSites, decodeCursor, siteOrder } from '../src/sites.js';
const db = new DatabaseSync(':memory:');
db.exec(`CREATE TABLE sites(id TEXT PRIMARY KEY,url TEXT,name TEXT,description_ja TEXT,description_en TEXT,favicon_path TEXT,registered_at TEXT,source_updated_at TEXT,last_crawled_at TEXT,country_code TEXT,region_code TEXT,search_text TEXT,bulk_import INTEGER);CREATE TABLE site_tags(site_id,label_ja,label_en);CREATE TABLE site_categories(site_id,category_id);`);
const ins = db.prepare('INSERT INTO sites(id,url,name,description_ja,registered_at,source_updated_at,bulk_import) VALUES(?,?,?,?,?,?,?)');
for (let i = 0; i < 300; i++) ins.run('s' + String(i).padStart(3, '0'), 'u' + i, 'n', 'd', i % 5 === 0 ? null : `2026-0${1 + i % 9}-0${1 + i % 7}T00:00:00Z`, i % 3 === 0 ? null : `2026-01-0${1 + i % 9}`, i % 4 === 0 ? 1 : (i % 11 === 0 ? null : 0));
const d = { prepare: sql => ({ bind: (...a) => ({ all: async () => ({ results: db.prepare(sql).all(...a) }), first: async () => db.prepare(sql).get(...a) }) }) };
let failed = 0;
for (const variant of ['現行の並び', '仮のキーをさらに先頭に足した並び']) {
  if (variant !== '現行の並び') SORT_KEYS.unshift({ expr: 's.region_code', dir: 'ASC', type: 'string' });
  const ref = db.prepare(`SELECT s.id FROM sites s ${siteOrder()}`).all().map(x => x.id);
  const bi = db.prepare('SELECT id, bulk_import AS b FROM sites s ' + siteOrder()).all().map(x => x.b);
  if (variant === '現行の並び' && bi.indexOf(1) !== -1 && bi.slice(bi.indexOf(1)).includes(0)) { console.log('NG  通常サイト(bulk_import=0)が先になっていない'); failed++; }
  for (const limit of [1, 7, 40, 100]) {
    const ids = []; let cur = null;
    for (let g = 0; g < 400; g++) { const r = await querySites(d, { limit, cursor: cur ? decodeCursor(cur) : null }); ids.push(...r.items.map(i => i.id)); cur = r.next_cursor; if (!cur) break; }
    const ok = JSON.stringify(ids) === JSON.stringify(ref);
    console.log(`${ok ? 'OK  ' : 'NG  '}${variant} limit=${limit}: ${ids.length}/${ref.length} 件`);
    if (!ok) failed++;
  }
}
// ピックアップに一括投入（bulk_import=1）が出ない
for (let i = 0; i < 20; i++) { const picks = await randomSites(d, null, 3); if (picks.some(p => p.bulk_import === 1)) { console.log('NG  ピックアップに bulk_import=1 が混ざった'); failed++; break; } if (i === 19) console.log('OK  ピックアップは bulk_import=0 のみ (20回)'); }
process.exit(failed ? 1 : 0);
