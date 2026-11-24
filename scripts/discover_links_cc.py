"""URL 発見ランナーの供給元 links / cc(#235)。scripts/discover_online.py の run() から呼ばれる。

原則: mise は人気・規模・被リンク数で選別も並べ替えもしない。個人名ドメイン・個人ブログ・趣味サイトも等しく載せる。
除外するのは (a) サイトの入口でないもの(SNS・ポータル・検索・URL 短縮・アプリストア・CDN/インフラ)と
(b) 安全上の拒否(IP・内部名・認証情報つき URL・80/443 以外のポート)だけ。
順序は機械的: links は sites.id の昇順、cc は CC ファイル内の並び順とホスト名ハッシュ(crc32)の 1/N。

ローカルに状態を持たない(ファイル・キャッシュ・SQLite を作らない)。続きの位置は D1 の discovery_cursors:
  links / sites_id                      = 最後に処理し終えた sites.id(続きは id > この値)
  cc    / <graph>/part<N>/every<M>      = .jp ホスト行を何行読み終えたか('done' = 最後まで読み終えた)
"""
import re
import subprocess
import urllib.parse
import zlib

import fetcher
from hostnames import blocklist, from_reversed, host_key, normalize_host, registrable

CC_BASE = 'https://data.commoncrawl.org/'
CC_GRAPH = 'cc-main-2026-jul-aug-sep'   # pipeline/discover.py と同じリリース
CC_JP_PART = 37                          # vertices の .jp ホストが入っているパート(pipeline/discover.py と同じ)
CC_SAMPLE_EVERY = 1000
CC_FLUSH_EVERY = 100
CC_MAX_FILESIZE = 100 * 1024 * 1024
DEFAULT_MAX_SITES = 100
LINKS_PAGE = 50

# サイトの入口でないもの。ホストとそのサブドメイン全部を除く。
ENTRANCE_TREE = (
    # SNS・メッセージ・動画
    'facebook.com', 'fb.com', 'fb.me', 'instagram.com', 'twitter.com', 'x.com', 't.co', 'threads.net', 'threads.com', 'tiktok.com',
    'linkedin.com', 'pinterest.com', 'pin.it', 'reddit.com', 'redd.it', 'youtube.com', 'youtu.be', 'nicovideo.jp', 'nico.ms',
    'line.me', 'lin.ee', 'discord.com', 'discord.gg', 't.me', 'telegram.me', 'whatsapp.com', 'wa.me', 'snapchat.com', 'vk.com',
    'weibo.com', 'ok.ru', 'mixi.jp', 'bsky.app', 'twitch.tv', 'linktr.ee', 'lit.link',
    # 検索・URL 短縮
    'google.com', 'google.co.jp', 'goo.gl', 'bing.com', 'yahoo.co.jp', 'yahoo.com', 'duckduckgo.com', 'baidu.com', 'yandex.com',
    'yandex.ru', 'naver.com', 'ecosia.org', 'bit.ly', 't.ly', 'tinyurl.com', 'is.gd', 'ow.ly', 'buff.ly', 'lnkd.in', 'amzn.to',
    'amzn.asia', 'rebrand.ly', 'cutt.ly',
    # アプリストア・大手ポータル・EC・百科事典・コード置き場の本体
    'apple.com', 'microsoft.com', 'amazon.co.jp', 'amazon.com', 'rakuten.co.jp', 'wikipedia.org', 'wikimedia.org', 'wikidata.org',
    'github.com', 'crates.io', 'npmjs.com', 'pypi.org', 'zenn.dev', 'qiita.com', 'dev.to', 'medium.com', 'readspeaker.com', 'tabelog.com', 'gurunavi.com', 'gnavi.co.jp', 'hotpepper.jp', 'retty.me',
    # CDN・インフラ
    'cloudfront.net', 'akamaized.net', 'akamai.net', 'fastly.net', 'cloudflare.com', 'amazonaws.com', 'azureedge.net',
    'googleapis.com', 'gstatic.com', 'googleusercontent.com', 'doubleclick.net', 'googletagmanager.com', 'google-analytics.com',
)


