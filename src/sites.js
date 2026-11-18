// サイト一覧の取得（API と SSR が共有する単一の実装）。
// ★ 並び順の定義はこのファイルの SORT_KEYS だけ。ORDER BY・カーソルのキー列・「次のページ」の比較条件・インデックス列はここから生成する。
//   並びのキーを足す/変えるとき（例: 先頭の `bulk_import`。#71 で足した）:
//   1. SORT_KEYS に { expr, dir } を足す（先頭ほど優先。末尾の s.id は必ず最後に残す）。
//   2. sortIndexSql() が返す CREATE INDEX を新しい migration に書く（scripts/check-api-headers.mjs が最新の migration と一致を検査する）。
//   3. 追加した列が画面に必要なら COLUMNS にも足す（並びだけに使う列は足さなくてよい）。
//   4. docs/design/ui.md の「既定の並び」を更新する。カーソルは配列なので形式の変更は不要。
export const PAGE_SIZE = 40, MAX_LIMIT = 100;
// 集計表（migration 0022: category_counts・scope_counts）の「全世界」の scope 値。国は country_code そのもの。
export const ALL_SCOPE = 'all';
// 絞り込み（q・tag・直下以外のカテゴリ）の総件数の上限（#126 第2段）。これを超えるときは数えず total = TOTAL_CAP・total_capped = true で返し、画面は「1,000+」と出す。
// 全件を数えると読み取りがヒット件数に比例して増えるため（docs/design/architecture.md）。変えるときは app.js の表示（formatCap）と docs/design/ui.md も確認する。
export const TOTAL_CAP = 1000;
// タグ絞り込みの計画の切替点: タグ 1 つの該当サイト数がこれ以下なら「タグの索引から引く」計画（一覧も件数もタグの索引を読むので該当数に比例）、超えたら「並びの索引を走査して EXISTS で確かめる」計画（#126）。
// 総件数の上限と同じ値にしてある: 上限以下なら件数をタグの索引で正確に数えられ、超えれば総件数は上限付きで返すので数えなくてよい（単独のタグのとき）。
export const TAG_DRIVE_MAX = TOTAL_CAP;
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
// 名前・日英の紹介文・URL の後ろにタグ（日本語名 英語名）を空白区切りで続ける（migration 0020 の暫定値と同じ並び。タグは label_ja 昇順の BINARY 順で渡す呼び出し側の責任で、ここでは並べ替えない）。s.tags は [{label_ja,label_en}]（省略可）。
export const searchText = s => normalizeSearch(`${s.name} ${s.description_ja} ${s.description_en ?? ''} ${s.url}${(s.tags ?? []).map(t => ` ${t.label_ja} ${t.label_en ?? ''}`).join('')}`);
// instr で比較するので、LIKE のメタ文字のエスケープも長さの制限（D1 は LIKE のパターンが約50バイトを超えると too complex）も要らない。
export const cleanQuery = raw => { const q = [...normalizeSearch(raw).trim()].slice(0, 100).join(''); return q || null; }; // コードポイント単位で切る

// タグ絞り込み（?tag=、複数は AND）。値はタグの日本語名か英語名（sites の site_tags.label_ja / label_en。画面は日本語名を URL に載せる）。
// 空文字は無視、重複は 1 つにまとめる。MAX_TAGS 個を超える・1 つが 100 文字を超える場合は null（呼び出し側が 400）。
export const MAX_TAGS = 5;
export function cleanTags(raw) {
  const out = [];
  for (const v of raw ?? []) { const t = String(v ?? '').trim(); if (!t) continue; if ([...t].length > 100) return null; if (!out.includes(t)) out.push(t); }
  return out.length > MAX_TAGS ? null : out;
}

