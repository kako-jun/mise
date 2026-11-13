#!/usr/bin/env python3
"""取得済みの出典（data/public-bodies/raw/）から名簿の台帳 CSV を作る。決定論のみ・LLM 推測なし。

出力:
  data/public-bodies/ledger.csv  台帳（正本）。2 つ以上の独立した出典でホスト名が一致したもの + 廃止/合併の履歴
  data/public-bodies/review.csv  要確認（出典 1 つ・不一致・出典なし）。掲載しない
一致の定義: 正規化したホスト名（www. の有無・http/https・末尾スラッシュ・パスを無視）が、独立した 2 出典で一致。
  URL の出典: Wikidata(P856) / e-Gov 政府機関関連情報 / J-LIS 全国自治体マップ検索。
  ドメイン区分(.lg.jp/.go.jp 等)は属性として記録するだけで、一致の出典には数えない。
使い方: uv run --with openpyxl python3 scripts/public_bodies_build.py
"""
import csv, datetime, hashlib, json, pathlib, re, sys, unicodedata, urllib.parse, collections, html
import openpyxl

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies'
RAW = ROOT / 'raw'
FIELDS = ['code', 'name_ja', 'name_en', 'reading_kana', 'type', 'prefecture', 'url', 'host', 'domain_class',
          'sources', 'status', 'status_date', 'successor_code', 'note']
REVIEW_FIELDS = ['code', 'name_ja', 'type', 'prefecture', 'reason', 'wikidata_urls', 'egov_url', 'jlis_url',
                 'domain_class', 'wikidata_name', 'note']

def host_key(u):
    if not u: return ''
    p = urllib.parse.urlsplit(u.strip() if '//' in u else '//' + u.strip())
    h = (p.hostname or '').lower().rstrip('.')
    return h[4:] if h.startswith('www.') else h

def domain_class(h):
    if h.endswith('.lg.jp'): return 'lg.jp'
    if h.endswith('.go.jp'): return 'go.jp'
    if re.match(r'^(city|town|vill|pref|metro)\.[a-z0-9-]+\.[a-z0-9-]+\.jp$', h) or re.match(r'^(pref|metro)\.[a-z0-9-]+\.jp$', h): return 'geo.jp'
    if h.endswith('.jp'): return 'other.jp'
    return 'other'

def clean_url(u):
    return re.sub(r'/index\.(html?|php)$', '/', u.strip())

def code6(v):
    return str(int(v)).zfill(6) if not isinstance(v, str) else v.strip().zfill(6)

def kata(s): return unicodedata.normalize('NFKC', s or '')

def hira2kata(s): return ''.join(chr(ord(c) + 96) if 'ぁ' <= c <= 'ゖ' else c for c in (s or ''))

def excel_date(v):
    if isinstance(v, datetime.datetime): return v.date().isoformat()
    if isinstance(v, (int, float)): return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(v))).isoformat()
    return None

def load_manifest():
    return json.loads((ROOT / 'sources-fetch.json').read_text())

# ---- 総務省 ----
def load_soumu(manifest):
    wb = openpyxl.load_workbook(RAW / 'soumu-codes.xlsx', read_only=True)
    cur, wards, asof = {}, set(), wb.worksheets[0].title
    for r in list(wb.worksheets[0].iter_rows(values_only=True))[1:]:
        if not r[0]: continue
        code = code6(r[0]); pref, muni = r[1], r[2]
        cur[code] = {'pref': pref, 'name': muni or pref, 'kana': kata(r[4] if muni else r[3]), 'is_pref': muni is None}
    for r in list(wb.worksheets[1].iter_rows(values_only=True))[1:]:
        # 政令指定都市シートは市本体と行政区を含む。現行シートに無い区コードを行政区として足す
        if r[0] and r[2] and r[2].endswith('区') and code6(r[0]) not in cur:
            c = code6(r[0]); wards.add(c)
            cur[c] = {'pref': r[1], 'name': r[2], 'kana': kata(r[4]), 'is_pref': False}
    return cur, wards, asof

