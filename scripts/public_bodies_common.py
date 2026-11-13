"""名簿スクリプト共通の小さな関数（取得・生成・差分で共有）。"""
import html, pathlib, re, urllib.parse

RAW = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies' / 'raw'

def host_key(u):
    if not u: return ''
    p = urllib.parse.urlsplit(u.strip() if '//' in u else '//' + u.strip())
    h = (p.hostname or '').lower().rstrip('.')
    return h[4:] if h.startswith('www.') else h

_INDEX = re.compile(r'/(index|default)\.(html?|php|aspx?|cgi|jsp)/*$', re.I)

def clean_url(u):
    """掲載用 URL: 空白・fragment・query を落とし、末尾の index 系ファイル名(大文字小文字を問わず、後ろにスラッシュが付いたものも)を落とす。"""
    u = u.strip().split('#', 1)[0].split('?', 1)[0]
    return _INDEX.sub('/', u)

def url_key(u):
    """重複・照合用のキー: ホスト(www.除去・小文字) + パス(index系・末尾スラッシュ除去、小文字化はしない)。scheme は無視。"""
    sp = urllib.parse.urlsplit(clean_url(u) if '//' in u else '//' + clean_url(u))
    return host_key(u) + sp.path.rstrip('/')

def anchors(path):
    t = (RAW / path).read_text(encoding='utf-8')
    out = []
    for u, n in re.findall(r'<a [^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>', t, re.S):
        n = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', n))).strip()
        u = u.strip()
        if 'e-gov.go.jp' in u or 'j-lis.go.jp' in u or not n: continue
        out.append((n, u))
    return out

