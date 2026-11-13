#!/usr/bin/env python3
"""名簿の出典を取得して data/public-bodies/raw/ に保存する（raw はコミットしない）。

各団体のトップページへは一切アクセスしない。取得先は名簿の出典だけ:
総務省(全国地方公共団体コード)、Wikidata(SPARQL)、e-Gov(政府機関関連情報)、J-LIS(全国自治体マップ検索)。
使い方: uv run python3 scripts/public_bodies_fetch.py
"""
import datetime, hashlib, json, pathlib, re, subprocess, sys, time, urllib.parse

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
}'''
# 国の機関・独立行政法人の公式サイトを持つ Wikidata 項目（host 逆引きの第2出典用）
WIKIDATA_GOJP_QUERY = '''SELECT ?item ?site ?ja WHERE {
  ?item wdt:P856 ?site . FILTER(CONTAINS(STR(?site), ".go.jp"))
  OPTIONAL { ?item rdfs:label ?ja FILTER(lang(?ja)="ja") }
}'''

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

def get(url, name, manifest, **kw):
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
    get('https://query.wikidata.org/sparql', 'wikidata-p429.csv', manifest, data='query=' + WIKIDATA_QUERY, headers=['Accept: text/csv'])
    get('https://query.wikidata.org/sparql', 'wikidata-gojp.csv', manifest, data='query=' + WIKIDATA_GOJP_QUERY, headers=['Accept: text/csv'])
    index = get(JLIS_INDEX, 'jlis-index.html', manifest).decode('utf-8', errors='replace')
    pages = sorted(set(re.findall(r'href="(/spd/map-search/[a-z]+/cms_[0-9]+\.html)"', index)))
    for i, p in enumerate(pages):
        get('https://www.j-lis.go.jp' + p, f'jlis-{i:03d}.html', manifest)
        manifest[f'jlis-{i:03d}.html']['path'] = p
    (ROOT / 'sources-fetch.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + '\n')
    print(f'取得完了: {len(manifest)} ファイル -> {RAW}')

if __name__ == '__main__': main()
