// サイト一覧の取得（API と SSR が共有する単一の実装）。
// ★ 並び順の定義はこのファイルの SORT_KEYS だけ。ORDER BY・カーソルのキー列・「次のページ」の比較条件・インデックス列はここから生成する。
//   並びのキーを足す/変えるとき（例: 先頭の `bulk_import`。#71 で足した）:
//   1. SORT_KEYS に { expr, dir } を足す（先頭ほど優先。末尾の s.id は必ず最後に残す）。
//   2. sortIndexSql() と regionSortIndexSql() が返す CREATE INDEX を新しい migration に書く（scripts/check-api-headers.mjs が最新の migration と一致を検査する）。
//   3. 追加した列が画面に必要なら COLUMNS にも足す（並びだけに使う列は足さなくてよい）。
//   4. docs/design/ui.md の「既定の並び」を更新する。カーソルは配列なので形式の変更は不要。
import { faviconUrl, thumbUrl } from './media.js';
import '../public/vertical-link.js';
export const { RECENT_CATEGORY, RECENT_DAYS, PAGE_SIZE, parsePage } = globalThis.MiseFeedEntry;
export const MAX_LIMIT = 100;
// 集計表（migration 0022: category_counts・scope_counts）の「全世界」の scope 値。国は country_code そのもの、区画（都道府県・州）は region_code そのもの（0026。例 JP-13）。
export const ALL_SCOPE = 'all';
// 範囲の集計キー: 区画 > 国 > 全世界。cc は国、region は区画（どちらも集計表の scope 値）。
export const scopeKeyOf = ({ cc = null, region = null } = {}) => region ?? cc ?? ALL_SCOPE;
// 絞り込み（q・tag・直下以外のカテゴリ）の総件数の上限（#126 第2段）。これを超えるときは数えず total = TOTAL_CAP・total_capped = true で返し、画面は「1,000+」と出す。
// 全件を数えると読み取りがヒット件数に比例して増えるため（docs/design/architecture.md）。変えるときは app.js の表示（formatCap）と docs/design/ui.md も確認する。
export const TOTAL_CAP = 1000;
// タグ絞り込みの計画の切替点: タグ 1 つの該当サイト数がこれ以下なら「タグの索引から引く」計画（一覧も件数もタグの索引を読むので該当数に比例）、超えたら「並びの索引を走査して EXISTS で確かめる」計画（#126）。
// 総件数の上限と同じ値にしてある: 上限以下なら件数をタグの索引で正確に数えられ、超えれば総件数は上限付きで返すので数えなくてよい（単独のタグのとき）。
export const TAG_DRIVE_MAX = TOTAL_CAP;
// 続きのページ（カーソルあり）で、稀なタグを索引（IN）から引くのは、該当サイト数がこれ以下のとき。
export const TAG_IN_MAX = 300;
// dir は SQLite の NULL の並びに合わせる: ASC では NULL が先頭、DESC では NULL が末尾。
export const SORT_KEYS = [
  // 台帳などからの一括投入（bulk_import=1。官公庁の名簿など）は後ろ。通常のサイト（0）が先（NULL なし、DEFAULT 0）
  { expr: `s.bulk_import`, dir: 'ASC', type: 'number' },
  // 新しい順（サイト自身の更新日、不明なら登録日。日時が読めない/NULL は DESC で末尾）
  { expr: `datetime(COALESCE(s.source_updated_at, s.registered_at))`, dir: 'DESC', type: 'string' },
  { expr: 's.id', dir: 'ASC', type: 'string' }, // 同値の安定化（必ず最後）
  // type はカーソルの値の型（'string' | 'number'）。不一致は 400。
];
const RECENT_SORT_KEYS = [
  { expr: 'datetime(s.registered_at)', dir: 'DESC', type: 'string' },
  { expr: 's.id', dir: 'ASC', type: 'string' },
];
export const siteOrder = (keys = SORT_KEYS) => `ORDER BY ${keys.map(k => `${k.expr} ${k.dir}`).join(', ')}`;
// migration に書くインデックス（テーブル別名 `s.` は外す）。名前に定義のハッシュを含めるので、SORT_KEYS を変えると名前も変わり、
// 旧インデックスと衝突（IF NOT EXISTS で作られずに残る）しない。新しい migration で新名の CREATE と旧名の DROP INDEX を書く。
const indexCols = () => SORT_KEYS.map(k => `${k.expr.replace(/\bs\./g, '')} ${k.dir}`.replace(/ ASC$/, '')).join(', ');
const fnv = str => { let h = 0x811c9dc5; for (const ch of str) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; } return h.toString(16).padStart(8, '0'); };
export const sortIndexName = () => `idx_sites_sort_${fnv(indexCols())}`;
export const sortIndexSql = () => `CREATE INDEX IF NOT EXISTS ${sortIndexName()} ON sites (${indexCols()});`;
// 区画（region_code）で絞った一覧用の部分索引（migration 0026。区画 NULL の行は載せない）。並びの列は上と同じなので、SORT_KEYS を変えたらこれも新しい migration で作り直す。
export const regionSortIndexSql = () => `CREATE INDEX IF NOT EXISTS idx_sites_region_sort ON sites (region_code, ${indexCols()}) WHERE region_code IS NOT NULL;`;
const keyCols = keys => keys.map((k, i) => `${k.expr} AS _k${i}`).join(", ");

