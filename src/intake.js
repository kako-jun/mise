// 申請(推薦 add / 取り下げ remove / 訂正 correct)の受付。Issue #4・#29、docs/operations/requests.md。
// サーバーは申請されたURLを取りに行かない(SSRF の余地を作らない)。検証は文字列だけで行う。
export const TYPES = ['add', 'remove', 'correct'];
export const LIMITS = { body: 8192, url: 500, reason: 1000, contact: 200, perHour: 3, perDay: 10, netPerDay: 30, addPerDay: 300, otherPerDay: 100 };

// 共有ホスティング(ユーザーごとのサブドメイン)。拒否リストはここでは完全一致(+その配下)だけで、親(github.io 等)には広げない。
export const SHARED_SUFFIXES = ['github.io', 'gitlab.io', 'pages.dev', 'netlify.app', 'vercel.app', 'workers.dev', 'blogspot.com', 'wordpress.com', 'tumblr.com', 'web.app', 'firebaseapp.com', 'herokuapp.com', 'fc2.com', 'hatenablog.com', 'hatenadiary.jp', 'wixsite.com', 'weebly.com', 'sakura.ne.jp', 'neocities.org', 'glitch.me'];

// 制御文字(C0/C1)・双方向制御文字を除いてトリムする。
const clean = s => String(s ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f​-‏⁠﻿‪-‮⁦-⁩]/g, '').trim();

const isIp = h => /^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.includes(':') || /^\[/.test(h) || /^0x/i.test(h) || /^\d+$/.test(h);

// RFC 3986 の unreserved(A-Za-z0-9-._~)の %XX を復号する(%7E と ~ を同一視)。それ以外の %XX は大文字に揃える。
export function decodeUnreserved(s) {
  return s.replace(/%([0-9a-f]{2})/gi, (m, h) => { const c = String.fromCharCode(parseInt(h, 16)); return /[A-Za-z0-9\-._~]/.test(c) ? c : `%${h.toUpperCase()}`; });
}

// 戻り値: { ok, url, host, path, key } | { ok:false, error }。key は比較キー(スキーム・www.・末尾スラッシュ・大文字小文字を同一視)。
export function normalizeUrl(input) {
  const raw = clean(input);
  if (!raw || raw.length > LIMITS.url) return { ok: false, error: 'invalid_url' };
  let u;
  // スキームがあるもの(://、または危険な既知スキーム)はそのまま、無いもの(example.com:443 を含む)は https:// を補う
  try { u = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) || /^(javascript|data|mailto|file|ftp|vbscript|blob|tel):/i.test(raw) ? raw : `https://${raw}`); } catch { return { ok: false, error: 'invalid_url' }; }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return { ok: false, error: 'invalid_url' };
  if (u.username || u.password) return { ok: false, error: 'invalid_url' };
  if (u.port) return { ok: false, error: 'invalid_url' }; // URL はデフォルトポートを落とす。それ以外のポートは受けない
  const host = u.hostname.replace(/\.$/, '').toLowerCase();
  if (isIp(host) || host === 'localhost' || !host.includes('.') || !/^[a-z0-9.-]+$/.test(host)) return { ok: false, error: 'invalid_url' };
  if (/\.(local|localhost|internal|lan|home|test|invalid|example)$/.test(host) || host.endsWith('.arpa')) return { ok: false, error: 'invalid_url' };
  const path = u.pathname.replace(/\/+$/, '');
  const kpath = decodeUnreserved(path).toLowerCase();
  return { ok: true, url: `${u.protocol}//${host}${path}`, host, path: kpath, key: `${host.replace(/^www\./, '')}${kpath}` };
}

// 拒否リスト照合用の候補ホスト。host 自身と親ドメイン(登録ドメイン配下を拒否)。www. は無視。
// 共有ホスティング配下は、ユーザー領域(共有サフィックスの 1 つ上)までで止める(github.io 全体は拒否しない)。
export function blockCandidates(host) {
  const h = host.replace(/^www\./, '');
  const labels = h.split('.');
  const shared = SHARED_SUFFIXES.find(s => h === s || h.endsWith(`.${s}`));
  const min = shared ? shared.split('.').length + 1 : 2;
  const out = [];
  for (let i = 0; labels.length - i >= min; i++) out.push(labels.slice(i).join('.'));
  return out;
}

