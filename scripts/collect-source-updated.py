#!/usr/bin/env python3
"""Deterministically extract each site's own last-update date, with evidence. No guessing: unknown stays null.

Order: sitemap lastmod, RSS/Atom newest entry, HTML <time>/meta modified, on-page "最終更新"/"Last updated" text.
Last-Modified headers are not used (dynamic pages return the request time). At most 4 requests per site
(robots.txt, top page, sitemap, feed; same-host redirect hops, at most 2 each, are extra), robots.txt obeyed, 2s between requests and between sites.
Known limits: targets are the fixed 25 sites only (only literal IPs are rejected; resolve and check the IP before widening);
sitemap lastmod takes the maximum, so a site with a frequently regenerated listing looks newer than its real update;
curl --max-filesize relies on Content-Length (chunked bodies are cut at 1 MiB after reading, bounded by --max-time).
Usage: uv run python3 scripts/collect-source-updated.py > data/source-updated.json
"""
import datetime, email.parser, email.utils, html.parser, ipaddress, json, re, subprocess, sys, time, urllib.parse, urllib.robotparser

UA = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)'
SITES = {  # id: url, same as the D1 rows
 'sample-1':'https://llll-ll.com/','sample-2':'https://hn.llll-ll.com/','sample-3':'https://3min.llll-ll.com/','sample-4':'https://agasteer.llll-ll.com/',
 'sample-5':'https://www.aozora.gr.jp/','sample-6':'https://www.ndl.go.jp/','sample-7':'https://www.nao.ac.jp/',
 'collected-hyuki':'https://www.hyuki.com/','collected-kmonos':'https://www.kmonos.net/','collected-elm-chan':'https://elm-chan.org/','collected-genbu':'https://www.genbu.net/',
 'collected-kofun-map':'https://kofun.info/','collected-zukan-bouz':'https://www.zukan-bouz.com/','collected-stellarscenes':'https://stellarscenes.net/','collected-sora-can':'https://sora-can.com/',
 'collected-hoshi-izanai':'https://www.asahi-net.or.jp/~dy7s-ynym/','collected-elecroom-avr':'https://www.ne.jp/asahi/shared/o-family/ElecRoom/ElecMAIN.htm',
 'collected-denshi-muscat':'https://www.zea.jp/audio/','collected-kyotani-hobby':'http://www.kyotani-hobby.com/','collected-susutawari':'https://susutawari.org/',
 'collected-mlum-factory':'https://www.mlum-factory.com/','collected-naruken':'http://naruken.cweb.tk/','collected-hana300':'https://www.hana300.com/',
 'collected-tanken-com':'https://tanken.com/','collected-arch-hiroshima':'https://arch-hiroshima.net/'}
NOW = datetime.datetime.now(datetime.timezone.utc)
MAXBODY = 1024 * 1024

def safe_host(host):
    try: ip = ipaddress.ip_address(host)
    except ValueError: return host.lower() not in ('localhost',) and '.' in host
    return ip.is_global

def fetch(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or not safe_host(p.hostname): raise ValueError('unsafe url')
    time.sleep(2)
    r = subprocess.run(['curl', '-s', '-S', '--max-time', '15', '--max-filesize', str(MAXBODY), '-A', UA, '-D', '-', url], check=True, capture_output=True)
    head, body = r.stdout.split(b'\r\n\r\n', 1)
    if head.startswith(b'HTTP/1.1 200 Connection established'): head, body = body.split(b'\r\n\r\n', 1)
    first, _, rest = head.partition(b'\r\n'); status = int(first.split()[1])
    h = email.parser.BytesParser().parsebytes(rest)
    return status, h, body[:MAXBODY]

def get(url, rules, hops=2):
    # Follows only same-host redirects; robots is re-checked for each target.
    for _ in range(hops + 1):
        if not rules.can_fetch(UA, url): raise ValueError('disallowed by robots.txt')
        status, h, body = fetch(url)
        if status in (301, 302, 303, 307, 308):
            nxt = urllib.parse.urljoin(url, h.get('Location', ''))
            if urllib.parse.urlsplit(nxt).hostname != urllib.parse.urlsplit(url).hostname: raise ValueError('cross-host redirect')
            url = nxt; continue
        if status != 200: raise ValueError(f'HTTP {status}')
        ct = h.get_content_charset() or 'utf-8'
        return url, body.decode(ct if ct else 'utf-8', errors='replace')
    raise ValueError('too many redirects')

def norm(s):
    s = s.strip()
    m = re.match(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})', s)
    if not m: return None
    try: d = datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError: return None
    if d.year < 1995 or d > NOW.date(): return None
    return d.isoformat()

