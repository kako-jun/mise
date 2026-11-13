// サイト一覧の取得（API と SSR が共有する単一の実装）。
// ★ 並び順の定義はこのファイルの SORT_KEYS だけ。ORDER BY・カーソルのキー列・「次のページ」の比較条件・インデックス列はここから生成する。
//   並びのキーを足す/変えるとき（例: 先頭の `bulk_import`。#71 で足した）:
//   1. SORT_KEYS に { expr, dir } を足す（先頭ほど優先。末尾の s.id は必ず最後に残す）。
//   2. sortIndexSql() が返す CREATE INDEX を新しい migration に書く（scripts/check-api-headers.mjs が最新の migration と一致を検査する）。
//   3. 追加した列が画面に必要なら COLUMNS にも足す（並びだけに使う列は足さなくてよい）。
//   4. docs/design/ui.md の「既定の並び」を更新する。カーソルは配列なので形式の変更は不要。
export const PAGE_SIZE = 40, MAX_LIMIT = 100;
// dir は SQLite の NULL の並びに合わせる: ASC では NULL が先頭、DESC では NULL が末尾。
export const SORT_KEYS = [
  // 台帳などからの一括投入（bulk_import=1。官公庁の名簿など）は後ろ。通常のサイト（0）が先（NULL なし、DEFAULT 0）
  { expr: `s.bulk_import`, dir: 'ASC', type: 'number' },
  // 新しい順（サイト自身の更新日、不明なら登録日。日時が読めない/NULL は DESC で末尾）
  { expr: `datetime(COALESCE(s.source_updated_at, s.registered_at))`, dir: 'DESC', type: 'string' },
  { expr: 's.id', dir: 'ASC', type: 'string' }, // 同値の安定化（必ず最後）
  // type はカーソルの値の型（'string' | 'number'）。不一致は 400。
];
export const siteOrder = () => `ORDER BY ${SORT_KEYS.map(k => `${k.expr} ${k.dir}`).join(', ')}`;
// migration に書くインデックス（テーブル別名 `s.` は外す）。名前に定義のハッシュを含めるので、SORT_KEYS を変えると名前も変わり、
// 旧インデックスと衝突（IF NOT EXISTS で作られずに残る）しない。新しい migration で新名の CREATE と旧名の DROP INDEX を書く。
const indexCols = () => SORT_KEYS.map(k => `${k.expr.replace(/\bs\./g, '')} ${k.dir}`.replace(/ ASC$/, '')).join(', ');
const fnv = str => { let h = 0x811c9dc5; for (const ch of str) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; } return h.toString(16).padStart(8, '0'); };
export const sortIndexName = () => `idx_sites_sort_${fnv(indexCols())}`;
export const sortIndexSql = () => `CREATE INDEX IF NOT EXISTS ${sortIndexName()} ON sites (${indexCols()});`;
const keyCols = () => SORT_KEYS.map((k, i) => `${k.expr} AS _k${i}`).join(", ");

// ---- 入力の解釈（不正は null → 呼び出し側が 400/404） ----
export const parseLimit = raw => { if (raw == null || raw === '') return PAGE_SIZE; if (!/^-?\d{1,9}$/.test(raw)) return null; const n = Number(raw); return n >= 1 ? Math.min(n, MAX_LIMIT) : null; }; // 上限超は MAX_LIMIT に丸める
export const parsePage = raw => { if (raw == null || raw === '') return 1; if (!/^\d{1,6}$/.test(raw)) return null; const n = Number(raw); return n >= 1 ? n : null; };
// カーソルは全キー値の配列（id を含む）を JSON → base64url にしたもの。キーが増えても形式は変わらない。
export const encodeCursor = vals => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(vals)))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
export function decodeCursor(raw) {
  if (!raw) return { none: true };
  try {
    const b = atob(raw.replace(/-/g, '+').replace(/_/g, '/')), vals = JSON.parse(new TextDecoder().decode(Uint8Array.from(b, c => c.charCodeAt(0))));
    const ok = Array.isArray(vals) && vals.length === SORT_KEYS.length && vals.every((v, i) => v === null || (typeof v === SORT_KEYS[i].type && (typeof v === 'number' ? Number.isFinite(v) : v.length <= 200)));
    return ok && vals.at(-1) !== null ? { vals } : null;
  } catch (_) { return null; }
}
// 全角半角・大文字小文字をそろえる（検索語と sites.search_text の両方に同じ処理を使う）
export const normalizeSearch = s => String(s ?? '').normalize('NFKC').toLowerCase();
export const searchText = s => normalizeSearch(`${s.name} ${s.description_ja} ${s.description_en ?? ''} ${s.url}`);
// instr で比較するので、LIKE のメタ文字のエスケープも長さの制限（D1 は LIKE のパターンが約50バイトを超えると too complex）も要らない。
export const cleanQuery = raw => { const q = [...normalizeSearch(raw).trim()].slice(0, 100).join(''); return q || null; }; // コードポイント単位で切る

