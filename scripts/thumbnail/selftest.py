"""撮影ジョブの自己検査(#130)。実サイトへアクセスしない。ローカルの小さなサーバー(127.0.0.1)だけが相手で、本番と同じ経路
(フィルタリングプロキシ netfilter.py → サンドボックス bwrap → Chromium sandbox)で動かす。

  PLAYWRIGHT_DIR=<node_modules/playwright> uv run --with pillow python3 scripts/thumbnail/selftest.py [--no-bwrap]

確かめること:
  1. 攻撃ページ(内部アドレスへの img/script/fetch、ポップアップ、専用ワーカー・SharedWorker の fetch と WebSocket、iframe 内の WebSocket、OOPIF、sendBeacon・
     EventSource・prefetch、IPv6 リテラル・IPv4 射影・NAT64・6to4・IPv4 互換、DNS の引けないホスト、80/443 以外のポート)の通信が、すべてフィルタで遮断され、
     サーバーに 1 件も届かないこと(サーバー側のカウンタ)
  2. 遷移の検査(別サイト・未検査の URL・meta refresh・JS の遷移・noindex・X-Robots-Tag・404・ダイアログ・ダウンロード)と、受信量・接続数の上限
  3. サンドボックスの中から、認証情報・環境変数・書き込み先・外向きのネットワークが見えないこと
終了コードは失敗の数(0 = 全合格)。一時ディレクトリは終了時に掃除する。
"""
import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import thumbnail as T  # noqa: E402

NO_BWRAP = '--no-bwrap' in sys.argv
NO_LIMITS = '--no-limits' in sys.argv
log = []
lock = threading.Lock()
LOG_PORTS = {}
BIG = b'x' * (1 << 20)


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):   # ブラウザが途中で切った接続の例外は静かに捨てる
        pass


