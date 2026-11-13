import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeUrl, normalizeContact, validateRequest, ipHash, ipPrefix, blockCandidates, isBlocked, matchPath, decodeUnreserved, readLimited, sameOrigin, receiveRequest, LIMITS } from '../src/intake.js';

test('normalizeUrl: 正規化', () => {
  assert.deepEqual(normalizeUrl('Example.COM/foo/?a=1#x'), { ok: true, url: 'https://example.com/foo', host: 'example.com', path: '/foo', key: 'example.com/foo' });
  assert.equal(normalizeUrl('http://blog.example.jp/').url, 'http://blog.example.jp');
});
test('normalizeUrl: SSRF になりうる宛先・形式を拒否', () => {
  for (const bad of ['http://127.0.0.1/', 'http://localhost/', 'https://10.0.0.1/', 'http://[::1]/', 'http://2130706433/', 'http://0x7f.1/', 'https://intranet/', 'https://a.local/', 'https://a.internal/', 'https://user:pw@example.com/', 'https://example.com:8443/', 'ftp://example.com/', 'javascript:alert(1)', 'file:///etc/passwd', 'https://exa mple.com/', '', 'https://' + 'a'.repeat(600) + '.com'])
    assert.equal(normalizeUrl(bad).ok, false, bad);
});
test('normalizeContact: メールか https URL のみ', () => {
  assert.equal(normalizeContact('me@example.com'), 'me@example.com');
  assert.equal(normalizeContact('https://www.facebook.com/profile.php?id=123'), 'https://www.facebook.com/profile.php?id=123'); // クエリを保つ
  for (const bad of ['', 'x', 'http://example.com/', 'a b@example.com', '<a@b.com>', 'https://example.com/a"b', 'https://example.com/a b', "https://example.com/'", 'https://example.com/<x>', 'javascript:alert(1)', 'a'.repeat(250) + '@example.com']) assert.equal(normalizeContact(bad), null, bad);
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

test('normalizeUrl: 比較キーは http/https・www.・末尾スラッシュ・大文字小文字を同一視', () => {
  const k = u => normalizeUrl(u).key;
  const base = k('https://example.com/Blog');
  for (const u of ['http://www.example.com/blog/', 'HTTPS://EXAMPLE.COM:443/blog', 'example.com/Blog/?x=1']) assert.equal(k(u), base, u);
  assert.notEqual(k('https://example.com/other'), base);
});
test('blockCandidates: 登録ドメイン配下を含み、共有ホスティングは親に広げない', () => {
  assert.deepEqual(blockCandidates('www.a.example.com'), ['a.example.com', 'example.com']);
  assert.deepEqual(blockCandidates('example.com'), ['example.com']);
  assert.deepEqual(blockCandidates('user.github.io'), ['user.github.io']);
  assert.deepEqual(blockCandidates('x.user.github.io'), ['x.user.github.io', 'user.github.io']);
});
test('ipPrefix: IPv6 は /64', () => {
  assert.equal(ipPrefix('203.0.113.9'), '203.0.113.9');
  assert.equal(ipPrefix('2001:db8:1:2:aaaa:bbbb:cccc:dddd'), ipPrefix('2001:0db8:0001:0002::1'));
  assert.notEqual(ipPrefix('2001:db8:1:2::1'), ipPrefix('2001:db8:1:3::1'));
  assert.equal(ipPrefix('::1'), '0:0:0:0');
});
test('readLimited: Content-Length と実バイト数の両方で上限', async () => {
  const mk = (body, h = {}) => new Request('https://m.example/x', { method: 'POST', headers: h, body });
  assert.equal(await readLimited(mk('{"a":1}')), '{"a":1}');
  assert.equal(await readLimited(mk('x'.repeat(9000))), null);
  assert.equal(await readLimited(mk('x', { 'Content-Length': '99999' })), null);
  assert.equal(await readLimited(mk('あ'.repeat(3000))), null); // 文字数 3000 は 9000 バイト
  assert.equal((await readLimited(mk('あ'.repeat(1500)))).length, 1500); // 4500 バイトは通る
});

// receiveRequest: 偽 D1 で分岐を確認(SQL 自体の確認は実 D1 の実機テストで行う)
function fakeDb({ blocked = false, changes = 1, dupAfter = false } = {}) {
  const calls = [];
  const mk = sql => ({ sql, args: [], bind(...a) { this.args = a; calls.push(this); return this; },
    all: async () => ({ results: blocked ? (Array.isArray(blocked) ? blocked : [{ host: 'example.com', path_prefix: '' }]) : [] }),
    first: async () => (dupAfter ? { x: 1 } : null),
    run: async () => ({ meta: { changes } }) });
  return { prepare: mk, calls };
}
const ctx = (body, db, { origin = 'https://mise.example', ct = 'application/json', salt = 'salt', ip = '203.0.113.9' } = {}) => ({
  req: { raw: new Request('https://mise.example/api/requests', { method: 'POST', headers: { Origin: origin, 'Content-Type': ct, 'CF-Connecting-IP': ip }, body: typeof body === 'string' ? body : JSON.stringify(body) }) },
  env: { DB: db, INTAKE_SALT: salt },
});
const good = { type: 'add', url: 'https://example.com/', contact: 'me@example.com' };
const ins = d => d.calls.find(c => /INSERT/.test(c.sql));
test('receiveRequest: 受理 / 検証 / 向き', async () => {
  assert.equal((await receiveRequest(ctx(good, fakeDb()))).status, 202);
  assert.equal((await receiveRequest(ctx(good, fakeDb(), { origin: 'https://evil.example' }))).status, 403);
  assert.equal((await receiveRequest(ctx(good, fakeDb(), { ct: 'text/plain' }))).status, 415);
  assert.equal((await receiveRequest(ctx(good, fakeDb(), { ct: 'application/json; charset=utf-8' }))).status, 202);
  assert.equal((await receiveRequest(ctx(good, fakeDb(), { ct: 'application/jsonx' }))).status, 415);
  assert.equal((await receiveRequest(ctx('{bad', fakeDb()))).status, 400);
  assert.equal((await receiveRequest(ctx({ ...good, url: 'http://127.0.0.1/' }, fakeDb()))).status, 400);
  assert.equal((await receiveRequest(ctx('x'.repeat(9000), fakeDb()))).status, 413);
});
test('receiveRequest: INTAKE_SALT 未設定なら 503 で何も保存しない', async () => {
  const d = fakeDb();
  assert.equal((await receiveRequest(ctx(good, d, { salt: '' }))).status, 503);
  assert.equal(d.calls.length, 0);
});
test('receiveRequest: 挿入されない場合は、同一IPの再送なら成功扱い、そうでなければ 429', async () => {
  assert.equal((await receiveRequest(ctx(good, fakeDb({ changes: 0, dupAfter: true })))).status, 202);
  assert.equal((await receiveRequest(ctx(good, fakeDb({ changes: 0 })))).status, 429);
});
test('receiveRequest: 同一URLでも別IPなら握りつぶさず保存(重複判定は ip_hash を含む)', async () => {
  const a = fakeDb(), b = fakeDb();
  await receiveRequest(ctx({ ...good, type: 'remove' }, a, { ip: '203.0.113.1' }));
  await receiveRequest(ctx({ ...good, type: 'remove', contact: 'real@example.com' }, b, { ip: '203.0.113.2' }));
  assert.match(ins(a).sql, /NOT EXISTS \(SELECT 1 FROM requests WHERE ip_hash=\?8 AND type=\?1 AND normalized_key=\?5/);
  assert.notEqual(ins(a).args[7], ins(b).args[7]);
  assert.equal(ins(b).args[6], 'real@example.com');
});
test('receiveRequest: 全体上限は推薦と取り下げ・訂正で別枠', async () => {
  const a = fakeDb(), r = fakeDb();
  await receiveRequest(ctx(good, a)); await receiveRequest(ctx({ ...good, type: 'remove' }, r));
  assert.deepEqual([ins(a).args[11], ins(a).args[12], ins(a).args[14]], [1, LIMITS.addPerDay, LIMITS.netPerDay]);
  assert.deepEqual([ins(r).args[11], ins(r).args[12]], [0, LIMITS.otherPerDay]);
});
test('receiveRequest: ハニーポットは保存しない', async () => {
  const h = fakeDb();
  assert.equal((await receiveRequest(ctx({ ...good, website: 'x' }, h))).status, 202);
  assert.equal(h.calls.length, 0);
});
test('receiveRequest: 拒否リスト済みへの推薦は rejected で連絡先を保存しない(親ドメイン・www. も照合)', async () => {
  const d = fakeDb({ blocked: true });
  assert.equal((await receiveRequest(ctx({ ...good, url: 'https://www.sub.example.com/x' }, d))).status, 202);
  const q = d.calls.find(c => /blocked_hosts/.test(c.sql));
  assert.deepEqual(q.args, ['sub.example.com', 'example.com']);
  assert.equal(ins(d).args[1], 'rejected'); assert.equal(ins(d).args[6], null); assert.equal(ins(d).args[8], 'blocklisted');
  const d2 = fakeDb({ blocked: true });
  await receiveRequest(ctx({ ...good, type: 'remove' }, d2));
  assert.equal(ins(d2).args[1], 'pending'); assert.equal(ins(d2).args[6], 'me@example.com');
});

test('normalizeUrl: スキーム省略+ポート、%7E と ~、ゼロ幅文字', () => {
  assert.equal(normalizeUrl('example.com:443').url, 'https://example.com');
  assert.equal(normalizeUrl('example.com:8080').ok, false);
  assert.equal(normalizeUrl('javascript:alert(1)').ok, false);
  assert.equal(normalizeUrl('https://sakura.ne.jp/%7Ebob/').key, normalizeUrl('https://sakura.ne.jp/~bob').key);
  assert.equal(decodeUnreserved('/%41%2f%7e%e3'), '/A%2F~%E3');
  assert.equal(normalizeUrl('exa\u200bmple.com\ufeff').url, 'https://example.com');
});
test('ipPrefix: IPv4 射影は IPv4 に、/48 も', () => {
  assert.equal(ipPrefix('::ffff:203.0.113.9'), '203.0.113.9');
  assert.equal(ipPrefix('::ffff:cb00:7109'), '203.0.113.9');
  assert.equal(ipPrefix('2001:db8:1:2::1', 3), ipPrefix('2001:db8:1:9::5', 3));
  assert.notEqual(ipPrefix('2001:db8:1:2::1', 3), ipPrefix('2001:db8:2:2::1', 3));
});
test('isBlocked: パス型は host 全体を拒否しない', () => {
  const rows = [{ host: 'ameblo.jp', path_prefix: '/foo' }];
  assert.equal(isBlocked(rows, 'ameblo.jp', '/foo'), true);
  assert.equal(isBlocked(rows, 'www.ameblo.jp', '/foo/entry-1'), true);
  assert.equal(isBlocked(rows, 'ameblo.jp', '/foobar'), false);
  assert.equal(isBlocked(rows, 'ameblo.jp', '/bar'), false);
  assert.equal(isBlocked(rows, 'ameblo.jp', ''), false);
  assert.equal(isBlocked([{ host: 'example.com', path_prefix: '' }], 'a.example.com', '/x'), true);
});
test('receiveRequest: パス型の拒否はそのパス配下だけ rejected', async () => {
  const rows = [{ host: 'ameblo.jp', path_prefix: '/foo' }];
  const d1 = fakeDb({ blocked: rows }); await receiveRequest(ctx({ ...good, url: 'https://ameblo.jp/foo/entry' }, d1));
  assert.equal(ins(d1).args[1], 'rejected');
  const d2 = fakeDb({ blocked: rows }); await receiveRequest(ctx({ ...good, url: 'https://ameblo.jp/bar' }, d2));
  assert.equal(ins(d2).args[1], 'pending');
});
test('receiveRequest: 重複判定は連絡先も含む(連絡先違いは別行)', async () => {
  const d = fakeDb(); await receiveRequest(ctx(good, d));
  assert.match(ins(d).sql, /contact IS \?7 AND status='pending'/);
});

test('パス型の拒否のすり抜け(//foo, %2F, ;x, %20, 末尾.)を畳んで防ぐ', () => {
  const rows = [{ host: 'ameblo.jp', path_prefix: '/foo' }];
  for (const u of ['https://ameblo.jp//foo', 'https://ameblo.jp/foo%2fbar', 'https://ameblo.jp/foo%2F', 'https://ameblo.jp/foo;x', 'https://ameblo.jp/foo%20', 'https://ameblo.jp/foo.', 'https://ameblo.jp/foo/./bar', 'https://ameblo.jp/x/../foo/y', 'https://ameblo.jp/%2e/foo', 'https://ameblo.jp/foo/;a/b', 'https://ameblo.jp/FOO/']) {
    const n = normalizeUrl(u);
    assert.equal(isBlocked(rows, n.host, n.path), true, u);
  }
  for (const u of ['https://ameblo.jp/foobar', 'https://ameblo.jp/bar/foo', 'https://s.ameblo.jp/foo']) {
    const n = normalizeUrl(u);
    assert.equal(isBlocked(rows, n.host, n.path), false, u);
  }
  assert.equal(matchPath('/a%2Fb;x/c.'), '/a/b/c');
  assert.equal(normalizeUrl('https://ameblo.jp//foo/').key, normalizeUrl('https://ameblo.jp/foo').key);
});
test('共有ホスティングの追加分と見えない文字・連絡先の長さ・長い ::ffff:', () => {
  for (const h of ['x.hateblo.jp', 'x.jugem.jp', 'x.exblog.jp', 'x.seesaa.net', 'x.hatenadiary.org']) assert.deepEqual(blockCandidates(h), [h], h);
  for (const ch of ['\u00ad', '\u061c', '\u180e', '\u2061', '\u2064', '\u200b', '\ufeff']) assert.equal(normalizeUrl(`exa${ch}mple.com`).url, 'https://example.com');
  assert.equal(normalizeContact('https://example.com/' + 'a'.repeat(170)), 'https://example.com/' + 'a'.repeat(170));
  assert.equal(normalizeContact('https://example.com/' + 'a'.repeat(150) + '\u00e9'.repeat(9)), null); // 文字数 200 以内でも href(%エンコード)が 200 を超える
  assert.equal(ipPrefix('0:0:0:0:0:ffff:203.0.113.9'), '203.0.113.9');
  assert.equal(ipPrefix('0000:0:0:0:0:ffff:cb00:7109'), '203.0.113.9');
});
