"""サムネイル撮影用の検査付きフィルタリングプロキシ(#130)。標準ライブラリのみ。

ブラウザの全通信(ワーカー・ポップアップ・iframe・WebSocket・WebRTC・prefetch を含む)をここへ通す。ブラウザ自身には名前解決も外への経路も持たせない。
ここが宛先を検査して通すか止めるかを決める「唯一の関所」で、**アドレス判定の表(is_public_address)もこの 1 か所だけ**(Node 側には判定を持たない)。

検査(CONNECT の宛先も平文 HTTP の宛先も同じ):
  - ポートは 80/443 だけ(テスト用の追加ポートは allow_ports で明示したときだけ)
  - ホスト名は自分で名前解決し、**全アドレスが公開アドレス**であること。引けない名前は拒否(fail closed)。IP リテラルは直接検査
  - 上流プロキシが無い環境では、検査した IP に固定して接続する(DNS リバインディングも塞ぐ)
  - 上流プロキシ(環境変数 http_proxy / https_proxy)がある環境では、そこへ連鎖する。**名前解決は上流が行うため完全には固定できない**。ここでの検査はローカルの名前解決に基づく最善努力
    (ローカルで引けない内部名は拒否するので、上流でだけ引ける内部名はそもそも通らない。ローカルと上流で答えが食い違う名前が残余リスク)
  - 1 本の接続につき 1 リクエスト(Connection: close)。同じ接続で別ホスト宛の 2 本目を通さない
  - 総受信バイト数・接続数に上限。超えたら全接続を切り over_limit を立てる(呼び出し側がページを閉じる)
"""
import asyncio
import ipaddress
import os
import socket
import threading
import urllib.parse

ALLOWED_PORTS = (80, 443)
MAX_BYTES = 48 * 1024 * 1024       # 1 ページで受け取る総バイト数(試行 20 件の最初の画面は 2〜32MB。暴走・無限ストリームを止める上限であって通常のページを止めない値)
MAX_CONNS = 300                    # 1 ページで受け付ける接続数
MAX_HEAD = 64 * 1024
MAX_BODY = 1024 * 1024             # 平文 HTTP のリクエスト本文の上限
IO_TIMEOUT = 60

# is_global が true を返しても通さない範囲(IPv4 を埋め込む移行用アドレス・非推奨の範囲。マルチキャストは is_public_address で除く)。
# is_global が既に false にする範囲(198.18/15・192.0.0/24・ドキュメント用・100.64/10・ループバック・リンクローカル・ULA 等)はここに重ねて書かない。
_EXTRA_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    '::/96',          # IPv4 互換(::7f00:1 など。非推奨だが一部の実装は IPv4 として扱う)
    '64:ff9b::/96',   # NAT64(IPv4 を埋め込む)
    '64:ff9b:1::/48',
    '2002::/16',      # 6to4(IPv4 を埋め込む)
    'fec0::/10',      # サイトローカル(非推奨)
    'fe80::/10',      # リンクローカル全体
    '100::/64',
))


def is_public_address(value):
    """文字列のアドレスが公開アドレスなら True。判定は Python の is_global に揃え、IPv4 を埋め込む IPv6 は埋め込まれた側も見る。"""
    try:
        ip = ipaddress.ip_address(str(value).strip().strip('[]').split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        if any(ip in n for n in _EXTRA_BLOCKED_V6):
            return False
    if ip.is_multicast or ip.is_unspecified:   # is_global はマルチキャストを真と返すことがあるので明示的に除く
        return False
    return bool(ip.is_global)


class Blocked(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(f'{reason}: {detail}'); self.reason = reason; self.detail = detail


def resolve_public(host, resolver=None):
    """ホスト名(または IP リテラル)を解決し、全アドレスが公開アドレスなら IP のリストを返す。それ以外は Blocked。"""
    h = host.strip().strip('[]')
    if not h or len(h) > 253 or any(c in h for c in ' /\\@\x00'):
        raise Blocked('bad_host', host)
    try:
        ipaddress.ip_address(h.split('%', 1)[0])
        literal = True
    except ValueError:
        literal = False
    if literal:
        if not is_public_address(h):
            raise Blocked('non_public', h)
        return [h]
    try:
        ips = (resolver or _system_resolve)(h)
    except OSError as e:
        raise Blocked('dns_failed', f'{h}: {e}')   # 引けない名前は拒否(上流でだけ引ける内部名を通さない)
    if not ips:
        raise Blocked('dns_failed', h)
    for ip in ips:
        if not is_public_address(ip):
            raise Blocked('non_public', f'{h} -> {ip}')
    return ips


def _system_resolve(host):
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos}, key=lambda a: (':' in a, a))   # IPv4 優先