def make_server():
    srv = Srv(('127.0.0.1', 0), None)
    P = srv.server_address[1]

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def send(self, body, ctype='text/html', status=200, headers=None):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(status); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(b))); self.send_header('Connection', 'close')
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(b)

        def page(self, body, headers=None):
            self.send(f'<!doctype html><title>t</title><body style="background:#4a8"><h1>x</h1>{body}', headers=headers)

        def go(self, loc):
            self.send_response(302); self.send_header('Location', loc); self.send_header('Content-Length', '0'); self.send_header('Connection', 'close'); self.end_headers()

        def do_GET(self):
            with lock:
                log.append((P, (self.headers.get('Host') or '').split(':')[0] + self.path))
            u = self.path; I = f'http://127.0.0.1:{P}'; Q = LOG_PORTS['second']
            if u.startswith('/img/'):
                return self.send(b'\x89PNG\r\n\x1a\n', 'image/png')
            if u.startswith('/probe'):
                return self.send('INTERNAL-SECRET', 'text/plain')
            routes = {
                '/ok': lambda: self.page('ok'),
                '/ok2': lambda: self.page('ok2'),
                '/redir-other': lambda: self.go(f'http://b.test:{P}/ok'),
                '/redir-ip': lambda: self.go(f'{I}/ok'),
                '/redir-www': lambda: self.go(f'http://www.a.test:{P}/ok'),
                '/redir-path': lambda: self.go('/ok2'),
                '/meta': lambda: self.page(f'<meta http-equiv="refresh" content="0;url=http://b.test:{P}/ok">meta'),
                '/js': lambda: self.page(f"js<script>setTimeout(()=>{{location.href='http://b.test:{P}/ok'}},100)</script>"),
                '/noindex': lambda: self.page('<meta name="robots" content="noindex">noindex'),
                '/other-bot': lambda: self.page('other', {'X-Robots-Tag': 'googlebot: noindex'}),
                '/xrt': lambda: self.page('xrt', {'X-Robots-Tag': 'noarchive'}),
                '/404': lambda: self.send('<h1>nf</h1>', status=404),
                '/dialog': lambda: self.page('dialog<script>alert("a");confirm("c");prompt("p")</script>'),
                '/download': lambda: self.send(b'x', 'application/octet-stream', headers={'Content-Disposition': 'attachment; filename=x.bin'}),
                '/limit-conns': lambda: self.page(''.join(f'<img width=1 height=1 src="/img/{i}.png">' for i in range(400))),
                '/limit-bytes': lambda: self.page('<script src="/big/1"></script><script src="/big/2"></script>'),
                '/w.js': lambda: self.send(f"fetch('{I}/probe-worker-fetch').catch(()=>{{}}); try{{new WebSocket('ws://127.0.0.1:{P}/probe-worker-ws')}}catch(e){{}}", 'text/javascript'),
                '/sw.js': lambda: self.send(f"onconnect=()=>{{}}; fetch('{I}/probe-shared-fetch').catch(()=>{{}});", 'text/javascript'),
                '/a-worker': lambda: self.page("<script>new Worker('/w.js')</script>"),
                '/a-shared': lambda: self.page("<script>new SharedWorker('/sw.js')</script>"),
                '/a-iframews': lambda: self.page(f"<script>const f=document.createElement('iframe');document.body.appendChild(f);try{{new f.contentWindow.WebSocket('ws://127.0.0.1:{P}/probe-iframe-ws')}}catch(e){{}}</script>"),
                '/a-ws': lambda: self.page(f"<script>try{{new WebSocket('ws://127.0.0.1:{P}/probe-ws')}}catch(e){{}}</script>"),
                '/a-popup': lambda: self.page(f"<script>window.open('{I}/probe-popup')</script>"),
                '/a-oopif': lambda: self.page(f'<iframe src="http://b.test:{P}/inner"></iframe>'),
                '/inner': lambda: self.page(f"<img src=\"{I}/probe-oopif-img.png\"><script>fetch('{I}/probe-oopif-fetch').catch(()=>{{}})</script>"),
                '/a-sub': lambda: self.page(f'<img src="{I}/probe-sub.png"><script src="{I}/probe-sub.js"></script><link rel=stylesheet href="{I}/probe-sub.css">'),
                '/a-misc': lambda: self.page(f"<script>navigator.sendBeacon('{I}/probe-beacon','x');new EventSource('{I}/probe-es')</script><link rel=prefetch href=\"{I}/probe-prefetch\"><link rel=preconnect href=\"{I}\">"),
                '/a-v6': lambda: self.page(''.join(f'<img src="http://{h}:{P}/probe-v6-{i}.png">' for i, h in enumerate(['[::1]', '[::ffff:127.0.0.1]', '[64:ff9b::7f00:1]', '[::7f00:1]', '[2002:7f00:1::]', '[fe80::1]', '[fec0::1]', '198.18.0.1', '169.254.169.254', '10.0.0.1']))),
                '/a-dns': lambda: self.page(f'<img src="http://nx.test:{P}/probe-nx.png"><iframe src="http://nx.test:{P}/probe-nx-frame"></iframe>'),
                '/a-port': lambda: self.page(f'<img src="http://a.test:{Q}/probe-port.png"><script src="http://a.test:{Q}/probe-port.js"></script><img src="http://a.test:8080/probe-8080.png"><img src="http://a.test:25/probe-25.png">'),
            }
            path = u.split('?')[0]
            if path.startswith('/big/'):
                self.send_response(200); self.send_header('Content-Type', 'text/javascript'); self.send_header('Connection', 'close'); self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(BIG)
                except OSError:
                    pass
                return
            (routes.get(path) or (lambda: self.send('nf', status=404)))()

    srv.RequestHandlerClass = H
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, P


