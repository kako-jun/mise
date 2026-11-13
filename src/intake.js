// 申請(推薦 add / 取り下げ remove / 訂正 correct)の受付。Issue #4・#29、docs/operations/requests.md。
// サーバーは申請されたURLを取りに行かない(SSRF の余地を作らない)。検証は文字列だけで行う。
export const TYPES = ['add', 'remove', 'correct'];
export const LIMITS = { body: 4096, url: 500, reason: 1000, contact: 200, perHour: 3, perDay: 10, pendingPerHost: 1, globalPerDay: 300 };

// 制御文字・双方向制御文字を除いてトリムする。
const clean = s => String(s ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f‎‏‪-‮⁦-⁩]/g, '').trim();

const isIp = h => /^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.includes(':') || /^\[/.test(h) || /^0x/i.test(h) || /^\d+$/.test(h);

// 戻り値: { ok, url, host } | { ok:false, error }
export function normalizeUrl(input) {
  const raw = clean(input);
  if (!raw || raw.length > LIMITS.url) return { ok: false, error: 'invalid_url' };
  let u;
  try { u = new URL(/^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : `https://${raw}`); } catch { return { ok: false, error: 'invalid_url' }; }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return { ok: false, error: 'invalid_url' };
  if (u.username || u.password) return { ok: false, error: 'invalid_url' };
  if (u.port) return { ok: false, error: 'invalid_url' }; // 既定ポート以外は受けない
  const host = u.hostname.replace(/\.$/, '').toLowerCase();
  if (isIp(host) || host === 'localhost' || !host.includes('.') || !/^[a-z0-9.-]+$/.test(host)) return { ok: false, error: 'invalid_url' };
  if (/\.(local|localhost|internal|lan|home|test|invalid|example)$/.test(host) || host.endsWith('.arpa')) return { ok: false, error: 'invalid_url' };
  const path = u.pathname.replace(/\/+$/, '');
  return { ok: true, url: `${u.protocol}//${host}${path}`, host };
}

const EMAIL = /^[^\s@<>"',;:()\[\]\\]+@[a-z0-9-]+(\.[a-z0-9-]+)+$/i;
// 連絡先はメールアドレスか https の URL(SNS のプロフィール等)。
export function normalizeContact(input) {
  const c = clean(input);
  if (!c || c.length > LIMITS.contact) return null;
  if (EMAIL.test(c)) return c;
  const u = normalizeUrl(c);
  return u.ok && /^https:/i.test(c) ? c : null;
}

// body: 任意のパース済みJSON。戻り値 { ok, value:{type,url,host,reason,contact} } | { ok:false, error }
export function validateRequest(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { ok: false, error: 'invalid_body' };
  if (body.website) return { ok: false, error: 'spam' }; // ハニーポット(人間には見えない欄)
  if (!TYPES.includes(body.type)) return { ok: false, error: 'invalid_type' };
  const u = normalizeUrl(body.url);
  if (!u.ok) return u;
  const reason = clean(body.reason);
  if (reason.length > LIMITS.reason) return { ok: false, error: 'reason_too_long' };
  if (body.type === 'correct' && !reason) return { ok: false, error: 'reason_required' };
  if (/<\s*\/?\s*[a-z!]/i.test(reason)) return { ok: false, error: 'invalid_reason' }; // HTML は受けない
  const contact = normalizeContact(body.contact);
  if (!contact) return { ok: false, error: 'invalid_contact' };
  return { ok: true, value: { type: body.type, url: u.url, host: u.host, reason: reason || null, contact } };
}

// 生 IP は保存しない。IP+日付(UTC日)+任意の塩の SHA-256 の先頭。日が替わると別人扱い(日次の上限は ip_hash 単位の直近24時間で数える)。
export async function ipHash(ip, salt = '') {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${salt}|${ip || 'unknown'}`));
  return [...new Uint8Array(d)].slice(0, 12).map(b => b.toString(16).padStart(2, '0')).join('');
}

// 同一オリジンからの fetch だけ受ける(Origin ヘッダが無い/異なるものは拒否)。
export function sameOrigin(req) {
  const o = req.headers.get('Origin');
  return !!o && o === new URL(req.url).origin;
}

// 受付本体。D1 を使う。戻り値 { status, body }
export async function receiveRequest(c) {
  const req = c.req.raw;
  if (!sameOrigin(req)) return { status: 403, body: { error: 'forbidden' } };
  if (!(req.headers.get('Content-Type') || '').toLowerCase().startsWith('application/json')) return { status: 415, body: { error: 'unsupported_media_type' } };
  const text = await req.text();
  if (text.length > LIMITS.body) return { status: 413, body: { error: 'too_large' } };
  let json; try { json = JSON.parse(text); } catch { return { status: 400, body: { error: 'invalid_body' } }; }
  const v = validateRequest(json);
  if (!v.ok) return v.error === 'spam' ? { status: 202, body: { status: 'pending' } } : { status: 400, body: { error: v.error } }; // ハニーポットは成功に見せる
  const { type, url, host, reason, contact } = v.value;
  const db = c.env.DB;
  const hash = await ipHash(req.headers.get('CF-Connecting-IP'), c.env.INTAKE_SALT);
  // 同じ種別・URL が未処理なら新規に積まない(成功に見せる。受付済みの事実は変わらない)。
  const dup = await db.prepare(`SELECT id FROM requests WHERE type=?1 AND url=?2 AND status='pending' LIMIT 1`).bind(type, url).first();
  if (dup) return { status: 202, body: { status: 'pending' } };
  // 拒否リスト済みホストへの推薦は受理せず rejected として記録(再追加しない)。取り下げ・訂正は拒否リスト済みでも記録するだけ。
  const blocked = type === 'add' ? await db.prepare(`SELECT 1 AS x FROM blocked_hosts WHERE host=?1`).bind(host).first() : null;
  const status = blocked ? 'rejected' : 'pending';
  // レート制限は INSERT と同時に判定する(競合しても上限を超えない)。IP 単位: 1時間3件・24時間10件、全体: 24時間300件。
  const r = await db.prepare(
    `INSERT INTO requests (type,status,url,host,reason,contact,ip_hash,note,decided_at)
     SELECT ?1,?2,?3,?4,?5,?6,?7,?8,CASE WHEN ?2='rejected' THEN datetime('now') END
     WHERE (SELECT COUNT(*) FROM requests WHERE ip_hash=?7 AND created_at >= datetime('now','-1 hour')) < ?9
       AND (SELECT COUNT(*) FROM requests WHERE ip_hash=?7 AND created_at >= datetime('now','-1 day')) < ?10
       AND (SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','-1 day')) < ?11`
  ).bind(type, status, url, host, reason, contact, hash, blocked ? 'blocklisted' : null, LIMITS.perHour, LIMITS.perDay, LIMITS.globalPerDay).run();
  if (!r.meta?.changes) return { status: 429, body: { error: 'rate_limited' } };
  return { status: 202, body: { status: 'pending' } };
}