def upstream_from_env(env=None):
    """環境変数の上流プロキシ。戻り: {'http': (host, port)|None, 'https': (host, port)|None}。認証は扱わない(認証不要前提)。"""
    env = os.environ if env is None else env
    out = {}
    for scheme in ('http', 'https'):
        v = env.get(f'{scheme}_proxy') or env.get(f'{scheme.upper()}_PROXY') or (env.get('http_proxy') or env.get('HTTP_PROXY') if scheme == 'https' else None)
        if not v:
            out[scheme] = None; continue
        p = urllib.parse.urlsplit(v if '://' in v else 'http://' + v)
        out[scheme] = (p.hostname, p.port or 80) if p.hostname else None
    return out


def _split_hostport(target):
    """CONNECT の宛先 host:port(IPv6 は [::1]:443)。ポートが無い・数字でないなら Blocked。"""
    t = target.strip()
    if t.startswith('['):
        host, _, rest = t[1:].partition(']')
        port = rest[1:] if rest.startswith(':') else ''
    elif t.count(':') == 1:
        host, _, port = t.partition(':')
    else:
        host, port = t, ''
    if not port.isdigit():
        raise Blocked('bad_port', target)
    return host, int(port)


class FilterProxy:
    """1 回の撮影につき 1 つ起動する。unix_path があれば Unix ソケット、無ければ 127.0.0.1 の空きポートで待ち受ける。
    resolve_override / allow_ports はテスト専用(selftest・単体テストが手元のサーバーを相手にするため)。本番では指定しない。"""
    def __init__(self, unix_path=None, upstream=None, max_bytes=MAX_BYTES, max_conns=MAX_CONNS, resolver=None,
                 resolve_override=None, allow_ports=(), on_limit=None):
        self.unix_path = unix_path
        self.upstream = upstream_from_env() if upstream is None else upstream
        self.max_bytes = max_bytes; self.max_conns = max_conns
        self.resolver = resolver; self.resolve_override = dict(resolve_override or {}); self.allow_ports = set(ALLOWED_PORTS) | set(allow_ports)
        self.on_limit = on_limit
        self.bytes = 0; self.conns = 0; self.over_limit = False
        self.blocked = []      # [(host:port, reason)]
        self.forwarded = []    # [host:port]
        self.port = None
        self._loop = None; self._server = None; self._thread = None; self._writers = set(); self._ready = threading.Event(); self._err = None

    # ---- 起動・停止 ----
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        self._ready.wait(10)
        if self._err:
            raise self._err
        return self

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def _run(self):
        self._loop = asyncio.new_event_loop(); asyncio.set_event_loop(self._loop)
        try:
            if self.unix_path:
                self._server = self._loop.run_until_complete(asyncio.start_unix_server(self._handle, path=self.unix_path))
            else:
                self._server = self._loop.run_until_complete(asyncio.start_server(self._handle, '127.0.0.1', 0))
                self.port = self._server.sockets[0].getsockname()[1]
        except Exception as e:
            self._err = e; self._ready.set(); return
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for t in pending:
            t.cancel()
        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def stop(self):
        if not self._loop:
            return
        def shut():
            if self._server:
                self._server.close()
            for w in list(self._writers):
                w.close()
            self._loop.call_later(0.1, self._loop.stop)
        self._loop.call_soon_threadsafe(shut)
        self._thread.join(5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    def _trip(self):
        if self.over_limit:
            return
        self.over_limit = True
        for w in list(self._writers):
            w.close()
        if self.on_limit:
            self.on_limit()

    # ---- 検査 ----
    async def _check(self, host, port):
        if port not in self.allow_ports:
            raise Blocked('bad_port', f'{host}:{port}')
        h = host.strip().strip('[]').lower()
        if h in self.resolve_override:
            return [self.resolve_override[h]]
        # 名前解決はブロッキングなので別スレッドで(IP リテラルの検査も同じ関数)
        return await asyncio.get_running_loop().run_in_executor(None, resolve_public, h, self.resolver)

    # ---- 接続の処理 ----
    async def _handle(self, r, w):
        self._writers.add(w)
        try:
            await self._serve(r, w)
        except Blocked as b:
            self.blocked.append((b.detail or b.reason, b.reason))
            await self._reply(w, 403, 'blocked: ' + b.reason)
        except (asyncio.TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:
            pass
        finally:
            self._writers.discard(w)
            try:
                w.close()
            except Exception:
                pass

    async def _reply(self, w, status, text):
        try:
            body = text.encode()
            w.write(f'HTTP/1.1 {status} {text[:40]}\r\nContent-Length: {len(body)}\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\n'.encode() + body)
            await w.drain()
        except Exception:
            pass

    async def _read_head(self, r):
        data = await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), IO_TIMEOUT)
        if len(data) > MAX_HEAD:
            raise Blocked('bad_request', 'head too large')
        return data

    async def _serve(self, r, w):
        if self.conns >= self.max_conns:
            self._trip()
        if self.over_limit:
            raise Blocked('limit', 'over limit')
        self.conns += 1
        head = await self._read_head(r)
        lines = head[:-4].decode('iso-8859-1').split('\r\n')
        try:
            method, target, _ver = lines[0].split(' ', 2)
        except ValueError:
            raise Blocked('bad_request', lines[0][:60])
        headers = []
        for ln in lines[1:]:
            k, _, v = ln.partition(':')
            headers.append((k.strip(), v.strip()))
        if method.upper() == 'CONNECT':
            host, port = _split_hostport(target)
            ips = await self._check(host, port)
            await self._tunnel(r, w, host, port, ips)
        else:
            p = urllib.parse.urlsplit(target)
            if p.scheme != 'http' or not p.hostname:
                raise Blocked('bad_request', target[:80])
            try:
                port = p.port or 80
            except ValueError:
                raise Blocked('bad_port', target[:80])
            ips = await self._check(p.hostname, port)
            await self._plain(r, w, method, p, port, headers, ips)

    async def _open(self, scheme_key, host, port, ips):
        """宛先へ接続する。上流プロキシがあればそこへ(名前は上流が解決)、無ければ検査した IP に固定して。戻り: (reader, writer, via_upstream)。"""
        up = self.upstream.get(scheme_key)
        if up:
            rr, ww = await asyncio.wait_for(asyncio.open_connection(*up), 15)
            return rr, ww, True
        last = None
        for ip in ips:
            try:
                rr, ww = await asyncio.wait_for(asyncio.open_connection(ip, port), 15)
                return rr, ww, False
            except OSError as e:
                last = e
        raise last or OSError('connect failed')

    async def _tunnel(self, r, w, host, port, ips):
        up_r, up_w, via = await self._open('https', host, port, ips)
        self._writers.add(up_w)
        try:
            if via:
                up_w.write(f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n'.encode()); await up_w.drain()
                resp = await asyncio.wait_for(up_r.readuntil(b'\r\n\r\n'), 20)
                if b' 200' not in resp.split(b'\r\n', 1)[0]:
                    await self._reply(w, 502, 'upstream refused'); return
            self.forwarded.append(f'{host}:{port}')
            w.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); await w.drain()
            await self._pipe(r, w, up_r, up_w)
        finally:
            self._writers.discard(up_w); up_w.close()

    async def _plain(self, r, w, method, p, port, headers, ips):
        n = 0
        for k, v in headers:
            if k.lower() == 'transfer-encoding':
                raise Blocked('bad_request', 'chunked request body')
            if k.lower() == 'content-length':
                n = int(v) if v.isdigit() else -1
        if n < 0 or n > MAX_BODY:
            raise Blocked('bad_request', 'body size')
        body = await asyncio.wait_for(r.readexactly(n), IO_TIMEOUT) if n else b''
        up_r, up_w, via = await self._open('http', p.hostname, port, ips)
        self._writers.add(up_w)
        try:
            drop = {'proxy-connection', 'connection', 'keep-alive', 'proxy-authorization', 'te', 'upgrade', 'host', 'transfer-encoding'}
            hdr = ''.join(f'{k}: {v}\r\n' for k, v in headers if k.lower() not in drop)
            authority = p.netloc.rsplit('@', 1)[-1]
            path = p.path or '/'
            if p.query:
                path += '?' + p.query
            target = urllib.parse.urlunsplit(('http', authority, path, '', '')) if via else path
            up_w.write(f'{method} {target} HTTP/1.1\r\nHost: {authority}\r\n{hdr}Connection: close\r\n\r\n'.encode() + body); await up_w.drain()
            self.forwarded.append(f'{p.hostname}:{port}')
            await self._pipe(None, w, up_r, up_w)   # 以降のクライアントからの入力は読まない(2 本目のリクエストを通さない)
        finally:
            self._writers.discard(up_w); up_w.close()

    async def _pipe(self, c_r, c_w, s_r, s_w):
        async def up():   # クライアント → サーバー
            try:
                while True:
                    d = await c_r.read(65536)
                    if not d:
                        break
                    s_w.write(d); await s_w.drain()
            except Exception:
                pass

        async def down():  # サーバー → クライアント(受信バイトを数える)
            try:
                while True:
                    d = await asyncio.wait_for(s_r.read(65536), IO_TIMEOUT)
                    if not d:
                        break
                    self.bytes += len(d)
                    if self.bytes > self.max_bytes:
                        self._trip(); break
                    c_w.write(d); await c_w.drain()
            except Exception:
                pass
        tasks = [asyncio.ensure_future(down())] + ([asyncio.ensure_future(up())] if c_r else [])
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
