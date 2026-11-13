#!/usr/bin/env python3
"""名簿の出典を取得して data/public-bodies/raw/ に保存する（raw はコミットしない）。

各団体のトップページへは一切アクセスしない。取得先は名簿の出典だけ:
総務省(全国地方公共団体コード)、Wikidata(SPARQL)、e-Gov(政府機関関連情報)、J-LIS(全国自治体マップ検索)。
使い方: uv run python3 scripts/public_bodies_fetch.py
"""
import datetime, hashlib, json, pathlib, re, subprocess, sys, time, urllib.parse
from public_bodies_common import anchors, clean_url

UA = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)'
ROOT = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies'
RAW = ROOT / 'raw'
SLEEP = 1.0  # 同一ホストへの連続アクセス間隔(秒)。並列にはしない

SOUMU_PAGE = 'https://www.soumu.go.jp/denshijiti/code.html'
EGOV = 'https://www.e-gov.go.jp/government-directory/'
JLIS_INDEX = 'https://www.j-lis.go.jp/spd/map-search/cms_1069.html'
WIKIDATA_QUERY = '''SELECT ?item ?code ?site ?rank ?ja ?en ?kana ?end ?succ ?succcode WHERE {
  ?item wdt:P429 ?code .
  OPTIONAL { ?item p:P856 ?st . ?st ps:P856 ?site . ?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank) }
  OPTIONAL { ?item rdfs:label ?ja FILTER(lang(?ja)="ja") }
  OPTIONAL { ?item rdfs:label ?en FILTER(lang(?en)="en") }
  OPTIONAL { ?item wdt:P1814 ?kana }
  OPTIONAL { ?item wdt:P576 ?end }
  OPTIONAL { ?item wdt:P1366 ?succ . OPTIONAL { ?succ wdt:P429 ?succcode } }
} ORDER BY ?code ?item ?site ?succ'''
# 国の機関・独立行政法人の第2出典（e-Gov のホストを Wikidata の公式サイト(P856)で逆引き）。
# 全件走査の FILTER(CONTAINS(...)) はタイムアウトや部分結果で結果が揺れたため、
# e-Gov の各 URL から作った候補 URL（scheme・www 有無・末尾スラッシュの違い）を VALUES で完全一致検索する。
def gojp_query():
    urls = set()
    for f in ('egov-ministries-and-agencies.html', 'egov-parliament-and-court.html', 'egov-IAAs.html'):
        for _, u in anchors(f):
            sp = urllib.parse.urlsplit(clean_url(u))
            h = (sp.hostname or '').lower()
            bare = h[4:] if h.startswith('www.') else h
            paths = {'', '/', sp.path.rstrip('/'), sp.path.rstrip('/') + '/'}
            for scheme in ('http', 'https'):
                for host in (bare, 'www.' + bare, h):
                    for path in paths: urls.add(f'{scheme}://{host}{path}')
    values = ' '.join(f'<{u}>' for u in sorted(urls))
    return ('SELECT ?item ?site ?ja WHERE { VALUES ?site { ' + values + ' } ?item wdt:P856 ?site . '
            'OPTIONAL { ?item rdfs:label ?ja FILTER(lang(?ja)="ja") } } ORDER BY ?item ?site')

def curl(url, out=None, data=None, headers=()):
    cmd = ['curl', '--silent', '--show-error', '--location', '--max-time', '180', '--user-agent', UA,
           '--dump-header', '-', '--write-out', '\n%{http_code}']
    for h in headers: cmd += ['-H', h]
    if data: cmd += ['--data-urlencode', data]
    cmd.append(url)
    r = subprocess.run(cmd, check=True, capture_output=True)
    blob = r.stdout
    code = int(blob.rsplit(b'\n', 1)[1]); blob = blob.rsplit(b'\n', 1)[0]
    # ヘッダブロック(プロキシのCONNECT・リダイレクト含む)を読み飛ばして本文へ
    last_headers = b''
    while blob.startswith(b'HTTP/'):
        head, _, blob = blob.partition(b'\r\n\r\n')
        last_headers = head
    lm = re.search(rb'(?im)^last-modified:\s*(.+?)\r?$', last_headers)
    return code, blob, (lm.group(1).decode() if lm else None)