// ---- 入力の解釈（不正は null → 呼び出し側が 400/404） ----
export const parseLimit = raw => { if (raw == null || raw === '') return PAGE_SIZE; if (!/^-?\d{1,9}$/.test(raw)) return null; const n = Number(raw); return n >= 1 ? Math.min(n, MAX_LIMIT) : null; }; // 上限超は MAX_LIMIT に丸める
// カーソルは全キー値の配列（id を含む）を JSON → base64url にしたもの。キーが増えても形式は変わらない。
// 検索の関連度の段（#301。下の RELEVANCE_MAX）を使う検索では、先頭に段の値（rank。0〜RANK_REST の整数）を足した 1 つ長い配列になり、{ vals, rel: true } を返す。
// 段の値の無いカーソル（段ができる前に発行したもの・段を使わない検索のもの）は今の並びのまま続ける（400 にしない。並びが途中で変わらないので重複・欠落も出ない）。
export const encodeCursor = vals => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(vals)))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
export function decodeCursor(raw, recent = false) {
  if (!raw) return { none: true };
  try {
    const b = atob(raw.replace(/-/g, '+').replace(/_/g, '/')), vals = JSON.parse(new TextDecoder().decode(Uint8Array.from(b, c => c.charCodeAt(0))));
    const keys = recent ? RECENT_SORT_KEYS : SORT_KEYS, rel = Array.isArray(vals) && vals.length === keys.length + 1;
    if (rel && !(Number.isInteger(vals[0]) && vals[0] >= 0 && vals[0] <= REL_MAX)) return null;
    const rest = rel ? vals.slice(1) : vals;
    const ok = Array.isArray(vals) && rest.length === keys.length && rest.every((v, i) => v === null || (typeof v === keys[i].type && (typeof v === 'number' ? Number.isFinite(v) : v.length <= 200)));
    return ok && vals.at(-1) !== null ? (rel ? { vals, rel: true } : { vals }) : null;
  } catch (_) { return null; }
}
// 全角半角・大文字小文字をそろえる（検索語と sites.search_text の両方に同じ処理を使う）
export const normalizeSearch = s => String(s ?? '').normalize('NFKC').toLowerCase();
// 名前・日英の紹介文・URL の後ろにタグ（日本語名 英語名）を空白区切りで続ける（migration 0020 の暫定値と同じ並び。タグは label_ja 昇順の BINARY 順で渡す呼び出し側の責任で、ここでは並べ替えない）。s.tags は [{label_ja,label_en}]（省略可）。
export const searchText = s => normalizeSearch(`${s.name}${s.name_ja ? ` ${s.name_ja}` : ''}${s.name_en ? ` ${s.name_en}` : ''} ${s.description_ja} ${s.description_en ?? ''} ${s.url}${(s.tags ?? []).map(t => ` ${t.label_ja} ${t.label_en ?? ''}`).join('')}`);
// instr で比較するので、LIKE のメタ文字のエスケープも長さの制限（D1 は LIKE のパターンが約50バイトを超えると too complex）も要らない。
export const cleanQuery = raw => { const q = [...normalizeSearch(raw).trim()].slice(0, 100).join(''); return q || null; }; // コードポイント単位で切る
const queryTerms = q => q.trim().split(/\s+/u).filter(Boolean);

// タグ絞り込み（?tag=、複数は AND）。値はタグの日本語名か英語名（sites の site_tags.label_ja / label_en。画面は日本語名を URL に載せる）。
// 空文字は無視、重複は 1 つにまとめる。MAX_TAGS 個を超える・1 つが 100 文字を超える場合は null（呼び出し側が 400）。
export const MAX_TAGS = 5;
export function cleanTags(raw) {
  const out = [];
  for (const v of raw ?? []) { const t = String(v ?? '').trim(); if (!t) continue; if ([...t].length > 100) return null; if (!out.includes(t)) out.push(t); }
  return out.length > MAX_TAGS ? null : out;
}

// 「cursor の次」の条件。キーを辞書式に比較する（先頭から: 前のキーが等しく、このキーが後ろ）。NULL は dir に合わせて扱う。
// 1 本の `A OR B OR C` なので、IN（所属サイト・稀なタグ）で引く計画のときに使う（IN で引いて並べ替える計画のまま、cursor の次だけを返せる）。
function afterCursor(vals, keys) {
  const alts = [], args = [];
  keys.forEach((k, i) => {
    const eq = [], a = [];
    for (let j = 0; j < i; j++) { if (vals[j] === null) eq.push(`${keys[j].expr} IS NULL`); else { eq.push(`${keys[j].expr} = ?`); a.push(vals[j]); } }
    let after;
    if (k.dir === 'ASC') { if (vals[i] === null) after = `${k.expr} IS NOT NULL`; else { after = `${k.expr} > ?`; a.push(vals[i]); } } // ASC: NULL が先頭
    else { if (vals[i] === null) after = null; else { after = `(${k.expr} < ? OR ${k.expr} IS NULL)`; a.push(vals[i]); } } // DESC: NULL が末尾
    if (after) { alts.push(`(${[...eq, after].join(' AND ')})`); args.push(...a); }
  });
  return { sql: `(${alts.join(' OR ')})`, args };
}

// 「cursor の次」を、並びの順に連結できる範囲条件の列にする（#126 第2段）。afterCursor と同じ条件を項ごとに分けたもの。
// 1 つの `A OR B OR C` にすると SQLite は並びの索引を先頭から走査してしまう（深いページほど読み取りが増える。実測: 9,000 件目の次で 3.4 万行）。
// そこで項ごとに別の問い合わせにする。各項は「前のキーが等しい」+「このキーが後ろ」の等値と範囲だけなので索引を直接引ける（SEARCH）。
// 項は並びの順に並べてある（最も深いキーの項が先、最初のキーの項が最後。DESC のキーでは NULL の項が「値が小さい」項の後）ので、前から順に取って連結すれば並びと同じになる。
// 各項の order は、その項で「まだ決まっていないキー」だけの並び（前のキーは等しいので並べ替えは要らない）。全キーで並べると、同じ日時が大量に続く塊で
// 索引の並びと一致せず一時 B-tree（USE TEMP B-TREE FOR LAST 2 TERMS）になり、同値の行を毎回全部読む（本番相当データで main の最大 14 倍）。
function cursorTerms(vals, keys) {
  const terms = [], order = from => siteOrder(keys.slice(from));
  for (let i = keys.length - 1; i >= 0; i--) {
    const k = keys[i], eq = [], eqArgs = [];
    for (let j = 0; j < i; j++) { if (vals[j] === null) eq.push(`${keys[j].expr} IS NULL`); else { eq.push(`${keys[j].expr} = ?`); eqArgs.push(vals[j]); } }
    const push = (cond, ord, args = []) => terms.push({ sql: `(${[...eq, cond].join(' AND ')})`, args: [...eqArgs, ...args], order: ord });
    if (k.dir === 'ASC') { if (vals[i] === null) push(`${k.expr} IS NOT NULL`, order(i)); else push(`${k.expr} > ?`, order(i), [vals[i]]); } // ASC: NULL が先頭
    else if (vals[i] !== null) { push(`${k.expr} < ?`, order(i), [vals[i]]); push(`${k.expr} IS NULL`, order(i + 1)); } // DESC: NULL が末尾。値の小さい行の後に NULL の行が続く（NULL の項はこのキーが全部 NULL なので次のキーから）
  }
  return terms;
}

