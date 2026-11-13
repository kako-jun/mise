"""ホスト名の正規化と、取得前（ドメイン種別）フィルタ。決定論・ネットワークなし。"""
import ipaddress
import re
from pathlib import Path

BLOCKLIST_DIR = Path(__file__).parent / 'blocklist'
JP_SECOND = {'co', 'or', 'ne', 'ac', 'ad', 'ed', 'go', 'gr', 'lg'}
PREFECTURES = set('hokkaido aomori iwate miyagi akita yamagata fukushima ibaraki tochigi gunma saitama chiba tokyo kanagawa niigata toyama ishikawa fukui yamanashi nagano gifu shizuoka aichi mie shiga kyoto osaka hyogo nara wakayama tottori shimane okayama hiroshima yamaguchi tokushima kagawa ehime kochi fukuoka saga nagasaki kumamoto oita miyazaki kagoshima okinawa'.split())
GOV_LABELS = {'city', 'town', 'vill', 'pref', 'metro'}
LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')


def load_list(name):
    """blocklist/NAME のセクション付きリストを {section: [entries]} で返す。#始まりは注釈。"""
    out, section = {'': []}, ''
    for line in (BLOCKLIST_DIR / name).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]; out.setdefault(section, []); continue
        out[section].append(line.lower())
    return out


_cache = {}
def blocklist(name):
    if name not in _cache:
        _cache[name] = load_list(name)
    return _cache[name]


def normalize_host(host):
    """小文字化・末尾ドット/ポート除去。IPリテラル・不正なラベルは None。"""
    host = (host or '').strip().lower().rstrip('.')
    if not host or ':' in host or '@' in host:
        return None
    try:
        ipaddress.ip_address(host); return None
    except ValueError:
        pass
    if len(host) > 253 or not all(LABEL_RE.match(l) for l in host.split('.')):
        return None
    return host


def host_key(host):
    return host[4:] if host.startswith('www.') else host


def from_reversed(rev):
    """Common Crawl ホストグラフの逆順表記 jp.example.www -> www.example.jp"""
    return normalize_host('.'.join(reversed(rev.split('.'))))


def registrable(host):
    labels = host.split('.')
    if labels[-1] != 'jp' or len(labels) < 2:
        return '.'.join(labels[-2:])
    if labels[-2] in JP_SECOND and len(labels) >= 3:
        return '.'.join(labels[-3:])
    if labels[-2] in PREFECTURES:  # 地域ドメイン (city.kanazawa.ishikawa.jp 等) は別途
        return '.'.join(labels[-4:]) if len(labels) >= 4 else host
    return '.'.join(labels[-2:])


def suffix_match(host, entries):
    return any(host == e or host.endswith('.' + e) for e in entries)


def host_tokens(host):
    return set(re.split(r'[-.]', host))


def prefilter(host, jp_only=True):
    """取得前の決定論フィルタ。None なら通過、文字列なら除外理由。jp_only=False は既掲載サイトからのリンク先用(.jp 以外も許す。日本語かは取得後に判定)。"""
    host = normalize_host(host)
    if host is None:
        return 'invalid_host'
    labels = host.split('.')
    if labels[-1] != 'jp':
        if jp_only:
            return 'not_jp'
        second = ''
    else:
        second = labels[-2] if len(labels) >= 2 else ''
    if second == 'go' or second == 'lg':
        return 'government'  # 層1（#2）で別に扱う
    if second in PREFECTURES and len(labels) >= 4 and labels[-4] in GOV_LABELS:
        return 'government'
    if second in ('ac', 'ed', 'ad'):
        return 'institution_domain'
    if suffix_match(host, blocklist('platforms.txt')['']):
        return 'platform'
    reg = registrable(host)
    extra = host[:-len(reg) - 1] if host != reg else ''
    if extra not in ('', 'www'):
        return 'subdomain'  # 共有サービス配下の可能性が高い。将来「別単位」で扱う候補
    if suffix_match(host, blocklist('major_domains.txt')['']):
        return 'major_org'  # 大手企業・大手メディア（種リスト。残りは LLM 工程の結果を run.py reject で取り込む）
    if suffix_match(host, blocklist('ad_domains.txt')['']):
        return 'ad_domain'
    if host_tokens(host) & set(blocklist('adult_markers.txt')['host']):
        return 'adult_host'
    name = reg.split('.')[0]
    if len(name) > 30 or re.search(r'\d{6,}', name) or name.count('-') >= 4:
        return 'spam_like_name'  # 量産ドメインの形
    return None


def priority(host):
    """発見順の目安。ne.jp(旧来のISP個人向け) と短い名前をやや優先。"""
    labels = host.split('.')
    p = 5
    if len(labels) >= 2 and labels[-2] == 'co':
        p -= 2  # 企業ドメインは載せるが後回し
    if len(labels) >= 2 and labels[-2] == 'ne':
        p += 3
    if len(registrable(host).split('.')[0]) <= 12:
        p += 2
    return p
