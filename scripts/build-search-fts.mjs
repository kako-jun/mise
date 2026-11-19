#!/usr/bin/env node
// sites_fts（検索の全文索引。migration 0027）を sites.search_text から構築・検証する。冪等（何度流しても同じ結果）。派生物なのでいつでも作り直せる。
// 手順: fts_state.ready = 0（検索は instr の経路に落ちる）→ 索引を空にする（'delete-all'）→ rowid の範囲を 1 万行ずつ INSERT → ready = 1 → 件数と整合性を検証（失敗したら ready = 0 に戻して止める）。
// ready = 1 を検証より先にするのは、検証と ready の間の登録・削除を見逃さないため（ready = 1 以降の変更はトリガが拾い、それ以前のずれは検証が見つける）。
// 10 万件の 'rebuild' 一発は 12〜24 秒かかり D1 の 1 クエリ 30 秒の上限に近いので、範囲で分ける（1 クエリ 1 万行 = 約 2 秒）。
// 使い方: node scripts/build-search-fts.mjs --local [--persist-to <dir>] [--chunk N] [--check]
//         node scripts/build-search-fts.mjs --remote [--db mise|mise-preview] [--chunk N] [--check]   意図して実行すること（本番は --db mise）
//   --check: 構築はせず、件数の一致と整合性（integrity-check）だけを確かめる（SELECT と検証コマンドのみ）。
// ready = 0 の間は同期トリガが何もしない（migration 0027）ので、構築中に登録・削除されたサイトは索引に入らない/残る。構築中は夜間ジョブ・登録を止める
// （構築の最後の件数と整合性の検査がずれを検出する。落ちたら ready = 0 に戻って止まるので、再実行する）。
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parseArgs as parseBase } from './refresh-search-text.mjs';

export const USAGE = 'usage: node scripts/build-search-fts.mjs --local [--persist-to <dir>] [--chunk N] [--check] | --remote [--db mise|mise-preview] [--chunk N] [--check]';
export const DEFAULT_CHUNK = 10000;
// 引数の検証は refresh-search-text.mjs と同じ規則（--local は mise のみ・--db は許可リスト）に --chunk と --check を足したもの。
export function parseArgs(argv) {
  const rest = [], o = { chunk: DEFAULT_CHUNK, check: false };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--chunk') { const v = argv[++i]; if (!/^\d{1,6}$/.test(v ?? '') || Number(v) < 1) throw new Error(`--chunk には 1 以上の整数が必要です\n${USAGE}`); o.chunk = Number(v); }
    else if (argv[i] === '--check') o.check = true;
    else rest.push(argv[i]);
  }
  return { ...parseBase(rest), ...o };
}
// 範囲 [min, max] を size 行ごとの [from, to] に分ける（rowid は欠番があってよい）。
export function chunkRanges(min, max, size) {
  const out = [];
  for (let a = min; a <= max; a += size) out.push([a, Math.min(a + size - 1, max)]);
  return out;
}
export const SQL = {
  begin: `UPDATE fts_state SET ready = 0 WHERE name = 'sites_fts'; INSERT INTO sites_fts (sites_fts) VALUES ('delete-all');`,
  chunk: (a, b) => `INSERT INTO sites_fts (rowid, search_text) SELECT rowid, search_text FROM sites WHERE rowid BETWEEN ${Number(a)} AND ${Number(b)};`,
  counts: `SELECT (SELECT COUNT(*) FROM sites) AS sites, (SELECT COUNT(*) FROM sites_fts_docsize) AS indexed`,
  // 外部コンテンツ表の整合性: 第 2 引数 rank = 1 で sites の内容と突き合わせる（ずれていればエラー）。
  integrity: `INSERT INTO sites_fts (sites_fts, rank) VALUES ('integrity-check', 1);`,
  ready: `UPDATE fts_state SET ready = 1 WHERE name = 'sites_fts';`,
  unready: `UPDATE fts_state SET ready = 0 WHERE name = 'sites_fts';`,
  range: `SELECT COALESCE(MIN(rowid), 0) AS lo, COALESCE(MAX(rowid), -1) AS hi FROM sites`,
};

// 構築の本体。wr(sql) は SQL を流して結果の行を返す関数（テストではモック）。
export function runBuild(wr, { chunk = DEFAULT_CHUNK, log = () => {} } = {}) {
  const verify = () => {
    const [c] = wr(SQL.counts);
    if (c.sites !== c.indexed) throw new Error(`件数が合わない: sites ${c.sites} / sites_fts ${c.indexed}。再実行する（構築中の登録・削除でずれた場合も再実行で直る）`);
    wr(SQL.integrity);
    return c;
  };
  const [{ lo, hi }] = wr(SQL.range);
  wr(SQL.begin);
  const ranges = chunkRanges(lo, hi, chunk);
  ranges.forEach(([a, b], i) => { wr(SQL.chunk(a, b)); log(`chunk ${i + 1}/${ranges.length} rowid ${a}..${b}`); });
  wr(SQL.ready);
  try { return verify(); }
  catch (e) { try { wr(SQL.unready); } catch (e2) { throw new Error(`${e.message}（さらに ready = 0 に戻す操作も失敗: ${e2.message}。fts_state.ready は 1 のまま。手で 0 にする）`, { cause: e }); } throw e; }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let o; try { o = parseArgs(process.argv.slice(2)); } catch (e) { console.error(e.message); process.exit(2); }
  const base = ['wrangler', 'd1', 'execute', o.db, `--${o.mode}`, ...(o.persistTo ? ['--persist-to', o.persistTo] : [])];
  const wr = sql => JSON.parse(execFileSync('npx', [...base, '--json', '--command', sql], { encoding: 'utf8', maxBuffer: 1 << 28 }))[0].results;
  try {
    if (o.check) {
      const [c] = wr(SQL.counts);
      if (c.sites !== c.indexed) throw new Error(`件数が合わない: sites ${c.sites} / sites_fts ${c.indexed}`);
      wr(SQL.integrity);
      console.log(`ok: ${c.sites} rows, integrity-check passed (${o.db} --${o.mode})`);
    } else {
      const c = runBuild(wr, { chunk: o.chunk, log: console.log });
      console.log(`built ${c.indexed} rows, integrity-check passed, ready = 1 (${o.db} --${o.mode})`);
    }
  } catch (e) { console.error(e.message); process.exit(1); }
}
