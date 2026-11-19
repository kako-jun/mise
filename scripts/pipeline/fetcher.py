"""行儀のよい取得。robots.txt 遵守・UA明示・内部IP拒否・サイズ/時間上限・リダイレクトはホップごとに再判定。

curl を子プロセスで使う（この環境のCA/プロキシ設定がそのまま通る。collect-sample.py と同じ作法）。
"""
import codecs
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.robotparser

import freshness
import netaddr

USER_AGENT = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)'
ROBOT_TOKEN = 'MiseResearchBot'
MAX_BODY = 512 * 1024        # 1ページの本文上限（先頭のみ使う）
MAX_ROBOTS = 128 * 1024
MAX_REDIRECTS = 3
MAX_FRESHNESS_REQUESTS = 2   # 更新の兆候用の追加リクエスト上限（feed / sitemap。robots 遵守・ホスト別待ち）
MAX_CRAWL_DELAY = 10
HOST_WAIT = 1.5              # 同一ホストへのリクエスト間隔（秒）
TIMEOUT = 20
ALLOWED_PORTS = (None, 80, 443)   # テストだけが偽サーバー用に差し替える
ALLOW_PRIVATE = False             # 同上(本番では常に False)


class FetchError(Exception):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}'); self.code = code; self.detail = detail


