"""名簿スクリプト共通の小さな関数（取得・生成・差分で共有）。"""
import html, pathlib, re, urllib.parse

RAW = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies' / 'raw'

def host_key(u):
    if not u: return ''
    p = urllib.parse.urlsplit(u.strip() if '//' in u else '//' + u.strip())
    h = (p.hostname or '').lower().rstrip('.')
    return h[4:] if h.startswith('www.') else h

def clean_url(u):
    return re.sub(r'/index\.(html?|php)$', '/', u.strip())

def anchors(path):
    t = (RAW / path).read_text(encoding='utf-8')
    out = []
    for u, n in re.findall(r'<a [^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>', t, re.S):
        n = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', n))).strip()
        u = u.strip()
        if 'e-gov.go.jp' in u or 'j-lis.go.jp' in u or not n: continue
        out.append((n, u))
    return out

