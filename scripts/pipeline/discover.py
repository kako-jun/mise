"""発見元。出典と発見日時を台帳に記録する。取得（サイトへのアクセス）はしない発見元と、する発見元がある。"""
import gzip
import json
import re
import subprocess
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import fetcher
import frontier
from hostnames import from_reversed, host_key, normalize_host, prefilter, priority

ROOT = frontier.ROOT
CC_BASE = 'https://data.commoncrawl.org/'
CC_GRAPH = 'cc-main-2026-jul-aug-sep'   # ホストレベルのウェブグラフのリリース
CC_JP_PART = 37                          # vertices の .jp ホストが入っているパート。リリースを変えたら各パート先頭を Range 3KB ずつ見て再確認（docs 参照）
CACHE = ROOT / 'var' / 'cache'


MAX_DOWNLOAD = 100 * 1024 * 1024   # パート(約27〜40MB)の上限


def _curl(url, dest):
    """一時ファイルへ書き、完了後に rename する（中断で壊れたファイルをキャッシュに残さない）。"""
    dest = Path(dest); tmp = dest.with_name(dest.name + '.part')
    try:
        subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', '300', '--max-filesize', str(MAX_DOWNLOAD),
                        '--user-agent', fetcher.USER_AGENT, '-o', str(tmp), url], check=True)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def cc_part_path(graph, part):
    """ホストグラフ vertices の1パート（約30〜40MB）をキャッシュへ取得して返す。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = CACHE / f'{graph}-vertices.paths.gz'
    if not paths.exists():
        _curl(f'{CC_BASE}projects/hyperlinkgraph/{graph}/host/{graph}-host-vertices.paths.gz', paths)
    rel = gzip.open(paths, 'rt').read().split()[part]
    dest = CACHE / Path(rel).name
    if not dest.exists():
        _curl(CC_BASE + rel, dest)
    return dest


def discover_commoncrawl(db, part_file=None, sample_every=1000, limit=None, graph=CC_GRAPH, part=CC_JP_PART):
    """ホストグラフの .jp ホストを決定論的にサンプルして台帳へ入れる（hash(host) % sample_every == 0）。"""
    path = Path(part_file) if part_file else cc_part_path(graph, part)
    added = seen = 0
    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            _, _, rev = line.rstrip('\n').partition('\t')
            if not rev.startswith('jp.'):
                continue
            seen += 1
            if zlib.crc32(rev.encode()) % sample_every:
                continue
            host = from_reversed(rev)
            if not host:
                continue
            if frontier.add(db, host_key(host), host, f'https://{host}/', 'commoncrawl-webgraph', f'{graph} vertices {path.name}', 0, priority=priority(host)):
                added += 1
            if limit and added >= limit:
                break
    frontier.set_cursor(db, 'commoncrawl-webgraph', 'last_run', f'{graph}/{path.name} jp_hosts_seen={seen} sample_every={sample_every} added={added}')
    db.commit()
    return {'jp_hosts_seen': seen, 'added': added}


def seed_urls():
    """既存掲載サイトのURL（data/collection-*.json）。"""
    urls = []
    for p in sorted((ROOT / 'data').glob('collection-*.json')):
        for r in json.loads(p.read_text(encoding='utf-8')):
            if r.get('url'):
                urls.append(r['url'])
    return urls


def discover_links(db, seeds=None, max_links=300):
    """既存掲載サイトのトップページから外部リンク先ホストを深さ1で発見する。robots.txt 遵守の取得をシードごとに行う。"""
    added = 0; fetched = 0
    for url in (seeds or seed_urls()):
        host = urllib.parse.urlsplit(url).hostname
        r = fetcher.fetch_site(host)
        if r['error'] or r['status'] != 200:
            continue
        fetched += 1
        text = fetcher.decode_body(r['body'], r['headers'])
        found = 0
        for href in re.findall(r'<a\s[^>]*href=["\']([^"\'#]+)', text, re.I):
            p = urllib.parse.urlsplit(urllib.parse.urljoin(r['final_url'], href))
            h = normalize_host(p.hostname) if p.scheme in ('http', 'https') else None
            if not h or fetcher.same_site(h, host):
                continue
            if frontier.add(db, host_key(h), h, f'{p.scheme}://{h}/', 'links', r['final_url'], 1, priority=priority(h) + 3):
                added += 1; found += 1
            if found >= max_links:
                break
    frontier.set_cursor(db, 'links', 'last_run', f'seeds_fetched={fetched} added={added}')
    db.commit()
    return {'seeds_fetched': fetched, 'added': added}
