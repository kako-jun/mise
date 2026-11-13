#!/usr/bin/env python3
"""Measure whether each registered site's top page may be shown in an iframe on mise (issue #41, step 1).

Run: uv run python3 scripts/check-embeddable.py > data/embeddability.json
Only response headers are observed (never bypassing anything). Per site: robots.txt (1 request),
the top URL (1), and at most one redirect follow (1); never parallel; 2 s between every request.
Judgement is deterministic from the observed headers (JS frame-busting etc. is out of scope).
robots.txt: 200 read, 404/410 no rules, anything else -> not measured (robots-unknown). Only same-host redirects are followed.
"""
import datetime, ipaddress, json, subprocess, sys, time, urllib.parse, urllib.robotparser

UA = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)'
MISE_ORIGIN = 'https://mise.llll-ll.com'
SITES = {  # id: url, same as the D1 rows
 'sample-1':'https://llll-ll.com/','sample-2':'https://hn.llll-ll.com/','sample-3':'https://3min.llll-ll.com/','sample-4':'https://agasteer.llll-ll.com/',
 'sample-5':'https://www.aozora.gr.jp/','sample-6':'https://www.ndl.go.jp/','sample-7':'https://www.nao.ac.jp/',
 'collected-hyuki':'https://www.hyuki.com/','collected-kmonos':'https://www.kmonos.net/','collected-elm-chan':'https://elm-chan.org/','collected-genbu':'https://www.genbu.net/',
 'collected-kofun-map':'https://kofun.info/','collected-zukan-bouz':'https://www.zukan-bouz.com/','collected-stellarscenes':'https://stellarscenes.net/','collected-sora-can':'https://sora-can.com/',
 'collected-hoshi-izanai':'https://www.asahi-net.or.jp/~dy7s-ynym/','collected-elecroom-avr':'https://www.ne.jp/asahi/shared/o-family/ElecRoom/ElecMAIN.htm',
 'collected-denshi-muscat':'https://www.zea.jp/audio/','collected-kyotani-hobby':'http://www.kyotani-hobby.com/','collected-susutawari':'https://susutawari.org/',
 'collected-mlum-factory':'https://www.mlum-factory.com/','collected-naruken':'http://naruken.cweb.tk/','collected-hana300':'https://www.hana300.com/',
 'collected-tanken-com':'https://tanken.com/','collected-arch-hiroshima':'https://arch-hiroshima.net/'}

