import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeUrl, normalizeContact, validateRequest, ipHash, sameOrigin, receiveRequest, LIMITS } from '../src/intake.js';

test('normalizeUrl: 正規化', () => {
  assert.deepEqual(normalizeUrl('Example.COM/foo/?a=1#x'), { ok: true, url: 'https://example.com/foo', host: 'example.com' });
  assert.equal(normalizeUrl('http://blog.example.jp/').url, 'http://blog.example.jp');
});
test('normalizeUrl: SSRF になりうる宛先・形式を拒否', () => {
  for (const bad of ['http://127.0.0.1/', 'http://localhost/', 'https://10.0.0.1/', 'http://[::1]/', 'http://2130706433/', 'http://0x7f.1/', 'https://intranet/', 'https://a.local/', 'https://a.internal/', 'https://user:pw@example.com/', 'https://example.com:8443/', 'ftp://example.com/', 'javascript:alert(1)', 'file:///etc/passwd', 'https://exa mple.com/', '', 'https://' + 'a'.repeat(600) + '.com'])
    assert.equal(normalizeUrl(bad).ok, false, bad);
});
test('normalizeContact: メールか https URL のみ', () => {
  assert.equal(normalizeContact('me@example.com'), 'me@example.com');
  assert.equal(normalizeContact('https://example.com/profile'), 'https://example.com/profile');
  for (const bad of ['', 'x', 'http://example.com/', 'a b@example.com', '<a@b.com>', 'javascript:alert(1)', 'a'.repeat(250) + '@example.com']) assert.equal(normalizeContact(bad), null, bad);
});
test('validateRequest: 正常・異常', () => {
  const ok = { type: 'add', url: 'https://example.com/', contact: 'me@example.com' };
  assert.equal(validateRequest(ok).ok, true);
  assert.equal(validateRequest({ ...ok, type: 'x' }).error, 'invalid_type');
  assert.equal(validateRequest({ ...ok, contact: '' }).error, 'invalid_contact');
  assert.equal(validateRequest({ ...ok, type: 'correct' }).error, 'reason_required');
  assert.equal(validateRequest({ ...ok, reason: 'x'.repeat(LIMITS.reason + 1) }).error, 'reason_too_long');
  assert.equal(validateRequest({ ...ok, reason: '<script>alert(1)</script>' }).error, 'invalid_reason');
  assert.equal(validateRequest({ ...ok, website: 'bot' }).error, 'spam');
  for (const b of [null, [], 'x', 1]) assert.equal(validateRequest(b).error, 'invalid_body');
  assert.equal(validateRequest({ ...ok, reason: 'a‮b\u0000c' }).value.reason, 'abc');
});
test('ipHash: 生 IP を含まず安定', async () => {
  const h = await ipHash('203.0.113.9', 's');
  assert.match(h, /^[0-9a-f]{24}$/);
  assert.equal(h, await ipHash('203.0.113.9', 's'));
  assert.notEqual(h, await ipHash('203.0.113.10', 's'));
});
test('sameOrigin', () => {
  const r = o => new Request('https://mise.example/api/requests', { method: 'POST', headers: o ? { Origin: o } : {} });
  assert.equal(sameOrigin(r('https://mise.example')), true);
  assert.equal(sameOrigin(r('https://evil.example')), false);
  assert.equal(sameOrigin(r(null)), false);
});

// receiveRequest: 偽 D1 で分岐を確認(SQL 自体の確認は実 D1 の実機テストで行う)
function fakeDb({ dup = false, blocked = false, changes = 1 } = {}) {
  const calls = [];
  const mk = sql => ({ sql, args: [], bind(...a) { this.args = a; calls.push(this); return this; }, first: async () => (/FROM requests WHERE type/.test(sql) ? (dup ? { id: 1 } : null) : (/blocked_hosts/.test(sql) ? (blocked ? { x: 1 } : null) : null)), run: async () => ({ meta: { changes } }) });
  return { prepare: mk, calls };
}
const ctx = (body, db, { origin = 'https://mise.example', ct = 'application/json' } = {}) => ({
  req: { raw: new Request('https://mise.example/api/requests', { method: 'POST', headers: { Origin: origin, 'Content-Type': ct }, body: typeof body === 'string' ? body : JSON.stringify(body) }) },
  env: { DB: db },
});
const good = { type: 'add', url: 'https://example.com/', contact: 'me@example.com' };
test('receiveRequest: 受理 / 検証 / 向き', async () => {
  assert.equal((await receiveRequest(ctx(good, fakeDb()))).status, 202);
  assert.equal((await receiveRequest(ctx(good, fakeDb(), { origin: 'https://evil.example' }))).status, 403);
  assert.equal((await receiveRequest(ctx(good, fakeDb(), { ct: 'text/plain' }))).status, 415);
  assert.equal((await receiveRequest(ctx('{bad', fakeDb()))).status, 400);
  assert.equal((await receiveRequest(ctx({ ...good, url: 'http://127.0.0.1/' }, fakeDb()))).status, 400);
  assert.equal((await receiveRequest(ctx('x'.repeat(5000), fakeDb()))).status, 413);
  assert.equal((await receiveRequest(ctx(good, fakeDb({ changes: 0 })))).status, 429);
});
test('receiveRequest: 重複は積まず成功扱い / ハニーポットは保存しない', async () => {
  const d = fakeDb({ dup: true });
  assert.equal((await receiveRequest(ctx(good, d))).status, 202);
  assert.ok(!d.calls.some(c => /INSERT/.test(c.sql)));
  const h = fakeDb();
  assert.equal((await receiveRequest(ctx({ ...good, website: 'x' }, h))).status, 202);
  assert.equal(h.calls.length, 0);
});
test('receiveRequest: 拒否リスト済みホストへの推薦は rejected で記録', async () => {
  const d = fakeDb({ blocked: true });
  assert.equal((await receiveRequest(ctx(good, d))).status, 202);
  const ins = d.calls.find(c => /INSERT/.test(c.sql));
  assert.equal(ins.args[1], 'rejected'); assert.equal(ins.args[7], 'blocklisted');
  const d2 = fakeDb({ blocked: true });
  await receiveRequest(ctx({ ...good, type: 'remove' }, d2));
  assert.equal(d2.calls.find(c => /INSERT/.test(c.sql)).args[1], 'pending');
});
