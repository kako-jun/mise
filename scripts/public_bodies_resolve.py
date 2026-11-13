#!/usr/bin/env python3
"""要確認(review.csv)の mismatch を、HTTP リダイレクトで解消できるものだけ台帳に昇格する(Issue #37 第2段)。

考え方: 出典 A の URL にアクセスして、リダイレクトの行き先ホストが別の出典 B の URL のホストと一致すれば、
A と B は同じサイトを指している(旧ドメインから新ドメインへの移行)。この「リダイレクトで確認」を第3の証拠として、
B のホストで台帳に載せる。行き先が一致しないもの(出典の誤り・別サイト)は要確認のまま。
LLM は使わない。人が決めるのは「この規則を使うか」だけで、個々の団体は判定しない。

  uv run python3 scripts/public_bodies_resolve.py fetch   # review.csv の mismatch の出典URLへアクセスし evidence/redirect-check.csv に記録(ネットワーク)
  uv run python3 scripts/public_bodies_resolve.py apply   # evidence を読んで review.csv から ledger.csv へ昇格(ネットワークなし・冪等)
  uv run python3 scripts/public_bodies_resolve.py national # 国の機関・独立行政法人の第2出典を URL(パス込み)単位で照合し直す(ネットワークなし・冪等)

build を再実行すると ledger/review は出典だけから作り直されるので、build の後に必ず apply をかける。
"""
import csv, datetime, json, pathlib, subprocess, sys, time, urllib.parse
from public_bodies_common import host_key, clean_url, url_key
import public_bodies_fetch as F

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies'
EVID = ROOT / 'evidence' / 'redirect-check.csv'
EVID_FIELDS = ['code', 'source', 'from_url', 'robots', 'http_status', 'final_url', 'checked_at']
SRC_NAME = {'wikidata': 'wikidata', 'egov': 'e-gov', 'jlis': 'j-lis'}
MAX_REDIRS = 5

def review_rows():
    return list(csv.DictReader(open(ROOT / 'review.csv', encoding='utf-8')))

def candidates(r):
    """review 1 行の URL 出典 -> [(source, url)]。"""
    out = []
    for key, src in (('wikidata_urls', 'wikidata'), ('egov_url', 'egov'), ('jlis_url', 'jlis')):
        out += [(src, u) for u in r[key].split() if u]
    return out

def fetch_one(url):
    cmd = ['curl', '--silent', '--show-error', '--location', '--max-redirs', str(MAX_REDIRS), '--max-time', '30', '--max-filesize', '3000000',
           '--user-agent', F.UA, '--output', '/dev/null', '--write-out', '%{http_code} %{url_effective}', url]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        return 0, ''
    code, _, final = out.partition(' ')
    return int(code), final