def _platforms():
    """共有サービス(ブログ・ホスティング)のドメイン。blocklist/platforms.txt が正本。本体(apex・www)は入口、配下の個人サブドメインは小さなサイトなので通す。"""
    return set(blocklist('platforms.txt')[''])


def entrance_reason(host):
    """入口(サイトでないもの)なら理由、そうでなければ None。人気・規模は見ない。"""
    host = normalize_host(host)
    if not host:
        return 'invalid_host'
    if any(host == e or host.endswith('.' + e) for e in ENTRANCE_TREE):
        return 'entrance'
    if host_key(host) in _platforms():
        return 'platform_apex'
    return None


def site_level_url(href, base=None):
    """リンク -> サイト単位の URL('https://host/')。入口・認証情報つき・非 http(s)・80/443 以外のポートは None。
    パスは捨てる(台帳の単位はホスト。pipeline/discover.py の discover_links と同じ)。ただし /~user/ は個人ページが 1 サイトなので残す。"""
    href = (href or '').strip()
    if not href or href.startswith(('mailto:', 'tel:', 'javascript:', '#', 'data:')):
        return None
    try:
        p = urllib.parse.urlsplit(urllib.parse.urljoin(base, href) if base else href)
        host, port = normalize_host(p.hostname), p.port
    except ValueError:
        return None
    if p.scheme not in ('http', 'https') or not host or p.username is not None or p.password is not None or port not in (None, 80, 443):
        return None
    if entrance_reason(host):
        return None
    first = p.path.split('/')[1] if p.path.startswith('/') and len(p.path) > 1 else ''
    path = f'/{first}/' if first.startswith('~') and len(first) > 1 else '/'
    return f'{p.scheme}://{host}{path}'


def same_site(a, b):
    """同じサイトか。同じ登録ドメインでも、共有サービス(xxx.hatenablog.com 同士)は別サイト。"""
    a, b = host_key(a), host_key(b)
    if a == b:
        return True
    reg = registrable(a)
    if reg != registrable(b):
        return False
    return not any(reg == e or reg.endswith('.' + e) for e in _platforms())


HREF = re.compile(r'''<a\b[^>]*?\bhref=["']([^"']+)["']''', re.I)


def extract_outbound(html, page_url):
    """ページの外部リンク -> サイト単位 URL の一覧(出現順・重複なし)。同じサイト内・入口は除く。"""
    own = urllib.parse.urlsplit(page_url).hostname or ''
    out, seen = [], set()
    for m in HREF.finditer(html):
        u = site_level_url(m.group(1).strip(), page_url)
        if not u or u in seen:
            continue
        if same_site(urllib.parse.urlsplit(u).hostname, own):
            continue
        seen.add(u)
        out.append(u)
    return out


# ------------------------------------------------------------------ links
def run_links(sink, cursors, pf, client, max_sites=DEFAULT_MAX_SITES):
    """公開済みサイト(sites)を id 昇順に、続きから max_sites 件ずつ。トップを取り、外部リンク先を候補にする。"""
    st = sink.stats.rows['links']
    last = int(cursors.get('links', 'sites_id') or 0)
    fetched_ok, reasons, processed, ended = 0, {}, 0, False
    while processed < max_sites and sink.room('links') and not sink.stats.mismatch and not ended:
        rows = client.query('SELECT id, url FROM sites WHERE id > ? ORDER BY id LIMIT ?', (last, min(LINKS_PAGE, max_sites - processed)))
        if not rows:
            sink.stats.notes.append(f'links: sites を最後まで読んだ(続き id>{last})')
            break
        for row in rows:
            if not sink.room('links') or sink.stats.mismatch:
                ended = True
                break
            before = st['over_limit']
            html, reason = pf.get(row['url'] or '')
            if html is None:
                reason = reason or 'unknown'
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                fetched_ok += 1
                for u in extract_outbound(html, row['url']):
                    sink.offer('links', 'outbound-link', u)
            sink.flush()   # 入れて検証してからカーソルを進める
            if st['over_limit'] > before or sink.stats.mismatch:
                ended = True   # 上限で捨てた候補がある。このサイトは次回やり直す(既知は除かれる)
                break
            last = row['id']
            cursors.set('links', 'sites_id', last)
            processed += 1
    sink.stats.notes.append(f'links: 処理 {processed} サイト(取得できた {fetched_ok} / 取得できず {sum(reasons.values())} {dict(sorted(reasons.items()))}) カーソル id={last}')


