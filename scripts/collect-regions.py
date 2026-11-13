#!/usr/bin/env python3
"""Wikidata から ISO 3166-2 第1階層の区画（コード・ja/enラベル・座標）を取得し public/regions.js を生成する。

使い方: uv run python3 scripts/collect-regions.py
  SSL_CERT_FILE を環境により指定（施設内プロキシのCAバンドル）。
第1階層 = P300（ISO 3166-2）が「国コード-英数字1〜3字」で、国の項目を P131（所在地行政区画）に直接持つ項目。
日本だけは「日本の都道府県」(Q50337) のインスタンスで取る（北海道は P131 なし、奈良は P131=近畿のため）。
ラベルは en が無ければ mul（Wikidata の多言語ラベル）を使う。
出典: Wikidata (CC0)。取得日・件数・欠損は public/regions.js の先頭コメントと docs/architecture.md に残る。
"""
import json, re, ssl, sys, time, urllib.parse, urllib.request, datetime, pathlib

ENDPOINT = 'https://query.wikidata.org/sparql'
UA = 'mise-directory-region-collector/1.0 (https://github.com/kako-jun/mise)'
# サイトが使う国（public/countries.js の MISE_FLAGS と同じ13か国）。国を足したらここにも足す。
COUNTRIES = ['JP', 'US', 'GB', 'DE', 'FR', 'KR', 'CN', 'AU', 'CA', 'BR', 'IN', 'IT', 'RU']
OUT = pathlib.Path(__file__).resolve().parent.parent / 'public' / 'regions.js'

def query(cc):
    # 日本は「日本の都道府県」(Q50337) の P31 で取る（北海道は P131 が無く、奈良は P131=近畿のため P131 では取りこぼす）。
    # それ以外は国の項目を P131 に直接持つもの = 第1階層。
    scope = '?item wdt:P31/wdt:P279* wd:Q10864048.'  # 国の第1階層行政区画（first-level administrative division of a country）
    q = f'''
SELECT ?code ?ja ?en ?mul ?coord WHERE {{
  {scope}
  ?item wdt:P300 ?code.
  FILTER NOT EXISTS {{ ?item wdt:P576 ?dissolved. }}
  FILTER(REGEX(?code, "^{cc}-[A-Z0-9]{{1,3}}$"))
  OPTIONAL {{ ?item wdt:P625 ?coord. }}
  OPTIONAL {{ ?item rdfs:label ?ja. FILTER(LANG(?ja)="ja") }}
  OPTIONAL {{ ?item rdfs:label ?en. FILTER(LANG(?en)="en") }}
  OPTIONAL {{ ?item rdfs:label ?mul. FILTER(LANG(?mul)="mul") }}
}}'''
    req = urllib.request.Request(ENDPOINT + '?' + urllib.parse.urlencode({'query': q, 'format': 'json'}),
                                 headers={'User-Agent': UA, 'Accept': 'application/sparql-results+json'})
    # Python 3.13 の厳格検証は施設内プロキシのCAを弾くことがある。証明書検証自体は有効のまま strict フラグだけ外す。
    ctx = ssl.create_default_context(); ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        return json.load(r)['results']['bindings']

def main():
    regions, missing = {}, []
    seen = set()
    for cc in COUNTRIES:
        rows = query(cc)
        for b in rows:
            code = b['code']['value']
            m = re.match(r'Point\(([-\d.]+) ([-\d.]+)\)', b.get('coord', {}).get('value', ''))
            ja, en = b.get('ja', {}).get('value'), b.get('en', {}).get('value') or b.get('mul', {}).get('value')
            seen.add(code)
            if not (m and en) or code in regions: continue  # 複数座標・複数ラベルは最初の1件
            regions[code] = {'ja': ja or en, 'en': en, 'lat': round(float(m.group(2)), 2), 'lon': round(float(m.group(1)), 2)}
        print(cc, sum(1 for c in regions if c.startswith(cc + '-')), file=sys.stderr)
        time.sleep(2)
    missing = sorted(seen - set(regions))
    today = datetime.date.today().isoformat()
    body = []
    for c, v in sorted(regions.items()):
        ja = v['ja'].replace("'", "\\'"); en = v['en'].replace("'", "\\'")
        body.append(f"'{c}':{{ja:'{ja}',en:'{en}',lat:{v['lat']},lon:{v['lon']}}}")
    out = (f"// ISO 3166-2 第1階層の区画 -> 名前(日英)と代表座標。scripts/collect-regions.py が生成する（手で編集しない）。\n"
           f"// 出典: Wikidata (CC0) の P300(ISO 3166-2)・P625(座標)・ja/enラベル。取得日 {today}。対象国 {','.join(COUNTRIES)}。件数 {len(regions)}。\n"
           f"// 座標またはenラベルが無い区画は含めない（欠損 {len(missing)} 件: {','.join(sorted(missing))}）。\n"
           "window.MISE_REGIONS = {\n  " + ",\n  ".join(body) + "\n};\n")
    OUT.write_text(out, encoding='utf-8')
    print(f'{len(regions)} regions, missing {len(missing)}: {sorted(missing)}', file=sys.stderr)

if __name__ == '__main__':
    main()
