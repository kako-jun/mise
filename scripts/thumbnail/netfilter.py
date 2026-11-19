"""サムネイル撮影用の検査付きフィルタリングプロキシ(#130)。標準ライブラリのみ。

ブラウザの全通信(ワーカー・ポップアップ・iframe・WebSocket・WebRTC・prefetch を含む)をここへ通す。ブラウザ自身には名前解決も外への経路も持たせない。
ここが宛先を検査して通すか止めるかを決める「唯一の関所」で、**アドレス判定の表は scripts/pipeline/netaddr.py の 1 か所だけ**(fetcher.check_public_host も同じ表。Node 側には判定を持たない)。

検査(CONNECT の宛先も平文 HTTP の宛先も同じ):
  - ポートは 80/443 だけ(テスト用の追加ポートは allow_ports で明示したときだけ)
  - ホスト名は自分で名前解決し、**全アドレスが公開アドレス**であること。引けない名前は拒否(fail closed)。IP リテラルは直接検査
  - 上流プロキシが無い環境では、検査した IP に固定して接続する(DNS リバインディングも塞ぐ)
  - 上流プロキシ(環境変数 http_proxy / https_proxy)がある環境では、そこへ連鎖する。
      * HTTPS(CONNECT)は検査した IP 宛て(`CONNECT <ip>:443`)にして固定する。**名前の CONNECT へのフォールバックはしない**(攻撃者が 5xx を強制して名前解決を上流に渡させる道を塞ぐ)。
        上流が IP 宛て CONNECT を受け付けるかは、ジョブの開始時に公開 IP リテラルへのプローブで 1 回だけ確かめる(probe_upstream)。受け付けない上流では停止し、
        名前の CONNECT を許すのは明示フラグ(allow_unpinned_connect。CLI は --allow-unpinned-upstream)のときだけ(その接続は unpinned に記録)
      * 平文 HTTP は、上流(Squid 系)が Host を URI のホストに書き換えるため IP 宛てにできず、名前解決を上流に渡すしかない。**既定では拒否する**(トップレベル・iframe・サブリソースとも。
        攻撃者の用意した rebinding 名の http:// へ誘導されても内部の HTTP を撮らせない)。明示フラグ(allow_plain_upstream。CLI は --allow-plain-http-upstream)のときだけ通し、unpinned に記録する
  - 1 本の接続につき 1 リクエスト(Connection: close)。同じ接続で別ホスト宛の 2 本目を通さない
  - 総受信バイト数・接続数に上限。超えたら全接続を切り over_limit を立てる(呼び出し側がページを閉じる)
"""
import asyncio
import ipaddress
import os
import re
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

ALLOWED_PORTS = (80, 443)
MAX_BYTES = 48 * 1024 * 1024       # 1 ページで受け取る総バイト数(試行 20 件の最初の画面は 2〜32MB。暴走・無限ストリームを止める上限であって通常のページを止めない値)
MAX_CONNS = 300                    # 1 ページで受け付ける接続数
MAX_HEAD = 64 * 1024
MAX_BODY = 1024 * 1024             # 平文 HTTP のリクエスト本文の上限
IO_TIMEOUT = 60
MAX_LINE = 8192                    # リクエスト行・ヘッダ 1 行の長さ
MAX_HEADERS = 100
TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")   # RFC 9110 の token(メソッド名・ヘッダ名)

sys.path.insert(0, str(Path(__file__).parents[1] / 'pipeline'))
import netaddr  # noqa: E402  (判定の表は scripts/pipeline/netaddr.py の 1 か所)

is_public_address = netaddr.is_public_address


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


def probe_upstream(upstream, target=('1.1.1.1', 443), timeout=15):
    """上流プロキシが IP 宛ての CONNECT を受け付けるかを、公開 IP リテラルへの CONNECT の応答コードだけで確かめる(データは送らない。実在の特定サイトに依存しない)。
    戻り: (受け付けるか, 説明)。上流が無ければ (True, 'no upstream')。"""
    up = upstream.get('https')
    if not up:
        return True, 'no upstream'
    authority = _authority(target[0], target[1])
    try:
        with socket.create_connection(up, timeout=timeout) as c:
            c.settimeout(timeout)
            c.sendall(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('iso-8859-1'))
            buf = b''
            while b'\r\n\r\n' not in buf and len(buf) < 8192:
                d = c.recv(4096)
                if not d:
                    break
                buf += d
    except OSError as e:
        return False, f'上流プロキシへ接続できない: {e}'
    m = re.match(rb'HTTP/\d\.\d (\d{3})', buf)
    code = int(m.group(1)) if m else 0
    return code == 200, f'CONNECT {authority} -> {code or "no response"}'