# ------------------------------------------------------------------ cc
def curl_lines(url, max_filesize=CC_MAX_FILESIZE):
    """gzip テキストを curl でストリーム取得して行を yield する。ディスクに書かない。途中で閉じれば curl も止める。"""
    proc = subprocess.Popen(['curl', '--silent', '--show-error', '--fail', '--max-time', '1800', '--max-filesize', str(max_filesize),
                             '--user-agent', fetcher.USER_AGENT, url], stdout=subprocess.PIPE)
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    tail = b''
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            parts = (tail + dec.decompress(chunk)).split(b'\n')
            tail = parts.pop()
            for line in parts:
                yield line.decode('utf-8', 'replace')
        tail += dec.flush()
        if tail:
            yield tail.decode('utf-8', 'replace')
        if proc.wait() != 0:
            raise RuntimeError(f'curl failed: {url}')
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()


def cc_part_url(open_lines, graph=CC_GRAPH, part=CC_JP_PART):
    paths = list(open_lines(f'{CC_BASE}projects/hyperlinkgraph/{graph}/host/{graph}-host-vertices.paths.gz'))
    return CC_BASE + [p.strip() for p in paths if p.strip()][part]


def run_cc(sink, cursors, open_lines=curl_lines, sample_every=CC_SAMPLE_EVERY, graph=CC_GRAPH, part=CC_JP_PART):
    """ホストグラフの .jp ホストを決定論的にサンプル(crc32 % N == 0。人気では選ばない)し、ストリームのまま候補にする。"""
    name = f'{graph}/part{part}/every{sample_every}'
    state = cursors.get('cc', name) or '0'
    st = sink.stats.rows['cc']
    if state == 'done':
        sink.stats.notes.append(f'cc: {name} は読み終えている(別リリース・別の sample_every は別カーソル)')
        return
    skip, pos, eof = int(state), 0, True
    stream = open_lines(cc_part_url(open_lines, graph, part))
    try:
        for line in stream:
            _, _, rev = line.rstrip('\n').partition('\t')
            if not rev.startswith('jp.'):
                continue
            pos += 1
            if pos <= skip or zlib.crc32(rev.encode()) % sample_every:
                continue
            host = from_reversed(rev)
            if not host:
                continue
            left = sink.room_left('cc')
            if left is not None and left <= 0:
                sink.flush()               # この行より前は全部入れて検証した
                if sink.stats.mismatch:
                    return
                cursors.set('cc', name, pos - 1)
                left = sink.room_left('cc')
                if left is not None and left <= 0:
                    eof = False            # 上限。この行は次回に読む
                    break
            if entrance_reason(host):
                st['fetched'] += 1
                st['invalid'] += 1
                continue
            sink.offer('cc', 'jp-host', f'https://{host}/')
            if len(sink.pending) >= CC_FLUSH_EVERY:
                sink.flush()
                if sink.stats.mismatch:
                    return
                cursors.set('cc', name, pos)
    finally:
        close = getattr(stream, 'close', None)
        if close:
            close()
    if eof:
        sink.flush()
        if sink.stats.mismatch:
            return
        cursors.set('cc', name, 'done')
        sink.stats.notes.append(f'cc: {name} を最後まで読んだ(.jp ホスト {pos} 行)')
    else:
        sink.stats.notes.append(f'cc: {name} 上限で中断(続きは .jp ホスト {pos - 1} 行目の後)')


def run_sources(sink, cursors, sources, client, pf, extra=None):
    """discover_online.run() の登録フック。extra: max_sites / cc_sample_every / cc_open(テスト用)。"""
    extra = extra or {}
    if 'links' in sources and not sink.stats.mismatch:
        run_links(sink, cursors, pf, client, extra.get('max_sites', DEFAULT_MAX_SITES))
    if 'cc' in sources and not sink.stats.mismatch:
        run_cc(sink, cursors, extra.get('cc_open', curl_lines), extra.get('cc_sample_every', CC_SAMPLE_EVERY))
