#!/usr/bin/env node
// 自己検査: refresh-search-text.mjs の引数検証（誤指定で別名の DB を作らない・本番以外の DB を取り違えない）と、0018・0020 の search_text トリガの挙動（node:sqlite のメモリ DB）。
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
// 0018・0020 のトリガ
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE sites(id TEXT PRIMARY KEY,url TEXT,name TEXT,description_ja TEXT,description_en TEXT,registered_at TEXT,source_updated_at TEXT);CREATE TABLE site_categories(site_id,category_id);CREATE TABLE site_tags(site_id TEXT NOT NULL,label_ja TEXT NOT NULL,label_en TEXT,PRIMARY KEY(site_id,label_ja));');
db.exec(readFileSync(new URL('../migrations/0018_site_sort_index.sql', import.meta.url), 'utf8'));
db.exec(readFileSync(new URL('../migrations/0020_search_text_tags.sql', import.meta.url), 'utf8'));
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
// 0020: タグ
db.exec("INSERT INTO sites(id,url,name,description_ja,description_en) VALUES('c','http://c/','Name','日本','En')");
t('タグなしは従来どおり', st('c') === 'name 日本 en http://c/');
db.exec("INSERT INTO site_tags VALUES('c','キノコ','Mushrooms')");
t('タグ INSERT で ja/en が入る', st('c') === 'name 日本 en http://c/ キノコ mushrooms');
db.exec("INSERT INTO site_tags VALUES('c','図鑑',NULL)");
t('英語名なしのタグも入る', st('c').includes('図鑑') && st('c').includes('mushrooms'));
db.exec("UPDATE site_tags SET label_en='Fungi' WHERE site_id='c' AND label_ja='キノコ'");
t('タグ UPDATE で追従', st('c').includes('fungi') && !st('c').includes('mushrooms'));
db.exec("UPDATE sites SET name='Renamed' WHERE id='c'");
t('サイト側の UPDATE でもタグを保つ', st('c').startsWith('renamed') && st('c').includes('fungi'));
db.exec("DELETE FROM site_tags WHERE site_id='c' AND label_ja='キノコ'");
t('タグ DELETE で追従', !st('c').includes('fungi') && st('c').includes('図鑑'));
db.exec("INSERT INTO site_tags VALUES('b','メモ','Memo')");
t('明示 search_text のサイトもタグ追加で作り直す（仕様）', st('b').includes('memo'));
// 埋め直し UPDATE（0020 末尾）が既存行にタグを入れる
db.exec("DROP TRIGGER site_tags_search_text_ai; INSERT INTO site_tags VALUES('a','後付','Late'); UPDATE sites SET search_text='stale' WHERE id='a'");
db.exec(readFileSync(new URL('../migrations/0020_search_text_tags.sql', import.meta.url), 'utf8').split('-- 既存行の埋め直し（暫定値）。')[1]);
t('既存行の埋め直し UPDATE', st('a').includes('late'));
process.exit(failed ? 1 : 0);