// 「cursor の次」を、並びの順に連結できる範囲条件の列にする（#126 第2段）。キーを辞書式に比較する（前のキーが等しく、このキーが後ろ）。NULL は dir に合わせて扱う。
// 1 つの `A OR B OR C` にすると SQLite は並びの索引を先頭から走査してしまう（深いページほど読み取りが増える。実測: 9,000 件目の次で 3.4 万行）。
// そこで項ごとに別の問い合わせにする。各項は「前のキーが等しい」+「このキーが後ろ」の等値と範囲だけなので索引を直接引ける（SEARCH）。
// 項は並びの順に並べてある（最も深いキーの項が先、最初のキーの項が最後。DESC のキーでは NULL の項が「値が小さい」項の後）ので、前から順に取って連結すれば並びと同じになる。
function cursorTerms(vals) {
  const terms = [];
  for (let i = SORT_KEYS.length - 1; i >= 0; i--) {
    const k = SORT_KEYS[i], eq = [], eqArgs = [];
    for (let j = 0; j < i; j++) { if (vals[j] === null) eq.push(`${SORT_KEYS[j].expr} IS NULL`); else { eq.push(`${SORT_KEYS[j].expr} = ?`); eqArgs.push(vals[j]); } }
    const push = (cond, args = []) => terms.push({ sql: `(${[...eq, cond].join(' AND ')})`, args: [...eqArgs, ...args] });
    if (k.dir === 'ASC') { if (vals[i] === null) push(`${k.expr} IS NOT NULL`); else push(`${k.expr} > ?`, [vals[i]]); } // ASC: NULL が先頭
    else if (vals[i] !== null) { push(`${k.expr} < ?`, [vals[i]]); push(`${k.expr} IS NULL`); } // DESC: NULL が末尾。値の小さい行の後に NULL の行が続く
  }
  return terms;
}

// ---- 検索計画（#126 第2段）----
// SQLite の計画器は件数を知らないので、重い経路は件数に応じてアプリが形を選ぶ。結果と並びはどの形でも同じ（読み取り行数だけが変わる）。
//   カテゴリ: 小さい（直下の所属サイトが少ない）→ `IN (所属サイト)` で所属から引いて並べ替える / 大きい → 並びの索引を走査して `EXISTS` で確かめ、1 ページ分で止まる。
//   タグ: 稀（該当サイトが TAG_DRIVE_MAX 以下）→ タグの索引（0023・0024）から引く / よく使われる → 並びの索引を走査して `EXISTS`。
// 費用の見積り（10 万件の実測から。Issue #126）: IN 形 ≒ 所属数 × 7 行、EXISTS 形 ≒ 41 件が見つかるまでの走査 = 41 × 母数 ÷ 該当数 × 3 行。
const CATEGORY_IN_COST = 7, CATEGORY_EXISTS_COST = 3;
export async function planQuery(db, { cc = null, category = null, tags = [] }) {
  const plan = { categoryMode: 'in', categoryLeafCount: null, driveTag: null, tagsOverCap: false };
  const jobs = [];
  if (category) jobs.push((async () => {
    const scope = cc ?? ALL_SCOPE;
    const r = await db.prepare(`SELECT (SELECT n FROM category_counts WHERE category_id = ?1 AND scope = 'all') AS n_all, (SELECT n FROM category_counts WHERE category_id = ?1 AND scope = ?2) AS n_scope, (SELECT n FROM scope_counts WHERE scope = ?2) AS n_base, EXISTS (SELECT 1 FROM categories WHERE parent_id = ?1) AS has_child`).bind(category, scope).first();
    const nAll = r?.n_all ?? 0, nScope = r?.n_scope ?? 0, base = r?.n_base ?? 0;
    // 葉カテゴリの category_counts は「直下の所属サイト数」と同じ（子孫が無いので）。総件数はこれをそのまま使える。
    if (r && !r.has_child) plan.categoryLeafCount = nScope;
    const costIn = nAll * CATEGORY_IN_COST, costExists = nScope > 0 ? CATEGORY_EXISTS_COST * 41 * Math.max(base, 1) / nScope : Infinity;
    plan.categoryMode = costExists < costIn ? 'exists' : 'in';
  })());
  if (tags.length) jobs.push((async () => {
    // タグごとに該当サイト数を TAG_DRIVE_MAX + 1 まで数え（索引 0023・0024 で最大 TAG_DRIVE_MAX + 1 行）、一番稀なものを索引から引く（超えたタグは対象外）。
    const counts = await Promise.all(tags.map(t => db.prepare(`SELECT COUNT(*) AS n FROM (SELECT site_id FROM site_tags WHERE label_ja = ?1 UNION SELECT site_id FROM site_tags WHERE label_en = ?1 LIMIT ?2)`).bind(t, TAG_DRIVE_MAX + 1).first()));
    let best = null;
    tags.forEach((t, i) => { const n = counts[i]?.n ?? 0; if (n <= TAG_DRIVE_MAX && (best === null || n < best.n)) best = { t, n }; });
    plan.driveTag = best?.t ?? null;
    plan.tagsOverCap = tags.length === 1 && best === null; // 単独のタグが上限を超える（他の絞り込みが無ければ総件数は上限付きと分かる）
  })());
  await Promise.all(jobs);
  return plan;
}