def main():
    srv, P = make_server()
    srv2 = Srv(('127.0.0.1', 0), None); Q = srv2.server_address[1]; LOG_PORTS['second'] = Q

    class H2(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            with lock:
                log.append((Q, self.path))
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
    srv2.RequestHandlerClass = H2
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix='mise-thumbs-selftest-')
    stats_box = {}
    cap = T.NodeCapturer(sandbox=not NO_BWRAP, limits=not NO_LIMITS, timeout=120, proxy_kwargs={
        'resolve_override': {'a.test': '127.0.0.1', 'b.test': '127.0.0.1', 'www.a.test': '127.0.0.1'}, 'allow_ports': {P}, 'upstream': {'http': None, 'https': None}},
        on_proxy=lambda s: stats_box.update(s))
    base = f'http://a.test:{P}'
    bad = 0

    def case(name, path, extra=(), want=None, probes_never=True):
        """want: None=撮れる / 文字列=その reason / 'NOT_OK'=撮れない"""
        nonlocal bad
        start = len(log)
        url = base + path
        rec, png = cap(url, [url] + [e for e in extra])
        got = None if rec.get('ok') else rec.get('reason')
        ok = (got is not None) if want == 'NOT_OK' else got == want
        seen = [h for _, h in log[start:] if 'probe' in h]
        leaked = bool(seen) if probes_never else False
        if not ok or leaked:
            bad += 1
        print(f"{'PASS' if ok and not leaked else 'FAIL'}\t{name}\twant={want}\tgot={got}" + (f'\tサーバーに届いた: {seen}' if leaked else ''), file=sys.stderr)
        return rec

    case('ok', '/ok')
    case('other-site-redirect', '/redir-other', want='cross-site-redirect')
    case('literal-ip-redirect', '/redir-ip', want='cross-site-redirect')
    case('www-unchecked', '/redir-www', want=f'redirect-unchecked:http://www.a.test:{P}/ok')
    case('www-checked-ok', '/redir-www', extra=[f'http://www.a.test:{P}/ok'])
    case('path-unchecked', '/redir-path', want=f'redirect-unchecked:{base}/ok2')
    case('path-checked-ok', '/redir-path', extra=[f'{base}/ok2'])
    case('meta-refresh', '/meta', want='cross-site-redirect')
    case('js-location', '/js', want='cross-site-redirect')
    case('noindex', '/noindex', want='meta-robots:noindex')
    case('x-robots-tag', '/xrt', want='x-robots-tag:noarchive')
    case('x-robots-tag-other-bot', '/other-bot')
    case('404', '/404', want='http-404')
    case('dialog', '/dialog')
    case('download', '/download', want='NOT_OK')
    case('limit-conns', '/limit-conns', want='resource-limit')
    case('limit-bytes', '/limit-bytes', want='resource-limit')
    for name, path in (('worker-fetch-and-ws', '/a-worker'), ('shared-worker-fetch', '/a-shared'), ('iframe-websocket', '/a-iframews'), ('main-websocket', '/a-ws'),
                       ('popup-internal', '/a-popup'), ('oopif', '/a-oopif'), ('subresources-ip', '/a-sub'), ('beacon-eventsource-prefetch', '/a-misc')):
        case(name, path)
    case('ipv6-mapped-nat64-6to4-linklocal', '/a-v6')
    blocked = [d for d, _ in stats_box.get('blocked', [])]
    for needle in ('::1', '::ffff:7f00:1', '64:ff9b::7f00:1', '::7f00:1', '2002:7f00:1::', 'fe80::1', 'fec0::1', '198.18.0.1', '169.254.169.254', '10.0.0.1'):
        hit = any(needle in d for d in blocked)
        if not hit:
            bad += 1
        print(f"{'PASS' if hit else 'FAIL'}\tフィルタが遮断した記録: {needle}", file=sys.stderr)
    case('dns-failure-host', '/a-dns')
    hit = any(w == 'dns_failed' for _, w in stats_box.get('blocked', []))
    bad += (not hit); print(f"{'PASS' if hit else 'FAIL'}\t引けないホスト(nx.test)を DNS 失敗で遮断した記録", file=sys.stderr)
    case('ports-not-80-443', '/a-port')
    hit = any(w == 'bad_port' for _, w in stats_box.get('blocked', [])) and not any(p == Q for p, _ in log)
    bad += (not hit); print(f"{'PASS' if hit else 'FAIL'}\t80/443 以外のポート(サブリソース含む)を遮断し、第 2 サーバーに届いていない", file=sys.stderr)

    bad += upstream_checks(cap)
    if not NO_BWRAP:
        bad += isolation(cap, tmp)
    print(f'selftest: {"all passed" if not bad else f"{bad} 件失敗"}', file=sys.stderr)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return bad


