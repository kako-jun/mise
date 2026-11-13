#!/usr/bin/env node
// sites.search_text（検索用の正規化済みテキスト）を、NFKC + 小文字化で全行埋め直す。
// サイトを追加・紹介文を直した後に実行する（migration の暫定値は ASCII の小文字化だけ）。
// 使い方: node scripts/refresh-search-text.mjs --local   /   node scripts/refresh-search-text.mjs --remote（本番。意図して実行すること）
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { searchText } from '../src/sites.js';
const flag = process.argv[2];
if (!['--local', '--remote'].includes(flag) || process.argv.length > 3) { console.error('usage: node scripts/refresh-search-text.mjs --local|--remote'); process.exit(2); }
const wr = args => execFileSync('npx', ['wrangler', 'd1', 'execute', 'mise', flag, ...args], { encoding: 'utf8', maxBuffer: 1 << 28 });
const rows = JSON.parse(wr(['--json', '--command', 'SELECT id, name, description_ja, description_en, url FROM sites']))[0].results;
const q = s => `'${s.replace(/'/g, "''")}'`;
const dir = mkdtempSync(join(tmpdir(), 'mise-st-')), file = join(dir, 'st.sql');
writeFileSync(file, rows.map(r => `UPDATE sites SET search_text = ${q(searchText(r))} WHERE id = ${q(r.id)};`).join('\n'));
wr(['--file', file]);
console.log(`updated ${rows.length} rows (${flag})`);