function filters({ cc, category, q, tags }, plan = {}) {
  const where = [], args = [];
  if (cc) { where.push('s.country_code = ?'); args.push(cc); }
  if (category) {
    if (plan.categoryMode === 'exists') where.push('EXISTS (SELECT 1 FROM site_categories sc WHERE sc.category_id = ? AND sc.site_id = s.id)');
    else where.push('s.id IN (SELECT site_id FROM site_categories WHERE category_id = ?)');
    args.push(category);
  }
  if (q) { where.push('instr(s.search_text, ?) > 0'); args.push(q); } // q は cleanQuery 済み（search_text も正規化済みなので大小・全角半角はそろっている）
  for (const t of tags ?? []) { // 複数タグは AND（タグごとに 1 条件）
    if (t === plan.driveTag) { where.push('s.id IN (SELECT site_id FROM site_tags WHERE label_ja = ? UNION SELECT site_id FROM site_tags WHERE label_en = ?)'); args.push(t, t); }
    else { where.push('EXISTS (SELECT 1 FROM site_tags t WHERE t.site_id = s.id AND (t.label_ja = ? OR t.label_en = ?))'); args.push(t, t); }
  }
  return { where, args };
}

// 画面に出す列。bulk_import は並びのほか NEW タグの抑止に画面が使うので含める。並びだけに使う列は SORT_KEYS で足りるのでここには要らない。
const COLUMNS = `s.id, s.name, s.url, s.description_ja, s.description_en, s.favicon_path, s.registered_at, s.bulk_import, s.source_updated_at, s.last_crawled_at, s.country_code, s.region_code, (SELECT json_group_array(json_object('ja',t.label_ja,'en',t.label_en)) FROM site_tags t WHERE t.site_id=s.id) AS tags_json, (SELECT json_group_array(c.category_id) FROM site_categories c WHERE c.site_id=s.id) AS category_ids_json`;
const shape = ({ category_ids_json, ...r }) => { for (const k of Object.keys(r)) if (k.startsWith('_k')) delete r[k]; return { ...r, category_ids: JSON.parse(category_ids_json || '[]') }; };