// 「cursor の次」の条件。キーを辞書式に比較する（先頭から: 前のキーが等しく、このキーが後ろ）。NULL は dir に合わせて扱う。
function afterCursor(vals) {
  const alts = [], args = [];
  SORT_KEYS.forEach((k, i) => {
    const eq = [], a = [];
    for (let j = 0; j < i; j++) { if (vals[j] === null) eq.push(`${SORT_KEYS[j].expr} IS NULL`); else { eq.push(`${SORT_KEYS[j].expr} = ?`); a.push(vals[j]); } }
    let after;
    if (k.dir === 'ASC') { if (vals[i] === null) after = `${k.expr} IS NOT NULL`; else { after = `${k.expr} > ?`; a.push(vals[i]); } } // ASC: NULL が先頭
    else { if (vals[i] === null) after = null; else { after = `(${k.expr} < ? OR ${k.expr} IS NULL)`; a.push(vals[i]); } } // DESC: NULL が末尾
    if (after) { alts.push(`(${[...eq, after].join(' AND ')})`); args.push(...a); }
  });
  return { sql: `(${alts.join(' OR ')})`, args };
}

function filters({ cc, category, q }) {
  const where = [], args = [];
  if (cc) { where.push('s.country_code = ?'); args.push(cc); }
  if (category) { where.push('s.id IN (SELECT site_id FROM site_categories WHERE category_id = ?)'); args.push(category); }
  if (q) { where.push('instr(s.search_text, ?) > 0'); args.push(q); } // q は cleanQuery 済み（search_text も正規化済みなので大小・全角半角はそろっている）
  return { where, args };
}

// 画面に出す列。bulk_import は並びのほか NEW タグの抑止に画面が使うので含める。並びだけに使う列は SORT_KEYS で足りるのでここには要らない。
const COLUMNS = `s.id, s.name, s.url, s.description_ja, s.description_en, s.favicon_path, s.registered_at, s.bulk_import, s.source_updated_at, s.last_crawled_at, s.country_code, s.region_code, (SELECT json_group_array(json_object('ja',t.label_ja,'en',t.label_en)) FROM site_tags t WHERE t.site_id=s.id) AS tags_json, (SELECT json_group_array(c.category_id) FROM site_categories c WHERE c.site_id=s.id) AS category_ids_json`;
const shape = ({ category_ids_json, ...r }) => { for (const k of Object.keys(r)) if (k.startsWith('_k')) delete r[k]; return { ...r, category_ids: JSON.parse(category_ids_json || '[]') }; };

// 1 ページ分。cursor は decodeCursor の結果、offset は ?page=N（SSR・直リンクの開始位置）用でカーソルが無いときだけ使う。
// total は先頭ページ（カーソルなし）のときだけ数える。q は cleanQuery 済みであること。
export async function querySites(db, { cc = null, category = null, q = null, limit = PAGE_SIZE, cursor = null, offset = 0 }) {
  const { where, args } = filters({ cc, category, q });
  const conds = [...where], a = [...args], hasCursor = cursor && !cursor.none;
  if (hasCursor) { const c = afterCursor(cursor.vals); conds.push(c.sql); a.push(...c.args); }
  const sql = `SELECT ${COLUMNS}, ${keyCols()} FROM sites s ${conds.length ? 'WHERE ' + conds.join(' AND ') : ''} ${siteOrder()} LIMIT ? OFFSET ?`;
  const { results } = await db.prepare(sql).bind(...a, limit + 1, hasCursor ? 0 : offset).all();
  const more = results.length > limit, rows = results.slice(0, limit);
  const next_cursor = more ? encodeCursor(SORT_KEYS.map((_, i) => rows.at(-1)[`_k${i}`] ?? null)) : null;
  const out = { items: rows.map(shape), next_cursor };
  if (!hasCursor) out.total = (await db.prepare(`SELECT COUNT(*) AS total FROM sites s ${where.length ? 'WHERE ' + where.join(' AND ') : ''}`).bind(...args).first())?.total ?? 0;
  return out;
}

// ピックアップ用: 範囲内からランダムに n 件。ピックアップは「発見」の表示なので、台帳などの一括投入（bulk_import=1）は除く。
export async function randomSites(db, cc, n = 3) {
  const { results } = await db.prepare(`SELECT ${COLUMNS} FROM sites s WHERE s.bulk_import = 0 AND (?1 IS NULL OR s.country_code = ?1) ORDER BY RANDOM() LIMIT ?2`).bind(cc, n).all();
  return results.map(shape);
}