def load_kaisei():
    """改正一覧表: 欠番(廃止)行を {旧コード: 名称・都道府県・施行日・事由・同じ改正ブロックの新設コード} で返す。
    ブロック = 施行日が書かれた行から次の施行日行の直前まで。"""
    ws = openpyxl.load_workbook(RAW / 'soumu-kaisei.xlsx', read_only=True).worksheets[0]
    out, pref, date, reason, block = {}, None, None, None, 0
    created = collections.defaultdict(list)
    for r in list(ws.iter_rows(values_only=True))[4:]:
        if r[4]: pref = r[4]
        d = excel_date(r[13]) if r[13] not in (None, '〃') else None
        if d: date = d; block += 1
        if r[12] not in (None, '〃') and not isinstance(r[12], (int, float, datetime.datetime)): reason = r[12]
        if r[5] and r[8] == '欠番' and r[6]:
            out[code6(r[5])] = {'pref': pref, 'name': r[6], 'kana': hira2kata(r[7]), 'date': date, 'reason': reason, 'block': block}
        if r[8] == '新設' and r[9] not in (None, '〃', '削除'):
            created[block].append(code6(r[9]))
    for v in out.values(): v['created'] = created.get(v['block'], [])
    return out

def kaisei_reason_for(code, rows, reason_map):
    return reason_map.get(code, '')

# ---- Wikidata ----
def load_wikidata():
    byc = {}
    for r in csv.DictReader(open(RAW / 'wikidata-p429.csv', encoding='utf-8')):
        e = byc.setdefault(r['code'], {'urls': {}, 'ja': r['ja'], 'en': r['en'], 'kana': r['kana'], 'end': None, 'succ': set(), 'item': r['item'].rsplit('/', 1)[-1]})
        if r['site']: e['urls'][r['site']] = r['rank'].endswith('PreferredRank')
        if r['end']: e['end'] = r['end'][:10]
        if r['succcode']: e['succ'].add(r['succcode'])
    return byc

def load_wikidata_gojp():
    by_host = collections.defaultdict(set)
    for r in csv.DictReader(open(RAW / 'wikidata-gojp.csv', encoding='utf-8')):
        by_host[host_key(r['site'])].add(r['ja'] or r['item'].rsplit('/', 1)[-1])
    return by_host

# ---- e-Gov ----
def anchors(path):
    t = (RAW / path).read_text(encoding='utf-8')
    out = []
    for u, n in re.findall(r'<a [^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>', t, re.S):
        n = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', n))).strip()
        u = u.strip()
        if 'e-gov.go.jp' in u or 'j-lis.go.jp' in u or not n: continue
        out.append((n, u))
    return out

# ---- J-LIS ----
def load_jlis(manifest):
    out, skip = [], ('j-lis', 'jpki', 'lg-waps', 'kojinbango', 'harp.lg.jp')
    dates = {}
    for f in sorted(RAW.glob('jlis-0*.html')):
        t = f.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'更新日：\s*(\d+)年(\d+)月(\d+)日', t)
        d = f'{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}' if m else ''
        for u, n in re.findall(r'<a [^>]*href="(https?://[^"]+)"[^>]*>([^<]+)', t):
            if any(s in u for s in skip): continue
            out.append((n.strip(), u.strip(), d))
    return out