// 1 ページ分。cursor は decodeCursor の結果、offset は ?page=N（SSR・直リンクの開始位置）用でカーソルが無いときだけ使う。
// total は先頭ページ（カーソルなし）のときだけ入れる。q は cleanQuery 済み、tags は cleanTags 済みであること。
// total の出し方（読み取りが件数に比例しないように。#126 第2段）:
//   1. 末尾まで届いた（次ページなし）なら、読んだ行数から正確に分かる（追加の読み取りなし。0 ヒットも 1 回の走査で済む）
//   2. 範囲（国）だけの絞り込みは集計表 scope_counts、直下が葉のカテゴリだけの絞り込みは category_counts（どちらも正確）
//   3. それ以外（q・tag・葉でないカテゴリ）は TOTAL_CAP + 1 件で数えるのをやめる（単独のタグが上限を超えると分かっているときは数えもしない）。超えたら total = TOTAL_CAP、total_capped = true（画面は「1,000+」）
export async function querySites(db, { cc = null, category = null, q = null, tags = [], limit = PAGE_SIZE, cursor = null, offset = 0 }) {
  const plan = await planQuery(db, { cc, category, tags });
  const { where, args } = filters({ cc, category, q, tags }, plan);
  const hasCursor = cursor && !cursor.none, start = hasCursor ? 0 : offset;
  const run = async (extra, extraArgs, lim, off) => {
    const conds = [...where, ...extra];
    return (await db.prepare(`SELECT ${COLUMNS}, ${keyCols()} FROM sites s ${conds.length ? 'WHERE ' + conds.join(' AND ') : ''} ${siteOrder()} LIMIT ? OFFSET ?`).bind(...args, ...extraArgs, lim, off).all()).results;
  };
  let results;
  if (hasCursor) { results = []; for (const t of cursorTerms(cursor.vals)) { if (results.length > limit) break; results.push(...await run([t.sql], t.args, limit + 1 - results.length, 0)); } } // 項ごとに索引を引いて前から連結（cursorTerms）
  else results = await run([], [], limit + 1, start);
  const more = results.length > limit, rows = results.slice(0, limit);
  const next_cursor = more ? encodeCursor(SORT_KEYS.map((_, i) => rows.at(-1)[`_k${i}`] ?? null)) : null;
  const out = { items: rows.map(shape), next_cursor };
  if (!hasCursor) {
    const plain = !category && !q && !tags?.length;
    if (!more && (start === 0 || rows.length > 0)) { out.total = start + rows.length; out.total_capped = false; }
    else if (plain) { out.total = (await db.prepare(`SELECT COALESCE((SELECT n FROM scope_counts WHERE scope = ?), 0) AS total`).bind(cc ?? ALL_SCOPE).first())?.total ?? 0; out.total_capped = false; }
    else if (plan.categoryLeafCount !== null && !q && !tags?.length) { out.total = plan.categoryLeafCount; out.total_capped = false; }
    else if (plan.tagsOverCap && !q && !category && !cc) { out.total = TOTAL_CAP; out.total_capped = true; } // 単独のタグが上限超え: 数えない
    else {
      const n = (await db.prepare(`SELECT COUNT(*) AS n FROM (SELECT 1 FROM sites s ${where.length ? 'WHERE ' + where.join(' AND ') : ''} LIMIT ${TOTAL_CAP + 1})`).bind(...args).first())?.n ?? 0;
      out.total = Math.min(n, TOTAL_CAP); out.total_capped = n > TOTAL_CAP;
    }
  }
  return out;
}

// ピックアップ用: 範囲内からランダムに n 件。ピックアップは「発見」の表示なので、台帳などの一括投入（bulk_import=1）は除く。
// 読み取りが件数に比例しないよう、`ORDER BY RANDOM()`（全件を読む）をやめ、rowid の乱数位置から該当する最初の 1 件を n 回引く（#126 第2段）。
// 厳密な一様抽選ではない（欠番や、該当しない行が続いた直後の行が当たりやすい）。ピックアップは目安の「発見」なので許容する。位置が重なって n 件に足りないときは先頭から補う。
export async function randomSites(db, cc, n = 3) {
  if (cc && !((await db.prepare(`SELECT n FROM scope_counts WHERE scope = ?`).bind(cc).first())?.n > 0)) return [];
  const max = (await db.prepare(`SELECT MAX(rowid) AS m FROM sites`).bind().first())?.m ?? 0;
  if (!max) return [];
  // `+s.bulk_import` は、並びの索引（bulk_import 先頭）を選ばせず rowid の順で走査させるため。
  const from = `FROM sites s WHERE s.rowid >= ?1 AND +s.bulk_import = 0 AND (?2 IS NULL OR s.country_code = ?2)`;
  const found = await Promise.all(Array.from({ length: n }, () => db.prepare(`SELECT ${COLUMNS} ${from} ORDER BY s.rowid LIMIT 1`).bind(Math.floor(Math.random() * max) + 1, cc).all()));
  const rows = [...new Map(found.flatMap(r => r.results).map(r => [r.id, r])).values()];
  if (rows.length < n) {
    const have = JSON.stringify(rows.map(r => r.id));
    const { results } = await db.prepare(`SELECT ${COLUMNS} ${from} AND s.id NOT IN (SELECT value FROM json_each(?3)) ORDER BY s.rowid LIMIT ?4`).bind(0, cc, have, n - rows.length).all();
    rows.push(...results);
  }
  return rows.sort(() => Math.random() - 0.5).map(shape);
}
