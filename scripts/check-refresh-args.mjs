#!/usr/bin/env node
// 自己検査: refresh-search-text.mjs の引数検証（誤指定で別名の DB を作らない・本番以外の DB を取り違えない）と、0018 の search_text トリガの挙動（node:sqlite のメモリ DB）。
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { parseArgs } from './refresh-search-text.mjs';
let failed = 0;
const t = (label, ok) => { console.log(`${ok ? 'OK  ' : 'NG  '}${label}`); if (!ok) failed++; };
const rejects = a => { try { parseArgs(a); return false; } catch (_) { return true; } };
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
t('--local は mise', eq(parseArgs(['--local']), { mode: 'local', db: 'mise', persistTo: null }));
t('--local --persist-to', parseArgs(['--local', '--persist-to', '/x']).persistTo === '/x');
t('--remote の既定は mise', parseArgs(['--remote']).db === 'mise');
t('--remote --db mise-preview', parseArgs(['--remote', '--db', 'mise-preview']).db === 'mise-preview');
for (const [label, a] of [['モードなし', []], ['--local --remote', ['--local', '--remote']], ['--local で preview', ['--local', '--db', 'mise-preview']], ['未許可の DB', ['--remote', '--db', 'other']], ['--db の値なし', ['--remote', '--db']], ['--remote で --persist-to', ['--remote', '--persist-to', '/x']], ['不明な引数', ['--local', '--x']]]) t(`拒否: ${label}`, rejects(a));
// 0018 のトリガ
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE sites(id TEXT PRIMARY KEY,url TEXT,name TEXT,description_ja TEXT,description_en TEXT,registered_at TEXT,source_updated_at TEXT);CREATE TABLE site_categories(site_id,category_id);');
db.exec(readFileSync(new URL('../migrations/0018_site_sort_index.sql', import.meta.url), 'utf8'));
const st = id => db.prepare('SELECT search_text s FROM sites WHERE id=?').get(id).s;
db.exec("INSERT INTO sites(id,url,name,description_ja,description_en) VALUES('a','http://a/','Name','日本','En')");
t('INSERT で暫定値', st('a') === 'name 日本 en http://a/');
db.exec("UPDATE sites SET description_en='Zebra' WHERE id='a'");
t('description_en の UPDATE で入れ直す', st('a').includes('zebra'));
db.exec("UPDATE sites SET name='x', search_text='correct' WHERE id='a'");
t('明示した search_text は上書きしない', st('a') === 'correct');
db.exec("UPDATE sites SET search_text='ＮＦＫＣ' WHERE id='a'");
t('search_text だけの UPDATE は再発火しない', st('a') === 'ＮＦＫＣ');
db.exec("INSERT INTO sites(id,url,name,description_ja,search_text) VALUES('b','http://b/','n','d','explicit')");
t('INSERT で明示した search_text を尊重', st('b') === 'explicit');
process.exit(failed ? 1 : 0);
