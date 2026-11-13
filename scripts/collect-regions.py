#!/usr/bin/env python3
"""Wikidata から ISO 3166-2 第1階層の区画（コード・ja/enラベル・座標）を取得し public/regions.js を生成する。

使い方: uv run python3 scripts/collect-regions.py
  SSL_CERT_FILE を環境により指定（施設内プロキシのCAバンドル）。
第1階層 = 全国共通で「国の第1階層行政区画」(Q10864048。P279* を辿る) のクラスに属し、P300（ISO 3166-2）が
「国コード-英数字1〜3字」で、解散日 (P576) を持たない項目。解散扱いの項目は除外されるため、ISO の件数と一致しない国がある
（例: KR は ISO 17件のうち KR-29・KR-46 が Wikidata 側で解散扱いのため除外され15件。該当区画のサイトは国旗のみ表示になる）。
ラベルは en が無ければ mul（Wikidata の多言語ラベル）を使う。座標または en/mul ラベルが無い項目は出力せず、ヘッダに欠損として列挙する。
TLS: Python 3.13 の VERIFY_X509_STRICT だけを無効にする（施設内プロキシのCAが strict で弾かれるため）。証明書・ホスト名の検証は有効のまま。
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
           f"// 解散扱い（P576あり）の項目、および座標またはen/mulラベルが無い項目は含めない（座標・ラベル欠損 {len(missing)} 件: {','.join(sorted(missing)) or 'なし'}。ISO の件数と一致しない国がある: 例 KR は17件中15件。KR-29・KR-46 は解散扱いで除外され、該当区画のサイトは国旗のみ表示）。\n"
           "window.MISE_REGIONS = {\n  " + ",\n  ".join(body) + "\n};\n")
    OUT.write_text(out, encoding='utf-8')
    print(f'{len(regions)} regions, missing {len(missing)}: {sorted(missing)}', file=sys.stderr)

if __name__ == '__main__':
    main()