def main():
    manifest = load_manifest()
    fetch_day = lambda k: manifest[k]['fetched_at'][:10]
    cur, wards, soumu_asof = load_soumu(manifest)
    kaisei = load_kaisei()
    wd = load_wikidata()
    wd_gojp = load_wikidata_gojp()

    name_to_codes = collections.defaultdict(list)
    for c, e in cur.items(): name_to_codes[e['name']].append(c)

    # URL 出典を code -> {src: [urls]} に集める
    urls = collections.defaultdict(lambda: collections.defaultdict(list))
    for c in cur:
        for u in wd.get(c, {}).get('urls', {}): urls[c]['wikidata'].append(u)
    wd_host = lambda c: {host_key(u) for u in urls[c]['wikidata']}

    unresolved = {'egov': [], 'jlis': []}
    def resolve(name, url, src):
        cands = name_to_codes.get(name, [])
        if len(cands) > 1:
            h = host_key(url)
            cands = [c for c in cands if h in wd_host(c)]
        if len(cands) == 1: return cands[0]
        unresolved[src].append((name, url)); return None
    egov_local = anchors('egov-local-governments.html')
    for n, u in egov_local:
        if n == '全国自治体マップ検索': continue
        c = resolve(n, u, 'egov')
        if c: urls[c]['egov'].append(u)
    jlis = load_jlis(manifest)
    jlis_date = {}
    for n, u, d in jlis:
        c = resolve(n, u, 'jlis')
        if c: urls[c]['jlis'].append(u); jlis_date.setdefault(c, d)

    ledger, review = [], []
    tally = collections.Counter()

    def typ(c, e):
        if e['is_pref']: return '都道府県'
        if c in wards: return '行政区'
        if e['pref'] == '東京都' and e['name'].endswith('区'): return '特別区'
        return {'市': '市', '区': '区', '町': '町', '村': '村'}.get(e['name'][-1], 'その他')

    for c, e in sorted(cur.items()):
        w = wd.get(c, {})
        t = typ(c, e)
        srcs = {k: v for k, v in urls[c].items() if v}
        hosts = {k: {host_key(u) for u in v} for k, v in srcs.items()}
        agree = [(a, b) for a in hosts for b in hosts if a < b and hosts[a] & hosts[b]]
        wd_note = ''
        if w.get('end'): wd_note = f'Wikidata では {w["end"]} に終了扱い（総務省 {soumu_asof} 時点の名簿には現存）'
        if w.get('ja') and w['ja'] != e['name'] and not e['name'].endswith(w['ja']):
            wd_note = (wd_note + ' ' if wd_note else '') + f'名称相違(総務省={e["name"]}/Wikidata={w["ja"]})'
        if agree:
            used = sorted({s for p in agree for s in p}, key=['wikidata', 'egov', 'jlis'].index)
            common = set.intersection(*[hosts[s] for s in used]) if len(used) > 1 else set()
            h = sorted(hosts[used[0]] & hosts[used[1]])[0]
            pick = None
            for s in ('egov', 'jlis', 'wikidata'):  # 掲載用 URL は手で整えられたリンク集を優先
                for u in srcs.get(s, []):
                    if host_key(u) == h: pick = clean_url(u); break
                if pick: break
            tag = ';'.join(f'{ {"wikidata": "wikidata", "egov": "e-gov", "jlis": "j-lis"}[s] }@{ {"wikidata": fetch_day("wikidata-p429.csv"), "egov": fetch_day("egov-local-governments.html"), "jlis": jlis_date.get(c) or fetch_day("jlis-000.html")}[s] }' for s in used)
            tally['confirmed'] += 1
            ledger.append({'code': c, 'name_ja': e['name'], 'name_en': w.get('en', ''), 'reading_kana': e['kana'], 'type': t,
                           'prefecture': e['pref'], 'url': pick, 'host': h, 'domain_class': domain_class(h), 'sources': tag,
                           'status': 'active', 'status_date': '', 'successor_code': '', 'note': wd_note})
        else:
            if len(srcs) >= 2: reason = 'mismatch'
            elif len(srcs) == 1: reason = 'single:' + next(iter(srcs))
            else: reason = 'none'
            tally[reason.replace(':', '_')] += 1
            hs = {h for v in hosts.values() for h in v}
            review.append({'code': c, 'name_ja': e['name'], 'type': t, 'prefecture': e['pref'], 'reason': reason,
                           'wikidata_urls': ' '.join(srcs.get('wikidata', [])), 'egov_url': ' '.join(srcs.get('egov', [])),
                           'jlis_url': ' '.join(srcs.get('jlis', [])),
                           'domain_class': domain_class(sorted(hs)[0]) if hs else '', 'wikidata_name': w.get('ja', ''), 'note': wd_note})

    # 廃止・合併の履歴
    cur_codes = set(cur)
    hist = 0
    for c, k in sorted(kaisei.items()):
        if c in cur_codes: continue
        w = wd.get(c, {})
        succ = []
        m = re.search(r'合併後は(.+)$', k['reason'] or '')
        if m:  # 「合併後は袋井市」: 同じ都道府県の現行名簿から名前で引く
            succ = [x for x in name_to_codes.get(m[1].strip(), []) if cur[x]['pref'] == k['pref']]
        if not succ:  # 同じ改正ブロックで新設された団体のうち現行名簿にあるもの
            succ = [x for x in k['created'] if x in cur_codes and cur[x]['pref'] == k['pref']]
        succ = sorted(set(succ))
        merged = '合併' in (k['reason'] or '')
        srcs = ['soumu-kaisei@' + fetch_day('soumu-kaisei.xlsx')] + (['wikidata@' + fetch_day('wikidata-p429.csv')] if w.get('end') else [])
        ledger.append({'code': c, 'name_ja': k['name'], 'name_en': w.get('en', ''), 'reading_kana': k['kana'], 'type': '',
                       'prefecture': k['pref'] or '', 'url': '', 'host': '', 'domain_class': '', 'sources': ';'.join(srcs),
                       'status': 'merged' if merged else 'abolished', 'status_date': k['date'] or '',
                       'successor_code': '|'.join(succ), 'note': (k['reason'] or '') + ('' if succ else ' / 後継団体コード未特定（要確認）')})
        hist += 1
    tally['history_rows'] = hist

    # 国の機関・独立行政法人（e-Gov を第 1 出典、Wikidata の .go.jp 公式サイトのホスト逆引きを第 2 出典）
    sect = [('egov-ministries-and-agencies.html', '国の機関'), ('egov-parliament-and-court.html', '国の機関'), ('egov-IAAs.html', '独立行政法人')]
    for f, t in sect:
        for n, u in anchors(f):
            h = host_key(u)
            ident = 'nat:' + hashlib.sha1((t + n).encode()).hexdigest()[:8]
            labels = sorted(wd_gojp.get(h, []))
            if labels:
                tally['national_confirmed'] += 1
                ledger.append({'code': ident, 'name_ja': n, 'name_en': '', 'reading_kana': '', 'type': t, 'prefecture': '',
                               'url': clean_url(u), 'host': h, 'domain_class': domain_class(h),
                               'sources': f'e-gov@{fetch_day(f)};wikidata@{fetch_day("wikidata-gojp.csv")}', 'status': 'active',
                               'status_date': '', 'successor_code': '', 'note': 'Wikidata 側の項目: ' + ' / '.join(labels[:3])})
            else:
                tally['national_single'] += 1
                review.append({'code': ident, 'name_ja': n, 'type': t, 'prefecture': '', 'reason': 'single:egov', 'wikidata_urls': '',
                               'egov_url': u, 'jlis_url': '', 'domain_class': domain_class(h), 'wikidata_name': '', 'note': ''})

    # Wikidata にあるが総務省の現行名簿に無い団体コード（新設の候補・コード改定）
    extra = [c for c, w in wd.items() if c not in cur_codes and c not in kaisei and not w.get('end')]
    for c in sorted(extra):
        w = wd[c]
        review.append({'code': c, 'name_ja': w['ja'], 'type': '', 'prefecture': '', 'reason': 'wikidata-only',
                       'wikidata_urls': ' '.join(w['urls']), 'egov_url': '', 'jlis_url': '', 'domain_class': '', 'wikidata_name': w['ja'],
                       'note': '総務省の名簿(%s)に無い。新設・コード改定の可能性' % soumu_asof})
    tally['wikidata_only'] = len(extra)

    for name, rows, fields in (('ledger.csv', ledger, FIELDS), ('review.csv', review, REVIEW_FIELDS)):
        with open(ROOT / name, 'w', encoding='utf-8', newline='') as f:
            wr = csv.DictWriter(f, fieldnames=fields, lineterminator='\n'); wr.writeheader()
            for r in sorted(rows, key=lambda r: r['code']): wr.writerow(r)
    print('現行名簿(総務省 %s): %d 団体' % (soumu_asof, len(cur)))
    for k, v in sorted(tally.items()): print(f'  {k}: {v}')
    print('  unresolved e-gov:', len(unresolved['egov']), unresolved['egov'][:5])
    print('  unresolved j-lis(名簿外/曖昧):', len(unresolved['jlis']))
    print(f'ledger.csv {len(ledger)} 行 / review.csv {len(review)} 行')

if __name__ == '__main__': main()
