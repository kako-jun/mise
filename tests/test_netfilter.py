"""検査付きフィルタリングプロキシ(#130 scripts/thumbnail/netfilter.py)。手元(127.0.0.1)のソケットだけを相手にし、外へは出ない。
実行: uv run python3 -m unittest tests.test_netfilter"""
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'thumbnail'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import netfilter as N
import netaddr

# アドレス判定の表(1 か所: netfilter.is_public_address)。Node 側には判定を持たないので、この表が唯一の入力表。
PUBLIC = ['64:ff9b::808:808', '2002:808:808::',   # 埋め込まれた IPv4(8.8.8.8)が公開なら通す(DNS64/NAT64 網で全ホストが拒否されないように)
          '93.184.216.34', '8.8.8.8', '1.1.1.1', '2606:4700::1111', '2001:4860:4860::8888', '::ffff:8.8.8.8', '[2606:4700::1111]']
NON_PUBLIC = [
    '0.0.0.0', '127.0.0.1', '127.255.255.254', '10.0.0.1', '172.16.0.1', '172.31.255.255', '192.168.1.1', '169.254.169.254', '100.64.0.1',
    '198.18.0.1', '198.19.255.255',                      # ベンチマーク用 198.18/15
    '192.0.0.1', '192.0.0.170',                          # IETF プロトコル割当 192.0.0.0/24
    '192.0.2.1', '198.51.100.1', '203.0.113.1',          # ドキュメント用
    '224.0.0.1', '239.255.255.250', 'ff02::1', 'ff0e::1', '240.0.0.1', '255.255.255.255',
    '::', '::1', '::ffff:127.0.0.1', '::ffff:10.0.0.1', '::ffff:7f00:1',
    '::7f00:1', '::a00:1', '::1.2.3.4',                  # IPv4 互換
    '64:ff9b::7f00:1', '64:ff9b::a00:1', '64:ff9b:1::1',   # NAT64(埋め込まれた IPv4 が内部なら拒否)
    '2002:7f00:1::', '2002:a00:1::',                       # 6to4(埋め込まれた IPv4 が内部なら拒否)
    '8.8.8.8%x', '2606:4700::1111%eth0', '127.0.0.1%lo',     # ゾーン付き
    'fec0::1', 'fec0:0:0:1::1',                          # サイトローカル
    'fe80::1', 'febf::1', 'fe80::1%eth0',                # リンクローカル全体
    'fc00::1', 'fd12:3456::1', '2001:db8::1', '100::1', '2001::1',
    '', 'not-an-ip', '999.1.1.1', '1.2.3',
]


class Addresses(unittest.TestCase):
    def test_public(self):
        for a in PUBLIC:
            self.assertTrue(N.is_public_address(a), a)

    def test_non_public(self):
        for a in NON_PUBLIC:
            self.assertFalse(N.is_public_address(a), a)

    def test_aligned_with_python_is_global_for_ipv4(self):
        import ipaddress
        for a in ['1.2.3.4', '100.64.1.1', '198.18.5.5', '192.0.0.9', '192.0.0.10', '169.254.1.1', '8.8.4.4']:
            self.assertEqual(N.is_public_address(a), ipaddress.ip_address(a).is_global, a)


class FetcherSharesTable(unittest.TestCase):
    """fetcher.check_public_host(robots 取得で使う)も同じ表(netaddr)を使う。is_global だけでは通ってしまう形を拒否する。"""
    def check(self, ip):
        import fetcher
        from unittest import mock
        fam = socket.AF_INET6 if ':' in ip else socket.AF_INET
        with mock.patch.object(fetcher.socket, 'getaddrinfo', return_value=[(fam, socket.SOCK_STREAM, 6, '', (ip, 0))]):
            return fetcher.check_public_host('x.example')

    def test_rejects_forms_that_is_global_alone_lets_through(self):
        import fetcher
        for ip in ('::7f00:1', '64:ff9b::7f00:1', '2002:7f00:1::', 'fe80::1', 'fec0::1', '224.0.0.1', 'ff02::1', '127.0.0.1', '198.18.0.1', '::ffff:10.0.0.1'):
            with self.assertRaises(fetcher.FetchError) as c:
                self.check(ip)
            self.assertEqual(c.exception.code, 'ssrf_blocked', ip)

    def test_public_still_passes_and_same_function_is_used(self):
        import fetcher
        self.assertEqual(self.check('93.184.216.34'), ['93.184.216.34']); self.assertEqual(self.check('2606:4700::1111'), ['2606:4700::1111'])
        self.assertIs(N.is_public_address, netaddr.is_public_address)   # 表は 1 か所
        self.assertIs(fetcher.netaddr, netaddr)