// 拒否リストの行(host,path_prefix)が申請のホスト・パスに当たるか。path_prefix が '' はホスト全体(親ドメイン配下を含む)、
// それ以外はそのホスト自身(www. 無視)のパス配下だけ。path は normalizeUrl の path(小文字・末尾スラッシュなし)。
export function isBlocked(rows, host, path) {
  const h = host.replace(/^www\./, '');
  return rows.some(r => !r.path_prefix || (r.host === h && (path === r.path_prefix || path.startsWith(`${r.path_prefix}/`))));
}

const EMAIL = /^[^\s@<>"',;:()\[\]\\]+@[a-z0-9-]+(\.[a-z0-9-]+)+$/i;
// 連絡先はメールアドレスか https の URL(SNS のプロフィール等)。URL は URL として検証し、元の href(クエリを含む)を保存する。
// profile.php?id=123 のようにクエリが意味を持つため normalizeUrl の出力は使わない(宛先の検査にだけ使う)。比較キーには使わない。
export function normalizeContact(input) {
  const c = clean(input);
  if (!c || c.length > LIMITS.contact) return null;
  if (EMAIL.test(c)) return c;
  if (/[<>"'\s]/.test(c) || !/^https:\/\//i.test(c) || !normalizeUrl(c).ok) return null;
  try { return new URL(c).href; } catch { return null; }
}

// body: 任意のパース済みJSON。戻り値 { ok, value:{type,url,host,path,key,reason,contact} } | { ok:false, error }
export function validateRequest(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { ok: false, error: 'invalid_body' };
  if (body.website) return { ok: false, error: 'spam' }; // ハニーポット(人間には見えない欄)
  if (!TYPES.includes(body.type)) return { ok: false, error: 'invalid_type' };
  const u = normalizeUrl(body.url);
  if (!u.ok) return u;
  const reason = clean(body.reason);
  if (reason.length > LIMITS.reason) return { ok: false, error: 'reason_too_long' };
  if (body.type === 'correct' && !reason) return { ok: false, error: 'reason_required' };
  if (/<\/?[a-z!?]/i.test(reason)) return { ok: false, error: 'invalid_reason' }; // HTML のタグらしいものは受けない("a < b" は可)
  const contact = normalizeContact(body.contact);
  if (!contact) return { ok: false, error: 'invalid_contact' };
  return { ok: true, value: { type: body.type, url: u.url, host: u.host, path: u.path, key: u.key, reason: reason || null, contact } };
}

// IPv4 射影アドレス(::ffff:a.b.c.d / ::ffff:xxxx:xxxx)は IPv4 にする。
function mapped(ip) {
  let m = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(ip); if (m) return m[1];
  m = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/i.exec(ip);
  if (m) { const a = parseInt(m[1], 16), b = parseInt(m[2], 16); return `${a >> 8}.${a & 255}.${b >> 8}.${b & 255}`; }
  return ip;
}
// IPv6 は先頭 n グループに丸める(既定 4 = /64、3 = /48)。IPv4 はそのまま。
export function ipPrefix(ip, n = 4) {
  ip = ip ? mapped(ip.trim()) : ip;
  if (!ip || !ip.includes(':')) return ip || 'unknown';
  const [head, tail = ''] = ip.split('::');
  const a = head ? head.split(':') : [], b = tail ? tail.split(':') : [];
  const groups = ip.includes('::') ? [...a, ...Array(Math.max(0, 8 - a.length - b.length)).fill('0'), ...b] : a;
  return groups.slice(0, n).map(g => g.toLowerCase().replace(/^0+(?=.)/, '')).join(':');
}

// 生 IP は保存しない。(塩 + IP[IPv6 は /64、n=3 なら /48]) の SHA-256 の先頭 24 文字。日付は含まない(24 時間の窓は DB 側で数える)。
export async function ipHash(ip, salt = '', n = 4) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${salt}|${ipPrefix(ip, n)}`));
  return [...new Uint8Array(d)].slice(0, 12).map(b => b.toString(16).padStart(2, '0')).join('');
}

// 同一オリジンからの fetch だけ受ける(Origin ヘッダが無い/異なるものは拒否)。
export function sameOrigin(req) {
  const o = req.headers.get('Origin');
  return !!o && o === new URL(req.url).origin;
}

// 本文を上限バイト数まで読む。超えたら null。Content-Length は先に見て、全文を読む前に拒否する。
export async function readLimited(req, max = LIMITS.body) {
  const len = req.headers.get('Content-Length');
  if (len && Number(len) > max) return null;
  if (!req.body) return '';
  const reader = req.body.getReader(), chunks = []; let n = 0;
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    n += value.byteLength;
    if (n > max) { await reader.cancel().catch(() => {}); return null; }
    chunks.push(value);
  }
  const all = new Uint8Array(n); let o = 0; for (const c of chunks) { all.set(c, o); o += c.byteLength; }
  return new TextDecoder().decode(all);
}

// 受付本体。D1 を使う。戻り値 { status, body }
export async function receiveRequest(c) {
  const req = c.req.raw;
  const salt = c.env.INTAKE_SALT;
  if (!salt) return { status: 503, body: { error: 'unavailable' } }; // 塩が無ければ受付を止める(IP ハッシュが弱くなるため)
  if (!sameOrigin(req)) return { status: 403, body: { error: 'forbidden' } };
  if ((req.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'application/json') return { status: 415, body: { error: 'unsupported_media_type' } };
  const text = await readLimited(req);
  if (text === null) return { status: 413, body: { error: 'too_large' } };
  let json; try { json = JSON.parse(text); } catch { return { status: 400, body: { error: 'invalid_body' } }; }
  const v = validateRequest(json);
  if (!v.ok) return v.error === 'spam' ? { status: 202, body: { status: 'pending' } } : { status: 400, body: { error: v.error } }; // ハニーポットは成功に見せる
  const { type, url, host, path, key, reason, contact } = v.value;
  const db = c.env.DB;
  const ip = req.headers.get('CF-Connecting-IP');
  const hash = await ipHash(ip, salt), net = await ipHash(ip, salt, 3);
  // 拒否リスト済み(ホスト全体は登録ドメイン配下を含む。パス型はそのパス配下)への推薦は rejected で記録し、連絡先は保存しない。
  // 取り下げ・訂正は拒否リストにあっても pending で記録する。
  let blocked = false;
  if (type === 'add') {
    const cands = blockCandidates(host);
    const { results } = await db.prepare(`SELECT host, path_prefix FROM blocked_hosts WHERE host IN (${cands.map((_, i) => `?${i + 1}`).join(',')})`).bind(...cands).all();
    blocked = isBlocked(results || [], host, path);
  }
  // 同一 URL の申請は(別人の可能性があるので)全件保存する。同一 IP ハッシュ+同種別+同一キー+同一連絡先の未処理の再送だけを重複とみなす
  // (連絡先が違えば別行。本物の運営者が連絡先を直して送り直せる)。
  // 重複・レート制限の判定は INSERT と同時に行う(競合しても上限を超えない)。IP 単位: 1時間3件・24時間10件。
  // 同一 /48(IPv6)は 24 時間 30 件。全体: 推薦は 24 時間 300 件、取り下げ・訂正は別枠で 24 時間 100 件。
  const r = await db.prepare(
    `INSERT INTO requests (type,status,url,host,normalized_key,reason,contact,ip_hash,note,decided_at,net_hash)
     SELECT ?1,?2,?3,?4,?5,?6,?7,?8,?9,CASE WHEN ?2='rejected' THEN datetime('now') END,?14
     WHERE NOT EXISTS (SELECT 1 FROM requests WHERE ip_hash=?8 AND type=?1 AND normalized_key=?5 AND contact IS ?7 AND status='pending')
       AND (SELECT COUNT(*) FROM requests WHERE ip_hash=?8 AND created_at >= datetime('now','-1 hour')) < ?10
       AND (SELECT COUNT(*) FROM requests WHERE ip_hash=?8 AND created_at >= datetime('now','-1 day')) < ?11
       AND (SELECT COUNT(*) FROM requests WHERE net_hash=?14 AND created_at >= datetime('now','-1 day')) < ?15
       AND (SELECT COUNT(*) FROM requests WHERE (type='add') = ?12 AND created_at >= datetime('now','-1 day')) < ?13`
  ).bind(type, blocked ? 'rejected' : 'pending', url, host, key, reason, blocked ? null : contact, hash, blocked ? 'blocklisted' : null,
    LIMITS.perHour, LIMITS.perDay, type === 'add' ? 1 : 0, type === 'add' ? LIMITS.addPerDay : LIMITS.otherPerDay, net, LIMITS.netPerDay).run();
  if (r.meta?.changes) return { status: 202, body: { status: 'pending' } };
  // 挿入されなかった: 同一 IP の再送なら成功に見せ、そうでなければレート超過。
  const dup = await db.prepare(`SELECT 1 AS x FROM requests WHERE ip_hash=?1 AND type=?2 AND normalized_key=?3 AND contact IS ?4 AND status='pending' LIMIT 1`).bind(hash, type, key, blocked ? null : contact).first();
  return dup ? { status: 202, body: { status: 'pending' } } : { status: 429, body: { error: 'rate_limited' } };
}
