#!/usr/bin/env node
// sites.search_text（検索用の正規化済みテキスト）を、NFKC + 小文字化で全行埋め直す。
// サイトを追加・紹介文を直した後に実行する（migration の暫定値は ASCII の小文字化だけ）。
// 使い方: node scripts/refresh-search-text.mjs --local [--persist-to <dir>]        ローカル D1（DB は mise のみ）
//         node scripts/refresh-search-text.mjs --remote [--db mise|mise-preview]   本番（既定 mise）/ プレビュー用 DB。意図して実行すること
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { searchText } from '../src/sites.js';

export const USAGE = 'usage: node scripts/refresh-search-text.mjs --local [--persist-to <dir>] | --remote [--db mise|mise-preview]';
const DBS = ['mise', 'mise-preview']; // 許可リスト
// 引数を検証して { mode, db, persistTo } を返す。不正は例外（メッセージは USAGE 付き）。--local で mise 以外を指定すると wrangler が別名の DB を作ってしまうので拒否する。
export function parseArgs(argv) {
  const fail = m => { throw new Error(`${m}\n${USAGE}`); };
  let mode = null, db = 'mise', persistTo = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--local' || a === '--remote') { if (mode) fail('--local と --remote は同時に指定できません'); mode = a.slice(2); }
    else if (a === '--db') { db = argv[++i]; if (!DBS.includes(db)) fail(`--db は ${DBS.join(' / ')} のみ`); }
    else if (a === '--persist-to') { persistTo = argv[++i]; if (!persistTo || persistTo.startsWith('--')) fail('--persist-to にはディレクトリが必要です'); }
    else fail(`不明な引数: ${a}`);
  }
  if (!mode) fail('--local か --remote が必要です');
  if (mode === 'local' && db !== 'mise') fail('--local で使える DB は mise のみです（プレビュー DB は --remote）');
  if (mode === 'remote' && persistTo) fail('--persist-to は --local 専用です');
  return { mode, db, persistTo };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let o; try { o = parseArgs(process.argv.slice(2)); } catch (e) { console.error(e.message); process.exit(2); }
  const base = ['wrangler', 'd1', 'execute', o.db, `--${o.mode}`, ...(o.persistTo ? ['--persist-to', o.persistTo] : [])];
  const wr = args => execFileSync('npx', [...base, ...args], { encoding: 'utf8', maxBuffer: 1 << 28 });
  const rows = JSON.parse(wr(['--json', '--command', 'SELECT id, name, description_ja, description_en, url FROM sites']))[0].results;
  const q = s => `'${s.replace(/'/g, "''")}'`;
  const dir = mkdtempSync(join(tmpdir(), 'mise-st-')), file = join(dir, 'st.sql');
  writeFileSync(file, rows.map(r => `UPDATE sites SET search_text = ${q(searchText(r))} WHERE id = ${q(r.id)};`).join('\n'));
  wr(['--file', file]);
  console.log(`updated ${rows.length} rows (${o.db} --${o.mode})`);
}
