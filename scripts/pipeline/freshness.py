"""更新の兆候の決定論抽出（Issue #129。LLM なし・ネットワークなしの純関数）。

返す日付は ISO 文字列 'YYYY-MM-DD'。年だけ・年月だけの表記は「その期間の最後の日」に丸める
（© 2019 は 2019-12-31。古さの過大評価＝実際は活動中のサイトの誤拒否を避けるため寛容側に倒す）。
未来日・1995 年より前は採らない。候補が複数あれば最新を採る。
"""
import calendar
import datetime
import email.utils
import re

MIN_YEAR = 1995
_ERA = {'令和': 2018, '平成': 1988, '昭和': 1925}

_YEAR = r'((?:19|20)\d{2})'
# © / Copyright の後ろの年（範囲 2015-2019 は大きい方）
_COPY = re.compile(r'(?:©|&copy;|\(c\)|copyright)[^0-9]{0,24}' + _YEAR + r'(?:\s*[-–~〜,]\s*' + _YEAR + r')?', re.I)
# 更新を示す語の直後の日付（年月日・年月・ISO/スラッシュ/ドット区切り）
_KEY = r'(?:最終更新日?|最終更新日時|更新日時?|更新履歴|サイト更新|last[\s_-]*(?:updated?|modified)|updated?|modified)'
_UPD = re.compile(_KEY + r'[^0-9令平昭]{0,16}' + _YEAR + r'\s*(?:年|[/.\-])\s*(\d{1,2})(?:\s*(?:月|[/.\-])\s*(\d{1,2}))?', re.I)
_UPD_ERA = re.compile(_KEY + r'[^0-9令平昭]{0,16}(令和|平成|昭和)\s*(\d{1,2}|元)\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?', re.I)


def _today(today=None):
    return today or datetime.date.today()


def _ymd(y, m=None, d=None, today=None):
    """年/年月/年月日 -> date（粗い表記は期間末）。不正・未来・古すぎは None。"""
    try:
        y = int(y)
        if y < MIN_YEAR:
            return None
        if m is None:
            out = datetime.date(y, 12, 31)
        else:
            m = int(m)
            if d is None:
                out = datetime.date(y, m, calendar.monthrange(y, m)[1])
            else:
                out = datetime.date(y, m, int(d))
    except (ValueError, TypeError):
        return None
    t = _today(today)
    if out > t:
        # 当年・当月の粗い表記は今日に丸める。それ以外の未来日は誤検出として捨てる
        if (m is None and y == t.year) or (d is None and m is not None and (y, int(m)) == (t.year, t.month)):
            return t
        return None
    return out


def footer_signal(text, today=None):
    """可視テキストから最新の日付を (iso, source) で返す（なければ (None, None)）。
    source は 'footer_updated'（「最終更新」類の表記）か 'footer_copyright'（© 年のみ。弱い兆候）。同日は更新表記を優先。"""
    if not text:
        return None, None
    found = []  # (date, rank, source) rank 大きいほど優先
    for m in _COPY.finditer(text[-4000:] + ' ' + text[:1500]):
        for g in m.groups():
            if g:
                found.append((_ymd(g, today=today), 0, 'footer_copyright'))
    upd = []
    for m in _UPD.finditer(text):
        upd.append(_ymd(m.group(1), m.group(2), m.group(3), today))
    for m in _UPD_ERA.finditer(text):
        base = _ERA[m.group(1)]
        n = 1 if m.group(2) == '元' else int(m.group(2))
        upd.append(_ymd(base + n, m.group(3), m.group(4), today))
    found += [(d, 1, 'footer_updated') for d in upd]
    found = [f for f in found if f[0]]
    if not found:
        return None, None
    d, _, src = max(found, key=lambda f: (f[0], f[1]))
    return d.isoformat(), src


def footer_date(text, today=None):
    """footer_signal の日付だけ。"""
    return footer_signal(text, today)[0]


def header_date(headers, today=None):
    """Last-Modified。動的ページは取得時刻をそのまま返すので、Date ヘッダ/今日から 1 日以内のものは捨てる。"""
    v = (headers or {}).get('last-modified')
    if not v:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    d = dt.date()
    if d.year < MIN_YEAR:
        return None
    t = _today(today)
    if d > t or (t - d).days <= 1:
        return None
    ref = (headers or {}).get('date')
    if ref:
        try:
            if abs((email.utils.parsedate_to_datetime(ref) - dt).total_seconds()) <= 86400:
                return None
        except (TypeError, ValueError):
            pass
    return d.isoformat()


_ISO = re.compile(r'(\d{4})-(\d{2})(?:-(\d{2}))?')
_SITEMAP_LASTMOD = re.compile(r'<lastmod>\s*([^<\s]+)\s*</lastmod>', re.I)
_FEED_TAGS = re.compile(r'<(?:pubDate|published|updated|dc:date)[^>]*>\s*(?:<!\[CDATA\[)?\s*([^<\]]+?)\s*(?:\]\]>)?\s*</(?:pubDate|published|updated|dc:date)>', re.I)


def _parse_stamp(s, today=None):
    s = s.strip()
    m = _ISO.match(s)
    if m:
        return _ymd(m.group(1), m.group(2), m.group(3), today)
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        return None
    return _ymd(dt.year, dt.month, dt.day, today) if dt else None


def sitemap_date(xml, today=None):
    ds = [_parse_stamp(s, today) for s in _SITEMAP_LASTMOD.findall(xml or '')]
    ds = [d for d in ds if d]
    return max(ds).isoformat() if ds else None


def feed_date(xml, today=None):
    ds = [_parse_stamp(s, today) for s in _FEED_TAGS.findall(xml or '')]
    ds = [d for d in ds if d]
    return max(ds).isoformat() if ds else None


_LINK = re.compile(r'<link\b[^>]*>', re.I)


def feed_links(html):
    """<link rel="alternate" type="application/rss+xml|atom+xml" href=...> の href（出現順）。"""
    out = []
    for tag in _LINK.findall(html or ''):
        low = tag.lower()
        if 'alternate' in low and re.search(r'application/(?:rss|atom)\+xml', low):
            m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if m:
                out.append(m.group(1).replace('&amp;', '&'))
    return out


def latest(candidates):
    """[(iso|None, source)] から最新を (iso, source) で返す。なければ (None, None)。同日は先頭（優先度順）を採る。"""
    best = (None, None)
    for iso, src in candidates:
        if iso and (best[0] is None or iso > best[0]):
            best = (iso, src)
    return best
