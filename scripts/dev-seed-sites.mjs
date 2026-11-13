#!/usr/bin/env node
// 開発用: ローカル D1 に合成サイトを投入/削除する（ページングの負荷確認用。本番には絶対に流さない: --local 固定）。
// 使い方: node scripts/dev-seed-sites.mjs [件数=2000]   /   node scripts/dev-seed-sites.mjs --clean
// 合成サイトの id は `dev-seed-` で始まる。政府機関カテゴリ (government-government-agencies) に全件、一部に NULL の更新日と他カテゴリ併載を混ぜる。
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const arg = process.argv[2] ?? '2000';
const run = sql => { const d = mkdtempSync(join(tmpdir(), 'mise-seed-')), f = join(d, 'seed.sql'); writeFileSync(f, sql); execFileSync('npx', ['wrangler', 'd1', 'execute', 'mise', '--local', '--file', f], { stdio: 'inherit' }); };
if (arg === '--clean') {
  run(`DELETE FROM site_tags WHERE site_id LIKE 'dev-seed-%'; DELETE FROM site_categories WHERE site_id LIKE 'dev-seed-%'; DELETE FROM sites WHERE id LIKE 'dev-seed-%';`);
} else {
  const n = Number(arg), lines = [];
  for (let i = 0; i < n; i++) {
    const id = `dev-seed-${String(i).padStart(5, '0')}`;
    const day = new Date(Date.UTC(2026, 10, 1) - (i % 700) * 86400000).toISOString().slice(0, 10); // 同日が約 3 件ずつ並ぶ（同値のタイブレークを試す）
    const upd = i % 10 === 0 ? 'NULL' : `'${day}'`;
    lines.push(`INSERT INTO sites (id,url,name,description_ja,description_en,country_code,source_updated_at,registered_at) VALUES ('${id}','https://dev-seed-${i}.example.jp/','合成サイト ${i}','合成データ ${i} の紹介文です。','Synthetic site ${i}.','JP',${upd},'${day}T00:00:00Z');`);
    lines.push(`INSERT INTO site_categories VALUES ('${id}','government-government-agencies');`);
    if (i % 7 === 0) lines.push(`INSERT INTO site_categories VALUES ('${id}','government-politics');`);
  }
  run(lines.join('\n'));
}