def sparql(name, query, manifest, min_rows, dest=None):
    """WDQS は 504 や部分結果で揺れる。最大3回試し、行数が下限未満なら取り込まず中止する。"""
    for attempt in range(3):
        try:
            code, body, lm = curl('https://query.wikidata.org/sparql', data='query=' + query, headers=['Accept: text/csv'])
        except subprocess.CalledProcessError:
            code, body = 0, b''
        rows = body.count(b'\n') - 1
        if code == 200 and rows >= min_rows: break
        print(f'{name}: 試行{attempt + 1} HTTP {code} rows={rows}（下限 {min_rows}）。待って再試行', file=sys.stderr)
        time.sleep(20 * (attempt + 1))
    else:
        raise SystemExit(f'{name}: 行数が下限未満のまま。揺れた結果は取り込まない')
    return get_bytes(name, body, lm, 'https://query.wikidata.org/sparql', manifest, dest)

def get_bytes(name, body, lm, url, manifest, dest=None):
    d = dest or RAW
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(body)
    manifest[name] = {'url': url, 'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                      'last_modified': lm, 'bytes': len(body), 'rows': body.count(b'\n') - 1, 'sha256': hashlib.sha256(body).hexdigest()}
    time.sleep(SLEEP)
    return body

_ROBOTS = {}
def allowed(url):
    """出典サイトの robots.txt を host ごとに1回だけ取得して確認する。取得できない(400/404等)ときは規則なし扱い。"""
    import urllib.robotparser
    sp = urllib.parse.urlsplit(url); host = f'{sp.scheme}://{sp.netloc}'
    if host not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        try:
            code, body, _ = curl(host + '/robots.txt')
        except subprocess.CalledProcessError:
            code, body = 0, b''
        if code == 200 and not body.lstrip().startswith(b'<'): rp.parse(body.decode('utf-8', errors='replace').splitlines())
        else: rp.parse([])
        _ROBOTS[host] = rp; time.sleep(SLEEP)
    return _ROBOTS[host].can_fetch(UA, url)

def get(url, name, manifest, **kw):
    if not allowed(url): raise SystemExit(f'{url}: robots.txt で禁止されているため取得しない')
    code, body, lm = curl(url, **kw)
    if code != 200: raise SystemExit(f'{url}: HTTP {code}')
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / name).write_bytes(body)
    manifest[name] = {'url': url, 'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                      'last_modified': lm, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    time.sleep(SLEEP)
    return body

def main():
    manifest = {}
    page = get(SOUMU_PAGE, 'soumu-code.html', manifest).decode('cp932', errors='replace')
    # 総務省ページから xlsx の URL を取り出す。見出しの順: コード一覧、改正一覧、一部事務組合等コード
    xlsx = re.findall(r'href="(/main_content/\d+\.xlsx)"', page)
    if len(xlsx) < 3: raise SystemExit('総務省ページの構成が変わった（xlsx リンクが3つ未満）。要確認')
    for key, path in zip(('soumu-codes.xlsx', 'soumu-kaisei.xlsx', 'soumu-kumiai.xlsx'), xlsx[:3]):
        get('https://www.soumu.go.jp' + path, key, manifest)
    for key in ('local-governments', 'ministries-and-agencies', 'parliament-and-court', 'IAAs'):
        get(f'{EGOV}{key}.html', f'egov-{key}.html', manifest)
    sparql('wikidata-p429.csv', WIKIDATA_QUERY, manifest, min_rows=1900)
    # 第2出典の根拠は commit する（Wikidata は実行時点の内容で変わるため、台帳の再現は evidence/ を固定入力にする）
    sparql('wikidata-gojp.csv', gojp_query(), manifest, min_rows=60, dest=ROOT / 'evidence')
    index = get(JLIS_INDEX, 'jlis-index.html', manifest).decode('utf-8', errors='replace')
    pages = sorted(set(re.findall(r'href="(/spd/map-search/[a-z]+/cms_[0-9]+\.html)"', index)))
    for i, p in enumerate(pages):
        get('https://www.j-lis.go.jp' + p, f'jlis-{i:03d}.html', manifest)
        manifest[f'jlis-{i:03d}.html']['path'] = p
    (ROOT / 'sources-fetch.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + '\n')
    print(f'取得完了: {len(manifest)} ファイル -> {RAW}')

if __name__ == '__main__': main()