def do_fetch():
    rows = [r for r in review_rows() if r['reason'] == 'mismatch']
    done = {}
    if EVID.exists():
        for e in csv.DictReader(open(EVID, encoding='utf-8')): done[(e['code'], e['from_url'])] = e
    out = []
    for r in rows:
        for src, u in candidates(r):
            if (r['code'], u) in done: out.append(done[(r['code'], u)]); continue
            ok = F.allowed(u)
            code, final = fetch_one(u) if ok else (0, '')
            if ok: time.sleep(1.5)
            out.append({'code': r['code'], 'source': src, 'from_url': u, 'robots': 'allowed' if ok else 'disallowed',
                        'http_status': code, 'final_url': final, 'checked_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
            print(r['name_ja'], src, u, '->', code, final, file=sys.stderr)
    EVID.parent.mkdir(exist_ok=True)
    with open(EVID, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=EVID_FIELDS, lineterminator='\n'); w.writeheader()
        for e in sorted(out, key=lambda e: (e['code'], e['from_url'])): w.writerow(e)

def promotable(r, evid):
    """(昇格先URL, 証拠の出典, 行き先を持つ出典, 確認日) を返す。決められなければ None。
    条件: ある出典 A の URL が HTTP 200 で、行き先ホストが A 自身のホストと違い、かつ別の出典 B のホストと一致する。"""
    cands = candidates(r)
    for e in evid:
        if e['code'] != r['code'] or e['robots'] != 'allowed' or e['http_status'] != '200': continue
        fh, oh = host_key(e['final_url']), host_key(e['from_url'])
        if not fh or fh == oh: continue
        for src, u in cands:
            if src != e['source'] and host_key(u) == fh:
                url = clean_url(u)
                if urllib.parse.urlsplit(e['final_url']).scheme == 'https' and url.startswith('http://'): url = 'https://' + url[len('http://'):]
                return url, e['source'], src, e['checked_at'][:10]
    return None

def do_apply():
    import public_bodies_build as B
    evid = list(csv.DictReader(open(EVID, encoding='utf-8'))) if EVID.exists() else []
    manifest = json.load(open(ROOT / 'sources-fetch.json', encoding='utf-8'))
    day = lambda k: manifest[k]['fetched_at'][:10]
    src_day = {'wikidata': day('wikidata-p429.csv'), 'egov': day('egov-local-governments.html'), 'jlis': day('jlis-000.html')}
    ledger = list(csv.DictReader(open(ROOT / 'ledger.csv', encoding='utf-8')))
    have = {r['code'] for r in ledger}
    review, promoted = [], 0
    for r in review_rows():
        p = promotable(r, evid) if r['reason'] == 'mismatch' and r['code'] not in have else None
        if not p: review.append(r); continue
        url, from_src, to_src, checked = p
        h = host_key(url)
        ledger.append({'code': r['code'], 'name_ja': r['name_ja'], 'name_en': '', 'reading_kana': '', 'type': r['type'], 'prefecture': r['prefecture'],
                       'url': url, 'host': h, 'domain_class': B.domain_class(h),
                       'sources': f'{SRC_NAME[to_src]}@{src_day[to_src]};{SRC_NAME[from_src]}+redirect@{checked}', 'status': 'active',
                       'status_date': '', 'successor_code': '', 'note': f'{SRC_NAME[from_src]} の旧URL({[u for s, u in candidates(r) if s == from_src][0]})が {SRC_NAME[to_src]} のホストへリダイレクトすることを確認'})
        promoted += 1
    for name, rows, fields in (('ledger.csv', ledger, B.FIELDS), ('review.csv', review, B.REVIEW_FIELDS)):
        with open(ROOT / name, 'w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n'); w.writeheader()
            for r in sorted(rows, key=lambda r: r['code']): w.writerow(r)
    print(f'昇格 {promoted} 件 / ledger {len(ledger)} 行 / review {len(review)} 行')

def do_normalize():
    """台帳の掲載候補の URL を clean_url で整え直す(規則を直したあとの再適用。冪等)。"""
    import public_bodies_build as B
    ledger = list(csv.DictReader(open(ROOT / 'ledger.csv', encoding='utf-8'))); n = 0
    for r in ledger:
        if r['status'] != 'active' or not r['url']: continue
        u = clean_url(r['url'])
        if u != r['url']: r['url'], r['host'], r['domain_class'] = u, host_key(u), B.domain_class(host_key(u)); n += 1
    with open(ROOT / 'ledger.csv', 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=B.FIELDS, lineterminator='\n'); w.writeheader()
        for r in sorted(ledger, key=lambda r: r['code']): w.writerow(r)
    print(f'URL を整えた {n} 件')

def do_national():
    """build 済みの台帳に、国の機関・独立行政法人の URL 単位の照合を適用する。
    ledger の URL(パス込み)が evidence/wikidata-gojp.csv の公式サイト(P856)と一致しない行は要確認(single:egov)へ戻す。
    一致する行の note は Wikidata 側の項目名を全部載せ直す。build はすでに同じ規則で動く(raw が無い環境でもこれで揃えられる)。"""
    import public_bodies_build as B
    by_url = {}
    for r in csv.DictReader(open(ROOT / 'evidence' / 'wikidata-gojp.csv', encoding='utf-8')):
        by_url.setdefault(url_key(r['site']), set()).add(r['ja'] or r['item'].rsplit('/', 1)[-1])
    ledger = list(csv.DictReader(open(ROOT / 'ledger.csv', encoding='utf-8')))
    review = list(csv.DictReader(open(ROOT / 'review.csv', encoding='utf-8')))
    keep, demoted = [], 0
    for r in ledger:
        if r['type'] in ('国の機関', '独立行政法人') and r['status'] == 'active' and r['code'].startswith('nat:'):
            labels = sorted(by_url.get(url_key(r['url']), []))
            if not labels:
                review.append({'code': r['code'], 'name_ja': r['name_ja'], 'type': r['type'], 'prefecture': '', 'reason': 'single:egov', 'wikidata_urls': '',
                               'egov_url': r['url'], 'jlis_url': '', 'domain_class': r['domain_class'], 'wikidata_name': '',
                               'note': 'Wikidata に同じ URL(パス込み)の公式サイトを持つ項目が無い(ホストだけの一致は採らない)'})
                demoted += 1; continue
            r['note'] = 'Wikidata 側の項目: ' + ' / '.join(labels)
            if len(labels) == 1 and labels[0] != r['name_ja']:  # 名称は Wikidata の正式名を優先(e-Gov の表記は途切れや前置きの揺れがある)。code は変えない
                r['note'] += f'（e-Gov 表記: {r["name_ja"]}）'; r['name_ja'] = labels[0]
        keep.append(r)
    for name, rows, fields in (('ledger.csv', keep, B.FIELDS), ('review.csv', review, B.REVIEW_FIELDS)):
        with open(ROOT / name, 'w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n'); w.writeheader()
            for r in sorted(rows, key=lambda r: r['code']): w.writerow(r)
    print(f'要確認へ戻した国の機関 {demoted} 件 / ledger {len(keep)} 行 / review {len(review)} 行')

if __name__ == '__main__':
    {'fetch': do_fetch, 'apply': do_apply, 'national': do_national, 'normalize': do_normalize}[sys.argv[1] if len(sys.argv) > 1 else 'apply']()