// ---- 全文索引（#126 第3段。migration 0027 の sites_fts = search_text の FTS5 trigram）----
// 3 文字以上の検索語は、まず索引の該当数を上限付きで数え、TOTAL_CAP 以下なら一覧も件数も索引（MATCH）から引く（読み取りは該当数に比例）。
// 超えるときは現行の「並びの索引を走査して instr で確かめる」形のまま（該当が多い語は 41 件で止まるこの形の方が安い）。1〜2 文字は trigram が 0 件を返す（エラーにならない）ので必ず instr。
// 索引が未構築（fts_state.ready = 0）・表が無い（ロールバック後）なら instr の経路に落ちる。FTS は大小の畳み込みが instr と少し違う（σ と ς など）ので、索引で絞った行にも instr を重ねて結果を instr と完全に一致させる。
export const FTS_MIN_CHARS = 3;
// 絞り込み（範囲・カテゴリ・タグ）を併用するときは、索引の該当数をここまで数える。超えた分は索引を使わない（総件数は現行どおり instr で数える）。ほぼ全件に出る語で読み取りが増えないよう小さめにしてある。
export const FTS_EXTRA_PROBE = 3000;
// MATCH の引数は FTS5 の文字列リテラル（" で囲み、中の " は二重にする）。trigram では空白も含めて部分文字列の一致になる。
export const ftsQuery = q => '"' + q.replace(/"/g, '""') + '"';
const ftsTerms = q => queryTerms(q).filter(t => [...t].length >= FTS_MIN_CHARS).map(ftsQuery).join(' AND ');

// ---- 検索の関連度（#301。migration 0041 の site_search_keys）----
// 検索語があるときだけ、検索語と「完全に一致する」サイトを段として先に出す（完全一致の段 > その他。同じ段の中は今の並び = 段は bulk_import より前）。
// 完全一致の段の中の順（rank。小さいほど上）: 0 ホスト名（www. を除く）が一致・トップ / 1 ホスト名が一致・パスあり / 2・3 ドメインの主要部（x.com の x）が一致・トップ / パスあり / 4 サイト名（name・name_ja・name_en）が一致。
// 鍵は sites のトリガが作る（定義は 0041 の先頭のコメント）。検索語との比較は「鍵 = 検索語（cleanQuery 済み = NFKC + 小文字）」の等値（URL の形の語は下の relevanceKeys の候補も）で、索引（主キー key, site_id）で引く。
// 前方一致の段（完全一致の段の次。githu → github.com、yaho → Yahoo! JAPAN）: 候補の最後の語（URL の形なら除いた語）で始まり、それより長い鍵を持つサイト。
//   段の中の順は日付でなく「語に近い順」: 一致した鍵の長さの短い順 → rank（ドメインのトップ > パスあり > 名前）→ 今の並び（score = 鍵の長さ × 8 + rank。サイトごとに最小）。
//   段に入るのは、前方一致の鍵を（鍵の長さ, 鍵）の順に並べた先頭 PREFIX_MAX 行に入る鍵を持つサイトだけ（上限で読み取りを止めるため。鍵が少ない語では全部）。
//   よく打たれる短い語（ama・g）でも短い鍵＝本命（aman・amazon）から入るので、上限で段をまるごと諦めない。超えた分のサイトは「その他」に今の並びで出る。
//   読み方: まず主キーの範囲で PREFIX_MAX + 1 行まで読み、超えたら式インデックス（0042。長さ → 鍵）を長さの短い順に引いて PREFIX_MAX 行で止まる。
//   0042 が無く PREFIX_MAX を超える語は前方一致の段を作らない（完全一致の段はそのまま）。段の集合は計画（先頭ページ）で決めて覚え、続きのページも同じ集合を使う。
//   段の語は prefixWord（URL の形の語は scheme・www. などを除いた語）。段の語が検索語と同じなら、鍵がその語で始まるサイトは部分一致にも入る。
//   違う（URL の形の語）ときは完全一致の段と同じく loose の扱い（段を部分一致で絞らず、部分一致の外の分を総件数に足す。下の relevanceKeys）。
// 部分一致（前方以外）の段は作らない: 段の中を語に近い順に並べる鍵が無く、該当を全部読む必要があるため。
// 完全一致が RELEVANCE_MAX 件を超える語（ありふれた名前）は段を使わず今の並び（段の中の並べ替えが該当数に比例するので上限を置く。100 件も同名なら「その 1 件を探す」検索ではない）。
// 段のあとの「その他」は、今の並び・今の計画のまま、鍵が一致したサイトだけを相関の `NOT EXISTS`（主キーの点引き。ほかの条件を通った行だけを確かめ、一致しなければ読み取りは 0 行）で除く。
//   `s.id NOT IN (SELECT …)` は走査する行ごとに一時表を引くぶん読み取りに数えられ、ローカル D1 で走査の読み取りがほぼ倍になった（1,490 → 2,984 行）。URL の形の語を除き、総件数は段の有無で変わらない（同じ集合）。
// 表が無い（migration 未適用・ロールバック後）ときは段を使わない（今の並び）。
// 鍵を引く候補（relevanceKeys）: 検索語そのものに加え、URL の形で打たれた語（`https://x.com/`・`www.x.com`・`www.yahoo.co.jp`）から scheme://・末尾の /・先頭の www.
//   （残りに . があるときだけ。鍵の www. の扱いと同じ）を除いた語も引く。除いた語でだけ一致するサイトは、検索語の部分一致の集合（その他の段・総件数の母数）に
//   入らないことがある（x.com の search_text に www.x.com は無い）。そのため候補が 2 つのときは、完全一致の段を部分一致で絞らず（範囲・カテゴリ・タグでは絞る）、
//   部分一致に入らない段のサイトの数を総件数に足す（数え方は runQuery の total）。一覧は「完全一致の段 + （部分一致 − 段）」で重複・欠落はない。
//   候補が検索語だけのときは、段も部分一致で絞る（鍵 = 検索語のサイトは部分一致に必ず入るので集合は今と同じ。総件数の読み足しもしない）。
export const relevanceKeys = q => {
  const s = q.replace(/^[a-z][a-z0-9+.-]*:\/\//, '').replace(/\/+$/, ''), t = /^www\..*\./.test(s) ? s.slice(4) : s;
  return t && t !== q ? [q, t] : [q];
};
// 前方一致の段の語: 候補の最後の語から、さらに先頭の www. を除く（打っている途中の www.githu でも github.com に届くように。残りが空なら除かない）。
export const prefixWord = q => { const t = relevanceKeys(q).at(-1); return t.startsWith('www.') && t.length > 4 ? t.slice(4) : t; };
export const RELEVANCE_MAX = 100;
export const RANK_REST = 5; // カーソルの段の値で「その他」を表す（完全一致の段の rank は 0〜4、前方一致の段は PREFIX_BASE + score）
export const PREFIX_MAX = 100, PREFIX_BASE = 6, KEY_LEN_MAX = 200; // 前方一致の段に入れる鍵の行数の上限・カーソルの段の値の起点・段に入る鍵の長さの上限
const prefixKeys = keys => [{ expr: 'p.sc', dir: 'ASC', type: 'number' }, ...keys];
const REL_MAX = PREFIX_BASE + KEY_LEN_MAX * 8 + 4; // カーソルの段の値の上限
// 前方一致の段の集合 { site_id: score }。語 c で始まり c より長い鍵（長さ KEY_LEN_MAX まで）の先頭 PREFIX_MAX 行（長さ → 鍵の順）から作る。作れないとき（0042 なしで上限超え）は null。
export async function prefixSites(db, c) {
  const hi = c + '\u{10FFFF}', L = [...c].length, toMap = rows => { const m = {}; for (const r of rows) if (!(r.site_id in m) || m[r.site_id] > r.sc) m[r.site_id] = r.sc; return m; };
  if (L >= KEY_LEN_MAX) return {};
  const all = (await db.prepare(`SELECT site_id, length(key) * 8 + rank AS sc FROM site_search_keys WHERE key > ?1 AND key < ?2 AND length(key) <= ${KEY_LEN_MAX} LIMIT ${PREFIX_MAX + 1}`).bind(c, hi).all()).results;
  if (all.length <= PREFIX_MAX) return toMap(all);
  const lens = Array.from({ length: KEY_LEN_MAX - L }, (_, i) => L + 1 + i).join(',');
  try { return toMap((await db.prepare(`SELECT site_id, length(key) * 8 + rank AS sc FROM site_search_keys INDEXED BY idx_site_search_keys_len WHERE length(key) IN (${lens}) AND key > ?1 AND key < ?2 ORDER BY length(key), key LIMIT ${PREFIX_MAX}`).bind(c, hi).all()).results); }
  catch (e) { if (/no such index/i.test(String(e?.message))) return null; throw e; }
}
const relKeys = keys => [{ expr: 'k.rank', dir: 'ASC', type: 'number' }, ...keys];
const REL_ERR = /no such table: site_search_keys/i;

// ---- 検索計画（#126 第2段）----
// SQLite の計画器は件数を知らないので、重い経路は件数に応じてアプリが形を選ぶ。結果と並びはどの形でも同じ（読み取り行数だけが変わる）。
//   カテゴリ: 小さい（直下の所属サイトが少ない）→ `IN (所属サイト)` で所属から引いて並べ替える / 大きい → 並びの索引を走査して `EXISTS` で確かめ、1 ページ分で止まる。
//   タグ: 稀（該当サイトが TAG_DRIVE_MAX 以下）→ タグの索引（0023・0024）から引く / よく使われる → 並びの索引を走査して `EXISTS`。
// 費用の見積り（10 万件の実測から。Issue #126）: IN 形 ≒ 所属数 × 7 行、EXISTS 形 ≒ 41 件が見つかるまでの走査 = 41 × 母数 ÷ 該当数 × 3 行。
const CATEGORY_IN_COST = 7, CATEGORY_EXISTS_COST = 3;
// 計画は先頭ページ（カーソルなし）で決めて覚え、続きのページ（カーソルあり）はそれを使う。ページごとにタグの該当数を数え直さない（よく使うタグ 5 個で 1 ページ約 1 万行）。
// 計画は読み取りの形を選ぶだけで結果は変えないので、isolate ごとのメモリに短時間覚えるだけでよい（別の isolate に当たったら計画し直す）。件数が変わって古くなっても結果は同じ。
const PLAN_TTL_MS = 10 * 60 * 1000, PLAN_CACHE_MAX = 500, planCache = new Map();
// q も鍵に含める（q の該当数で索引を使うかが決まる。続きのページが先頭ページと同じ判断になる）。
const planKey = ({ cc, region, category, tags, q }) => JSON.stringify([cc ?? null, region ?? null, category ?? null, tags ?? [], q ?? null]);
async function planFor(db, o, fresh) {
  const key = planKey(o), hit = planCache.get(key);
  if (!fresh && hit && Date.now() - hit.at < PLAN_TTL_MS) {
    // 索引の経路で覚えた計画は、使う前に ready を 1 行で確かめる（索引の作り直し中 = ready 0 の空の索引を MATCH すると 0 件になる・DROP 後は表が無い）。失敗したら計画し直して instr にする。
    if (hit.plan.qMode !== 'fts') return hit.plan;
    try { if ((await db.prepare(`SELECT ready FROM fts_state WHERE name = 'sites_fts'`).first())?.ready === 1) return hit.plan; } catch (_) { /* 表なし → 計画し直し */ }
  }
  const plan = await planQuery(db, o);
  if (planCache.size >= PLAN_CACHE_MAX) planCache.clear();
  planCache.set(key, { plan, at: Date.now() });
  return plan;
}
export async function planQuery(db, { cc = null, region = null, category = null, tags = [], q = null }) {
  // qMode: 'fts'（一覧も件数も索引）/ 'instr'。qOver: 索引の該当数が TOTAL_CAP を超えると分かった。qCountViaFts: 併用ありで総件数だけ索引から数える（該当が TOTAL_CAP 超〜FTS_EXTRA_PROBE 以下）。
  const plan = { categoryMode: 'in', categoryLeafCount: null, categoryAll: null, driveTag: null, tagsOverCap: false, qMode: 'instr', qOver: false, qCountViaFts: false, relevance: false, prefix: null };
  const jobs = [];
  // 関連度の段（#301）: 候補の鍵に一致するサイトを RELEVANCE_MAX + 1 件まで数える（鍵の索引で 候補数 × (RELEVANCE_MAX + 1) 行まで。1 サイトは候補ごとに高々 1 行なので、
  // この行数で打ち切っても「異なるサイトが RELEVANCE_MAX 以下」は正しく判定できる）。1〜RELEVANCE_MAX 件なら段を使う。
  if (q) jobs.push((async () => {
    const ks = relevanceKeys(q);
    // 完全一致が RELEVANCE_MAX 件を超える語は、前方一致の段も作らない（ありふれた語。完全一致のサイトより前方一致のサイトが上に来てしまうため）。
    try {
      const n = (await db.prepare(`SELECT COUNT(DISTINCT site_id) AS n FROM (SELECT site_id FROM site_search_keys WHERE key IN (${ks.map(() => '?').join(', ')}) LIMIT ?)`).bind(...ks, ks.length * (RELEVANCE_MAX + 1)).first())?.n ?? 0;
      if (n <= RELEVANCE_MAX) { plan.prefix = await prefixSites(db, prefixWord(q)); plan.relevance = n >= 1 || Object.keys(plan.prefix ?? {}).length > 0; }
    }
    catch (e) { if (!REL_ERR.test(String(e?.message))) throw e; /* 表なし → 段なし */ }
  })());
  if (category) jobs.push((async () => {
    const scope = scopeKeyOf({ cc, region });
    const r = await db.prepare(`SELECT (SELECT n FROM category_counts WHERE category_id = ?1 AND scope = 'all') AS n_all, (SELECT n FROM category_counts WHERE category_id = ?1 AND scope = ?2) AS n_scope, (SELECT n FROM scope_counts WHERE scope = ?2) AS n_base, EXISTS (SELECT 1 FROM categories WHERE parent_id = ?1) AS has_child`).bind(category, scope).first();
    const nAll = r?.n_all ?? 0, nScope = r?.n_scope ?? 0, base = r?.n_base ?? 0;
    plan.categoryAll = nAll;
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
    plan.driveTag = best?.t ?? null; plan.driveN = best?.n ?? null;
    plan.tagsOverCap = tags.length === 1 && best === null; // 単独のタグが上限を超える（他の絞り込みが無ければ総件数は上限付きと分かる）
  })());
  await Promise.all(jobs);
  // 全文索引を使うか。稀なタグで引ける（タグの索引から ≤TAG_DRIVE_MAX 件）・小さいカテゴリ（IN 形で ≤TOTAL_CAP 件）のときは、そちらが既に該当数に比例して小さいので索引の該当数を数える分だけ損になる。使わない。
  const small = !!plan.driveTag || (category && plan.categoryMode === 'in' && plan.categoryAll <= TOTAL_CAP);
  if (q && ftsTerms(q) && !small) {
    const extras = !!(cc || region || category || tags.length), cap = extras ? FTS_EXTRA_PROBE : TOTAL_CAP;
    try { // 表が無い（ロールバック後）・未構築のときは instr に落ちる
      const r = await db.prepare(`SELECT (SELECT ready FROM fts_state WHERE name = 'sites_fts') AS ready, (SELECT COUNT(*) FROM (SELECT rowid FROM sites_fts WHERE sites_fts MATCH ?1 LIMIT ?2)) AS n`).bind(ftsTerms(q), cap + 1).first();
      if (r?.ready === 1) {
        if (r.n <= TOTAL_CAP) plan.qMode = 'fts';
        else if (queryTerms(q).length === 1) { plan.qOver = true; plan.qCountViaFts = extras && r.n <= cap; }
      }
    } catch (e) { if (!FTS_ERR.test(String(e?.message))) throw e; /* 表なし・未構築 → instr */ }
  }
  return plan;
}

export function filters({ cc, region, category, q, tags, recent }, plan = {}) {
  const where = [], args = [];
  // 区画は国の部分集合（region_code は country_code が必須で先頭 2 文字が一致する）ので、区画があれば区画だけで絞る（国だけ分かって区画 NULL のサイトは出ない）。
  if (region) { where.push('s.region_code = ?'); args.push(region); }
  else if (cc) { where.push('s.country_code = ?'); args.push(cc); }
  if (recent) where.push('s.bulk_import = 0', `datetime(s.registered_at) > datetime('now', '-${RECENT_DAYS} days')`, "datetime(s.registered_at) <= datetime('now')");
  if (category) {
    if (plan.categoryMode === 'exists') where.push('EXISTS (SELECT 1 FROM site_categories sc WHERE sc.category_id = ? AND sc.site_id = s.id)');
    else where.push('s.id IN (SELECT site_id FROM site_categories WHERE category_id = ?)');
    args.push(category);
  }
  if (q && plan.qMode === 'fts') { where.push('s.rowid IN (SELECT rowid FROM sites_fts WHERE sites_fts MATCH ?)'); args.push(ftsTerms(q)); }
  if (q) for (const term of queryTerms(q)) { where.push('instr(s.search_text, ?) > 0'); args.push(term); }
  for (const t of tags ?? []) { // 複数タグは AND（タグごとに 1 条件）
    if (t === plan.driveTag) { where.push('s.id IN (SELECT site_id FROM site_tags WHERE label_ja = ? UNION SELECT site_id FROM site_tags WHERE label_en = ?)'); args.push(t, t); }
    else { where.push('EXISTS (SELECT 1 FROM site_tags t WHERE t.site_id = s.id AND (t.label_ja = ? OR t.label_en = ?))'); args.push(t, t); }
  }
  return { where, args };
}

// 画面に出す列。bulk_import は並びのほか NEW タグの抑止に画面が使うので含める。並びだけに使う列は SORT_KEYS で足りるのでここには要らない。
const COLUMNS = `s.id, s.name, s.name_ja, s.name_en, s.url, s.description_ja, s.description_en, s.favicon_path, s.favicon_v, s.thumb_path, s.thumb_lqip, s.thumb_captured_at, s.registered_at, s.bulk_import, s.source_updated_at, s.last_crawled_at, s.country_code, s.region_code, (SELECT json_group_array(json_object('ja',t.label_ja,'en',t.label_en)) FROM site_tags t WHERE t.site_id=s.id) AS tags_json, (SELECT json_group_array(c.category_id) FROM site_categories c WHERE c.site_id=s.id) AS category_ids_json`;
// favicon_path は列の値のまま返す（app.js が favicon_url の無いときに静的な /icons/… の値へ戻す経路で読むため。消さない）。配信 URL を favicon_url に足す（取れていなければ null）。favicon_v は favicon_url の ?v= になるので出さない。
// thumb_path（サイト固定のキー名）・thumb_captured_at は返さず、配信 URL（`?v=` に撮影時刻由来のバージョン）を thumb_url、極小プレースホルダを thumb_lqip に出す（どちらも無ければ null。画像が無いのにプレースホルダだけは出さない）。
const LQIP = /^data:image\/(webp|jpeg|png);base64,[A-Za-z0-9+/=]{1,400}$/;
const shape = ({ category_ids_json, favicon_v, thumb_path, thumb_lqip, thumb_captured_at, _r, ...r }) => { for (const k of Object.keys(r)) if (k.startsWith('_k')) delete r[k]; const thumb_url = thumbUrl(thumb_path, thumb_captured_at); return { ...r, favicon_url: faviconUrl(r.favicon_path, favicon_v), thumb_url, thumb_lqip: thumb_url && typeof thumb_lqip === 'string' && LQIP.test(thumb_lqip) ? thumb_lqip : null, category_ids: JSON.parse(category_ids_json || '[]') }; };

// 1 ページ分。cursor は decodeCursor の結果、offset は ?page=N（SSR・直リンクの開始位置）用でカーソルが無いときだけ使う。
// total は先頭ページ（カーソルなし）のときだけ入れる。q は cleanQuery 済み、tags は cleanTags 済みであること。
// total の出し方（読み取りが件数に比例しないように。#126 第2段）:
//   1. 末尾まで届いた（次ページなし）なら、読んだ行数から正確に分かる（追加の読み取りなし。0 ヒットも 1 回の走査で済む）
//   2. 範囲（国・区画）だけの絞り込みは集計表 scope_counts、直下が葉のカテゴリだけの絞り込みは category_counts（どちらも正確）
//   2.5. q が 3 文字以上で索引の該当が TOTAL_CAP 超（かつ他の絞り込みなし）は、数えずに上限付き。TOTAL_CAP 以下なら索引から正確に数える。絞り込み併用で該当が FTS_EXTRA_PROBE 以下なら総件数だけ索引から数える
//   3. それ以外（q・tag・葉でないカテゴリ）は TOTAL_CAP + 1 件で数えるのをやめる（単独のタグが上限を超えると分かっているときは数えもしない）。超えたら total = TOTAL_CAP、total_capped = true（画面は「1,000+」）
// 索引の経路が「索引がまだ無い・消えた・壊れている」で失敗したときのエラー（これだけ instr でやり直す）。D1 の一時エラー・本番だけで出る SQL エラー（too many terms in compound SELECT など）は握らず投げる（握ると毎回「索引で失敗 → instr」の二重実行になり気づけない）。
const FTS_ERR = /no such table|sites_fts|fts5|CORRUPT_VTAB|malformed/i;
const usesFts = p => p.qMode === 'fts' || p.qCountViaFts;
const ftsReady = async db => { try { return (await db.prepare(`SELECT ready FROM fts_state WHERE name = 'sites_fts'`).first())?.ready === 1; } catch (_) { return false; } };
export async function querySites(db, o) {
  if (o.category === RECENT_CATEGORY) o = { ...o, category: null, recent: true };
  const hasCursor = o.cursor && !o.cursor.none, pk = { cc: o.cc, region: o.region, category: o.category, tags: o.tags, q: o.q };
  const basePlan = await planFor(db, pk, !hasCursor);
  if (!usesFts(basePlan)) return runQuery(db, o, basePlan);
  const fallback = async why => { // instr の経路でやり直す（結果は同じ）。計画も instr にして覚える。
    console.error('sites_fts の経路を使えず instr でやり直す:', why instanceof Error ? why.message : why);
    const plan = { ...basePlan, qMode: 'instr', qOver: false, qCountViaFts: false };
    planCache.set(planKey(pk), { plan, at: Date.now() });
    try { return await runQuery(db, o, plan); }
    catch (e2) { if (why instanceof Error && e2 instanceof Error) e2.message += `（先に索引の経路が失敗: ${why.message}）`; throw e2; }
  };
  let res;
  try { res = await runQuery(db, o, basePlan); }
  catch (e) { if (!FTS_ERR.test(String(e?.message))) throw e; return fallback(e); }
  // 実行の「後」に ready を確かめる。作り直しは ready = 0 と delete-all を同じ文でこの順に流すので、実行が空・作りかけの索引を見ても、ここを読む前に構築が終わっていなければ ready = 0 が見える（残る窓: 実行が作りかけを見てからここを読むまでの D1 1 往復［数 ms］の間に、残りのチャンク全部と ready = 1 が終わり切る場合。最後のチャンクは秒単位なので実際上は起きない）
  // （実行の前の確認だけだと、確認と実行の間に作り直しが始まる窓で一覧が途中で切れる・0 件になる）。
  if (!await ftsReady(db)) return fallback('ready が 1 でない（作り直し中・表なし）');
  return res;
}
async function runQuery(db, o, basePlan) {
  const { cc = null, region = null, category = null, q = null, tags = [], limit = PAGE_SIZE, cursor = null, offset = 0, recent = false } = o;
  const hasCursor = cursor && !cursor.none, start = hasCursor ? 0 : offset;
  // 関連度の段を使うか: 先頭ページは計画で決め、続きのページはカーソルの形（段の値の有無）に従う（途中で計画が変わっても並びを変えない）。
  const rel = !!q && (hasCursor ? !!cursor.rel : !!basePlan.relevance);
  if (hasCursor && cursor.rel && !rel) return runQuery(db, { ...o, cursor: { vals: cursor.vals.slice(1) } }, basePlan); // 検索語の無い段つきカーソル: 今の並びの位置として読む
  // 稀なタグの索引（IN）は、先頭ページでは件数を該当数に比例して数えられるので使う。続きのページでは該当数が TAG_IN_MAX を超えるなら使わない
  // （ページごとに該当サイトの全件を取り出し直すより、並びの索引を走査して EXISTS で確かめる方が安い。779 件のタグで全ページ合計が main の 1.4 倍だった）。
  const plan = hasCursor && basePlan.driveTag && basePlan.driveN > TAG_IN_MAX ? { ...basePlan, driveTag: null } : basePlan;
  const keys = recent ? RECENT_SORT_KEYS : SORT_KEYS;
  const { where, args } = filters({ cc, region, category, q, tags, recent }, plan);
  // 「その他」の段（段を使わないときは全体）。excl は一覧だけに足す条件（完全一致の段のサイトを除く）。総件数の where には入れない。
  const listRest = async (cur, lim, off, excl, exclArgs) => {
    const run = async (extra, extraArgs, lim2, off2, order = siteOrder(keys)) => {
      const conds = [...where, ...excl, ...extra];
      return (await db.prepare(`SELECT ${COLUMNS}, ${keyCols(keys)} FROM sites s ${conds.length ? 'WHERE ' + conds.join(' AND ') : ''} ${order} LIMIT ? OFFSET ?`).bind(...args, ...exclArgs, ...extraArgs, lim2, off2).all()).results;
    };
    // IN（所属サイト・稀なタグの索引）で引く計画は、範囲条件を足すと SQLite が並びの索引の走査に切り替えて IN が効かなくなる。1 本の `A OR B OR C`（afterCursor）なら IN で引いたまま並べ替える。
    const inDriven = (category && plan.categoryMode === 'in') || !!plan.driveTag || plan.qMode === 'fts'; // 索引（MATCH）も IN で引く計画
    if (cur && inDriven) { const c = afterCursor(cur, keys); return run([c.sql], c.args, lim, 0); }
    if (cur) { const res = []; for (const t of cursorTerms(cur, keys)) { if (res.length >= lim) break; res.push(...await run([t.sql], t.args, lim - res.length, 0, t.order)); } return res; } // 項ごとに索引を引いて前から連結（cursorTerms）
    return run([], [], lim, off);
  };
  let results;
  if (!rel) results = await listRest(hasCursor ? cursor.vals : null, limit + 1, start, [], []);
  else {
    try { results = await listRelevance(); }
    catch (e) { if (!REL_ERR.test(String(e?.message))) throw e; console.error('site_search_keys が無いので段なしで続ける:', e.message); return runQuery(db, { ...o, cursor: hasCursor ? { vals: cursor.vals.slice(1) } : cursor }, { ...basePlan, relevance: false }); }
  }
  // 段の SQL。完全一致の段の k は候補の鍵ごとの一致を 1 サイト 1 行（小さい rank）にまとめたもの。前方一致の段の p は計画の集合（json_each で 1 サイト 1 行）。
  // loose（候補が 2 つ）のときは段を部分一致で絞らない（relevanceKeys の説明）。前方一致の段からは完全一致の段のサイトを除く（主キーの点引き）。
  function tierParts() {
    const ks = relevanceKeys(q), loose = ks.length > 1 || prefixWord(q) !== q, ph = ks.map(() => '?').join(', ');
    const ex = filters({ cc, region, category, q: loose ? null : q, tags, recent }, { ...plan, categoryMode: 'exists', driveTag: null, qMode: 'instr' });
    const notExact = `NOT EXISTS (SELECT 1 FROM site_search_keys x WHERE x.key IN (${ph}) AND x.site_id = s.id)`;
    const pre = plan.prefix && Object.keys(plan.prefix).length ? JSON.stringify(plan.prefix) : null;
    const exact = {
      sql: (sel, extra = []) => `SELECT ${sel} FROM (SELECT site_id, MIN(rank) AS rank FROM site_search_keys WHERE key IN (${ph}) GROUP BY site_id) k CROSS JOIN sites s ON s.id = k.site_id${[...ex.where, ...extra].length ? ' WHERE ' + [...ex.where, ...extra].join(' AND ') : ''}`,
      args: extraArgs => [...ks, ...ex.args, ...extraArgs], keys: relKeys(keys), col: 'k.rank', toVals: v => v, fromSc: sc => sc,
    };
    const prefix = pre && {
      sql: (sel, extra = []) => `SELECT ${sel} FROM (SELECT j.key AS site_id, j.value AS sc FROM json_each(?) j) p CROSS JOIN sites s ON s.id = p.site_id WHERE ${[...ex.where, notExact, ...extra].join(' AND ')}`,
      args: extraArgs => [pre, ...ex.args, ...ks, ...extraArgs], keys: prefixKeys(keys), col: 'p.sc', toVals: v => [v[0] - PREFIX_BASE, ...v.slice(1)], fromSc: sc => sc + PREFIX_BASE,
    };
    return { ks, loose, notExact, pre, prefixIds: new Set(Object.keys(plan.prefix ?? {})), tiers: [exact, prefix].filter(Boolean) };
  }
  // 総件数に足す分: loose のとき、部分一致の集合に入らない段のサイトの数（段はそれぞれ 100 件以下なので、読むのも鍵と段のサイトだけ）。
  const outsideHits = async () => {
    if (!rel) return 0;
    const { loose, tiers } = tierParts();
    if (!loose) return 0;
    const terms = queryTerms(q), notHit = `NOT (${terms.map(() => 'instr(s.search_text, ?) > 0').join(' AND ')})`;
    let n = 0;
    for (const t of tiers) n += (await db.prepare(t.sql('COUNT(*) AS n', [notHit])).bind(...t.args(terms)).first())?.n ?? 0;
    return n;
  };
  // 完全一致の段 → 前方一致の段 → その他 の順に limit + 1 行まで集める。段は鍵の索引・計画の集合から引き（CROSS JOIN で外側に固定）、絞り込みは 1 行ずつ確かめる形（EXISTS・instr）にする。
  // カーソルの段の値: 0〜4 = 完全一致の段（rank）、PREFIX_BASE 以上 = 前方一致の段（PREFIX_BASE + score）、RANK_REST = その他。
  async function listRelevance() {
    const { ks, notExact, pre, prefixIds, tiers } = tierParts();
    const r0 = hasCursor ? cursor.vals[0] : null;
    const at = r0 === null ? 0 : r0 < RANK_REST ? 0 : r0 >= PREFIX_BASE ? 1 : 2; // 0 完全一致 / 1 前方一致 / 2 その他
    const out = [];
    let off = start;
    for (const [i, t] of [tiers[0], tiers[1] ?? null].entries()) {
      if (i < at || !t) continue;
      const need = limit + 1 - out.length;
      const c = hasCursor && i === at ? afterCursor(t.toVals(cursor.vals), t.keys) : null;
      // 並べ替えは id と段の値・並びのキーだけで行い、画面の列（タグ・カテゴリの副問い合わせを含む）は残った 1 ページ分だけで作る（並べ替える全行で作ると、段の該当数 × タグ数を読む）。
      const page = `${t.sql(`s.id AS sid, ${t.col} AS rk`, c ? [c.sql] : [])} ${siteOrder(t.keys)} LIMIT ? OFFSET ?`;
      const rows = (await db.prepare(`SELECT ${COLUMNS}, ${keyCols(keys)}, k.rk AS _r FROM (${page}) k CROSS JOIN sites s ON s.id = k.sid ORDER BY k.rk, ${siteOrder(keys).slice('ORDER BY '.length)}`).bind(...t.args(c?.args ?? []), need, off).all()).results;
      out.push(...rows.map(r => ({ ...r, _r: t.fromSc(r._r) })));
      if (out.length > limit) return out;
      // ?page=N（offset）が段を越えたら、段の件数を引いた位置から次を読む（段はそれぞれ 100 件以下）。
      off = !hasCursor && off > 0 && !rows.length ? Math.max(0, off - ((await db.prepare(t.sql('COUNT(*) AS n')).bind(...t.args([])).first())?.n ?? 0)) : 0;
    }
    // その他: 完全一致の段のサイトは SQL（主キーの点引き）で、前方一致の段のサイトは読んだ行から除く（除いた分だけ読み足す。集合は 100 件以下）。
    // ?page=N の途中から読むときだけは位置を合わせるため SQL で除く（json_each の行も読み取りに数えられるので、ふだんは使わない）。
    const cur0 = hasCursor && at === 2 ? cursor.vals.slice(1) : null, need0 = limit + 1 - out.length;
    if (off > 0 && pre) { const rows = await listRest(null, need0, off, [notExact, 'NOT EXISTS (SELECT 1 FROM json_each(?) j WHERE j.key = s.id)'], [...ks, pre]); return [...out, ...rows.map(r => ({ ...r, _r: RANK_REST }))]; }
    let cur = cur0;
    for (let g = 0; g <= prefixIds.size + 1; g++) {
      const want = limit + 1 - out.length, rows = await listRest(cur, want, g === 0 ? off : 0, [notExact], ks);
      for (const r of rows) if (!prefixIds.has(r.id)) out.push({ ...r, _r: RANK_REST });
      if (out.length > limit || rows.length < want) break;
      cur = keys.map((_, i) => rows.at(-1)[`_k${i}`] ?? null);
    }
    return out;
  }
  const more = results.length > limit, rows = results.slice(0, limit);
  const next_cursor = more ? encodeCursor([...(rel ? [rows.at(-1)._r] : []), ...keys.map((_, i) => rows.at(-1)[`_k${i}`] ?? null)]) : null;
  const out = { items: rows.map(shape), next_cursor };
  if (!hasCursor) {
    const plain = !category && !q && !tags?.length;
    if (!more && (start === 0 || rows.length > 0)) { out.total = start + rows.length; out.total_capped = false; }
    else if (plain && !recent) { out.total = (await db.prepare(`SELECT COALESCE((SELECT n FROM scope_counts WHERE scope = ?), 0) AS total`).bind(scopeKeyOf({ cc, region })).first())?.total ?? 0; out.total_capped = false; }
    else if (plan.categoryLeafCount !== null && !q && !tags?.length) { out.total = plan.categoryLeafCount; out.total_capped = false; }
    else if (plan.tagsOverCap && !q && !category && !cc && !region && !recent) { out.total = TOTAL_CAP; out.total_capped = true; } // 単独のタグが上限超え: 数えない
    else if (plan.qOver && !category && !cc && !region && !tags?.length && !recent) { out.total = TOTAL_CAP; out.total_capped = true; } // q だけで索引の該当が上限超え: 数えない
    else if (plan.qCountViaFts) { // 併用ありで q の該当が上限超え〜FTS_EXTRA_PROBE 以下: 一覧は instr の走査のまま、総件数だけ索引から数える
      const f2 = filters({ cc, region, category, q, tags, recent }, { ...plan, qMode: 'fts' });
      const n = ((await db.prepare(`SELECT COUNT(*) AS n FROM (SELECT 1 FROM sites s WHERE ${f2.where.join(' AND ')} LIMIT ${TOTAL_CAP + 1})`).bind(...f2.args).first())?.n ?? 0) + await outsideHits();
      out.total = Math.min(n, TOTAL_CAP); out.total_capped = n > TOTAL_CAP;
    }
    else {
      const n = ((await db.prepare(`SELECT COUNT(*) AS n FROM (SELECT 1 FROM sites s ${where.length ? 'WHERE ' + where.join(' AND ') : ''} LIMIT ${TOTAL_CAP + 1})`).bind(...args).first())?.n ?? 0) + await outsideHits(); // 段の部分一致に入らない分（relevanceKeys）
      out.total = Math.min(n, TOTAL_CAP); out.total_capped = n > TOTAL_CAP;
    }
  }
  return out;
}

// ピックアップ用: 範囲内からランダムに n 件。ピックアップは「発見」の表示なので、台帳などの一括投入（bulk_import=1）は除く。
// 読み取りが件数に比例しないよう、`ORDER BY RANDOM()`（全件を読む）をやめ、乱数の rowid を PICK_BATCHES[0] 個（足りなければ増やして引き直し）まとめて `rowid IN (…)` で引き、条件（一括投入でない・範囲内）に合う行だけを残す棄却抽選にする（#126 第2段）。
// 各 rowid は同じ確率で選ばれ、合う行だけが残るので、合う行の中で一様になる（「乱数位置の次の該当行」方式は、一括投入の塊の直後の 1 件に偏った）。足りなければ PICK_BATCHES の回数まで引き直す（引くたびに増やす）。
// 範囲内の件数が PICK_RANDOM_MAX 以下（棄却が多く、全件でも少ない）なら従来の `ORDER BY RANDOM()`。それでも n 件に足りなければ、足りない分だけ `ORDER BY RANDOM()` で補う（件数が少ないときだけ起きる）。
const PICK_BATCHES = [40, 80], PICK_RANDOM_MAX = 50; // 1 回に引く rowid の数（D1 のバインド値は 1 文 100 個まで）。合う行が少ない範囲（本番では日本の非一括は 3%）ほど引き直しが要る
const shuffle = a => { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }; // Fisher-Yates
// region（例 JP-13）があれば区画内、無ければ cc（国）内、どちらも無ければ全世界。区画があるとき cc は使わない（区画は国の部分集合）。
// ORDER BY RANDOM() で引く SQL。?1 = 区画コード（region あり）または国コード（無ければ null で全世界）、除外 id は ?2〜。region があるときは region_code の等値だけ（idx_sites_region_sort を使う）。
export const randomPickSql = ({ region = null, nExclude = 0, k = 3 }) => `SELECT ${COLUMNS} FROM sites s WHERE s.bulk_import = 0 AND ${region ? 's.region_code = ?1' : '(?1 IS NULL OR s.country_code = ?1)'}${nExclude ? ` AND s.id NOT IN (${Array.from({ length: nExclude }, (_, i) => '?' + (i + 2)).join(',')})` : ''} ORDER BY RANDOM() LIMIT ${Number(k)}`;
export async function randomSites(db, cc, n = 3, region = null) {
  let scopeN = null;
  if (cc || region) { scopeN = (await db.prepare(`SELECT n FROM scope_counts WHERE scope = ?`).bind(region ?? cc).first())?.n ?? 0; if (!(scopeN > 0)) return []; }
  const byRandom = async (excludeIds, k) => (await db.prepare(randomPickSql({ region, nExclude: excludeIds.length, k })).bind(region ?? cc, ...excludeIds).all()).results;
  // 区画は全体のごく一部なので、乱数の rowid で引くと当たりが少ない。区画の索引（0026）で区画内の行だけを読んで並べ替える（区画の件数に比例。最大でも数千行）。
  // 区画のときだけ `s.region_code = ?1` を書く（`(?2 IS NULL OR …)` の形だと索引が使われず activity の索引＋一時 B-tree になった。filters() と同じ方針）。
  if (scopeN !== null && (region || scopeN <= PICK_RANDOM_MAX)) return shuffle(await byRandom([], n)).map(shape);
  const max = (await db.prepare(`SELECT MAX(rowid) AS m FROM sites`).bind().first())?.m ?? 0;
  if (!max) return [];
  // `+s.bulk_import` は、並びの索引（bulk_import が先頭）を選ばせず rowid の点引きにするため（ローカルの D1 で、付けないと非一括の全件を走査して 300 行余り読んだ）。
  const found = new Map();
  for (let round = 0; round < PICK_BATCHES.length && found.size < n; round++) {
    const rowids = Array.from({ length: PICK_BATCHES[round] }, () => Math.floor(Math.random() * max) + 1);
    const { results } = await db.prepare(`SELECT ${COLUMNS} FROM sites s WHERE s.rowid IN (${rowids.map((_, i) => '?' + (i + 2)).join(',')}) AND +s.bulk_import = 0 AND (?1 IS NULL OR s.country_code = ?1)`).bind(cc, ...rowids).all();
    for (const r of results) if (!found.has(r.id)) found.set(r.id, r);
  }
  const rows = shuffle([...found.values()]).slice(0, n);
  if (rows.length < n) rows.push(...await byRandom(rows.map(r => r.id), n - rows.length));
  return shuffle(rows).map(shape);
}