def parse_head(head):
    """リクエストの先頭(リクエスト行とヘッダ)を厳密に解釈する。上流へ送る前に、リクエスト密輸になる形(行内の裸の CR・LF・NUL・制御文字、
    obs-fold、token でないメソッド名・ヘッダ名、長すぎる行・多すぎるヘッダ)を拒否する。戻り: (method, target, [(name, value)])。"""
    lines = head[:-4].split(b'\r\n')
    for ln in lines:
        if len(ln) > MAX_LINE:
            raise Blocked('bad_request', 'line too long')
        if any((c < 0x20 and c != 0x09) or c == 0x7f for c in ln):   # CR・LF・NUL・その他の制御文字(HTAB は許す)
            raise Blocked('bad_request', 'control character')
    if len(lines) - 1 > MAX_HEADERS:
        raise Blocked('bad_request', 'too many headers')
    parts = lines[0].decode('iso-8859-1').split(' ')
    if len(parts) != 3 or not TOKEN.match(parts[0]) or parts[2] not in ('HTTP/1.1', 'HTTP/1.0') or not parts[1]:
        raise Blocked('bad_request', lines[0][:60].decode('iso-8859-1'))
    headers = []
    for ln in lines[1:]:
        if ln[:1] in (b' ', b'\t'):
            raise Blocked('bad_request', 'obs-fold')
        k, sep, v = ln.partition(b':')
        if not sep or not TOKEN.match(k.decode('iso-8859-1')) or b'_' in k:   # `_` を含む名前は一部のバックエンドが `-` に正規化する(Transfer_Encoding 等)
            raise Blocked('bad_request', 'bad header')
        headers.append((k.decode('iso-8859-1'), v.decode('iso-8859-1').strip(' \t')))
    return parts[0], parts[1], headers


def _is_ip(h):
    try:
        ipaddress.ip_address(h.strip('[]').split('%', 1)[0])
        return True
    except ValueError:
        return False


def _authority(ip, port):
    ip = ip.strip('[]')
    return f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}'