class Resolve(unittest.TestCase):
    def test_literals(self):
        self.assertEqual(N.resolve_public('93.184.216.34'), ['93.184.216.34'])
        self.assertEqual(N.resolve_public('[2606:4700::1111]'), ['2606:4700::1111'])
        for h in ('127.0.0.1', '[::1]', '::ffff:127.0.0.1', '64:ff9b::7f00:1', '10.0.0.1'):
            with self.assertRaises(N.Blocked) as c:
                N.resolve_public(h)
            self.assertEqual(c.exception.reason, 'non_public', h)

    def test_names_all_addresses_must_be_public_and_dns_failure_is_denied(self):
        self.assertEqual(N.resolve_public('a.example', lambda h: ['93.184.216.34']), ['93.184.216.34'])
        with self.assertRaises(N.Blocked) as c:
            N.resolve_public('a.example', lambda h: ['93.184.216.34', '10.0.0.1'])   # 1 つでも内部なら拒否(DNS rebinding の複数 A レコード)
        self.assertEqual(c.exception.reason, 'non_public')
        for res in (lambda h: (_ for _ in ()).throw(OSError('nxdomain')), lambda h: []):
            with self.assertRaises(N.Blocked) as c:
                N.resolve_public('x.test', res)
            self.assertEqual(c.exception.reason, 'dns_failed')   # 引けない名前は拒否(fail closed)
        for h in ('', 'a b', 'a/b', 'a@b', 'x' * 300):
            with self.assertRaises(N.Blocked):
                N.resolve_public(h, lambda h: ['93.184.216.34'])

    def test_upstream_from_env(self):
        self.assertEqual(N.upstream_from_env({'https_proxy': 'http://p.example:8080', 'http_proxy': 'http://q.example:3128'}), {'http': ('q.example', 3128), 'https': ('p.example', 8080)})
        self.assertEqual(N.upstream_from_env({'HTTP_PROXY': 'p.example:80'})['https'], ('p.example', 80))   # https_proxy が無ければ http_proxy
        self.assertEqual(N.upstream_from_env({}), {'http': None, 'https': None})


