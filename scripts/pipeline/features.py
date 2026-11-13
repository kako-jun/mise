"""取得したHTMLから特徴を抽出し、決定論ルールで候補/除外を判定する（LLMなし）。"""
import html.parser
import json
import re
import urllib.parse

from hostnames import adult_host_hint, blocklist, suffix_match

# 判定の閾値（誤判定の傾向を見て調整する。docs/personal-sites-pipeline.md 参照）
MIN_TEXT_LEN = 200          # 可視テキストの最小文字数
MIN_KANA = 20               # 日本語らしさ: かな文字数
AD_HITS_REJECT = 5          # 広告タグ数がこれ以上なら除外
AD_HITS_THIN = 2            # 広告2つ以上かつ本文が THIN_TEXT 未満なら除外
THIN_TEXT = 1500
PERSONAL_WORDS = ['個人', '趣味', '日記', '管理人', '自作', 'ブログ', '雑記', '備忘', '作品', '研究', 'ホームページ', '私の', '僕の', '自己紹介', 'リンク集', 'welcome']
KANA = re.compile(r'[぀-ヿ]')


class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = None; self.title = ''; self.desc = None; self.robots = []; self.rating = []
        self.text = []; self.skip = 0; self.in_title = False
        self.refs = []   # (tag, url)
        self.ins_ads = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or '') for k, v in attrs}
        if tag == 'html': self.lang = a.get('lang') or a.get('xml:lang')
        elif tag == 'title': self.in_title = True
        elif tag == 'meta':
            n = a.get('name', '').lower()
            if n == 'description': self.desc = a.get('content')
            elif n == 'rating': self.rating.append(a.get('content', ''))
            elif n in ('robots', 'miseresearchbot'): self.robots.append(a.get('content', ''))
        elif tag in ('script', 'iframe'):
            if a.get('src'): self.refs.append((tag, a['src']))
        elif tag == 'a' and a.get('href'): self.refs.append((tag, a['href']))
        elif tag == 'ins' and 'adsbygoogle' in a.get('class', ''): self.ins_ads += 1
        if tag in ('script', 'style', 'noscript', 'template'): self.skip += 1

    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
        if tag in ('script', 'style', 'noscript', 'template') and self.skip: self.skip -= 1

    def handle_data(self, data):
        if self.in_title: self.title += data
        elif not self.skip: self.text.append(data)


def extract(host, text, headers):
    """HTML文字列と応答ヘッダから特徴 dict を作る。"""
    p = Page()
    try:
        p.feed(text)
    except Exception:
        pass
    visible = re.sub(r'\s+', ' ', ' '.join(p.text)).strip()
    low = (p.title + ' ' + (p.desc or '') + ' ' + visible[:3000]).lower()
    ad_domains = blocklist('ad_domains.txt')['']
    base = host.removeprefix('www.')
    ad_hits = p.ins_ads; ext = 0
    for tag, ref in p.refs:
        h = urllib.parse.urlsplit(urllib.parse.urljoin('https://' + host + '/', ref)).hostname or ''
        if h and h != base and not h.endswith('.' + base):
            if tag in ('script', 'iframe'): ext += 1
            if suffix_match(h, ad_domains): ad_hits += 1
    rob = ' '.join([headers.get('x-robots-tag', ''), *p.robots]).lower()
    flags = {
        'noindex': bool(re.search(r'noindex|\bnone\b', rob)),
        'parking': next((m for m in blocklist('parking_markers.txt')[''] if m in low), None),
        'adult_rating': any(re.search(r'adult|mature|rta-5042|restricted', x, re.I) for x in p.rating),
        'adult_text': next((m for m in blocklist('adult_markers.txt')['text'] if m in low), None),
    }
    return {
        'title': p.title.strip()[:300], 'description': (p.desc or '').strip()[:500], 'lang': (p.lang or '').lower()[:20],
        'kana_count': len(KANA.findall(visible)), 'text_len': len(visible), 'ad_hits': ad_hits, 'ext_refs': ext,
        'flags': json.dumps(flags, ensure_ascii=False), 'text_head': visible[:300],
        'personal_score': sum(1 for w in PERSONAL_WORDS if w in low),
    }


def site_kind(host, title, description):
    """決定論で分かる範囲の種別(個人|事業|団体|企業|不明)。属性のみで拒否には使わない。個人/事業の区別は LLM 工程(#48)。"""
    text = (title or '') + (description or '')
    marks = blocklist('site_kind_markers.txt')
    labels = host.split('.')
    if any(m.lower() in text.lower() for m in marks['団体']) or (len(labels) >= 2 and labels[-2] in ('or', 'gr')):
        return '団体'
    if any(m in text for m in marks['企業']) or (len(labels) >= 2 and labels[-2] == 'co'):
        return '企業'
    return '不明'


def adult_attr(row):
    """成人向け属性の決定論の一次判定: 'yes'（rating メタ/RTA・年齢確認等の文言・ホスト名の手がかり）か 'unknown'。
    拒否には使わない。'no' は決定論では断定せず、後段(LLM #48)に委ねる。"""
    f = json.loads(row['flags'] or '{}')
    return 'yes' if f.get('adult_rating') or f.get('adult_text') or adult_host_hint(row['host']) else 'unknown'


def judge_http(status):
    """HTTP状態のみでの判定。None=続行 / (state, reason)"""
    if status is None: return ('failed', 'no_response')
    if status in (429, 503) or status >= 500: return ('failed', f'http_{status}')
    if status != 200: return ('rejected', f'http_{status}')
    return None


def judge(row):
    """特徴列から (state, reason)。candidate なら reason は None。"""
    f = json.loads(row['flags'] or '{}')
    if f.get('noindex'): return ('rejected', 'noindex')
    if f.get('parking'): return ('rejected', 'parking_or_default_page')
    if (row['text_len'] or 0) < MIN_TEXT_LEN: return ('rejected', 'thin_content')
    if (row['kana_count'] or 0) < MIN_KANA: return ('rejected', 'not_japanese')
    if not row['title']: return ('rejected', 'no_title')
    if row['ad_hits'] >= AD_HITS_REJECT or (row['ad_hits'] >= AD_HITS_THIN and row['text_len'] < THIN_TEXT):
        return ('rejected', 'ad_heavy')
    return ('candidate', None)