class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.dates = []; self.feeds = []; self.text = []; self.skip = False
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag in ('script', 'style'): self.skip = True
        if tag == 'time' and a.get('datetime'): self.dates.append(('html-time', a['datetime']))
        if tag == 'meta':
            k = (a.get('property') or a.get('name') or a.get('itemprop') or '').lower()
            if k in ('article:modified_time', 'og:updated_time', 'datemodified', 'last-modified', 'dcterms.modified') and a.get('content'): self.dates.append(('html-meta:' + k, a['content']))
        if tag == 'link' and a.get('rel', '').lower() == 'alternate' and a.get('type') in ('application/rss+xml', 'application/atom+xml') and a.get('href'): self.feeds.append(a['href'])
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = False
    def handle_data(self, d):
        if not self.skip: self.text.append(d)

def xml_dates(body, tag_re):
    return [d for d in (norm(m) for m in re.findall(tag_re, body)) if d]

def extract(sid, url):
    p = urllib.parse.urlsplit(url); base = f'{p.scheme}://{p.netloc}'
    rules = urllib.robotparser.RobotFileParser()
    try:
        s, _, b = fetch(base + '/robots.txt')
        rules.parse(b.decode('utf-8', errors='replace').splitlines() if s == 200 else [])
        if s not in (200, 404, 410): return None, None, None, f'robots.txt HTTP {s}'
    except Exception as e: return None, None, None, f'robots.txt: {e}'
    notes = []; page = Page()
    try:
        purl, body = get(url, rules); page.feed(body)
    except Exception as e: purl, body = url, ''; notes.append(f'top: {e}')
    if re.search(r'<meta[^>]+name=["\'](?:robots|miseresearchbot)["\'][^>]+noindex', body, re.I): return None, None, None, 'noindex'
    try:
        su, sb = get(base + '/sitemap.xml', rules)
        d = sorted(xml_dates(sb, r'<lastmod>\s*([^<]+?)\s*</lastmod>'))
        if d: return d[-1], su, 'sitemap-lastmod', None
        notes.append('sitemap: no lastmod')
    except Exception as e: notes.append(f'sitemap: {e}')
    if page.feeds:
        try:
            fu, fb = get(urllib.parse.urljoin(purl, page.feeds[0]), rules)
            d = sorted(xml_dates(fb, r'<(?:updated|pubDate|dc:date|published)>\s*([^<]+?)\s*</'))
            if not d:  # RFC 822 pubDate
                for m in re.findall(r'<pubDate>\s*([^<]+?)\s*</pubDate>', fb):
                    try: d.append(email.utils.parsedate_to_datetime(m).date().isoformat())
                    except Exception: pass
            d = [x for x in d if x <= NOW.date().isoformat() and x >= '1995']
            if d: return max(d), fu, 'feed-latest', None
            notes.append('feed: no date')
        except Exception as e: notes.append(f'feed: {e}')
    ds = sorted(x for x in (norm(v) for _, v in page.dates) if x)
    if ds: return ds[-1], purl, 'html-time-or-meta', None
    text = ''.join(page.text)
    m = re.findall(r'(?:最終更新(?:日)?|更新日|更新履歴|Last\s+(?:updated|modified))\s*[:：]?\s*(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2})', text, re.I)
    ds = sorted(x for x in (norm(re.sub(r'\s+', '', v)) for v in m) if x)
    if ds: return ds[-1], purl, 'page-text-last-updated', None
    return None, None, None, "; ".join(notes) or "no date found"

if __name__ == '__main__':
    out = []
    for sid, url in SITES.items():
        try: d, ev, kind, why = extract(sid, url)
        except Exception as e: d = ev = kind = None; why = f'error: {e}'
        out.append({'id': sid, 'url': url, 'source_updated_at': d, 'evidence_url': ev, 'kind': kind, 'reason': why, 'checked_at': NOW.isoformat(timespec='seconds')})
        print(sid, d, kind, why, file=sys.stderr)
        time.sleep(2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
