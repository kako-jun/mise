// サイト一覧の取得（API と SSR が共有する単一の実装）。並びは「新しい順 + id」の安定ソートで、カーソルは (並びのキー, id) の複合。
// 並びの式は migration 0018 の式インデックスと同じ文字列にする（変えるとインデックスが効かなくなる）。
export const PAGE_SIZE = 40, MAX_LIMIT = 100;
export const SORT_KEY = `datetime(COALESCE(s.source_updated_at, s.registered_at))`;
// 新しい順（日時が読めない/NULL は DESC で自然に末尾）、同値は id 昇順。
export const SITE_ORDER = `ORDER BY ${SORT_KEY} DESC, s.id`;
const KEY_RE = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/;

export const parseLimit = raw => { const n = Number.parseInt(raw ?? '', 10); return Number.isFinite(n) && n >= 1 ? Math.min(n, MAX_LIMIT) : PAGE_SIZE; };
export const parsePage = raw => { const n = /^\d{1,6}$/.test(raw ?? '') ? Number(raw) : 1; return n >= 1 ? n : 1; };
export const encodeCursor = row => `${row.sort_key ?? ''}|${row.id}`;
// 不正なカーソルは null（呼び出し側が 400 にする）。空文字は「カーソルなし」。
export function decodeCursor(raw) {
  if (!raw) return { none: true };
  const i = raw.indexOf('|'), key = raw.slice(0, i), id = raw.slice(i + 1);
  return i >= 0 && id && (key === '' || KEY_RE.test(key)) ? { key: key || null, id } : null;
}
const likeEscape = s => s.replace(/[\\%_]/g, ch => '\\' + ch);

function filters({ cc, category, q }) {
  const where = [], args = [];
  if (cc) { where.push('s.country_code = ?'); args.push(cc); }
  if (category) { where.push('s.id IN (SELECT site_id FROM site_categories WHERE category_id = ?)'); args.push(category); }
  if (q) {
    const p = `%${likeEscape(q)}%`;
    where.push(`(s.name LIKE ? ESCAPE '\\' OR s.description_ja LIKE ? ESCAPE '\\' OR s.description_en LIKE ? ESCAPE '\\' OR s.url LIKE ? ESCAPE '\\')`);
    args.push(p, p, p, p);
  }
  return { where, args };
}

const COLUMNS = `s.id, s.name, s.url, s.description_ja, s.description_en, s.favicon_path, s.registered_at, s.source_updated_at, s.last_crawled_at, s.country_code, s.region_code, ${SORT_KEY} AS sort_key, (SELECT json_group_array(json_object('ja',t.label_ja,'en',t.label_en)) FROM site_tags t WHERE t.site_id=s.id) AS tags_json, (SELECT json_group_array(c.category_id) FROM site_categories c WHERE c.site_id=s.id) AS category_ids_json`;

// 1 ページ分。cursor は decodeCursor の結果、offset は ?page=N（SSR・直リンクの開始位置）用でカーソルが無いときだけ使う。
// total は先頭ページ（カーソルなし）のときだけ数える。
export async function querySites(db, { cc = null, category = null, q = null, limit = PAGE_SIZE, cursor = null, offset = 0 }) {
  const { where, args } = filters({ cc, category, q });
  const conds = [...where], a = [...args];
  if (cursor && !cursor.none) {
    if (cursor.key) { conds.push(`(${SORT_KEY} < ? OR (${SORT_KEY} = ? AND s.id > ?) OR ${SORT_KEY} IS NULL)`); a.push(cursor.key, cursor.key, cursor.id); }
    else { conds.push(`(${SORT_KEY} IS NULL AND s.id > ?)`); a.push(cursor.id); }
  }
  const sql = `SELECT ${COLUMNS} FROM sites s ${conds.length ? 'WHERE ' + conds.join(' AND ') : ''} ${SITE_ORDER} LIMIT ? OFFSET ?`;
  const { results } = await db.prepare(sql).bind(...a, limit + 1, cursor && !cursor.none ? 0 : offset).all();
  const more = results.length > limit, rows = results.slice(0, limit);
  const items = rows.map(({ sort_key, category_ids_json, ...r }) => ({ ...r, category_ids: JSON.parse(category_ids_json || '[]') }));
  let total;
  if (!cursor || cursor.none) {
    const t = await db.prepare(`SELECT COUNT(*) AS total FROM sites s ${where.length ? 'WHERE ' + where.join(' AND ') : ''}`).bind(...args).first();
    total = t?.total ?? 0;
  }
  return { items, next_cursor: more ? encodeCursor(rows.at(-1)) : null, ...(total === undefined ? {} : { total }) };
}

// ピックアップ用: 範囲内からランダムに n 件。
export async function randomSites(db, cc, n = 3) {
  const { results } = await db.prepare(`SELECT ${COLUMNS} FROM sites s WHERE (?1 IS NULL OR s.country_code = ?1) ORDER BY RANDOM() LIMIT ?2`).bind(cc, n).all();
  return results.map(({ sort_key, category_ids_json, ...r }) => ({ ...r, category_ids: JSON.parse(category_ids_json || '[]') }));
}