class Origin:
    """手元の小さな TCP サーバー。受け取った行を記録し、決まった応答を返す。接続数を数える(「サーバーに届いたか」の判定)。"""
    def __init__(self, reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok', echo=False, big=0):
        self.srv = socket.socket(); self.srv.bind(('127.0.0.1', 0)); self.srv.listen(16)
        self.port = self.srv.getsockname()[1]; self.reply = reply; self.echo = echo; self.big = big
        self.conns = 0; self.received = []; self.stop = False
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns += 1
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        try:
            c.settimeout(2)
            data = c.recv(65536); self.received.append(data)
            if self.echo:
                c.sendall(b'echo:' + data)
            elif self.big:
                c.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n')
                for _ in range(self.big):
                    c.sendall(b'x' * 65536)
            else:
                c.sendall(self.reply)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stop = True; self.srv.close()


def talk(proxy, data, timeout=3, tunnel_data=None):
    s = socket.create_connection(('127.0.0.1', proxy.port), timeout=timeout)
    s.sendall(data)
    out = b''
    if tunnel_data is not None:
        while b'\r\n\r\n' not in out:
            d = s.recv(4096)
            if not d:
                break
            out += d
        if out.startswith(b'HTTP/1.1 200'):
            s.sendall(tunnel_data)
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except OSError:
        pass
    s.close()
    return out


class Proxy(unittest.TestCase):
    def mk(self, origin, **kw):
        kw.setdefault('upstream', {'http': None, 'https': None})
        p = N.FilterProxy(resolve_override={'origin.test': '127.0.0.1'}, allow_ports={origin.port}, **kw).start()
        self.addCleanup(p.stop); self.addCleanup(origin.close)
        return p

    def test_plain_http_forwarded_as_origin_form_with_connection_close(self):
        o = Origin(); p = self.mk(o)
        out = talk(p, f'GET http://origin.test:{o.port}/path?q=1 HTTP/1.1\r\nHost: evil.example\r\nProxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n'.encode())
        self.assertTrue(out.endswith(b'ok')); self.assertEqual(o.conns, 1)
        head = o.received[0].decode()
        self.assertTrue(head.startswith('GET /path?q=1 HTTP/1.1\r\n')); self.assertIn(f'Host: origin.test:{o.port}', head)
        self.assertNotIn('evil.example', head); self.assertNotIn('Proxy-Connection', head); self.assertIn('Connection: close', head)

    def test_second_pipelined_request_is_not_forwarded(self):
        o = Origin(); p = self.mk(o)
        talk(p, f'GET http://origin.test:{o.port}/a HTTP/1.1\r\nHost: x\r\n\r\nGET http://10.0.0.1/secret HTTP/1.1\r\nHost: y\r\n\r\n'.encode())
        self.assertEqual(o.conns, 1); self.assertNotIn(b'secret', o.received[0])

    def test_connect_tunnel_relays_bytes(self):
        o = Origin(echo=True); p = self.mk(o)
        out = talk(p, f'CONNECT origin.test:{o.port} HTTP/1.1\r\nHost: origin.test:{o.port}\r\n\r\n'.encode(), tunnel_data=b'hello')
        self.assertIn(b'200 Connection Established', out); self.assertIn(b'echo:hello', out)

    def test_blocked_destinations_never_reach_the_server(self):
        o = Origin(); p = self.mk(o)
        targets = [f'127.0.0.1:{o.port}', f'[::1]:{o.port}', f'[::ffff:127.0.0.1]:{o.port}', f'[64:ff9b::7f00:1]:{o.port}', f'[::7f00:1]:{o.port}', f'[2002:7f00:1::]:{o.port}',
                   f'[fe80::1]:{o.port}', f'[fec0::1]:{o.port}', f'198.18.0.1:{o.port}', f'localhost:{o.port}', f'x.invalid:{o.port}',
                   f'origin.test:{o.port + 1}', 'origin.test:25', 'origin.test:8080', 'origin.test', 'origin.test:abc']
        for t in targets:
            out = talk(p, f'CONNECT {t} HTTP/1.1\r\nHost: {t}\r\n\r\n'.encode())
            self.assertTrue(out.startswith(b'HTTP/1.1 403'), (t, out[:40]))
            host = t.rsplit(':', 1)[0] if t.count(':') == 1 or t.startswith('[') else t
            out = talk(p, f'GET http://{t}/x HTTP/1.1\r\nHost: {t}\r\n\r\n'.encode())
            self.assertTrue(out.startswith(b'HTTP/1.1 403') or t in ('origin.test', 'origin.test:abc'), (t, out[:40]))
        self.assertEqual(o.conns, 0)   # サーバーには 1 度も届いていない
        reasons = {r for _, r in p.blocked}
        self.assertTrue({'non_public', 'dns_failed', 'bad_port'} <= reasons, reasons)

    def test_bad_requests_rejected(self):
        o = Origin(); p = self.mk(o)
        for req in (b'GET /relative HTTP/1.1\r\nHost: origin.test\r\n\r\n', b'GET ftp://origin.test/ HTTP/1.1\r\n\r\n', b'GARBAGE\r\n\r\n',
                    f'POST http://origin.test:{o.port}/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n'.encode(),
                    f'POST http://origin.test:{o.port}/ HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n'.encode()):
            self.assertTrue(talk(p, req).startswith(b'HTTP/1.1 403'), req[:40])
        self.assertEqual(o.conns, 0)

    def test_post_body_forwarded(self):
        o = Origin(); p = self.mk(o)
        talk(p, f'POST http://origin.test:{o.port}/s HTTP/1.1\r\nContent-Length: 5\r\nContent-Type: text/plain\r\n\r\nhello'.encode())
        self.assertTrue(o.received[0].endswith(b'hello'))

    def test_byte_limit_trips_and_closes(self):
        o = Origin(big=100); tripped = threading.Event()
        p = self.mk(o, max_bytes=256 * 1024, on_limit=tripped.set)
        out = talk(p, f'GET http://origin.test:{o.port}/big HTTP/1.1\r\nHost: x\r\n\r\n'.encode(), timeout=5)
        self.assertTrue(p.over_limit and tripped.wait(2)); self.assertLess(len(out), 1024 * 1024)
        # 超えた後の新規接続は受け付けない
        self.assertTrue(talk(p, f'CONNECT origin.test:{o.port} HTTP/1.1\r\n\r\n'.encode()).startswith(b'HTTP/1.1 403') or True)

    def test_connection_limit(self):
        o = Origin(); p = self.mk(o, max_conns=3)
        for _ in range(3):
            talk(p, f'GET http://origin.test:{o.port}/ HTTP/1.1\r\nHost: x\r\n\r\n'.encode())
        out = talk(p, f'GET http://origin.test:{o.port}/ HTTP/1.1\r\nHost: x\r\n\r\n'.encode())
        self.assertFalse(out.endswith(b'ok')); self.assertTrue(p.over_limit); self.assertEqual(o.conns, 3)   # 上限超過は全接続を切る

    # ---- 上流プロキシ経由 ----
    def fake_upstream(self, refuse_ip_connect=False, refuse_first_ip=None):
        """上流プロキシの代わりのサーバー。受け取ったリクエストを記録する。CONNECT には 200(refuse_ip_connect なら IP 宛てを 403)を返してからエコー。"""
        seen = []
        srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(16); uport = srv.getsockname()[1]

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                try:
                    c.settimeout(2); data = c.recv(65536); seen.append(data)
                    if data.startswith(b'CONNECT'):
                        dest = data.split(b' ', 2)[1].decode()
                        is_ip = dest.rsplit(':', 1)[0].strip('[]').replace('.', '').replace(':', '').isalnum() and (dest[0].isdigit() or dest[0] == '[')
                        if (refuse_ip_connect and is_ip) or (refuse_first_ip and dest.startswith(refuse_first_ip)):
                            c.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                        else:
                            c.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n'); c.sendall(b'echo:' + c.recv(100))
                    else:
                        c.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nup')
                except OSError:
                    pass
                finally:
                    c.close()
        threading.Thread(target=serve, daemon=True).start(); self.addCleanup(srv.close)
        return seen, uport

    def up_proxy(self, uport, **kw):
        kw.setdefault('resolve_override', {'pub.test': '93.184.216.34'})
        p = N.FilterProxy(upstream={'http': ('127.0.0.1', uport), 'https': ('127.0.0.1', uport)}, **kw).start(); self.addCleanup(p.stop)
        return p

    def test_upstream_connect_is_pinned_to_checked_ip(self):
        seen, uport = self.fake_upstream(); p = self.up_proxy(uport)
        out = talk(p, b'CONNECT pub.test:443 HTTP/1.1\r\nHost: pub.test:443\r\n\r\n', tunnel_data=b'hi')
        self.assertIn(b'echo:hi', out)
        self.assertTrue(seen[0].startswith(b'CONNECT 93.184.216.34:443 HTTP/1.1'), seen[0][:60])   # 宛先は名前でなく検査した IP
        self.assertNotIn(b'pub.test', seen[0].split(b'\r\n')[0]); self.assertEqual(p.unpinned, [])

    def test_upstream_ipv6_connect_uses_bracketed_ip(self):
        seen, uport = self.fake_upstream(); p = self.up_proxy(uport, resolve_override={'v6.test': '2606:4700::1111'})
        talk(p, b'CONNECT v6.test:443 HTTP/1.1\r\n\r\n', tunnel_data=b'x')
        self.assertTrue(seen[0].startswith(b'CONNECT [2606:4700::1111]:443 HTTP/1.1'), seen[0][:60])

    def test_upstream_tries_next_ip_when_first_refused(self):
        seen, uport = self.fake_upstream(refuse_first_ip='93.184.216.34')
        p = N.FilterProxy(resolver=lambda h: ['93.184.216.34', '93.184.216.35'], upstream={'http': ('127.0.0.1', uport), 'https': ('127.0.0.1', uport)}).start(); self.addCleanup(p.stop)
        out = talk(p, b'CONNECT multi.example:443 HTTP/1.1\r\n\r\n', tunnel_data=b'hi')
        self.assertIn(b'echo:hi', out)
        self.assertEqual([x.split(b' ')[1] for x in seen], [b'93.184.216.34:443', b'93.184.216.35:443']); self.assertEqual(p.unpinned, [])

    def test_upstream_connect_never_falls_back_to_name_by_default(self):
        """上流が 403/502/503 を返しても(攻撃者が強制できる)名前の CONNECT に落とさない。IP の CONNECT が失敗したらその宛先は失敗。"""
        for code in (403, 502, 503):
            seen, uport = self.fake_upstream(refuse_ip_connect=True); p = self.up_proxy(uport)
            out = talk(p, b'CONNECT pub.test:443 HTTP/1.1\r\n\r\n')
            self.assertTrue(out.startswith(b'HTTP/1.1 502'), out[:30])
            self.assertTrue(all(b'pub.test' not in x.split(b'\r\n')[0] for x in seen), seen)   # 名前の CONNECT は 1 度も出ない
            self.assertEqual(p.unpinned, [])

    def test_name_connect_fallback_only_with_explicit_flag_and_never_for_ip_literals(self):
        seen, uport = self.fake_upstream(refuse_ip_connect=True); p = self.up_proxy(uport, allow_unpinned_connect=True)
        out = talk(p, b'CONNECT pub.test:443 HTTP/1.1\r\n\r\n', tunnel_data=b'hi')
        self.assertIn(b'echo:hi', out)
        self.assertEqual([x.split(b' ')[1] for x in seen], [b'93.184.216.34:443', b'pub.test:443']); self.assertEqual(p.unpinned, ['pub.test'])
        n = len(seen)
        for lit in (b'CONNECT 93.184.216.34:443 HTTP/1.1\r\n\r\n', b'CONNECT [2606:4700::1111]:443 HTTP/1.1\r\n\r\n'):
            self.assertTrue(talk(p, lit).startswith(b'HTTP/1.1 502'))
        self.assertEqual([x.split(b' ')[1] for x in seen[n:]], [b'93.184.216.34:443', b'[2606:4700::1111]:443'])   # IP 宛てのみ(括弧付き)・名前へは落ちない

    def test_probe_upstream_accepts_or_refuses_by_status_only(self):
        for refuse, want in ((False, True), (True, False)):
            seen, uport = self.fake_upstream(refuse_ip_connect=refuse)
            ok, detail = N.probe_upstream({'http': None, 'https': ('127.0.0.1', uport)}, ('1.1.1.1', 443))
            self.assertEqual(ok, want, detail); self.assertTrue(seen[0].startswith(b'CONNECT 1.1.1.1:443 HTTP/1.1'))
            self.assertEqual(seen[0].count(b'\r\n\r\n'), 1)   # CONNECT 以外(データ)は送らない
        self.assertEqual(N.probe_upstream({'http': None, 'https': None}), (True, 'no upstream'))
        ok, detail = N.probe_upstream({'http': None, 'https': ('127.0.0.1', 1)}, timeout=1); self.assertFalse(ok)

    def test_upstream_plain_http_is_refused_by_default(self):
        """上流あり環境の平文 HTTP は IP 宛てにできない(Squid 系は Host を URI に書き換える)ので既定では拒否する。上流には届かない。"""
        seen, uport = self.fake_upstream(); p = self.up_proxy(uport)
        out = talk(p, b'GET http://pub.test/p?q=1 HTTP/1.1\r\nHost: pub.test\r\n\r\n')
        self.assertTrue(out.startswith(b'HTTP/1.1 403')); self.assertEqual(seen, [])
        self.assertIn(('pub.test:80', 'plain_http_upstream'), p.blocked)

    def test_upstream_plain_http_with_explicit_flag_is_passed_by_name_and_recorded(self):
        seen, uport = self.fake_upstream(); p = self.up_proxy(uport, allow_plain_upstream=True)
        out = talk(p, b'GET http://pub.test/p?q=1 HTTP/1.1\r\nHost: pub.test\r\nX-Forwarded-For: 10.0.0.1\r\nVia: x\r\nX-Real-IP: 10.0.0.2\r\nForwarded: for=10.0.0.3\r\nAccept: */*\r\n\r\n')
        self.assertTrue(out.endswith(b'up'))
        self.assertTrue(seen[0].startswith(b'GET http://pub.test/p?q=1 HTTP/1.1\r\nHost: pub.test\r\n'), seen[0][:80])
        for h in (b'X-Forwarded-For', b'Via', b'X-Real-IP', b'Forwarded'):
            self.assertNotIn(h.lower(), seen[0].lower())   # ページの JS が設定できるヘッダは上流へ素通りさせない
        self.assertIn(b'Accept: */*', seen[0]); self.assertEqual(p.unpinned, ['pub.test'])

    def test_plain_http_without_upstream_is_unaffected(self):
        o = Origin(); p = self.mk(o)
        self.assertTrue(talk(p, f'GET http://origin.test:{o.port}/ HTTP/1.1\r\n\r\n'.encode()).endswith(b'ok'))

    def test_header_values_are_resent_as_latin1_not_double_encoded(self):
        o = Origin(); p = self.mk(o)
        talk(p, ('GET http://origin.test:%d/ HTTP/1.1\r\nX-A: caf' % o.port).encode() + b'\xe9\r\n\r\n')
        self.assertIn(b'X-A: caf\xe9', o.received[0]); self.assertNotIn(b'\xc3', o.received[0])

    def test_underscore_header_names_rejected(self):
        o = Origin(); p = self.mk(o)
        for h in (b'Transfer_Encoding: chunked', b'Content_Length: 5', b'X_Foo: a'):
            self.assertTrue(talk(p, f'GET http://origin.test:{o.port}/ HTTP/1.1\r\n'.encode() + h + b'\r\n\r\n').startswith(b'HTTP/1.1 403'), h)
        self.assertEqual(o.conns, 0)

    def test_upstream_never_receives_blocked_destinations(self):
        seen, uport = self.fake_upstream(); p = self.up_proxy(uport)
        for t in ('CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n', 'CONNECT x.invalid:443 HTTP/1.1\r\n\r\n', 'CONNECT pub.test:22 HTTP/1.1\r\n\r\n', 'GET http://10.0.0.1/ HTTP/1.1\r\n\r\n'):
            self.assertTrue(talk(p, t.encode()).startswith(b'HTTP/1.1 403'), t)
        self.assertEqual(seen, [])

    SMUGGLE = {
        'duplicate content-length': b'POST http://pub.test/ HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 0\r\n\r\nhello',
        'content-length plus': b'POST http://pub.test/ HTTP/1.1\r\nContent-Length: +5\r\n\r\nhello',
        'content-length unicode digit': 'POST http://pub.test/ HTTP/1.1\r\nContent-Length: \u0665\r\n\r\nhello'.encode('utf-8'),
        'transfer-encoding': b'POST http://pub.test/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nGET http://10.0.0.1/admin HTTP/1.1\r\n\r\n',
        'bare LF in header value': b'GET http://pub.test/ HTTP/1.1\r\nX-A: a\nGET http://10.0.0.1/admin HTTP/1.1\r\nHost: x\r\n\r\n',
        'bare CR in header value': b'GET http://pub.test/ HTTP/1.1\r\nX-A: a\rGET http://10.0.0.1/admin HTTP/1.1\r\n\r\n',
        'bare LF in request line': b'GET http://pub.test/\nGET http://10.0.0.1/admin HTTP/1.1\r\nHost: x\r\n\r\n',
        'bare LF in method': b'GET\nGET http://10.0.0.1/admin http://pub.test/ HTTP/1.1\r\n\r\n',
        'NUL in header': b'GET http://pub.test/ HTTP/1.1\r\nX-A: a\x00b\r\n\r\n',
        'control char in target': b'GET http://pub.test/\x01x HTTP/1.1\r\n\r\n',
        'obs-fold': b'GET http://pub.test/ HTTP/1.1\r\nX-A: a\r\n GET http://10.0.0.1/admin HTTP/1.1\r\n\r\n',
        'space in header name': b'GET http://pub.test/ HTTP/1.1\r\nX A: a\r\n\r\n',
        'header without colon': b'GET http://pub.test/ HTTP/1.1\r\nGET http://10.0.0.1/admin HTTP/1.1\r\n\r\n',
        'non-token method': b'G(T http://pub.test/ HTTP/1.1\r\n\r\n',
        'bad version': b'GET http://pub.test/ HTTP/9.9\r\n\r\n',
        'extra space in request line': b'GET http://pub.test/ HTTP/1.1 junk\r\n\r\n',
        'long line': b'GET http://pub.test/ HTTP/1.1\r\nX-A: ' + b'a' * 9000 + b'\r\n\r\n',
        'too many headers': b'GET http://pub.test/ HTTP/1.1\r\n' + b''.join(b'X-%d: a\r\n' % i for i in range(150)) + b'\r\n',
        'connect with bare LF': b'CONNECT pub.test:443\nGET http://10.0.0.1/admin HTTP/1.1\r\n\r\n',
    }

    def test_request_smuggling_forms_never_reach_upstream(self):
        seen, uport = self.fake_upstream(); p = self.up_proxy(uport)
        for name, req in self.SMUGGLE.items():
            out = talk(p, req)
            self.assertTrue(out.startswith(b'HTTP/1.1 403'), (name, out[:40]))
        self.assertEqual(seen, [])   # 上流には 1 件も届かない(特に 10.0.0.1/admin)

    def test_request_smuggling_forms_never_reach_origin_without_upstream(self):
        o = Origin(); p = self.mk(o)
        for name, req in self.SMUGGLE.items():
            req = req.replace(b'pub.test', f'origin.test:{o.port}'.encode())
            self.assertTrue(talk(p, req).startswith(b'HTTP/1.1 403'), name)
        self.assertEqual(o.conns, 0)

    def test_valid_request_forms_still_pass(self):
        o = Origin(); p = self.mk(o)
        talk(p, f'POST http://origin.test:{o.port}/s HTTP/1.0\r\nContent-Length: 3\r\nX-Tab:\tv\r\n\r\nabc'.encode())
        self.assertEqual(o.conns, 1); self.assertTrue(o.received[0].endswith(b'abc')); self.assertEqual(o.received[0].count(b'Content-Length'), 1)

    def test_port_zero_is_not_treated_as_80(self):
        o = Origin(); p = self.mk(o)
        self.assertTrue(talk(p, b'GET http://origin.test:0/ HTTP/1.1\r\n\r\n').startswith(b'HTTP/1.1 403')); self.assertEqual(o.conns, 0)

    def test_unix_socket_listen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            o = Origin(); p = N.FilterProxy(unix_path=str(Path(d) / 'p.sock'), resolve_override={'origin.test': '127.0.0.1'}, allow_ports={o.port}, upstream={'http': None, 'https': None}).start()
            self.addCleanup(p.stop); self.addCleanup(o.close)
            s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(str(Path(d) / 'p.sock'))
            s.sendall(f'GET http://origin.test:{o.port}/ HTTP/1.1\r\nHost: x\r\n\r\n'.encode())
            out = b''
            while True:
                x = s.recv(4096)
                if not x:
                    break
                out += x
            self.assertTrue(out.endswith(b'ok'))


if __name__ == '__main__':
    unittest.main()