def safe(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname: return False
    try: return ipaddress.ip_address(p.hostname).is_global
    except ValueError: return p.hostname.lower() != 'localhost' and '.' in p.hostname

def fetch(url, want_body):
    """One GET. Headers of the first response only; body is cut (1 byte, or 512 KiB for robots.txt)."""
    time.sleep(2)
    if not safe(url): return {'error': 'unsafe-url'}
    limit = '524288' if want_body else '1'
    r = subprocess.run(['curl', '-sS', '-D', '-', '-o', '-' if want_body else '/dev/null', '--max-time', '15',
                        '--max-filesize', limit, '-A', UA, url], capture_output=True)
    raw = r.stdout
    # Skip the proxy's "200 Connection established" preamble: the real response is the last header block.
    head, body = b'', raw
    while True:
        h_, sep, rest = body.partition(b'\r\n\r\n')
        if not h_.startswith(b'HTTP/') or not sep: break
        head, body = h_, rest
        if b'Connection established' in h_.split(b'\r\n')[0]: head = b''; continue
        break
    if not want_body: body = b''
    lines = head.decode('latin-1').split('\r\n')
    if not lines or not lines[0].startswith('HTTP/'):
        return {'error': (r.stderr.decode('utf-8', 'replace').strip() or 'no-response')[:200]}
    status = int(lines[0].split()[1])
    h = {}
    for l in lines[1:]:
        if ':' in l:
            k, v = l.split(':', 1); h.setdefault(k.strip().lower(), []).append(v.strip())
    return {'status': status, 'headers': h, 'body': body}

def csp_ancestors(csps):
    """Return list of frame-ancestors source lists (one per CSP header; None if directive absent)."""
    out = []
    for c in csps:
        for d in c.split(','):  # multiple policies may be comma joined
            found = None
            for part in d.split(';'):
                t = part.strip().split()
                if t and t[0].lower() == 'frame-ancestors': found = t[1:]
            if found is not None: out.append(found)
    return out

def allows_mise(srcs):
    mh = urllib.parse.urlsplit(MISE_ORIGIN).hostname
    for s in srcs:
        s = s.lower()
        if s in ('*', 'https:'): return True
        if s.startswith("'"): continue  # 'self' / 'none' never match a foreign origin
        t = s if '://' in s else 'https://' + s
        p = urllib.parse.urlsplit(t)
        if p.scheme != 'https' or not p.hostname: continue
        if p.port not in (None, 443): continue
        if p.path not in ('', '/'): continue
        if p.hostname == mh or (p.hostname.startswith('*.') and mh.endswith(p.hostname[1:])): return True
    return False

def judge(final_url, status, h):
    reasons, notes = [], []
    if not final_url.startswith('https://'): reasons.append('http-only')
    if status is None or status >= 400 or status < 200: reasons.append('error')
    xfo = [v.strip().upper() for v in h.get('x-frame-options', [])]
    for v in xfo:
        if v.startswith('DENY'): reasons.append('xfo-deny')
        elif v.startswith('SAMEORIGIN'): reasons.append('xfo-sameorigin')
        elif v.startswith('ALLOW-FROM'): notes.append('xfo-allow-from-obsolete (ignored by modern browsers)')
        else: notes.append(f'xfo-unrecognized:{v}')
    fa = csp_ancestors(h.get('content-security-policy', []))
    for srcs in fa:
        if not allows_mise(srcs): reasons.append('csp-frame-ancestors')
    return sorted(set(reasons)), notes

def check(sid, url):
    o = {'id': sid, 'url': url, 'requests': 0}
    p = urllib.parse.urlsplit(url)
    rules = urllib.robotparser.RobotFileParser()
    rr = fetch(f'{p.scheme}://{p.netloc}/robots.txt', True); o['requests'] += 1
    if 'error' in rr: o['robots'] = {'error': rr['error']}; allowed = None
    else:
        o['robots'] = {'status': rr['status']}
        if rr['status'] in (404, 410): allowed = True
        elif rr['status'] == 200: rules.parse(rr['body'].decode('utf-8', 'replace').splitlines()); allowed = rules.can_fetch(UA, url)
        else: allowed = None  # 3xx/401/403/5xx: unknown, same policy as collect-source-updated.py
    o['robots']['allowed'] = allowed
    if allowed is None:
        o.update(result='not-measured', embeddable=None, reasons=['robots-unknown'], notes=[]); return o
    if not allowed:
        o.update(result='not-measured', embeddable=None, reasons=['robots-disallow'], notes=[]); return o
    chain, cur, resp = [], url, None
    for hop in range(2):
        resp = fetch(cur, False); o['requests'] += 1
        if 'error' in resp:
            o.update(chain=chain, final_url=cur, status=None, headers={}, embeddable=False, reasons=['error'], error=resp['error'], notes=[]); return o
        chain.append({'url': cur, 'status': resp['status'], 'location': (resp['headers'].get('location') or [None])[0]})
        loc = chain[-1]['location']
        if resp['status'] in (301, 302, 303, 307, 308) and loc and hop == 0:
            nxt = urllib.parse.urljoin(cur, loc)
            if urllib.parse.urlsplit(nxt).hostname != urllib.parse.urlsplit(cur).hostname:
                # only same-host redirects are followed (robots.txt of this host already applied)
                o.update(chain=chain, final_url=cur, status=resp['status'], headers={}, embeddable=None,
                         reasons=['cross-host-redirect-not-followed'], notes=[]); return o
            cur = nxt; continue
        break
    h = resp['headers']
    keep = {k: h[k] for k in ('x-frame-options', 'content-security-policy', 'content-security-policy-report-only', 'content-type') if k in h}
    reasons, notes = judge(cur, resp['status'], h)
    if resp['status'] in (301, 302, 303, 307, 308): notes.append('redirect-not-followed-beyond-1-hop'); reasons = sorted(set(reasons) | {'error'})
    if cur.startswith('https://') and url.startswith('http://'): notes.append('http-to-https-upgrade')
    o.update(chain=chain, final_url=cur, final_scheme=urllib.parse.urlsplit(cur).scheme, status=resp['status'], headers=keep,
             embeddable=not reasons, reasons=reasons, notes=notes)
    return o

def main():
    out = {'fetched_at_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'ua': UA, 'mise_origin': MISE_ORIGIN, 'sites': []}
    for sid, url in SITES.items():
        out['sites'].append(check(sid, url)); print(sid, out['sites'][-1].get('reasons'), file=sys.stderr)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1); print()

if __name__ == '__main__': main()