class FilterProxy:
    """1 回の撮影につき 1 つ起動する。unix_path があれば Unix ソケット、無ければ 127.0.0.1 の空きポートで待ち受ける。
    resolve_override / allow_ports はテスト専用(selftest・単体テストが手元のサーバーを相手にするため)。本番では指定しない。"""
    def __init__(self, unix_path=None, upstream=None, max_bytes=MAX_BYTES, max_conns=MAX_CONNS, resolver=None,
                 resolve_override=None, allow_ports=(), on_limit=None, allow_unpinned_connect=False, allow_plain_upstream=False):
        self.unix_path = unix_path
        self.upstream = upstream_from_env() if upstream is None else upstream
        self.max_bytes = max_bytes; self.max_conns = max_conns
        self.resolver = resolver; self.resolve_override = dict(resolve_override or {}); self.allow_ports = set(ALLOWED_PORTS) | set(allow_ports)
        self.on_limit = on_limit; self.allow_unpinned_connect = allow_unpinned_connect; self.allow_plain_upstream = allow_plain_upstream
        self.bytes = 0; self.conns = 0; self.over_limit = False
        self.blocked = []      # [(host:port, reason)]
        self.forwarded = []    # [host:port]
        self.unpinned = []     # 名前解決が上流になった(IP を固定できなかった)ホスト。明示フラグ(allow_unpinned_connect・allow_plain_upstream)のときだけ起こる
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
        method, target, headers = parse_head(await self._read_head(r))
        if method.upper() == 'CONNECT':
            host, port = _split_hostport(target)
            ips = await self._check(host, port)
            await self._tunnel(r, w, host, port, ips)
        else:
            p = urllib.parse.urlsplit(target)
            if p.scheme != 'http' or not p.hostname:
                raise Blocked('bad_request', target[:80])
            try:
                port = p.port if p.port is not None else 80   # :0 を 80 扱いにしない(0 はポート検査で拒否)
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

    async def _upstream_connect(self, up, authority):
        """上流プロキシへ CONNECT する。戻り: (reader, writer, status)。接続できなければ OSError。"""
        rr, ww = await asyncio.wait_for(asyncio.open_connection(*up), 15)
        try:
            ww.write(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('iso-8859-1')); await ww.drain()
            resp = await asyncio.wait_for(rr.readuntil(b'\r\n\r\n'), 20)
        except BaseException:
            ww.close(); raise   # タイムアウト・切断でも接続を残さない
        m = re.match(rb'HTTP/\d\.\d (\d{3})', resp)
        return rr, ww, int(m.group(1)) if m else 0

    async def _tunnel(self, r, w, host, port, ips):
        up = self.upstream.get('https')
        if up:
            # 上流経由でも、検査した IP に固定する(名前解決を上流に渡さない)。TLS の SNI・証明書検証はトンネルの中で Chromium がホスト名で行う。
            up_r = up_w = None
            for ip in ips:
                rr, ww, status = await self._upstream_connect(up, _authority(ip, port))
                if status == 200:
                    up_r, up_w = rr, ww; break
                ww.close()
            if up_w is None and self.allow_unpinned_connect and not _is_ip(host):
                # 明示フラグのときだけ、名前の CONNECT に戻す(その接続は名前解決が上流＝固定できない。記録に残す)。IP リテラル宛てはフォールバックしない。
                rr, ww, status = await self._upstream_connect(up, _authority(host, port))
                if status == 200:
                    up_r, up_w = rr, ww; self.unpinned.append(host)
                else:
                    ww.close()
            if up_w is None:
                await self._reply(w, 502, 'upstream refused'); return   # IP 宛て CONNECT が失敗したらその宛先は失敗(フォールバックしない)
            self._writers.add(up_w)
            try:
                self.forwarded.append(f'{host}:{port}')
                w.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); await w.drain()
                await self._pipe(r, w, up_r, up_w)
            finally:
                self._writers.discard(up_w); up_w.close()
            return
        up_r, up_w, _via = await self._open('https', host, port, ips)
        self._writers.add(up_w)
        try:
            self.forwarded.append(f'{host}:{port}')
            w.write(b'HTTP/1.1 200 Connection Established\r\n\r\n'); await w.drain()
            await self._pipe(r, w, up_r, up_w)
        finally:
            self._writers.discard(up_w); up_w.close()

    async def _plain(self, r, w, method, p, port, headers, ips):
        lengths = [v for k, v in headers if k.lower() == 'content-length']
        if any(k.lower() == 'transfer-encoding' for k, _ in headers):
            raise Blocked('bad_request', 'chunked request body')
        if len(lengths) > 1:
            raise Blocked('bad_request', 'duplicate content-length')   # 密輸に使われる(上流と解釈がずれる)
        n = 0
        if lengths:
            if not (lengths[0].isascii() and lengths[0].isdigit()) or int(lengths[0]) > MAX_BODY:
                raise Blocked('bad_request', 'content-length')
            n = int(lengths[0])
        if self.upstream.get('http') and not self.allow_plain_upstream:
            raise Blocked('plain_http_upstream', f'{p.hostname}:{port}')   # 上流あり環境の平文 HTTP は IP 宛てにできないので既定では拒否
        body = await asyncio.wait_for(r.readexactly(n), IO_TIMEOUT) if n else b''
        up_r, up_w, via = await self._open('http', p.hostname, port, ips)
        self._writers.add(up_w)
        try:
            drop = {'proxy-connection', 'connection', 'keep-alive', 'proxy-authorization', 'te', 'upgrade', 'host', 'transfer-encoding', 'content-length',
                    'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-port', 'forwarded', 'via', 'x-real-ip', 'client-ip', 'true-client-ip', 'x-client-ip', 'x-original-url', 'x-rewrite-url'}   # ページの JS が設定でき、上流に素通りして偽装に使えるヘッダは落とす
            hdr = ''.join(f'{k}: {v}\r\n' for k, v in headers if k.lower() not in drop)
            if n:
                hdr += f'Content-Length: {n}\r\n'
            authority = p.netloc.rsplit('@', 1)[-1]
            path = p.path or '/'
            if p.query:
                path += '?' + p.query
            # 上流経由の平文 HTTP は、IP 宛て(絶対 URI を IP、Host を元の名前)にできない: 実機の上流プロキシ(Squid 系)は Host ヘッダを URI のホストに
            # 書き換えるので、仮想ホストのサイトが既定ページ(403 等)を返してしまう(2026-11-19 に実機で確認)。そのため名前のまま上流へ渡し、
            # 名前解決が上流＝IP を固定できない事実を unpinned に記録する。HTTPS(CONNECT)は IP に固定できる(_tunnel)。
            if via:
                target = urllib.parse.urlunsplit(('http', authority, path, '', ''))
                self.unpinned.append(p.hostname)
            else:
                target = path
            up_w.write(f'{method} {target} HTTP/1.1\r\nHost: {authority}\r\n{hdr}Connection: close\r\n\r\n'.encode('iso-8859-1', 'replace') + body); await up_w.drain()
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