def check_public_host(host):
    """ホスト名を解決し、全IPが公開アドレスであることを確認して返す。内部IPなら例外。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError('dns', str(e))
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise FetchError('dns', 'no address')
    if ALLOW_PRIVATE:
        return ips
    for ip in ips:
        if not netaddr.is_public_address(ip):   # 判定の表は netaddr 1 か所(is_global だけでは ::7f00:1・64:ff9b::7f00:1 を通す)
            raise FetchError('ssrf_blocked', f'{host} -> non-public address')
    return ips


def _uses_proxy(scheme):
    return bool(os.environ.get(f'{scheme}_proxy') or os.environ.get(f'{scheme.upper()}_PROXY'))


def http_get(url, max_body=MAX_BODY, extra_headers=None):
    """1リクエスト。リダイレクトは追わない。extra_headers は条件付き GET 用の追加ヘッダ(dict)。戻り: (status, headers(dict, 小文字キー), body bytes)。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise FetchError('bad_url', url)
    if parts.port not in ALLOWED_PORTS:
        raise FetchError('bad_port', str(parts.port))
    ips = check_public_host(parts.hostname)
    cmd = ['curl', '--silent', '--show-error', '--include', '--max-time', str(TIMEOUT), '--connect-timeout', '10',
           '--max-redirs', '0', '--compressed', '--proto', '=http,https', '--user-agent', USER_AGENT,
           '--header', 'Accept-Language: ja,en;q=0.5']
    for k, v in (extra_headers or {}).items():
        cmd += ['--header', f'{k}: {v}']
    if not _uses_proxy(parts.scheme):
        # 検査したIPに固定して接続する（検査と接続の間の名前解決差し替えを防ぐ）
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        ip = next((i for i in ips if ':' not in i), ips[0])   # IPv4 優先
        cmd += ['--resolve', f'{parts.hostname}:{port}:' + (f'[{ip}]' if ':' in ip else ip)]
    proc = subprocess.Popen(cmd + [url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buf = b''
    try:
        while len(buf) < max_body + 16384:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read().decode('utf-8', 'replace').strip()
        proc.wait()
        proc.stdout.close(); proc.stderr.close()
    if not buf:
        raise FetchError('connect', err[:200] or 'empty response')
    return _parse_response(buf, max_body)


def _parse_response(buf, max_body):
    # プロキシの CONNECT 応答と 1xx を読み飛ばす
    while True:
        head, sep, rest = buf.partition(b'\r\n\r\n')
        if not sep:
            raise FetchError('protocol', 'no header terminator')
        lines = head.decode('iso-8859-1').split('\r\n')
        m = re.match(r'HTTP/\S+\s+(\d{3})', lines[0])
        if not m:
            raise FetchError('protocol', lines[0][:80])
        status = int(m.group(1))
        if status < 200 and status != 101 or lines[0].startswith('HTTP/1.1 200 Connection established'):
            buf = rest; continue
        break
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(':')
        headers[k.strip().lower()] = v.strip()
    return status, headers, rest[:max_body]


def decode_body(body, headers):
    ctype = headers.get('content-type', '')
    m = re.search(r'charset=([\w-]+)', ctype, re.I) or re.search(r'<meta[^>]+charset=["\']?([\w-]+)', body[:4096].decode('ascii', 'ignore'), re.I)
    candidates = [m.group(1)] if m else []
    candidates += ['utf-8', 'cp932', 'euc-jp']
    for enc in candidates:
        try:
            codecs.lookup(enc)
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode('utf-8', 'replace')


def same_site(a, b):
    strip = lambda h: h[4:] if h.startswith('www.') else h
    return strip(a) == strip(b)


class RobotsCache:
    """origin ごとの robots.txt。取得は同一サイト内のリダイレクトのみ追従。"""
    def __init__(self, sleep):
        self.rules = {}; self.sleep = sleep
        self.detail = {}   # origin -> 'unavailable' の理由(診断用。例: 'http_503'・'redirect_offsite')

    def _fetch(self, origin):
        url = origin + '/robots.txt'
        host = urllib.parse.urlsplit(origin).hostname
        for _ in range(MAX_REDIRECTS + 1):
            self.sleep(host)
            status, headers, body = http_get(url, MAX_ROBOTS)
            if 300 <= status < 400 and headers.get('location'):
                nxt = urllib.parse.urljoin(url, headers['location'])
                nh = urllib.parse.urlsplit(nxt).hostname or ''
                if not same_site(nh, host):
                    self.detail[origin] = 'redirect_offsite'
                    return 'unavailable', None
                url = nxt; host = nh; continue
            break
        if status == 200:
            p = urllib.robotparser.RobotFileParser(); p.parse(decode_body(body, headers).splitlines())
            return 'allowed', p
        if status in (404, 410):
            return 'allowed_no_robots', None
        if status in (401, 403):
            return 'disallowed', None   # 保守的に拒否扱い
        self.detail[origin] = f'http_{status}'
        return 'unavailable', None      # 5xx 等。再試行待ち

    def check(self, origin, path):
        """戻り: (robots_status, 許可されているか, crawl_delay)"""
        if origin not in self.rules:
            self.rules[origin] = self._fetch(origin)
        st, p = self.rules[origin]
        if st == 'allowed':
            delay = p.crawl_delay(ROBOT_TOKEN)
            return st, p.can_fetch(ROBOT_TOKEN, origin + path), min(float(delay), MAX_CRAWL_DELAY) if delay else 0
        return st, st == 'allowed_no_robots', 0


def fetch_site(host, scheme_order=('https', 'http'), path='/'):
    """ホストのトップページを robots 確認の上で取得する（1ホスト: robots 1〜2 + ページ 1〜4 リクエスト）。

    戻り dict: robots_status, status, headers, body, final_url, error(None|FetchError.code)
    """
    def sleep(h):
        time.sleep(HOST_WAIT)
    robots = RobotsCache(sleep)
    hosts = [host, host[4:] if host.startswith('www.') else 'www.' + host]
    out = {'robots_status': None, 'status': None, 'headers': {}, 'body': b'', 'final_url': None, 'error': None}
    for h in hosts:
        for scheme in scheme_order:
            origin = f'{scheme}://{h}'
            try:
                st, ok, delay = robots.check(origin, path)
            except FetchError as e:
                out['error'] = e.code; out['error_detail'] = e.detail
                if e.code in ('ssrf_blocked',):
                    return out
                continue
            out['error'] = None; out['robots_status'] = st
            if st == 'unavailable':
                out['error'] = 'robots_unavailable'; return out
            if not ok:
                out['error'] = 'robots_disallowed'; return out
            res = _follow(out, origin + path, robots, delay)
            if not res['error'] and res['status'] == 200 and 'html' in res['headers'].get('content-type', 'html').lower():
                try:
                    res['freshness'] = collect_freshness(res, robots)
                except Exception as e:  # 兆候は付随情報。失敗してもトップ取得の結果は捨てない
                    print(f'freshness: {host}: {type(e).__name__}: {e}', file=sys.stderr)
                    res['freshness'] = []
            return res
    return out


def collect_freshness(out, robots, today=None):
    """トップ取得に成功したサイトの更新の兆候(header / feed / sitemap)を [(iso, source)] で返す。追加リクエストは最大 MAX_FRESHNESS_REQUESTS。
    feed で日付が取れたら sitemap は取らない。失敗・robots 不許可は黙って飛ばす（兆候なしになるだけ）。"""
    found = [(freshness.header_date(out.get('headers'), today), 'header')]
    url = out.get('final_url')
    if not url or out.get('status') != 200:
        return found
    parts = urllib.parse.urlsplit(url)
    origin = f'{parts.scheme}://{parts.netloc}'
    html = decode_body(out['body'], out['headers']) if out.get('body') else ''
    targets = []
    for href in freshness.feed_links(html):
        u = urllib.parse.urljoin(url, href)
        up = urllib.parse.urlsplit(u)
        if up.scheme in ('http', 'https') and same_site(up.hostname or '', parts.hostname or '') and ('feed', u) not in targets:
            targets.append(('feed', u))
    targets = targets[:1]   # 公式に宣言された feed は 1 本だけ
    targets.append(('sitemap', origin + '/sitemap.xml'))
    if not targets[0][0] == 'feed':
        targets.append(('feed', origin + '/rss.xml'))
    done = 0
    for kind, u in targets:
        if done >= MAX_FRESHNESS_REQUESTS:
            break
        up = urllib.parse.urlsplit(u)
        try:
            st, ok, delay = robots.check(f'{up.scheme}://{up.netloc}', (up.path or '/') + (f'?{up.query}' if up.query else ''))
            if not ok:
                continue
            time.sleep(max(HOST_WAIT, delay)); done += 1
            status, headers, body = http_get(u)
        except FetchError:
            continue
        if status != 200:
            continue
        text = decode_body(body, headers)
        d = freshness.feed_date(text, today) if kind == 'feed' else freshness.sitemap_date(text, today)
        if d:
            found.append((d, kind))
            if kind == 'feed':
                break
    return found


def _follow(out, url, robots, delay):
    first_host = urllib.parse.urlsplit(url).hostname
    for _ in range(MAX_REDIRECTS + 1):
        host = urllib.parse.urlsplit(url).hostname
        time.sleep(max(HOST_WAIT, delay))
        try:
            status, headers, body = http_get(url)
        except FetchError as e:
            out['error'] = e.code; out['error_detail'] = e.detail; return out
        out.update(status=status, headers=headers, body=body, final_url=url)
        if 300 <= status < 400 and headers.get('location'):
            nxt = urllib.parse.urljoin(url, headers['location'])
            parts = urllib.parse.urlsplit(nxt)
            if parts.scheme not in ('http', 'https') or not same_site(parts.hostname or '', first_host):
                out['error'] = 'redirect_offsite'; out['redirect_to'] = nxt; return out
            origin = f'{parts.scheme}://{parts.netloc}'
            try:
                st, ok, delay = robots.check(origin, parts.path or '/')
            except FetchError as e:
                out['error'] = e.code; return out
            if not ok:
                out['robots_status'] = st; out['error'] = 'robots_disallowed'; return out
            url = nxt; continue
        return out
    out['error'] = 'too_many_redirects'
    return out
