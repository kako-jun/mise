#!/usr/bin/env node
// デプロイ前の確認（読み取りのみ）: migrations/ の全ファイルが、本番 `mise` とプレビュー `mise-preview` の D1 に適用済みか。
// 新しい Worker は新しい列を読むので、migration より先にデプロイされると API と SSR 全体が 500 になる（例: 0028 の thumb_path）。マージ前に必ず実行する。
//   node scripts/check-migrations-applied.mjs [--db mise --db mise-preview]
// D1 の `d1_migrations` 表を SELECT で読むだけ（`d1 execute --remote`。書き込みはしない）。未適用が 1 つでもあれば、または読めなければ非 0 で終了する。
// プロキシ環境では NODE_EXTRA_CA_CERTS が要ることがある（docs/operations/deploy.md）。
import { readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const migrationFiles = dir => readdirSync(dir).filter(f => /^\d{4}_.*\.sql$/.test(f)).sort();
export const pendingMigrations = (files, applied) => files.filter(f => !new Set(applied).has(f));
// wrangler の --json 出力（[{ results: [{ name }] }]）から適用済みの名前を取り出す。形が違えば例外（読めたことにしない）。
export const appliedFromJson = text => {
  const j = JSON.parse(text);
  if (!Array.isArray(j) || !j[0] || !Array.isArray(j[0].results)) throw new Error('unexpected wrangler output');
  return j[0].results.map(r => r.name);
};

export function check(dbs, { files = migrationFiles(new URL('../migrations/', import.meta.url)), query } = {}) {
  const q = query || (db => appliedFromJson(execFileSync('npx', ['wrangler', 'd1', 'execute', db, '--remote', '--json', '--command', 'SELECT name FROM d1_migrations ORDER BY id'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })));
  const report = {};
  for (const db of dbs) {
    try { report[db] = { pending: pendingMigrations(files, q(db)) }; } catch (e) { report[db] = { error: String(e.message).split('\n')[0] }; }
  }
  return report;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2), dbs = args.flatMap((a, i) => (a === '--db' ? [args[i + 1]] : []));
  const report = check(dbs.length ? dbs : ['mise', 'mise-preview']);
  let bad = 0;
  for (const [db, r] of Object.entries(report)) {
    if (r.error) { bad++; console.log(`NG  ${db}: 読めない（${r.error}）`); }
    else if (r.pending.length) { bad++; console.log(`NG  ${db}: 未適用 ${r.pending.join(', ')}（先に適用する。手順は docs/operations/deploy.md。mise-preview は -c の一時設定が要る）`); }
    else console.log(`OK  ${db}: migrations/ の全ファイルが適用済み`);
  }
  process.exit(bad ? 1 : 0);
}