def upstream_checks(cap):
    """上流プロキシ経由モード: ① 上流には検査した IP 宛てで届く(名前を渡さない)、② リクエスト密輸の各形が上流に 1 件も届かない。実 Chromium で撮影も通す。"""
    import socket
    sys.path.insert(0, str(Path(__file__).parents[2] / 'tests'))
    import test_netfilter as TN
    seen = []

    class U(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def do_GET(self):
            with lock:
                seen.append(self.requestline + ' | Host: ' + (self.headers.get('Host') or ''))
            body = b'<!doctype html><title>t</title><body style="background:#48a"><h1>upstream</h1>'
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.send_header('Content-Length', str(len(body))); self.send_header('Connection', 'close'); self.end_headers(); self.wfile.write(body)
    up = Srv(('127.0.0.1', 0), U); threading.Thread(target=up.serve_forever, daemon=True).start(); uport = up.server_address[1]
    ucap = T.NodeCapturer(sandbox=not NO_BWRAP, limits=not NO_LIMITS, timeout=120, proxy_kwargs={'resolve_override': {'pub.test': '93.184.216.34'}, 'upstream': {'http': ('127.0.0.1', uport), 'https': ('127.0.0.1', uport)}})
    rec, _ = ucap('http://pub.test/', ['http://pub.test/'])
    ok1 = bool(rec.get('ok'))
    ok2 = bool(seen) and seen[0].startswith('GET http://pub.test/ HTTP/1.1') and seen[0].endswith('Host: pub.test')
    bad = 0
    for name, ok in (('上流プロキシ経由でも実 Chromium で撮れる', ok1), ('上流へは平文 HTTP を名前のまま渡す(Squid 系は Host を URI に書き換えるため IP 宛てにできない)', ok2)):
        bad += (not ok); print(f"{'PASS' if ok else 'FAIL'}\t上流連鎖: {name}", file=sys.stderr)
    # 密輸の各形(上流には何も届かない)
    from netfilter import FilterProxy
    got = []
    srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(32); sport = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            got.append(c.recv(65536)); c.close()
    threading.Thread(target=serve, daemon=True).start()
    p = FilterProxy(resolve_override={'pub.test': '93.184.216.34'}, upstream={'http': ('127.0.0.1', sport), 'https': ('127.0.0.1', sport)}).start()
    for name, req in TN.Proxy.SMUGGLE.items():
        out = TN.talk(p, req)
        ok = out.startswith(b'HTTP/1.1 403')
        bad += (not ok); print(f"{'PASS' if ok else 'FAIL'}\t密輸: {name}", file=sys.stderr)
    ok = not got
    bad += (not ok); print(f"{'PASS' if ok else 'FAIL'}\t密輸の全ての形で上流に 1 件も届いていない", file=sys.stderr)
    p.stop(); srv.close(); up.shutdown()
    return bad


def isolation(cap, tmp):
    """サンドボックスの中から見えるもの。認証情報・環境変数・書き込み先・外向きのネットワーク。"""
    home = str(Path.home())
    os.environ['MISE_SELFTEST_SECRET'] = 'canary-secret'
    d = tempfile.mkdtemp(prefix='mise-thumb-iso-')
    script = f'''
echo "ENV:$(env | grep -c -i -E 'canary|TOKEN|SECRET|KEY|PASSWORD|proxy' || true)"
for p in .ssh .config .npmrc .gitconfig .aws .netrc .cache/gh .local/share .claude .config/gh .config/wrangler .wrangler; do [ -e "{home}/$p" ] && echo "VISIBLE:$p"; done
ls /home | tr '\\n' ' ' | sed 's/^/HOME_LS:/'; echo
[ -e /root ] && [ -n "$(ls -A /root 2>/dev/null)" ] && echo VISIBLE:/root
touch /usr/x 2>/dev/null && echo WRITABLE:/usr
touch /etc/x 2>/dev/null && echo WRITABLE:/etc
[ -e /sys/class/net ] && echo SYSNET:visible || echo SYSNET:none
for p in /var/tmp /opt /srv; do [ -n "$(ls -A $p 2>/dev/null)" ] && echo VISIBLE:$p; done
touch {d}/ok 2>/dev/null && echo WRITABLE_TMPDIR:ok
tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | tr '\\n' ' ' | sed 's/^/NETDEVS:/'; echo
(exec 3<>/dev/tcp/1.1.1.1/53) 2>/dev/null && echo NET:reachable || echo NET:unreachable
'''
    out = subprocess.run(cap._bwrap(d) + ['--', '/bin/bash', '-c', script], capture_output=True, text=True, timeout=60, env=dict(os.environ)).stdout
    os.environ.pop('MISE_SELFTEST_SECRET', None)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    lines = out.strip().splitlines()
    checks = [
        ('環境変数に秘密・プロキシが無い', 'ENV:0' in lines),
        ('認証情報(~/.ssh・~/.config・~/.npmrc・~/.gitconfig・~/.aws・gh/wrangler の設定)が見えない', not any(l.startswith('VISIBLE:') for l in lines)),
        ('/usr・/etc に書けない', not any(l.startswith('WRITABLE:') for l in lines)),
        ('撮影用の一時ディレクトリには書ける', 'WRITABLE_TMPDIR:ok' in lines),
        ('ネットワークデバイスは lo だけ', any(l.startswith('NETDEVS:') and l.split(':', 1)[1].split() == ['lo'] for l in lines)),
        ('/sys にホストのネットワーク IF 名が見えない', 'SYSNET:none' in lines),
        ('/var/tmp・/opt・/srv は空', 'VISIBLE:/var/tmp' not in lines and 'VISIBLE:/opt' not in lines and 'VISIBLE:/srv' not in lines),
        ('外向きのネットワークに届かない(1.1.1.1:53)', 'NET:unreachable' in lines),
    ]
    bad = 0
    for name, ok in checks:
        bad += (not ok); print(f"{'PASS' if ok else 'FAIL'}\tサンドボックス: {name}", file=sys.stderr)
    if bad:
        print(out, file=sys.stderr)
    return bad


if __name__ == '__main__':
    sys.exit(main())
